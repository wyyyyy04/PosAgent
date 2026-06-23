"""
配置文件 — API Key、模型参数、匹配阈值。

配置优先级：~/.menupilot/config.json > 环境变量 > 程序默认值

使用方式：
    from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, MATCHING_CONFIG
"""

import json
import os

# ── 用户配置文件加载 ─────────────────────────────────────────────

_CONFIG_DIR = os.path.expanduser("~/.menupilot")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")


def _load_json_config() -> dict:
    """从 ~/.menupilot/config.json 加载用户配置。"""
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


_json = _load_json_config()

# ── DeepSeek API 配置 ───────────────────────────────────────────
# 优先级：JSON 配置文件 > 环境变量 > 默认值

DEEPSEEK_API_KEY = (
    _json.get("DEEPSEEK_API_KEY") or
    os.environ.get("DEEPSEEK_API_KEY") or
    ""  # 不再有硬编码 fallback，未配置时为空字符串（首次运行触发配置向导）
)

DEEPSEEK_BASE_URL = (
    _json.get("DEEPSEEK_BASE_URL") or
    os.environ.get("DEEPSEEK_BASE_URL") or
    "https://api.deepseek.com/v1"
)

DEEPSEEK_MODEL = (
    _json.get("DEEPSEEK_MODEL") or
    os.environ.get("DEEPSEEK_MODEL") or
    "deepseek-chat"
)

# API 调用参数
LLM_TEMPERATURE = 0.1          # Schema 分析和 Token 分类需要确定性输出
LLM_MAX_TOKENS = 4096
LLM_TIMEOUT_SECONDS = 30

# 调试模式（环境变量 MENUPILOT_DEBUG=1 开启）
DEBUG = os.environ.get("MENUPILOT_DEBUG", "0") == "1"

# ── 匹配引擎配置 ────────────────────────────────────────────────

MATCHING_CONFIG = {
    # 商品名匹配（RapidFuzz token_sort_ratio）
    "product_name_threshold": 90,      # ≥ 此值视为匹配
    "product_name_scorer": "token_sort_ratio",

    # Embedding 兜底
    "embedding_enabled": False,         # 默认关闭（按 README 设计）
    "embedding_model": "paraphrase-multilingual-MiniLM-L12-v2",
    "embedding_top_k": 3,              # 候选召回数量
    "embedding_similarity_threshold": 0.85,

    # 低置信度
    "low_confidence_threshold": 80,     # 商品名匹配低于此值直接 LOW_CONFIDENCE
}

# ── 日志配置 ────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ── 自测用 Mock 配置 ───────────────────────────────────────────

# 自测时使用此标志跳过真实 LLM 调用
USE_MOCK_LLM = os.environ.get("USE_MOCK_LLM", "0") == "1"

# Mock LLM 响应（Schema Analyzer）
MOCK_SCHEMA_RESPONSE = {
    "field_mapping": {
        "菜品名称": "product_name",
        "规格": "size",
    },
    "composite_col": "口味做法组合",
    "target_col": "配料",
    "irrelevant_cols": [],
}

# Mock LLM 响应（Token Classifier）
MOCK_TOKEN_RESPONSE = [
    {
        "tokens": [
            {"value": "红茶", "type": "茶底"},
            {"value": "十二分糖", "type": "糖度"},
            {"value": "温热", "type": "温度"},
        ],
        "missing": ["奶底"],
    },
    {
        "tokens": [
            {"value": "红茶", "type": "茶底"},
            {"value": "十二分糖", "type": "糖度"},
            {"value": "正常冰", "type": "温度"},
        ],
        "missing": ["奶底"],
    },
]
