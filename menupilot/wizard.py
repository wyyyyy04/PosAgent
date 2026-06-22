"""
首次运行配置向导 — 引导用户设置 DeepSeek API Key。

仅在 ~/.menupilot/config.json 不存在或缺少 API Key 时触发。
配置写入 ~/.menupilot/config.json，后续运行自动读取。
"""

import json
import os
import sys

_CONFIG_DIR = os.path.expanduser("~/.menupilot")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")


def run_wizard() -> str:
    """启动中文配置向导，返回用户输入的 API Key。

    如果 config.json 已存在，保留其中的其他配置项（BASE_URL、MODEL 等）。
    """
    print()
    print("=" * 60)
    print("  \U0001f375 欢迎使用 MenuPilot — 智能 POS 模板映射助手")
    print("=" * 60)
    print()
    print("  首次使用需要配置 DeepSeek API Key。")
    print("  申请地址: https://platform.deepseek.com/api_keys")
    print()
    print("  ⚠️  你的 API Key 只会保存在本地，不会上传到任何服务器。")
    print()

    # ── 交互式输入 ──
    while True:
        try:
            api_key = input("  请输入你的 DeepSeek API Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  ❌ 已取消配置。可设置环境变量 DEEPSEEK_API_KEY 后重试。")
            sys.exit(1)

        if api_key:
            break
        print("  ⚠️  API Key 不能为空，请重新输入。")
        print()

    # ── 写入配置文件（保留已有配置） ──
    os.makedirs(_CONFIG_DIR, exist_ok=True)

    existing = {}
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    existing["DEEPSEEK_API_KEY"] = api_key
    existing.setdefault("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    existing.setdefault("DEEPSEEK_MODEL", "deepseek-chat")

    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    print()
    print(f"  ✅ 配置已保存到: {_CONFIG_PATH}")
    print("  如需修改，可直接编辑该文件，或删除后重新运行 menupilot。")
    print("=" * 60)
    print()

    return api_key


def get_saved_api_key() -> str:
    """静默读取已保存的 API Key（不触发向导）。"""
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("DEEPSEEK_API_KEY", "")
        except (json.JSONDecodeError, IOError):
            pass
    return ""


if __name__ == "__main__":
    run_wizard()
