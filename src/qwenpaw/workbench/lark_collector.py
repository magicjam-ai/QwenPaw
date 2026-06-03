# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterable
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Any, Protocol

from .models import WorkbenchLarkIntegrationConfig, WorkbenchRawRecord

DEFAULT_CHAT_KEYWORDS = (
    "blocked",
    "blocker",
    "overdue",
    "延期",
    "待确认",
    "owner",
)


class JsonCommandRunner(Protocol):
    async def run_json(self, argv: list[str]) -> Any:
        """Run a command and return parsed JSON output."""


class LarkCommandError(RuntimeError):
    pass


class LarkCommandRunner:
    async def run_json(self, argv: list[str]) -> Any:
        executable = shutil.which(argv[0]) or argv[0]
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = stderr_text.strip()
            raise LarkCommandError(
                f"{argv[:3]} exited {process.returncode}: {detail}",
            )
        return _parse_json_output(stdout_text)


class LarkCollector:
    def __init__(self, runner: JsonCommandRunner | None = None):
        self.runner = runner or LarkCommandRunner()
        self.last_errors: dict[str, str] = {}

    async def collect(
        self,
        *,
        date: str,
        config: WorkbenchLarkIntegrationConfig,
        sources: list[str] | None = None,
        chat_keywords: list[str] | None = None,
    ) -> list[WorkbenchRawRecord]:
        self.last_errors = {}
        if not config.enabled:
            return []

        enabled_sources = set(sources or ("calendar", "tasks", "chat"))
        records: list[WorkbenchRawRecord] = []
        if config.collect_calendar and "calendar" in enabled_sources:
            records.extend(
                await self._safe_collect(
                    "calendar",
                    self._collect_calendar(date),
                ),
            )
        if config.collect_tasks and "tasks" in enabled_sources:
            records.extend(
                await self._safe_collect("tasks", self._collect_tasks(date)),
            )
        if config.collect_chat and "chat" in enabled_sources:
            keywords = chat_keywords or list(DEFAULT_CHAT_KEYWORDS)
            records.extend(
                await self._safe_collect(
                    "chat",
                    self._collect_chat(date, keywords),
                ),
            )
        return _dedupe_records(records)

    async def _safe_collect(
        self,
        source: str,
        awaitable,
    ) -> list[WorkbenchRawRecord]:
        try:
            return await awaitable
        except Exception as exc:  # pragma: no cover - lark-cli failures vary
            self.last_errors[source] = str(exc)
            return []

    async def _collect_calendar(self, date: str) -> list[WorkbenchRawRecord]:
        start, end = _day_window(date, days=7)
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
        return [
            _event_to_record(item)
            for item in _find_items(payload, _looks_like_event)
        ]

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
        return [
            _task_to_record(item)
            for item in _find_items(payload, _looks_like_task)
        ]

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


def _day_window(date: str, *, days: int) -> tuple[str, str]:
    tz = datetime.now().astimezone().tzinfo
    target = date_type.fromisoformat(date)
    start = datetime.combine(target, time.min, tzinfo=tz)
    end = start + timedelta(days=days)
    return (
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
    )


def _chat_window(date: str) -> tuple[str, str]:
    tz = datetime.now().astimezone().tzinfo
    target = date_type.fromisoformat(date)
    start = datetime.combine(target - timedelta(days=1), time.min, tzinfo=tz)
    end = datetime.combine(target + timedelta(days=1), time.min, tzinfo=tz)
    return (
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
    )


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
    has_id = any(key in item for key in ("event_id", "calendar_event_id"))
    return has_id and any(
        key in item for key in ("summary", "title", "subject")
    )


def _looks_like_task(item: dict[str, Any]) -> bool:
    has_id = any(key in item for key in ("guid", "task_guid", "task_id"))
    return has_id and any(key in item for key in ("summary", "title", "name"))


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
        created_at=_timestamp_to_iso(
            _first_str(item, "create_time", "created_at"),
        ),
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
            people.extend(
                _name_from_dict(child)
                for child in value
                if isinstance(child, dict)
            )
    return [person for person in dict.fromkeys(people) if person]


def _sender_name(item: dict[str, Any]) -> str:
    sender = item.get("sender")
    if isinstance(sender, dict):
        return _name_from_dict(sender)
    return _first_str(item, "sender_name", "sender_id")


def _name_from_dict(item: dict[str, Any]) -> str:
    return _first_str(
        item,
        "display_name",
        "name",
        "user_name",
        "open_id",
        "id",
    )


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


def _dedupe_records(
    records: Iterable[WorkbenchRawRecord],
) -> list[WorkbenchRawRecord]:
    seen: set[tuple[str, str]] = set()
    unique: list[WorkbenchRawRecord] = []
    for record in records:
        key = (record.source_type, record.source_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique
