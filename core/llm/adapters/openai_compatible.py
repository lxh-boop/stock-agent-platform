"""OpenAI-compatible transport adapter with streaming timing telemetry."""

from __future__ import annotations

import time
from typing import Any

from core.llm.adapters.base import LLMAdapter
from core.llm.contracts import (
    LLMConfigurationError,
    LLMProviderError,
    LLMResponse,
    LLMResponseError,
)
from core.llm.profiles import ModelProfile


def _redacted_error(exc: Exception, credential: str) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if credential:
        message = message.replace(credential, "<redacted>")
    return message[:600]


def _round_ms(seconds: float) -> float:
    return round(max(0.0, float(seconds)) * 1000.0, 3)


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _usage_metrics(usage: Any) -> dict[str, int]:
    prompt_tokens = int(_attribute(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(_attribute(usage, "completion_tokens", 0) or 0)
    total_tokens = int(_attribute(usage, "total_tokens", 0) or 0)

    prompt_details = _attribute(usage, "prompt_tokens_details", None)
    completion_details = _attribute(usage, "completion_tokens_details", None)
    if completion_details is None:
        completion_details = _attribute(usage, "output_tokens_details", None)

    cached_prompt_tokens = int(_attribute(prompt_details, "cached_tokens", 0) or 0)
    reasoning_tokens = int(_attribute(completion_details, "reasoning_tokens", 0) or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


def _delta_parts(chunk: Any) -> tuple[str, str]:
    choices = _attribute(chunk, "choices", []) or []
    if not choices:
        return "", ""
    delta = _attribute(choices[0], "delta", None)
    content = str(_attribute(delta, "content", "") or "")
    reasoning = str(
        _attribute(delta, "reasoning_content", "")
        or _attribute(delta, "reasoning", "")
        or ""
    )
    return content, reasoning


def _responses_usage_metrics(usage: Any) -> dict[str, int]:
    input_tokens = int(_attribute(usage, "input_tokens", 0) or 0)
    output_tokens = int(_attribute(usage, "output_tokens", 0) or 0)
    total_tokens = int(_attribute(usage, "total_tokens", 0) or 0)

    input_details = _attribute(usage, "input_tokens_details", None)
    output_details = _attribute(usage, "output_tokens_details", None)
    cached_prompt_tokens = int(_attribute(input_details, "cached_tokens", 0) or 0)
    reasoning_tokens = int(_attribute(output_details, "reasoning_tokens", 0) or 0)
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


def _responses_message_parts(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert Chat Messages to Responses API instructions and input items."""

    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for raw_message in messages:
        role = str(raw_message.get("role") or "user").lower()
        content = str(raw_message.get("content") or "").strip()
        if not content:
            continue
        if role == "system" or role == "developer":
            instructions.append(content)
            continue
        if role == "assistant":
            input_items.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            )
        else:
            input_items.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                }
            )
    return "\n\n".join(instructions), input_items


def _responses_content(response: Any) -> tuple[str, int]:
    output_text = _attribute(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip(), 0

    parts: list[str] = []
    reasoning_chars = 0
    for item in _attribute(response, "output", []) or []:
        item_type = str(_attribute(item, "type", "") or "")
        if item_type == "reasoning":
            reasoning_chars += len(str(_attribute(item, "summary", "") or ""))
            continue
        for content_item in _attribute(item, "content", []) or []:
            content_type = str(_attribute(content_item, "type", "") or "")
            if content_type in {"output_text", "text"}:
                text = str(_attribute(content_item, "text", "") or "")
                if text:
                    parts.append(text)
            elif content_type == "refusal":
                refusal = str(_attribute(content_item, "refusal", "") or "")
                if refusal:
                    raise LLMResponseError(f"模型拒绝回答：{refusal}")
    return "".join(parts).strip(), reasoning_chars


class OpenAICompatibleAdapter(LLMAdapter):
    """Own SDK construction, request assembly and response extraction.

    Timing boundaries are observed through the provider's streaming response:

    - ``queue_network_ms``: request submission until the SDK returns response
      headers/a stream object. It includes client network time and any provider
      queueing before headers are returned.
    - ``input_prefill_ms``: response headers until the first provider delta. It
      is the closest client-observable boundary for prompt prefill; providers do
      not expose a universally reliable standalone prefill timer.
    - ``thinking_output_ms``: first provider delta until stream completion. It
      includes streamed reasoning (when exposed) and final answer generation.

    These measurements are transport observations, not invented estimates.
    """

    def _build_client(self, profile: ModelProfile, credential: str):
        if not credential:
            raise LLMConfigurationError("远程 API 未配置凭据。")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMConfigurationError("当前环境缺少 openai 包，请使用项目虚拟环境安装依赖。") from exc
        kwargs: dict[str, Any] = {
            "api_key": credential,
            "timeout": profile.request_timeout_seconds,
            "max_retries": profile.max_retries,
        }
        if profile.base_url:
            kwargs["base_url"] = profile.base_url
        return OpenAI(**kwargs)

    @staticmethod
    def _prepared_messages(
        profile: ModelProfile,
        messages: list[dict[str, Any]],
        *,
        disable_thinking: bool | None = None,
    ) -> list[dict[str, Any]]:
        copied = [dict(item) for item in messages]
        effective_disable = (
            profile.disable_thinking
            if disable_thinking is None
            else bool(disable_thinking)
        )
        # DashScope/OpenAI-compatible Qwen APIs expose an explicit
        # ``enable_thinking`` request parameter.  Do not spend prompt tokens on
        # ``/no_think`` for remote API calls; the provider parameter below is
        # authoritative.  Keep the prompt marker only as a compatibility
        # fallback for non-API Qwen-compatible runtimes.
        if profile.deployment_mode == "api":
            return copied
        if not (effective_disable and "qwen" in profile.model_name.lower()):
            return copied
        for item in copied:
            if item.get("role") == "system":
                content = str(item.get("content") or "")
                if "/no_think" not in content:
                    item["content"] = f"{content.rstrip()}\n/no_think".strip()
                return copied
        copied.insert(0, {"role": "system", "content": "/no_think"})
        return copied

    @staticmethod
    def _provider_parameters(
        profile: ModelProfile,
        *,
        disable_thinking: bool | None = None,
    ) -> dict[str, Any]:
        effective_disable = (
            profile.disable_thinking
            if disable_thinking is None
            else bool(disable_thinking)
        )
        marker = f"{profile.provider_id} {profile.base_url} {profile.model_name}".lower()
        # Qwen3.7/3.6/3.5 hybrid-thinking models enable thinking by default.
        # The official OpenAI-compatible API requires this non-standard
        # parameter inside ``extra_body``.  Send both True and False explicitly
        # so every stage has deterministic thinking behavior.
        if "qwen" in marker:
            return {"extra_body": {"enable_thinking": not effective_disable}}
        if effective_disable and "deepseek" in marker and "v4" in marker:
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {}

    @staticmethod
    def _stream_parameters(profile: ModelProfile) -> dict[str, Any]:
        # Remote OpenAI-compatible endpoints used by this project expose usage
        # on the terminal stream chunk when include_usage is requested. Ollama
        # may not implement stream_options, so it is intentionally omitted for
        # local mode and usage remains best-effort there.
        if profile.deployment_mode == "api":
            return {"stream": True, "stream_options": {"include_usage": True}}
        return {"stream": True}

    @staticmethod
    def _non_streaming_response(
        response: Any,
        *,
        profile: ModelProfile,
        request_started: float,
        response_received: float,
        client_setup_ms: float,
    ) -> LLMResponse:
        message = response.choices[0].message
        content = str(getattr(message, "content", "") or "").strip()
        if not content:
            if getattr(message, "reasoning_content", None):
                raise LLMResponseError("模型仅返回 reasoning_content；普通 content 为空。")
            raise LLMResponseError("模型返回内容为空。")
        usage = getattr(response, "usage", None)
        duration_ms = _round_ms(response_received - request_started)
        return LLMResponse(
            content=content,
            provider_id=profile.provider_id,
            model_name=profile.model_name,
            profile_id=profile.profile_id,
            config_hash=profile.config_hash,
            usage=_usage_metrics(usage),
            timing={
                "measurement_mode": "aggregate_only",
                "client_setup_ms": client_setup_ms,
                "queue_network_ms": None,
                "input_prefill_ms": None,
                "thinking_output_ms": None,
                "request_to_first_token_ms": None,
                "provider_transport_total_ms": duration_ms,
                "unattributed_provider_ms": duration_ms,
                "first_delta_kind": "unknown",
                "reasoning_chars": len(str(getattr(message, "reasoning_content", "") or "")),
                "content_chars": len(content),
                "timing_note": "Provider ignored streaming; only aggregate request duration is measurable.",
            },
            provider_request_id=str(getattr(response, "id", "") or ""),
        )

    @staticmethod
    def _uses_responses_api(profile: ModelProfile) -> bool:
        """ZAI's GLM-5 family is exposed through the Responses contract."""

        return profile.provider_id.strip().lower() == "zai"

    def _generate_responses(
        self,
        *,
        client: Any,
        profile: ModelProfile,
        messages: list[dict[str, Any]],
        temperature: float,
        max_output_tokens: int,
        request_started: float,
        client_setup_ms: float,
    ) -> LLMResponse:
        instructions, input_items = _responses_message_parts(messages)
        kwargs: dict[str, Any] = {
            "model": profile.model_name,
            "input": input_items,
            "max_output_tokens": max(1, int(max_output_tokens)),
            "temperature": float(temperature),
        }
        if self._uses_responses_api(profile):
            kwargs["reasoning"] = {"effort": "low"}
        if instructions:
            kwargs["instructions"] = instructions

        response = client.responses.create(**kwargs)
        response_received = time.perf_counter()
        content, reasoning_chars = _responses_content(response)
        if not content:
            raise LLMResponseError("模型返回内容为空。")

        duration_ms = _round_ms(response_received - request_started)
        return LLMResponse(
            content=content,
            provider_id=profile.provider_id,
            model_name=profile.model_name,
            profile_id=profile.profile_id,
            config_hash=profile.config_hash,
            usage=_responses_usage_metrics(getattr(response, "usage", None)),
            timing={
                "measurement_mode": "responses_aggregate",
                "client_setup_ms": client_setup_ms,
                "queue_network_ms": None,
                "input_prefill_ms": None,
                "thinking_output_ms": None,
                "request_to_first_token_ms": None,
                "provider_transport_total_ms": duration_ms,
                "unattributed_provider_ms": duration_ms,
                "first_delta_kind": "unknown",
                "reasoning_chars": reasoning_chars,
                "content_chars": len(content),
                "timing_note": "Responses API returned one aggregate response.",
            },
            provider_request_id=str(getattr(response, "id", "") or ""),
        )

    def generate(
        self,
        *,
        profile: ModelProfile,
        credential: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_output_tokens: int,
        disable_thinking: bool | None = None,
    ) -> LLMResponse:
        try:
            client_started = time.perf_counter()
            client = self._build_client(profile, credential)
            client_ready = time.perf_counter()
            client_setup_ms = _round_ms(client_ready - client_started)

            request_started = time.perf_counter()
            if self._uses_responses_api(profile):
                return self._generate_responses(
                    client=client,
                    profile=profile,
                    messages=messages,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    request_started=request_started,
                    client_setup_ms=client_setup_ms,
                )
            response = client.chat.completions.create(
                model=profile.model_name,
                messages=self._prepared_messages(
                    profile, messages, disable_thinking=disable_thinking
                ),
                temperature=float(temperature),
                max_tokens=max(1, int(max_output_tokens)),
                **self._provider_parameters(
                    profile, disable_thinking=disable_thinking
                ),
                **self._stream_parameters(profile),
            )
            headers_received = time.perf_counter()

            # Defensive compatibility: a non-conforming endpoint may ignore
            # stream=True and return a normal completion object.
            if hasattr(response, "choices") and not hasattr(response, "__iter__"):
                return self._non_streaming_response(
                    response,
                    profile=profile,
                    request_started=request_started,
                    response_received=headers_received,
                    client_setup_ms=client_setup_ms,
                )

            content_parts: list[str] = []
            reasoning_chars = 0
            first_delta_at: float | None = None
            first_delta_kind = ""
            usage: Any = None
            provider_request_id = ""

            for chunk in response:
                observed_at = time.perf_counter()
                provider_request_id = provider_request_id or str(_attribute(chunk, "id", "") or "")
                chunk_usage = _attribute(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                content, reasoning = _delta_parts(chunk)
                if content or reasoning:
                    if first_delta_at is None:
                        first_delta_at = observed_at
                        first_delta_kind = "reasoning" if reasoning and not content else "content"
                    if content:
                        content_parts.append(content)
                    reasoning_chars += len(reasoning)

            stream_completed = time.perf_counter()
            content = "".join(content_parts).strip()
            if not content:
                if reasoning_chars:
                    raise LLMResponseError("模型仅返回 reasoning_content；普通 content 为空。")
                raise LLMResponseError("模型返回内容为空。")

            if first_delta_at is None:
                first_delta_at = stream_completed
                first_delta_kind = "none"

            queue_network_ms = _round_ms(headers_received - request_started)
            input_prefill_ms = _round_ms(first_delta_at - headers_received)
            thinking_output_ms = _round_ms(stream_completed - first_delta_at)
            provider_total_ms = _round_ms(stream_completed - request_started)
            return LLMResponse(
                content=content,
                provider_id=profile.provider_id,
                model_name=profile.model_name,
                profile_id=profile.profile_id,
                config_hash=profile.config_hash,
                usage=_usage_metrics(usage),
                timing={
                    "measurement_mode": "stream_observed",
                    "client_setup_ms": client_setup_ms,
                    "queue_network_ms": queue_network_ms,
                    "input_prefill_ms": input_prefill_ms,
                    "thinking_output_ms": thinking_output_ms,
                    "request_to_first_token_ms": _round_ms(first_delta_at - request_started),
                    "provider_transport_total_ms": provider_total_ms,
                    "unattributed_provider_ms": 0.0,
                    "first_delta_kind": first_delta_kind,
                    "reasoning_chars": reasoning_chars,
                    "content_chars": len(content),
                    "thinking_disable_requested": bool(
                        profile.disable_thinking
                        if disable_thinking is None
                        else disable_thinking
                    ),
                    "thinking_disable_effective": not bool(
                        (profile.disable_thinking if disable_thinking is None else disable_thinking)
                        and reasoning_chars
                    ),
                    "timing_note": (
                        "queue_network=request-to-stream-headers; "
                        "input_prefill=headers-to-first-provider-delta; "
                        "thinking_output=first-delta-to-stream-complete"
                    ),
                },
                provider_request_id=provider_request_id,
            )
        except (LLMConfigurationError, LLMResponseError):
            raise
        except Exception as exc:
            raise LLMProviderError(_redacted_error(exc, credential)) from exc


__all__ = ["OpenAICompatibleAdapter"]
