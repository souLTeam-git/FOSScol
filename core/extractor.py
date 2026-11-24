"""Field extraction from a rendered HTML document."""

from __future__ import annotations

import html as html_module
import re
import threading
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable
from urllib.parse import urljoin

import lxml.html
from lxml import etree


@dataclass
class ExtractionOutcome:
    values: dict[str, Any] = dataclass_field(default_factory=dict)
    errors: list[str] = dataclass_field(default_factory=list)


def normalize_text(value: str) -> str:
    """Collapse markup whitespace while keeping readable single spaces."""

    value = value.replace("\xa0", " ").replace("\u2009", " ").replace("\u202f", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\s*\n\s*", " ", value)
    return value.strip()


def html_to_text(value: str) -> str:
    """Convert an HTML fragment to plain text."""

    if not value:
        return ""
    if "<" in value and ">" in value:
        try:
            fragment = lxml.html.fragment_fromstring(value, create_parent="div")
            text = fragment.text_content()
        except (etree.ParserError, ValueError):
            text = re.sub(r"<[^>]+>", " ", value)
    else:
        text = html_module.unescape(value)
    return normalize_text(text)


_XPATH_CACHE_LIMIT = 512
_XPATH_CACHE = threading.local()


def cached_xpath(selector: str) -> etree.XPath:
    """Compile ``selector`` once per thread and reuse it.

    lxml's ``Element.xpath()`` compiles the expression on every call.  Crawl
    workers evaluate the same campaign selectors for every page, so a
    thread-local cache removes that repeated parsing cost without sharing
    compiled objects between threads.
    """

    cache = getattr(_XPATH_CACHE, "cache", None)
    if cache is None:
        cache = {}
        _XPATH_CACHE.cache = cache
    compiled = cache.get(selector)
    if compiled is None:
        compiled = etree.XPath(selector)
        if len(cache) >= _XPATH_CACHE_LIMIT:
            cache.clear()
        cache[selector] = compiled
    return compiled


def is_meaningful_value(value: Any) -> bool:
    """True when a field value contains real data rather than markup/empties."""

    if isinstance(value, str):
        # Markup-only values (an empty container caught by an
        # ``xpath_outer_html`` field) mean "not rendered yet".
        probe = re.sub(r"<[^>]*>", " ", value) if "<" in value else value
        return bool(probe.replace("\xa0", " ").strip())
    return value not in (None, "", [], {}, ())


def _node_value(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, bytes):
        return node.decode("utf-8", errors="replace")
    if hasattr(node, "text_content"):
        try:
            return node.text_content()
        except (ValueError, AttributeError):
            return ""
    return str(node)


_SPEC_HEADER_LABELS = {
    "характеристика",
    "характеристики",
    "характеристики товара",
    "параметр",
    "параметры",
    "показник",
    "показатели",
    "название",
    "наименование",
    "найменування",
    "значение",
    "значення",
    "величина",
    "property",
    "parameter",
    "characteristic",
    "specification",
    "name",
    "value",
}


def _is_header_spec_pair(name: str, value: str) -> bool:
    """True for a table header row such as «Характеристика | Значение»."""

    left = name.strip().rstrip(":").casefold()
    right = value.strip().rstrip(":").casefold()
    return bool(left) and bool(right) and left in _SPEC_HEADER_LABELS and right in _SPEC_HEADER_LABELS


def _first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value and value.strip():
            return value
    return ""


class FieldExtractor:
    """Evaluate all campaign fields against one page."""

    def __init__(self, campaign: dict[str, Any]) -> None:
        self.campaign = campaign
        self.fields: list[dict[str, Any]] = [
            item for item in campaign.get("fields", []) if isinstance(item, dict)
        ]

    def extract(self, html: str, url: str) -> ExtractionOutcome:
        outcome = ExtractionOutcome()
        try:
            document = lxml.html.fromstring(html or "<html></html>")
            if document.getroottree().getroot() is not document:
                document = document.getroottree().getroot()
        except (etree.ParserError, ValueError) as exc:
            outcome.errors.append(f"Не удалось разобрать HTML: {exc}")
            document = None

        for index, field in enumerate(self.fields, start=1):
            name = str(field.get("name") or f"Поле {index}").strip()
            field_type = str(field.get("type") or "xpath").strip().lower()
            selector = str(field.get("selector") or "")
            strip_tags = bool(field.get("strip_tags"))
            try:
                value = self._extract_one(document, field_type, selector, url, html)
                if strip_tags and field_type not in {"xpath_outer_html", "xpath_attr_list"}:
                    value = html_to_text(value)
                else:
                    value = value.strip() if isinstance(value, str) else value
                value = self._apply_replacements(value, field)
                outcome.values[name] = value
            except Exception as exc:  # a broken selector must not stop the page
                outcome.values[name] = ""
                outcome.errors.append(f"{name}: {exc}")
        return outcome

    @staticmethod
    def _apply_replacements(value: Any, field: dict[str, Any]) -> Any:
        if not isinstance(value, str):
            return value
        replacements = field.get("replace_remove") or []
        if not isinstance(replacements, (list, tuple)):
            replacements = [replacements]
        result = value
        for fragment in replacements:
            fragment = str(fragment)
            if fragment:
                result = result.replace(fragment, "")
        return result.strip()

    def _extract_one(
        self,
        document: lxml.html.HtmlElement | None,
        field_type: str,
        selector: str,
        url: str,
        html: str,
    ) -> str:
        if field_type == "spec_url":
            return url
        if field_type == "regex":
            return self._extract_regex(selector, html)
        if document is None:
            return ""
        if field_type == "xpath_outer_html":
            matches = cached_xpath(selector)(document)
            for match in matches:
                if isinstance(match, str):
                    if match.strip():
                        return match
                    continue
                try:
                    return lxml.html.tostring(match, encoding="unicode")
                except (TypeError, ValueError):
                    continue
            return ""
        if field_type in {"xpath", "xpath_attr"}:
            matches = cached_xpath(selector)(document)
            if field_type == "xpath_attr":
                return self._attribute_value(matches, document, selector)
            return _first_non_empty(_node_value(match) for match in matches)
        if field_type == "xpath_attr_list":
            matches = cached_xpath(selector)(document)
            return self._attribute_list(matches, document, selector, url)
        raise ValueError(f"неизвестный тип поля «{field_type}»")

    @classmethod
    def _attribute_list(
        cls,
        matches: list[Any],
        document: lxml.html.HtmlElement | None,
        selector: str,
        url: str,
    ) -> list[str]:
        """Collect every URL-like attribute, preserving document order."""

        elements: list[Any] = []
        raw_values: list[str] = []

        # ``//img/@src`` returns attribute strings, which lose access to the
        # sibling ``data-src``.  Re-evaluate through the parent axis so each
        # gallery image can be inspected as a whole element.
        if matches and all(isinstance(item, (str, bytes)) for item in matches):
            if document is not None and selector.strip():
                try:
                    owners = cached_xpath(f"({selector})/..")(document)
                except (etree.XPathError, ValueError):
                    owners = []
                if owners and all(hasattr(item, "get") for item in owners):
                    elements = owners
                else:
                    raw_values = [
                        item.decode("utf-8", "replace") if isinstance(item, bytes) else item
                        for item in matches
                    ]
        else:
            elements = list(matches)

        values: list[str] = []
        for element in elements:
            value = cls._element_image_url(element)
            if value:
                values.append(value)
        values.extend(raw_values)

        resolved = []
        for value in values:
            text = (value or "").strip()
            if not text:
                continue
            if text.startswith("/"):
                text = urljoin(url, text)
            resolved.append(text)
        return list(dict.fromkeys(resolved))

    @classmethod
    def _element_image_url(cls, element: Any) -> str:
        if not hasattr(element, "get"):
            value = element.decode("utf-8", "replace") if isinstance(element, bytes) else str(element)
            return value if cls._is_usable_attribute(value) else ""
        for attribute in ("data-src", "data-original", "data-lazy-src", "src"):
            value = element.get(attribute)
            if value and cls._is_usable_attribute(value):
                return value
        # ``meta[property=og:image]`` keeps its address in ``content``.
        if str(getattr(element, "tag", "")).lower() == "meta":
            value = element.get("content")
            if value and cls._is_usable_attribute(value):
                return value
        return ""

    @classmethod
    def _attribute_value(
        cls,
        matches: list[Any],
        document: lxml.html.HtmlElement | None = None,
        selector: str = "",
    ) -> str:
        value = cls._first_usable_attribute(matches)
        if value:
            return value

        # ``@src`` may contain an empty string or a base64 placeholder while
        # the real address lives in ``data-src``.  When the selector targets
        # attributes, re-evaluate it against the lazy-load attribute.
        if document is not None and selector:
            for attribute in ("data-src", "data-original", "data-lazy-src"):
                fallback = selector.replace("/@src", f"/@{attribute}")
                if fallback == selector:
                    break
                try:
                    fallback_matches = cached_xpath(fallback)(document)
                except (etree.XPathError, ValueError):
                    continue
                value = cls._first_usable_attribute(fallback_matches)
                if value:
                    return value
        return ""

    @classmethod
    def _first_usable_attribute(cls, matches: Iterable[Any]) -> str:
        for match in matches:
            if isinstance(match, (str, bytes)):
                value = match.decode("utf-8", "replace") if isinstance(match, bytes) else match
                if cls._is_usable_attribute(value):
                    return value
                continue
            for attribute in (
                "src",
                "data-src",
                "data-original",
                "data-lazy-src",
                "href",
                "content",
            ):
                value = match.get(attribute)
                if value and cls._is_usable_attribute(value):
                    return value
        return ""

    @staticmethod
    def _is_usable_attribute(value: str) -> bool:
        text = (value or "").strip()
        if not text:
            return False
        # Inline base64 images are placeholders, not the real gallery URL.
        return not text.lower().startswith("data:image")

    @staticmethod
    def _extract_regex(selector: str, html_text: str) -> str:
        # The regex is applied to the original HTML, not to the lxml tree.
        compiled = re.compile(selector, re.DOTALL)
        match = compiled.search(html_text or "")
        if not match:
            return ""
        if match.groups():
            return match.group(1) or ""
        return match.group(0)


def split_specifications(
    html: str,
    source_html: str,
    plugins: dict[str, Any],
) -> OrderedDict[str, str]:
    """Turn the specification table into a flat ``name -> value`` mapping.

    The configured XPaths usually point at the full document (for example
    ``//tr``, ``//tr/td[1]`` and ``//tr/td[2]``).  The values are zipped by
    row index, which matches the way FOSScol's "split specs" plugin works.
    If the XPaths are row-relative, they are evaluated against each row.
    """

    result: OrderedDict[str, str] = OrderedDict()
    row_xpath = str(plugins.get("specs_row_xpath") or "").strip()
    name_xpath = str(plugins.get("specs_name_xpath") or "").strip()
    value_xpath = str(plugins.get("specs_val_xpath") or "").strip()
    if not row_xpath or not name_xpath or not value_xpath:
        return result

    source = source_html or html
    try:
        document = lxml.html.fromstring(source or "<html></html>")
        if document.getroottree().getroot() is not document:
            document = document.getroottree().getroot()
        rows = cached_xpath(row_xpath)(document)
    except (etree.ParserError, etree.XPathError, ValueError):
        return result
    if not rows:
        return result

    names: list[Any] = []
    values: list[Any] = []
    try:
        names = cached_xpath(name_xpath)(document)
        values = cached_xpath(value_xpath)(document)
    except (etree.XPathError, ValueError):
        names = []
        values = []

    pairs: list[tuple[Any, Any]] = []
    if (
        isinstance(names, list)
        and isinstance(values, list)
        and len(names) == len(rows)
        and len(values) == len(rows)
    ):
        pairs = list(zip(names, values))
    else:
        relative_name = _relative_row_xpath(row_xpath, name_xpath)
        relative_value = _relative_row_xpath(row_xpath, value_xpath)
        for row in rows:
            try:
                row_names = cached_xpath(relative_name)(row) if relative_name else []
                row_values = cached_xpath(relative_value)(row) if relative_value else []
            except (etree.XPathError, ValueError):
                continue
            name_node = _first_xpath_value(row_names)
            value_node = _first_xpath_value(row_values)
            if name_node is not None and value_node is not None:
                pairs.append((name_node, value_node))

    if not pairs:
        return result

    for name_node, value_node in pairs:
        name = _clean_spec_text(name_node).rstrip(":").strip()
        value = _clean_spec_text(value_node)
        if not name or not value:
            continue
        if _is_header_spec_pair(name, value):
            continue
        unique_name = name
        suffix = 2
        while unique_name in result:
            unique_name = f"{name} ({suffix})"
            suffix += 1
        result[unique_name] = value
    return result


def _clean_spec_text(node: Any) -> str:
    """Text of a specs cell without markup, tooltips and section icons."""

    if not hasattr(node, "xpath"):
        text = _node_value(node)
        if "<" in text and ">" in text:
            try:
                fragment = lxml.html.fragment_fromstring(text, create_parent="div")
            except (etree.ParserError, ValueError):
                fragment = None
            if fragment is not None:
                _strip_spec_tooltips(fragment)
                text = fragment.text_content()
            else:
                text = html_to_text(text)
        return normalize_text(text)

    try:
        cleaned = deepcopy(node)
    except (TypeError, ValueError):
        cleaned = node
    _strip_spec_tooltips(cleaned)
    return normalize_text(cleaned.text_content())


def _strip_spec_tooltips(container: Any) -> None:
    try:
        unwanted = cached_xpath(
            ".//*[contains(translate(@class, "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'tooltip')]"
            " | .//script | .//style | .//noscript"
        )(container)
    except (etree.XPathError, ValueError):
        return
    for element in unwanted:
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)


def _first_xpath_value(result: Any) -> Any:
    """First node/string of an XPath result, without lxml truth-testing."""

    if isinstance(result, list):
        return result[0] if result else None
    if isinstance(result, str):
        return result if result.strip() else None
    return result


def _relative_row_xpath(row_xpath: str, xpath: str) -> str:
    """Best-effort conversion of ``//tr/td[1]`` to ``./td[1]``."""

    row = row_xpath.rstrip("/")
    candidate = xpath.strip()
    if candidate == row_xpath:
        return "."
    if candidate.startswith(row + "/"):
        suffix = candidate[len(row) + 1 :]
        if suffix.startswith("/"):
            suffix = suffix.lstrip("/")
        return f"./{suffix}" if suffix else "."
    if candidate.startswith(".//"):
        return candidate
    if candidate.startswith("./"):
        return candidate
    if candidate.startswith(
        (
            "substring-before(",
            "substring-after(",
            "normalize-space(",
            "concat(",
            "translate(",
        )
    ):
        # XPath string functions are evaluated against each row.
        return candidate
    return ""
