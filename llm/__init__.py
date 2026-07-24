"""Language-model backend abstractions and lazy client construction."""

from __future__ import annotations

from config import Settings
from llm.base import LLMClient


def get_client(settings: Settings) -> LLMClient:
    """Construct only the backend adapter selected by immutable settings."""

    if settings.backend == "agent_sdk":
        from llm.agent_sdk_client import AgentSDKClient

        return AgentSDKClient(settings)
    if settings.backend == "anthropic_api":
        from llm.anthropic_api_client import AnthropicAPIClient

        return AnthropicAPIClient(settings)
    raise ValueError(
        f"Unsupported AutoCover backend {settings.backend!r}. "
        "Use 'agent_sdk' or 'anthropic_api'."
    )


__all__ = ["LLMClient", "get_client"]
