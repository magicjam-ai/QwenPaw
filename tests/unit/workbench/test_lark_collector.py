# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from qwenpaw.workbench.lark_collector import LarkCollector
from qwenpaw.workbench.models import WorkbenchLarkIntegrationConfig


class FakeRunner:
    def __init__(self):
        self.argvs: list[list[str]] = []

    async def run_json(self, argv: list[str]):
        self.argvs.append(argv)
        command = " ".join(argv)
        if "calendar +agenda" in command:
            return {
                "items": [
                    {
                        "event_id": "event-1",
                        "summary": "Daily radar design review",
                        "description": "Review launch risks and owners",
                        "start_time": "2026-06-02T15:00:00+08:00",
                        "attendees": [
                            {"display_name": "Alice"},
                            {"name": "Bob"},
                        ],
                        "url": "https://example.com/event-1",
                    },
                ],
            }
        if "task +get-my-tasks" in command:
            return {
                "tasks": [
                    {
                        "guid": "task-1",
                        "summary": "Prepare launch checklist",
                        "description": "Blocked by API review",
                        "due": {"timestamp": "1780394400000"},
                        "members": [{"name": "Alice"}],
                        "url": "https://example.com/task-1",
                    },
                ],
            }
        if "im +messages-search" in command:
            return {
                "items": [
                    {
                        "message_id": "msg-1",
                        "chat_name": "Workbench Group",
                        "sender": {"name": "Bob"},
                        "text": "Need confirm owner for overdue task",
                        "create_time": "1780372800000",
                        "url": "https://example.com/msg-1",
                    },
                    {
                        "message_id": "msg-1",
                        "chat_name": "Workbench Group",
                        "sender": {"name": "Bob"},
                        "text": "Need confirm owner for overdue task",
                        "create_time": "1780372800000",
                    },
                ],
            }
        raise AssertionError(f"unexpected argv: {argv}")


@pytest.mark.asyncio
async def test_lark_collector_runs_read_only_commands_and_maps_records():
    runner = FakeRunner()
    collector = LarkCollector(runner=runner)

    records = await collector.collect(
        date="2026-06-02",
        config=WorkbenchLarkIntegrationConfig(),
        sources=["calendar", "tasks", "chat"],
        chat_keywords=["owner"],
    )

    assert [record.source_type for record in records] == [
        "lark_calendar",
        "lark_task",
        "lark_message",
    ]
    assert records[0].source_id == "event-1"
    assert records[0].title == "Daily radar design review"
    assert records[0].starts_at == "2026-06-02T15:00:00+08:00"
    assert records[0].people == ["Alice", "Bob"]
    assert records[1].source_id == "task-1"
    assert records[1].title == "Prepare launch checklist"
    assert records[1].summary == "Blocked by API review"
    assert records[2].source_id == "msg-1"
    assert records[2].title == "Workbench Group"
    assert records[2].summary == "Need confirm owner for overdue task"
    assert records[2].people == ["Bob"]

    assert runner.argvs[0][:4] == [
        "lark-cli",
        "calendar",
        "+agenda",
        "--as",
    ]
    assert runner.argvs[0][4] == "user"
    assert runner.argvs[1][:5] == [
        "lark-cli",
        "task",
        "+get-my-tasks",
        "--as",
        "user",
    ]
    assert "--complete=false" in runner.argvs[1]
    assert runner.argvs[2][:5] == [
        "lark-cli",
        "im",
        "+messages-search",
        "--as",
        "user",
    ]
    assert "--query" in runner.argvs[2]
    assert "owner" in runner.argvs[2]


@pytest.mark.asyncio
async def test_lark_collector_respects_disabled_sources():
    runner = FakeRunner()
    collector = LarkCollector(runner=runner)
    config = WorkbenchLarkIntegrationConfig(
        collect_calendar=False,
        collect_tasks=True,
        collect_chat=False,
    )

    records = await collector.collect(
        date="2026-06-02",
        config=config,
        sources=None,
        chat_keywords=None,
    )

    assert [record.source_type for record in records] == ["lark_task"]
    assert len(runner.argvs) == 1
    assert runner.argvs[0][1:3] == ["task", "+get-my-tasks"]
