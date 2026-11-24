"""Reading and writing campaign JSON files.

A campaign lives in a single ``*.json`` file inside the ``campaigns``
directory (sub-directories are supported as well).  Writes are atomic so
that a crash can never leave a half-written campaign behind.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from core.schema import CampaignValidationError, normalize_campaign, validate_campaign


class StorageError(RuntimeError):
    """Raised for filesystem or JSON problems."""


@dataclass(frozen=True)
class CampaignRecord:
    """Lightweight description of a campaign file."""

    name: str
    category: str
    path: Path
    modified: float

    @property
    def stem(self) -> str:
        return self.path.stem


class CampaignStorage:
    """CRUD facade for the campaigns directory."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last_errors: list[tuple[Path, str]] = []

    # ------------------------------------------------------------------
    # Paths and names
    # ------------------------------------------------------------------
    def _inside_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.directory)
            return True
        except ValueError:
            return False

    def _resolve(self, path_or_name: str | os.PathLike[str]) -> Path:
        candidate = Path(path_or_name)
        if candidate.is_absolute():
            path = candidate
        elif candidate.suffix.lower() == ".json" or len(candidate.parts) > 1:
            path = (self.directory / candidate)
        else:
            path = self.directory / f"{candidate}.json"
        path = path.resolve()
        if not self._inside_root(path):
            raise StorageError(f"Файл находится вне папки кампаний: {path}")
        return path

    @staticmethod
    def slugify(name: str) -> str:
        """Turn a campaign name into a safe file name (Unicode is kept)."""

        cleaned = name.strip()
        cleaned = re.sub(r'[\\/:*?"<>|]+', "-", cleaned)
        cleaned = re.sub(r"[\x00-\x1f\x7f]+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        if not cleaned:
            cleaned = "campaign"
        if len(cleaned) > 120:
            cleaned = cleaned[:120].rstrip(" .")
        return cleaned

    def path_for(self, name: str) -> Path:
        return self.directory / f"{self.slugify(name)}.json"

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def list_campaigns(self) -> list[CampaignRecord]:
        """Scan the campaign directory recursively.

        Broken files are reported through ``last_errors`` and skipped.
        """

        self.last_errors = []
        records: list[CampaignRecord] = []
        for path in sorted(self.directory.rglob("*.json")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                data = self._read_json(path)
                campaign = normalize_campaign(data)
                name = str(campaign.get("campaign_name") or path.stem).strip() or path.stem
                category = str(campaign.get("category") or "").strip()
                records.append(
                    CampaignRecord(
                        name=name,
                        category=category,
                        path=path.resolve(),
                        modified=path.stat().st_mtime,
                    )
                )
            except (StorageError, OSError, ValueError) as exc:
                self.last_errors.append((path, str(exc)))
        records.sort(key=lambda item: (item.category.casefold(), item.name.casefold()))
        return records

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise StorageError(f"Некорректный JSON: строка {exc.lineno}, колонка {exc.colno}.") from exc
        except OSError as exc:
            raise StorageError(f"Не удалось прочитать файл: {exc}") from exc
        if not isinstance(data, dict):
            raise StorageError("Корень JSON-документа должен быть объектом.")
        return data

    def load(self, path_or_name: str | os.PathLike[str]) -> dict[str, Any]:
        path = self._resolve(path_or_name)
        if not path.exists():
            raise StorageError(f"Кампания не найдена: {path}")
        data = self._read_json(path)
        campaign = normalize_campaign(data)
        if not str(data.get("campaign_name") or "").strip():
            campaign["campaign_name"] = path.stem
        return campaign

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def save(
        self,
        campaign: dict[str, Any],
        previous_path: str | os.PathLike[str] | None = None,
    ) -> Path:
        """Validate, normalise and atomically save a campaign.

        When the campaign was renamed, ``previous_path`` points to the old
        file and is removed after the new file has been written.
        """

        normalized = normalize_campaign(campaign)
        errors = validate_campaign(normalized, for_run=False)
        if errors:
            raise CampaignValidationError(errors)

        target = self.path_for(str(normalized["campaign_name"]))
        previous = self._resolve(previous_path) if previous_path else None

        if target.exists() and previous != target:
            existing = normalize_campaign(self._read_json(target))
            if existing.get("campaign_name") != normalized.get("campaign_name"):
                raise StorageError(f"Файл кампании уже существует: {target.name}")
            raise StorageError(f"Кампания с таким именем уже существует: {target.name}")

        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=".campaign-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            temp_name = None
        except OSError as exc:
            raise StorageError(f"Не удалось сохранить кампанию: {exc}") from exc
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

        if previous and previous != target:
            try:
                previous.unlink()
            except OSError as exc:
                raise StorageError(f"Новая кампания сохранена, но старый файл не удалён: {exc}") from exc
        return target

    def create(self, name: str, category: str = "") -> tuple[dict[str, Any], Path]:
        from core.schema import new_campaign

        campaign = new_campaign(name, category)
        path = self.save(campaign)
        return campaign, path

    def delete(self, path_or_name: str | os.PathLike[str]) -> None:
        path = self._resolve(path_or_name)
        if not path.exists():
            raise StorageError(f"Кампания не найдена: {path}")
        try:
            path.unlink()
        except OSError as exc:
            raise StorageError(f"Не удалось удалить кампанию: {exc}") from exc

    def duplicate(self, path_or_name: str | os.PathLike[str], new_name: str) -> Path:
        source = self._resolve(path_or_name)
        campaign = self.load(source)
        campaign["campaign_name"] = new_name
        return self.save(campaign)

    def rename(self, path_or_name: str | os.PathLike[str], new_name: str) -> Path:
        source = self._resolve(path_or_name)
        campaign = self.load(source)
        campaign["campaign_name"] = new_name
        return self.save(campaign, previous_path=source)

    def set_category(self, path_or_name: str | os.PathLike[str], category: str) -> Path:
        path = self._resolve(path_or_name)
        campaign = self.load(path)
        campaign["category"] = category
        return self.save(campaign, previous_path=path)

    def update_categories(self, records: Iterable[CampaignRecord], category: str) -> list[str]:
        """Assign a new category to several campaigns, return error messages."""

        errors: list[str] = []
        for record in records:
            try:
                campaign = self.load(record.path)
                campaign["category"] = category
                self.save(campaign, previous_path=record.path)
            except (StorageError, CampaignValidationError) as exc:
                errors.append(f"{record.name}: {exc}")
        return errors
