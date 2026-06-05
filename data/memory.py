"""
长期记忆管理 — 持久化存储未知词分类、模板规则、匹配修正。

存储路径：~/.pos_agent/memory.json（用户目录，不入 git）。
与 CLI 的 /memory 指令共用同一存储文件。

结构:
    {
      "token_aliases": {
        "黑芝麻仙草": {"type": "tea_base", "added": "2025-06-05"}
      },
      "template_rules": {},
      "match_corrections": []
    }
"""

import json
import os
from datetime import date
from typing import Any, Dict, Optional

# ── 存储路径 ─────────────────────────────────────────────────────

_MEMORY_DIR = os.path.join(os.path.expanduser("~"), ".pos_agent")
_MEMORY_PATH = os.path.join(_MEMORY_DIR, "memory.json")

# ── 内存缓存 ─────────────────────────────────────────────────────

_data: Optional[Dict[str, Any]] = None
_dirty: bool = False


# ── 内部方法 ────────────────────────────────────────────────────

def _ensure_dir() -> None:
    """确保存储目录存在。"""
    os.makedirs(_MEMORY_DIR, exist_ok=True)


def _load() -> Dict[str, Any]:
    """从磁盘加载记忆数据（惰性加载，首次访问时触发）。"""
    global _data
    if _data is not None:
        return _data

    _ensure_dir()
    if os.path.exists(_MEMORY_PATH):
        try:
            with open(_MEMORY_PATH, "r", encoding="utf-8") as f:
                _data = json.load(f)
        except (json.JSONDecodeError, IOError):
            _data = _default_structure()
    else:
        _data = _default_structure()

    # 确保顶层键存在
    for key, default in _default_structure().items():
        if key not in _data:
            _data[key] = default

    return _data


def _default_structure() -> Dict[str, Any]:
    """返回记忆文件的默认结构。"""
    return {
        "token_aliases": {},
        "template_rules": {},
        "match_corrections": [],
    }


def _save() -> None:
    """将记忆数据持久化到磁盘。"""
    global _data, _dirty
    if _data is None:
        return
    _ensure_dir()
    try:
        with open(_MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False, indent=2)
        _dirty = False
    except IOError as e:
        print(f"[WARNING] 记忆保存失败: {e}")


# ── 公开 API ────────────────────────────────────────────────────


def get_token_type(word: str) -> Optional[str]:
    """查询 token 别名表中是否已有此词的分类。

    Args:
        word: 待查询的 token 文本（已 normalize 后的值）。

    Returns:
        类型字符串（如 "茶底"/"奶底"）或 None（未找到）。
    """
    data = _load()
    entry = data.get("token_aliases", {}).get(word)
    if entry and isinstance(entry, dict):
        return entry.get("type")
    return None


def add_token(word: str, token_type: str) -> None:
    """将新 token 写入别名表并持久化。

    Args:
        word: token 文本（已 normalize 后的值）。
        token_type: 类型字符串（茶底/奶底/糖度/温度/规格 之一）。
    """
    global _dirty
    data = _load()
    today = date.today().isoformat()
    data.setdefault("token_aliases", {})[word] = {
        "type": token_type,
        "added": today,
    }
    _dirty = True
    _save()


def list_aliases() -> Dict[str, Dict[str, str]]:
    """返回所有已记录的 token 别名（用于 /memory 查看）。

    Returns:
        {word: {"type": "茶底", "added": "2025-06-05"}, ...}
    """
    data = _load()
    return dict(data.get("token_aliases", {}))


def get_template_rules() -> Dict[str, Any]:
    """返回已记录的模板规则。"""
    data = _load()
    return dict(data.get("template_rules", {}))


def add_template_rule(key: str, value: Any) -> None:
    """添加模板规则并持久化。"""
    global _dirty
    data = _load()
    data.setdefault("template_rules", {})[key] = value
    _dirty = True
    _save()


def add_match_correction(entry: Dict[str, Any]) -> None:
    """添加匹配修正记录并持久化。"""
    global _dirty
    data = _load()
    data.setdefault("match_corrections", []).append(entry)
    _dirty = True
    _save()


def get_memory_path() -> str:
    """返回记忆文件路径（供 /memory 指令显示）。"""
    return _MEMORY_PATH


def reload() -> None:
    """强制从磁盘重新加载记忆（用于外部修改后刷新）。"""
    global _data
    _data = None
    _load()


def reset_memory() -> None:
    """清空所有记忆数据（用于测试）。"""
    global _data, _dirty
    _data = _default_structure()
    _dirty = True
    _save()


# ── 自测 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    # ── 使用真实记忆路径，先备份原始数据 ──
    # 加载当前记忆（如果有的话）
    _initial_data_backup = None
    if os.path.exists(_MEMORY_PATH):
        try:
            with open(_MEMORY_PATH, "r", encoding="utf-8") as f:
                _initial_data_backup = f.read()
        except Exception:
            pass

    # 清空记忆，从干净状态开始自测
    reset_memory()

    passed = 0
    failed = 0

    def check(condition, msg):
        global passed, failed
        if condition:
            passed += 1
            print(f"  PASS  {msg}")
        else:
            failed += 1
            print(f"  FAIL  {msg}")

    print("=== Memory 自测 ===\n")

    # ── 1. 初始状态 ──
    print("1. 初始状态（空记忆）")
    check(get_token_type("珍珠") is None, "未知词返回 None")
    check(get_token_type("红茶") is None, "标准词也未在记忆中（归 token_dict 管）")
    print()

    # ── 2. 写入并查询 ──
    print("2. add_token → get_token_type")
    add_token("黑芝麻仙草", "茶底")
    check(get_token_type("黑芝麻仙草") == "茶底", "写入后能查到 type=茶底")

    add_token("豆乳", "奶底")
    check(get_token_type("豆乳") == "奶底", "写入后能查到 type=奶底")
    print()

    # ── 3. 持久化验证 ──
    print("3. 持久化验证（reload 后仍存在）")
    reload()
    check(get_token_type("黑芝麻仙草") == "茶底", "reload 后 '黑芝麻仙草' 仍在")
    check(get_token_type("豆乳") == "奶底", "reload 后 '豆乳' 仍在")
    check(os.path.exists(_MEMORY_PATH), f"memory.json 文件已创建: {_MEMORY_PATH}")
    print()

    # ── 4. JSON 结构验证 ──
    print("4. JSON 结构正确")
    with open(_MEMORY_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    check("token_aliases" in raw, "顶层含 token_aliases")
    check("template_rules" in raw, "顶层含 template_rules")
    check("match_corrections" in raw, "顶层含 match_corrections")
    entry = raw["token_aliases"]["黑芝麻仙草"]
    check("type" in entry and "added" in entry, "别名条目含 type 和 added")
    check(entry["type"] == "茶底", "type 值正确")
    print()

    # ── 5. list_aliases ──
    print("5. list_aliases")
    aliases = list_aliases()
    check(len(aliases) == 2, f"2 条别名（实际 {len(aliases)}）")
    check("黑芝麻仙草" in aliases and "豆乳" in aliases, "两条都在")
    print()

    # ── 6. template_rules ──
    print("6. template_rules 读写")
    check(get_template_rules() == {}, "初始为空")
    add_template_rule("last_composite_col", "口味做法组合")
    check(get_template_rules().get("last_composite_col") == "口味做法组合", "写入后正确")
    print()

    # ── 7. match_corrections ──
    print("7. match_corrections 追加")
    add_match_correction({"row": 5, "from": "T240", "to": "T265", "reason": "人工修正"})
    data = _load()
    check(len(data["match_corrections"]) == 1, "1 条修正记录")
    check(data["match_corrections"][0]["row"] == 5, "记录内容正确")
    print()

    # ── 8. get_memory_path ──
    print("8. get_memory_path")
    path = get_memory_path()
    check(path == _MEMORY_PATH, f"路径正确: {path}")
    print()

    # ── 9. 覆盖写入（同一词重复 add） ──
    print("9. 重复 add 同一词（覆盖更新）")
    add_token("黑芝麻仙草", "茶底")  # 再写一次，不抛异常
    check(get_token_type("黑芝麻仙草") == "茶底", "覆盖后仍正确")
    print()

    # ── 10. reset_memory ──
    print("10. reset_memory")
    reset_memory()
    data = _load()
    check(data["token_aliases"] == {}, "token_aliases 已清空")
    check(data["template_rules"] == {}, "template_rules 已清空")
    check(data["match_corrections"] == [], "match_corrections 已清空")
    print()

    # ── 还原原始记忆数据 ──
    if _initial_data_backup is not None:
        _ensure_dir()
        with open(_MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(_initial_data_backup)
        # 重新加载
        reload()
    else:
        # 自测前没有记忆文件，清理自测产生的文件
        reset_memory()
        if os.path.exists(_MEMORY_PATH):
            os.remove(_MEMORY_PATH)

    print(f"=== 结果: {passed} passed, {failed} failed ===")
