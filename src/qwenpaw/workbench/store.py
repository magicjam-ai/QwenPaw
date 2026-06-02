# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import DailyRadar, WorkbenchConfig, WorkbenchRawRecord

ModelT = TypeVar("ModelT", bound=BaseModel)


def _dump_model(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def _raw_record_key(payload: dict[str, Any]) -> tuple[str, str] | None:
    source_type = payload.get("source_type")
    source_id = payload.get("source_id")
    if isinstance(source_type, str) and isinstance(source_id, str):
        source_type = source_type.strip()
        source_id = source_id.strip()
        if source_type and source_id:
            return source_type, source_id
    return None


class WorkbenchStore:
    def __init__(self, working_dir: Path):
        self.working_dir = Path(working_dir)
        self.data_root = self.working_dir / ".workbench"
        self._lock = asyncio.Lock()

    @property
    def config_path(self) -> Path:
        return self.data_root / "config.json"

    def raw_path(self, date: str) -> Path:
        return self.data_root / "cache" / "raw" / f"{date}.json"

    def radar_path(self, date: str) -> Path:
        return self.data_root / "summaries" / f"daily-radar-{date}.json"

    def issue_path(self, insight_id: str) -> Path:
        safe_id = "".join(
            char if char.isalnum() or char in ("-", "_") else "_"
            for char in insight_id
        )
        return self.data_root / "issues" / f"{safe_id}.json"

    def ensure_directories(self) -> None:
        for rel_path in (
            "cache/raw",
            "summaries",
            "people",
            "meetings",
            "comms",
            "issues",
        ):
            (self.data_root / rel_path).mkdir(parents=True, exist_ok=True)

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    async def read_config(self) -> WorkbenchConfig | None:
        async with self._lock:
            payload = self._read_json(self.config_path, None)
        if not isinstance(payload, dict):
            return None
        return WorkbenchConfig(**payload)

    async def write_config(self, config: WorkbenchConfig) -> WorkbenchConfig:
        async with self._lock:
            self.ensure_directories()
            self._write_json(self.config_path, _dump_model(config))
        return config

    async def append_raw_records(
        self,
        date: str,
        records: list[WorkbenchRawRecord],
    ) -> list[WorkbenchRawRecord]:
        async with self._lock:
            self.ensure_directories()
            path = self.raw_path(date)
            existing_payload = self._read_json(path, [])
            existing = (
                existing_payload if isinstance(existing_payload, list) else []
            )
            next_payload: list[dict[str, Any]] = []
            index_by_key: dict[tuple[str, str], int] = {}
            for item in existing:
                if not isinstance(item, dict):
                    continue
                key = _raw_record_key(item)
                if key is not None and key in index_by_key:
                    next_payload[index_by_key[key]] = item
                    continue
                if key is not None:
                    index_by_key[key] = len(next_payload)
                next_payload.append(item)
            for record in records:
                payload = _dump_model(record)
                key = _raw_record_key(payload)
                if key is not None and key in index_by_key:
                    next_payload[index_by_key[key]] = payload
                    continue
                if key is not None:
                    index_by_key[key] = len(next_payload)
                next_payload.append(payload)
            self._write_json(path, next_payload)
        return records

    async def read_raw_records(self, date: str) -> list[WorkbenchRawRecord]:
        async with self._lock:
            payload = self._read_json(self.raw_path(date), [])
        if not isinstance(payload, list):
            return []
        return [
            WorkbenchRawRecord(**item)
            for item in payload
            if isinstance(item, dict)
        ]

    async def write_radar(self, radar: DailyRadar) -> DailyRadar:
        async with self._lock:
            self.ensure_directories()
            self._write_json(self.radar_path(radar.date), _dump_model(radar))
        return radar

    async def read_radar(self, date: str) -> DailyRadar | None:
        async with self._lock:
            payload = self._read_json(self.radar_path(date), None)
        if not isinstance(payload, dict):
            return None
        return DailyRadar(**payload)

    async def write_issue(
        self,
        insight_id: str,
        payload: dict[str, Any],
    ) -> None:
        async with self._lock:
            self.ensure_directories()
            self._write_json(self.issue_path(insight_id), payload)
