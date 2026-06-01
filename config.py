"""
配置文件 — API Key、模型参数、匹配阈值。

使用方式：
    from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, MATCHING_CONFIG
"""

import os

# ── DeepSeek API 配置 ───────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# API 调用参数
LLM_TEMPERATURE = 0.1          # Schema 分析和 Token 分类需要确定性输出
LLM_MAX_TOKENS = 2048
LLM_TIMEOUT_SECONDS = 30

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
