import time
from types import SimpleNamespace

import httpx
import pytest

from qwenpaw.providers.copilot_provider import (
    CopilotProvider,
    COPILOT_API_BASE_URL,
    COPILOT_TOKEN_EXCHANGE_URL,
)


def _make_provider(api_key: str = "gho_test") -> CopilotProvider:
    return CopilotProvider(
        id="github-copilot",
        name="GitHub Copilot",
        base_url=COPILOT_API_BASE_URL,
        api_key=api_key,
        chat_model="OpenAIChatModel",
        auth_mode="auth_token",
        support_model_discovery=True,
    )


async def test_get_copilot_token_exchanges_and_caches(monkeypatch) -> None:
    provider = _make_provider()
    calls: list[str] = []

    async def fake_get(self, url, headers=None):  # noqa: ANN001
        calls.append(url)
        return httpx.Response(
            200,
            json={"token": "copilot-abc", "expires_at": int(time.time()) + 1800},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    token1 = await provider._get_copilot_token()
    token2 = await provider._get_copilot_token()

    assert token1 == "copilot-abc"
    assert token2 == "copilot-abc"
    # Second call served from cache -> exchange endpoint hit exactly once.
    assert calls == [COPILOT_TOKEN_EXCHANGE_URL]


async def test_get_copilot_token_refreshes_when_expired(monkeypatch) -> None:
    provider = _make_provider()
    provider._copilot_token = "stale"
    provider._copilot_token_expires_at = time.time() + 60  # within safety margin

    async def fake_get(self, url, headers=None):  # noqa: ANN001
        return httpx.Response(
            200,
            json={"token": "fresh", "expires_at": int(time.time()) + 1800},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    token = await provider._get_copilot_token()

    assert token == "fresh"


async def test_get_copilot_token_raises_on_unauthorized(monkeypatch) -> None:
    provider = _make_provider()

    async def fake_get(self, url, headers=None):  # noqa: ANN001
        return httpx.Response(401, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(PermissionError):
        await provider._get_copilot_token()


async def test_check_connection_uses_models_endpoint(monkeypatch) -> None:
    provider = _make_provider()

    async def fake_token(self):  # noqa: ANN001
        return "copilot-abc"

    class FakeModels:
        async def list(self, timeout=None):
            return SimpleNamespace(data=[])

    monkeypatch.setattr(CopilotProvider, "_get_copilot_token", fake_token)
    monkeypatch.setattr(
        provider, "_client", lambda timeout=5: SimpleNamespace(models=FakeModels())
    )

    ok, msg = await provider.check_connection(timeout=2.0)

    assert ok is True
    assert msg == ""


async def test_fetch_models_maps_payload(monkeypatch) -> None:
    provider = _make_provider()

    async def fake_token(self):  # noqa: ANN001
        return "copilot-abc"

    class FakeModels:
        async def list(self, timeout=None):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id="gpt-4o"),
                    SimpleNamespace(id="claude-3.7-sonnet"),
                ]
            )

    monkeypatch.setattr(CopilotProvider, "_get_copilot_token", fake_token)
    monkeypatch.setattr(
        provider, "_client", lambda timeout=5: SimpleNamespace(models=FakeModels())
    )

    models = await provider.fetch_models(timeout=2.0)

    assert {m.id for m in models} == {"gpt-4o", "claude-3.7-sonnet"}
