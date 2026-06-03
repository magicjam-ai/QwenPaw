# -*- coding: utf-8 -*-
"""GitHub Copilot provider.

Authenticates via GitHub OAuth device flow. The long-lived GitHub OAuth token
(``gho_...``) is stored in ``api_key`` (encrypted on disk). On demand it is
exchanged for a short-lived Copilot session token, which is cached in memory
and used as a Bearer token against the OpenAI-compatible Copilot API.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import List

import httpx
from openai import APIError, AsyncOpenAI

from agentscope.model import ChatModelBase

from .provider import ModelInfo, Provider

logger = logging.getLogger(__name__)

# --- Copilot endpoints / constants -------------------------------------------
COPILOT_API_BASE_URL = "https://api.githubcopilot.com"
COPILOT_TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"

# Header values that mimic the VS Code Copilot Chat client. Centralised here so
# they can be bumped in one place when GitHub updates expected versions.
_EDITOR_VERSION = "vscode/1.107.1"
_EDITOR_PLUGIN_VERSION = "copilot-chat/0.26.7"
_USER_AGENT = "GitHubCopilotChat/0.26.7"
_GITHUB_API_VERSION = "2025-04-01"

# Refresh the Copilot token this many seconds before its stated expiry.
_TOKEN_SAFETY_MARGIN = 120


def _api_error_detail(exc: APIError) -> str:
    """Best-effort human-readable reason from an OpenAI/Copilot ``APIError``.

    Copilot returns structured errors such as
    ``{"error": {"message": "You have exceeded your monthly quota", ...}}``.
    Surfacing that message instead of a generic "API error" lets the UI show
    actionable causes (quota exhaustion, auth failures, bad model id) rather
    than hiding them behind an opaque string.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
    message = getattr(exc, "message", None)
    return str(message) if message else str(exc)


def _copilot_chat_headers() -> dict:
    """Headers required by the Copilot chat/models API (per request)."""
    return {
        "copilot-integration-id": "vscode-chat",
        "editor-version": _EDITOR_VERSION,
        "editor-plugin-version": _EDITOR_PLUGIN_VERSION,
        "user-agent": _USER_AGENT,
        "openai-intent": "conversation-panel",
        "x-github-api-version": _GITHUB_API_VERSION,
        "x-request-id": str(uuid.uuid4()),
    }


class _CopilotAuthTransport(httpx.AsyncHTTPTransport):
    """Async transport that injects a fresh Copilot bearer token per request.

    Copilot session tokens are short-lived (~25 min). A chat model built once
    by :meth:`CopilotProvider.get_chat_model_instance` may outlive a single
    token, so the token must be minted/refreshed on every outgoing request
    rather than captured at client-construction time. This transport calls back
    into the provider's cached, auto-refreshing token getter and overwrites the
    ``Authorization`` and per-request ``x-request-id`` headers accordingly.
    """

    def __init__(self, provider: "CopilotProvider") -> None:
        super().__init__()
        self._provider = provider

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        # pylint: disable-next=protected-access
        token = await self._provider._get_copilot_token()
        headers = [
            (k, v)
            for k, v in request.headers.items()
            if k.lower() not in ("authorization", "x-request-id")
        ]
        headers.append(("authorization", f"Bearer {token}"))
        headers.append(("x-request-id", str(uuid.uuid4())))
        new_request = httpx.Request(
            method=request.method,
            url=request.url,
            headers=headers,
            content=request.content,
            extensions=request.extensions,
        )
        return await super().handle_async_request(new_request)


class CopilotProvider(Provider):
    """Provider for GitHub Copilot models via the OpenAI-compatible API."""

    # In-memory cache of the short-lived Copilot session token.
    _copilot_token: str | None = None
    _copilot_token_expires_at: float = 0.0
    # Guards concurrent refreshes so parallel requests don't double-exchange.
    _refresh_lock: asyncio.Lock | None = None

    def _get_refresh_lock(self) -> asyncio.Lock:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    def update_config(self, config: dict) -> None:
        """Invalidate the cached session token when the GitHub token changes.

        The short-lived Copilot session token is derived from ``api_key`` (the
        long-lived ``gho_`` token). If the user re-authenticates with a
        different GitHub account, the previously cached session token would
        otherwise remain valid for up to ~25 minutes and keep serving the old
        account, so it must be dropped whenever ``api_key`` is updated.
        """
        new_key = config.get("api_key")
        if new_key is not None and str(new_key).strip() != self.api_key:
            self._copilot_token = None
            self._copilot_token_expires_at = 0.0
        super().update_config(config)

    async def _get_copilot_token(self) -> str:
        """Return a valid Copilot session token, refreshing if needed.

        Raises:
            PermissionError: when the stored GitHub token is rejected and the
                user must re-authenticate.
        """
        now = time.time()
        if (
            self._copilot_token
            and self._copilot_token_expires_at - _TOKEN_SAFETY_MARGIN > now
        ):
            return self._copilot_token

        async with self._get_refresh_lock():
            now = time.time()
            if (
                self._copilot_token
                and self._copilot_token_expires_at - _TOKEN_SAFETY_MARGIN > now
            ):
                return self._copilot_token

            headers = {
                "Authorization": f"token {self.api_key}",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
                "Editor-Version": _EDITOR_VERSION,
                "Editor-Plugin-Version": _EDITOR_PLUGIN_VERSION,
                "x-github-api-version": _GITHUB_API_VERSION,
            }
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    COPILOT_TOKEN_EXCHANGE_URL,
                    headers=headers,
                )
            if resp.status_code in (401, 403):
                raise PermissionError("GitHub Copilot authorization expired.")
            resp.raise_for_status()
            data = resp.json()
            self._copilot_token = str(data["token"])
            self._copilot_token_expires_at = float(data["expires_at"])
            return self._copilot_token

    def _client(self, token: str, timeout: float = 5) -> AsyncOpenAI:
        """Build an OpenAI-compatible client bound to a Copilot session token.

        ``token`` must be a freshly retrieved Copilot session token (see
        :meth:`_get_copilot_token`); it is sent as the bearer credential. The
        Copilot API rejects requests with a missing/empty ``Authorization``
        header, so callers must pass the live token rather than relying on any
        cached instance state.
        """
        return AsyncOpenAI(
            base_url=self.base_url,
            api_key=token or "",
            timeout=timeout,
            default_headers=_copilot_chat_headers(),
        )

    async def check_connection(self, timeout: float = 5) -> tuple[bool, str]:
        try:
            token = await self._get_copilot_token()
            client = self._client(token, timeout=timeout)
            await client.models.list(timeout=timeout)
            return True, ""
        except PermissionError as e:
            return False, str(e)
        except APIError as exc:
            return False, _api_error_detail(exc)
        except Exception:
            return (
                False,
                f"Unknown exception when connecting to `{self.base_url}`",
            )

    async def fetch_models(self, timeout: float = 5) -> List[ModelInfo]:
        try:
            token = await self._get_copilot_token()
            client = self._client(token, timeout=timeout)
            payload = await client.models.list(timeout=timeout)
            models: List[ModelInfo] = []
            for item in getattr(payload, "data", []) or []:
                model_id = getattr(item, "id", None)
                if not model_id:
                    continue
                models.append(
                    ModelInfo(
                        id=model_id,
                        name=getattr(item, "name", None) or model_id,
                        probe_source="documentation",
                    ),
                )
            return models
        except APIError as exc:
            logger.warning(
                "GitHub Copilot model discovery failed: %s",
                _api_error_detail(exc),
            )
            return []
        except Exception:
            logger.warning(
                "GitHub Copilot model discovery failed",
                exc_info=True,
            )
            return []

    async def check_model_connection(
        self,
        model_id: str,
        timeout: float = 5,
    ) -> tuple[bool, str]:
        model_id = (model_id or "").strip()
        if not model_id:
            return False, "Empty model ID"
        try:
            token = await self._get_copilot_token()
            client = self._client(token, timeout=timeout)
            res = await client.chat.completions.create(
                model=model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "ping"}],
                    },
                ],
                timeout=timeout,
                max_tokens=20,
                stream=True,
            )
            async for _ in res:
                break
            return True, ""
        except PermissionError as e:
            return False, str(e)
        except APIError as exc:
            return False, _api_error_detail(exc)
        except Exception:
            return (
                False,
                f"Unknown exception when connecting to model '{model_id}'",
            )

    def get_chat_model_instance(self, model_id: str) -> ChatModelBase:
        """Return a chat model bound to this provider and ``model_id``.

        Uses the cached short-lived Copilot session token as the bearer key.
        Callers should ensure a token has been fetched (e.g. via
        ``check_connection``/``check_model_connection``) before driving chat;
        the token is auto-refreshed by ``_get_copilot_token``.
        """
        from .openai_chat_model_compat import OpenAIChatModelCompat

        client_kwargs: dict = {
            "base_url": self.base_url,
            "default_headers": _copilot_chat_headers(),
            # The transport injects a fresh bearer token on every request, so
            # long-lived chat models never use a stale Copilot session token.
            "http_client": httpx.AsyncClient(
                transport=_CopilotAuthTransport(self),
            ),
        }
        return OpenAIChatModelCompat(
            model_name=model_id,
            stream=True,
            # Placeholder: the real Authorization header is set per request by
            # _CopilotAuthTransport. AsyncOpenAI just requires a non-empty key.
            api_key="copilot",
            stream_tool_parsing=False,
            client_kwargs=client_kwargs,
            generate_kwargs=self.get_effective_generate_kwargs(model_id),
        )
