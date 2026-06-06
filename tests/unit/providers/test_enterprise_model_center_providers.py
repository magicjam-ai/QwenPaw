# -*- coding: utf-8 -*-
"""Enterprise model-center built-in providers."""

import pytest

from qwenpaw.providers.openai_provider import OpenAIProvider
from qwenpaw.providers.provider import ModelInfo
from qwenpaw.providers.provider_manager import (
    PROVIDER_CLI_PROXY,
    PROVIDER_MODELSIGHT,
    PROVIDER_TRAE_CN,
    ProviderManager,
)
from qwenpaw.app.routers import providers as providers_router
from qwenpaw.app.routers.providers import (
    CliCommandStatus,
    CliProxyStatus,
    ConfigureTraeCliRequest,
    TraeCliStatusResponse,
    configure_trae_cli_provider,
    configure_provider,
    list_all_providers,
    ProviderConfigRequest,
)

pytestmark = pytest.mark.usefixtures("isolated_secret_dir")


def test_enterprise_model_center_providers_are_openai_compatible() -> None:
    """Company/CLI model sources should use the existing callable stack."""
    for provider in (
        PROVIDER_MODELSIGHT,
        PROVIDER_TRAE_CN,
        PROVIDER_CLI_PROXY,
    ):
        assert isinstance(provider, OpenAIProvider)
        assert provider.freeze_url is False
        assert provider.require_api_key is False
        assert provider.support_model_discovery is True
        assert provider.support_connection_check is True


def test_enterprise_model_center_providers_registered() -> None:
    """ProviderManager should expose the new model-center sources."""
    manager = ProviderManager()

    expected = {
        "modelsight": "company_private",
        "trae-cn": "company_commercial",
        "cli-proxy": "cli_proxy",
    }
    for provider_id, source_type in expected.items():
        provider = manager.get_provider(provider_id)
        assert provider is not None
        assert isinstance(provider, OpenAIProvider)
        assert provider.meta["source_type"] == source_type


async def test_can_activate_enterprise_provider() -> None:
    """Configured company providers should become callable active slots."""
    manager = ProviderManager()

    assert manager.update_provider(
        "modelsight",
        {
            "base_url": "http://modelsight.internal/v1",
            "api_key": "company-token",
        },
    )

    await manager.add_model_to_provider(
        "modelsight",
        ModelInfo(id="modelsight-chat", name="ModelSight Chat"),
    )
    await manager.activate_model("modelsight", "modelsight-chat")

    active = manager.get_active_model()
    assert active is not None
    assert active.provider_id == "modelsight"
    assert active.model == "modelsight-chat"

    reloaded = ProviderManager()
    provider = reloaded.get_provider("modelsight")
    assert provider is not None
    assert provider.base_url == "http://modelsight.internal/v1"
    assert provider.has_model("modelsight-chat")
    assert reloaded.get_active_model().provider_id == "modelsight"


async def test_provider_config_api_can_update_display_name() -> None:
    manager = ProviderManager()

    updated = await configure_provider(
        manager=manager,
        provider_id="cli-proxy",
        body=ProviderConfigRequest(
            name="9Router",
            base_url="http://127.0.0.1:20128/v1",
        ),
    )

    assert updated.name == "9Router"
    assert manager.get_provider("cli-proxy").name == "9Router"


async def test_list_providers_annotates_trae_cli_status(
    monkeypatch,
) -> None:
    async def fake_status():
        return TraeCliStatusResponse(
            cli=CliCommandStatus(
                installed=True,
                command="traecli",
                path="/usr/bin/traecli",
                version="traecli 1.0.0",
            ),
            proxies=[
                CliProxyStatus(
                    base_url="http://127.0.0.1:20128/v1",
                    healthy=True,
                ),
            ],
            selected_base_url="http://127.0.0.1:20128/v1",
        )

    monkeypatch.setattr(providers_router, "_trae_cli_status", fake_status)

    providers = await list_all_providers(manager=ProviderManager())
    trae = next(provider for provider in providers if provider.id == "trae-cn")

    assert trae.meta["cli_status"]["cli"]["installed"] is True
    assert trae.meta["detected_base_url"] == "http://127.0.0.1:20128/v1"


async def test_configure_trae_cli_provider_registers_openai_proxy(
    monkeypatch,
) -> None:
    async def fake_status():
        return TraeCliStatusResponse(
            cli=CliCommandStatus(
                installed=True,
                command="traecli",
                path="/usr/bin/traecli",
            ),
            proxies=[],
            selected_base_url="http://127.0.0.1:20128/v1",
        )

    monkeypatch.setattr(providers_router, "_trae_cli_status", fake_status)
    manager = ProviderManager()

    provider = await configure_trae_cli_provider(
        manager=manager,
        body=ConfigureTraeCliRequest(model_id="trae-enterprise"),
    )

    assert provider.id == "trae-cn"
    assert provider.name == "Trae 企业版"
    assert provider.base_url == "http://127.0.0.1:20128/v1"
    assert provider.extra_models[0].id == "trae-enterprise"
