"""Exact adapter selection with no provider fallback."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from core.llm.adapters import LLMAdapter, OllamaAdapter, OpenAICompatibleAdapter
from core.llm.contracts import LLMConfigurationError
from core.llm.profiles import ModelProfile


class AdapterRegistry:
    def __init__(self, adapters: Mapping[str, LLMAdapter] | None = None) -> None:
        openai_compatible = OpenAICompatibleAdapter()
        configured = dict(adapters or {
            "openai_compatible": openai_compatible,
            "openai-compatible": openai_compatible,
            "deepseek": openai_compatible,
            "zai": openai_compatible,
            "zhipu": openai_compatible,
            "glm": openai_compatible,
            "ollama": OllamaAdapter(),
            "ollama_local": OllamaAdapter(),
        })
        self._adapters = MappingProxyType({str(key).lower(): value for key, value in configured.items()})

    def adapter_for(self, profile: ModelProfile) -> LLMAdapter:
        adapter = self._adapters.get(profile.provider_id.lower())
        if adapter is None:
            raise LLMConfigurationError(f"未注册的 LLM Provider：{profile.provider_id}")
        return adapter

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


__all__ = ["AdapterRegistry"]
