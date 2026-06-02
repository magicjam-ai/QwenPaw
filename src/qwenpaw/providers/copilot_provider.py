"""GitHub Copilot provider.

Authenticates via GitHub OAuth device flow. The long-lived GitHub OAuth token
(``gho_...``) is stored in ``api_key`` (encrypted on disk). On demand it is
exchanged for a short-lived Copilot session token, which is cached in memory
and used as a Bearer token against the OpenAI-compatible Copilot API.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import List

import httpx
from openai import APIError, AsyncOpenAI

from agentscope.model import ChatModelBase

from .provider import ModelInfo, Provider

# --- Copilot endpoints / constants (see decolua/9router for provenance) -------
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
                    COPILOT_TOKEN_EXCHANGE_URL, headers=headers
                )
            if resp.status_code in (401, 403):
                raise PermissionError(
                    "GitHub Copilot authorization expired. Please log in again."
                )
            resp.raise_for_status()
            data = resp.json()
            self._copilot_token = data["token"]
            self._copilot_token_expires_at = float(data["expires_at"])
            return self._copilot_token

    async def _build_headers(self) -> dict:
        token = await self._get_copilot_token()
        headers = _copilot_chat_headers()
        if self.custom_headers:
            headers.update(self.custom_headers)
        # Bearer is supplied via AsyncOpenAI api_key; return the rest here.
        self._auth_token = token  # used by _client
        return headers

    def _client(self, timeout: float = 5) -> AsyncOpenAI:
        # NOTE: callers must have awaited _build_headers() to populate the
        # token; in practice _client is always preceded by token retrieval in
        # the methods below.
        return AsyncOpenAI(
            base_url=self.base_url,
            api_key=getattr(self, "_auth_token", "") or "",
            timeout=timeout,
            default_headers=_copilot_chat_headers(),
        )

    async def check_connection(self, timeout: float = 5) -> tuple[bool, str]:
        try:
            await self._get_copilot_token()
            client = self._client(timeout=timeout)
            await client.models.list(timeout=timeout)
            return True, ""
        except PermissionError as e:
            return False, str(e)
        except APIError:
            return False, f"API error when connecting to `{self.base_url}`"
        except Exception:
            return (
                False,
                f"Unknown exception when connecting to `{self.base_url}`",
            )

    async def fetch_models(self, timeout: float = 5) -> List[ModelInfo]:
        try:
            await self._get_copilot_token()
            client = self._client(timeout=timeout)
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
                    )
                )
            return models
        except Exception:
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
            await self._get_copilot_token()
            client = self._client(timeout=timeout)
            res = await client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "ping"}]}
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
        except APIError:
            return False, f"API error when connecting to model '{model_id}'"
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
        }
        return OpenAIChatModelCompat(
            model_name=model_id,
            stream=True,
            api_key=getattr(self, "_auth_token", "") or self._copilot_token or "",
            stream_tool_parsing=False,
            client_kwargs=client_kwargs,
            generate_kwargs=self.get_effective_generate_kwargs(model_id),
        )
