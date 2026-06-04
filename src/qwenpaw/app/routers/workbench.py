# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import APIRouter, Query

from ...workbench.models import (
    DailyRadar,
    WorkbenchAnalyzeRequest,
    WorkbenchAnalyzeResponse,
    WorkbenchCollectRequest,
    WorkbenchCollectResponse,
    WorkbenchConfig,
    WorkbenchConfigUpdateRequest,
    WorkbenchDailyRadarScheduleResponse,
    WorkbenchInsightActionRequest,
    WorkbenchInsightActionResponse,
)
from ...workbench.service import get_workbench_service

router = APIRouter(prefix="/workbench", tags=["workbench"])


@router.get("/config", response_model=WorkbenchConfig)
async def get_workbench_config() -> WorkbenchConfig:
    return await get_workbench_service().get_config()


@router.patch("/config", response_model=WorkbenchConfig)
async def patch_workbench_config(
    payload: WorkbenchConfigUpdateRequest,
) -> WorkbenchConfig:
    return await get_workbench_service().update_config(
        daily_radar_schedule=payload.daily_radar_schedule,
    )


@router.post("/init", response_model=WorkbenchConfig)
async def post_workbench_init() -> WorkbenchConfig:
    return await get_workbench_service().init_workbench()


@router.post(
    "/schedule/daily-radar/ensure",
    response_model=WorkbenchDailyRadarScheduleResponse,
)
async def post_workbench_daily_radar_schedule_ensure() -> WorkbenchDailyRadarScheduleResponse:
    schedule, created, updated = (
        await get_workbench_service().ensure_daily_radar_schedule()
    )
    return WorkbenchDailyRadarScheduleResponse(
        schedule=schedule,
        created=created,
        updated=updated,
    )


@router.post(
    "/schedule/daily-radar/disable",
    response_model=WorkbenchDailyRadarScheduleResponse,
)
async def post_workbench_daily_radar_schedule_disable() -> WorkbenchDailyRadarScheduleResponse:
    schedule = await get_workbench_service().disable_daily_radar_schedule()
    return WorkbenchDailyRadarScheduleResponse(
        schedule=schedule,
        updated=True,
    )


@router.get("/radar/today", response_model=DailyRadar)
async def get_workbench_radar_today() -> DailyRadar:
    return await get_workbench_service().get_radar()


@router.get("/radar", response_model=DailyRadar)
async def get_workbench_radar(
    date: str | None = Query(None),
) -> DailyRadar:
    return await get_workbench_service().get_radar(date=date)


@router.post("/collect", response_model=WorkbenchCollectResponse)
async def post_workbench_collect(
    payload: WorkbenchCollectRequest,
) -> WorkbenchCollectResponse:
    service = get_workbench_service()
    date, records, coverage = await service.collect(
        date=payload.date,
        mode=payload.mode,
        sources=payload.sources,
        chat_keywords=payload.chat_keywords,
        records=payload.records,
    )
    return WorkbenchCollectResponse(
        date=date,
        records_added=len(records),
        coverage=coverage,
        collection_errors=service.last_collection_errors,
        collection_diagnostics=service.last_collection_diagnostics,
    )


@router.post("/analyze", response_model=WorkbenchAnalyzeResponse)
async def post_workbench_analyze(
    payload: WorkbenchAnalyzeRequest | None = None,
) -> WorkbenchAnalyzeResponse:
    service = get_workbench_service()
    radar = await service.analyze_today(date=payload.date if payload else None)

    from ..inbox_store import append_event

    await append_event(
        agent_id="default",
        source_type="workbench",
        source_id=f"daily-radar-{radar.date}",
        event_type="daily_radar_analyzed",
        status="completed",
        severity="info" if not radar.sections.risks else "warning",
        title="Daily radar analyzed",
        body=(
            f"Generated daily radar for {radar.date}: "
            f"{len(radar.highlights)} highlight(s), "
            f"{len(radar.sections.risks)} risk(s)."
        ),
        payload={
            "date": radar.date,
            "generated_at": radar.generated_at,
            "coverage": radar.coverage.model_dump(mode="json"),
            "counts": {
                "highlights": len(radar.highlights),
                "risks": len(radar.sections.risks),
                "questions": len(radar.sections.questions),
            },
        },
    )
    return WorkbenchAnalyzeResponse(radar=radar)


@router.post(
    "/insights/{insight_id}/confirm",
    response_model=WorkbenchInsightActionResponse,
)
async def post_workbench_insight_confirm(
    insight_id: str,
    payload: WorkbenchInsightActionRequest | None = None,
) -> WorkbenchInsightActionResponse:
    insight = await get_workbench_service().update_insight_status(
        insight_id,
        "confirmed",
        date=payload.date if payload else None,
    )
    return WorkbenchInsightActionResponse(insight=insight)


@router.post(
    "/insights/{insight_id}/ignore",
    response_model=WorkbenchInsightActionResponse,
)
async def post_workbench_insight_ignore(
    insight_id: str,
    payload: WorkbenchInsightActionRequest | None = None,
) -> WorkbenchInsightActionResponse:
    insight = await get_workbench_service().update_insight_status(
        insight_id,
        "ignored",
        date=payload.date if payload else None,
    )
    return WorkbenchInsightActionResponse(insight=insight)


@router.post(
    "/insights/{insight_id}/convert-to-issue",
    response_model=WorkbenchInsightActionResponse,
)
async def post_workbench_insight_convert_to_issue(
    insight_id: str,
    payload: WorkbenchInsightActionRequest | None = None,
) -> WorkbenchInsightActionResponse:
    insight = await get_workbench_service().update_insight_status(
        insight_id,
        "converted",
        date=payload.date if payload else None,
    )
    return WorkbenchInsightActionResponse(insight=insight)
