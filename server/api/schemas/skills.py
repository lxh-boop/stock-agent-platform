from __future__ import annotations

from pydantic import BaseModel, Field


class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=64_000)


class SkillImportGithubRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
