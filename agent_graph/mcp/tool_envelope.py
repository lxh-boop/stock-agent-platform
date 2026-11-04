from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MCPToolEnvelope(BaseModel):
    """Transport structure only; business semantics live in Tool contracts."""

    model_config = ConfigDict(extra="allow")

    success: bool
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MCPToolEnvelope":
        return cls.model_validate(dict(payload or {}))


__all__ = ["MCPToolEnvelope"]
