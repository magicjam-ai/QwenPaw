# -*- coding: utf-8 -*-
# pylint: disable=protected-access
from __future__ import annotations

import json
import os
import sys

import pytest

from qwenpaw.workbench.lark_collector import (
    LarkAuthEnvCollector,
    LarkCollector,
    LarkCommandError,
    LarkCommandRunner,
    _auth_check_python_argv,
    _load_lark_auth_token,
    _message_should_collect,
    _parse_lark_auth_check_output,
    _parse_lark_auth_env_file,
)
from qwenpaw.workbench.models import (
    WorkbenchCollectionIssue,
    WorkbenchLarkIntegrationConfig,
    WorkbenchRawRecord,
)

OWNER_CONFIRM_TEXT = "Need confirm owner for overdue task"


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
                            "content": json.dumps(
                                {"text": OWNER_CONFIRM_TEXT},
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
                            "content": json.dumps(
                                {"text": OWNER_CONFIRM_TEXT},
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
    async def collect(self, **_kwargs):
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


def test_parse_lark_auth_check_output():
    result = _parse_lark_auth_check_output(
        "\n".join(
            [
                "AUTH_STATUS: SUCCESS",
                "APP_ID: cli_test",
                r"ENV_FILE: C:\Users\me\.magic_skills\.feishu_auth_env",
            ],
        ),
    )

    assert result == {
        "AUTH_STATUS": "SUCCESS",
        "APP_ID": "cli_test",
        "ENV_FILE": r"C:\Users\me\.magic_skills\.feishu_auth_env",
    }


def test_parse_lark_auth_env_file_supports_bash_and_powershell(tmp_path):
    env_file = tmp_path / ".feishu_auth_env"
    env_file.write_text(
        "\n".join(
            [
                "export LARKSUITE_CLI_USER_ACCESS_TOKEN=token-from-bash",
                "export LARKSUITE_CLI_APP_ID=cli_bash",
                '$env:LARKSUITE_CLI_TENANT_ACCESS_TOKEN = "token-from-ps"',
            ],
        ),
        encoding="utf-8",
    )

    assert _parse_lark_auth_env_file(env_file) == {
        "LARKSUITE_CLI_USER_ACCESS_TOKEN": "token-from-bash",
        "LARKSUITE_CLI_APP_ID": "cli_bash",
        "LARKSUITE_CLI_TENANT_ACCESS_TOKEN": "token-from-ps",
    }


def test_message_should_collect_accepts_naive_message_time():
    assert _message_should_collect(
        {"message_id": "msg-1", "text": "blocked", "create_time": "2026-06-05 10:30:00"},
        "2026-06-05T00:00:00+08:00",
        "2026-06-06T00:00:00+08:00",
    )


def test_message_should_collect_accepts_utc_z_message_time():
    assert _message_should_collect(
        {"message_id": "msg-1", "text": "blocked", "create_time": "2026-06-05T02:30:00Z"},
        "2026-06-05T00:00:00+08:00",
        "2026-06-06T00:00:00+08:00",
    )


@pytest.mark.asyncio
async def test_lark_command_runner_loads_lark_auth_env(monkeypatch, tmp_path):
    auth_dir = tmp_path / "lark-auth-check"
    script = auth_dir / "scripts" / "check_auth.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test fixture\n", encoding="utf-8")
    env_file = tmp_path / ".feishu_auth_env"
    env_file.write_text(
        "\n".join(
            [
                "export LARKSUITE_CLI_USER_ACCESS_TOKEN=token",
                "export LARKSUITE_CLI_APP_ID=cli_test",
            ],
        ),
        encoding="utf-8",
    )

    calls: list[dict] = []

    class FakeProcess:
        def __init__(self, returncode: int, stdout: str, stderr: str = ""):
            self.returncode = returncode
            self._stdout = stdout.encode("utf-8")
            self._stderr = stderr.encode("utf-8")

        async def communicate(self):
            return self._stdout, self._stderr

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        if str(script) in args:
            return FakeProcess(
                0,
                f"AUTH_STATUS: SUCCESS\nENV_FILE: {env_file}\n",
            )
        return FakeProcess(0, '{"ok": true}')

    monkeypatch.setattr(
        "qwenpaw.workbench.lark_collector.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        "qwenpaw.workbench.lark_collector.shutil.which",
        lambda executable: executable,
    )
    runner = LarkCommandRunner(auth_check_dir=auth_dir)

    assert await runner.run_json(["lark-cli", "calendar", "+agenda"]) == {
        "ok": True,
    }

    assert calls[0]["kwargs"]["cwd"] == str(auth_dir)
    lark_call_env = calls[1]["kwargs"]["env"]
    assert lark_call_env["LARKSUITE_CLI_USER_ACCESS_TOKEN"] == "token"
    assert lark_call_env["LARKSUITE_CLI_APP_ID"] == "cli_test"


def test_auth_check_python_argv_uses_override(monkeypatch):
    monkeypatch.setenv("LARK_AUTH_CHECK_PYTHON", "/opt/python/bin/python")

    assert _auth_check_python_argv() == ["/opt/python/bin/python"]


def test_auth_check_python_argv_frozen_finds_external_python(monkeypatch):
    calls: list[list[str]] = []

    class FakeCompleted:
        returncode = 0

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return FakeCompleted()

    monkeypatch.delenv("LARK_AUTH_CHECK_PYTHON", raising=False)
    monkeypatch.delenv("QWENPAW_EXTERNAL_PYTHON", raising=False)
    monkeypatch.setattr("qwenpaw.workbench.lark_collector.sys.frozen", True, raising=False)
    monkeypatch.setattr("qwenpaw.workbench.lark_collector.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("qwenpaw.workbench.lark_collector.subprocess.run", fake_run)

    argv = _auth_check_python_argv()

    assert argv[0] in {"/usr/bin/py", "/usr/bin/python"}
    if argv[0].endswith("/py"):
        assert argv[1] == "-3"
    assert calls[0][-1] == "--version"


@pytest.mark.asyncio
async def test_lark_collector_exposes_actionable_command_errors():
    runner = FailingRunner()
    collector = LarkCollector(
        runner=runner,
        auth_env_collector=NoopAuthEnvCollector(),
    )

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
    collector = LarkCollector(
        runner=runner,
        auth_env_collector=NoopAuthEnvCollector(),
    )

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
    assert not collector.last_errors
    assert not collector.last_error_details


def test_load_lark_auth_token_prefers_refreshed_env_file(
    tmp_path,
    monkeypatch,
):
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
            "body": {"content": '{"text":"@_all blocked by deployment"}'},
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

    async def fake_request(_client, _token, _method, path, **_kwargs):
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
        await runner.run_json(
            [sys.executable, "-c", "import time; time.sleep(1)"],
        )

    assert exc_info.value.code == "timeout"
    assert "timed out" in exc_info.value.message
