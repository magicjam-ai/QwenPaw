# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime
from typing import Iterable, Sequence

from ..constant import WORKING_DIR
from .models import (
    DailyRadar,
    DailyRadarCoverage,
    DailyRadarSections,
    WorkbenchCollectMode,
    WorkbenchCollectSource,
    InsightStatus,
    WorkbenchConfig,
    WorkbenchInsight,
    WorkbenchRawRecord,
    WorkbenchSourceRef,
    WorkbenchWorkspaceConfig,
)
from .store import WorkbenchStore, _dump_model


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _insight_id(
    kind: str,
    source_type: str,
    source_id: str,
    title: str,
) -> str:
    key = f"{kind}:{source_type}:{source_id}:{title}".encode("utf-8")
    digest = hashlib.sha1(key).hexdigest()[:12]
    return f"{kind}-{digest}"


def _source_ref(record: WorkbenchRawRecord) -> WorkbenchSourceRef:
    return WorkbenchSourceRef(
        source_type=record.source_type,
        source_id=record.source_id,
        title=record.title,
        url=record.metadata.get("url")
        if isinstance(record.metadata.get("url"), str)
        else None,
        excerpt=record.summary,
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


class WorkbenchService:
    def __init__(self, store: WorkbenchStore, lark_collector=None):
        self.store = store
        self.lark_collector = lark_collector
        self.last_collection_errors: dict[str, str] = {}

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

    async def collect(
        self,
        *,
        records: Sequence[dict | WorkbenchRawRecord],
        date: str | None = None,
        mode: WorkbenchCollectMode = "manual",
        sources: list[WorkbenchCollectSource] | None = None,
        chat_keywords: list[str] | None = None,
    ) -> tuple[str, list[WorkbenchRawRecord], DailyRadarCoverage]:
        target_date = date or _today()
        self.last_collection_errors = {}
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
        return await self.store.write_radar(radar)

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
            for person in record.people:
                if person.strip():
                    people_counter[person.strip()] += 1
            text = f"{record.title} {record.summary}"
            if _contains_any(text, ("blocked", "blocker", "overdue", "延期")):
                risk_records.append(record)
            if _contains_any(text, ("confirm", "待确认", "需要确认", "owner")):
                question_records.append(record)
            if record.source_type == "lark_task":
                sections.key_tasks.append(
                    self._record_insight(record, "task", due_at=record.due_at),
                )
            elif record.source_type == "lark_calendar":
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
            title=record.title or record.source_id or kind,
            summary=record.summary,
            priority=priority,  # type: ignore[arg-type]
            confidence="medium",
            sources=[_source_ref(record)],
            related_people=record.people,
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
