"""Playwright based crawling and scraping engine.

The engine is independent from Qt: it can be used from a script, from a
worker thread or from the desktop application.

Playwright's synchronous API is strictly thread-affine: objects created in
one thread cannot be used in another.  The engine therefore launches a single
Chromium in the thread that calls :meth:`CrawlEngine.run` and lets every
worker connect to it over the DevTools protocol, opening its own tab in the
shared browser context.  Campaigns that assign a different proxy to every
worker fall back to one dedicated Chromium per worker, because Chromium
applies proxies per browser context.
"""

from __future__ import annotations

import json
import queue
import random
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import lxml.html
from lxml import etree
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    ProxySettings,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from core.extractor import (
    ExtractionOutcome,
    FieldExtractor,
    cached_xpath,
    is_meaningful_value,
)
from core.plugins.chain import apply_plugins, resolve_plugins

LogCallback = Callable[[str, str], None]
ResultCallback = Callable[[dict[str, Any]], None]
ProgressCallback = Callable[[int, int], None]
PauseCallback = Callable[[bool, str], None]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# The shared browser is started by the thread that calls ``run()`` and is
# reached by the workers over the DevTools protocol (the sync Playwright API
# cannot share objects between threads).  This is how long we wait for the
# debugging endpoint to come up.
CDP_READY_TIMEOUT_SECONDS = 20.0
CDP_POLL_SECONDS = 0.15
STEALTH_INIT_SCRIPT = r"""
(() => {
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => false });
  } catch (error) { /* ignore */ }
  try {
    if (!window.chrome) {
      window.chrome = { runtime: {} };
    }
  } catch (error) { /* ignore */ }
})();
"""

IMAGE_WAIT_TIMEOUT_MS = 3000
DOM_READY_TIMEOUT_MS = 4000
DOM_READY_MIN_MS = 2500
DOM_READY_PENDING_MS = 3200
DOM_READY_STABLE_TICKS = 3
DOM_RETRY_WAIT_MS = 1800
FIELD_RETRY_RATIO = 0.5
FIELD_RETRY_TIMEOUT_RATIO = 0.75
CAPTCHA_WAIT_SECONDS = 120
CAPTCHA_POLL_SECONDS = 1.0
CAPTCHA_CLEAR_TICKS = 2
CAPTCHA_MAX_ATTEMPTS = 2
CAPTCHA_STATUS_CODES = frozenset({403, 429, 503})
CAPTCHA_HEADER_NAMES = ("cf-mitigated", "x-datadome")
HEAVY_RESOURCE_TYPES = frozenset({"image", "media", "font"})
TRACKER_HOST_MARKERS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com",
    "facebook.net",
    "hotjar.com",
    "hotjar.io",
    "clarity.ms",
)
CAPTCHA_MARKERS_JS = r"""
() => {
  const selectors = [
    '.cf-turnstile',
    '.g-recaptcha',
    '.h-captcha',
    '#challenge-form',
    '#cf-wrapper',
    '[data-sitekey]',
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="recaptcha"]',
  ];
  const found = [];
  for (const selector of selectors) {
    try {
      if (document.querySelector(selector)) found.push(selector);
    } catch (error) {
      /* a broken selector must not break detection */
    }
  }
  try {
    const marker = '/cdn-cgi/challenge-platform/';
    let hasChallenge = false;
    // Inspect only the handful of elements that carry the challenge URL
    // instead of serialising the whole document into a string.
    if (document.querySelector(
      'script[src*="/cdn-cgi/challenge-platform/"], ' +
      'iframe[src*="/cdn-cgi/challenge-platform/"]'
    )) {
      hasChallenge = true;
    }
    if (!hasChallenge && document.scripts) {
      for (const script of document.scripts) {
        const src = script.getAttribute('src') || script.src || '';
        const body = script.textContent || '';
        if (src.includes(marker) || body.includes(marker)) {
          hasChallenge = true;
          break;
        }
      }
    }
    if (hasChallenge) {
      found.push(marker);
    }
  } catch (error) {
    /* ignore */
  }
  return found;
}
"""
DOM_READY_WAIT_JS = r"""
(payload) => {
  const xpaths = (payload && payload.xpaths) || [];
  const token = payload ? payload.token : '';
  const minStable = payload && payload.minStable ? payload.minStable : 3;
  const minWait = payload && payload.minWait ? payload.minWait : 2500;
  const pendingWait = payload && payload.pendingWait ? payload.pendingWait : 3200;
  if (!document.body) return false;
  let state = window.__fosscolDomReady;
  if (!state || state.token !== token) {
    state = window.__fosscolDomReady = {
      token: token,
      last: '',
      stable: 0,
      start: performance.now(),
    };
  }
  // A selector "matches" only when it yields real content: an empty table
  // container that is still waiting for Vue/JS rows must not count as ready.
  // Such containers are tracked as "pending" and keep the wait alive for a
  // while, because their rows are exactly what a throttled tab renders late.
  let matched = 0;
  let pending = 0;
  for (const xpath of xpaths) {
    try {
      const snapshot = document.evaluate(
        xpath,
        document,
        null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
        null
      );
      let hasText = false;
      for (let index = 0; index < snapshot.snapshotLength; index += 1) {
        const node = snapshot.snapshotItem(index);
        const value = node ? (node.textContent || '') : '';
        if (value.trim()) {
          hasText = true;
          break;
        }
      }
      if (hasText) {
        matched += 1;
      } else if (snapshot.snapshotLength > 0) {
        pending += 1;
      }
    } catch (error) {
      /* a broken selector must not block the wait */
    }
  }
  const elementCount = document.getElementsByTagName('*').length;
  const textLength = (document.body.innerText || '').length;
  const signature = `${matched}:${textLength}`;
  if (signature === state.last) {
    state.stable += 1;
  } else {
    state.last = signature;
    state.stable = 0;
  }
  const total = xpaths.length;
  const allFields = total > 0 && matched >= total;
  const stable = state.stable >= minStable;
  const enoughContent = textLength >= 50 || elementCount >= 50;
  const elapsed = performance.now() - state.start;
  // Stub/blocked pages never grow: stop waiting instead of burning the full
  // timeout on every such URL during mass runs.  A page that already exposes
  // campaign fields is not "empty" even when it is small.
  const emptyPage =
    elapsed > 1500 && matched < 2 && elementCount < 150 && textLength < 100;
  // How many campaign XPaths must match before the wait may finish: at least
  // two when the campaign has them, never fewer than the previous pass saw.
  const floor = Math.min(2, total);
  const baseline = payload && payload.minMatched > 0 ? payload.minMatched : 0;
  const needAll = !!(payload && payload.requireAll);
  const required = Math.min(total, Math.max(floor, baseline, needAll ? total : 0));
  const fieldReady = total > 0 && matched >= required;
  // A short grace period stops the wait from finishing during a brief pause
  // in rendering (throttled tab) before late blocks such as spec tables load.
  const graceElapsed = elapsed >= minWait;
  const pendingSettled = pending === 0 || elapsed >= pendingWait;
  const ready =
    allFields ||
    (stable && fieldReady && graceElapsed && pendingSettled) ||
    (stable && total === 0 && enoughContent) ||
    emptyPage;
  if (!ready) return false;
  return {
    matched: matched,
    pending: pending,
    total: total,
    stable: stable,
    elements: elementCount,
    text: textLength,
    empty: emptyPage,
  };
}
"""
IMAGE_XPATH_WAIT_JS = r"""
(imageXpaths) => {
  if (!Array.isArray(imageXpaths) || imageXpaths.length === 0) {
    return { ready: 0, total: 0, skipped: true };
  }
  const isRealUrl = (raw) => {
    const value = (raw || '').trim();
    if (!value) return false;
    if (/^(?:data:image|blob:|about:|javascript:|#)/i.test(value)) return false;
    if (/(?:^|[\s/_.?&=-])(?:placeholder|no[-_]?image|noimage|spacer|blank|transparent|dummy|1x1|pixel)(?:[\s/_.?&=-]|$)/i.test(value)) {
      return false;
    }
    return true;
  };
  const elementUrl = (element) => {
    for (const attribute of ['data-src', 'data-original', 'src']) {
      const value = element.getAttribute(attribute);
      if (isRealUrl(value)) return value.trim();
    }
    return '';
  };
  const elements = new Set();
  const urls = new Set();
  for (const xpath of imageXpaths) {
    let snapshot;
    try {
      snapshot = document.evaluate(
        xpath,
        document,
        null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
        null
      );
    } catch (error) {
      continue;
    }
    for (let index = 0; index < snapshot.snapshotLength; index += 1) {
      const node = snapshot.snapshotItem(index);
      if (!node) continue;
      let element = null;
      if (node.nodeType === Node.ELEMENT_NODE) {
        element = node;
      } else if (node.nodeType === Node.ATTRIBUTE_NODE) {
        element = node.ownerElement;
        const name = (node.name || '').toLowerCase();
        if (['src', 'data-src', 'data-original', 'content'].includes(name) && isRealUrl(node.value)) {
          urls.add(node.value.trim());
        }
      }
      if (!element) continue;
      elements.add(element);
      const value = elementUrl(element);
      if (value) urls.add(value);
    }
  }
  if (elements.size === 0) return false;
  const needed = Math.min(2, elements.size);
  return urls.size >= needed
    ? { ready: urls.size, total: elements.size, skipped: false }
    : false;
}
"""


class CrawlError(RuntimeError):
    """Base class for crawl related failures."""


class ScenarioError(CrawlError):
    """A mandatory scenario step could not be executed."""


class PageLoadError(CrawlError):
    """The page returned an HTTP error or could not be opened."""


class CaptchaBlockedError(CrawlError):
    """A captcha/anti-bot wall could not be passed after several attempts."""


@dataclass
class CrawlSummary:
    started_at: datetime
    finished_at: datetime
    total_urls: int
    success: int
    errors: int
    stopped: bool
    worker_errors: list[str] = dataclass_field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_urls": self.total_urls,
            "success": self.success,
            "errors": self.errors,
            "stopped": self.stopped,
            "duration_seconds": self.duration_seconds,
            "worker_errors": list(self.worker_errors),
        }


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Convert a link to a canonical absolute http(s) URL."""

    value = (url or "").strip()
    if not value or value.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    if base:
        value = urljoin(base, value)
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    netloc = parts.netloc
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    if port and not ((parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def host_key(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def parse_proxy(raw: str) -> ProxySettings | None:
    """Parse ``host:port`` or ``scheme://user:pass@host:port``."""

    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"http://{value}"
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise CrawlError(f"Некорректный прокси «{raw}»: {exc}") from exc
    if not parts.hostname:
        raise CrawlError(f"Некорректный прокси «{raw}»: не указан хост.")
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    proxy: ProxySettings = {"server": f"{parts.scheme or 'http'}://{host}"}
    if parts.username:
        proxy["username"] = parts.username
    if parts.password:
        proxy["password"] = parts.password
    return proxy


class CrawlEngine:
    """Breadth-first crawl over start URLs with configurable extraction."""

    def __init__(
        self,
        campaign: dict[str, Any],
        on_log: LogCallback | None = None,
        on_result: ResultCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_pause: PauseCallback | None = None,
    ) -> None:
        self.campaign = campaign
        self._on_log = on_log or (lambda level, message: None)
        self._on_result = on_result or (lambda row: None)
        self._on_progress = on_progress or (lambda done, total: None)
        self._on_pause = on_pause or (lambda paused, reason: None)

        engine = campaign.get("engine") if isinstance(campaign.get("engine"), dict) else {}
        self.threads = max(1, min(32, int(engine.get("threads", 3) or 3)))
        self.headless = bool(engine.get("headless", False))
        self.disable_images = bool(engine.get("disable_images", True))
        self.block_heavy_resources = bool(engine.get("block_heavy_resources", True))
        self.delay_min = max(0, int(engine.get("delay_min_ms", 0) or 0)) / 1000.0
        self.delay_max = max(self.delay_min, int(engine.get("delay_max_ms", 0) or 0) / 1000.0)
        self.proxies = [line.strip() for line in str(engine.get("proxy_list") or "").splitlines() if line.strip()]

        navigation = campaign.get("navigation") if isinstance(campaign.get("navigation"), dict) else {}
        self.gather_xpaths = [str(item) for item in navigation.get("gather_links_xpaths", []) if str(item).strip()]
        self.max_depth = max(0, int(navigation.get("max_depth", 0) or 0))

        self.scenario = [
            step for step in campaign.get("scenario", []) if isinstance(step, dict)
        ]
        self.extractor = FieldExtractor(campaign)
        self.fields = [item for item in campaign.get("fields", []) if isinstance(item, dict)]
        # ``xpath_attr``/``xpath_attr_list`` fields that read image attributes
        # must see the real DOM values.  Blocking images prevents gallery
        # lazy-loaders from writing the final URL into ``src``/``data-src``.
        self.image_fields = [
            item
            for item in self.fields
            if str(item.get("type") or "").strip().lower()
            in {"xpath_attr", "xpath_attr_list"}
            and any(
                token in str(item.get("selector") or "").lower()
                for token in ("@src", "@data-src")
            )
        ]
        self.needs_images = bool(self.image_fields)
        self.image_xpaths = list(
            dict.fromkeys(
                str(field.get("selector") or "").strip()
                for field in self.image_fields
                if str(field.get("selector") or "").strip()
            )
        )
        # XPath selectors of every DOM-based field: the DOM readiness wait
        # checks how many of them already match before taking ``page.content``.
        self.field_xpaths = list(
            dict.fromkeys(
                str(field.get("selector") or "").strip()
                for field in self.fields
                if str(field.get("type") or "").strip().lower()
                in {"xpath", "xpath_outer_html", "xpath_attr", "xpath_attr_list"}
                and str(field.get("selector") or "").strip()
            )
        )
        self.plugin_runs = resolve_plugins(campaign)

        self._queue: queue.Queue[tuple[str, int] | None] = queue.Queue()
        self._seen: set[str] = set()
        self._allowed_hosts: set[str] = set()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending = 0
        self._total = 0
        self._success = 0
        self._errors = 0
        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []
        self._worker_errors: list[str] = []
        self._started_at = datetime.now()

        # Pause state is shared by every worker: the parser waits on
        # ``_pause_event`` between pages and before its heavier steps.
        self._pause_lock = threading.Lock()
        self._pause_reasons: set[str] = set()
        self._pause_event = threading.Event()
        self._captcha_lock = threading.Lock()
        self._captcha_attempts: dict[str, int] = {}
        self._last_pause_state: tuple[bool, str] = (False, "")

        # Cookies solved/received in one tab are shared with every other tab
        # so a passed captcha or login does not have to be repeated per worker.
        self._cookies_lock = threading.Lock()
        self._cookies: list[dict[str, Any]] = []
        self._cookies_version = 0
        self._cookie_seen: dict[int, int] = {}

        # Heavy resources are blocked per tab.  While the user passes a
        # captcha the image block is lifted for that tab only, so the
        # challenge images become visible there and other workers keep their
        # normal blocking.
        self._start_hosts: set[str] = set()
        self._image_block_lock = threading.Lock()
        self._relaxed_pages: set[int] = set()

        # Shared-browser state.  In the common case (no proxies or one shared
        # proxy) ``run()`` launches a single Chromium and every worker opens
        # its own tab in the default context.  With several proxies each
        # worker still launches its own browser, because Chromium applies a
        # proxy per browser context (and each context is a separate window).
        self._shared_mode = False
        self._cdp_url = ""
        self._host_browser: Browser | None = None
        self._host_playwright: Playwright | None = None

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def stop(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()
            self._clear_pause()
            self._log("warning", "Получена команда остановки, завершаю текущие страницы…")
            for _ in range(max(1, len(self._workers))):
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    break

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    @property
    def paused(self) -> bool:
        return self._pause_event.is_set()

    @property
    def pause_reasons(self) -> tuple[str, ...]:
        with self._pause_lock:
            return tuple(sorted(self._pause_reasons))

    def pause(self) -> None:
        """Put every worker on hold (manual pause from the UI)."""

        if self._stop_event.is_set():
            return
        self._set_pause_reason("manual", True)
        self._log("info", "Парсинг приостановлен — нажмите «Продолжить», чтобы возобновить.")

    def resume(self) -> None:
        """Release the manual pause; a captcha pause ends on its own."""

        if "captcha" in self.pause_reasons:
            self._log(
                "info",
                "Идёт ожидание прохождения капчи — проверку нужно пройти в окне браузера.",
            )
        self._set_pause_reason("manual", False)

    def toggle_pause(self) -> None:
        if self._stop_event.is_set():
            return
        if "captcha" in self.pause_reasons:
            self._log(
                "info",
                "Идёт ожидание прохождения капчи — проверку нужно пройти в окне браузера.",
            )
            return
        if "manual" in self.pause_reasons:
            self._set_pause_reason("manual", False)
            self._log("info", "Парсинг продолжен.")
        else:
            self.pause()

    def _clear_pause(self) -> None:
        with self._pause_lock:
            self._pause_reasons.clear()
        self._pause_event.clear()
        self._emit_pause_state()

    def _set_pause_reason(self, reason: str, active: bool) -> None:
        with self._pause_lock:
            was_active = reason in self._pause_reasons
            if active:
                self._pause_reasons.add(reason)
            else:
                self._pause_reasons.discard(reason)
            reasons = bool(self._pause_reasons)
        if reasons:
            self._pause_event.set()
        else:
            self._pause_event.clear()
        if was_active != active:
            self._emit_pause_state()

    def _wait_if_paused(self) -> None:
        while self._pause_event.is_set() and not self._stop_event.is_set():
            self._stop_event.wait(0.2)

    def _emit_pause_state(self, force: bool = False) -> None:
        reasons = self.pause_reasons
        reason = "captcha" if "captcha" in reasons else ("manual" if reasons else "")
        state = (bool(reasons), reason)
        with self._pause_lock:
            if not force and state == self._last_pause_state:
                return
            self._last_pause_state = state
        try:
            self._on_pause(state[0], state[1])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Callbacks and counters
    # ------------------------------------------------------------------
    def _log(self, level: str, message: str) -> None:
        try:
            self._on_log(level, message)
        except Exception:
            pass

    def _emit_result(self, row: dict[str, Any]) -> None:
        try:
            self._on_result(row)
        except Exception:
            pass

    def _emit_progress(self) -> None:
        with self._condition:
            total = self._total
            pending = self._pending
        done = max(0, total - pending)
        try:
            self._on_progress(done, total)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------
    def _enqueue(self, url: str, depth: int) -> bool:
        normalized = normalize_url(url)
        if not normalized:
            return False
        host = host_key(normalized)
        if host and self._allowed_hosts and host not in self._allowed_hosts:
            return False
        with self._condition:
            if normalized in self._seen:
                return False
            self._seen.add(normalized)
            self._pending += 1
            self._total += 1
        self._queue.put((normalized, depth))
        self._emit_progress()
        return True

    def _task_done(self) -> None:
        with self._condition:
            self._pending = max(0, self._pending - 1)
            self._condition.notify_all()
        self._emit_progress()

    def _pending_count(self) -> int:
        with self._condition:
            return self._pending

    def _requeue(self, url: str, depth: int) -> None:
        """Return a URL to the queue without marking it as processed."""

        with self._condition:
            self._pending += 1
            self._condition.notify_all()
        self._queue.put((url, depth))
        self._emit_progress()

    def _register_captcha_attempt(self, url: str) -> int:
        with self._condition:
            count = self._captcha_attempts.get(url, 0) + 1
            self._captcha_attempts[url] = count
            return count

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def _free_port() -> int:
        """Reserve a local port for Chromium's DevTools endpoint."""

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _launch_options(self, proxy_raw: str = "") -> dict[str, Any]:
        options: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if proxy_raw:
            options["proxy"] = parse_proxy(proxy_raw)
        return options

    def _start_shared_browser(self) -> None:
        """Launch the single Chromium used by all workers.

        The browser belongs to the thread that calls ``run()``.  Workers
        cannot touch this object, so they connect over the DevTools protocol
        and open a tab in the default browser context instead.

        Chromium's default context is used on purpose: creating an extra
        ``BrowserContext`` with ``new_context()`` opens another window in
        headed mode, while pages added to the default context become tabs of
        the one window the user sees.
        """

        port = self._free_port()
        endpoint = f"http://127.0.0.1:{port}"
        if self._stop_event.is_set():
            raise CrawlError("Запуск Chromium отменён.")
        proxy_raw = self.proxies[0] if len(self.proxies) == 1 else ""
        options = self._launch_options(proxy_raw)
        options["args"] = [
            *options["args"],
            f"--remote-debugging-port={port}",
            f"--user-agent={USER_AGENT}",
            "--lang=ru-RU",
            "--window-size=1440,900",
            "--ignore-certificate-errors",
        ]
        playwright = sync_playwright().start()
        browser: Browser | None = None
        try:
            browser = playwright.chromium.launch(**options)
            self._wait_for_cdp(endpoint)
        except Exception as exc:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            try:
                playwright.stop()
            except Exception:
                pass
            if isinstance(exc, CrawlError):
                raise
            suffix = f" (прокси {proxy_raw})" if proxy_raw else ""
            raise CrawlError(
                f"Не удалось запустить Chromium{suffix}: {self._short_error(exc)}"
            ) from exc
        self._host_playwright = playwright
        self._host_browser = browser
        self._cdp_url = endpoint
        self._shared_mode = True

    def _wait_for_cdp(self, endpoint: str) -> None:
        """Wait until Chromium answers on its DevTools HTTP endpoint."""

        deadline = time.monotonic() + CDP_READY_TIMEOUT_SECONDS
        last_error = "нет ответа"
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                raise CrawlError("Запуск Chromium отменён.")
            try:
                with urllib.request.urlopen(
                    f"{endpoint}/json/version", timeout=1
                ) as response:
                    json.loads(response.read().decode("utf-8"))
                return
            except (OSError, ValueError) as exc:
                last_error = self._short_error(exc)
                time.sleep(CDP_POLL_SECONDS)
        raise CrawlError(
            f"Chromium не открыл порт отладки за "
            f"{CDP_READY_TIMEOUT_SECONDS:.0f} с: {last_error}"
        )

    def _stop_shared_browser(self) -> None:
        """Close the host browser after every worker has disconnected."""

        browser = self._host_browser
        playwright = self._host_playwright
        self._host_browser = None
        self._host_playwright = None
        self._cdp_url = ""
        self._shared_mode = False
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def _launch(
        self, playwright: Playwright, worker_index: int
    ) -> tuple[Browser, BrowserContext]:
        """Launch a dedicated Chromium for one worker (multi-proxy mode)."""

        proxy_raw = (
            self.proxies[worker_index % len(self.proxies)] if self.proxies else ""
        )
        try:
            browser = playwright.chromium.launch(**self._launch_options(proxy_raw))
        except PlaywrightError as exc:
            suffix = f" (прокси {proxy_raw})" if proxy_raw else ""
            raise CrawlError(f"Не удалось запустить Chromium{suffix}: {exc}") from exc

        context_options: dict[str, Any] = {
            "user_agent": USER_AGENT,
            "locale": "ru-RU",
            "viewport": {"width": 1440, "height": 900},
            "ignore_https_errors": True,
        }
        try:
            context = browser.new_context(**context_options)
            context.set_default_timeout(45000)
            context.set_default_navigation_timeout(60000)
        except Exception:
            try:
                browser.close()
            except Exception:
                pass
            raise
        # No context.route here: every worker prepares its own page, so tabs
        # sharing one context never affect each other's blocking.
        return browser, context

    def _connect_shared_browser(
        self,
    ) -> tuple[Playwright, Browser, BrowserContext]:
        """Connect one worker thread to the host browser and its context.

        Each worker gets its own Playwright connection and therefore its own
        Python wrappers; they all resolve to the same default browser context,
        which is what turns their pages into tabs of a single window.
        """

        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(self._cdp_url)
        except Exception:
            try:
                playwright.stop()
            except Exception:
                pass
            raise
        contexts = browser.contexts
        if not contexts:
            try:
                browser.close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass
            raise CrawlError("Общий Chromium не предоставил BrowserContext.")
        return playwright, browser, contexts[0]

    def _prepare_page(self, page: Page) -> None:
        """Apply timeouts, anti-detect script and resource blocking to a tab."""

        try:
            page.set_default_timeout(45000)
            page.set_default_navigation_timeout(60000)
        except PlaywrightError:
            pass
        if self.headless:
            # The shared context has no explicit viewport; keep headless
            # rendering consistent with the dedicated-browser mode.
            try:
                page.set_viewport_size({"width": 1440, "height": 900})
            except PlaywrightError:
                pass
        try:
            page.add_init_script(STEALTH_INIT_SCRIPT)
        except PlaywrightError:
            pass
        if self._image_blocking_active():
            # Routes are attached per tab: in shared mode one context holds
            # every worker's tab, and lifting the image block for a captcha in
            # one tab must not affect the others.
            try:
                page.route("**/*", self._make_route_handler(page))
            except PlaywrightError:
                pass

    def _forget_page(self, page: Page) -> None:
        with self._image_block_lock:
            self._relaxed_pages.discard(id(page))

    # ------------------------------------------------------------------
    # Heavy resource blocking
    # ------------------------------------------------------------------
    def _make_route_handler(self, page: Page):
        def handle(route: Any) -> None:
            self._route_request(route, page)

        return handle

    def _route_request(self, route: Any, page: Page) -> None:
        try:
            request = route.request
            reason = self._should_abort_request(
                request.resource_type,
                request.url,
                self._images_relaxed(page),
            )
        except PlaywrightError:
            self._continue_route(route)
            return
        if reason:
            try:
                route.abort()
            except PlaywrightError:
                pass
            return
        self._continue_route(route)

    @staticmethod
    def _continue_route(route: Any) -> None:
        try:
            route.continue_()
        except PlaywrightError:
            pass

    def _image_blocking_active(self) -> bool:
        """Whether image requests are blocked for an ordinary page."""

        return self.block_heavy_resources or (
            self.disable_images and not self.needs_images
        )

    def _images_relaxed(self, page: Page) -> bool:
        with self._image_block_lock:
            return id(page) in self._relaxed_pages

    def _relax_image_blocking(self, page: Page, relaxed: bool) -> bool:
        """Toggle the image block for one tab; returns ``True`` on change."""

        if not self._image_blocking_active():
            return False
        key = id(page)
        with self._image_block_lock:
            if relaxed:
                if key in self._relaxed_pages:
                    return False
                self._relaxed_pages.add(key)
                return True
            if key not in self._relaxed_pages:
                return False
            self._relaxed_pages.discard(key)
            return True

    def _is_tracker_url(self, url: str) -> bool:
        host = host_key(url)
        if not host or host in self._start_hosts:
            return False
        return any(marker in host for marker in TRACKER_HOST_MARKERS)

    def _should_abort_request(
        self, resource_type: str, url: str, relaxed_images: bool = False
    ) -> str:
        """Return the block reason, or ``""`` when the request must continue.

        ``document``, ``script``, ``xhr``, ``fetch`` and ``stylesheet`` are
        never blocked (except for known tracker hosts), because Vue-style
        sites render their characteristic tables with JavaScript.
        """

        resource = (resource_type or "").strip().lower()
        if (
            self.block_heavy_resources
            and resource != "document"
            and self._is_tracker_url(url)
        ):
            return "tracker"
        if resource not in HEAVY_RESOURCE_TYPES:
            return ""
        if resource == "image":
            if relaxed_images or not self._image_blocking_active():
                return ""
            return "image"
        return resource if self.block_heavy_resources else ""

    # ------------------------------------------------------------------
    # Shared browser session
    # ------------------------------------------------------------------
    def _apply_shared_cookies(self, page: Page) -> None:
        if self._shared_mode:
            # Every tab lives in the same BrowserContext, so cookies set by a
            # captcha or login are already visible to the other workers.
            return
        ident = threading.get_ident()
        with self._cookies_lock:
            version = self._cookies_version
            if self._cookie_seen.get(ident) == version:
                return
            cookies = [dict(item) for item in self._cookies]
            self._cookie_seen[ident] = version
        if not cookies:
            return
        try:
            page.context.add_cookies(cookies)
        except (PlaywrightError, ValueError, TypeError):
            pass

    def _store_shared_cookies(self, page: Page) -> None:
        if self._shared_mode:
            return
        try:
            cookies = page.context.cookies()
        except (PlaywrightError, ValueError, TypeError):
            return
        if not cookies:
            return
        with self._cookies_lock:
            if cookies == self._cookies:
                return
            self._cookies = cookies
            self._cookies_version += 1

    # ------------------------------------------------------------------
    # Anti-bot / captcha
    # ------------------------------------------------------------------
    @staticmethod
    def _is_image_field(field: dict[str, Any]) -> bool:
        field_type = str(field.get("type") or "").strip().lower()
        if field_type not in {"xpath_attr", "xpath_attr_list"}:
            return False
        selector = str(field.get("selector") or "").lower()
        return any(token in selector for token in ("@src", "@data-src", "//img", "og:image"))

    def _has_meaningful_fields(self, values: dict[str, Any]) -> bool:
        """True when at least one real (non-URL, non-image) field was filled."""

        for field in self.fields:
            if str(field.get("type") or "").strip().lower() == "spec_url":
                continue
            if self._is_image_field(field):
                continue
            name = str(field.get("name") or "").strip()
            if is_meaningful_value(values.get(name)):
                return True
        return False

    def _captcha_markers(self, page: Page) -> list[str]:
        try:
            found = page.evaluate(CAPTCHA_MARKERS_JS)
        except (PlaywrightError, ValueError, TypeError):
            return []
        if not isinstance(found, list):
            return []
        return [str(item) for item in found]

    @staticmethod
    def _has_waf_headers(headers: Any) -> bool:
        if not isinstance(headers, dict):
            return False
        names = {str(key).lower() for key in headers}
        return any(name in names for name in CAPTCHA_HEADER_NAMES)

    def _captcha_hard_signals(self, status: int, headers: Any) -> list[str]:
        """Server-level block markers that need no DOM inspection."""

        signals: list[str] = []
        if status in CAPTCHA_STATUS_CODES:
            signals.append(f"HTTP {status}")
        if self._has_waf_headers(headers):
            signals.append("WAF-заголовок")
        return signals

    def _handle_captcha(self, page: Page, url: str) -> bool:
        """Pause the crawl and wait until the captcha markers disappear.

        Returns ``True`` when the check looks passed (the caller re-parses the
        URL), ``False`` when the wait timed out or another tab is already
        handling the same block (the caller puts the URL back into the queue).
        """

        if self._stop_event.is_set():
            return False
        if not self._captcha_lock.acquire(blocking=False):
            self._log(
                "debug",
                f"Капча уже обрабатывается в другой вкладке — URL вернётся в очередь ({url})",
            )
            return False
        try:
            self._set_pause_reason("captcha", True)
            self._log(
                "warning",
                f"Капча/блокировка на {url}. Все потоки на паузе — пройдите проверку "
                f"в открытом окне браузера (ожидание до {CAPTCHA_WAIT_SECONDS} с).",
            )
            if self.headless:
                self._log(
                    "warning",
                    "Браузер запущен в headless-режиме: окно не видно, капчу пройти нельзя.",
                )
            try:
                page.bring_to_front()
            except (PlaywrightError, ValueError):
                pass

            if self._relax_image_blocking(page, True):
                self._log(
                    "info",
                    "Капча: временно включаю загрузку изображений в этой вкладке, "
                    "чтобы проверка отображалась полностью.",
                )
                try:
                    page.reload(wait_until="domcontentloaded", timeout=45000)
                except (PlaywrightTimeoutError, PlaywrightError):
                    pass

            started = time.monotonic()
            clear_ticks = 0
            resolved = False
            while not self._stop_event.is_set():
                if self._captcha_markers(page):
                    clear_ticks = 0
                else:
                    clear_ticks += 1
                    if clear_ticks >= CAPTCHA_CLEAR_TICKS:
                        resolved = True
                        break
                if time.monotonic() - started >= CAPTCHA_WAIT_SECONDS:
                    break
                self._stop_event.wait(CAPTCHA_POLL_SECONDS)

            if resolved:
                self._store_shared_cookies(page)
                self._log("success", "Капча пройдена — снимаю паузу и повторяю извлечение URL.")
                return True
            if not self._stop_event.is_set():
                self._log(
                    "warning",
                    f"Капча не пройдена за {CAPTCHA_WAIT_SECONDS} с — URL остаётся в очереди ({url}).",
                )
            return False
        finally:
            try:
                if self._relax_image_blocking(page, False):
                    self._log(
                        "debug",
                        "Капча завершена: блокировка изображений снова включена.",
                    )
            except (PlaywrightError, ValueError):
                pass
            self._set_pause_reason("captcha", False)
            self._captcha_lock.release()

    # ------------------------------------------------------------------
    # Scenario
    # ------------------------------------------------------------------
    def _run_scenario(self, page: Page) -> None:
        for index, step in enumerate(self.scenario, start=1):
            if self._stop_event.is_set():
                return
            self._wait_if_paused()
            action = str(step.get("action") or "click").strip().lower()
            xpath = str(step.get("xpath") or "").strip()
            wait_ms = max(0, int(step.get("wait_ms", 0) or 0))
            optional = bool(step.get("optional", True))
            try:
                if action == "click":
                    page.locator(f"xpath={xpath}").first.click(timeout=8000)
                elif action == "hover":
                    page.locator(f"xpath={xpath}").first.hover(timeout=8000)
                elif action == "scroll":
                    page.evaluate("window.scrollBy(0, Math.max(300, window.innerHeight * 0.9))")
                elif action == "wait":
                    pass
                else:
                    raise ScenarioError(f"неизвестное действие «{action}»")
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
            except (PlaywrightTimeoutError, PlaywrightError, ScenarioError) as exc:
                if optional:
                    self._log(
                        "warning",
                        f"Необязательный шаг сценария {index} ({action}) пропущен: {self._short_error(exc)}",
                    )
                    continue
                raise ScenarioError(
                    f"Шаг сценария {index} ({action}) не выполнен: {self._short_error(exc)}"
                ) from exc

    @staticmethod
    def _short_error(exc: BaseException) -> str:
        text = str(exc).strip().splitlines()
        return text[0] if text else exc.__class__.__name__

    # ------------------------------------------------------------------
    # Page processing
    # ------------------------------------------------------------------
    def _wait_for_campaign_images(
        self, page: Page, image_xpaths: list[str] | None, url: str
    ) -> None:
        """Wait until the campaign's own image XPaths expose real URLs.

        Only nodes matched by the current campaign's image fields are checked,
        so logos, banners and recommendations can no longer satisfy the wait.
        Only attribute values (``src``/``data-src``) are inspected — the wait
        never depends on the image bytes arriving, so heavy-resource blocking
        and slow image servers cannot stall it.
        With no image XPaths the whole step is skipped without any delay.
        """

        if not image_xpaths:
            return
        started = time.monotonic()
        try:
            state = page.wait_for_function(
                IMAGE_XPATH_WAIT_JS,
                arg=list(image_xpaths),
                timeout=IMAGE_WAIT_TIMEOUT_MS,
                polling=100,
            ).json_value()
        except PlaywrightTimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            self._log(
                "debug",
                f"XPath изображений не дали URL за {elapsed} мс — продолжаю без ожидания ({url})",
            )
            return
        except (PlaywrightError, ValueError, TypeError):
            self._log("debug", f"Ожидание изображений по XPath пропущено — {url}")
            return

        elapsed = int((time.monotonic() - started) * 1000)
        if not isinstance(state, dict) or state.get("skipped"):
            return
        ready = int(state.get("ready", 0) or 0)
        total = int(state.get("total", 0) or 0)
        if total and ready:
            self._log(
                "debug",
                f"XPath изображений готовы: {ready}/{total} за {elapsed} мс — {url}",
            )

    def _wait_for_dom_ready(
        self,
        page: Page,
        url: str,
        phase: str = "main",
        min_matched: int = 0,
        require_all: bool = False,
    ) -> dict[str, Any] | None:
        """Wait until the campaign's DOM fields are populated and stable.

        Under parallel load tabs can be throttled, so ``page.content()`` may be
        taken before Vue/JS renders tables and similar blocks.  The wait checks
        how many campaign XPaths yield real text and whether the matched/text
        signature stopped changing.  A selector that matches only an empty
        container ("pending") keeps the wait alive, and a short grace period
        prevents finishing during a brief pause in rendering.  The wait always
        continues after the timeout.

        ``min_matched`` keeps the retry pass at least as complete as the first
        one, and ``require_all`` (used when the first pass saw nothing) asks
        for every campaign XPath.

        The wait only inspects the DOM (XPath matches, text length, element
        count) and never waits for image/font/media loading, so blocked or
        broken heavy resources cannot stall it.
        """

        token = str(time.monotonic_ns())
        started = time.monotonic()
        try:
            state = page.wait_for_function(
                DOM_READY_WAIT_JS,
                arg={
                    "xpaths": list(self.field_xpaths),
                    "token": token,
                    "minStable": DOM_READY_STABLE_TICKS,
                    "minWait": DOM_READY_MIN_MS,
                    "pendingWait": DOM_READY_PENDING_MS,
                    "minMatched": max(0, int(min_matched)),
                    "requireAll": bool(require_all),
                },
                timeout=DOM_READY_TIMEOUT_MS,
                polling=120,
            ).json_value()
        except PlaywrightTimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            self._log(
                "debug",
                f"DOM не стабилизировался за {elapsed} мс — извлекаю как есть ({url})",
            )
            return None
        except (PlaywrightError, ValueError, TypeError):
            self._log("debug", f"Ожидание готовности DOM пропущено ({phase}) — {url}")
            return None

        elapsed = int((time.monotonic() - started) * 1000)
        if not isinstance(state, dict):
            return None
        matched = int(state.get("matched", 0) or 0)
        total = int(state.get("total", 0) or 0)
        elements = int(state.get("elements", 0) or 0)
        empty = bool(state.get("empty"))
        pending = int(state.get("pending", 0) or 0)
        if empty:
            self._log(
                "debug",
                f"Страница пустая или заблокирована ({phase}): полей {matched}/{total}, "
                f"элементов {elements} — {url}",
            )
            return state
        self._log(
            "debug",
            f"DOM готов ({phase}): полей {matched}/{total}, пустых блоков {pending}, "
            f"элементов {elements}, {elapsed} мс — {url}",
        )
        return state

    def _count_filled_fields(self, values: dict[str, Any]) -> int:
        filled = 0
        for field in self.fields:
            name = str(field.get("name") or "").strip()
            # Markup-only values (an empty container caught by an
            # ``xpath_outer_html`` field) mean "not rendered yet".
            if is_meaningful_value(values.get(name)):
                filled += 1
        return filled

    def _should_retry_extraction(
        self,
        outcome: ExtractionOutcome,
        page: Page,
        url: str,
        dom_state: dict[str, Any] | None = None,
    ) -> bool:
        """Decide whether a page deserves one more extraction pass.

        Two signals are used: the share of filled campaign fields (less than
        half of them means the snapshot is suspicious) and the state of the
        DOM wait (a timed-out wait or a matched-but-empty block means content
        was still being rendered).
        """

        total = len(self.fields)
        if total == 0 or self._stop_event.is_set():
            return False
        filled = self._count_filled_fields(outcome.values)
        ratio = filled / total
        # Usually a page needs only the low-water mark.  When the DOM wait
        # never confirmed the campaign fields, the page was likely still
        # rendering, so a higher mark is used to give it one more chance.
        dom_timed_out = dom_state is None
        limit = FIELD_RETRY_TIMEOUT_RATIO if dom_timed_out else FIELD_RETRY_RATIO
        pending = 0
        if isinstance(dom_state, dict):
            pending = int(dom_state.get("pending", 0) or 0)
        # A matched but empty block (e.g. a spec container without rows) is a
        # direct sign of under-rendering even when the fill ratio looks fine.
        under_rendered = pending > 0 and ratio < 0.9
        if ratio >= limit and not under_rendered:
            return False
        if filled == 0:
            # Nothing at all was extracted: retry only when the page itself
            # looks loaded (blocked/empty pages should not be fetched twice).
            try:
                if not page.title().strip():
                    return False
            except (PlaywrightTimeoutError, PlaywrightError):
                return False
        if under_rendered:
            reason = f"пустых блоков {pending}"
        elif dom_timed_out:
            reason = "DOM не подтвердил поля"
        else:
            reason = "мало заполненных полей"
        self._log(
            "debug",
            f"Заполнено {filled}/{total} полей ({reason}) — "
            f"планирую повторное извлечение ({url})",
        )
        return True

    def _extract_with_retry(
        self,
        page: Page,
        url: str,
        dom_state: dict[str, Any] | None = None,
        blocked_markers: list[str] | None = None,
    ) -> tuple[str, str, ExtractionOutcome]:
        html = page.content()
        page_url = page.url
        outcome = self.extractor.extract(html, page_url)
        if blocked_markers and self._count_filled_fields(outcome.values) == 0:
            # A challenge screen has no campaign fields to re-render, so the
            # extra pass would only delay the captcha pause.
            self._log(
                "debug",
                f"Страница похожа на капчу ({', '.join(blocked_markers[:3])}), "
                f"повторное извлечение не требуется — {url}",
            )
            return html, page_url, outcome
        if not self._should_retry_extraction(outcome, page, url, dom_state):
            return html, page_url, outcome

        min_matched = 0
        if isinstance(dom_state, dict):
            min_matched = int(dom_state.get("matched", 0) or 0)
        filled = self._count_filled_fields(outcome.values)
        total = len(self.fields)
        self._log(
            "info",
            f"Повторное извлечение (заполнено {filled}/{total}) через "
            f"{DOM_RETRY_WAIT_MS} мс: {url}",
        )
        # A background/occluded tab can be throttled by Chromium; bringing the
        # page to front restores normal timers before the retry pass.
        try:
            page.bring_to_front()
        except (PlaywrightError, ValueError):
            pass
        try:
            page.wait_for_timeout(DOM_RETRY_WAIT_MS)
        except (PlaywrightTimeoutError, PlaywrightError):
            return html, page_url, outcome
        if self._stop_event.is_set():
            return html, page_url, outcome
        self._wait_if_paused()
        if self._stop_event.is_set():
            return html, page_url, outcome

        self._wait_for_dom_ready(
            page,
            url,
            phase="retry",
            min_matched=min_matched,
            require_all=min_matched <= 0,
        )
        retry_html = page.content()
        retry_url = page.url
        retry_outcome = self.extractor.extract(retry_html, retry_url)
        retry_filled = self._count_filled_fields(retry_outcome.values)
        self._log(
            "debug",
            f"Повторное извлечение: {retry_filled}/{total} полей — {url}",
        )
        if retry_filled >= filled:
            return retry_html, retry_url, retry_outcome
        return html, page_url, outcome

    def _apply_row_plugins(self, row: dict[str, Any], url: str) -> int:
        """Run the active plugin chain and return the number of new columns."""

        before = set(row)
        errors: list[str] = []
        transformed = apply_plugins(
            row,
            self.campaign,
            runs=self.plugin_runs,
            errors=errors,
        )
        if transformed is not row:
            row.clear()
            row.update(transformed)
        for message in errors:
            self._log("warning", f"{message} — {url}")
        added = [key for key in row if key not in before]
        # Exporter uses this internal marker to avoid running the plugin chain
        # (and specs_parser in particular) a second time over the same row.
        row["__plugins_applied__"] = True
        if added:
            self._log("debug", f"Плагины: {len(added)} новых колонок — {url}")
        return len(added)

    def _process_page(
        self, page: Page, url: str, depth: int, image_xpaths: list[str] | None = None
    ) -> None:
        while True:
            if self._stop_event.is_set():
                return
            self._wait_if_paused()
            if self._stop_event.is_set():
                return
            self._apply_shared_cookies(page)
            self._log("info", f"[{depth}] Открываю {url}")
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                raise PageLoadError(
                    f"Не удалось открыть страницу: {self._short_error(exc)}"
                ) from exc

            status = int(response.status) if response is not None else 0
            headers = response.headers if response is not None else {}
            status_text = str(getattr(response, "status_text", "") or "").strip() if response is not None else ""
            if status >= 400 and status not in CAPTCHA_STATUS_CODES:
                raise PageLoadError(f"HTTP {status} {status_text}".strip())

            # Markers may be removed by the challenge script before the page is
            # fully processed, so remember what was on screen right after load.
            initial_markers = self._captcha_markers(page)
            hard_signals = self._captcha_hard_signals(status, headers)
            if hard_signals and not self._stop_event.is_set():
                try:
                    snapshot_html = page.content()
                except (PlaywrightTimeoutError, PlaywrightError):
                    snapshot_html = ""
                snapshot = self.extractor.extract(snapshot_html, page.url)
                if not self._has_meaningful_fields(snapshot.values):
                    attempt = self._register_captcha_attempt(url)
                    self._log(
                        "warning",
                        f"Признаки капчи/блокировки ({'; '.join(hard_signals)}), "
                        f"целевых полей нет — {url}",
                    )
                    if attempt > CAPTCHA_MAX_ATTEMPTS:
                        raise CaptchaBlockedError(
                            f"капча не пройдена за {CAPTCHA_MAX_ATTEMPTS} попытки"
                        )
                    if self._handle_captcha(page, url):
                        continue
                    self._requeue(url, depth)
                    return
            break

        final_host = host_key(page.url)
        if final_host:
            with self._condition:
                self._allowed_hosts.add(final_host)

        try:
            page.wait_for_timeout(350)
            self._wait_for_campaign_images(page, image_xpaths, url)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            self._log("debug", f"Начальная пауза перед извлечением прервана: {self._short_error(exc)}")
        self._wait_if_paused()
        if self._stop_event.is_set():
            return
        self._run_scenario(page)
        if self._stop_event.is_set():
            return

        self._wait_if_paused()
        if self._stop_event.is_set():
            return
        dom_state = self._wait_for_dom_ready(page, url)
        if self._stop_event.is_set():
            return

        markers_before = list(initial_markers)
        for marker in self._captcha_markers(page):
            if marker not in markers_before:
                markers_before.append(marker)
        html, page_url, outcome = self._extract_with_retry(
            page, url, dom_state, markers_before
        )
        for message in outcome.errors:
            self._log("warning", f"Поле не извлечено на {url}: {message}")

        if not self._stop_event.is_set() and not self._has_meaningful_fields(
            outcome.values
        ):
            # Anti-bot widgets are often injected by JS after the first check
            # or removed by the challenge script before extraction finishes.
            late_markers = list(markers_before)
            for marker in self._captcha_markers(page):
                if marker not in late_markers:
                    late_markers.append(marker)
            if late_markers:
                attempt = self._register_captcha_attempt(url)
                self._log(
                    "warning",
                    f"Капча появилась после рендера ({', '.join(late_markers[:3])}), "
                    f"целевых полей нет — {url}",
                )
                if attempt > CAPTCHA_MAX_ATTEMPTS:
                    raise CaptchaBlockedError(
                        f"капча не пройдена за {CAPTCHA_MAX_ATTEMPTS} попытки"
                    )
                if self._handle_captcha(page, url):
                    self._process_page(page, url, depth, image_xpaths)
                    return
                self._requeue(url, depth)
                return

        row: dict[str, Any] = dict(outcome.values)
        # Lazy-loaded galleries often return root-relative paths; exports and
        # downstream plugins must always receive absolute image URLs.
        for field in self.image_fields:
            name = str(field.get("name") or "").strip()
            if not name:
                continue
            value = row.get(name)
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("/"):
                    row[name] = urljoin(page_url, text)
            elif isinstance(value, list):
                row[name] = [
                    urljoin(page_url, str(item).strip())
                    if str(item).strip().startswith("/")
                    else item
                    for item in value
                ]
                if str(field.get("type") or "").strip().lower() == "xpath_attr_list":
                    unique_count = len([item for item in row[name] if str(item).strip()])
                    self._log(
                        "info",
                        f"Поле «{name}»: уникальных URL изображений — {unique_count} ({url})",
                    )
        row["__url__"] = page_url
        if outcome.errors:
            row["__status__"] = f"OK ({len(outcome.errors)} ошиб.)"
        else:
            row["__status__"] = "OK"

        plugin_columns = self._apply_row_plugins(row, url)

        with self._condition:
            self._success += 1
        self._store_shared_cookies(page)
        self._emit_result(row)
        self._log(
            "success",
            f"Готово: {self._short_url(page_url)} — полей {len(outcome.values)}"
            + (f", новых колонок плагинов {plugin_columns}" if plugin_columns else ""),
        )

        if depth < self.max_depth and self.gather_xpaths and not self._stop_event.is_set():
            links = self._gather_links(html, page.url)
            added = 0
            for link in links:
                if self._enqueue(link, depth + 1):
                    added += 1
            if added:
                self._log("debug", f"Добавлено ссылок: {added} (глубина {depth + 1})")

    @staticmethod
    def _short_url(url: str, limit: int = 110) -> str:
        return url if len(url) <= limit else url[: limit - 1] + "…"

    def _gather_links(self, html: str, base_url: str) -> list[str]:
        try:
            document = lxml.html.fromstring(html or "<html></html>")
            if document.getroottree().getroot() is not document:
                document = document.getroottree().getroot()
        except (etree.ParserError, ValueError):
            return []
        found: list[str] = []
        for xpath in self.gather_xpaths:
            try:
                matches = cached_xpath(xpath)(document)
            except (etree.XPathError, ValueError) as exc:
                self._log("error", f"Ошибка XPath сбора ссылок «{xpath}»: {exc}")
                continue
            for match in matches:
                if isinstance(match, (str, bytes)):
                    href = match.decode("utf-8", "replace") if isinstance(match, bytes) else match
                else:
                    href = match.get("href") or match.get("data-href") or ""
                href = (href or "").strip()
                if not href:
                    continue
                absolute = normalize_url(href, base=base_url)
                if not absolute:
                    continue
                host = host_key(absolute)
                if self._allowed_hosts and host not in self._allowed_hosts:
                    continue
                found.append(absolute)
        return found

    def _error_row(self, url: str, message: str) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for index, field in enumerate(self.fields, start=1):
            name = str(field.get("name") or f"Поле {index}")
            row[name] = url if str(field.get("type")) == "spec_url" else ""
        row["__url__"] = url
        row["__status__"] = f"Ошибка: {message}"
        return row

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _worker(self, index: int) -> None:
        playwright: Playwright | None = None
        browser: Browser | None = None
        context: BrowserContext | None = None
        page: Page | None = None
        shared = self._shared_mode and bool(self._cdp_url)
        rng = random.Random()
        try:
            if shared:
                playwright, browser, context = self._connect_shared_browser()
                page = context.new_page()
                self._prepare_page(page)
                self._log(
                    "debug",
                    f"Поток {index + 1}: вкладка открыта в общем окне Chromium",
                )
            else:
                playwright = sync_playwright().start()
                browser, context = self._launch(playwright, index)
                page = context.new_page()
                self._prepare_page(page)
                self._log("debug", f"Поток {index + 1}: Chromium готов")
            while not self._stop_event.is_set():
                self._wait_if_paused()
                if self._stop_event.is_set():
                    break
                try:
                    task = self._queue.get(timeout=0.25)
                except queue.Empty:
                    if self._pending_count() == 0:
                        break
                    continue
                if task is None:
                    break
                url, depth = task
                try:
                    if self._stop_event.is_set():
                        continue
                    self._process_page(page, url, depth, self.image_xpaths)
                except PageLoadError as exc:
                    with self._condition:
                        self._errors += 1
                    message = self._short_error(exc)
                    self._emit_result(self._error_row(url, message))
                    self._log("error", f"Ошибка страницы {url}: {message}")
                except CaptchaBlockedError as exc:
                    # A captcha is not a data row: log it and keep the table clean.
                    with self._condition:
                        self._errors += 1
                    message = self._short_error(exc)
                    self._log("error", f"Капча не пройдена: {url} — {message}")
                except ScenarioError as exc:
                    with self._condition:
                        self._errors += 1
                    message = self._short_error(exc)
                    self._emit_result(self._error_row(url, message))
                    self._log("error", f"Сценарий на {url}: {message}")
                except Exception as exc:  # keep the crawl alive no matter what
                    with self._condition:
                        self._errors += 1
                    message = self._short_error(exc)
                    self._emit_result(self._error_row(url, message))
                    self._log("error", f"Сбой обработки {url}: {message}")
                finally:
                    self._task_done()

                if self._stop_event.is_set():
                    break
                if self._pending_count() == 0:
                    break
                delay = rng.uniform(self.delay_min, self.delay_max)
                if delay > 0:
                    self._stop_event.wait(delay)
        except Exception as exc:
            message = f"Поток {index + 1}: {self._short_error(exc)}"
            with self._condition:
                self._worker_errors.append(message)
            self._log("error", message)
        finally:
            if page is not None:
                self._forget_page(page)
                try:
                    page.close()
                except (PlaywrightError, ValueError):
                    pass
            if shared:
                # Disconnecting a CDP client must not close the host browser
                # or the shared context that the other tabs still use.
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
            else:
                if context is not None:
                    try:
                        context.close()
                    except (PlaywrightError, ValueError):
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except (PlaywrightError, ValueError):
                        pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self) -> CrawlSummary:
        self._started_at = datetime.now()
        self._clear_pause()
        start_urls = [
            item for item in (self.campaign.get("input") or {}).get("start_urls", []) if str(item).strip()
        ]
        normalized_starts: list[str] = []
        for raw_url in start_urls:
            normalized = normalize_url(str(raw_url))
            if not normalized:
                self._log("warning", f"Пропущен некорректный стартовый URL: {raw_url}")
                continue
            if normalized not in normalized_starts:
                normalized_starts.append(normalized)
                self._allowed_hosts.add(host_key(normalized))
        self._start_hosts = {
            host for host in (host_key(item) for item in normalized_starts) if host
        }

        if not normalized_starts:
            self._log("error", "Нет корректных стартовых URL — запуск отменён.")
            finished = datetime.now()
            return CrawlSummary(
                started_at=self._started_at,
                finished_at=finished,
                total_urls=0,
                success=0,
                errors=0,
                stopped=False,
            )

        self._log(
            "system",
            f"Запуск «{self.campaign.get('campaign_name', '')}»: URL {len(normalized_starts)}, "
            f"потоков {self.threads}, глубина {self.max_depth}, "
            f"задержка {int(self.delay_min * 1000)}–{int(self.delay_max * 1000)} мс",
        )
        if self.block_heavy_resources:
            self._log(
                "info",
                "Блокировка тяжёлых ресурсов включена: изображения, медиа, шрифты "
                "и трекеры не загружаются.",
            )
            if self.image_fields:
                names = ", ".join(str(field.get("name") or "?") for field in self.image_fields)
                self._log(
                    "info",
                    f"URL изображений читаются из атрибутов HTML для полей: {names}.",
                )
        elif self.needs_images and self.disable_images:
            names = ", ".join(str(field.get("name") or "?") for field in self.image_fields)
            self._log(
                "info",
                f"Блокировка изображений отключена для полей: {names} — нужны реальные URL из @src.",
            )
        for start in normalized_starts:
            self._enqueue(start, 0)

        worker_count = self.threads
        if len(self.proxies) <= 1:
            self._log(
                "info",
                f"Браузер: одно окно Chromium, по вкладке на поток ({worker_count}).",
            )
            try:
                self._start_shared_browser()
            except CrawlError:
                if not self._stop_event.is_set():
                    raise
                self._log("warning", "Остановка: общий Chromium не был запущен.")
                finished = datetime.now()
                return CrawlSummary(
                    started_at=self._started_at,
                    finished_at=finished,
                    total_urls=self._total,
                    success=self._success,
                    errors=self._errors,
                    stopped=True,
                    worker_errors=list(self._worker_errors),
                )
        else:
            self._log(
                "info",
                f"Прокси: отдельный Chromium на каждый поток ({len(self.proxies)} шт.).",
            )

        try:
            self._workers = [
                threading.Thread(
                    target=self._worker,
                    args=(index,),
                    name=f"crawl-{index + 1}",
                    daemon=True,
                )
                for index in range(worker_count)
            ]
            for worker in self._workers:
                worker.start()
            for worker in self._workers:
                worker.join()
        finally:
            self._stop_shared_browser()

        self._emit_progress()
        finished = datetime.now()
        summary = CrawlSummary(
            started_at=self._started_at,
            finished_at=finished,
            total_urls=self._total,
            success=self._success,
            errors=self._errors,
            stopped=self._stop_event.is_set(),
            worker_errors=list(self._worker_errors),
        )
        self._log(
            "system",
            f"Завершено: успешно {summary.success}, ошибок {summary.errors}, "
            f"всего {summary.total_urls}, время {summary.duration_seconds:.1f} с"
            + (" (остановлено)" if summary.stopped else ""),
        )
        return summary


def campaign_fingerprint(campaign: dict[str, Any]) -> str:
    """Stable representation used in debug logs."""

    return json.dumps(campaign, ensure_ascii=False, sort_keys=True)
