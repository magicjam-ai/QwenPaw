# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, time
from typing import Iterable

from ..constant import WORKING_DIR
from .models import (
    DailyRadar,
    DailyRadarCoverage,
    DailyRadarSections,
    WorkbenchCollectionDiagnostic,
    WorkbenchCollectMode,
    WorkbenchCollectSource,
    InsightStatus,
    WorkbenchConfig,
    WorkbenchDailyRadarScheduleConfig,
    WorkbenchInsight,
    WorkbenchRawRecord,
    WorkbenchSourceRef,
    WorkbenchWorkspaceConfig,
)
from .store import WorkbenchStore, _dump_model

_RAW_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:ou|cli|open|user|union)_[A-Za-z0-9_-]+\b",
    re.IGNORECASE,
)
_RAW_IDENTIFIER_PAREN_PATTERN = re.compile(
    r"\((?:ou|cli|open|user|union)_[A-Za-z0-9_-]+\)",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _insight_id(kind: str, source_type: str, source_id: str, title: str) -> str:
    key = f"{kind}:{source_type}:{source_id}:{title}".encode("utf-8")
    digest = hashlib.sha1(key).hexdigest()[:12]
    return f"{kind}-{digest}"


def _source_ref(record: WorkbenchRawRecord) -> WorkbenchSourceRef:
    return WorkbenchSourceRef(
        source_type=record.source_type,
        source_id=record.source_id,
        title=_sanitize_visible_text(record.title),
        url=record.metadata.get("url")
        if isinstance(record.metadata.get("url"), str)
        else None,
        excerpt=_sanitize_visible_text(record.summary),
    )


def _coverage(records: Iterable[WorkbenchRawRecord]) -> DailyRadarCoverage:
    sources: set[str] = set()
    tasks = 0
    meetings = 0
    chats = 0
    for record in records:
        sources.add(record.source_type)
        if record.source_type == "lark_task":
            tasks += 1
        elif record.source_type == "lark_calendar":
            meetings += 1
        elif record.source_type == "lark_message":
            chats += 1
    return DailyRadarCoverage(
        chat_messages=chats,
        calendar_events=meetings,
        tasks=tasks,
        sources=sorted(sources),
    )


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone()
    except ValueError:
        return None


def _target_day_bounds(date: str) -> tuple[datetime, datetime]:
    target = datetime.fromisoformat(date).date()
    tz = datetime.now().astimezone().tzinfo
    start = datetime.combine(target, time.min, tzinfo=tz)
    end = datetime.combine(target, time.max, tzinfo=tz)
    return start, end


def _is_raw_identifier(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("ou_", "cli_", "open_", "user_", "union_"))


def _sanitize_visible_text(value: str) -> str:
    if not value:
        return ""
    sanitized = _RAW_IDENTIFIER_PAREN_PATTERN.sub("", value)
    sanitized = _RAW_IDENTIFIER_PATTERN.sub("", sanitized)
    return re.sub(r"[ \t]{2,}", " ", sanitized).strip()


def _clean_people(people: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for person in people:
        value = person.strip()
        if not value or _is_raw_identifier(value):
            continue
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def _is_broadcast_task(record: WorkbenchRawRecord) -> bool:
    text = f"{record.title} {record.summary}"
    return any(marker in text for marker in ("@所有人", "@all", "@ All", "所有人"))


def _is_stale_task(record: WorkbenchRawRecord, target_date: str) -> bool:
    due_at = _parse_dt(record.due_at)
    if due_at is None:
        return False
    start, _ = _target_day_bounds(target_date)
    return due_at < start


def _is_today_meeting(record: WorkbenchRawRecord, target_date: str) -> bool:
    starts_at = _parse_dt(record.starts_at)
    if starts_at is None:
        return True
    start, end = _target_day_bounds(target_date)
    return start <= starts_at <= end


def _looks_like_ci_message(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "jenkins",
            "pipeline",
            "build success",
            "build failed",
            "failed stage",
            "artifact url",
        )
    )


def _is_success_ci_message(text: str) -> bool:
    lowered = text.lower()
    return _looks_like_ci_message(text) and any(
        marker in lowered for marker in ("build success", "[ok]", "success")
    ) and not any(
        marker in lowered for marker in ("failed", "failure", "blocked")
    )


def _is_failed_ci_message(text: str) -> bool:
    lowered = text.lower()
    return _looks_like_ci_message(text) and any(
        marker in lowered for marker in ("failed", "failure", "failed stage", "blocked")
    )


class WorkbenchService:
    def __init__(self, store: WorkbenchStore, lark_collector=None):
        self.store = store
        self.lark_collector = lark_collector
        self.last_collection_errors: dict[str, str] = {}
        self.last_collection_diagnostics: list[WorkbenchCollectionDiagnostic] = []

    def default_config(self) -> WorkbenchConfig:
        return WorkbenchConfig(
            workspace=WorkbenchWorkspaceConfig(
                data_root=self.store.data_root,
            ),
        )

    async def get_config(self) -> WorkbenchConfig:
        config = await self.store.read_config()
        if config is not None:
            return config
        return await self.store.write_config(self.default_config())

    async def init_workbench(self) -> WorkbenchConfig:
        config = await self.get_config()
        self.store.ensure_directories()
        return await self.store.write_config(config)

    async def update_config(
        self,
        *,
        daily_radar_schedule: WorkbenchDailyRadarScheduleConfig | None = None,
    ) -> WorkbenchConfig:
        config = await self.get_config()
        if daily_radar_schedule is not None:
            config.features.daily_radar_schedule = daily_radar_schedule
            config.features.daily_radar = (
                "enabled" if daily_radar_schedule.enabled else "disabled"
            )
        return await self.store.write_config(config)

    async def ensure_daily_radar_schedule(
        self,
    ) -> tuple[WorkbenchDailyRadarScheduleConfig, bool, bool]:
        config = await self.get_config()
        schedule = config.features.daily_radar_schedule
        created = not bool(schedule.job_id)
        updated = False
        next_schedule = schedule.model_copy(
            update={
                "enabled": True,
                "cron": schedule.cron or "30 8 * * mon-fri",
                "timezone": schedule.timezone or "Asia/Shanghai",
                "output_to_inbox": True,
                "job_id": schedule.job_id or "workbench-daily-radar",
            },
        )
        if next_schedule != schedule or config.features.daily_radar != "enabled":
            updated = True
            await self.update_config(daily_radar_schedule=next_schedule)
        return next_schedule, created, updated

    async def disable_daily_radar_schedule(
        self,
    ) -> WorkbenchDailyRadarScheduleConfig:
        config = await self.get_config()
        schedule = config.features.daily_radar_schedule.model_copy(
            update={"enabled": False},
        )
        await self.update_config(daily_radar_schedule=schedule)
        return schedule

    async def collect(
        self,
        *,
        records: list[dict | WorkbenchRawRecord],
        date: str | None = None,
        mode: WorkbenchCollectMode = "manual",
        sources: list[WorkbenchCollectSource] | None = None,
        chat_keywords: list[str] | None = None,
    ) -> tuple[str, list[WorkbenchRawRecord], DailyRadarCoverage]:
        target_date = date or _today()
        self.last_collection_errors = {}
        self.last_collection_diagnostics = []
        if mode == "live":
            config = await self.get_config()
            collector = self._get_lark_collector()
            records = await collector.collect(
                date=target_date,
                config=config.integrations.lark,
                sources=sources,
                chat_keywords=chat_keywords
                or config.integrations.lark.chat_keywords,
            )
            self.last_collection_errors = dict(
                getattr(collector, "last_errors", {}) or {},
            )
            self.last_collection_diagnostics = list(
                getattr(collector, "last_diagnostics", []) or [],
            )
        else:
            self.last_collection_diagnostics = [
                WorkbenchCollectionDiagnostic(
                    source="manual",
                    status="ok" if records else "empty",
                    records=len(records),
                    message="" if records else "No manual records provided",
                ),
            ]
        normalized = [
            record
            if isinstance(record, WorkbenchRawRecord)
            else WorkbenchRawRecord(**record)
            for record in records
        ]
        stamped = [
            record.model_copy(
                update={"created_at": record.created_at or _now_iso()},
            )
            for record in normalized
        ]
        await self.store.append_raw_records(target_date, stamped)
        all_records = await self.store.read_raw_records(target_date)
        return target_date, stamped, _coverage(all_records)

    def _get_lark_collector(self):
        if self.lark_collector is None:
            from .lark_collector import LarkCollector

            self.lark_collector = LarkCollector()
        return self.lark_collector

    async def get_radar(self, *, date: str | None = None) -> DailyRadar:
        target_date = date or _today()
        radar = await self.store.read_radar(target_date)
        if radar is not None:
            return radar
        records = await self.store.read_raw_records(target_date)
        return self._build_radar(target_date, records)

    async def analyze_today(self, *, date: str | None = None) -> DailyRadar:
        target_date = date or _today()
        records = await self.store.read_raw_records(target_date)
        radar = self._build_radar(target_date, records)
        radar = await self.store.write_radar(radar)
        await self.store.export_sot(radar)
        return radar

    async def update_insight_status(
        self,
        insight_id: str,
        status: InsightStatus,
        *,
        date: str | None = None,
    ) -> WorkbenchInsight:
        radar = await self.get_radar(date=date)
        updated: WorkbenchInsight | None = None
        for insight in self._iter_insights(radar):
            if insight.id == insight_id:
                insight.status = status
                updated = insight
        if updated is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="insight not found")
        await self.store.write_radar(radar)
        if status == "converted":
            await self.store.write_issue(
                insight_id,
                {"date": radar.date, "insight": _dump_model(updated)},
            )
            await self.store.export_sot(radar)
        return updated

    def _build_radar(
        self,
        date: str,
        records: list[WorkbenchRawRecord],
    ) -> DailyRadar:
        sections = DailyRadarSections()
        people_counter: Counter[str] = Counter()
        risk_records: list[WorkbenchRawRecord] = []
        question_records: list[WorkbenchRawRecord] = []

        for record in records:
            clean_people = _clean_people(record.people)
            for person in clean_people:
                if person.strip():
                    people_counter[person.strip()] += 1
            text = f"{record.title} {record.summary}"
            if _is_failed_ci_message(text) or _contains_any(
                text,
                ("blocked", "blocker", "overdue", "延期"),
            ):
                risk_records.append(record)
            if (
                not _is_success_ci_message(text)
                and _contains_any(text, ("confirm", "待确认", "需要确认", "owner"))
            ):
                question_records.append(record)
            if record.source_type == "lark_task":
                if not _is_stale_task(record, date) and not _is_broadcast_task(record):
                    sections.key_tasks.append(
                        self._record_insight(record, "task", due_at=record.due_at),
                    )
            elif record.source_type == "lark_calendar":
                if _is_today_meeting(record, date):
                    sections.key_meetings.append(
                        self._record_insight(
                            record,
                            "meeting",
                            due_at=record.starts_at,
                        ),
                    )

        for person, count in people_counter.most_common(8):
            sections.key_people.append(
                WorkbenchInsight(
                    id=_insight_id("person", "people", person, person),
                    kind="person",
                    title=person,
                    summary=f"Appears in {count} collected work signal(s).",
                    priority="P2",
                    confidence="medium" if count < 3 else "high",
                    related_people=[person],
                    created_at=_now_iso(),
                ),
            )

        for record in risk_records[:8]:
            sections.risks.append(
                self._record_insight(record, "risk", priority="P1"),
            )
        for record in question_records[:8]:
            sections.questions.append(
                self._record_insight(record, "question", priority="P2"),
            )

        highlights = (
            sections.key_tasks[:3]
            + sections.key_meetings[:2]
            + sections.risks[:3]
            + sections.questions[:2]
        )
        return DailyRadar(
            date=date,
            generated_at=_now_iso(),
            coverage=_coverage(records),
            highlights=highlights[:10],
            sections=sections,
        )

    def _record_insight(
        self,
        record: WorkbenchRawRecord,
        kind: str,
        *,
        priority: str = "P2",
        due_at: str | None = None,
    ) -> WorkbenchInsight:
        return WorkbenchInsight(
            id=_insight_id(
                kind,
                record.source_type,
                record.source_id,
                record.title,
            ),
            kind=kind,  # type: ignore[arg-type]
            title=_sanitize_visible_text(record.title) or record.source_id or kind,
            summary=_sanitize_visible_text(record.summary),
            priority=priority,  # type: ignore[arg-type]
            confidence="medium",
            sources=[_source_ref(record)],
            related_people=_clean_people(record.people),
            related_projects=record.projects,
            due_at=due_at,
            created_at=record.created_at or _now_iso(),
        )

    def _iter_insights(self, radar: DailyRadar):
        yield from radar.sections.key_people
        yield from radar.sections.key_tasks
        yield from radar.sections.key_meetings
        yield from radar.sections.risks
        yield from radar.sections.questions


_DEFAULT_SERVICE: WorkbenchService | None = None


def get_workbench_service() -> WorkbenchService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = WorkbenchService(WorkbenchStore(WORKING_DIR))
    return _DEFAULT_SERVICE
