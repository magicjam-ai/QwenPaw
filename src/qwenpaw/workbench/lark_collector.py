# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time as time_module
from collections.abc import Iterable
from datetime import date as date_type
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from .models import (
    WorkbenchCollectionDiagnostic,
    WorkbenchLarkIntegrationConfig,
    WorkbenchRawRecord,
)

DEFAULT_CHAT_KEYWORDS = ("blocked", "blocker", "overdue", "延期", "待确认", "owner")


class JsonCommandRunner(Protocol):
    async def run_json(self, argv: list[str]) -> Any:
        """Run a command and return parsed JSON output."""


class LarkCommandError(RuntimeError):
    pass


class LarkCommandRunner:
    def __init__(
        self,
        *,
        auth_check_dir: str | Path | None = None,
        auth_cache_ttl_seconds: int = 300,
    ):
        self.auth_check_dir = Path(auth_check_dir) if auth_check_dir else None
        self.auth_cache_ttl_seconds = auth_cache_ttl_seconds
        self._auth_env_cache: dict[str, str] | None = None
        self._auth_checked_at = 0.0

    async def run_json(self, argv: list[str]) -> Any:
        env = await self._authenticated_env()
        executable = shutil.which(argv[0]) or argv[0]
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise LarkCommandError(
                f"{argv[:3]} exited {process.returncode}: {stderr_text.strip()}",
            )
        return _parse_json_output(stdout_text)

    async def _authenticated_env(self) -> dict[str, str]:
        auth_env = await self._load_lark_auth_env()
        env = os.environ.copy()
        env.update(auth_env)
        return env

    async def _load_lark_auth_env(self) -> dict[str, str]:
        now = time_module.monotonic()
        if (
            self._auth_env_cache is not None
            and now - self._auth_checked_at < self.auth_cache_ttl_seconds
        ):
            return dict(self._auth_env_cache)

        auth_dir = self.auth_check_dir or _default_lark_auth_check_dir()
        script = auth_dir / "scripts" / "check_auth.py"
        if not script.exists():
            raise LarkCommandError(
                f"lark-auth-check skill not found at {script}",
            )

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(auth_dir),
        )
        stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise LarkCommandError(
                "lark-auth-check failed: "
                f"{_sanitize_auth_text(stderr_text or stdout_text)}",
            )

        auth_result = _parse_lark_auth_check_output(stdout_text)
        status = auth_result.get("AUTH_STATUS")
        if status == "SUCCESS":
            env_file = auth_result.get("ENV_FILE", "")
            if not env_file:
                raise LarkCommandError("lark-auth-check did not return ENV_FILE")
            auth_env = _parse_lark_auth_env_file(Path(env_file))
            if not auth_env:
                raise LarkCommandError("lark-auth-check ENV_FILE is empty")
            self._auth_env_cache = dict(auth_env)
            self._auth_checked_at = now
            return auth_env

        if status == "NEED_AUTH":
            auth_link = auth_result.get("AUTH_LINK", "")
            raise LarkCommandError(
                "lark-auth-check requires user authorization"
                + (f": {auth_link}" if auth_link else ""),
            )

        message = auth_result.get("ERROR_MESSAGE") or stdout_text or stderr_text
        raise LarkCommandError(
            f"lark-auth-check returned {status or 'unknown'}: "
            f"{_sanitize_auth_text(message)}",
        )


class LarkCollector:
    def __init__(self, runner: JsonCommandRunner | None = None):
        self.runner = runner or LarkCommandRunner()
        self.last_errors: dict[str, str] = {}
        self.last_diagnostics: list[WorkbenchCollectionDiagnostic] = []

    async def collect(
        self,
        *,
        date: str,
        config: WorkbenchLarkIntegrationConfig,
        sources: list[str] | None = None,
        chat_keywords: list[str] | None = None,
    ) -> list[WorkbenchRawRecord]:
        self.last_errors = {}
        self.last_diagnostics = []
        if not config.enabled:
            for source in ("calendar", "tasks", "chat"):
                self.last_diagnostics.append(
                    WorkbenchCollectionDiagnostic(
                        source=source,
                        status="skipped",
                        message="Lark integration disabled",
                    ),
                )
            return []

        enabled_sources = set(sources or ("calendar", "tasks", "chat"))
        records: list[WorkbenchRawRecord] = []
        if config.collect_calendar and "calendar" in enabled_sources:
            records.extend(await self._safe_collect("calendar", self._collect_calendar(date)))
        else:
            self._mark_skipped("calendar")
        if config.collect_tasks and "tasks" in enabled_sources:
            records.extend(await self._safe_collect("tasks", self._collect_tasks(date)))
        else:
            self._mark_skipped("tasks")
        if config.collect_chat and "chat" in enabled_sources:
            records.extend(
                await self._safe_collect(
                    "chat",
                    self._collect_chat(date, chat_keywords or list(DEFAULT_CHAT_KEYWORDS)),
                ),
            )
        else:
            self._mark_skipped("chat")
        return _dedupe_records(records)

    def _mark_skipped(self, source: str) -> None:
        self.last_diagnostics.append(
            WorkbenchCollectionDiagnostic(source=source, status="skipped"),
        )

    async def _safe_collect(
        self,
        source: str,
        awaitable,
    ) -> list[WorkbenchRawRecord]:
        try:
            records = await awaitable
            self.last_diagnostics.append(
                WorkbenchCollectionDiagnostic(
                    source=source,
                    status="ok" if records else "empty",
                    records=len(records),
                    message="" if records else "No records returned",
                ),
            )
            return records
        except Exception as exc:  # pragma: no cover - exact lark-cli failures vary
            self.last_errors[source] = str(exc)
            self.last_diagnostics.append(
                WorkbenchCollectionDiagnostic(
                    source=source,
                    status="error",
                    message=str(exc),
                ),
            )
            return []

    async def _collect_calendar(self, date: str) -> list[WorkbenchRawRecord]:
        start, end = _day_window(date, days=1)
        payload = await self.runner.run_json(
            [
                "lark-cli",
                "calendar",
                "+agenda",
                "--as",
                "user",
                "--start",
                start,
                "--end",
                end,
                "--format",
                "json",
            ],
        )
        return [_event_to_record(item) for item in _find_items(payload, _looks_like_event)]

    async def _collect_tasks(self, date: str) -> list[WorkbenchRawRecord]:
        _, end = _day_window(date, days=7)
        payload = await self.runner.run_json(
            [
                "lark-cli",
                "task",
                "+get-my-tasks",
                "--as",
                "user",
                "--complete=false",
                "--due-end",
                end,
                "--page-all",
                "--page-limit",
                "5",
                "--format",
                "json",
            ],
        )
        return [_task_to_record(item) for item in _find_items(payload, _looks_like_task)]

    async def _collect_chat(
        self,
        date: str,
        keywords: list[str],
    ) -> list[WorkbenchRawRecord]:
        start, end = _chat_window(date)
        records: list[WorkbenchRawRecord] = []
        for keyword in [item.strip() for item in keywords if item.strip()]:
            payload = await self.runner.run_json(
                [
                    "lark-cli",
                    "im",
                    "+messages-search",
                    "--as",
                    "user",
                    "--query",
                    keyword,
                    "--start",
                    start,
                    "--end",
                    end,
                    "--page-all",
                    "--page-limit",
                    "2",
                    "--page-size",
                    "20",
                    "--format",
                    "json",
                ],
            )
            records.extend(
                _message_to_record(item)
                for item in _find_items(payload, _looks_like_message)
            )
        return records


def _parse_json_output(text: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
            return payload
        except json.JSONDecodeError:
            continue
    if not text.strip():
        return {}
    raise LarkCommandError("lark-cli did not return JSON")


def _default_lark_auth_check_dir() -> Path:
    return Path.home() / ".agents" / "skills" / "lark-auth-check"


def _parse_lark_auth_check_output(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


_BASH_EXPORT_RE = re.compile(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_POWERSHELL_ENV_RE = re.compile(
    r"^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$",
)


def _parse_lark_auth_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _BASH_EXPORT_RE.match(line) or _POWERSHELL_ENV_RE.match(line)
        if not match:
            continue
        key, raw_value = match.groups()
        env[key] = _unquote_env_value(raw_value.strip())
    return env


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _sanitize_auth_text(text: str) -> str:
    sanitized = re.sub(
        r"(LARKSUITE_CLI_[A-Z_]*TOKEN=)[^\s]+",
        r"\1***",
        text,
    )
    sanitized = re.sub(
        r"(\$env:LARKSUITE_CLI_[A-Z_]*TOKEN\s*=\s*)[^\r\n]+",
        r"\1***",
        sanitized,
    )
    return sanitized.strip()


def _day_window(date: str, *, days: int) -> tuple[str, str]:
    tz = datetime.now().astimezone().tzinfo
    target = date_type.fromisoformat(date)
    start = datetime.combine(target, time.min, tzinfo=tz)
    end = start + timedelta(days=days)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _chat_window(date: str) -> tuple[str, str]:
    tz = datetime.now().astimezone().tzinfo
    target = date_type.fromisoformat(date)
    start = datetime.combine(target - timedelta(days=1), time.min, tzinfo=tz)
    end = datetime.combine(target + timedelta(days=1), time.min, tzinfo=tz)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _find_items(payload: Any, predicate) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if predicate(value):
                found.append(value)
                return
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return found


def _looks_like_event(item: dict[str, Any]) -> bool:
    return any(key in item for key in ("event_id", "calendar_event_id")) and any(
        key in item for key in ("summary", "title", "subject")
    )


def _looks_like_task(item: dict[str, Any]) -> bool:
    return any(key in item for key in ("guid", "task_guid", "task_id")) and any(
        key in item for key in ("summary", "title", "name")
    )


def _looks_like_message(item: dict[str, Any]) -> bool:
    return any(key in item for key in ("message_id", "msg_id")) and any(
        key in item for key in ("text", "content", "body")
    )


def _event_to_record(item: dict[str, Any]) -> WorkbenchRawRecord:
    title = _first_str(item, "summary", "title", "subject", "name")
    description = _first_str(item, "description", "desc", "content")
    return WorkbenchRawRecord(
        source_type="lark_calendar",
        source_id=_first_str(item, "event_id", "calendar_event_id", "id"),
        title=title,
        summary=description or title,
        starts_at=_first_str(item, "start_time", "start", "start_at"),
        people=_people_from(item),
        metadata={"url": _first_str(item, "url", "app_link", "share_url")},
    )


def _task_to_record(item: dict[str, Any]) -> WorkbenchRawRecord:
    title = _first_str(item, "summary", "title", "name")
    description = _first_str(item, "description", "desc", "notes")
    return WorkbenchRawRecord(
        source_type="lark_task",
        source_id=_first_str(item, "guid", "task_guid", "task_id", "id"),
        title=title,
        summary=description or title,
        due_at=_due_from(item),
        people=_people_from(item),
        metadata={"url": _first_str(item, "url", "app_link", "link")},
    )


def _message_to_record(item: dict[str, Any]) -> WorkbenchRawRecord:
    chat_name = _first_str(item, "chat_name", "chat_title", "chat_id")
    text = _first_str(item, "text", "content", "body", "message")
    sender = _sender_name(item)
    people = [sender] if sender else []
    return WorkbenchRawRecord(
        source_type="lark_message",
        source_id=_first_str(item, "message_id", "msg_id", "id"),
        title=chat_name or sender or "Lark message",
        summary=text,
        people=people,
        created_at=_timestamp_to_iso(_first_str(item, "create_time", "created_at")),
        metadata={"url": _first_str(item, "url", "app_link", "link")},
    )


def _first_str(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _people_from(item: dict[str, Any]) -> list[str]:
    people: list[str] = []
    for key in ("attendees", "members", "owners", "followers", "assignees"):
        value = item.get(key)
        if isinstance(value, list):
            people.extend(_name_from_dict(child) for child in value if isinstance(child, dict))
    return [person for person in dict.fromkeys(people) if person]


def _sender_name(item: dict[str, Any]) -> str:
    sender = item.get("sender")
    if isinstance(sender, dict):
        return _name_from_dict(sender)
    return _first_str(item, "sender_name", "sender_id")


def _name_from_dict(item: dict[str, Any]) -> str:
    return _first_str(item, "display_name", "name", "user_name", "open_id", "id")


def _due_from(item: dict[str, Any]) -> str | None:
    for key in ("due_at", "due_time", "deadline"):
        value = _first_str(item, key)
        if value:
            return _timestamp_to_iso(value) or value
    due = item.get("due")
    if isinstance(due, dict):
        value = _first_str(due, "timestamp", "time", "due_time")
        if value:
            return _timestamp_to_iso(value) or value
    return None


def _timestamp_to_iso(value: str) -> str | None:
    if not value:
        return None
    if "T" in value:
        return value
    if not value.isdigit():
        return None
    timestamp = int(value)
    if timestamp > 10_000_000_000:
        timestamp = timestamp // 1000
    return datetime.fromtimestamp(timestamp).astimezone().isoformat()


def _dedupe_records(records: Iterable[WorkbenchRawRecord]) -> list[WorkbenchRawRecord]:
    seen: set[tuple[str, str]] = set()
    unique: list[WorkbenchRawRecord] = []
    for record in records:
        key = (record.source_type, record.source_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique
