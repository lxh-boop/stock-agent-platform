"""Unified, profile-bound LLM service."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import local
from typing import Any, Callable

from core.llm.contracts import LLMConfigurationError, LLMJSONError, LLMResponse, extract_json_object
from core.llm.registry import AdapterRegistry
from core.llm.runtime_settings import LLMRuntimeSettings


@dataclass
class _ServiceState:
    thread_local: local = field(default_factory=local)


@dataclass(frozen=True)
class LLMService:
    """One immutable model binding reused for every stage of an Agent run."""

    settings: LLMRuntimeSettings
    registry: AdapterRegistry = field(default_factory=AdapterRegistry, repr=False, compare=False)
    _state: _ServiceState = field(default_factory=_ServiceState, repr=False, compare=False)

    @property
    def profile(self):
        return self.settings.profile

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id

    @property
    def config_hash(self) -> str:
        return self.profile.config_hash

    @property
    def is_available(self) -> bool:
        return self.settings.is_configured

    @property
    def last_response(self) -> LLMResponse | None:
        return getattr(self._state.thread_local, "last_response", None)

    @property
    def last_usage(self) -> dict[str, int]:
        response = self.last_response
        return dict(response.usage) if response is not None else {}

    @property
    def last_timing(self) -> dict[str, Any]:
        response = self.last_response
        return dict(response.timing) if response is not None else {}

    @property
    def last_audit_event_id(self) -> str:
        return str(getattr(self._state.thread_local, "last_audit_event_id", "") or "")

    def _set_response(self, response: LLMResponse | None, event_id: str) -> None:
        self._state.thread_local.last_response = response
        self._state.thread_local.last_audit_event_id = event_id

    def _record_call(self, **_kwargs: Any) -> str:
        """Legacy LLM audit hook removed with the self-built runtime."""
        return ""

    @staticmethod
    def _record_schema(_event_id: str, _valid: bool) -> None:
        return None

    @staticmethod
    def _emit_generation_event(
        callback: Callable[[str, dict[str, Any]], None] | None,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        """Emit a best-effort JSON-generation lifecycle event.

        The callback is deliberately optional and isolated from model execution:
        observability failures must never change the LLM result or retry policy.
        """
        if callback is None:
            return
        try:
            callback(str(event), dict(payload))
        except Exception:
            return

    @staticmethod
    def _validation_error_context(error: Exception) -> dict[str, Any]:
        """Extract a machine-readable contract error from an exception chain."""

        chain: list[Exception] = []
        current: BaseException | None = error
        seen: set[int] = set()
        while isinstance(current, Exception) and id(current) not in seen:
            seen.add(id(current))
            chain.append(current)
            current = current.__cause__ or current.__context__

        contract_error = next(
            (
                item
                for item in chain
                if str(getattr(item, "code", "") or "").strip()
            ),
            None,
        )
        selected = contract_error or error
        return {
            "error_type": type(error).__name__,
            "message": str(error)[:4000],
            "contract_code": str(getattr(selected, "code", "") or ""),
            "path": str(getattr(selected, "path", "") or ""),
            "detail": str(getattr(selected, "detail", "") or "")[:4000],
            "exception_chain": [
                {
                    "type": type(item).__name__,
                    "message": str(item)[:2000],
                }
                for item in chain[:6]
            ],
        }

    @staticmethod
    def _repair_candidate_payload(
        candidate: dict[str, Any] | None,
        raw_output: str,
    ) -> dict[str, Any] | str:
        """Keep the complete parsed candidate when possible for targeted repair."""

        if isinstance(candidate, dict):
            return candidate
        return str(raw_output or "")[:24000]

    def generate_text(
        self,
        *,
        stage: str,
        messages: list[dict[str, Any]],
        max_output_tokens: int,
        temperature: float = 0.2,
        operation: str = "",
        disable_thinking: bool | None = None,
    ) -> str:
        if not self.is_available:
            raise LLMConfigurationError("当前 Model Profile 未配置可用凭据或模型。")
        request_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        started = time.perf_counter()
        prompt_chars = sum(
            len(str(item.get("content") or ""))
            for item in messages
            if isinstance(item, dict)
        )
        prompt_dump = None
        try:
            from core.llm.prompt_dump import start_prompt_dump

            prompt_dump = start_prompt_dump(
                stage=stage,
                operation=operation or "primary",
                profile=self.profile,
                messages=[dict(item) for item in messages],
                temperature=float(temperature),
                max_output_tokens=max(1, int(max_output_tokens)),
            )
        except Exception:
            prompt_dump = None
        try:
            response = self.registry.adapter_for(self.profile).generate(
                profile=self.profile,
                credential=self.settings.credential,
                messages=[dict(item) for item in messages],
                temperature=float(temperature),
                max_output_tokens=max(1, int(max_output_tokens)),
                disable_thinking=disable_thinking,
            )
        except Exception as exc:
            try:
                from core.llm.prompt_dump import finish_prompt_dump

                finish_prompt_dump(prompt_dump, error=exc)
            except Exception:
                pass
            event_id = self._record_call(
                stage=stage,
                operation=operation or "primary",
                temperature=temperature,
                request_at=request_at,
                started=started,
                success=False,
                error=exc,
                prompt_chars=prompt_chars,
                max_output_tokens=max_output_tokens,
                disable_thinking=disable_thinking,
            )
            self._set_response(None, event_id)
            mode_label = "本地 Ollama" if self.profile.deployment_mode == "local" else "远程 API"
            raise type(exc)(
                f"{mode_label}调用失败：{exc}。当前配置禁止自动切换模型，本次未执行任何自动回退。"
            ) from exc
        try:
            from core.llm.prompt_dump import finish_prompt_dump

            finish_prompt_dump(prompt_dump, response=response)
        except Exception:
            pass
        event_id = self._record_call(
            stage=stage,
            operation=operation or "primary",
            temperature=temperature,
            request_at=request_at,
            started=started,
            success=True,
            response=response,
            prompt_chars=prompt_chars,
            max_output_tokens=max_output_tokens,
            disable_thinking=disable_thinking,
        )
        self._set_response(response, event_id)
        return response.content

    def generate_json(
        self,
        *,
        stage: str,
        messages: list[dict[str, Any]],
        max_output_tokens: int,
        validator: Callable[[dict[str, Any]], None] | None = None,
        operation: str = "",
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        repair_guidance: str = "",
        repair_mode: str = "regenerate",
        repair_context_builder: Callable[
            [dict[str, Any] | None, dict[str, Any]], list[dict[str, Any]]
        ] | None = None,
        disable_thinking: bool | None = None,
    ) -> dict[str, Any]:
        """Generate JSON and perform exactly one full-plan repair request.

        ``event_callback`` exposes only bounded lifecycle diagnostics.
        ``repair_guidance`` adds caller-owned contract guidance to the single
        existing repair attempt; it does not alter timeout, retry count, model
        binding, or validation policy. ``repair_mode="targeted"`` preserves the
        parsed candidate and asks the model to repair only the contract fields
        identified by the validator while still returning a complete JSON object.
        ``repair_context_builder`` may replace the repeated primary prompt with a
        caller-owned compact repair context; the invalid candidate and exact
        validation error are still appended by this method.
        """

        effective_operation = operation or "primary"
        normalized_repair_mode = str(repair_mode or "regenerate").strip().lower()
        if normalized_repair_mode not in {"regenerate", "targeted"}:
            raise ValueError(f"unsupported_repair_mode:{normalized_repair_mode}")
        diagnostics: dict[str, Any] = {
            "stage": stage,
            "operation": effective_operation,
            "primary": {},
            "repair": {},
        }
        self._emit_generation_event(
            event_callback,
            "request_started",
            {
                "stage": stage,
                "operation": effective_operation,
                "attempt": "primary",
                "prompt_chars": sum(
                    len(str(item.get("content") or ""))
                    for item in messages
                    if isinstance(item, dict)
                ),
                "message_count": len(messages),
                "max_output_tokens": max_output_tokens,
                "thinking_disabled": (
                    bool(getattr(self.profile, "disable_thinking", False))
                    if disable_thinking is None
                    else bool(disable_thinking)
                ),
            },
        )
        output = self.generate_text(
            stage=stage,
            messages=messages,
            max_output_tokens=max_output_tokens,
            temperature=0.0,
            operation=effective_operation,
            disable_thinking=disable_thinking,
        )
        first_event_id = self.last_audit_event_id
        diagnostics["primary"]["audit_event_id"] = first_event_id
        diagnostics["primary"]["raw_output_excerpt"] = str(output or "")[:6000]
        self._emit_generation_event(
            event_callback,
            "response_received",
            {
                "stage": stage,
                "operation": effective_operation,
                "attempt": "primary",
                "response_chars": len(str(output or "")),
                "usage": self.last_usage,
                "timing": self.last_timing,
                "audit_event_id": first_event_id,
            },
        )

        first_candidate: dict[str, Any] | None = None
        try:
            first_candidate = extract_json_object(output)
            diagnostics["primary"]["candidate"] = first_candidate
            self._emit_generation_event(
                event_callback,
                "candidate_generated",
                {
                    "stage": stage,
                    "attempt": "primary",
                    "candidate": first_candidate,
                },
            )
            if validator:
                validator(first_candidate)
            self._record_schema(first_event_id, True)
            self._emit_generation_event(
                event_callback,
                "validation_succeeded",
                {
                    "stage": stage,
                    "attempt": "primary",
                    "candidate": first_candidate,
                },
            )
            return first_candidate
        except Exception as first_exc:
            self._record_schema(first_event_id, False)
            error_context = self._validation_error_context(first_exc)
            diagnostics["primary"]["error_type"] = type(first_exc).__name__
            diagnostics["primary"]["error_message"] = str(first_exc)[:2000]
            diagnostics["primary"]["error_context"] = error_context
            self._emit_generation_event(
                event_callback,
                "validation_failed",
                {
                    "stage": stage,
                    "attempt": "primary",
                    "error_type": type(first_exc).__name__,
                    "error_message": str(first_exc)[:2000],
                    "error_context": error_context,
                    "candidate": first_candidate,
                },
            )

            targeted = normalized_repair_mode == "targeted"
            repair_instruction = (
                "保留原用户目标、仍然合法的任务和合法依赖，只修改校验错误指出的字段及其必然受影响字段。"
                if targeted
                else "丢弃不合法结构并重新生成满足相同用户目标的完整结果。"
            )
            repair_request = {
                "repair_mode": (
                    "targeted_complete_json" if targeted else "regenerate_complete_json"
                ),
                "instruction": repair_instruction,
                "validation_error": error_context,
                "invalid_candidate": self._repair_candidate_payload(
                    first_candidate, output
                ),
                "caller_repair_guidance": str(repair_guidance or "")[:6000],
                "output_requirements": [
                    "Return one complete independently valid JSON object.",
                    "Preserve the original user goal and side-effect boundary.",
                    "Do not output Markdown or explanatory prose.",
                    "Do not invent missing facts, entities, constraints, or dependencies.",
                ],
            }
            if repair_context_builder is not None:
                base_repair_messages = [
                    dict(item)
                    for item in repair_context_builder(first_candidate, error_context)
                    if isinstance(item, dict)
                ]
                if not base_repair_messages:
                    base_repair_messages = [dict(item) for item in messages]
            else:
                base_repair_messages = [dict(item) for item in messages]
            repair_messages = [
                *base_repair_messages,
                {
                    "role": "user",
                    "content": json.dumps(
                        repair_request,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]
            self._emit_generation_event(
                event_callback,
                "repair_started",
                {
                    "stage": stage,
                    "operation": "schema_repair",
                    "attempt": "repair",
                    "repair_mode": normalized_repair_mode,
                    "previous_error_type": type(first_exc).__name__,
                    "previous_error_message": str(first_exc)[:2000],
                    "error_context": error_context,
                    "prompt_chars": sum(
                        len(str(item.get("content") or ""))
                        for item in repair_messages
                        if isinstance(item, dict)
                    ),
                    "max_output_tokens": max_output_tokens,
                    "thinking_disabled": (
                        bool(getattr(self.profile, "disable_thinking", False))
                        if disable_thinking is None
                        else bool(disable_thinking)
                    ),
                },
            )
            repaired = self.generate_text(
                stage=stage,
                messages=repair_messages,
                max_output_tokens=max_output_tokens,
                temperature=0.0,
                operation="schema_repair",
                disable_thinking=disable_thinking,
            )
            repair_event_id = self.last_audit_event_id
            diagnostics["repair"]["audit_event_id"] = repair_event_id
            diagnostics["repair"]["raw_output_excerpt"] = str(repaired or "")[:6000]
            self._emit_generation_event(
                event_callback,
                "repair_response_received",
                {
                    "stage": stage,
                    "attempt": "repair",
                    "response_chars": len(str(repaired or "")),
                    "usage": self.last_usage,
                    "timing": self.last_timing,
                    "audit_event_id": repair_event_id,
                },
            )

            repair_candidate: dict[str, Any] | None = None
            try:
                repair_candidate = extract_json_object(repaired)
                diagnostics["repair"]["candidate"] = repair_candidate
                self._emit_generation_event(
                    event_callback,
                    "repair_candidate_generated",
                    {
                        "stage": stage,
                        "attempt": "repair",
                        "candidate": repair_candidate,
                    },
                )
                if validator:
                    validator(repair_candidate)
                self._record_schema(repair_event_id, True)
                self._emit_generation_event(
                    event_callback,
                    "repair_validation_succeeded",
                    {
                        "stage": stage,
                        "attempt": "repair",
                        "candidate": repair_candidate,
                    },
                )
                return repair_candidate
            except Exception as second_exc:
                self._record_schema(repair_event_id, False)
                diagnostics["repair"]["error_type"] = type(second_exc).__name__
                diagnostics["repair"]["error_message"] = str(second_exc)[:2000]
                self._emit_generation_event(
                    event_callback,
                    "repair_failed",
                    {
                        "stage": stage,
                        "attempt": "repair",
                        "error_type": type(second_exc).__name__,
                        "error_message": str(second_exc)[:2000],
                        "candidate": repair_candidate,
                    },
                )
                error = LLMJSONError(
                    f"LLM JSON/schema repair failed: {type(first_exc).__name__}; "
                    f"{type(second_exc).__name__}: {second_exc}"
                )
                setattr(error, "diagnostics", diagnostics)
                raise error from second_exc

    def validate_connection(self) -> tuple[bool, str]:
        try:
            validation_output_budget = (
                25600 if self.profile.deployment_mode == "local" else 2000
            )
            self.generate_text(
                stage="completion",
                messages=[
                    {"role": "system", "content": "只回复 OK。"},
                    {"role": "user", "content": "请回复 OK，用于连接测试。"},
                ],
                temperature=0.0,
                max_output_tokens=validation_output_budget,
                operation="connection_validation",
            )
            label = "本地 Ollama" if self.profile.deployment_mode == "local" else "远程 API"
            return True, f"{label}连接成功，当前模型：{self.profile.model_name}"
        except Exception as exc:
            label = "本地 Ollama" if self.profile.deployment_mode == "local" else "远程 API"
            return False, f"{label}调用失败：{exc}"


__all__ = ["LLMService"]
