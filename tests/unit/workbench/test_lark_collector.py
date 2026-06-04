# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from qwenpaw.workbench.lark_collector import (
    LarkCommandRunner,
    LarkCollector,
    _parse_lark_auth_check_output,
    _parse_lark_auth_env_file,
)
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
