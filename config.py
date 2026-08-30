import os

from core.config.paths import (
    ensure_runtime_directories,
    get_config_dir,
    get_data_dir,
    get_database_dir,
    get_logs_dir,
    get_models_dir,
    get_outputs_dir,
    get_resource_root,
    get_runtime_dir,
    is_frozen_app,
)


def _mode_path(path, dev_value: str) -> str:
    return str(path) if is_frozen_app() else dev_value


# ============================================================
# 股票池设置
# ============================================================

# 可选：
# "manual"：使用手写 STOCK_POOL
# "csi300"：使用 CSI300 股票池
UNIVERSE = "csi300"

# Qlib 数据目录
QLIB_PROVIDER_URI = os.environ.get("QLIB_PROVIDER_URI", r"D:\qlib_data\cn_data")

# CSI300 股票池缓存文件
CSI300_POOL_CACHE_PATH = r"data\csi300_stock_pool.csv"

# 最近一次通过完整数量校验的股票池。在线接口临时失败时用于兜底。
CSI300_POOL_LAST_GOOD_PATH = os.environ.get(
    "CSI300_POOL_LAST_GOOD_PATH",
    r"data\csi300_stock_pool.last_good.csv",
)

# 当前缓存超过该天数后尝试在线刷新；刷新失败仍可使用 last-good 缓存。
CSI300_POOL_CACHE_MAX_AGE_DAYS = int(
    os.environ.get("CSI300_POOL_CACHE_MAX_AGE_DAYS", "45")
)

# Tushare index_weight 是月度数据，按自然月逐月向前回溯。
CSI300_INDEX_WEIGHT_LOOKBACK_MONTHS = int(
    os.environ.get("CSI300_INDEX_WEIGHT_LOOKBACK_MONTHS", "18")
)

# 如果 Qlib 的 csi300 文件不存在，是否尝试从 Tushare 获取。
USE_TUSHARE_INDEX_WEIGHT_FALLBACK = True

# Tushare 无数据或权限不足时，使用 AKShare 的中证指数成分接口。
CSI300_AKSHARE_FALLBACK_ENABLED = (
    os.environ.get("CSI300_AKSHARE_FALLBACK_ENABLED", "true")
    .strip()
    .lower()
    not in {"0", "false", "no", "off"}
)


# ============================================================
# 手写股票池，仅用于测试
# ============================================================

STOCK_POOL = {
    "000001": "平安银行",
    "000002": "万科A",
    "000333": "美的集团",
    "000858": "五粮液",
    "002475": "立讯精密",
    "300750": "宁德时代",
    "600036": "招商银行",
    "600519": "贵州茅台",
    "600900": "长江电力",
    "601318": "中国平安",
    "601899": "紫金矿业",
    "603259": "药明康德",
}


# ============================================================
# 数据设置
# ============================================================

START_DATE = "20200101"
PRED_HORIZON = 5
MODEL_REG_LABEL_COL = "future_5d_score"
MODEL_PRED_COL = "pred_score"
LABEL_RET_CLIP = 0.30
LABEL_ZSCORE_CLIP = 3.0
ALPHA_WINDOWS = [5, 10, 20, 30, 60]
EPS = 1e-12

# ============================================================
# 新闻/公告事件特征
# ============================================================

# 第一版只做标题级关键词规则；接口失败时自动退化为全 0 特征。
ENABLE_NEWS_FEATURES = True
NEWS_EVENT_LOOKBACK_DAYS = 5
ENABLE_AKSHARE_NEWS_FALLBACK = True
AKSHARE_FETCH_ANNOUNCEMENTS = True
AKSHARE_FETCH_STOCK_NEWS = True
AKSHARE_NOTICE_RECENT_PAGES = 20
AKSHARE_NOTICE_MAX_DAYS = 10
AKSHARE_STOCK_NEWS_MAX_CODES = 300
AKSHARE_REQUEST_SLEEP_SECONDS = 0.05
AKSHARE_FETCH_WORKERS = 4
ENABLE_COLD_START_NEWS_ADJUSTMENT = True
COLD_START_NEWS_RELIABILITY_WEIGHT = 1.0
ENABLE_RAG = True
ENABLE_LLM_EXPLAINER = True

# ============================================================
# Neo4j 金融事实图
# ============================================================
# Agent 公共实体协议只使用 GraphRef。证券代码、名称和供应商标识只能在
# Worker 私有 Provider Adapter 内部使用。Neo4j 未配置时不回退旧实体解析器。
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", os.environ.get("NEO4J_USER", "neo4j"))
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")
FINANCIAL_GRAPH_ID = os.environ.get("FINANCIAL_GRAPH_ID", "financial_graph")
NEO4J_CONNECTION_TIMEOUT_SECONDS = float(os.environ.get("NEO4J_CONNECTION_TIMEOUT_SECONDS", "10"))
NEO4J_MAX_CONNECTION_POOL_SIZE = int(os.environ.get("NEO4J_MAX_CONNECTION_POOL_SIZE", "20"))


# ============================================================
# 大模型接口设置
# ============================================================

LLM_PROVIDER = "openai_compatible"
LLM_API_KEY_ENV = "LLM_API_KEY"
LLM_BASE_URL_ENV = "LLM_BASE_URL"
LLM_MODEL_ENV = "LLM_MODEL"

DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_LLM_BASE_URL = ""
DEFAULT_LLM_MODE = "api"
DEFAULT_API_LLM_PROVIDER = "openai_compatible"
DEFAULT_LOCAL_LLM_PROVIDER = "ollama_local"
DEFAULT_LOCAL_LLM_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LOCAL_LLM_MODEL = "stock-agent-qwen3-4b"
DEFAULT_LOCAL_LLM_DISABLE_THINKING = True
DEFAULT_API_LLM_CONTEXT_WINDOW = 128000
DEFAULT_LOCAL_LLM_CONTEXT_WINDOW = 32768


# ============================================================
# External model settings
# ============================================================

DEFAULT_DFT_UNET_CHECKPOINT_PATH = os.environ.get(
    "DFT_UNET_CHECKPOINT_PATH",
    (
        r"D:\paper_work\Unet_DFT\experiments\search_pure_unet_l3_seed0"
        r"\DFT_UNET_dft_unet_l3_d64_ic_seed0_20260529_020115\best_model.pth"
    ),
)
DFT_UNET_MODEL_NAME = "dft_unet"
DFT_UNET_DEFAULT_TRAIN_MODE = "predict_only"

# DFT_UNET market context. The external model uses 63 market features:
# CSI300, CSI500 and CSI800, each with 21 rolling index indicators.
MARKET_CONTEXT_START_DATE = "20080101"
MARKET_CONTEXT_FIT_END_DATE = "20200331"


# ============================================================
# 评分阈值
# ============================================================

CONFIDENCE_HIGH_THRESHOLD = 0.70
CONFIDENCE_MEDIUM_THRESHOLD = 0.45


# ============================================================
# 当前默认排名模型
# ============================================================

# 业务层只依赖这组稳定身份，不依赖具体算法或 checkpoint 文件名。
ACTIVE_RANKING_MODEL_NAME = "cross_sectional_ranker"
ACTIVE_RANKING_MODEL_BACKEND = "registered_ranker"
ACTIVE_RANKING_MODEL_VERSION = "2026.08.29.1"
MODEL_NAME = ACTIVE_RANKING_MODEL_NAME


# ============================================================
# 路径
# ============================================================

DATA_DIR = _mode_path(get_data_dir(), "data")
MODEL_DIR = _mode_path(get_models_dir(), "models")
OUTPUT_DIR = _mode_path(get_outputs_dir(), "outputs")
LOG_DIR = _mode_path(get_logs_dir(), "logs")
RUNTIME_DIR = _mode_path(get_runtime_dir(), "runtime")
CONFIG_DIR = _mode_path(get_config_dir(), ".")
AI_EXPLANATION_DIR = os.path.join(OUTPUT_DIR, "ai_explanations")
DFT_UNET_MODEL_DIR = os.path.join(MODEL_DIR, DFT_UNET_MODEL_NAME)
DFT_UNET_BASE_DIR = os.path.join(DFT_UNET_MODEL_DIR, "base")
DFT_UNET_LATEST_DIR = os.path.join(DFT_UNET_MODEL_DIR, "latest")
DFT_UNET_BASE_MODEL_PATH = os.path.join(DFT_UNET_BASE_DIR, "best_model.pth")
DFT_UNET_LATEST_MODEL_PATH = os.path.join(DFT_UNET_LATEST_DIR, "model.pth")
DFT_UNET_LATEST_METRICS_PATH = os.path.join(DFT_UNET_LATEST_DIR, "metrics.json")
DFT_UNET_FINETUNE_LOG_PATH = os.path.join(OUTPUT_DIR, "dft_unet_finetune_log.csv")
MARKET_CONTEXT_INDEX_DAILY_CACHE_PATH = os.path.join(DATA_DIR, "market_index_daily.csv")
MARKET_CONTEXT_FEATURE_CACHE_PATH = os.path.join(DATA_DIR, "market_context_features.csv")

NEWS_CACHE_PATH = os.path.join(DATA_DIR, "news_cache.csv")
ANNOUNCEMENT_CACHE_PATH = os.path.join(DATA_DIR, "announcement_cache.csv")
RAG_DOCUMENTS_PATH = os.path.join(DATA_DIR, "rag_documents.csv")
RAG_INDEX_PATH = os.path.join(DATA_DIR, "rag_tfidf_index.pkl")

RAW_DATA_PATH = os.path.join(DATA_DIR, "raw_stock_data.csv")
FEATURE_DATA_PATH = os.path.join(DATA_DIR, "feature_stock_data_alpha158.csv")

TRAIN_RAW_DATA_PATH = os.path.join(DATA_DIR, "train_raw_stock_data.csv")
TRAIN_FEATURE_DATA_PATH = os.path.join(DATA_DIR, "train_feature_stock_data_alpha158.csv")

LATEST_RAW_DATA_PATH = os.path.join(DATA_DIR, "latest_raw_stock_data.csv")
LATEST_FEATURE_DATA_PATH = os.path.join(DATA_DIR, "latest_feature_stock_data_alpha158.csv")
KRONOS_MARKET_HISTORY_CACHE_PATH = os.path.join(DATA_DIR, "kronos_market_history.csv")
ACTIVE_RANKING_MODEL_DIR = os.path.join(MODEL_DIR, ACTIVE_RANKING_MODEL_NAME)
ACTIVE_RANKING_MODEL_MANIFEST_PATH = os.environ.get(
    "ACTIVE_RANKING_MODEL_MANIFEST_PATH",
    _mode_path(
        get_resource_root() / "configs" / "active_ranker.json",
        os.path.join("configs", "active_ranker.json"),
    ),
)
ACTIVE_RANKING_MODEL_METRICS_PATH = os.path.join(
    ACTIVE_RANKING_MODEL_DIR,
    "metrics.json",
)
ACTIVE_RANKING_FEATURE_DIR = os.path.join(DATA_DIR, "model_precision", "tushare")

# 旧运行时仍可读取这些名字；正式业务代码不再依赖它们。
KRONOS_LATEST_METRICS_PATH = ACTIVE_RANKING_MODEL_METRICS_PATH
KRONOS_STOCK_DIRECTION_FEATURE_DIR = ACTIVE_RANKING_FEATURE_DIR

RANKING_LATEST_PATH = os.path.join(OUTPUT_DIR, "ranking_latest.csv")
EVAL_METRICS_PATH = os.path.join(OUTPUT_DIR, "evaluation_metrics.csv")
TEST_PREDICTIONS_PATH = os.path.join(OUTPUT_DIR, "test_predictions.csv")
BACKTEST_NAV_PATH = os.path.join(OUTPUT_DIR, "backtest_nav.csv")
BACKTEST_METRICS_PATH = os.path.join(OUTPUT_DIR, "backtest_metrics.json")
BACKTEST_TRADES_PATH = os.path.join(OUTPUT_DIR, "backtest_trades.csv")
BACKTEST_DAILY_PREDICTIONS_PATH = os.path.join(OUTPUT_DIR, "backtest_daily_predictions.csv")
METRICS_PATH = os.path.join(MODEL_DIR, "metrics.pkl")


def ensure_dirs():
    ensure_runtime_directories()
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MODEL_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# 本地训练数据设置
# ============================================================

# 初始训练使用本地数据，不依赖 APP，不需要 Tushare Token
# 可选："qlib" 或 "csv"
LOCAL_TRAIN_SOURCE = "qlib"

# 改成你自己的 Qlib 数据路径
QLIB_PROVIDER_URI = os.environ.get("QLIB_PROVIDER_URI", r"D:\qlib_data\cn_data")

# 如果你不用 Qlib，也可以准备一个本地 CSV
LOCAL_TRAIN_CSV_PATH = r"data\local_train_stock_data.csv"


# Paper trading defaults
DEFAULT_INITIAL_CASH = 150000.0
DEFAULT_PAPER_TRADING_START_DATE = "2026-04-01"
