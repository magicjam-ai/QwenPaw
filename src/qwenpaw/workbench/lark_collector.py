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

import httpx

from .models import (
    WorkbenchCollectionDiagnostic,
    WorkbenchCollectionIssue,
    WorkbenchLarkIntegrationConfig,
    WorkbenchRawRecord,
)

DEFAULT_CHAT_KEYWORDS = (
    "blocked",
    "blocker",
    "overdue",
    "延期",
    "待确认",
    "confirm",
)
LARK_OPENAPI_BASE_URL = "https://open.feishu.cn/open-apis"


class JsonCommandRunner(Protocol):
    async def run_json(self, argv: list[str]) -> Any:
        """Run a command and return parsed JSON output."""


class AuthEnvCollectorProtocol(Protocol):
    async def collect(
        self,
        *,
        date: str,
        config: WorkbenchLarkIntegrationConfig,
        sources: list[str],
        chat_keywords: list[str],
    ) -> tuple[list[WorkbenchRawRecord], dict[str, WorkbenchCollectionIssue]]:
        """Collect records by using lark-auth-check user access token."""


class LarkCommandError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        recovery_actions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.recovery_actions = recovery_actions or []

    def to_issue(self, source: str) -> WorkbenchCollectionIssue:
        return WorkbenchCollectionIssue(
            source=source,
            message=self.message,
            code=self.code,
            recovery_actions=self.recovery_actions,
        )


class LarkCommandRunner:
    def __init__(
        self,
        *,
        auth_check_dir: str | Path | None = None,
        auth_cache_ttl_seconds: int = 300,
        timeout_seconds: float = 20.0,
    ):
        self.auth_check_dir = Path(auth_check_dir) if auth_check_dir else None
        self.auth_cache_ttl_seconds = auth_cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
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
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            process.kill()
            await process.communicate()
            raise LarkCommandError(
                f"{argv[:3]} timed out after {self.timeout_seconds:g}s",
                code="timeout",
                recovery_actions=[
                    "检查本机网络和飞书登录状态后重试。",
                    "如果 lark-cli 首次授权弹窗未完成，请完成授权后点击“重新采集”。",
                ],
            ) from exc
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        payload = _try_parse_json_output(
            stdout_text,
        ) or _try_parse_json_output(
            stderr_text,
        )
        if process.returncode != 0:
            return_code = (
                process.returncode if process.returncode is not None else 1
            )
            raise _command_error_from_payload(
                argv,
                return_code,
                payload,
                stdout_text,
                stderr_text,
            )
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise _command_error_from_payload(
                argv,
                process.returncode,
                payload,
                stdout_text,
                stderr_text,
            )
        return (
            payload if payload is not None else _parse_json_output(stdout_text)
        )

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
                code="lark_auth_check_missing",
                recovery_actions=[
                    "确认本机已安装 lark-auth-check skill 后重试。",
                ],
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
                code="lark_auth_check_failed",
                recovery_actions=[
                    "按 lark-auth-check 输出完成飞书认证后，回到收件箱点击“重新采集”。",
                ],
            )

        auth_result = _parse_lark_auth_check_output(stdout_text)
        status = auth_result.get("AUTH_STATUS")
        if status == "SUCCESS":
            env_file = auth_result.get("ENV_FILE", "")
            if not env_file:
                raise LarkCommandError(
                    "lark-auth-check did not return ENV_FILE",
                    code="lark_auth_env_missing",
                )
            auth_env = _parse_lark_auth_env_file(Path(env_file))
            if not auth_env:
                raise LarkCommandError(
                    "lark-auth-check ENV_FILE is empty",
                    code="lark_auth_env_empty",
                )
            self._auth_env_cache = dict(auth_env)
            self._auth_checked_at = now
            return auth_env

        if status == "NEED_AUTH":
            auth_link = auth_result.get("AUTH_LINK", "")
            raise LarkCommandError(
                "lark-auth-check requires user authorization"
                + (f": {auth_link}" if auth_link else ""),
                code="need_user_authorization",
                recovery_actions=[
                    "打开 lark-auth-check 返回的授权链接完成认证。",
                    "认证成功后回到收件箱点击“重新采集”。",
                ],
            )

        message = auth_result.get("ERROR_MESSAGE") or stdout_text or stderr_text
        raise LarkCommandError(
            f"lark-auth-check returned {status or 'unknown'}: "
            f"{_sanitize_auth_text(message)}",
            code="lark_auth_check_unknown",
            recovery_actions=[
                "按 lark-auth-check 输出完成飞书认证后，回到收件箱点击“重新采集”。",
            ],
        )


class LarkAuthEnvCollector:
    def __init__(
        self,
        *,
        env_paths: list[Path] | None = None,
        base_url: str = LARK_OPENAPI_BASE_URL,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.env_paths = env_paths
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._user_name_cache: dict[str, str] = {}
        self._chat_name_cache: dict[str, str] = {}

    async def collect(
        self,
        *,
        date: str,
        config: WorkbenchLarkIntegrationConfig,
        sources: list[str],
        chat_keywords: list[str],
    ) -> tuple[list[WorkbenchRawRecord], dict[str, WorkbenchCollectionIssue]]:
        token = _load_lark_auth_token(self.env_paths)
        if not token:
            return [], {
                source: _auth_env_missing_issue(source) for source in sources
            }

        records: list[WorkbenchRawRecord] = []
        issues: dict[str, WorkbenchCollectionIssue] = {}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        ) as client:
            if config.collect_calendar and "calendar" in sources:
                collected = await self._safe_collect(
                    "calendar",
                    self._collect_calendar(client, token, date),
                )
                records.extend(collected[0])
                issues.update(collected[1])
            if config.collect_tasks and "tasks" in sources:
                collected = await self._safe_collect(
                    "tasks",
                    self._collect_tasks(client, token),
                )
                records.extend(collected[0])
                issues.update(collected[1])
            if config.collect_chat and "chat" in sources:
                collected = await self._safe_collect(
                    "chat",
                    self._collect_chat(client, token, date, chat_keywords),
                )
                records.extend(collected[0])
                issues.update(collected[1])
            records = await self._resolve_record_labels(client, token, records)
        return _dedupe_records(records), issues

    async def _safe_collect(
        self,
        source: str,
        awaitable,
    ) -> tuple[list[WorkbenchRawRecord], dict[str, WorkbenchCollectionIssue]]:
        try:
            return await awaitable, {}
        except LarkCommandError as exc:
            return [], {source: exc.to_issue(source)}
        except Exception as exc:  # pragma: no cover - network failures vary
            return [], {source: _generic_issue(source, exc)}

    async def _collect_calendar(
        self,
        client: httpx.AsyncClient,
        token: str,
        date: str,
    ) -> list[WorkbenchRawRecord]:
        start, end = _day_window_unix(date, days=7)
        payload = await self._request(
            client,
            token,
            "GET",
            "/calendar/v4/calendars/primary/events",
            params={
                "page_size": "50",
                "start_time": str(start),
                "end_time": str(end),
                "user_id_type": "open_id",
            },
        )
        return [
            _event_to_record(item)
            for item in _find_items(payload, _looks_like_event)
        ]

    async def _collect_tasks(
        self,
        client: httpx.AsyncClient,
        token: str,
    ) -> list[WorkbenchRawRecord]:
        payload = await self._request(
            client,
            token,
            "GET",
            "/task/v2/tasks",
            params={
                "page_size": "50",
                "completed": "false",
                "type": "my_tasks",
                "user_id_type": "open_id",
            },
        )
        return [
            _task_to_record(item)
            for item in _find_items(payload, _looks_like_task)
        ]

    async def _collect_chat(
        self,
        client: httpx.AsyncClient,
        token: str,
        date: str,
        keywords: list[str],
    ) -> list[WorkbenchRawRecord]:
        start, end = _day_window(date, days=1)
        records: list[WorkbenchRawRecord] = []
        keyword_failures: list[tuple[str, WorkbenchCollectionIssue]] = []
        seen_message_ids: set[str] = set()
        for keyword in [item.strip() for item in keywords if item.strip()]:
            try:
                payload = await self._request(
                    client,
                    token,
                    "POST",
                    "/search/v2/message",
                    params={"page_size": "20"},
                    json_body={"query": keyword},
                )
                message_ids = _find_message_ids(payload)
                for message_id in message_ids[:10]:
                    if message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(message_id)
                    message_payload = await self._request(
                        client,
                        token,
                        "GET",
                        f"/im/v1/messages/{message_id}",
                    )
                    records.extend(
                        _message_to_record(item)
                        for item in _find_items(
                            message_payload,
                            _looks_like_message,
                        )
                        if _message_should_collect(item, start, end)
                    )
            except LarkCommandError as exc:
                keyword_failures.append((keyword, exc.to_issue("chat")))
                if _is_fatal_lark_error(exc.to_issue("chat")):
                    raise exc
                continue
            except (
                Exception
            ) as exc:  # pragma: no cover - network failures vary
                keyword_failures.append((keyword, _generic_issue("chat", exc)))
                continue
        if keyword_failures:
            issue = _keyword_failure_issue(keyword_failures)
            if not records:
                raise LarkCommandError(
                    issue.message,
                    code=issue.code,
                    recovery_actions=issue.recovery_actions,
                )
        return records

    async def _resolve_record_labels(
        self,
        client: httpx.AsyncClient,
        token: str,
        records: list[WorkbenchRawRecord],
    ) -> list[WorkbenchRawRecord]:
        updated: list[WorkbenchRawRecord] = []
        for record in records:
            people: list[str] = []
            for person in record.people:
                display = _displayable_person_name(person)
                if display:
                    people.append(display)
                    continue
                if _looks_like_lark_user_id(person):
                    resolved = await self._resolve_user_name(
                        client,
                        token,
                        person,
                    )
                    if resolved:
                        people.append(resolved)
            title = record.title
            if _looks_like_lark_chat_id(title):
                title = await self._resolve_chat_name(client, token, title)
            if not title:
                title = _summary_title(record.summary)
            updated.append(
                record.model_copy(
                    update={
                        "people": list(dict.fromkeys(people)),
                        "title": title or record.title,
                    },
                ),
            )
        return updated

    async def _resolve_user_name(
        self,
        client: httpx.AsyncClient,
        token: str,
        user_id: str,
    ) -> str:
        cached = self._user_name_cache.get(user_id)
        if cached is not None:
            return cached
        try:
            payload = await self._request(
                client,
                token,
                "GET",
                f"/contact/v3/users/{user_id}",
            )
        except LarkCommandError:
            self._user_name_cache[user_id] = ""
            return ""
        user = payload.get("user") if isinstance(payload, dict) else None
        name = _name_from_dict(user) if isinstance(user, dict) else ""
        name = _displayable_person_name(name)
        self._user_name_cache[user_id] = name
        return name

    async def _resolve_chat_name(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
    ) -> str:
        cached = self._chat_name_cache.get(chat_id)
        if cached is not None:
            return cached
        try:
            payload = await self._request(
                client,
                token,
                "GET",
                f"/im/v1/chats/{chat_id}",
            )
        except LarkCommandError:
            self._chat_name_cache[chat_id] = ""
            return ""
        name = (
            _first_str(payload, "name", "chat_name", "title")
            if isinstance(payload, dict)
            else ""
        )
        name = "" if _looks_like_lark_identifier(name) else name
        self._chat_name_cache[chat_id] = name
        return name

    async def _request(
        self,
        client: httpx.AsyncClient,
        token: str,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        except httpx.TimeoutException as exc:
            raise LarkCommandError(
                "飞书 OpenAPI 请求超时",
                code="openapi_timeout",
                recovery_actions=[
                    "检查本机网络后回到收件箱点击“重新采集”。",
                    "如果认证刚完成，请稍等几秒再重试。",
                ],
            ) from exc
        except httpx.HTTPError as exc:
            raise LarkCommandError(
                f"飞书 OpenAPI 请求失败：{exc}",
                code=exc.__class__.__name__,
                recovery_actions=[
                    "检查本机网络和代理设置后回到收件箱点击“重新采集”。",
                ],
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise LarkCommandError(
                f"飞书 OpenAPI 返回了非 JSON 响应（HTTP {response.status_code}）",
                code="invalid_json",
                recovery_actions=[
                    "检查飞书认证是否过期；必要时重新运行 lark-auth-check。",
                    "稍后回到收件箱点击“重新采集”。",
                ],
            ) from exc
        if response.status_code >= 400 or (
            isinstance(payload, dict) and payload.get("code") not in (None, 0)
        ):
            raise _openapi_error_from_payload(response.status_code, payload)
        return (
            payload.get("data", payload)
            if isinstance(payload, dict)
            else payload
        )


class LarkCollector:
    def __init__(
        self,
        runner: JsonCommandRunner | None = None,
        auth_env_collector: AuthEnvCollectorProtocol | None = None,
    ):
        self.runner = runner or LarkCommandRunner()
        self.auth_env_collector = auth_env_collector or LarkAuthEnvCollector()
        self.last_errors: dict[str, str] = {}
        self.last_diagnostics: list[WorkbenchCollectionDiagnostic] = []
        self.last_error_details: dict[str, WorkbenchCollectionIssue] = {}

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
        self.last_error_details = {}
        if not config.enabled:
            for source in ("calendar", "tasks", "chat"):
                self._mark_skipped(
                    source,
                    message="Lark integration disabled",
                )
            return []

        enabled_sources = set(sources or ("calendar", "tasks", "chat"))
        requested_sources = _requested_lark_sources(config, enabled_sources)
        preflight_issue = await self._preflight_context()
        if preflight_issue is not None:
            (
                fallback_records,
                fallback_issues,
            ) = await self.auth_env_collector.collect(
                date=date,
                config=config,
                sources=requested_sources,
                chat_keywords=chat_keywords or list(DEFAULT_CHAT_KEYWORDS),
            )
            if fallback_records or fallback_issues:
                for source, issue in fallback_issues.items():
                    self.last_errors[source] = issue.message
                    self.last_error_details[source] = issue
                self._record_fallback_diagnostics(
                    requested_sources,
                    fallback_records,
                    fallback_issues,
                )
                return fallback_records
            self._record_preflight_issue(preflight_issue, requested_sources)
            return []

        records: list[WorkbenchRawRecord] = []
        if config.collect_calendar and "calendar" in enabled_sources:
            records.extend(
                await self._safe_collect(
                    "calendar",
                    self._collect_calendar(date),
                ),
            )
        else:
            self._mark_skipped("calendar")
        if config.collect_tasks and "tasks" in enabled_sources:
            records.extend(
                await self._safe_collect("tasks", self._collect_tasks(date)),
            )
        else:
            self._mark_skipped("tasks")
        if config.collect_chat and "chat" in enabled_sources:
            records.extend(
                await self._safe_collect(
                    "chat",
                    self._collect_chat(
                        date,
                        chat_keywords or list(DEFAULT_CHAT_KEYWORDS),
                    ),
                ),
            )
        else:
            self._mark_skipped("chat")
        records.extend(
            await self._retry_failed_sources_with_auth_env(
                date=date,
                config=config,
                chat_keywords=chat_keywords or list(DEFAULT_CHAT_KEYWORDS),
            ),
        )
        return _dedupe_records(records)

    def _mark_skipped(self, source: str, *, message: str = "") -> None:
        self.last_diagnostics.append(
            WorkbenchCollectionDiagnostic(
                source=source,
                status="skipped",
                message=message,
            ),
        )

    def _record_preflight_issue(
        self,
        preflight_issue: WorkbenchCollectionIssue,
        sources: list[str],
    ) -> None:
        for source in sources:
            issue = preflight_issue.model_copy(update={"source": source})
            self.last_errors[source] = issue.message
            self.last_error_details[source] = issue
            self._record_issue_diagnostic(source, issue)

    def _record_fallback_diagnostics(
        self,
        sources: list[str],
        records: list[WorkbenchRawRecord],
        issues: dict[str, WorkbenchCollectionIssue],
    ) -> None:
        counts = _record_counts_by_lark_source(records)
        for source in sources:
            issue = issues.get(source)
            if issue is not None:
                self._record_issue_diagnostic(source, issue, records=counts.get(source, 0))
                continue
            count = counts.get(source, 0)
            self.last_diagnostics.append(
                WorkbenchCollectionDiagnostic(
                    source=source,
                    status="ok" if count else "empty",
                    records=count,
                    message="" if count else "No records returned",
                ),
            )

    def _clear_diagnostics_for_sources(self, sources: list[str]) -> None:
        source_set = set(sources)
        self.last_diagnostics = [
            diagnostic
            for diagnostic in self.last_diagnostics
            if diagnostic.source not in source_set
        ]

    async def _retry_failed_sources_with_auth_env(
        self,
        *,
        date: str,
        config: WorkbenchLarkIntegrationConfig,
        chat_keywords: list[str],
    ) -> list[WorkbenchRawRecord]:
        failed_sources = [
            source
            for source, issue in self.last_error_details.items()
            if _is_fatal_lark_error(issue)
        ]
        if not failed_sources:
            return []

        (
            fallback_records,
            fallback_issues,
        ) = await self.auth_env_collector.collect(
            date=date,
            config=config,
            sources=failed_sources,
            chat_keywords=chat_keywords,
        )
        for source in failed_sources:
            issue = fallback_issues.get(source)
            if issue is None:
                self.last_errors.pop(source, None)
                self.last_error_details.pop(source, None)
                continue
            self.last_errors[source] = issue.message
            self.last_error_details[source] = issue
        self._clear_diagnostics_for_sources(failed_sources)
        self._record_fallback_diagnostics(
            failed_sources,
            fallback_records,
            fallback_issues,
        )
        return fallback_records

    async def _preflight_context(self) -> WorkbenchCollectionIssue | None:
        try:
            await self.runner.run_json(["lark-cli", "config", "show"])
        except LarkCommandError as exc:
            issue = exc.to_issue("lark")
            if _is_fatal_lark_error(issue):
                return issue
        except Exception:
            return None
        return None

    async def _safe_collect(
        self,
        source: str,
        awaitable,
    ) -> list[WorkbenchRawRecord]:
        try:
            records = await awaitable
            issue = self.last_error_details.get(source)
            if issue is not None:
                self._record_issue_diagnostic(source, issue, records=len(records))
            else:
                self.last_diagnostics.append(
                    WorkbenchCollectionDiagnostic(
                        source=source,
                        status="ok" if records else "empty",
                        records=len(records),
                        message="" if records else "No records returned",
                    ),
                )
            return records
        except LarkCommandError as exc:
            self.last_errors[source] = exc.message
            issue = exc.to_issue(source)
            self.last_error_details[source] = issue
            self._record_issue_diagnostic(source, issue)
            return []
        except (
            Exception
        ) as exc:  # pragma: no cover - exact lark-cli failures vary
            issue = _generic_issue(source, exc)
            self.last_errors[source] = issue.message
            self.last_error_details[source] = issue
            self._record_issue_diagnostic(source, issue)
            return []

    def _record_issue_diagnostic(
        self,
        source: str,
        issue: WorkbenchCollectionIssue,
        *,
        records: int = 0,
    ) -> None:
        self.last_diagnostics.append(
            WorkbenchCollectionDiagnostic(
                source=source,
                status="error",
                records=records,
                message=issue.message,
            ),
        )

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
        start, end = _day_window(date, days=1)
        records: list[WorkbenchRawRecord] = []
        keyword_failures: list[tuple[str, WorkbenchCollectionIssue]] = []
        for keyword in [item.strip() for item in keywords if item.strip()]:
            try:
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
            except LarkCommandError as exc:
                issue = exc.to_issue("chat")
                keyword_failures.append((keyword, issue))
                if _is_fatal_lark_error(issue):
                    raise exc
                continue
            except (
                Exception
            ) as exc:  # pragma: no cover - command failures vary
                keyword_failures.append((keyword, _generic_issue("chat", exc)))
                continue
            records.extend(
                _message_to_record(item)
                for item in _find_items(payload, _looks_like_message)
                if _message_should_collect(item, start, end)
            )
        if keyword_failures:
            issue = _keyword_failure_issue(keyword_failures)
            self.last_errors["chat"] = issue.message
            self.last_error_details["chat"] = issue
            if not records:
                raise LarkCommandError(
                    issue.message,
                    code=issue.code,
                    recovery_actions=issue.recovery_actions,
                )
        return records


def _parse_json_output(text: str) -> Any:
    payload = _try_parse_json_output(text)
    if payload is not None:
        return payload
    if not text.strip():
        return {}
    raise LarkCommandError("lark-cli did not return JSON")


def _try_parse_json_output(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
            return payload
        except json.JSONDecodeError:
            continue
    return None


def _command_error_from_payload(
    argv: list[str],
    returncode: int,
    payload: Any,
    stdout_text: str,
    stderr_text: str,
) -> LarkCommandError:
    message = ""
    hint = ""
    code = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code_value = error.get("type") or error.get("code")
            code = str(code_value) if code_value else None
            message_value = error.get("message") or error.get("msg")
            hint_value = error.get("hint")
            message = str(message_value).strip() if message_value else ""
            hint = str(hint_value).strip() if hint_value else ""
        elif isinstance(error, str):
            message = error.strip()
        if not message and isinstance(payload.get("message"), str):
            message = payload["message"].strip()

    output = stderr_text.strip() or stdout_text.strip()
    if not message:
        message = f"{argv[:3]} exited {returncode}: {output}"

    recovery_actions = _recovery_actions(message, hint, payload)
    return LarkCommandError(
        message,
        code=code,
        recovery_actions=recovery_actions,
    )


def _openapi_error_from_payload(
    status_code: int,
    payload: Any,
) -> LarkCommandError:
    code = str(status_code)
    message = f"飞书 OpenAPI 返回 HTTP {status_code}"
    if isinstance(payload, dict):
        value = payload.get("code")
        if value not in (None, ""):
            code = str(value)
        message_value = payload.get("msg") or payload.get("message")
        if message_value:
            message = str(message_value).strip()
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            message = str(
                error.get("message") or error.get("msg") or message,
            ).strip()
            if error.get("code"):
                code = str(error["code"])
    return LarkCommandError(
        message,
        code=code,
        recovery_actions=_recovery_actions(message, "", payload),
    )


def _generic_issue(source: str, exc: Exception) -> WorkbenchCollectionIssue:
    message = str(exc) or exc.__class__.__name__
    return WorkbenchCollectionIssue(
        source=source,
        message=message,
        code=exc.__class__.__name__,
        recovery_actions=_recovery_actions(message, "", None),
    )


def _auth_env_missing_issue(source: str) -> WorkbenchCollectionIssue:
    return WorkbenchCollectionIssue(
        source=source,
        message="未找到 lark-auth-check 生成的飞书认证环境，无法通过 OpenAPI 采集数据。",
        code="lark_auth_env_missing",
        recovery_actions=[
            "运行 lark-auth-check 完成飞书认证检查。",
            "认证成功后回到收件箱点击“重新采集”。",
        ],
    )


def _load_lark_auth_token(paths: list[Path] | None = None) -> str:
    values: dict[str, str] = {}
    for path in _freshest_existing_paths(paths or _lark_auth_env_paths()):
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                key, value = _parse_env_line(line)
                if key and value:
                    values[key] = value
        except OSError:
            continue
        token = values.get("LARKSUITE_CLI_USER_ACCESS_TOKEN", "").strip()
        if token:
            return token
    return os.environ.get("LARKSUITE_CLI_USER_ACCESS_TOKEN", "").strip()


def _freshest_existing_paths(paths: list[Path]) -> list[Path]:
    existing: list[tuple[float, Path]] = []
    missing: list[Path] = []
    for path in paths:
        try:
            existing.append((path.stat().st_mtime, path))
        except OSError:
            missing.append(path)
    return [
        path
        for _, path in sorted(existing, key=lambda item: item[0], reverse=True)
    ] + missing


def _lark_auth_env_paths() -> list[Path]:
    file_names = (".feishu_auth_env.ps1", ".feishu_auth_env")
    roots: list[Path] = []
    if os.environ.get("JAM_SESSION_ID"):
        roots.append(Path.cwd() / ".magic_skills")
    roots.append(Path.home() / ".magic_skills")
    roots.append(Path.cwd() / ".magic_skills")
    paths: list[Path] = []
    for root in dict.fromkeys(roots):
        paths.extend(root / file_name for file_name in file_names)
    return paths


def _parse_env_line(line: str) -> tuple[str, str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return "", ""
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :]
    elif stripped.startswith("$env:"):
        stripped = stripped[len("$env:") :]
    if "=" not in stripped:
        return "", ""
    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    return key, value


def _requested_lark_sources(
    config: WorkbenchLarkIntegrationConfig,
    enabled_sources: set[str],
) -> list[str]:
    requested: list[str] = []
    if config.collect_calendar and "calendar" in enabled_sources:
        requested.append("calendar")
    if config.collect_tasks and "tasks" in enabled_sources:
        requested.append("tasks")
    if config.collect_chat and "chat" in enabled_sources:
        requested.append("chat")
    return requested


def _record_counts_by_lark_source(
    records: Iterable[WorkbenchRawRecord],
) -> dict[str, int]:
    counts = {"calendar": 0, "tasks": 0, "chat": 0}
    for record in records:
        if record.source_type == "lark_calendar":
            counts["calendar"] += 1
        elif record.source_type == "lark_task":
            counts["tasks"] += 1
        elif record.source_type == "lark_message":
            counts["chat"] += 1
    return counts


def _is_fatal_lark_error(issue: WorkbenchCollectionIssue) -> bool:
    text = f"{issue.code or ''} {issue.message}".lower()
    return (
        "hermes" in text
        or "not bound" in text
        or "auth" in text
        or "token" in text
        or "need_user_authorization" in text
        or "permission" in text
        or "scope" in text
        or "forbidden" in text
    )


def _keyword_failure_issue(
    failures: list[tuple[str, WorkbenchCollectionIssue]],
) -> WorkbenchCollectionIssue:
    messages = [f"{keyword}: {issue.message}" for keyword, issue in failures]
    actions: list[str] = []
    code = failures[0][1].code
    for _, issue in failures:
        actions.extend(issue.recovery_actions)
    return WorkbenchCollectionIssue(
        source="chat",
        message="部分飞书消息关键词搜索失败：" + "；".join(messages),
        code=code or "partial_keyword_failure",
        recovery_actions=list(dict.fromkeys(actions))
        or ["检查 lark-cli 消息搜索权限后，点击“重新采集”。"],
    )


def _recovery_actions(message: str, hint: str, payload: Any) -> list[str]:
    text = f"{message} {hint}".lower()
    actions: list[str] = []
    if "not bound" in text or "config bind" in text or "hermes" in text:
        actions.extend(
            [
                "在终端运行 `lark-cli config bind --help` 查看绑定方式。",
                "确认要使用当前用户身份后，运行 `lark-cli config bind` 绑定 user-default 身份。",
                "绑定完成后回到收件箱，点击“重新采集”。",
            ],
        )
    elif (
        "auth" in text
        or "token" in text
        or "login" in text
        or "need_user_authorization" in text
    ):
        actions.extend(
            [
                "运行 `lark-cli auth login --as user` 重新登录飞书用户身份。",
                "确认已授予日历、任务和消息搜索所需权限后，点击“重新采集”。",
            ],
        )
    elif "permission" in text or "scope" in text or "forbidden" in text:
        actions.extend(
            [
                "在飞书开放平台或 lark-cli 授权页补齐缺失权限。",
                "重新授权后点击“重新采集”。",
            ],
        )
    elif "not found" in text or "no such file" in text:
        actions.extend(
            [
                "确认已安装 lark-cli，并且 `lark-cli --version` 可以在当前终端运行。",
                "安装或修复 PATH 后点击“重新采集”。",
            ],
        )
    elif "did not return json" in text:
        actions.extend(
            [
                "运行对应 lark-cli 命令并加上 `--format json` 检查输出。",
                "如果 CLI 版本较旧，先运行 `lark-cli update` 后重试。",
            ],
        )

    if hint and hint not in actions and "ask the user" not in hint.lower():
        actions.append(hint)

    if isinstance(payload, dict):
        notice = payload.get("_notice")
        update = notice.get("update") if isinstance(notice, dict) else None
        if isinstance(update, dict) and isinstance(update.get("command"), str):
            actions.append(f"可选：运行 `{update['command']}` 升级 lark-cli。")

    if not actions:
        actions.append("检查 lark-cli 输出和网络连接后，回到收件箱点击“重新采集”。")
    return list(dict.fromkeys(actions))


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
    return start.isoformat(timespec="seconds"), end.isoformat(
        timespec="seconds",
    )


def _day_window_unix(date: str, *, days: int) -> tuple[int, int]:
    tz = datetime.now().astimezone().tzinfo
    target = date_type.fromisoformat(date)
    start = datetime.combine(target, time.min, tzinfo=tz)
    end = start + timedelta(days=days)
    return int(start.timestamp()), int(end.timestamp())


def _chat_window(date: str) -> tuple[str, str]:
    tz = datetime.now().astimezone().tzinfo
    target = date_type.fromisoformat(date)
    start = datetime.combine(target - timedelta(days=1), time.min, tzinfo=tz)
    end = datetime.combine(target + timedelta(days=1), time.min, tzinfo=tz)
    return start.isoformat(timespec="seconds"), end.isoformat(
        timespec="seconds",
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


def _find_message_ids(payload: Any) -> list[str]:
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str) and value.startswith("om_"):
            found.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return list(dict.fromkeys(found))


def _looks_like_event(item: dict[str, Any]) -> bool:
    return any(
        key in item for key in ("event_id", "calendar_event_id")
    ) and any(key in item for key in ("summary", "title", "subject"))


def _looks_like_task(item: dict[str, Any]) -> bool:
    return any(
        key in item for key in ("guid", "task_guid", "task_id")
    ) and any(key in item for key in ("summary", "title", "name"))


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
        starts_at=_first_time(item, "start_time", "start", "start_at"),
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
    text = _message_text(item)
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
        if isinstance(value, dict):
            nested = _first_str(
                value,
                "text",
                "content",
                "plain_text",
                "summary",
                "title",
                "name",
                "display_name",
                "timestamp",
                "time",
            )
            if nested:
                return nested
    return ""


def _message_text(item: dict[str, Any]) -> str:
    direct = _first_str(item, "text", "content", "message")
    if direct:
        return _json_text(direct) or direct
    body = item.get("body")
    if isinstance(body, dict):
        nested = _first_str(body, "text", "content", "plain_text")
        return _json_text(nested) or nested
    if isinstance(body, str):
        return _json_text(body) or body.strip()
    return ""


def _message_should_collect(
    item: dict[str, Any],
    start: str,
    end: str,
) -> bool:
    if _message_mentions_all(item):
        return False
    created_at = _first_str(item, "create_time", "created_at")
    if not created_at:
        return True
    created = _datetime_from_lark_time(created_at)
    start_at = _datetime_from_lark_time(start)
    end_at = _datetime_from_lark_time(end)
    if created is None or start_at is None or end_at is None:
        return True
    return start_at <= created < end_at


def _message_mentions_all(item: dict[str, Any]) -> bool:
    text = _message_text(item).lower()
    if "@_all" in text or "@所有人" in text or "@all" in text:
        return True
    mentions = item.get("mentions")
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            candidate = " ".join(
                _first_str(mention, key).lower()
                for key in ("id", "key", "name", "text", "tenant_key")
            )
            if "_all" in candidate or "all" == candidate.strip():
                return True
    return False


def _json_text(value: str) -> str:
    if not value:
        return ""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return ""
    return _extract_text(payload).strip()


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        chunks: list[str] = []
        for key in ("text", "content", "plain_text", "title", "name"):
            nested = value.get(key)
            if nested is not None:
                chunks.append(_extract_text(nested))
        if chunks:
            return " ".join(chunk for chunk in chunks if chunk)
        return " ".join(
            _extract_text(child)
            for child in value.values()
            if isinstance(child, (dict, list))
        )
    if isinstance(value, list):
        return " ".join(_extract_text(child) for child in value)
    return ""


def _first_time(item: dict[str, Any], *keys: str) -> str:
    value = _first_str(item, *keys)
    return _timestamp_to_iso(value) or value


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


def _displayable_person_name(value: str) -> str:
    person = value.strip()
    if not person or _looks_like_lark_identifier(person):
        return ""
    return person


def _summary_title(value: str) -> str:
    text = " ".join(value.strip().split())
    if not text:
        return ""
    return text[:80]


def _looks_like_lark_user_id(value: str) -> bool:
    return bool(re.match(r"^ou_[A-Za-z0-9]+$", value.strip()))


def _looks_like_lark_chat_id(value: str) -> bool:
    return bool(re.match(r"^oc_[A-Za-z0-9]+$", value.strip()))


def _looks_like_lark_identifier(value: str) -> bool:
    text = value.strip()
    return bool(
        re.match(r"^(ou|on|oc|om|cli)_[A-Za-z0-9]+$", text)
        or re.match(r"^[A-Za-z0-9_-]{24,}$", text),
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


def _datetime_from_lark_time(value: str) -> datetime | None:
    if not value:
        return None
    iso_value = _timestamp_to_iso(value) or value
    try:
        return datetime.fromisoformat(iso_value)
    except ValueError:
        return None


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
