# -*- coding: utf-8 -*-
"""GitHub Copilot device-flow OAuth endpoints.

Drives the GitHub OAuth device flow server-side. The resulting long-lived
GitHub token is written straight into the ``github-copilot`` provider config
(encrypted by the secret store); it is never returned to the browser.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Dict, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ...providers.provider_manager import ProviderManager

router = APIRouter(prefix="/models", tags=["copilot-oauth"])

# --- GitHub device-flow constants --------------------------------------------
_GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
_GITHUB_SCOPE = "read:user"
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_COPILOT_PROVIDER_ID = "github-copilot"


@dataclass
class _Flow:
    device_code: str
    expires_at: float


_flow_store: Dict[str, _Flow] = {}


def _purge_expired() -> None:
    now = time.time()
    for key in [k for k, v in _flow_store.items() if v.expires_at < now]:
        del _flow_store[key]


async def get_provider_manager(request: Request) -> ProviderManager:
    return request.app.state.provider_manager


class DeviceCodeResponse(BaseModel):
    flow_id: str = Field(..., description="Opaque handle for status polling")
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceStatusResponse(BaseModel):
    status: Literal["pending", "slow_down", "authorized", "expired", "denied"]


@router.post(
    "/github-copilot/device-code",
    response_model=DeviceCodeResponse,
    summary="Start GitHub Copilot device-flow login",
)
async def start_device_code() -> DeviceCodeResponse:
    _purge_expired()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _DEVICE_CODE_URL,
            data={"client_id": _GITHUB_CLIENT_ID, "scope": _GITHUB_SCOPE},
            headers={"Accept": "application/json"},
        )
    resp.raise_for_status()
    data = resp.json()

    flow_id = secrets.token_urlsafe(24)
    expires_in = int(data.get("expires_in", 900))
    _flow_store[flow_id] = _Flow(
        device_code=data["device_code"],
        expires_at=time.time() + expires_in,
    )
    return DeviceCodeResponse(
        flow_id=flow_id,
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        expires_in=expires_in,
        interval=int(data.get("interval", 5)),
    )


@router.get(
    "/github-copilot/device-status",
    response_model=DeviceStatusResponse,
    summary="Poll GitHub Copilot device-flow status",
)
async def poll_device_status(
    flow_id: str = Query(...),
    manager: ProviderManager = Depends(get_provider_manager),
) -> DeviceStatusResponse:
    _purge_expired()
    flow = _flow_store.get(flow_id)
    if flow is None:
        return DeviceStatusResponse(status="expired")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _ACCESS_TOKEN_URL,
            data={
                "client_id": _GITHUB_CLIENT_ID,
                "device_code": flow.device_code,
                "grant_type": _GRANT_TYPE,
            },
            headers={"Accept": "application/json"},
        )
    data = resp.json()

    access_token = data.get("access_token")
    if access_token:
        manager.update_provider(
            _COPILOT_PROVIDER_ID,
            {"api_key": access_token},
        )
        _flow_store.pop(flow_id, None)
        return DeviceStatusResponse(status="authorized")

    error = data.get("error")
    if error == "authorization_pending":
        return DeviceStatusResponse(status="pending")
    if error == "slow_down":
        return DeviceStatusResponse(status="slow_down")
    if error == "access_denied":
        _flow_store.pop(flow_id, None)
        return DeviceStatusResponse(status="denied")
    # expired_token or anything else
    _flow_store.pop(flow_id, None)
    return DeviceStatusResponse(status="expired")
