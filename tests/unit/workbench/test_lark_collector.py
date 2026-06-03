# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

import pytest

from qwenpaw.workbench.lark_collector import (
    LarkAuthEnvCollector,
    LarkCollector,
    LarkCommandError,
    LarkCommandRunner,
    _load_lark_auth_token,
    _message_should_collect,
)
from qwenpaw.workbench.models import (
    WorkbenchCollectionIssue,
    WorkbenchLarkIntegrationConfig,
    WorkbenchRawRecord,
)


class FakeRunner:
    def __init__(self):
        self.argvs: list[list[str]] = []

    async def run_json(self, argv: list[str]):
        self.argvs.append(argv)
        command = " ".join(argv)
        if "config show" in command:
            return {"ok": True}
        if "calendar +agenda" in command:
            return {
                "items": [
                    {
                        "event_id": "event-1",
                        "summary": "Daily radar design review",
                        "description": "Review launch risks and owners",
                        "start_time": {"timestamp": "1780383600000"},
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
                        "body": {
                            "content": (
                                "{\"text\":\"Need confirm owner for overdue task\"}"
                            ),
                        },
                        "create_time": "1780372800000",
                        "url": "https://example.com/msg-1",
                    },
                    {
                        "message_id": "msg-1",
                        "chat_name": "Workbench Group",
                        "sender": {"name": "Bob"},
                        "body": {
                            "content": (
                                "{\"text\":\"Need confirm owner for overdue task\"}"
                            ),
                        },
                        "create_time": "1780372800000",
                    },
                ],
            }
        raise AssertionError(f"unexpected argv: {argv}")


class FailingRunner:
    def __init__(self):
        self.argvs: list[list[str]] = []

    async def run_json(self, argv: list[str]):
        self.argvs.append(argv)
        raise LarkCommandError(
            "hermes context detected but lark-cli is not bound to it",
            code="hermes",
            recovery_actions=[
                "在终端运行 `lark-cli config bind --help` 查看绑定方式。",
                "绑定完成后回到收件箱，点击“重新采集”。",
            ],
        )


class NoopAuthEnvCollector:
    async def collect(self, **kwargs):
        return [], {}


class FakeAuthEnvCollector:
    def __init__(self):
        self.calls: list[dict] = []

    async def collect(self, **kwargs):
        self.calls.append(kwargs)
        return [
            WorkbenchRawRecord(
                source_type="lark_calendar",
                source_id="auth-event-1",
                title="Fallback event",
                summary="Collected through lark-auth-check",
            ),
        ], {
            "chat": WorkbenchCollectionIssue(
                source="chat",
                message="消息搜索权限不足",
                code="99991663",
                recovery_actions=["重新运行 lark-auth-check 补齐缺失权限。"],
            ),
        }


class PartiallyFailingChatRunner:
    def __init__(self):
        self.argvs: list[list[str]] = []

    async def run_json(self, argv: list[str]):
        self.argvs.append(argv)
        command = " ".join(argv)
        if "config show" in command:
            return {"ok": True}
        if "im +messages-search" not in command:
            raise AssertionError(f"unexpected argv: {argv}")
        query = argv[argv.index("--query") + 1]
        if query == "blocked":
            raise LarkCommandError(
                "temporary search backend unavailable",
                code="temporary_unavailable",
                recovery_actions=["稍后点击“重新采集”。"],
            )
        return {
            "items": [
                {
                    "message_id": "msg-owner-1",
                    "chat_name": "Workbench Group",
                    "sender": {"name": "Bob"},
                    "text": "Need confirm owner for overdue task",
                    "create_time": "1780372800000",
                },
            ],
        }


class SourceAuthFailingRunner:
    def __init__(self):
        self.argvs: list[list[str]] = []

    async def run_json(self, argv: list[str]):
        self.argvs.append(argv)
        command = " ".join(argv)
        if "config show" in command:
            return {"ok": True}
        raise LarkCommandError(
            "need_user_authorization",
            code="authentication",
            recovery_actions=["补齐权限后重试。"],
        )


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
    assert records[0].starts_at is not None
    assert records[0].starts_at.startswith("2026-06-02")
    assert records[0].people == ["Alice", "Bob"]
    assert records[1].source_id == "task-1"
    assert records[1].title == "Prepare launch checklist"
    assert records[1].summary == "Blocked by API review"
    assert records[2].source_id == "msg-1"
    assert records[2].title == "Workbench Group"
    assert records[2].summary == "Need confirm owner for overdue task"
    assert records[2].people == ["Bob"]

    assert runner.argvs[0] == ["lark-cli", "config", "show"]
    assert runner.argvs[1][:4] == [
        "lark-cli",
        "calendar",
        "+agenda",
        "--as",
    ]
    assert runner.argvs[1][4] == "user"
    assert runner.argvs[2][:5] == [
        "lark-cli",
        "task",
        "+get-my-tasks",
        "--as",
        "user",
    ]
    assert "--complete=false" in runner.argvs[2]
    assert runner.argvs[3][:5] == [
        "lark-cli",
        "im",
        "+messages-search",
        "--as",
        "user",
    ]
    assert "--query" in runner.argvs[3]
    assert "owner" in runner.argvs[3]


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
    assert len(runner.argvs) == 2
    assert runner.argvs[0] == ["lark-cli", "config", "show"]
    assert runner.argvs[1][1:3] == ["task", "+get-my-tasks"]


@pytest.mark.asyncio
async def test_lark_collector_exposes_actionable_command_errors():
    runner = FailingRunner()
    collector = LarkCollector(runner=runner, auth_env_collector=NoopAuthEnvCollector())

    records = await collector.collect(
        date="2026-06-02",
        config=WorkbenchLarkIntegrationConfig(
            collect_calendar=True,
            collect_tasks=False,
            collect_chat=False,
        ),
    )

    assert records == []
    assert collector.last_errors == {
        "calendar": "hermes context detected but lark-cli is not bound to it",
    }
    assert runner.argvs == [["lark-cli", "config", "show"]]
    issue = collector.last_error_details["calendar"]
    assert issue.code == "hermes"
    assert "config bind" in issue.recovery_actions[0]


@pytest.mark.asyncio
async def test_lark_collector_preflight_fans_out_global_errors():
    runner = FailingRunner()
    collector = LarkCollector(runner=runner, auth_env_collector=NoopAuthEnvCollector())

    records = await collector.collect(
        date="2026-06-02",
        config=WorkbenchLarkIntegrationConfig(),
        sources=["calendar", "tasks", "chat"],
    )

    assert records == []
    assert runner.argvs == [["lark-cli", "config", "show"]]
    assert set(collector.last_errors) == {"calendar", "tasks", "chat"}
    assert collector.last_error_details["chat"].code == "hermes"


@pytest.mark.asyncio
async def test_lark_collector_uses_auth_env_fallback_when_preflight_fails():
    runner = FailingRunner()
    auth_env_collector = FakeAuthEnvCollector()
    collector = LarkCollector(
        runner=runner,
        auth_env_collector=auth_env_collector,
    )

    records = await collector.collect(
        date="2026-06-02",
        config=WorkbenchLarkIntegrationConfig(),
        sources=["calendar", "chat"],
        chat_keywords=["owner"],
    )

    assert [record.source_id for record in records] == ["auth-event-1"]
    assert runner.argvs == [["lark-cli", "config", "show"]]
    assert auth_env_collector.calls[0]["sources"] == ["calendar", "chat"]
    assert auth_env_collector.calls[0]["chat_keywords"] == ["owner"]
    assert collector.last_errors == {"chat": "消息搜索权限不足"}
    assert collector.last_error_details["chat"].recovery_actions == [
        "重新运行 lark-auth-check 补齐缺失权限。",
    ]


@pytest.mark.asyncio
async def test_lark_collector_uses_auth_env_fallback_for_source_auth_errors():
    runner = SourceAuthFailingRunner()
    auth_env_collector = FakeAuthEnvCollector()
    collector = LarkCollector(
        runner=runner,
        auth_env_collector=auth_env_collector,
    )

    records = await collector.collect(
        date="2026-06-02",
        config=WorkbenchLarkIntegrationConfig(
            collect_calendar=True,
            collect_tasks=False,
            collect_chat=False,
        ),
        sources=["calendar"],
    )

    assert [record.source_id for record in records] == ["auth-event-1"]
    assert auth_env_collector.calls[0]["sources"] == ["calendar"]
    assert collector.last_errors == {}
    assert collector.last_error_details == {}


def test_load_lark_auth_token_prefers_refreshed_env_file(tmp_path, monkeypatch):
    stale_file = tmp_path / ".feishu_auth_env.ps1"
    fresh_file = tmp_path / ".feishu_auth_env"
    stale_file.write_text(
        '$env:LARKSUITE_CLI_USER_ACCESS_TOKEN = "file-stale-token"\n',
        encoding="utf-8",
    )
    fresh_file.write_text(
        "export LARKSUITE_CLI_USER_ACCESS_TOKEN=fresh-token\n",
        encoding="utf-8",
    )
    os.utime(stale_file, (100, 100))
    os.utime(fresh_file, (200, 200))
    monkeypatch.setenv("LARKSUITE_CLI_USER_ACCESS_TOKEN", "stale-token")

    assert _load_lark_auth_token([stale_file, fresh_file]) == "fresh-token"


def test_message_should_collect_filters_all_mentions_and_old_messages():
    start = "2026-06-02T00:00:00+08:00"
    end = "2026-06-03T00:00:00+08:00"

    assert not _message_should_collect(
        {
            "message_id": "msg-all",
            "body": {"content": "{\"text\":\"@_all blocked by deployment\"}"},
            "create_time": "2026-06-02T10:00:00+08:00",
        },
        start,
        end,
    )
    assert not _message_should_collect(
        {
            "message_id": "msg-old",
            "text": "Need confirm owner",
            "create_time": "2026-05-01T10:00:00+08:00",
        },
        start,
        end,
    )
    assert _message_should_collect(
        {
            "message_id": "msg-today",
            "text": "Need confirm owner",
            "create_time": "2026-06-02T10:00:00+08:00",
        },
        start,
        end,
    )


@pytest.mark.asyncio
async def test_auth_env_collector_resolves_lark_ids_to_names(monkeypatch):
    collector = LarkAuthEnvCollector()
    requests: list[str] = []

    async def fake_request(client, token, method, path, **kwargs):
        requests.append(path)
        if path == "/im/v1/chats/oc_123":
            return {"name": "Launch Group"}
        return {
            "user": {
                "open_id": "ou_123",
                "name": "Alice Zhang",
            },
        }

    monkeypatch.setattr(collector, "_request", fake_request)

    records = await collector._resolve_record_labels(
        None,
        "token",
        [
            WorkbenchRawRecord(
                source_type="lark_message",
                source_id="msg-1",
                title="oc_123",
                people=["ou_123", "cli_456", "Bob"],
            ),
        ],
    )

    assert records[0].title == "Launch Group"
    assert records[0].people == ["Alice Zhang", "Bob"]
    assert requests == ["/contact/v3/users/ou_123", "/im/v1/chats/oc_123"]


@pytest.mark.asyncio
async def test_lark_collector_keeps_chat_records_when_one_keyword_fails():
    collector = LarkCollector(runner=PartiallyFailingChatRunner())

    records = await collector.collect(
        date="2026-06-02",
        config=WorkbenchLarkIntegrationConfig(
            collect_calendar=False,
            collect_tasks=False,
            collect_chat=True,
        ),
        sources=["chat"],
        chat_keywords=["blocked", "owner"],
    )

    assert [record.source_id for record in records] == ["msg-owner-1"]
    assert "chat" in collector.last_errors
    assert "blocked: temporary search backend unavailable" in (
        collector.last_error_details["chat"].message
    )


@pytest.mark.asyncio
async def test_lark_command_runner_times_out_hung_process():
    runner = LarkCommandRunner(timeout_seconds=0.01)

    with pytest.raises(LarkCommandError) as exc_info:
        await runner.run_json([sys.executable, "-c", "import time; time.sleep(1)"])

    assert exc_info.value.code == "timeout"
    assert "timed out" in exc_info.value.message
