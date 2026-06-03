# -*- coding: utf-8 -*-
"""Apply product bundles in an idempotent, auditable way."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, ValidationError

from ..agents.skill_system import SkillPoolService
from ..agents.skill_system.hub import import_pool_skill_from_hub
from ..agents.skill_system.store import (
    get_pool_skill_manifest_path,
    read_skill_pool_manifest,
    read_skill_manifest,
)
from ..app.routers.agents import _initialize_agent_workspace
from ..config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    ChannelConfig,
    HeartbeatConfig,
    MCPConfig,
    ModelSlotConfig,
    ToolsConfig,
    load_agent_config,
    save_agent_config,
    validate_agent_id,
)
from ..config.utils import load_config, save_config
from ..constant import WORKING_DIR
from ..providers.provider import ModelInfo, ProviderInfo
from ..providers.provider_manager import ProviderManager
from .settings import get_product_settings, save_product_settings


class BundleSkillHub(BaseModel):
    base_url: HttpUrl | None = None
    label: str = "LuoBotou SkillHub"
    channels: list[str] = Field(default_factory=list)
    search_path: str = "/api/v1/search"
    bundle_url_template: str = "{base_url}/api/v1/skills/{id}"


class BundleModel(BaseModel):
    provider_id: str
    display_name: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    default_model: str | None = None
    scope: Literal["global", "agent"] = "global"
    agent_id: str | None = None
    models: list[ModelInfo] = Field(default_factory=list)


class BundleAgent(BaseModel):
    agent_id: str
    name: str
    description: str = ""
    enabled: bool = True
    language: str = "zh"
    workspace_dir: str | None = None
    active_model: ModelSlotConfig | None = None
    skills: list[str] = Field(default_factory=list)


class BundleSkill(BaseModel):
    id: str
    source: Literal["inline", "skillhub", "pool"] = "pool"
    enabled: bool = True
    name: str | None = None
    bundle_url: str | None = None
    version: str = ""
    content: str | None = None
    references: dict[str, Any] = Field(default_factory=dict)
    scripts: dict[str, Any] = Field(default_factory=dict)
    extra_files: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    agents: list[str] = Field(default_factory=list)


class ProductBundle(BaseModel):
    bundle_id: str
    version: str
    skillhub: BundleSkillHub | None = None
    models: list[BundleModel] = Field(default_factory=list)
    agents: list[BundleAgent] = Field(default_factory=list)
    skills: list[BundleSkill] = Field(default_factory=list)


@dataclass(frozen=True)
class BundleChange:
    action: str
    target: str
    detail: str = ""


@dataclass(frozen=True)
class BundleApplyResult:
    bundle_id: str
    version: str
    fingerprint: str
    dry_run: bool
    changes: list[BundleChange] = field(default_factory=list)
    state_path: Path | None = None

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "dry_run": self.dry_run,
            "changed": self.changed,
            "changes": [
                {
                    "action": change.action,
                    "target": change.target,
                    "detail": change.detail,
                }
                for change in self.changes
            ],
            "state_path": str(self.state_path) if self.state_path else None,
        }


def load_product_bundle(path: str | Path) -> ProductBundle:
    """Load and validate a product bundle manifest."""
    bundle_path = Path(path).expanduser()
    try:
        with open(bundle_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except OSError as exc:
        raise ValueError(f"Cannot read product bundle: {bundle_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON product bundle: {bundle_path}",
        ) from exc
    try:
        return ProductBundle.model_validate(data)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def apply_product_bundle(
    bundle: ProductBundle,
    *,
    dry_run: bool = False,
) -> BundleApplyResult:
    """Apply a product bundle.

    The apply path is intentionally idempotent: every mutation is an upsert or
    a conflict-safe install, and a bundle fingerprint is recorded for audit.
    """
    fingerprint = _bundle_fingerprint(bundle)
    changes: list[BundleChange] = []

    if bundle.skillhub is not None:
        _apply_skillhub(bundle, dry_run=dry_run, changes=changes)

    if bundle.models:
        _apply_models(bundle.models, dry_run=dry_run, changes=changes)

    if bundle.agents:
        _apply_agents(bundle.agents, dry_run=dry_run, changes=changes)

    if bundle.skills:
        asyncio.run(
            _apply_skills(bundle, dry_run=dry_run, changes=changes),
        )

    state_path = (
        _write_bundle_state(bundle, fingerprint) if not dry_run else None
    )
    return BundleApplyResult(
        bundle_id=bundle.bundle_id,
        version=bundle.version,
        fingerprint=fingerprint,
        dry_run=dry_run,
        changes=changes,
        state_path=state_path,
    )


def _bundle_fingerprint(bundle: ProductBundle) -> str:
    payload = bundle.model_dump(mode="json", exclude_none=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(
        "utf-8",
    )
    return hashlib.sha256(raw).hexdigest()


def _write_bundle_state(bundle: ProductBundle, fingerprint: str) -> Path:
    state_dir = WORKING_DIR / "product" / "bundles"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{bundle.bundle_id}.json"
    payload = {
        "bundle_id": bundle.bundle_id,
        "version": bundle.version,
        "fingerprint": fingerprint,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(state_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
    return state_path


def _apply_skillhub(
    bundle: ProductBundle,
    *,
    dry_run: bool,
    changes: list[BundleChange],
) -> None:
    skillhub = bundle.skillhub
    if skillhub is None:
        return
    settings = get_product_settings()
    existing = settings.get("skillhub")
    desired = skillhub.model_dump(mode="json", exclude_none=True)
    if existing == desired:
        return
    changes.append(
        BundleChange(
            "upsert",
            "product.skillhub",
            str(desired.get("base_url", "")),
        ),
    )
    if dry_run:
        return
    settings["skillhub"] = desired
    save_product_settings(settings)


def _apply_models(  # pylint: disable=too-many-branches
    models: list[BundleModel],
    *,
    dry_run: bool,
    changes: list[BundleChange],
) -> None:
    manager = ProviderManager()
    for spec in models:
        provider = manager.get_provider(spec.provider_id)
        if provider is None:
            if not spec.base_url:
                changes.append(
                    BundleChange(
                        "skip",
                        f"models.{spec.provider_id}",
                        "provider missing and base_url not supplied",
                    ),
                )
                continue
            changes.append(
                BundleChange(
                    "create",
                    f"models.{spec.provider_id}",
                    spec.display_name or spec.provider_id,
                ),
            )
            if not dry_run:
                provider_info = asyncio.run(
                    manager.add_custom_provider(
                        ProviderInfo(
                            id=spec.provider_id,
                            name=spec.display_name or spec.provider_id,
                            base_url=spec.base_url,
                            api_key="",
                            api_key_prefix="",
                            extra_models=spec.models,
                        ),
                    ),
                )
                provider = manager.get_provider(provider_info.id)

        if provider is None:
            continue

        if spec.display_name and provider.name != spec.display_name:
            changes.append(
                BundleChange(
                    "update",
                    f"models.{provider.id}.name",
                    spec.display_name,
                ),
            )
            if not dry_run:
                provider.name = spec.display_name

        if (
            spec.base_url
            and getattr(provider, "base_url", "") != spec.base_url
        ):
            changes.append(
                BundleChange(
                    "update",
                    f"models.{provider.id}.base_url",
                    spec.base_url,
                ),
            )
            if not dry_run and not getattr(provider, "freeze_url", False):
                provider.base_url = spec.base_url

        known = {model.id for model in provider.models + provider.extra_models}
        for model in spec.models:
            if model.id in known:
                continue
            changes.append(
                BundleChange(
                    "add",
                    f"models.{provider.id}.{model.id}",
                    model.name,
                ),
            )
            if not dry_run:
                asyncio.run(manager.add_model_to_provider(provider.id, model))
                known.add(model.id)

        if not dry_run:
            manager.save_provider_config(provider.id, provider)

        if spec.default_model:
            _activate_model_if_needed(
                manager,
                spec,
                dry_run=dry_run,
                changes=changes,
            )


def _activate_model_if_needed(
    manager: ProviderManager,
    spec: BundleModel,
    *,
    dry_run: bool,
    changes: list[BundleChange],
) -> None:
    if spec.scope == "global":
        current = manager.get_active_model()
        desired = ModelSlotConfig(
            provider_id=spec.provider_id,
            model=spec.default_model or "",
        )
        if current == desired:
            return
        changes.append(
            BundleChange(
                "activate",
                "models.active.global",
                f"{desired.provider_id}/{desired.model}",
            ),
        )
        if not dry_run:
            asyncio.run(
                manager.activate_model(desired.provider_id, desired.model),
            )
        return

    if not spec.agent_id:
        changes.append(
            BundleChange(
                "skip",
                f"models.{spec.provider_id}.agent",
                "agent scope requires agent_id",
            ),
        )
        return
    try:
        agent_config = load_agent_config(spec.agent_id)
    except Exception:
        changes.append(
            BundleChange(
                "skip",
                f"models.{spec.provider_id}.{spec.agent_id}",
                "agent does not exist yet",
            ),
        )
        return
    desired = ModelSlotConfig(
        provider_id=spec.provider_id,
        model=spec.default_model or "",
    )
    if agent_config.active_model == desired:
        return
    changes.append(
        BundleChange(
            "activate",
            f"models.active.{spec.agent_id}",
            f"{desired.provider_id}/{desired.model}",
        ),
    )
    if not dry_run:
        agent_config.active_model = desired
        save_agent_config(spec.agent_id, agent_config)


def _apply_agents(
    agents: list[BundleAgent],
    *,
    dry_run: bool,
    changes: list[BundleChange],
) -> None:
    config = load_config()
    existing_ids = set(config.agents.profiles.keys())
    save_root_config = False
    for spec in agents:
        if spec.agent_id not in config.agents.profiles:
            validate_agent_id(spec.agent_id, existing_ids)
            workspace_dir = Path(
                spec.workspace_dir
                or f"{WORKING_DIR}/workspaces/{spec.agent_id}",
            ).expanduser()
            changes.append(
                BundleChange("create", f"agents.{spec.agent_id}", spec.name),
            )
            if dry_run:
                continue
            workspace_dir.mkdir(parents=True, exist_ok=True)
            _initialize_agent_workspace(
                workspace_dir,
                skill_names=[],
                language=spec.language,
            )
            agent_config = AgentProfileConfig(
                id=spec.agent_id,
                name=spec.name,
                description=spec.description,
                workspace_dir=str(workspace_dir),
                language=spec.language,
                channels=ChannelConfig(),
                mcp=MCPConfig(),
                heartbeat=HeartbeatConfig(),
                tools=ToolsConfig(),
                active_model=spec.active_model,
            )
            config.agents.profiles[spec.agent_id] = AgentProfileRef(
                id=spec.agent_id,
                workspace_dir=str(workspace_dir),
                enabled=spec.enabled,
            )
            config.agents.agent_order = _normalized_agent_order(config)
            save_config(config)
            save_agent_config(spec.agent_id, agent_config)
            existing_ids.add(spec.agent_id)
            continue

        try:
            agent_config = load_agent_config(spec.agent_id)
        except Exception:
            continue
        changed = False
        if agent_config.name != spec.name:
            changes.append(
                BundleChange(
                    "update",
                    f"agents.{spec.agent_id}.name",
                    spec.name,
                ),
            )
            agent_config.name = spec.name
            changed = True
        if spec.description and agent_config.description != spec.description:
            changes.append(
                BundleChange(
                    "update",
                    f"agents.{spec.agent_id}.description",
                    spec.description,
                ),
            )
            agent_config.description = spec.description
            changed = True
        if (
            spec.active_model
            and agent_config.active_model != spec.active_model
        ):
            active_model_id = (
                f"{spec.active_model.provider_id}/"
                f"{spec.active_model.model}"
            )
            changes.append(
                BundleChange(
                    "activate",
                    f"agents.{spec.agent_id}.active_model",
                    active_model_id,
                ),
            )
            agent_config.active_model = spec.active_model
            changed = True
        ref = config.agents.profiles[spec.agent_id]
        if getattr(ref, "enabled", True) != spec.enabled:
            changes.append(
                BundleChange(
                    "update",
                    f"agents.{spec.agent_id}.enabled",
                    str(spec.enabled),
                ),
            )
            ref.enabled = spec.enabled
            save_root_config = True
        if changed and not dry_run:
            save_agent_config(spec.agent_id, agent_config)

    if save_root_config and not dry_run:
        save_config(config)


async def _apply_skills(
    bundle: ProductBundle,
    *,
    dry_run: bool,
    changes: list[BundleChange],
) -> None:
    skillhub = bundle.skillhub
    pool_service = SkillPoolService()
    bundle_agent_ids = {agent.agent_id for agent in bundle.agents}
    for spec in bundle.skills:
        if spec.source == "inline":
            await _ensure_inline_pool_skill(
                pool_service,
                spec,
                dry_run=dry_run,
                changes=changes,
            )
        elif spec.source == "skillhub":
            await _ensure_hub_pool_skill(
                skillhub,
                spec,
                dry_run=dry_run,
                changes=changes,
            )
        elif spec.source == "pool":
            _ensure_pool_skill_exists(spec, changes=changes)

        agent_ids = spec.agents or [
            agent.agent_id
            for agent in bundle.agents
            if spec.id in agent.skills
            or (spec.name and spec.name in agent.skills)
        ]
        for agent_id in agent_ids:
            _ensure_workspace_skill(
                pool_service,
                spec.name or spec.id,
                agent_id,
                enabled=spec.enabled,
                bundle_agent_ids=bundle_agent_ids,
                dry_run=dry_run,
                changes=changes,
            )


async def _ensure_inline_pool_skill(
    pool_service: SkillPoolService,
    spec: BundleSkill,
    *,
    dry_run: bool,
    changes: list[BundleChange],
) -> None:
    skill_name = spec.name or spec.id
    manifest = read_skill_pool_manifest()
    if skill_name in manifest.get("skills", {}):
        return
    changes.append(
        BundleChange("create", f"skills.pool.{skill_name}", "inline"),
    )
    if dry_run:
        return
    content = spec.content or _default_skill_content(skill_name)
    pool_service.create_skill(
        name=skill_name,
        content=content,
        references=spec.references,
        scripts=spec.scripts,
        extra_files=spec.extra_files,
        config=spec.config,
        installed_from="product-bundle",
    )


async def _ensure_hub_pool_skill(
    skillhub: BundleSkillHub | None,
    spec: BundleSkill,
    *,
    dry_run: bool,
    changes: list[BundleChange],
) -> None:
    skill_name = spec.name or spec.id
    manifest = read_skill_pool_manifest()
    if skill_name in manifest.get("skills", {}):
        return
    bundle_url = spec.bundle_url or _skillhub_bundle_url(skillhub, spec.id)
    changes.append(
        BundleChange("import", f"skills.pool.{skill_name}", bundle_url),
    )
    if dry_run:
        return
    await import_pool_skill_from_hub(
        bundle_url=bundle_url,
        version=spec.version,
        target_name=skill_name,
    )


def _ensure_pool_skill_exists(
    spec: BundleSkill,
    *,
    changes: list[BundleChange],
) -> None:
    skill_name = spec.name or spec.id
    manifest = read_skill_pool_manifest()
    if skill_name in manifest.get("skills", {}):
        return
    changes.append(
        BundleChange(
            "missing",
            f"skills.pool.{skill_name}",
            str(get_pool_skill_manifest_path()),
        ),
    )


def _ensure_workspace_skill(
    pool_service: SkillPoolService,
    skill_name: str,
    agent_id: str,
    *,
    enabled: bool,
    bundle_agent_ids: set[str],
    dry_run: bool,
    changes: list[BundleChange],
) -> None:
    config = load_config()
    ref = config.agents.profiles.get(agent_id)
    if ref is None:
        if dry_run and agent_id in bundle_agent_ids:
            changes.append(
                BundleChange(
                    "install",
                    f"agents.{agent_id}.skills.{skill_name}",
                    "pool",
                ),
            )
            return
        changes.append(
            BundleChange(
                "skip",
                f"agents.{agent_id}.skills.{skill_name}",
                "agent missing",
            ),
        )
        return
    workspace_dir = Path(ref.workspace_dir).expanduser()
    workspace_manifest = read_skill_manifest(workspace_dir)
    entry = workspace_manifest.get("skills", {}).get(skill_name)
    if entry is None:
        changes.append(
            BundleChange(
                "install",
                f"agents.{agent_id}.skills.{skill_name}",
                "pool",
            ),
        )
        if not dry_run:
            pool_service.download_to_workspace(
                skill_name=skill_name,
                workspace_dir=workspace_dir,
                overwrite=False,
            )
            entry = (
                read_skill_manifest(workspace_dir)
                .get("skills", {})
                .get(skill_name)
            )
    if enabled and entry is not None and not bool(entry.get("enabled", False)):
        changes.append(
            BundleChange(
                "enable",
                f"agents.{agent_id}.skills.{skill_name}",
                "",
            ),
        )
        if not dry_run:
            from ..agents.skill_system.workspace_service import SkillService

            SkillService(workspace_dir).enable_skill(skill_name)


def _skillhub_bundle_url(
    skillhub: BundleSkillHub | None,
    skill_id: str,
) -> str:
    if skillhub is None or skillhub.base_url is None:
        raise ValueError(
            "skillhub.base_url or skills[].bundle_url is required for "
            f"skillhub skill '{skill_id}'",
        )
    base_url = str(skillhub.base_url).rstrip("/")
    return skillhub.bundle_url_template.format(
        base_url=base_url,
        id=skill_id,
        slug=skill_id,
    )


def _default_skill_content(skill_name: str) -> str:
    title = skill_name.replace("-", " ").replace("_", " ").title()
    return (
        "---\n"
        f"name: {skill_name}\n"
        f"description: Product bundle placeholder for {title}.\n"
        "---\n\n"
        f"# {title}\n\n"
        "This enterprise skill is managed by the product bundle.\n"
    )


def _normalized_agent_order(config: Any) -> list[str]:
    profile_ids = list(config.agents.profiles.keys())
    ordered_ids: list[str] = []
    for agent_id in config.agents.agent_order:
        if agent_id in config.agents.profiles and agent_id not in ordered_ids:
            ordered_ids.append(agent_id)
    for agent_id in profile_ids:
        if agent_id not in ordered_ids:
            ordered_ids.append(agent_id)
    return ordered_ids
