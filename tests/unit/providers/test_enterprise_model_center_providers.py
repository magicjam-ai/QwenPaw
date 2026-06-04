# -*- coding: utf-8 -*-
"""Enterprise model-center built-in providers."""

from qwenpaw.providers.openai_provider import OpenAIProvider
from qwenpaw.providers.provider import ModelInfo
from qwenpaw.providers.provider_manager import (
    PROVIDER_CLI_PROXY,
    PROVIDER_MODELSIGHT,
    PROVIDER_TRAE_CN,
    ProviderManager,
)


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


def test_enterprise_model_center_providers_registered(
    isolated_secret_dir,
) -> None:
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


async def test_can_configure_add_model_and_activate_enterprise_provider(
    isolated_secret_dir,
) -> None:
    """Configured company providers should become real callable active slots."""
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
