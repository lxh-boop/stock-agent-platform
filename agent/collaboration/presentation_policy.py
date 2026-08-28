from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .request_bundle import RequestBundle, RequestCategory


@dataclass
class PresentationPolicy:
    language: str = ""
    style: str = ""
    length: str = ""
    format: str = ""
    scope: str = "current_turn"
    persist: bool = False
    source_request_ids: list[str] = field(default_factory=list)
    request_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    session_update: dict[str, str] = field(default_factory=dict)
    schema_version: str = "presentation_policy.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "language": self.language,
            "style": self.style,
            "length": self.length,
            "format": self.format,
            "scope": self.scope,
            "persist": bool(self.persist),
            "source_request_ids": list(self.source_request_ids),
            "request_overrides": {
                str(request_id): dict(values)
                for request_id, values in self.request_overrides.items()
            },
            "session_update": dict(self.session_update),
        }

    def for_request(self, request_id: str) -> dict[str, str]:
        """Return the effective presentation fields for one Request.

        Request-scoped explicit fields override the bundle/session derived global
        policy without mutating the global policy itself.
        """
        result = {
            "language": self.language,
            "style": self.style,
            "length": self.length,
            "format": self.format,
        }
        override = self.request_overrides.get(str(request_id or ""), {})
        for name in ("language", "style", "length", "format"):
            value = str(override.get(name) or "").strip()
            if value:
                result[name] = value.lower() if name == "language" else value
        return result


@dataclass
class PresentationValidation:
    valid: bool
    violations: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "presentation_validation.v1",
            "valid": bool(self.valid),
            "violations": list(self.violations),
            "checks": dict(self.checks),
        }


class PresentationPolicyResolver:
    """Resolve one authoritative presentation policy.

    Precedence:
      current explicit request > bundle-global request > session preference >
      user preference (reserved) > system default.
    Within the same scope, later explicit Request wins for the same attribute.
    """

    SESSION_KEY = "session_presentation_preference"

    def resolve(
        self,
        *,
        bundle: RequestBundle,
        session_preference: dict[str, Any] | None,
        system_language: str,
    ) -> PresentationPolicy:
        session = dict(session_preference or {})
        policy = PresentationPolicy(
            language=str(session.get("language") or system_language or "zh").strip().lower(),
            style=str(session.get("style") or "").strip(),
            length=str(session.get("length") or "").strip(),
            format=str(session.get("format") or "").strip(),
            scope="current_turn",
            persist=False,
        )
        requests = [item for item in bundle.requests if item.category == RequestCategory.PRESENTATION]
        request_ids = {item.request_id for item in bundle.requests}
        # Stable source order preserves the user's "later explicit instruction wins" semantics.
        requests.sort(key=lambda item: (int(item.source_index), item.request_id))
        for item in requests:
            p = item.presentation
            if p is None:
                continue
            fields = {
                name: str(getattr(p, name) or "").strip()
                for name in ("language", "style", "length", "format")
            }
            if p.scope == "request":
                # Request-level Presentation must name its target through the
                # Request dependency edge.  It does not mutate the whole-bundle
                # policy.  Later Presentation Requests win field-by-field.
                raw_targets = list(p.request_ids or [])
                targets = [str(target) for target in raw_targets if str(target) in request_ids]
                if not targets:
                    # Deterministic fallback: apply to the nearest preceding
                    # non-presentation Request. This preserves source order when
                    # the semantic decomposer omitted an explicit edge.
                    previous = [
                        candidate for candidate in bundle.requests
                        if candidate.category != RequestCategory.PRESENTATION
                        and int(candidate.source_index) <= int(item.source_index)
                    ]
                    if previous:
                        previous.sort(key=lambda candidate: (int(candidate.source_index), candidate.request_id))
                        targets = [previous[-1].request_id]
                for target in targets:
                    override = policy.request_overrides.setdefault(target, {})
                    for name, value in fields.items():
                        if value:
                            override[name] = value.lower() if name == "language" else value
                policy.source_request_ids.append(item.request_id)
                continue

            # whole_bundle/current_turn/session all contribute to this turn's
            # global final-answer policy. Session-scoped persistent fields are
            # recorded independently so a later current-turn override does not
            # accidentally erase the requested session preference update.
            for name, value in fields.items():
                if not value:
                    continue
                normalized = value.lower() if name == "language" else value
                setattr(policy, name, normalized)
                if p.scope == "session" and bool(p.persist):
                    policy.session_update[name] = normalized
            policy.scope = p.scope or policy.scope
            policy.persist = bool(p.persist)
            policy.source_request_ids.append(item.request_id)
        return policy


class PresentationValidator:
    """Deterministic checks for presentation properties that can be verified."""

    _HAN_RE = re.compile(r"[\u4e00-\u9fff]")
    _LATIN_RE = re.compile(r"[A-Za-z]")
    _BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[\.、)])\s+")

    @staticmethod
    def _length_limit(value: str) -> int | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        match = re.search(r"(\d{2,6})", text)
        if match:
            return int(match.group(1))
        mapping = {"short": 800, "brief": 800, "concise": 800, "简短": 800, "简洁": 800}
        return mapping.get(text)

    def validate(self, answer: str, policy: PresentationPolicy) -> PresentationValidation:
        text = str(answer or "")
        violations: list[str] = []
        checks: dict[str, Any] = {}
        if policy.language == "en":
            han = len(self._HAN_RE.findall(text))
            latin = len(self._LATIN_RE.findall(text))
            checks["language"] = {"expected": "en", "han_chars": han, "latin_chars": latin}
            if han > max(6, latin // 8):
                violations.append("language_not_english")
        elif policy.language == "zh":
            han = len(self._HAN_RE.findall(text))
            latin = len(self._LATIN_RE.findall(text))
            checks["language"] = {"expected": "zh", "han_chars": han, "latin_chars": latin}
            if han == 0 and latin > 40:
                violations.append("language_not_chinese")

        limit = self._length_limit(policy.length)
        if limit is not None:
            checks["length"] = {"max_chars": limit, "actual_chars": len(text)}
            if len(text) > limit:
                violations.append("length_exceeded")

        fmt = str(policy.format or "").strip().lower()
        if fmt in {"bullet", "bullet_list", "bullets", "分点", "列表"}:
            matched = bool(self._BULLET_RE.search(text))
            checks["format"] = {"expected": "bullet_list", "matched": matched}
            if not matched:
                violations.append("format_not_bullet_list")
        elif fmt in {"table", "markdown_table", "表格"}:
            lines = [line for line in text.splitlines() if "|" in line]
            matched = len(lines) >= 2
            checks["format"] = {"expected": "table", "matched": matched}
            if not matched:
                violations.append("format_not_table")

        return PresentationValidation(valid=not violations, violations=violations, checks=checks)


__all__ = [
    "PresentationPolicy",
    "PresentationPolicyResolver",
    "PresentationValidation",
    "PresentationValidator",
]
