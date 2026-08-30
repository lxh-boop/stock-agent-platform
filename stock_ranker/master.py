"""Market-guided cross-sectional Transformer adapted from MASTER.

The architecture is based on the official AAAI 2024 implementation at
https://github.com/SJTU-DMTai/MASTER (MIT license). The data loader and
training/evaluation contract are project-specific and live outside this module.
"""

from __future__ import annotations

import math

import torch
from torch import nn


class PositionalEncoding(nn.Module):
    def __init__(self, dimension: int, max_length: int = 100) -> None:
        super().__init__()
        encoding = torch.zeros(max_length, dimension)
        position = torch.arange(max_length, dtype=torch.float32).unsqueeze(1)
        divisor = torch.exp(
            torch.arange(0, dimension, 2, dtype=torch.float32)
            * (-math.log(10000.0) / dimension)
        )
        encoding[:, 0::2] = torch.sin(position * divisor)
        encoding[:, 1::2] = torch.cos(position * divisor)
        self.register_buffer("encoding", encoding)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return values + self.encoding[: values.shape[1]]


class AttentionBlock(nn.Module):
    def __init__(self, dimension: int, heads: int, dropout: float) -> None:
        super().__init__()
        self.input_norm = nn.LayerNorm(dimension, eps=1e-5)
        self.attention = nn.MultiheadAttention(
            dimension,
            heads,
            dropout=dropout,
            batch_first=True,
        )
        self.output_norm = nn.LayerNorm(dimension, eps=1e-5)
        self.feed_forward = nn.Sequential(
            nn.Linear(dimension, dimension),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(dimension, dimension),
            nn.Dropout(dropout),
        )

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        normalized = self.input_norm(values)
        attended, _ = self.attention(
            normalized,
            normalized,
            normalized,
            need_weights=False,
        )
        residual = values + attended
        normalized_residual = self.output_norm(residual)
        return residual + self.feed_forward(normalized_residual)


class TemporalAggregation(nn.Module):
    def __init__(self, dimension: int) -> None:
        super().__init__()
        self.transform = nn.Linear(dimension, dimension, bias=False)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        hidden = self.transform(values)
        query = hidden[:, -1].unsqueeze(-1)
        weights = torch.softmax(torch.matmul(hidden, query).squeeze(-1), dim=1)
        return torch.matmul(weights.unsqueeze(1), values).squeeze(1)


class MasterRanker(nn.Module):
    """MASTER-style ranker operating on one complete stock cross-section.

    Input shape is ``[stocks, lookback, stock_features + market_features]``.
    The output is one unconstrained ranking score for every input stock.
    """

    def __init__(
        self,
        *,
        stock_feature_count: int,
        market_feature_count: int,
        model_dimension: int = 64,
        temporal_heads: int = 4,
        stock_heads: int = 4,
        temporal_dropout: float = 0.1,
        stock_dropout: float = 0.1,
        gate_temperature: float = 5.0,
        maximum_lookback: int = 100,
    ) -> None:
        super().__init__()
        if stock_feature_count < 1 or market_feature_count < 1:
            raise ValueError("MASTER requires both stock and market features")
        if model_dimension % temporal_heads or model_dimension % stock_heads:
            raise ValueError("model_dimension must be divisible by both head counts")
        self.stock_feature_count = int(stock_feature_count)
        self.market_feature_count = int(market_feature_count)
        self.gate_temperature = float(gate_temperature)
        self.market_gate = nn.Linear(market_feature_count, stock_feature_count)
        self.stock_projection = nn.Linear(stock_feature_count, model_dimension)
        self.position = PositionalEncoding(model_dimension, maximum_lookback)
        self.temporal_attention = AttentionBlock(
            model_dimension, temporal_heads, temporal_dropout
        )
        self.stock_attention = AttentionBlock(
            model_dimension, stock_heads, stock_dropout
        )
        self.temporal_aggregation = TemporalAggregation(model_dimension)
        self.decoder = nn.Linear(model_dimension, 1)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        if values.ndim != 3:
            raise ValueError("Expected [stocks, lookback, features] input")
        expected = self.stock_feature_count + self.market_feature_count
        if values.shape[-1] != expected:
            raise ValueError(f"Expected {expected} features, got {values.shape[-1]}")
        stock_values = values[:, :, : self.stock_feature_count]
        market_now = values[:, -1, self.stock_feature_count :]
        gate = torch.softmax(
            self.market_gate(market_now) / self.gate_temperature,
            dim=-1,
        ) * self.stock_feature_count
        hidden = self.stock_projection(stock_values * gate.unsqueeze(1))
        hidden = self.position(hidden)
        hidden = self.temporal_attention(hidden)
        # Every time step is an independent batch whose sequence dimension is
        # the current stock cross-section.
        hidden = self.stock_attention(hidden.transpose(0, 1)).transpose(0, 1)
        hidden = self.temporal_aggregation(hidden)
        return self.decoder(hidden).squeeze(-1)


__all__ = ["MasterRanker"]
