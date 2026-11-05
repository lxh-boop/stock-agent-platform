"""LangChain ChatModel factory bound to the project's LLM runtime settings.

Replaces the self-built ``core/llm`` adapter stack (``LLMService`` /
``OpenAICompatibleAdapter``) with ``langchain-openai.ChatOpenAI``. Every provider
in use (DeepSeek, Qwen/DashScope, ZAI GLM, local Ollama) speaks the
OpenAI-compatible chat protocol, so a single factory covers api and local modes.
Provider quirks from the legacy adapter are preserved:

- Qwen: ``extra_body.enable_thinking`` mirrors the legacy ``disable_thinking`` flag.
- DeepSeek v4: ``extra_body.thinking`` disabled when ``disable_thinking`` is set.
- ZAI (provider_id ``zai``): Responses API (``use_responses_api=True``).
"""

from __future__ import annotations

from typing import TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from core.llm.runtime_settings import LLMRuntimeSettings, resolve_active_llm_settings

_TModel = TypeVar("_TModel", bound=BaseModel)


def _provider_marker(settings: LLMRuntimeSettings) -> str:
    return f"{settings.base_url} {settings.model}".lower()


def _provider_extra_body(settings: LLMRuntimeSettings) -> dict[str, object] | None:
    """Mirror the legacy adapter's provider-specific request bodies."""

    marker = _provider_marker(settings)
    if "qwen" in marker:
        return {"enable_thinking": not settings.disable_thinking}
    if settings.disable_thinking and "deepseek" in marker and "v4" in marker:
        return {"thinking": {"type": "disabled"}}
    return None


def build_chat_model(
    settings: LLMRuntimeSettings | None = None,
    *,
    temperature: float = 0.0,
    **kwargs: object,
) -> ChatOpenAI:
    """Build one ``ChatOpenAI`` from resolved runtime settings (one run, one binding)."""

    resolved = settings or resolve_active_llm_settings()
    extra_body = _provider_extra_body(resolved)
    use_responses_api = str(resolved.provider).strip().lower() == "zai"
    return ChatOpenAI(
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        model=resolved.model,
        temperature=temperature,
        timeout=resolved.request_timeout_seconds,
        max_retries=resolved.max_retries,
        extra_body=extra_body,
        use_responses_api=use_responses_api,
        **kwargs,
    )


