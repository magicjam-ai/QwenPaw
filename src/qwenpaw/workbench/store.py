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

    def daily_summary_dir(self) -> Path:
        return self.data_root / "summaries" / "daily"

    def active_issue_dir(self) -> Path:
        return self.data_root / "issues" / "active"

    def meeting_dir(self) -> Path:
        return self.data_root / "meetings"

    def people_dir(self) -> Path:
        return self.data_root / "people"

    def ensure_directories(self) -> None:
        for rel_path in (
            "cache/raw",
            "summaries",
            "summaries/daily",
            "people",
            "meetings",
            "comms",
            "issues",
            "issues/active",
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

    async def export_sot(self, radar: DailyRadar) -> None:
        async with self._lock:
            self.ensure_directories()
            self._write_daily_summary(radar)
            self._write_active_issues(radar)
            self._write_meetings(radar)
            self._write_people(radar)

    def _safe_slug(self, value: str) -> str:
        slug = "".join(
            char if char.isalnum() or char in ("-", "_") else "-"
            for char in value.strip()
        ).strip("-")
        return slug[:80] or "untitled"

    def _write_markdown(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(content, encoding="utf-8", newline="\n")
        tmp_path.replace(path)

    def _write_daily_summary(self, radar: DailyRadar) -> None:
        lines = [
            "---",
            f"date: {radar.date}",
            "type: daily-radar",
            "---",
            "",
            f"# Daily Radar {radar.date}",
            "",
            "## Coverage",
            "",
            f"- Tasks: {radar.coverage.tasks}",
            f"- Meetings: {radar.coverage.calendar_events}",
            f"- Chat messages: {radar.coverage.chat_messages}",
            f"- Sources: {', '.join(radar.coverage.sources)}",
            "",
            "## Highlights",
            "",
        ]
        lines.extend(f"- [{item.priority}] {item.title}" for item in radar.highlights)
        lines.extend(["", "## Risks", ""])
        lines.extend(f"- [{item.priority}] {item.title}" for item in radar.sections.risks)
        lines.extend(["", "## Questions", ""])
        lines.extend(f"- [{item.priority}] {item.title}" for item in radar.sections.questions)
        self._write_markdown(
            self.daily_summary_dir() / f"{radar.date}.md",
            "\n".join(lines).rstrip() + "\n",
        )

    def _write_active_issues(self, radar: DailyRadar) -> None:
        for insight in (*radar.sections.risks, *radar.sections.questions):
            if insight.status in {"ignored", "converted"}:
                continue
            payload = {
                "date": radar.date,
                "insight": _dump_model(insight),
            }
            self._write_json(
                self.active_issue_dir() / f"{self._safe_slug(insight.id)}.json",
                payload,
            )

    def _write_meetings(self, radar: DailyRadar) -> None:
        for insight in radar.sections.key_meetings:
            self._write_json(
                self.meeting_dir() / f"{self._safe_slug(insight.id)}.json",
                {"date": radar.date, "insight": _dump_model(insight)},
            )

    def _write_people(self, radar: DailyRadar) -> None:
        for insight in radar.sections.key_people:
            self._write_json(
                self.people_dir() / f"{self._safe_slug(insight.title)}.json",
                {"date": radar.date, "insight": _dump_model(insight)},
            )
