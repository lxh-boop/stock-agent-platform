# 因子库

登录后从顶部「因子库」进入 `/factors`。首次访问会在当前账号的 PostgreSQL 数据域中幂等初始化 158 个 Alpha158 定义。系统因子的表达式与现有 Alpha158 计算一致，最近数值直接读取该账号已保存的 `alpha158_daily`，没有行情时页面会明确提示先完成日更。

## 添加个人因子

点击「添加因子」，只填写名称和一条 Python 表达式。选择模板后可以修改窗口参数，点击「校验并继续」查看最近行情的计算预览，再确认保存。保存失败后可以直接重试，同一次提交不会重复创建。个人因子与名称按账号隔离，已有 Alpha158 名称不能覆盖。

名称示例：`五日动量`。表达式模板：

```python
close / delay(close, 5) - 1
```

其他模板：

```python
# 20 日均线偏离
safe_div(close, mean(close, 20)) - 1

# 20 日收益率标准差
std(close / delay(close, 1) - 1, 20)

# 5 日量比
safe_div(volume, mean(volume, 5))

# 20 日价格与对数成交量相关系数
corr(close, log(volume + 1), 20)
```

输入框填写表达式本身，无需复制注释。字段为 `open`、`high`、`low`、`close`、`volume`、`vwap`；页面「可用字段与函数」列出完整函数和含义。窗口使用 1～252 个交易日，累计最多回看 504 日。`delay`/`shift` 仅允许正整数，表示取过去数据。`std` 使用样本标准差（`ddof=1`）。`safe_div(x, y)` 与现有 Alpha158 一样计算 `x / (y + 1e-12)`。

表达式由受限 AST 解释器求值，不接受 import、赋值、属性访问、索引、循环或任意 Python 代码。每只股票独立按日期升序计算，缺少历史窗口或无效结果显示为空。自定义预览只读取本人数据库中最近最多 3 只股票、每只 600 条行情；可按六位股票代码查询。保存的是因子定义，预览即时计算，不回填历史因子表，也不会自动改变训练模型的特征。

## API 与存储

- `GET /api/v1/web/factors`：数据库因子目录、模板与表达式说明。
- `GET /api/v1/web/factors/{factor_id}/values`：最近数值，支持 `code` 和 `limit`。
- `POST /api/v1/web/factors/validate`：校验名称/表达式并预览，不保存定义。
- `POST /api/v1/web/factors`：复校后保存，要求 `confirmed`、`request_id`、`idempotency_key`。

API 从登录会话确定账号，不接受调用者指定其他账号。`factor_catalog` 和 `factor_catalog_requests` 在本人数据域中保存定义与幂等结果。系统种子按名称与特征合同校验，后续初始化保留个人因子。

本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。
