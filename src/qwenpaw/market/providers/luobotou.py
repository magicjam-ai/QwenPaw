# -*- coding: utf-8 -*-
"""LuoBotou SkillHub market provider."""

from __future__ import annotations

from typing import Any

from ...agents.skill_system.hub import http_json_get
from ...constant import EnvVarLoader
from ...product.settings import get_product_settings
from ..schema import MarketResult
from .base import MARKET_SEARCH_TIMEOUT_S


class LuoBotouSkillHubProvider:
    key = "luobotou"
    label = "LuoBotou SkillHub"

    def available(self) -> tuple[bool, str | None]:
        base_url = self._base_url()
        if not base_url:
            return False, (
                "Set LUOBOTOU_SKILLHUB_BASE_URL or apply a product bundle "
                "with skillhub.base_url."
            )
        return True, None

    async def search(
        self,
        query: str,
        limit: int,
        page: int,
        lang: str = "en",
    ) -> tuple[list[MarketResult], bool, int | None]:
        base_url = self._base_url()
        if not base_url:
            return [], False, 0
        settings = _skillhub_settings()
        search_path = str(settings.get("search_path") or "/api/v1/search")
        url = f"{base_url.rstrip('/')}/{search_path.lstrip('/')}"
        body = await http_json_get(
            url,
            params={
                "q": query,
                "limit": max(1, int(limit)),
                "page": max(1, int(page)),
                "lang": lang,
            },
            timeout=MARKET_SEARCH_TIMEOUT_S,
        )
        items, total, has_more = _normalize_search_response(body, limit, page)
        results: list[MarketResult] = []
        for item in items:
            result = _to_market_result(base_url, item)
            if result is not None:
                results.append(result)
        return results, has_more, total

    def _base_url(self) -> str:
        from_env = EnvVarLoader.get_str("LUOBOTOU_SKILLHUB_BASE_URL", "")
        if from_env:
            return from_env
        settings = _skillhub_settings()
        return str(settings.get("base_url") or "")


def _skillhub_settings() -> dict[str, Any]:
    settings = get_product_settings().get("skillhub")
    return settings if isinstance(settings, dict) else {}


def _normalize_search_response(
    body: Any,
    limit: int,
    page: int,
) -> tuple[list[dict[str, Any]], int | None, bool]:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)], None, False
    if not isinstance(body, dict):
        return [], 0, False
    raw_items = (
        body.get("items")
        or body.get("results")
        or body.get("skills")
        or body.get("data")
        or []
    )
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("items") or raw_items.get("skills") or []
    items = (
        [x for x in raw_items if isinstance(x, dict)]
        if isinstance(raw_items, list)
        else []
    )
    total = body.get("total")
    total_value = total if isinstance(total, int) else None
    has_more = bool(body.get("has_more") or body.get("hasMore"))
    if total_value is not None:
        has_more = page * max(1, limit) < total_value
    return items, total_value, has_more


def _to_market_result(
    base_url: str,
    item: dict[str, Any],
) -> MarketResult | None:
    slug = str(
        item.get("slug") or item.get("id") or item.get("name") or "",
    ).strip()
    if not slug:
        return None
    name = str(
        item.get("display_name")
        or item.get("displayName")
        or item.get("name")
        or slug,
    )
    source_url = str(item.get("source_url") or item.get("url") or "").strip()
    if not source_url:
        source_url = f"{base_url.rstrip()}/skills/{slug}"
    return MarketResult(
        source="luobotou",
        slug=slug,
        name=name,
        description=_optional_text(
            item.get("description") or item.get("summary"),
        ),
        source_url=source_url,
        version=_optional_text(item.get("version")),
        author=_optional_text(item.get("author") or item.get("owner")),
        icon_url=_optional_text(item.get("icon_url") or item.get("iconUrl")),
        stats=(
            item.get("stats") if isinstance(item.get("stats"), dict) else None
        ),
    )


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


provider = LuoBotouSkillHubProvider()
