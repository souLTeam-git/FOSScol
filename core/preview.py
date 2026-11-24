"""One-page preview used by the campaign editor.

The module opens a single URL in Chromium, waits for the campaign XPaths and
runs :class:`~core.extractor.FieldExtractor` on the result.  It is used by
"Test on 1 URL" and by smart auto-detect, never by the crawl queue, and it
does not emit result rows or touch the storage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field as dataclass_field
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from core.engine import (
    CAPTCHA_MARKERS_JS,
    CAPTCHA_STATUS_CODES,
    DOM_READY_WAIT_JS,
    USER_AGENT,
    normalize_url,
)
from core.extractor import ExtractionOutcome, FieldExtractor
from core.plugins.chain import apply_plugins

PREVIEW_DOM_TIMEOUT_MS = 8000
PREVIEW_IDLE_MS = 2000
PREVIEW_POLL_MS = 150


class PreviewError(RuntimeError):
    """The preview page could not be opened."""


@dataclass
class PageSnapshot:
    requested_url: str
    final_url: str
    html: str
    status: int
    captcha: bool
    title: str
    elapsed: float


@dataclass
class PreviewField:
    name: str
    field_type: str
    selector: str
    value: str
    status: str  # "ok" | "empty" | "error"
    error: str = ""


@dataclass
class PreviewResult:
    snapshot: PageSnapshot
    fields: list[PreviewField] = dataclass_field(default_factory=list)
    errors: list[str] = dataclass_field(default_factory=list)

    @property
    def filled(self) -> int:
        return sum(1 for item in self.fields if item.status == "ok")


def _route_handler(route: Any) -> None:
    try:
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
        else:
            route.continue_()
    except PlaywrightError:
        try:
            route.continue_()
        except PlaywrightError:
            pass


def _field_xpaths(campaign: dict[str, Any]) -> list[str]:
    xpaths: list[str] = []
    for field in campaign.get("fields") or []:
        if not isinstance(field, dict):
            continue
        field_type = str(field.get("type") or "xpath").strip().lower()
        if field_type not in {"xpath", "xpath_outer_html", "xpath_attr", "xpath_attr_list"}:
            continue
        selector = str(field.get("selector") or "").strip()
        if selector and selector not in xpaths:
            xpaths.append(selector)
    return xpaths


def capture_page(
    campaign: dict[str, Any],
    url: str,
    wait_xpaths: list[str] | None = None,
    headless: bool = True,
) -> PageSnapshot:
    """Open ``url`` once and return the rendered HTML and its status."""

    normalized = normalize_url((url or "").strip())
    if not normalized:
        raise PreviewError("Введите корректный http:// или https:// адрес.")
    engine = campaign.get("engine") if isinstance(campaign.get("engine"), dict) else {}
    block_images = bool(engine.get("block_heavy_resources", True)) or bool(
        engine.get("disable_images", True)
    )

    started = time.monotonic()
    playwright = None
    browser = None
    context = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ru-RU",
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        context.set_default_timeout(45000)
        context.set_default_navigation_timeout(60000)
        if block_images:
            context.route("**/*", _route_handler)
        page = context.new_page()
        response = page.goto(normalized, wait_until="domcontentloaded", timeout=60000)
        status = int(response.status) if response is not None else 0

        xpaths = [item for item in (wait_xpaths or []) if str(item).strip()]
        if xpaths:
            try:
                page.wait_for_function(
                    DOM_READY_WAIT_JS,
                    arg={
                        "xpaths": xpaths,
                        "token": str(time.monotonic_ns()),
                        "minStable": 2,
                        "minWait": 1200,
                        "pendingWait": 2500,
                        "minMatched": 0,
                        "requireAll": False,
                    },
                    timeout=PREVIEW_DOM_TIMEOUT_MS,
                    polling=PREVIEW_POLL_MS,
                )
            except (PlaywrightTimeoutError, PlaywrightError):
                pass
        else:
            page.wait_for_timeout(PREVIEW_IDLE_MS)

        try:
            markers = page.evaluate(CAPTCHA_MARKERS_JS)
        except (PlaywrightError, ValueError, TypeError):
            markers = []
        captcha = status in CAPTCHA_STATUS_CODES or bool(markers)
        try:
            title = page.title()
        except (PlaywrightTimeoutError, PlaywrightError):
            title = ""
        html = page.content()
        final_url = page.url or normalized
        return PageSnapshot(
            requested_url=normalized,
            final_url=final_url,
            html=html,
            status=status,
            captcha=captcha,
            title=title,
            elapsed=time.monotonic() - started,
        )
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        message = str(exc).strip().splitlines()
        raise PreviewError(message[0] if message else exc.__class__.__name__) from exc
    finally:
        if context is not None:
            try:
                context.close()
            except PlaywrightError:
                pass
        if browser is not None:
            try:
                browser.close()
            except PlaywrightError:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


def preview_fields(
    campaign: dict[str, Any],
    url: str,
    headless: bool = True,
) -> PreviewResult:
    """Extract the campaign fields from one page without a crawl queue."""

    snapshot = capture_page(
        campaign,
        url,
        wait_xpaths=_field_xpaths(campaign),
        headless=headless,
    )
    extractor = FieldExtractor(campaign)
    outcome: ExtractionOutcome = extractor.extract(snapshot.html, snapshot.final_url)
    # Match the crawl engine: image attributes are reported as absolute URLs.
    image_names = {
        str(field.get("name") or "").strip()
        for field in campaign.get("fields") or []
        if isinstance(field, dict)
        and str(field.get("type") or "").strip().lower() in {"xpath_attr", "xpath_attr_list"}
        and any(
            token in str(field.get("selector") or "").lower()
            for token in ("@src", "@data-src")
        )
    }
    for name in image_names:
        value = outcome.values.get(name)
        if isinstance(value, str) and value.strip().startswith("/"):
            outcome.values[name] = urljoin(snapshot.final_url, value.strip())
        elif isinstance(value, list):
            outcome.values[name] = [
                urljoin(snapshot.final_url, str(item).strip())
                if str(item).strip().startswith("/")
                else item
                for item in value
            ]

    plugin_errors: list[str] = []
    values = apply_plugins(dict(outcome.values), campaign, errors=plugin_errors)
    error_by_name: dict[str, str] = {}
    for message in outcome.errors:
        name, _, detail = message.partition(":")
        error_by_name[name.strip()] = detail.strip() or message

    fields: list[PreviewField] = []
    static_names: set[str] = set()
    for index, field in enumerate(campaign.get("fields") or [], start=1):
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or f"Поле {index}").strip()
        static_names.add(name)
        value = values.get(name, "")
        text = value if isinstance(value, str) else str(value)
        if text.strip():
            status = "ok"
            error = ""
        elif name in error_by_name:
            status = "error"
            error = error_by_name[name]
        else:
            status = "empty"
            error = ""
        fields.append(
            PreviewField(
                name=name,
                field_type=str(field.get("type") or "xpath"),
                selector=str(field.get("selector") or ""),
                value=text,
                status=status,
                error=error,
            )
        )

    # Plugin-generated columns (specs, list normalisation, custom plugins) are
    # not backed by a single field selector, so show them explicitly.
    for name, value in values.items():
        if name.startswith("__") or name in static_names:
            continue
        text = value if isinstance(value, str) else str(value)
        fields.append(
            PreviewField(
                name=str(name),
                field_type="specs",
                selector="",
                value=text,
                status="ok" if text.strip() else "empty",
            )
        )
    return PreviewResult(
        snapshot=snapshot,
        fields=fields,
        errors=list(outcome.errors) + plugin_errors,
    )
