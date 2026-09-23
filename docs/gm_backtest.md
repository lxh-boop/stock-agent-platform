# 使用本地模型结果 + 掘金数据源做每日 10 点回测

这个集成方式把系统拆成两层：

```text
本地模型结果: model_prediction / ranking CSV
        ↓ export_gm_targets.py
目标权重表: signal_date, symbol, target_weight
        ↓ strategies/gm_daily_1000.py
掘金回测: A股交易日历、历史行情、10 点撮合、手续费、滑点、绩效
```

不要在掘金策略循环里重新加载/训练你的模型。应该每天收盘后先生成次日信号，回测时掘金只读取已经固化的目标权重表。

## 1. 安装和准备

掘金回测依赖 `gm` SDK 和掘金量化终端：

```powershell
pip install gm
```

你需要在掘金终端中创建一个 Python 回测策略，并准备：

- `strategy_id`
- 掘金 `token`
- 回测区间

掘金 SDK 对 Python 版本有约束。建议优先使用掘金终端指定的 Python 环境；不要为了回测强行把 `gm` 依赖塞进当前项目的 Python 3.12 FastAPI 镜像。

注意：`gm` 是回测/行情/交易 SDK；`gmtrade` 主要用于仿真/实盘交易，不用于这里说的回测。

## 2. 导出模型目标持仓

如果你的模型结果是本项目常见的 ranking CSV：

```powershell
cd D:\cht\agent\stock-agent-platform
python tools\export_gm_targets.py `
  --input outputs\ranking_history.csv `
  --top-k 10 `
  --cash-ratio 0.98 `
  --output strategies\gm_daily_1000_targets.csv
```

如果要从 PostgreSQL 的 `model_prediction` 表读取：

```powershell
python tools\export_gm_targets.py `
  --from-db `
  --start-date 2024-01-01 `
  --end-date 2026-01-01 `
  --top-k 10 `
  --output strategies\gm_daily_1000_targets.csv
```

导出的 CSV核心字段是：

```csv
signal_date,symbol,rank,score,target_weight
2026-09-18,SHSE.600519,1,0.931,0.098
2026-09-18,SZSE.000858,2,0.910,0.098
```

`signal_date` 是模型使用的信号日。策略在执行日只会选择 `signal_date < 执行日` 的最新一组信号，避免未来函数。如果模型结果里有 `prediction_for_date`，导出后会作为 `execution_hint_date`；策略会在该执行日优先使用这组信号，但仍要求 `signal_date < execution_hint_date`。

## 3. 运行掘金回测

`strategies/gm_daily_1000.py` 会在每个 A 股交易日 `10:00:00` 调仓：

1. 读取最近一个已经完成的信号日。
2. 先卖出不在目标组合里的旧持仓。
3. 再买入/调整目标组合到 `target_weight`。
4. 由掘金完成行情撮合、费用、滑点和绩效统计。

如果目标 CSV 已生成到 `strategies/gm_daily_1000_targets.csv`，策略会自动读取它。
也可以不复制文件，改用绝对路径：

```powershell
$env:GM_TARGETS_PATH="D:\cht\agent\stock-agent-platform\outputs\gm\gm_daily_1000_targets.csv"
```

回测入口已经写在 `strategies/gm_daily_1000.py` 的 `__main__` 里，可以用环境变量启动：

```powershell
$env:GM_STRATEGY_ID="your_gm_strategy_id"
$env:GM_TOKEN="your_gm_token"
$env:GM_BACKTEST_START="2024-01-01 09:00:00"
$env:GM_BACKTEST_END="2026-01-01 15:00:00"
python strategies\gm_daily_1000.py
```

`backtest_adjust` 建议先用 `ADJUST_PREV`。如果你的模型因子是用不复权价格计算的，要确认价格口径一致，否则会有复权偏差。

## 4. 关键参数

| 参数 | 含义 |
| --- | --- |
| `--top-k` | 每天取模型排名前几只 |
| `--cash-ratio` | 目标仓位总比例，例如 `0.98` 表示留 2% 现金 |
| `--model-name` | 只导出指定模型 |
| `--min-confidence` | 最低置信度过滤 |
| `--max-risk` | 最高风险分过滤 |
| `--start-date` / `--end-date` | 只导出指定信号日期范围 |

## 5. 每天收盘后滚动更新：掘金数据 + 单模型

新增的单模型契约是一个 LightGBM Booster，特征固定在：

```text
D:\cht\agent\stock-agent-platform\data_gm.py -> GM_DAILY_FEATURE_COLUMNS
```

包含动量、均线偏离、波动率、回撤、量能和成交额比例等 16 个日线特征。模型文件必须使用完全一致的特征顺序。

### 5.1 生成训练数据

先在可以访问掘金 SDK 的 Python 环境里跑一次/多次每日更新，得到滚动日线缓存：

```powershell
$env:GM_TOKEN="your_gm_token"
python tools\gm_daily_refresh.py `
  --symbols-file datacsi300_stock_pool.csv `
  --lookback 260 `
  --min-coverage 0.90 `
  --fetch-only
```

`--fetch-only` 只生成 `outputs\gm\gm_bars_latest.csv`，不加载模型、不写 PostgreSQL、不生成目标持仓。第一次训练前请先用这个模式拉取历史日线。

### 5.2 训练唯一模型

```powershell
python tools\train_gm_daily_ranker.py `
  --bars outputs\gm\gm_bars_latest.csv `
  --train-start 2024-01-01 `
  --train-end 2025-12-31 `
  --validation-start 2026-01-01 `
  --validation-end 2026-06-30 `
  --model-output models\gm_daily_ranker\model.txt `
  --metrics-output outputs\gm\gm_daily_ranker_training_metrics.json
```

训练脚本会：

1. 用掘金日线重建 `GM_DAILY_FEATURE_COLUMNS`。
2. 用次日收盘涨跌作为二分类标签。
3. 严格按日期切分训练集和验证集。
4. 输出一个 LightGBM `model.txt`。

### 5.3 每日收盘后推理

模型训练完成后，每天收盘后执行：

```powershell
$env:GM_TOKEN="your_gm_token"
$env:GM_DAILY_MODEL_PATH="D:\cht\agent\stock-agent-platform\models\gm_daily_ranker\model.txt"

python tools\gm_daily_refresh.py `
  --symbols-file datacsi300_stock_pool.csv `
  --lookback 260 `
  --min-coverage 0.90 `
  --top-k 10 `
  --cash-ratio 0.98
```

默认会生成：

```text
outputs\gm\gm_bars_latest.csv
outputs\gm\ranking_latest.csv
outputs\gm\ranking_YYYYMMDD_gm_daily_ranker_xxx.csv
strategies\gm_daily_1000_targets.csv
outputs\gm\gm_daily_refresh_metrics.json
```

如果不加 `--dry-run`，还会把全部预测写入 PostgreSQL 的 `model_prediction`，`source_kind` 为：

```text
gm_daily_ranking
```

### 5.4 Windows 计划任务示例

可以在收盘后 16:10 执行：

```powershell
schtasks /Create /TN "GM Daily Model Refresh" /TR "D:\cht\agent\stock-agent-platform\.venv\Scripts\python.exe D:\cht\agent\stock-agent-platform\tools\gm_daily_refresh.py" /SC DAILY /ST 16:10
```

注意 `gm` SDK 的 Python 版本约束；如果系统 Python 3.12 不兼容，请把计划任务里的解释器改成掘金兼容的 Python 路径。

## 6. 需要后续对齐的规则

当前骨架默认：

- 每天等权配置前 K 名。
- 每天 10 点调仓。
- 目标总仓位 `98%`。
- 移出前 K 的股票清仓。
- 不做涨停可买/跌停可卖/停牌/最小 100 股约束，这些交由掘金撮合规则处理；如果掘金返回废单，策略会继续处理后续标的。

后续如果要更严谨，可以在策略里增加：

- 停牌过滤。
- ST 股票过滤。
- 涨停不买、跌停不卖。
- 单票最大权重。
- 卖出优先、资金不足时按排名截断买入。
- 每周/每 N 天调仓，而不是每天调仓。
