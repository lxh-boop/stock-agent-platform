from uuid import UUID

from pydantic import Field

from backtesting.contracts import BacktestSpec, StrictModel
from typing import Literal


class StrategyMutation(StrictModel):
    request_id: UUID
    confirmed: Literal[True]


class StrategyFromReport(StrategyMutation):
    run_id: UUID
    name: str | None = Field(default=None, min_length=1, max_length=80)


class StrategySave(StrategyMutation):
    spec: BacktestSpec
    expected_version: int | None = Field(default=None, ge=1, strict=True)


class StrategyRerun(StrategyMutation):
    expected_version: int = Field(ge=1, strict=True)


class StrategyDelete(StrategyMutation):
    expected_version: int = Field(ge=1, strict=True)

