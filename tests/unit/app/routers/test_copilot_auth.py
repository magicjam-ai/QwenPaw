import httpx
import pytest

import qwenpaw.app.routers.copilot_auth as copilot_auth
from qwenpaw.app.routers.copilot_auth import (
    _flow_store,
    start_device_code,
    poll_device_status,
)


class _FakeManager:
    def __init__(self) -> None:
        self.saved: dict | None = None

    def update_provider(self, provider_id: str, config: dict) -> bool:
        self.saved = {"provider_id": provider_id, "config": config}
        return True


@pytest.fixture(autouse=True)
def _clear_store():
    _flow_store.clear()
    yield
    _flow_store.clear()


async def test_start_device_code_stores_flow(monkeypatch) -> None:
    async def fake_post(self, url, data=None, headers=None):  # noqa: ANN001
        return httpx.Response(
            200,
            json={
                "device_code": "DEV123",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = await start_device_code()

    assert resp.user_code == "ABCD-EFGH"
    assert resp.verification_uri == "https://github.com/login/device"
    assert resp.flow_id in _flow_store
    # Raw device_code must never be returned to the client.
    assert not hasattr(resp, "device_code")


async def test_poll_status_pending(monkeypatch) -> None:
    _flow_store["flow-1"] = copilot_auth._Flow(device_code="DEV", expires_at=1e18)

    async def fake_post(self, url, data=None, headers=None):  # noqa: ANN001
        return httpx.Response(
            200,
            json={"error": "authorization_pending"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    manager = _FakeManager()
    resp = await poll_device_status(flow_id="flow-1", manager=manager)

    assert resp.status == "pending"
    assert manager.saved is None


async def test_poll_status_authorized_saves_token(monkeypatch) -> None:
    _flow_store["flow-2"] = copilot_auth._Flow(device_code="DEV", expires_at=1e18)

    async def fake_post(self, url, data=None, headers=None):  # noqa: ANN001
        return httpx.Response(
            200,
            json={"access_token": "gho_real"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    manager = _FakeManager()
    resp = await poll_device_status(flow_id="flow-2", manager=manager)

    assert resp.status == "authorized"
    assert manager.saved == {
        "provider_id": "github-copilot",
        "config": {"api_key": "gho_real"},
    }
    assert "flow-2" not in _flow_store  # session cleared


async def test_poll_status_unknown_flow_returns_expired() -> None:
    manager = _FakeManager()
    resp = await poll_device_status(flow_id="nope", manager=manager)
    assert resp.status == "expired"
