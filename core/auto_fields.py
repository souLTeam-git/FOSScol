"""Automatic field suggestions for a product page.

The module parses rendered HTML and proposes campaign fields from structured
data (JSON-LD ``Product``, OpenGraph, microdata) and one or more dynamic
specification blocks.  Specification tables, ``dl`` lists and repeated
label/value blocks are suggested through the existing split-specifications
plugin instead of becoming dozens of static fields.  It is pure Python and does
not touch Playwright, so it can be unit tested.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin

import lxml.html
from lxml import etree

from core.extractor import normalize_text, split_specifications

MAX_SUGGESTIONS = 60
MAX_DYNAMIC_GROUPS = 120

_PRODUCT_TYPES = {"product", "productgroup"}

_ITEMPROP_LABELS = {
    "name": "Название",
    "headline": "Название",
    "price": "Цена",
    "pricecurrency": "Валюта",
    "lowprice": "Цена",
    "highprice": "Цена",
    "description": "Описание",
    "image": "Фото",
    "thumbnailurl": "Фото",
    "sku": "Артикул",
    "mpn": "Артикул",
    "brand": "Бренд",
    "availability": "Наличие",
    "ratingvalue": "Рейтинг",
    "reviewcount": "Отзывы",
}

_HEADER_LABELS = {
    "характеристика",
    "характеристики",
    "параметр",
    "параметры",
    "название",
    "наименование",
    "значение",
    "property",
    "parameter",
    "characteristic",
    "name",
    "value",
}

_PRICE_HINTS = (
    "грн",
    "₴",
    "руб",
    "₽",
    "$",
    "€",
    "usd",
    "uah",
    "цена",
    "стоимость",
    "купить",
    "в корзину",
    "магазин",
    "предложение",
)
_NAV_HINTS = (
    "главная",
    "каталог",
    "меню",
    "навигация",
    "категории",
    "категория",
    "новости",
    "статьи",
    "обзоры",
    "отзывы",
    "показать",
    "ещё",
    "еще",
    "похожие",
    "аксессуары",
)
_SKIP_TAGS = {"script", "style", "noscript", "br", "template"}

_DATA_ATTRIBUTES = (
    "data-testid",
    "data-test",
    "data-qa",
    "data-id",
    "data-name",
    "data-role",
    "itemprop",
    "name",
    "aria-label",
)


@dataclass
class DetectedField:
    """One suggested campaign field or a dynamic specifications block."""

    name: str
    field_type: str
    selector: str
    value: str
    source: str
    strip_tags: bool = False
    kind: str = "field"
    checked: bool = True
    source_field: str = "Характеристики"
    row_selector: str = ""
    name_selector: str = ""
    value_selector: str = ""
    pairs: list[tuple[str, str]] = dataclass_field(default_factory=list)

    @property
    def is_dynamic(self) -> bool:
        return self.kind == "dynamic"

    def as_field(self) -> dict[str, Any]:
        if self.is_dynamic:
            return {
                "name": self.source_field,
                "type": "xpath_outer_html",
                "selector": self.selector,
                "strip_tags": False,
                "value": self.value,
                "source": self.source,
                "kind": "dynamic",
            }
        return {
            "name": self.name,
            "type": self.field_type,
            "selector": self.selector,
            "strip_tags": self.strip_tags,
            "value": self.value,
            "source": self.source,
            "kind": "field",
        }

    def as_plugin(self, source_field: str) -> dict[str, Any]:
        """Return the existing split-specifications plugin configuration."""

        if not self.is_dynamic:
            return {}
        return {
            "split_specs_to_columns": True,
            "specs_source_field": source_field,
            "specs_row_xpath": self.row_selector,
            "specs_name_xpath": self.name_selector,
            "specs_val_xpath": self.value_selector,
        }

    def preview_text(self, limit: int = 5) -> str:
        return " · ".join(f"{name}: {value}" for name, value in self.pairs[:limit])


# ----------------------------------------------------------------------
# Selector helpers
# ----------------------------------------------------------------------
def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    pieces = [f"'{part}'" for part in parts]
    return "concat(" + ", \"'\", ".join(pieces) + ")"


def _as_element(node: Any) -> Any:
    return node if hasattr(node, "tag") and hasattr(node, "getparent") else None


def _unique(document: Any, selector: str) -> bool:
    try:
        return float(document.xpath(f"count({selector})")) == 1.0
    except (etree.XPathError, ValueError, TypeError):
        return False


def _direct_selector(document: Any, element: Any) -> str:
    if element is None:
        return ""
    tag = str(element.tag or "").lower()
    if not tag or tag in {"html", "body"}:
        return ""
    element_id = str(element.get("id") or "").strip()
    if element_id:
        selector = f"//*[@id={_xpath_literal(element_id)}]"
        if _unique(document, selector):
            return selector
    for attribute in _DATA_ATTRIBUTES:
        value = str(element.get(attribute) or "").strip()
        if not value or len(value) > 120:
            continue
        selector = f"//{tag}[@{attribute}={_xpath_literal(value)}]"
        if _unique(document, selector):
            return selector
    classes = [
        token
        for token in str(element.get("class") or "").split()
        if re.fullmatch(r"[A-Za-z_][\w-]{1,40}", token) and not re.search(r"\d{4,}", token)
    ]
    for token in classes:
        literal = _xpath_literal(f" {token} ")
        selector = (
            f"//{tag}[contains(concat(' ', normalize-space(@class), ' '), {literal})]"
        )
        if _unique(document, selector):
            return selector
    return ""


def _segment(element: Any) -> str:
    tag = str(element.tag or "").lower()
    position = 1
    sibling = element.getprevious()
    while sibling is not None:
        if str(sibling.tag or "").lower() == tag:
            position += 1
        sibling = sibling.getprevious()
    return f"{tag}[{position}]"


def build_xpath(document: Any, element: Any, max_ancestor_depth: int = 7) -> str:
    """Build a short, stable XPath for an lxml element."""

    element = _as_element(element)
    if element is None:
        return ""
    direct = _direct_selector(document, element)
    if direct:
        return direct

    ancestor = element.getparent()
    depth = 0
    prefix = ""
    while ancestor is not None and depth < max_ancestor_depth:
        prefix = _direct_selector(document, ancestor)
        if prefix:
            break
        ancestor = ancestor.getparent()
        depth += 1
    if ancestor is None:
        prefix = ""
        ancestor = None

    chain: list[str] = []
    current = element
    while current is not None and current is not ancestor:
        chain.append(_segment(current))
        current = current.getparent()
    relative = "/".join(reversed(chain))
    if prefix:
        return f"{prefix}/{relative}" if relative else prefix
    # No stable anchor: keep the absolute chain short enough to stay readable.
    absolute: list[str] = []
    current = element
    while current is not None:
        tag = str(current.tag or "").lower()
        if tag == "html":
            absolute.append("html")
            break
        if isinstance(current.tag, str):
            absolute.append(_segment(current))
        current = current.getparent()
    return "/" + "/".join(reversed(absolute)) if absolute else ""


def _text(element: Any) -> str:
    try:
        return normalize_text(element.text_content() or "")
    except (ValueError, AttributeError):
        return ""


def _attr(element: Any, *names: str) -> str:
    for name in names:
        value = str(element.get(name) or "").strip()
        if value:
            return value
    return ""


# ----------------------------------------------------------------------
# Structured data
# ----------------------------------------------------------------------
def _walk_json(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _json_objects(document: Any) -> Iterator[dict[str, Any]]:
    for script in document.xpath("//script[@type='application/ld+json']"):
        raw = script.text_content() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        yield from _walk_json(data)


def _is_product(node: dict[str, Any]) -> bool:
    node_type = node.get("@type")
    values: Iterable[Any] = node_type if isinstance(node_type, (list, tuple)) else [node_type]
    return any(str(item or "").strip().lower() in _PRODUCT_TYPES for item in values)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "value", "url", "@value"):
            if key in value:
                return _json_text(value[key])
    if isinstance(value, list):
        for item in value:
            text = _json_text(item)
            if text:
                return text
    return ""


def _json_image(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _json_text(value.get("url") or value.get("contentUrl") or value.get("image"))
    if isinstance(value, list):
        for item in value:
            text = _json_image(item)
            if text:
                return text
    return ""


def _json_offers(product: dict[str, Any]) -> dict[str, Any]:
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return {}
    aggregate = offers.get("priceSpecification")
    if "price" not in offers and isinstance(aggregate, dict):
        merged = dict(aggregate)
        merged.update({key: value for key, value in offers.items() if key not in merged})
        return merged
    return offers


def _regex_for_json(key: str) -> str:
    return rf'"{re.escape(key)}"\s*:\s*"?([^",}}\]]+)'


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------
class _SuggestionList:
    def __init__(self) -> None:
        self.items: list[DetectedField] = []
        self._names: set[str] = set()
        self._selectors: set[str] = set()

    def add(
        self,
        name: str,
        field_type: str,
        selector: str,
        value: str,
        source: str,
        *,
        unique_name: bool = False,
        strip_tags: bool = False,
    ) -> None:
        label = normalize_text(name or "").strip().rstrip(":")
        value = normalize_text(value or "")
        selector = (selector or "").strip()
        if not label or not selector or not value:
            return
        if len(label) > 90 or len(value) > 800:
            return
        if selector in self._selectors:
            return
        key = label.casefold()
        if key in self._names:
            if unique_name:
                return
            suffix = 2
            while f"{label} ({suffix})".casefold() in self._names:
                suffix += 1
            label = f"{label} ({suffix})"
            key = label.casefold()
        if len(self.items) >= MAX_SUGGESTIONS:
            return
        self._names.add(key)
        self._selectors.add(selector)
        self.items.append(
            DetectedField(
                name=label,
                field_type=field_type,
                selector=selector,
                value=value,
                source=source,
                strip_tags=strip_tags,
            )
        )


def _detect_json_ld(document: Any, suggestions: _SuggestionList) -> None:
    for product in _json_objects(document):
        if not _is_product(product):
            continue
        offers = _json_offers(product)
        candidates = (
            ("Название", _json_text(product.get("name")), _regex_for_json("name")),
            (
                "Цена",
                _json_text(offers.get("price") or offers.get("lowPrice")),
                _regex_for_json("price"),
            ),
            (
                "Валюта",
                _json_text(offers.get("priceCurrency")),
                _regex_for_json("priceCurrency"),
            ),
            (
                "Бренд",
                _json_text(product.get("brand")),
                r'"brand"\s*:\s*(?:\{[^}]*?"name"\s*:\s*)?"([^"]+)"',
            ),
            (
                "Артикул",
                _json_text(product.get("sku") or product.get("mpn")),
                r'"(?:sku|mpn)"\s*:\s*"([^"]+)"',
            ),
            (
                "Описание",
                _json_text(product.get("description")),
                _regex_for_json("description"),
            ),
            ("Фото", _json_image(product.get("image")), _regex_for_json("image")),
        )
        for name, value, selector in candidates:
            if value:
                suggestions.add(name, "regex", selector, value, "JSON-LD", unique_name=True)
        # One product is enough: the page usually describes a single card.
        if suggestions.items:
            return


def _detect_open_graph(document: Any, suggestions: _SuggestionList) -> None:
    meta = (
        ("Заголовок", "og:title"),
        ("Описание", "og:description"),
        ("Фото", "og:image"),
        ("Цена", "product:price:amount"),
        ("Валюта", "product:price:currency"),
    )
    for name, prop in meta:
        nodes = document.xpath(
            f"//meta[@property={_xpath_literal(prop)} or @name={_xpath_literal(prop)}]"
        )
        for node in nodes:
            content = _attr(node, "content")
            if content:
                attribute = "property" if str(node.get("property") or "").strip() else "name"
                selector = f"//meta[@{attribute}={_xpath_literal(prop)}]/@content"
                suggestions.add(
                    name, "xpath_attr", selector, content, "OpenGraph", unique_name=True
                )
                break


def _microdata_selector(document: Any, element: Any, field_type: str, attribute: str = "") -> str:
    base = build_xpath(document, element)
    if not base:
        return ""
    if field_type == "xpath_attr":
        if not attribute:
            return ""
        return f"{base}/@{attribute}"
    return base


def _detect_microdata(document: Any, suggestions: _SuggestionList) -> None:
    for element in document.xpath("//*[@itemprop]"):
        itemprop = _attr(element, "itemprop").split()[0] if _attr(element, "itemprop") else ""
        if not itemprop:
            continue
        key = itemprop.casefold()
        name = _ITEMPROP_LABELS.get(key)
        if not name:
            continue
        tag = str(element.tag or "").lower()
        if tag == "img":
            attribute = _attr(element, "data-src", "src")
            if not attribute:
                continue
            selector = _microdata_selector(document, element, "xpath_attr", "data-src" if element.get("data-src") else "src")
            value = attribute
            field_type = "xpath_attr"
        elif tag in {"meta", "link"}:
            attribute = _attr(element, "content", "href")
            if not attribute:
                continue
            selector = _microdata_selector(document, element, "xpath_attr", "content" if element.get("content") else "href")
            value = attribute
            field_type = "xpath_attr"
        else:
            content = _attr(element, "content")
            if content:
                value = content
                selector = _microdata_selector(document, element, "xpath_attr", "content")
                field_type = "xpath_attr"
            else:
                value = _text(element)
                if not value:
                    continue
                selector = _microdata_selector(document, element, "xpath")
                field_type = "xpath"
        if selector:
            suggestions.add(name, field_type, selector, value, "microdata", unique_name=True)


def _is_header_label(name: str) -> bool:
    return normalize_text(name or "").strip().rstrip(":").casefold() in _HEADER_LABELS


def _outer_html(element: Any) -> str:
    try:
        return lxml.html.tostring(element, encoding="unicode")
    except (TypeError, ValueError):
        return ""


def _class_tokens(element: Any) -> list[str]:
    tokens: list[str] = []
    for token in str(element.get("class") or "").split():
        if (
            re.fullmatch(r"[A-Za-z_][\w-]{1,40}", token)
            and not re.search(r"\d{4,}", token)
        ):
            tokens.append(token)
    return tokens


def _class_predicate(token: str) -> str:
    literal = _xpath_literal(f" {token} ")
    return f"contains(concat(' ', normalize-space(@class), ' '), {literal})"


def _selector_count(context: Any, selector: str) -> int:
    try:
        return int(float(context.xpath(f"count({selector})")))
    except (etree.XPathError, TypeError, ValueError):
        return 0


def _repeat_selector(container: Any, rows: list[Any]) -> str:
    """Return a relative XPath selecting one repeated group of rows."""

    if len(rows) < 2:
        return ""
    tag = str(rows[0].tag or "").lower()
    if not tag or tag in _SKIP_TAGS:
        return ""
    # Relative descendant selectors survive the extra html/body wrapper that
    # lxml adds when a fragment is parsed by ``split_specifications``.
    prefix = ".//"

    token_sets = [_class_tokens(row) for row in rows]
    common = [
        token
        for token in token_sets[0]
        if all(token in tokens for tokens in token_sets[1:])
    ]
    candidates: list[str] = []
    for token in common:
        candidates.append(f"{prefix}{tag}[{_class_predicate(token)}]")
    for index, first in enumerate(common):
        for second in common[index + 1 :]:
            candidates.append(
                f"{prefix}{tag}[{_class_predicate(first)} and {_class_predicate(second)}]"
            )
    if not common:
        candidates.append(f"{prefix}{tag}")
    for selector in candidates:
        if _selector_count(container, selector) == len(rows):
            return selector
    for selector in candidates:
        if _selector_count(container, selector) >= len(rows):
            return selector
    return ""


def _looks_like_junk(name: str, value: str) -> bool:
    text = f"{name} {value}".casefold()
    if any(hint in text for hint in _PRICE_HINTS):
        return True
    lowered_name = name.casefold()
    return any(hint in lowered_name for hint in _NAV_HINTS)


def _valid_dynamic_pairs(
    pairs: list[tuple[str, str]],
    source_text: str = "",
) -> bool:
    """Reject navigation, shop-offer and header-only tables."""

    if len(pairs) < 2:
        return False
    names = [name for name, _value in pairs]
    values = [value for _name, value in pairs]
    if len({name.casefold() for name in names}) < max(2, int(len(names) * 0.6)):
        return False
    if len({value.casefold() for value in values}) < 2:
        return False
    junk = sum(1 for name, value in pairs if _looks_like_junk(name, value))
    if junk >= max(2, int(len(pairs) * 0.3)):
        return False
    if all(_is_header_label(name) for name in names):
        return False
    lowered = (source_text or "").casefold()
    price_hits = sum(lowered.count(hint) for hint in _PRICE_HINTS)
    if price_hits >= max(3, len(pairs)):
        return False
    return True


def _pairs_label(count: int) -> str:
    """Russian plural form for the detected pair count."""

    if count % 10 == 1 and count % 100 != 11:
        word = "пара"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "пары"
    else:
        word = "пар"
    return f"{count} {word}"


def _make_dynamic_candidate(
    document: Any,
    source: Any,
    row_selector: str,
    name_selector: str,
    value_selector: str,
    source_label: str,
    title: str,
) -> DetectedField | None:
    """Build one dynamic specifications suggestion and validate its preview."""

    source_html = _outer_html(source)
    if not source_html:
        return None
    plugins = {
        "split_specs_to_columns": True,
        "specs_source_field": "Характеристики",
        "specs_row_xpath": row_selector,
        "specs_name_xpath": name_selector,
        "specs_val_xpath": value_selector,
    }
    try:
        specs = split_specifications(source_html, source_html, plugins)
    except (etree.ParserError, etree.XPathError, ValueError):
        return None
    pairs = [
        (name, value)
        for name, value in specs.items()
        if name.strip() and (value or "").strip()
    ]
    if not _valid_dynamic_pairs(pairs, _text(source)):
        return None
    selector = build_xpath(document, source)
    if not selector:
        return None
    return DetectedField(
        name=title,
        field_type="xpath_outer_html",
        selector=selector,
        value=_pairs_label(len(pairs)),
        source=source_label,
        kind="dynamic",
        checked=False,
        source_field="Характеристики",
        row_selector=row_selector,
        name_selector=name_selector,
        value_selector=value_selector,
        pairs=pairs,
    )


_TABLE_VARIANTS: tuple[tuple[str, str, str], ...] = (
    (".//tr[td[2]]", ".//tr[td[2]]/td[1]", ".//tr[td[2]]/td[2]"),
    (
        ".//tr[*[self::td or self::th][2]]",
        ".//tr[*[self::td or self::th][2]]/*[self::td or self::th][1]",
        ".//tr[*[self::td or self::th][2]]/*[self::td or self::th][2]",
    ),
    (
        ".//tr[td[1] and not(td[2]) and td[1]/*[2]]",
        ".//tr[td[1] and not(td[2]) and td[1]/*[2]]/td[1]/*[1]",
        ".//tr[td[1] and not(td[2]) and td[1]/*[2]]/td[1]/*[2]",
    ),
    (
        ".//tr[td[1] and not(td[2]) and contains(normalize-space(td[1]), ':')]",
        "substring-before(normalize-space(.), ':')",
        "substring-after(normalize-space(.), ':')",
    ),
)


def _table_candidates(document: Any) -> Iterator[DetectedField]:
    for table in document.xpath("//table")[:40]:
        if table.xpath("ancestor::table"):
            continue
        for row_selector, name_selector, value_selector in _TABLE_VARIANTS:
            candidate = _make_dynamic_candidate(
                document,
                table,
                row_selector,
                name_selector,
                value_selector,
                "Таблица",
                "Динамические характеристики (таблица)",
            )
            if candidate is not None:
                yield candidate


def _dl_candidates(document: Any) -> Iterator[DetectedField]:
    for dl in document.xpath("//dl")[:40]:
        if dl.xpath("ancestor::dl"):
            continue
        candidate = _make_dynamic_candidate(
            document,
            dl,
            ".//dt",
            ".//dt",
            ".//dt/following-sibling::dd[1]",
            "Список dl",
            "Динамические характеристики (dl)",
        )
        if candidate is not None:
            yield candidate


def _paired_candidates(document: Any) -> Iterator[DetectedField]:
    groups: dict[tuple[Any, ...], list[Any]] = {}
    for node in document.xpath("//*[count(*)=2]"):
        tag = str(node.tag or "").lower()
        if tag not in {"div", "li", "span", "p", "section"}:
            continue
        parent = node.getparent()
        if parent is None:
            continue
        children = [child for child in node if isinstance(child.tag, str)]
        if len(children) != 2:
            continue
        name = _text(children[0])
        value = _text(children[1])
        if not name or not value or name == value or len(name) > 70:
            continue
        tokens = _class_tokens(node)
        if not tokens:
            continue
        key = (
            parent,
            tag,
            tuple(tokens),
            tuple(str(child.tag or "").lower() for child in children),
        )
        groups.setdefault(key, []).append(node)

    remaining = MAX_DYNAMIC_GROUPS
    for (parent, _tag, _tokens, _child_tags), nodes in groups.items():
        if len(nodes) < 2:
            continue
        remaining -= 1
        if remaining < 0:
            break
        row_selector = _repeat_selector(parent, nodes)
        if not row_selector:
            continue
        candidate = _make_dynamic_candidate(
            document,
            parent,
            row_selector,
            f"{row_selector}/*[1]",
            f"{row_selector}/*[2]",
            "Парный блок",
            "Динамические характеристики (блок)",
        )
        if candidate is not None:
            yield candidate


def _detect_dynamic_fields(document: Any) -> list[DetectedField]:
    """Collect dynamic specs blocks; the richest one is checked by default."""

    candidates = [
        *_table_candidates(document),
        *_dl_candidates(document),
        *_paired_candidates(document),
    ]
    unique: list[DetectedField] = []
    seen: set[Any] = set()
    for candidate in candidates:
        signature = (
            candidate.selector,
            candidate.row_selector,
            candidate.name_selector,
            candidate.value_selector,
        )
        preview_signature = (
            candidate.selector,
            tuple(pair for pair in candidate.pairs[:5]),
        )
        if signature in seen or preview_signature in seen:
            continue
        seen.add(signature)
        seen.add(preview_signature)
        unique.append(candidate)

    priority = {"Таблица": 0, "Список dl": 1, "Парный блок": 2}
    unique.sort(
        key=lambda item: (-len(item.pairs), priority.get(item.source, 3))
    )
    counters: dict[str, int] = {}
    for candidate in unique:
        counters[candidate.name] = counters.get(candidate.name, 0) + 1
        candidate.name = f"{candidate.name} {counters[candidate.name]}"
    for index, candidate in enumerate(unique):
        candidate.checked = index == 0
    return unique


def detect_fields(html: str, page_url: str = "", limit: int = MAX_SUGGESTIONS) -> list[DetectedField]:
    """Return suggested fields for one rendered product page."""

    try:
        document = lxml.html.fromstring(html or "<html></html>")
        if document.getroottree().getroot() is not document:
            document = document.getroottree().getroot()
    except (etree.ParserError, ValueError):
        return []

    suggestions = _SuggestionList()
    _detect_json_ld(document, suggestions)
    _detect_open_graph(document, suggestions)
    _detect_microdata(document, suggestions)
    dynamic = _detect_dynamic_fields(document)

    results: list[DetectedField] = []
    for item in [*suggestions.items, *dynamic][:limit]:
        if item.is_dynamic:
            results.append(item)
            continue
        if page_url:
            attribute = (
                item.selector.rsplit("/@", 1)[-1] if "/@" in item.selector else ""
            ).strip()
            if attribute in {"src", "data-src", "data-original", "data-lazy-src", "href"}:
                item.value = urljoin(page_url, item.value)
            elif attribute == "content" and re.fullmatch(r"/\S*|https?://\S+", item.value):
                item.value = urljoin(page_url, item.value)
        results.append(item)
    return results
