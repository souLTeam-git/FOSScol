"""Visual XPath picker backed by a separate Chromium window."""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from ui.icons import app_icon

# A worker that is still closing Playwright when the dialog disappears must
# stay referenced until its finally-block finishes; it is never terminated.
_DETACHED_WORKERS: set[QThread] = set()


def _detach_worker(worker: QThread) -> None:
    worker.setParent(None)
    _DETACHED_WORKERS.add(worker)
    worker.finished.connect(lambda: _DETACHED_WORKERS.discard(worker))


PICKER_SCRIPT = r"""
(() => {
    if (window.__fosscolPickerInstalled) {
        return;
    }
    window.__fosscolPickerInstalled = true;

    const style = document.createElement('style');
    style.textContent = `
        #__fosscol-picker-banner {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 2147483647;
            background: #2f6fed;
            color: #ffffff;
            font: 600 14px/1.45 "Noto Sans", "DejaVu Sans", sans-serif;
            padding: 10px 16px;
            box-shadow: 0 3px 14px rgba(0,0,0,.28);
            text-align: center;
            pointer-events: none;
        }
        #__fosscol-picker-banner small {
            display: block;
            font-weight: 400;
            opacity: .86;
            margin-top: 2px;
        }
        .__fosscol-picker-hover {
            outline: 3px solid #ff6b35 !important;
            outline-offset: -3px !important;
            cursor: crosshair !important;
            background-color: rgba(255, 107, 53, .10) !important;
        }
        .__fosscol-picker-chosen {
            outline: 3px solid #1a7f37 !important;
            outline-offset: -3px !important;
            background-color: rgba(26, 127, 55, .12) !important;
        }
        html.__fosscol-picker-active, html.__fosscol-picker-active * {
            cursor: crosshair !important;
        }
        #__fosscol-picker-tip {
            position: fixed;
            z-index: 2147483647;
            max-width: 420px;
            background: rgba(23, 31, 44, .94);
            color: #ffffff;
            font: 500 12px/1.4 "Noto Sans", "DejaVu Sans", sans-serif;
            padding: 5px 9px;
            border-radius: 5px;
            pointer-events: none;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            box-shadow: 0 3px 10px rgba(0,0,0,.32);
        }
        #__fosscol-picker-tip b {
            color: #ffb08a;
            font-weight: 700;
        }
    `;
    function applyPickerChrome() {
        // The init script also runs before <html> exists on some pages.
        const target = document.head || document.documentElement;
        if (!target) {
            return false;
        }
        if (!style.isConnected) {
            target.appendChild(style);
        }
        document.documentElement.classList.add('__fosscol-picker-active');
        return true;
    }
    if (!applyPickerChrome()) {
        document.addEventListener('DOMContentLoaded', applyPickerChrome, { once: true });
    }

    function bannerText() {
        if (window.__fosscol_dynamic__) {
            return [
                'Шаг 1 из 2: кликните название характеристики',
                'Затем кликните по её значению. Esc — отмена.',
            ];
        }
        return [
            'FOSScol: кликните по нужному элементу',
            'Наведите курсор для подсветки. Esc — отмена выбора.',
        ];
    }

    function showBanner() {
        if (!document.body || document.getElementById('__fosscol-picker-banner')) {
            return;
        }
        const banner = document.createElement('div');
        banner.id = '__fosscol-picker-banner';
        const text = bannerText();
        banner.innerHTML = text[0] + '<small>' + text[1] + '</small>';
        document.body.appendChild(banner);
        window.scrollTo({ top: 0, behavior: 'auto' });
    }

    function updateBanner(main, small) {
        const banner = document.getElementById('__fosscol-picker-banner');
        if (!banner) {
            return;
        }
        banner.innerHTML = main + (small ? '<small>' + small + '</small>' : '');
    }

    window.__fosscol_setDynamic__ = function () {
        window.__fosscol_dynamic__ = true;
        const text = bannerText();
        updateBanner(text[0], text[1]);
    };
    if (document.body) {
        showBanner();
    } else {
        document.addEventListener('DOMContentLoaded', showBanner, { once: true });
    }

    let hovered = null;
    let dynamicFirst = null;

    function clearHover() {
        if (hovered && hovered.classList) {
            hovered.classList.remove('__fosscol-picker-hover');
        }
        hovered = null;
    }

    function uniqueIdXPath(element) {
        if (!element.id) {
            return '';
        }
        try {
            if (document.querySelectorAll('#' + CSS.escape(element.id)).length !== 1) {
                return '';
            }
        } catch (error) {
            return '';
        }
        return '//*[@id=' + xpathLiteral(element.id) + ']';
    }

    function xpathLiteral(value) {
        const text = String(value);
        if (!text.includes("'")) {
            return "'" + text + "'";
        }
        if (!text.includes('"')) {
            return '"' + text + '"';
        }
        const parts = text.split("'");
        return (
            'concat(' +
            parts.map(function (part) { return "'" + part + "'"; }).join(', "\'", ') +
            ')'
        );
    }

    function isUnique(selector) {
        try {
            const result = document.evaluate(
                'count(' + selector + ')',
                document,
                null,
                XPathResult.NUMBER_TYPE,
                null
            );
            return result.numberValue === 1;
        } catch (error) {
            return false;
        }
    }

    function directXPath(element) {
        const tag = element.tagName.toLowerCase();
        const byId = uniqueIdXPath(element);
        if (byId) {
            return byId;
        }
        const attributes = [
            'data-testid', 'data-test', 'data-qa', 'data-id', 'data-name',
            'data-role', 'itemprop', 'name', 'aria-label',
        ];
        for (const attribute of attributes) {
            const value = (element.getAttribute(attribute) || '').trim();
            if (!value || value.length > 120) {
                continue;
            }
            const selector = '//' + tag + '[@' + attribute + '=' + xpathLiteral(value) + ']';
            if (isUnique(selector)) {
                return selector;
            }
        }
        const classes = Array.from(element.classList || []).filter(function (token) {
            return /^[A-Za-z_][\w-]{1,40}$/.test(token) &&
                !/\d{4,}/.test(token) &&
                !token.startsWith('__fosscol');
        });
        for (const token of classes) {
            const selector =
                '//' + tag + "[contains(concat(' ', normalize-space(@class), ' '), " +
                xpathLiteral(' ' + token + ' ') + ')]';
            if (isUnique(selector)) {
                return selector;
            }
        }
        return '';
    }

    function segment(element) {
        const tag = element.tagName.toLowerCase();
        let index = 1;
        let sibling = element.previousElementSibling;
        while (sibling) {
            if (sibling.tagName === element.tagName) {
                index += 1;
            }
            sibling = sibling.previousElementSibling;
        }
        return tag + '[' + index + ']';
    }

    function buildXPath(element) {
        if (!(element instanceof Element)) {
            return '';
        }
        const direct = directXPath(element);
        if (direct) {
            return direct;
        }
        // Anchor the short relative chain to the nearest stable ancestor
        // (id, data-* or a unique class) instead of a long absolute path.
        let ancestor = element.parentElement;
        let prefix = '';
        let depth = 0;
        while (ancestor && depth < 7) {
            prefix = directXPath(ancestor);
            if (prefix) {
                break;
            }
            ancestor = ancestor.parentElement;
            depth += 1;
        }
        const chain = [];
        let current = element;
        while (current && current !== ancestor) {
            chain.unshift(segment(current));
            current = current.parentElement;
        }
        if (prefix) {
            return prefix + '/' + chain.join('/');
        }
        const absolute = [];
        current = element;
        while (current) {
            absolute.unshift(segment(current));
            if (current.tagName.toLowerCase() === 'html') {
                break;
            }
            current = current.parentElement;
        }
        return '/' + absolute.join('/');
    }

    function cssSelector(element) {
        const tag = element.tagName.toLowerCase();
        if (element.id) {
            return '#' + CSS.escape(element.id);
        }
        for (const attribute of ['data-testid', 'data-test', 'data-qa', 'itemprop']) {
            const value = (element.getAttribute(attribute) || '').trim();
            if (value) {
                return tag + '[' + attribute + '="' + value.replace(/"/g, '\\"') + '"]';
            }
        }
        const classes = Array.from(element.classList || []).filter(function (token) {
            return /^[A-Za-z_][\w-]{1,40}$/.test(token) &&
                !/\d{4,}/.test(token) &&
                !token.startsWith('__fosscol');
        }).slice(0, 2);
        let selector = tag + classes.map(function (token) { return '.' + CSS.escape(token); }).join('');
        try {
            if (document.querySelectorAll(selector).length === 1) {
                return selector;
            }
        } catch (error) {
            /* fall through to :nth-of-type */
        }
        const parent = element.parentElement;
        if (parent) {
            let index = 1;
            let sibling = element.previousElementSibling;
            while (sibling) {
                if (sibling.tagName === element.tagName) {
                    index += 1;
                }
                sibling = sibling.previousElementSibling;
            }
            selector += ':nth-of-type(' + index + ')';
        }
        return selector;
    }

    function absoluteUrl(value) {
        try {
            return new URL(value, document.baseURI).href;
        } catch (error) {
            return value;
        }
    }

    function describe(element) {
        const tag = element.tagName.toLowerCase();
        const base = buildXPath(element);
        let fieldType = 'xpath';
        let attribute = '';
        if (tag === 'img') {
            attribute = element.hasAttribute('data-src') ? 'data-src' : 'src';
            fieldType = 'xpath_attr';
        } else if (tag === 'a') {
            attribute = 'href';
            fieldType = 'xpath_attr';
        } else if (tag === 'meta' && element.getAttribute('content')) {
            attribute = 'content';
            fieldType = 'xpath_attr';
        } else if (tag === 'source' && element.getAttribute('src')) {
            attribute = 'src';
            fieldType = 'xpath_attr';
        }
        const text = (element.innerText || element.textContent || '')
            .replace(/\s+/g, ' ').trim();
        let preview = '';
        if (attribute) {
            preview = (element.getAttribute(attribute) || '').trim();
            if (['src', 'data-src', 'href'].indexOf(attribute) !== -1 && preview) {
                preview = absoluteUrl(preview);
            }
            if (!preview && attribute === 'data-src') {
                attribute = 'src';
                preview = absoluteUrl((element.getAttribute('src') || '').trim());
            }
        } else {
            preview = text.slice(0, 300);
        }
        const selector = attribute ? base + '/@' + attribute : base;
        return {
            xpath: base,
            css: cssSelector(element),
            selector: selector,
            type: fieldType,
            tag: tag,
            attribute: attribute,
            itemprop: (element.getAttribute('itemprop') || '').trim(),
            preview: preview,
        };
    }

    function buildDynamicPair(nameEl, valueEl) {
        if (!(nameEl instanceof Element) || !(valueEl instanceof Element)) {
            return { error: 'Нужно выбрать два элемента.' };
        }
        nameEl = normalizePickTarget(nameEl);
        valueEl = normalizePickTarget(valueEl);
        if (nameEl === valueEl || nameEl.contains(valueEl) || valueEl.contains(nameEl)) {
            return { error: 'Название и значение должны быть разными элементами.' };
        }

        let row;
        let container;
        let nameSteps = '';
        let valueSteps = '';
        const nameTerm = nameEl.closest ? nameEl.closest('dt') : null;
        const valueDef = valueEl.closest ? valueEl.closest('dd') : null;
        if (nameTerm && valueDef && nameTerm.parentElement === valueDef.parentElement) {
            row = nameTerm;
            container = nameTerm.parentElement;
            valueSteps = 'following-sibling::dd[1]';
        } else {
            row = lowestCommonAncestor(nameEl, valueEl);
            if (!row || row === document.body || row === document.documentElement) {
                return { error: 'Не удалось найти общий блок характеристики.' };
            }
            container = findContainer(row);
            nameSteps = stepsBetween(row, nameEl);
            valueSteps = stepsBetween(row, valueEl);
            if (!nameSteps || !valueSteps) {
                return { error: 'Не удалось построить путь к названию и значению.' };
            }
        }

        const nameRelative = row.tagName === 'DT' && !nameSteps ? '' : nameSteps;
        const rowSelectors = rowCandidates(row);
        for (const rowSelector of rowSelectors) {
            const rows = xpathNodes(rowSelector, container);
            if (rows.length < 2 || rows.indexOf(row) === -1) {
                continue;
            }
            const nameSelector = nameRelative
                ? rowSelector + '/' + nameRelative
                : rowSelector;
            const valueSelector = valueSteps
                ? rowSelector + '/' + valueSteps
                : '';
            if (!valueSelector) {
                continue;
            }
            const names = xpathNodes(nameSelector, container);
            const values = xpathNodes(valueSelector, container);
            if (names.length !== rows.length || values.length !== rows.length) {
                continue;
            }
            const pairs = [];
            for (let index = 0; index < Math.min(rows.length, 40); index += 1) {
                const name = normalizeText(names[index].textContent);
                const value = normalizeText(values[index].textContent);
                if (name && value) {
                    pairs.push([name, value]);
                }
            }
            if (pairs.length < 2) {
                continue;
            }
            const sourceSelector = buildXPath(container);
            if (!sourceSelector) {
                continue;
            }
            const preview = pairs.slice(0, 3).map(function (pair) {
                return pair[0] + ': ' + pair[1];
            }).join(' · ');
            return {
                dynamic: true,
                xpath: sourceSelector,
                selector: sourceSelector,
                source_xpath: sourceSelector,
                row_selector: rowSelector,
                name_selector: nameSelector,
                value_selector: valueSelector,
                pairs: pairs,
                count: rows.length,
                preview: preview,
                tag: 'dynamic',
                type: 'xpath_outer_html',
                name: 'Динамические характеристики',
                attribute: '',
            };
        }
        return {
            error: 'Не удалось обобщить XPath: количество названий и значений не совпало.',
        };
    }

    function xpathNodes(selector, context) {
        try {
            const result = document.evaluate(
                selector,
                context || document,
                null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                null
            );
            const items = [];
            for (let index = 0; index < result.snapshotLength; index += 1) {
                items.push(result.snapshotItem(index));
            }
            return items;
        } catch (error) {
            return [];
        }
    }

    function isStableClass(token) {
        return /^[A-Za-z_][\w-]{1,40}$/.test(token) &&
            !/\d{4,}/.test(token) &&
            !token.startsWith('__fosscol');
    }

    function classPredicate(token) {
        return "contains(concat(' ', normalize-space(@class), ' '), " +
            xpathLiteral(' ' + token + ' ') + ')';
    }

    function rowCandidates(row) {
        const tag = row.tagName.toLowerCase();
        const classes = Array.from(row.classList || []).filter(isStableClass);
        const candidates = [];
        for (const token of classes.slice(0, 3)) {
            const predicate = '[' + classPredicate(token) + ']';
            candidates.push('.//' + tag + predicate + '[*[2]]');
            candidates.push('.//' + tag + predicate);
        }
        if (tag === 'tr') {
            candidates.push('.//tr[td[2]]');
            candidates.push('.//tr[*[self::td or self::th][2]]');
        }
        candidates.push('.//' + tag);
        return candidates.filter(function (item, index, list) {
            return list.indexOf(item) === index;
        });
    }

    function stepsBetween(base, target) {
        const steps = [];
        let current = target;
        let guard = 0;
        while (current && current !== base && guard < 30) {
            steps.unshift(segment(current));
            current = current.parentElement;
            guard += 1;
        }
        return current === base ? steps.join('/') : '';
    }

    function normalizeText(value) {
        return (value || '').replace(/\s+/g, ' ').trim();
    }

    function lowestCommonAncestor(first, second) {
        const ancestors = new Set();
        let current = first;
        while (current) {
            ancestors.add(current);
            current = current.parentElement;
        }
        current = second;
        while (current) {
            if (ancestors.has(current)) {
                return current;
            }
            current = current.parentElement;
        }
        return document.body;
    }

    function findContainer(row) {
        const table = row.closest ? row.closest('table') : null;
        if (table && table.contains(row)) {
            return table;
        }
        return row.parentElement || document.body;
    }

    function normalizePickTarget(element) {
        if (!element.closest) {
            return element;
        }
        return element.closest('td') || element.closest('th') || element;
    }

    let tooltip = null;

    function ensureTooltip() {
        if (!tooltip || !tooltip.isConnected) {
            tooltip = document.createElement('div');
            tooltip.id = '__fosscol-picker-tip';
            (document.body || document.documentElement).appendChild(tooltip);
        }
        tooltip.style.display = 'block';
        return tooltip;
    }

    function moveTooltip(event) {
        if (!tooltip) {
            return;
        }
        const gap = 16;
        let left = event.clientX + gap;
        let top = event.clientY + gap;
        if (left + 430 > window.innerWidth) {
            left = Math.max(8, event.clientX - 440);
        }
        if (top + 42 > window.innerHeight) {
            top = Math.max(8, event.clientY - 46);
        }
        tooltip.style.left = left + 'px';
        tooltip.style.top = top + 'px';
    }

    document.addEventListener('mousemove', moveTooltip, true);

    document.addEventListener('mouseover', (event) => {
        clearHover();
        const target = event.target;
        if (
            target instanceof Element &&
            target.id !== '__fosscol-picker-banner' &&
            target.id !== '__fosscol-picker-tip'
        ) {
            hovered = target;
            hovered.classList.add('__fosscol-picker-hover');
            const info = describe(target);
            const preview = (info.preview || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const tip = ensureTooltip();
            tip.innerHTML = '<b>' + info.tag + '</b>' + (preview ? ' · ' + preview : '');
            moveTooltip(event);
        }
    }, true);

    document.addEventListener('mouseout', () => {
        clearHover();
        if (tooltip) {
            tooltip.style.display = 'none';
        }
    }, true);

    document.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        const target = event.target;
        clearHover();
        if (!(target instanceof Element)) {
            return false;
        }
        if (window.__fosscol_dynamic__) {
            if (!dynamicFirst) {
                dynamicFirst = target;
                target.classList.add('__fosscol-picker-chosen');
                updateBanner(
                    'Шаг 1 из 2: название характеристики выбрано',
                    'Теперь кликните по её значению. Esc — отмена.'
                );
                return false;
            }
            const first = dynamicFirst;
            const info = buildDynamicPair(first, target);
            if (info.error) {
                if (first.classList) {
                    first.classList.remove('__fosscol-picker-chosen');
                }
                dynamicFirst = null;
                updateBanner(
                    'Не удалось: ' + info.error,
                    'Кликните название характеристики ещё раз. Esc — отмена.'
                );
                return false;
            }
            if (first.classList) {
                first.classList.remove('__fosscol-picker-chosen');
            }
            dynamicFirst = null;
            if (tooltip) {
                tooltip.remove();
                tooltip = null;
            }
            if (window.__fosscol_pick__) {
                window.__fosscol_pick__(JSON.stringify(info));
            }
            return false;
        }
        const info = describe(target);
        if (tooltip) {
            tooltip.remove();
            tooltip = null;
        }
        if (info.xpath && window.__fosscol_pick__) {
            window.__fosscol_pick__(JSON.stringify(info));
        }
        return false;
    }, true);

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && window.__fosscol_cancel__) {
            event.preventDefault();
            window.__fosscol_cancel__();
        }
    }, true);
})();
"""


class XPathPickerWorker(QThread):
    """Open a visible Chromium, wait for a click and report its XPath."""

    picked = Signal(object)
    failed = Signal(str)
    status = Signal(str)

    def __init__(
        self,
        url: str,
        dynamic: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.url = url
        self.dynamic = dynamic
        self._stop_event = threading.Event()
        self._result: dict[str, Any] | None = None
        self._cancelled = False
        self._closed_by_user = False

    def stop(self) -> None:
        self._stop_event.set()

    def _on_pick(self, payload: Any) -> None:
        if self._stop_event.is_set():
            return
        data: Any = payload
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, TypeError, ValueError):
                data = {"xpath": payload}
        if not isinstance(data, dict):
            return
        if str(data.get("xpath") or "").strip():
            self._result = data
        elif (
            data.get("dynamic")
            and str(data.get("row_selector") or "").strip()
            and str(data.get("name_selector") or "").strip()
            and str(data.get("value_selector") or "").strip()
        ):
            self._result = data

    def _on_cancel(self) -> None:
        if not self._stop_event.is_set():
            self._cancelled = True

    def run(self) -> None:
        playwright = None
        browser = None
        context = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=False,
                args=["--window-size=1440,960", "--window-position=80,60"],
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 880},
                locale="ru-RU",
                ignore_https_errors=True,
            )
            context.add_init_script(PICKER_SCRIPT)
            page = context.new_page()
            page.expose_function("__fosscol_pick__", self._on_pick)
            page.expose_function("__fosscol_cancel__", self._on_cancel)
            self.status.emit("Загружаю страницу…")
            page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
            if self.dynamic:
                page.evaluate(
                    "window.__fosscol_setDynamic__ ? "
                    "window.__fosscol_setDynamic__() : "
                    "(window.__fosscol_dynamic__ = true)"
                )
                self.status.emit(
                    "Кликните название характеристики, затем её значение"
                )
            else:
                self.status.emit("Кликните по элементу в окне Chromium")

            while not self._stop_event.is_set():
                if self._result is not None:
                    break
                if self._cancelled:
                    break
                if page.is_closed():
                    self._closed_by_user = True
                    break
                try:
                    page.wait_for_timeout(150)
                except PlaywrightError:
                    if page.is_closed():
                        self._closed_by_user = True
                    break

            if self._result is not None and not self._stop_event.is_set():
                self.picked.emit(self._result)
            elif self._cancelled and not self._stop_event.is_set():
                self.failed.emit("Выбор отменён (Esc).")
            elif self._closed_by_user and not self._stop_event.is_set():
                self.failed.emit("Окно браузера закрыто без выбора элемента.")
        except Exception as exc:
            if not self._stop_event.is_set():
                message = str(exc).strip().splitlines()
                first_line = message[0] if message else exc.__class__.__name__
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    first_line += "\nУстановите браузер командой: playwright install chromium"
                self.failed.emit(first_line)
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass


_ITEMPROP_NAMES = {
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


def _suggest_field_name(info: dict[str, Any]) -> str:
    """Guess a human readable field name for the picked element."""

    itemprop = str(info.get("itemprop") or "").strip().split()
    if itemprop:
        label = _ITEMPROP_NAMES.get(itemprop[0].casefold())
        if label:
            return label
    tag = str(info.get("tag") or "").strip().lower()
    attribute = str(info.get("attribute") or "").strip().lower()
    if tag == "img" or attribute in {"src", "data-src"}:
        return "Фото"
    if tag == "a" or attribute == "href":
        return "Ссылка"
    if tag == "meta":
        return "Мета-поле"
    preview = str(info.get("preview") or "").strip()
    if preview:
        short = preview if len(preview) <= 40 else preview[:39].rstrip() + "…"
        return short
    return tag.capitalize() if tag else "Новое поле"


class XPathPickerDialog(QDialog):
    """Collect a URL, open Chromium and return the selected element."""

    def __init__(
        self,
        url: str = "",
        parent: QWidget | None = None,
        dynamic: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Выбрать на странице")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.resize(800, 470)
        self._worker: XPathPickerWorker | None = None
        self._closing = False
        self._suggested_name = ""
        self._picked_type = "xpath"
        self._dynamic_payload: dict[str, Any] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(9)

        title = QLabel("Выбор элемента на странице")
        title.setStyleSheet("font-size: 13pt; font-weight: 700; color: #1f2733;")
        layout.addWidget(title)

        hint = QLabel(
            "Введите адрес и откройте Chromium. Наведите курсор — элемент подсветится, "
            "клик по нему вернёт устойчивый XPath, тип поля и превью значения. "
            "Для изображений и ссылок автоматически выбирается атрибут src/data-src/href. "
            "В режиме динамических характеристик кликните сначала название, затем значение."
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        url_row = QHBoxLayout()
        url_label = QLabel("Страница:")
        url_label.setMinimumWidth(70)
        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://example.com/catalog")
        self.url_edit.returnPressed.connect(self.open_browser)
        url_row.addWidget(url_label)
        url_row.addWidget(self.url_edit, 1)
        self.open_button = QPushButton("Открыть браузер")
        self.open_button.setProperty("variant", "primary")
        self.open_button.clicked.connect(self.open_browser)
        url_row.addWidget(self.open_button)
        layout.addLayout(url_row)

        mode_row = QHBoxLayout()
        mode_label = QLabel("Режим:")
        mode_label.setMinimumWidth(70)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Обычный элемент", "element")
        self.mode_combo.addItem("Динамические характеристики", "dynamic")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.mode_combo, 1)
        layout.addLayout(mode_row)

        attribute_row = QHBoxLayout()
        attribute_label = QLabel("Атрибут:")
        attribute_label.setMinimumWidth(70)
        self.attribute_checkbox = QCheckBox("Добавить /@")
        self.attribute_checkbox.toggled.connect(self._update_preview)
        self.attribute_combo = QComboBox()
        self.attribute_combo.setEditable(True)
        self.attribute_combo.addItems(["src", "href", "data-src", "alt", "title", "content"])
        self.attribute_combo.setCurrentText("src")
        self.attribute_combo.currentTextChanged.connect(self._update_preview)
        attribute_row.addWidget(attribute_label)
        attribute_row.addWidget(self.attribute_checkbox)
        attribute_row.addWidget(self.attribute_combo, 1)
        layout.addLayout(attribute_row)

        xpath_row = QHBoxLayout()
        xpath_label = QLabel("XPath:")
        xpath_label.setMinimumWidth(70)
        self.xpath_edit = QLineEdit()
        self.xpath_edit.setPlaceholderText("Результат появится после клика по элементу")
        self.xpath_edit.textChanged.connect(self._update_preview)
        xpath_row.addWidget(xpath_label)
        xpath_row.addWidget(self.xpath_edit, 1)
        layout.addLayout(xpath_row)

        preview_row = QHBoxLayout()
        preview_label = QLabel("Значение:")
        preview_label.setMinimumWidth(70)
        self.preview_edit = QLineEdit()
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setPlaceholderText("Превью появится после клика по элементу")
        preview_row.addWidget(preview_label)
        preview_row.addWidget(self.preview_edit, 1)
        layout.addLayout(preview_row)

        css_row = QHBoxLayout()
        css_label = QLabel("CSS:")
        css_label.setMinimumWidth(70)
        self.css_edit = QLineEdit()
        self.css_edit.setReadOnly(True)
        self.css_edit.setPlaceholderText("CSS-селектор (справочно)")
        css_row.addWidget(css_label)
        css_row.addWidget(self.css_edit, 1)
        layout.addLayout(css_row)

        self.dynamic_group = QWidget()
        dynamic_layout = QGridLayout(self.dynamic_group)
        dynamic_layout.setContentsMargins(0, 4, 0, 0)
        dynamic_layout.setHorizontalSpacing(10)
        dynamic_layout.setVerticalSpacing(6)
        dynamic_layout.addWidget(QLabel("XPath строк:"), 0, 0)
        self.row_xpath_edit = QLineEdit()
        self.row_xpath_edit.setReadOnly(True)
        dynamic_layout.addWidget(self.row_xpath_edit, 0, 1)
        dynamic_layout.addWidget(QLabel("XPath названий:"), 1, 0)
        self.name_xpath_edit = QLineEdit()
        self.name_xpath_edit.setReadOnly(True)
        dynamic_layout.addWidget(self.name_xpath_edit, 1, 1)
        dynamic_layout.addWidget(QLabel("XPath значений:"), 2, 0)
        self.value_xpath_edit = QLineEdit()
        self.value_xpath_edit.setReadOnly(True)
        dynamic_layout.addWidget(self.value_xpath_edit, 2, 1)
        dynamic_layout.addWidget(QLabel("Найдено пар:"), 3, 0)
        self.dynamic_count_label = QLabel("—")
        dynamic_layout.addWidget(self.dynamic_count_label, 3, 1)
        self.dynamic_preview_label = QLabel("")
        self.dynamic_preview_label.setObjectName("sectionHint")
        self.dynamic_preview_label.setWordWrap(True)
        dynamic_layout.addWidget(self.dynamic_preview_label, 4, 0, 1, 2)
        self.dynamic_group.setVisible(False)
        layout.addWidget(self.dynamic_group)

        self.status_label = QLabel("Браузер ещё не открыт.")
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.use_button = QPushButton("Использовать")
        self.use_button.setProperty("variant", "primary")
        self.use_button.setEnabled(False)
        self.use_button.clicked.connect(self.accept)
        buttons.addWidget(self.use_button)
        layout.addLayout(buttons)

        if dynamic:
            index = self.mode_combo.findData("dynamic")
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
        self._update_preview()
        if not url:
            self.url_edit.setFocus()
        elif re.match(r"^https?://\S+", url.strip(), re.IGNORECASE):
            # The test URL is already known: start Chromium right away.
            QTimer.singleShot(0, self.open_browser)

    def _set_status(self, text: str, color: str = "") -> None:
        self.status_label.setText(text)
        if color:
            self.status_label.setStyleSheet(f"color: {color};")
        else:
            self.status_label.setStyleSheet("")

    def _on_mode_changed(self) -> None:
        dynamic = self.mode_combo.currentData() == "dynamic"
        self.dynamic_group.setVisible(dynamic)
        self.attribute_checkbox.setEnabled(not dynamic)
        self.attribute_combo.setEnabled(not dynamic and self.attribute_checkbox.isChecked())
        self._dynamic_payload = None
        self.dynamic_count_label.setText("—")
        self.dynamic_preview_label.setText("")
        if dynamic:
            self._set_status(
                "Динамический режим: кликните название характеристики, затем её значение."
            )
        else:
            self._set_status("Браузер ещё не открыт.")
        self._update_preview()

    def open_browser(self) -> None:
        url = self.url_edit.text().strip()
        if not url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, "Пикер XPath", "Введите корректный http:// или https:// адрес.")
            return
        self._stop_worker()
        self.open_button.setEnabled(False)
        self.open_button.setText("Загрузка…")
        self._set_status("Открываю Chromium…")
        worker = XPathPickerWorker(
            url,
            self.mode_combo.currentData() == "dynamic",
            self,
        )
        worker.picked.connect(self._on_picked)
        worker.failed.connect(self._on_failed)
        worker.status.connect(self._set_status)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _stop_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.stop()
        if worker.isRunning() and not worker.wait(10000):
            _detach_worker(worker)

    def _on_picked(self, payload: Any) -> None:
        if self._closing:
            return
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            info = payload
        elif isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed = {"xpath": payload}
            info = parsed if isinstance(parsed, dict) else {"xpath": payload}

        if info.get("dynamic"):
            self._apply_dynamic_picked(info)
            return
        self._dynamic_payload = None
        self.dynamic_group.setVisible(self.mode_combo.currentData() == "dynamic")

        xpath = str(info.get("xpath") or info.get("selector") or "").strip()
        if not xpath:
            self._set_status("Не удалось построить XPath для элемента.", "#c62828")
            return

        attribute = str(info.get("attribute") or "").strip()
        selector = str(info.get("selector") or "").strip()
        if not attribute and "/@" in selector:
            attribute = selector.rsplit("/@", 1)[1].strip()
        base_xpath = re.sub(r"/@[A-Za-z_][-A-Za-z0-9_.:]*$", "", xpath)

        self.xpath_edit.setText(base_xpath)
        self.attribute_checkbox.setChecked(bool(attribute))
        if attribute:
            if self.attribute_combo.findText(attribute) < 0:
                self.attribute_combo.addItem(attribute)
            self.attribute_combo.setCurrentText(attribute)
        self.preview_edit.setText(str(info.get("preview") or ""))
        self.css_edit.setText(str(info.get("css") or ""))

        field_type = str(info.get("type") or "").strip()
        if field_type not in {"xpath", "xpath_attr"}:
            field_type = "xpath_attr" if attribute else "xpath"
        self._picked_type = field_type
        self._suggested_name = _suggest_field_name(info)
        self._set_status(
            f"Выбран элемент <{info.get('tag') or '?'}>. "
            "Проверьте XPath, при необходимости переименуйте поле и нажмите «Использовать».",
            "#1a7f37",
        )
        self.open_button.setText("Открыть снова")

    def _apply_dynamic_picked(self, info: dict[str, Any]) -> None:
        row_selector = str(info.get("row_selector") or "").strip()
        name_selector = str(info.get("name_selector") or "").strip()
        value_selector = str(info.get("value_selector") or "").strip()
        source_selector = str(
            info.get("source_xpath") or info.get("selector") or info.get("xpath") or ""
        ).strip()
        if not (row_selector and name_selector and value_selector and source_selector):
            self._set_status(
                "Не удалось построить динамические XPath. Попробуйте ещё раз.",
                "#c62828",
            )
            self._update_preview()
            return

        pairs = [
            (str(name), str(value))
            for name, value in (info.get("pairs") or [])
            if str(name).strip() and str(value).strip()
        ]
        if len(pairs) < 2:
            self._set_status(
                "Найдено меньше двух пар характеристик. Выберите другую таблицу или блок.",
                "#c62828",
            )
            self._update_preview()
            return

        self._dynamic_payload = {
            **info,
            "selector": source_selector,
            "xpath": source_selector,
            "source_xpath": source_selector,
            "row_selector": row_selector,
            "name_selector": name_selector,
            "value_selector": value_selector,
            "pairs": pairs,
        }
        self.dynamic_group.setVisible(True)
        self.row_xpath_edit.setText(row_selector)
        self.name_xpath_edit.setText(name_selector)
        self.value_xpath_edit.setText(value_selector)
        count = len(pairs)
        self.dynamic_count_label.setText(str(count))
        preview = " · ".join(f"{name}: {value}" for name, value in pairs[:5])
        self.dynamic_preview_label.setText(f"Превью: {preview}")

        self.xpath_edit.setText(source_selector)
        self.attribute_checkbox.setChecked(False)
        self.preview_edit.setText(preview)
        self.css_edit.setText("")
        self._picked_type = "xpath_outer_html"
        self._suggested_name = "Динамические характеристики"
        self._set_status(
            f"Динамические характеристики: найдено пар — {count}. "
            "Названия и значения сопоставлены по индексу строки.",
            "#1a7f37",
        )
        self.open_button.setText("Открыть снова")
        self.open_button.setEnabled(True)
        self._update_preview()

    def _on_failed(self, message: str) -> None:
        if self._closing:
            return
        self._set_status(message, "#c62828")
        self.open_button.setEnabled(True)
        self.open_button.setText("Открыть браузер")

    def _on_worker_finished(self) -> None:
        if self._closing:
            return
        self.open_button.setEnabled(True)
        if self.open_button.text() == "Загрузка…":
            self.open_button.setText("Открыть браузер")
        self._worker = None

    def _update_preview(self) -> None:
        dynamic_mode = self.mode_combo.currentData() == "dynamic"
        self.use_button.setEnabled(
            bool(self._dynamic_payload) or bool(self.xpath_edit.text().strip())
        )
        self.attribute_checkbox.setEnabled(not dynamic_mode)
        self.attribute_combo.setEnabled(
            not dynamic_mode and self.attribute_checkbox.isChecked()
        )

    @property
    def selector(self) -> str:
        if self._dynamic_payload:
            return str(self._dynamic_payload.get("selector") or "").strip()
        xpath = self.xpath_edit.text().strip()
        if not xpath or not self.attribute_checkbox.isChecked():
            return xpath
        attribute = self.attribute_combo.currentText().strip()
        if not attribute:
            return xpath
        if re.search(r"/@[A-Za-z_][-A-Za-z0-9_.:]*$", xpath):
            return xpath
        return f"{xpath.rstrip('/')}/@{attribute}"

    @property
    def field_type(self) -> str:
        if self._dynamic_payload:
            return "xpath_outer_html"
        if self.attribute_checkbox.isChecked() and self.attribute_combo.currentText().strip():
            return "xpath_attr"
        return "xpath" if self._picked_type != "xpath_outer_html" else self._picked_type

    @property
    def dynamic(self) -> bool:
        return bool(self._dynamic_payload)

    @property
    def dynamic_payload(self) -> dict[str, Any]:
        return dict(self._dynamic_payload or {})

    @property
    def row_selector(self) -> str:
        return str((self._dynamic_payload or {}).get("row_selector") or "")

    @property
    def name_selector(self) -> str:
        return str((self._dynamic_payload or {}).get("name_selector") or "")

    @property
    def value_selector(self) -> str:
        return str((self._dynamic_payload or {}).get("value_selector") or "")

    @property
    def dynamic_pairs(self) -> list[tuple[str, str]]:
        return list((self._dynamic_payload or {}).get("pairs") or [])

    @property
    def suggested_name(self) -> str:
        return self._suggested_name

    @property
    def preview(self) -> str:
        return self.preview_edit.text().strip()

    @property
    def css_selector(self) -> str:
        return self.css_edit.text().strip()

    def done(self, result: int) -> None:
        self._closing = True
        self._stop_worker()
        super().done(result)

    def closeEvent(self, event: Any) -> None:
        self._closing = True
        self._stop_worker()
        event.accept()
