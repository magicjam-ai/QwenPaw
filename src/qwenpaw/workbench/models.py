# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

InsightKind = Literal["person", "task", "meeting", "risk", "decision", "question"]
InsightPriority = Literal["P0", "P1", "P2", "P3"]
InsightStatus = Literal["new", "confirmed", "ignored", "converted"]
InsightConfidence = Literal["low", "medium", "high"]
WorkbenchCollectMode = Literal["manual", "live"]
WorkbenchCollectSource = Literal["calendar", "tasks", "chat"]


class WorkbenchWorkspaceConfig(BaseModel):
    data_root: Path
    storage_mode: Literal["local"] = "local"
    protect_personal_data: bool = True


class WorkbenchLarkIntegrationConfig(BaseModel):
    enabled: bool = True
    collect_chat: bool = True
    collect_calendar: bool = True
    collect_tasks: bool = True
    chat_scope: str = "bot_visible"
    chat_keywords: list[str] = Field(
        default_factory=lambda: [
            "blocked",
            "blocker",
            "overdue",
            "延期",
            "待确认",
            "confirm",
        ],
    )


class WorkbenchIntegrationsConfig(BaseModel):
    lark: WorkbenchLarkIntegrationConfig = Field(
        default_factory=WorkbenchLarkIntegrationConfig,
    )


class WorkbenchFeaturesConfig(BaseModel):
    daily_radar: str = "enabled"
    meeting_collection: str = "auto"
    action_hub: str = "enabled"


class WorkbenchConfig(BaseModel):
    workspace: WorkbenchWorkspaceConfig
    integrations: WorkbenchIntegrationsConfig = Field(
        default_factory=WorkbenchIntegrationsConfig,
    )
    features: WorkbenchFeaturesConfig = Field(
        default_factory=WorkbenchFeaturesConfig,
    )


class WorkbenchSourceRef(BaseModel):
    source_type: str
    source_id: str
    title: str = ""
    url: str | None = None
    excerpt: str = ""


class WorkbenchInsight(BaseModel):
    id: str
    kind: InsightKind
    title: str
    summary: str
    priority: InsightPriority = "P2"
    status: InsightStatus = "new"
    confidence: InsightConfidence = "medium"
    sources: list[WorkbenchSourceRef] = Field(default_factory=list)
    related_people: list[str] = Field(default_factory=list)
    related_projects: list[str] = Field(default_factory=list)
    due_at: str | None = None
    created_at: str


class DailyRadarCoverage(BaseModel):
    chat_messages: int = 0
    calendar_events: int = 0
    tasks: int = 0
    sources: list[str] = Field(default_factory=list)


class DailyRadarSections(BaseModel):
    key_people: list[WorkbenchInsight] = Field(default_factory=list)
    key_tasks: list[WorkbenchInsight] = Field(default_factory=list)
    key_meetings: list[WorkbenchInsight] = Field(default_factory=list)
    risks: list[WorkbenchInsight] = Field(default_factory=list)
    questions: list[WorkbenchInsight] = Field(default_factory=list)


class DailyRadar(BaseModel):
    date: str
    generated_at: str
    coverage: DailyRadarCoverage = Field(default_factory=DailyRadarCoverage)
    highlights: list[WorkbenchInsight] = Field(default_factory=list)
    sections: DailyRadarSections = Field(default_factory=DailyRadarSections)


class WorkbenchRawRecord(BaseModel):
    source_type: str
    source_id: str = ""
    title: str = ""
    summary: str = ""
    due_at: str | None = None
    starts_at: str | None = None
    people: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    created_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkbenchCollectRequest(BaseModel):
    date: str | None = None
    mode: WorkbenchCollectMode = "manual"
    sources: list[WorkbenchCollectSource] | None = None
    chat_keywords: list[str] | None = None
    records: list[WorkbenchRawRecord] = Field(default_factory=list)


class WorkbenchCollectionIssue(BaseModel):
    source: str
    message: str
    recovery_actions: list[str] = Field(default_factory=list)
    code: str | None = None


class WorkbenchAnalyzeRequest(BaseModel):
    date: str | None = None


class WorkbenchInsightActionRequest(BaseModel):
    date: str | None = None


class WorkbenchCollectResponse(BaseModel):
    date: str
    records_added: int
    coverage: DailyRadarCoverage
    collection_errors: dict[str, str] = Field(default_factory=dict)
    collection_diagnostics: dict[str, WorkbenchCollectionIssue] = Field(
        default_factory=dict,
    )


class WorkbenchAnalyzeResponse(BaseModel):
    radar: DailyRadar


class WorkbenchInsightActionResponse(BaseModel):
    insight: WorkbenchInsight
