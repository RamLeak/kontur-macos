"""Provider presets and re-exports for settings/UI routing.

OpenCode's official provider catalog is intentionally not copied into the
application: it changes independently and is selected by provider/model IDs.
The registry accepts any validated OpenCode provider ID, plus the common local
presets below.  No provider credentials are discovered from the host.
"""

from __future__ import annotations

from offline_ai import (
    PRESET_BY_ID,
    PROVIDER_PRESETS,
    ProviderConfig,
    ProviderPreset,
    ProviderRegistry,
    validate_provider_config,
)

# Common API-key provider IDs documented by OpenCode.  This is display
# metadata only; the OpenCode CLI remains the authority for the live catalog.
# Custom IDs are supported by ProviderConfig without changing this list.
OPENCODE_COMMON_PROVIDER_IDS: tuple[str, ...] = (
    "openai",
    "anthropic",
    "google",
    "deepseek",
    "openrouter",
    "xai",
    "groq",
    "mistral",
    "cohere",
    "cerebras",
    "fireworks-ai",
    "togetherai",
    "ollama-cloud",
    "zai",
    "zenmux",
)


def opencode_provider_options() -> tuple[dict[str, str], ...]:
    """Return non-secret labels for a convenient provider picker."""

    return tuple({"id": provider_id, "label": provider_id} for provider_id in OPENCODE_COMMON_PROVIDER_IDS)


__all__ = [
    "OPENCODE_COMMON_PROVIDER_IDS",
    "PRESET_BY_ID",
    "PROVIDER_PRESETS",
    "ProviderConfig",
    "ProviderPreset",
    "ProviderRegistry",
    "opencode_provider_options",
    "validate_provider_config",
]
