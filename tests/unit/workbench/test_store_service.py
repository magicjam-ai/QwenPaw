# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from qwenpaw.workbench.models import WorkbenchRawRecord
from qwenpaw.workbench.service import WorkbenchService
from qwenpaw.workbench.store import WorkbenchStore


class FakeCollector:
    def __init__(self):
        self.calls = []

    async def collect(
        self,
        *,
        date: str,
        config,
        sources=None,
        chat_keywords=None,
    ):
        self.calls.append(
            {
                "date": date,
                "config": config,
                "sources": sources,
                "chat_keywords": chat_keywords,
            },
        )
        return [
            {
                "source_type": "lark_calendar",
                "source_id": "live-event-1",
                "title": "Live calendar event",
                "summary": "Live agenda item",
                "starts_at": "2026-06-02T10:00:00+08:00",
                "people": ["Alice"],
            },
        ]


@pytest.mark.asyncio
async def test_default_config_uses_backend_owned_local_data_root(tmp_path):
    store = WorkbenchStore(tmp_path)
    service = WorkbenchService(store)

    config = await service.get_config()

    assert str(config.workspace.data_root).endswith(".workbench")
    assert config.workspace.storage_mode == "local"
    assert config.integrations.lark.enabled is True
    assert config.integrations.lark.collect_calendar is True
    assert config.integrations.lark.collect_tasks is True


@pytest.mark.asyncio
async def test_init_workbench_creates_required_directories(tmp_path):
    store = WorkbenchStore(tmp_path)
    service = WorkbenchService(store)

    config = await service.init_workbench()

    data_root = config.workspace.data_root
    assert data_root == tmp_path / ".workbench"
    for rel_path in (
        "cache/raw",
        "summaries",
        "people",
        "meetings",
        "comms",
        "issues",
    ):
        assert (data_root / rel_path).is_dir()


@pytest.mark.asyncio
async def test_analyze_today_builds_radar_from_lark_shaped_raw_records(
    tmp_path,
):
    store = WorkbenchStore(tmp_path)
    service = WorkbenchService(store)
    await service.init_workbench()
    await service.collect(
        records=[
            {
                "source_type": "lark_task",
                "source_id": "task-1",
                "title": "Prepare launch checklist",
                "summary": "Blocked by API review",
                "due_at": "2026-06-02T18:00:00+08:00",
                "people": ["Alice"],
                "projects": ["Workbench"],
            },
            {
                "source_type": "lark_calendar",
                "source_id": "meeting-1",
                "title": "Daily radar design review",
                "summary": "Review key risks and owners",
                "starts_at": "2026-06-02T15:00:00+08:00",
                "people": ["Alice", "Bob"],
                "projects": ["Workbench"],
            },
            {
                "source_type": "lark_message",
                "source_id": "msg-1",
                "title": "Bob mentioned a blocker",
                "summary": "Need confirm owner for overdue task",
                "people": ["Bob"],
                "projects": ["Workbench"],
            },
        ],
        date="2026-06-02",
    )

    radar = await service.analyze_today(date="2026-06-02")

    assert radar.date == "2026-06-02"
    assert radar.coverage.tasks == 1
    assert radar.coverage.calendar_events == 1
    assert radar.coverage.chat_messages == 1
    assert radar.sections.key_tasks[0].kind == "task"
    assert radar.sections.key_meetings[0].kind == "meeting"
    assert radar.sections.key_people[0].kind == "person"
    assert radar.sections.risks
    assert radar.sections.questions


@pytest.mark.asyncio
async def test_update_insight_status_persists_actions(tmp_path):
    store = WorkbenchStore(tmp_path)
    service = WorkbenchService(store)
    await service.init_workbench()
    await service.collect(
        records=[
            {
                "source_type": "lark_task",
                "source_id": "task-1",
                "title": "Prepare launch checklist",
                "summary": "Confirm release owner",
            },
        ],
        date="2026-06-02",
    )
    radar = await service.analyze_today(date="2026-06-02")
    insight_id = radar.sections.key_tasks[0].id

    confirmed = await service.update_insight_status(
        insight_id,
        "confirmed",
        date="2026-06-02",
    )
    ignored = await service.update_insight_status(
        insight_id,
        "ignored",
        date="2026-06-02",
    )
    converted = await service.update_insight_status(
        insight_id,
        "converted",
        date="2026-06-02",
    )
    persisted = await service.get_radar(date="2026-06-02")

    assert confirmed.status == "confirmed"
    assert ignored.status == "ignored"
    assert converted.status == "converted"
    assert persisted.sections.key_tasks[0].status == "converted"
    assert (tmp_path / ".workbench" / "issues" / f"{insight_id}.json").is_file()


@pytest.mark.asyncio
async def test_collect_live_uses_lark_collector_and_persists_records(tmp_path):
    store = WorkbenchStore(tmp_path)
    collector = FakeCollector()
    service = WorkbenchService(store, lark_collector=collector)
    await service.init_workbench()

    date, records, coverage = await service.collect(
        mode="live",
        date="2026-06-02",
        sources=["calendar"],
        chat_keywords=["owner"],
        records=[],
    )
    persisted = await store.read_raw_records("2026-06-02")

    assert date == "2026-06-02"
    assert collector.calls[0]["date"] == "2026-06-02"
    assert collector.calls[0]["sources"] == ["calendar"]
    assert collector.calls[0]["chat_keywords"] == ["owner"]
    assert records[0].source_id == "live-event-1"
    assert persisted[0].source_id == "live-event-1"
    assert coverage.calendar_events == 1


@pytest.mark.asyncio
async def test_collect_live_is_idempotent_for_same_source_records(tmp_path):
    store = WorkbenchStore(tmp_path)
    collector = FakeCollector()
    service = WorkbenchService(store, lark_collector=collector)
    await service.init_workbench()

    await service.collect(
        mode="live",
        date="2026-06-02",
        records=[],
    )
    _, _, coverage = await service.collect(
        mode="live",
        date="2026-06-02",
        records=[],
    )
    persisted = await store.read_raw_records("2026-06-02")

    assert len(persisted) == 1
    assert persisted[0].source_id == "live-event-1"
    assert coverage.calendar_events == 1


@pytest.mark.asyncio
async def test_append_raw_records_compacts_existing_duplicate_source_records(
    tmp_path,
):
    store = WorkbenchStore(tmp_path)
    store.ensure_directories()
    raw_path = store.raw_path("2026-06-02")
    raw_path.write_text(
        """
[
  {"source_type":"lark_task","source_id":"task-1","title":"old"},
  {"source_type":"lark_task","source_id":"task-1","title":"older"}
]
""",
        encoding="utf-8",
    )

    await store.append_raw_records(
        "2026-06-02",
        [
            WorkbenchRawRecord(
                source_type="lark_task",
                source_id="task-1",
                title="fresh",
            ),
        ],
    )
    persisted = await store.read_raw_records("2026-06-02")

    assert len(persisted) == 1
    assert persisted[0].title == "fresh"
