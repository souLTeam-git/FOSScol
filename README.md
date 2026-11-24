# FOSScol

FOSScol — настольный визуальный веб-парсер для Windows и Linux на Python,
PySide6 и Playwright. Позволяет без программирования настроить кампанию,
выбрать элементы прямо в браузере, собрать данные и выгрузить их в Excel или
CSV.

## Возможности

- **Кампании** — дерево кампаний с категориями, создание, копирование,
  сохранение и удаление; каждая кампания хранится в JSON.
- **Визуальный пикер** — выбор элементов в Chromium с подсветкой, устойчивыми
  XPath и автоопределением текста, ссылок и атрибутов изображений.
- **Playwright/HTTP и прокси** — обход через Chromium или HTTP, пул прокси,
  несколько потоков и ограничение по хостам.
- **Сценарии** — клики, наведение, прокрутка, паузы, сбор ссылок и переходы
  по страницам с настройкой глубины и задержек.
- **Извлечение полей** — XPath (текст, HTML, атрибут, список атрибутов),
  регулярные выражения, JSON-LD, OpenGraph и микроразметка; автозаполнение
  полей товара и разбор таблиц характеристик.
- **Тест на 1 URL** — проверка всех полей кампании на одной странице без
  очереди и записи в результаты.
- **Экспорт** — XLSX и CSV с колонкой статуса и приоритетным порядком
  динамических колонок.

## Плагины

Все плагины — обычные `.py`-файлы в папке `plugins/`. Они включены в общую
цепочку обработки каждой строки результата.

- `specs_parser.py` — разбирает HTML-таблицу характеристик на отдельные
  колонки «название → значение».
- `list_separator.py` — склеивает значения столбца «все картинки» через
  выбранный разделитель, например `"url1+url2"`.
- Если плагин отключён или удалён, списковые значения автоматически склеиваются
  через запятую (,).
Включать, устанавливать, удалять и обновлять плагины можно на вкладке
**«Плагины и Экспорт»**. Настройки хранятся в JSON кампании. Если файл плагина
удалён, кампания продолжает работать, а плагин просто пропускается.

Минимальный шаблон плагина:

```python
from typing import Any

from core.plugins.base import BasePlugin


class MyPlugin(BasePlugin):
    id = "my_plugin"
    name = "Мой плагин"
    description = "Короткое описание"
    order = 100  # меньшее значение выполняется раньше

    def default_config(self) -> dict:
        return {"enabled": False, "prefix": ""}

    def config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "prefix",
                "label": "Префикс",
                "type": "str",
                "default": "",
                "placeholder": "Например, NEW: ",
            },
        ]

    def process_row(
        self,
        row: dict[str, Any],
        config: dict[str, Any],
        campaign: dict[str, Any],
    ) -> dict[str, Any]:
        if config.get("enabled"):
            prefix = str(config.get("prefix") or "")
            row["Название"] = prefix + str(row.get("Название") or "")
        return row
```

Подробнее — в `plugins/README.md`.

Подробное руководство по созданию своих плагинов и примеры — в [PLUGINS.md](PLUGINS.md).

## Быстрый запуск

**Windows**

```bat
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
.venv\Scripts\python main.py
```

**Linux**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python main.py
```

## Структура

```text
main.py          точка входа
campaigns/       кампании в JSON
plugins/         все плагины (.py)
core/            движок, извлечение, плагинная инфраструктура
ui/              главное окно, вкладки и визуальный пикер
exports/         результаты экспорта
assets/          логотипы и ресурсы интерфейса
tests/           локальные тесты
```
