# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
from __future__ import annotations

import json
import sys
import types
from collections.abc import Generator
from dataclasses import dataclass
from importlib import util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw.app import inbox_store
from qwenpaw.workbench import service as workbench_service_module
from qwenpaw.workbench.models import WorkbenchCollectionIssue
from qwenpaw.workbench.service import WorkbenchService
from qwenpaw.workbench.store import WorkbenchStore


class FakeCollector:
    last_errors: dict[str, str] = {}

    async def collect(
        self,
        *,
        date: str,  # pylint: disable=unused-argument
        config,  # pylint: disable=unused-argument
        sources=None,  # pylint: disable=unused-argument
        chat_keywords=None,  # pylint: disable=unused-argument
    ):
        return [
            {
                "source_type": "lark_task",
                "source_id": "live-task-1",
                "title": "Live task from Lark",
                "summary": "Confirm production collector",
                "due_at": "2026-06-02T18:00:00+08:00",
                "people": ["Alice"],
            },
        ]


class FailingCollector:
    def __init__(self):
        self.last_errors: dict[str, str] = {}
        self.last_error_details: dict[str, WorkbenchCollectionIssue] = {}

    async def collect(
        self,
        *,
        date: str,  # pylint: disable=unused-argument
        config,  # pylint: disable=unused-argument
        sources=None,  # pylint: disable=unused-argument
        chat_keywords=None,  # pylint: disable=unused-argument
    ):
        auth_error = (
            "need_user_authorization: calendar:" + "calendar.event:read"
        )
        self.last_errors = {
            "calendar": auth_error,
        }
        self.last_error_details = {
            "calendar": WorkbenchCollectionIssue(
                source="calendar",
                message=auth_error,
                code="need_user_authorization",
                recovery_actions=[
                    "运行 `lark-cli auth login --as user` 重新登录飞书用户身份。",
                    "确认已授予日历权限后，点击“重新采集”。",
                ],
            ),
        }
        return []


def _load_workbench_router():
    routers_pkg_name = "qwenpaw.app.routers"
    if routers_pkg_name not in sys.modules:
        routers_pkg = types.ModuleType(routers_pkg_name)
        routers_pkg.__path__ = [
            str(
                Path(__file__).parents[2]
                / "src"
                / "qwenpaw"
                / "app"
                / "routers",
            ),
        ]
        sys.modules[routers_pkg_name] = routers_pkg

    module_name = f"{routers_pkg_name}.workbench"
    module_path = (
        Path(__file__).parents[2]
        / "src"
        / "qwenpaw"
        / "app"
        / "routers"
        / "workbench.py"
    )
    spec = util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.router


workbench_router = _load_workbench_router()


@dataclass
class WorkbenchClient:
    client: TestClient
    working_dir: Path


def _workbench_root(working_dir: Path) -> Path:
    return working_dir / ".workbench"


@pytest.fixture()
def workbench_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[WorkbenchClient, None, None]:
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.setattr(
        workbench_service_module,
        "_DEFAULT_SERVICE",
        WorkbenchService(
            WorkbenchStore(working_dir),
            lark_collector=FakeCollector(),
        ),
    )
    monkeypatch.setattr(
        inbox_store,
        "_INBOX_PATH",
        working_dir / "inbox_events.json",
    )

    app = FastAPI()
    app.include_router(workbench_router, prefix="/api")
    with TestClient(app) as client:
        yield WorkbenchClient(client=client, working_dir=working_dir)


@pytest.mark.integration
@pytest.mark.p1
def test_workbench_config_and_init_contract(
    workbench_client: WorkbenchClient,
) -> None:
    config_resp = workbench_client.client.get("/api/workbench/config")
    assert config_resp.status_code == 200
    config = config_resp.json()
    assert config["workspace"]["storage_mode"] == "local"
    assert config["integrations"]["lark"]["collect_calendar"] is True

    init_resp = workbench_client.client.post("/api/workbench/init")
    assert init_resp.status_code == 200
    for rel_path in (
        "cache/raw",
        "summaries",
        "people",
        "meetings",
        "comms",
        "issues",
    ):
        assert (
            _workbench_root(workbench_client.working_dir) / rel_path
        ).is_dir()


@pytest.mark.integration
@pytest.mark.p1
def test_workbench_collect_analyze_and_actions_roundtrip(
    workbench_client: WorkbenchClient,
) -> None:
    init_resp = workbench_client.client.post("/api/workbench/init")
    assert init_resp.status_code == 200

    collect_resp = workbench_client.client.post(
        "/api/workbench/collect",
        json={
            "date": "2026-06-02",
            "records": [
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
        },
    )
    assert collect_resp.status_code == 200
    assert collect_resp.json()["coverage"]["tasks"] == 1

    analyze_resp = workbench_client.client.post(
        "/api/workbench/analyze",
        json={"date": "2026-06-02"},
    )
    assert analyze_resp.status_code == 200
    radar = analyze_resp.json()["radar"]
    insight_id = radar["sections"]["key_tasks"][0]["id"]
    assert radar["coverage"]["calendar_events"] == 1
    assert radar["sections"]["key_meetings"]
    assert radar["sections"]["key_people"]
    assert radar["sections"]["risks"]

    get_today_resp = workbench_client.client.get(
        "/api/workbench/radar",
        params={"date": "2026-06-02"},
    )
    assert get_today_resp.status_code == 200
    assert get_today_resp.json()["date"] == "2026-06-02"

    confirm_resp = workbench_client.client.post(
        f"/api/workbench/insights/{insight_id}/confirm",
        json={"date": "2026-06-02"},
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["insight"]["status"] == "confirmed"

    ignore_resp = workbench_client.client.post(
        f"/api/workbench/insights/{insight_id}/ignore",
        json={"date": "2026-06-02"},
    )
    assert ignore_resp.status_code == 200
    assert ignore_resp.json()["insight"]["status"] == "ignored"

    convert_resp = workbench_client.client.post(
        f"/api/workbench/insights/{insight_id}/convert-to-issue",
        json={"date": "2026-06-02"},
    )
    assert convert_resp.status_code == 200
    assert convert_resp.json()["insight"]["status"] == "converted"
    assert (
        _workbench_root(workbench_client.working_dir)
        / "issues"
        / f"{insight_id}.json"
    ).is_file()

    inbox_file = workbench_client.working_dir / "inbox_events.json"
    inbox_events = json.loads(inbox_file.read_text(encoding="utf-8"))
    assert any(event["source_type"] == "workbench" for event in inbox_events)


@pytest.mark.integration
@pytest.mark.p1
def test_workbench_radar_today_returns_stable_empty_radar(
    workbench_client: WorkbenchClient,
) -> None:
    resp = workbench_client.client.get("/api/workbench/radar/today")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["coverage"]["tasks"] == 0
    assert payload["sections"]["key_tasks"] == []


@pytest.mark.integration
@pytest.mark.p1
def test_workbench_live_collect_contract(
    workbench_client: WorkbenchClient,
) -> None:
    collect_resp = workbench_client.client.post(
        "/api/workbench/collect",
        json={
            "mode": "live",
            "date": "2026-06-02",
            "sources": ["tasks"],
            "chat_keywords": ["owner"],
        },
    )

    assert collect_resp.status_code == 200
    payload = collect_resp.json()
    assert payload["records_added"] == 1
    assert payload["coverage"]["tasks"] == 1

    radar_resp = workbench_client.client.post(
        "/api/workbench/analyze",
        json={"date": "2026-06-02"},
    )

    assert radar_resp.status_code == 200
    radar = radar_resp.json()["radar"]
    assert radar["sections"]["key_tasks"][0]["title"] == "Live task from Lark"
    assert radar["sections"]["key_tasks"][0]["sources"][0]["source_id"] == (
        "live-task-1"
    )


@pytest.mark.integration
@pytest.mark.p1
def test_workbench_live_collect_returns_collection_errors(
    workbench_client: WorkbenchClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_service = WorkbenchService(
        WorkbenchStore(workbench_client.working_dir),
        lark_collector=FailingCollector(),
    )
    monkeypatch.setattr(
        workbench_service_module,
        "_DEFAULT_SERVICE",
        failing_service,
    )

    collect_resp = workbench_client.client.post(
        "/api/workbench/collect",
        json={"mode": "live", "date": "2026-06-02"},
    )

    assert collect_resp.status_code == 200
    payload = collect_resp.json()
    assert payload["records_added"] == 0
    assert payload["collection_errors"] == {
        "calendar": "need_user_authorization: calendar:calendar.event:read",
    }
    assert payload["collection_diagnostics"]["calendar"]["code"] == (
        "need_user_authorization"
    )
    assert (
        "auth login"
        in payload["collection_diagnostics"]["calendar"]["recovery_actions"][0]
    )
