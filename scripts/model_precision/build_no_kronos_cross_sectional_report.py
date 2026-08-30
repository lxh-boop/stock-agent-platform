from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "outputs" / "model_precision"
JSON_PATH = OUTPUT / "no_kronos_cross_sectional_final_report.json"
MARKDOWN_PATH = OUTPUT / "no_kronos_cross_sectional_final_report.md"
DISCLAIMER = "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。"


def _read(name: str) -> dict[str, Any]:
    return json.loads((OUTPUT / name).read_text(encoding="utf-8"))


def _atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    old_master_mse = _read("master_no_kronos_top15_mse.json")
    old_master_bce = _read("master_no_kronos_top15_bce.json")
    old_qlib = _read("qlib_lightgbm_no_kronos_top15.json")
    native = _read("tushare_native_top15.json")
    search = _read("tushare_native_ranker_search.json")
    rolling = _read("tushare_native_rolling_top15.json")
    master = _read("master_tushare_native_top15.json")
    predictions = pd.read_parquet(
        ROOT / "data" / "model_precision" / "tushare_native_ranker_search.parquet"
    )
    base = (
        predictions.groupby(predictions["date"].dt.year)["label_up_tushare"]
        .mean()
        .to_dict()
    )
    best_2025 = search["validation_2025"]
    best_2026 = search["holdout_2026"]
    required_2025 = math.ceil(int(best_2025["signals"]) * 0.55)
    required_2026 = math.ceil(int(best_2026["signals"]) * 0.55)
    attempts = [
        {
            "family": "MASTER on legacy historical universe",
            "variant": "MSE",
            "precision_2025": old_master_mse["validation_2025"]["precision"],
            "precision_2026": old_master_mse["holdout_2026"]["precision"],
        },
        {
            "family": "MASTER on legacy historical universe",
            "variant": "BCE",
            "precision_2025": old_master_bce["validation_2025"]["precision"],
            "precision_2026": old_master_bce["holdout_2026"]["precision"],
        },
        {
            "family": "Qlib-style LightGBM on legacy historical universe",
            "variant": old_qlib["model_search"]["selected"]["name"],
            "precision_2025": old_qlib["validation_2025"]["precision"],
            "precision_2026": old_qlib["holdout_2026"]["precision"],
        },
        {
            "family": "Qlib-style LightGBM on complete Tushare universe",
            "variant": native["model_search"]["selected"]["name"],
            "precision_2025": native["validation_2025"]["precision"],
            "precision_2026": native["holdout_2026"]["precision"],
        },
        {
            "family": "finite parameter/feature search and rank ensemble",
            "variant": " + ".join(search["selected_members"]),
            "precision_2025": best_2025["precision"],
            "precision_2026": best_2026["precision"],
        },
        {
            "family": "quarterly expanding rolling ensemble",
            "variant": " + ".join(rolling["members_fixed_before_rolling_test"]),
            "precision_2025": rolling["validation_2025"]["precision"],
            "precision_2026": rolling["holdout_2026"]["precision"],
        },
        {
            "family": "MASTER on complete Tushare universe",
            "variant": master["selected_loss"],
            "precision_2025": master["validation_2025"]["precision"],
            "precision_2026": master["holdout_2026"]["precision"],
        },
    ]
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accepted": False,
        "objective": "no_kronos_fixed_daily_top15_next_day_up_precision",
        "target_precision": 0.55,
        "selection_count_per_day": 15,
        "abstention_allowed": False,
        "best_model": {
            "members": search["selected_members"],
            "validation_2025": best_2025,
            "holdout_2026": best_2026,
            "required_correct_2025": required_2025,
            "shortfall_correct_2025": required_2025 - int(best_2025["correct"]),
            "required_correct_2026": required_2026,
            "shortfall_correct_2026": required_2026 - int(best_2026["correct"]),
            "research_checkpoints": search["checkpoints"],
        },
        "complete_local_history": {
            **search["data_contract"]["audit"],
            "source": "existing local Tushare caches",
            "download_performed": False,
            "market_base_up_rate_2025": float(base[2025]),
            "market_base_up_rate_2026": float(base[2026]),
        },
        "attempts": attempts,
        "additional_rejected_checks": [
            "single-factor high/low direction audit",
            "causal trailing-performance score-direction switching",
            "hindsight window audit for the online switch (diagnostic only)",
            "MASTER plus LightGBM heterogeneous rank blending",
        ],
        "data_contract": {
            "kronos_model_used": False,
            "kronos_prediction_file_used_by_best_model": False,
            "kronos_market_history_used_by_best_model": False,
            "uses_future_features": False,
        },
        "production_decision": {
            "promoted": False,
            "reason": "No candidate reached 55% on both 2025 validation and 2026 holdout.",
        },
        "conclusion": "The 55% fixed-daily Top15 target was not achieved with the available local data and tested model families.",
        "disclaimer": DISCLAIMER,
    }
    _atomic(JSON_PATH, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    markdown = f"""# 无 Kronos 截面排序实验最终报告

- 目标：每天固定选满 15 只，下一交易日收盘上涨记为正确，逐日平均精确率达到 55%。
- 完整本地数据：{search['data_contract']['audit']['rows']:,} 行、{search['data_contract']['audit']['stocks']} 只股票、{search['data_contract']['audit']['days']} 个交易日，{search['data_contract']['audit']['start_date']} 至 {search['data_contract']['audit']['end_date']}。
- 最佳模型：`{' + '.join(search['selected_members'])}`。
- 2025：{best_2025['precision']:.4%}（{best_2025['correct']}/{best_2025['signals']}），距 55% 少 {required_2025-int(best_2025['correct'])} 次正确预测。
- 2026：{best_2026['precision']:.4%}（{best_2026['correct']}/{best_2026['signals']}），距 55% 少 {required_2026-int(best_2026['correct'])} 次正确预测。
- 结论：未达标，研究模型未接入正式排序链路。

已验证 MASTER、Qlib 风格 LightGBM、分类/回归/排序损失、Top15/30/50 截断、深浅树、轮数、特征消融、同构/异构秩融合、季度滚动和在线方向校准。最佳模型完全使用本地 Tushare 收盘价、估值、资金流、融资融券与指数数据，不读取 Kronos 模型、预测或行情缓存。

{DISCLAIMER}
"""
    _atomic(MARKDOWN_PATH, markdown)
    print(
        json.dumps(
            {"json": str(JSON_PATH), "markdown": str(MARKDOWN_PATH)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
