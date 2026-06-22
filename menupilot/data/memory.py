"""
长期记忆管理 — 持久化存储未知词分类、模板规则、匹配修正。

存储路径：~/.menupilot/memory.json（用户目录，不入 git）。
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

_MEMORY_DIR = os.path.join(os.path.expanduser("~"), ".menupilot")
_MEMORY_PATH = os.path.join(_MEMORY_DIR, "memory.json")

# ── 兼容迁移：旧目录 → 新目录 ──
_OLD_MEMORY_DIR = os.path.join(os.path.expanduser("~"), ".pos_agent")
_OLD_MEMORY_PATH = os.path.join(_OLD_MEMORY_DIR, "memory.json")

def _migrate_old_data():
    """如果旧目录存在且新目录不存在，自动迁移数据。"""
    if os.path.exists(_OLD_MEMORY_PATH) and not os.path.exists(_MEMORY_PATH):
        try:
            import shutil
            os.makedirs(_MEMORY_DIR, exist_ok=True)
            shutil.copy(_OLD_MEMORY_PATH, _MEMORY_PATH)
        except Exception:
            pass  # 迁移失败不阻塞启动

# ── 内存缓存 ─────────────────────────────────────────────────────

_data: Optional[Dict[str, Any]] = None
_dirty: bool = False

# ── 会话内新增 token 追踪（用于运行结束后的摘要展示）──────────────

_session_new_tokens: list = []  # [(word, type), ...]


def get_new_tokens() -> list:
    """返回本次管线运行中新增的 token 列表（供摘要展示）。

    Returns:
        [(word, type), ...] 按添加顺序排列。
    """
    return list(_session_new_tokens)


def reset_new_tokens() -> None:
    """清空会话新增 token 追踪（每次管线运行开始时调用）。"""
    global _session_new_tokens
    _session_new_tokens = []


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
    _migrate_old_data()
    if os.path.exists(_MEMORY_PATH):
        try:
            with open(_MEMORY_PATH, "r", encoding="utf-8") as f:
                _data = json.load(f)
        except (json.JSONDecodeError, IOError):
            # 文件损坏 → 尝试从 .bak 恢复
            bak_path = _MEMORY_PATH + ".bak"
            if os.path.exists(bak_path):
                try:
                    with open(bak_path, "r", encoding="utf-8") as fb:
                        _data = json.load(fb)
                    print(f"[WARNING] 记忆文件损坏，已从 .bak 备份恢复")
                except (json.JSONDecodeError, IOError):
                    _data = _default_structure()
                    print(f"[WARNING] 记忆文件及备份均损坏，已重置为空")
            else:
                _data = _default_structure()
                print(f"[WARNING] 记忆文件损坏且无备份，已重置为空")
    else:
        _data = _default_structure()

    # 确保顶层键存在（兼容旧版 memory.json 缺少新增键）
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
        "column_aliases": {},
        "confirmed_mappings": {},
    }


def _save() -> None:
    """将记忆数据原子化持久化到磁盘。

    先写临时文件，再原子替换，防止写入过程中断导致文件损坏。
    写入前保留一份 .bak 备份，极端情况下可手动恢复。
    """
    global _data, _dirty
    if _data is None:
        return
    _ensure_dir()
    try:
        # Step 1: 写入临时文件
        tmp_path = _MEMORY_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())  # 确保刷到磁盘（Windows 关键）

        # Step 2: 保留旧文件为 .bak
        bak_path = _MEMORY_PATH + ".bak"
        if os.path.exists(_MEMORY_PATH):
            try:
                os.replace(_MEMORY_PATH, bak_path)
            except OSError:
                pass  # 备份失败不阻塞

        # Step 3: 原子替换
        os.replace(tmp_path, _MEMORY_PATH)
        _dirty = False
    except (IOError, OSError) as e:
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
    # 记录到会话新增列表（用于运行后摘要展示）
    _session_new_tokens.append((word, token_type))


def edit_token(word: str, new_type: str) -> bool:
    """修改已有 token 别名的类型并持久化。

    Args:
        word: 要修改的 token 文本。
        new_type: 新的类型字符串。

    Returns:
        True 如果词条存在并被修改，False 如果词条不存在。
    """
    global _dirty
    data = _load()
    aliases = data.get("token_aliases", {})
    if word not in aliases:
        return False
    aliases[word]["type"] = new_type
    aliases[word]["modified"] = date.today().isoformat()
    _dirty = True
    _save()
    return True


def list_aliases() -> Dict[str, Dict[str, str]]:
    """返回所有已记录的 token 别名（用于 /memory 查看）。

    Returns:
        {word: {"type": "茶底", "added": "2025-06-05"}, ...}
    """
    data = _load()
    return dict(data.get("token_aliases", {}))


def get_column_alias(col_name: str) -> Optional[str]:
    """查询列别名表，返回列名对应的 canonical 字段或特殊标记。

    特殊标记：'ignore' 表示忽略此列。

    Args:
        col_name: 原始列名。

    Returns:
        canonical 字段名（如 "product_name"）、特殊标记（"ignore"）或 None。
    """
    data = _load()
    entry = data.get("column_aliases", {}).get(col_name)
    if entry and isinstance(entry, dict):
        return entry.get("field")
    return None


def add_column_alias(col_name: str, canonical_field: str) -> None:
    """将列名 → canonical 字段映射写入别名表并持久化。

    Args:
        col_name: 原始列名。
        canonical_field: canonical 字段名或特殊标记（"ignore"）。
    """
    global _dirty
    data = _load()
    today = date.today().isoformat()
    data.setdefault("column_aliases", {})[col_name] = {
        "field": canonical_field,
        "added": today,
    }
    _dirty = True
    _save()


def list_column_aliases() -> Dict[str, Dict[str, str]]:
    """返回所有已记录的列别名映射。

    Returns:
        {col_name: {"field": "product_name", "added": "2025-06-05"}, ...}
    """
    data = _load()
    return dict(data.get("column_aliases", {}))


def get_template_rules() -> Dict[str, Any]:
    """返回已记录的模板规则。"""
    data = _load()
    return dict(data.get("template_rules", {}))


# ── Human Review 确认映射 ────────────────────────────────────────


def build_confirmed_key(master_fingerprint: str, canonical_row: dict) -> str:
    """为 human_review 确认结果构建唯一键。

    键格式：fingerprint|product_name|size|milk_base|temperature|sugar|tea_base

    Args:
        master_fingerprint: 主数据文件 MD5 前 8 位。
        canonical_row: 模板 canonical 行。

    Returns:
        唯一键字符串。
    """
    parts = [master_fingerprint]
    for field in ["product_name", "size", "milk_base", "temperature", "sugar", "tea_base"]:
        val = canonical_row.get(field) or ""
        parts.append(str(val).strip())
    return "|".join(parts)


def add_confirmed_mapping(key: str, sop: str) -> None:
    """持久化 human_review 确认的映射关系。

    Args:
        key: build_confirmed_key 生成的唯一键。
        sop: 确认的 SOP 代码，或 "__SKIP__" 表示永久跳过。
    """
    global _dirty
    data = _load()
    data.setdefault("confirmed_mappings", {})[key] = {
        "sop": sop,
        "confirmed_at": date.today().isoformat(),
    }
    _dirty = True
    _save()


def get_confirmed_mapping(key: str) -> Optional[str]:
    """查询已确认的映射关系。

    Args:
        key: build_confirmed_key 生成的唯一键。

    Returns:
        确认的 SOP 代码，或 None。
    """
    data = _load()
    entry = data.get("confirmed_mappings", {}).get(key)
    if entry and isinstance(entry, dict):
        return entry.get("sop")
    return None


def add_template_rule(key: str, value: Any) -> None:
    """添加模板规则并持久化。"""
    global _dirty
    data = _load()
    data.setdefault("template_rules", {})[key] = value
    _dirty = True
    _save()


def get_template_rule(fingerprint: str) -> Optional[Dict[str, Any]]:
    """根据模板指纹查询缓存的字段映射配置。

    指纹由模板所有列名排序后 MD5 生成，作为 template_rules 的键。

    Args:
        fingerprint: 模板指纹（MD5 hex 字符串）。

    Returns:
        缓存的 schema 分析结果 dict，或 None（未命中）。
    """
    data = _load()
    entry = data.get("template_rules", {}).get(fingerprint)
    if entry and isinstance(entry, dict) and "rule" in entry:
        return entry["rule"]
    return None


def save_template_rule(fingerprint: str, rule: Dict[str, Any]) -> None:
    """将模板字段映射配置按指纹持久化存储。

    Args:
        fingerprint: 模板指纹（MD5 hex 字符串）。
        rule: Schema Analyzer 输出的字段映射配置 dict。
    """
    global _dirty
    data = _load()
    today = date.today().isoformat()
    data.setdefault("template_rules", {})[fingerprint] = {
        "rule": rule,
        "cached_at": today,
        "columns_hash": fingerprint,
    }
    _dirty = True
    _save()


def add_match_correction(entry: Dict[str, Any]) -> None:
    """添加匹配修正记录并持久化。"""
    global _dirty
    data = _load()
    data.setdefault("match_corrections", []).append(entry)
    _dirty = True
    _save()


def delete_token(word: str) -> bool:
    """删除指定的 token 别名。

    Args:
        word: 要删除的 token 文本。

    Returns:
        True 如果词条存在并被删除，False 如果词条不存在。
    """
    global _dirty
    data = _load()
    aliases = data.get("token_aliases", {})
    if word in aliases:
        del aliases[word]
        _dirty = True
        _save()
        return True
    return False


def delete_template_rule(fingerprint_prefix: str) -> Optional[str]:
    """根据指纹前缀删除模板规则缓存。

    支持模糊匹配：只要 rule 的 fingerprint 以给定前缀开头即匹配。
    若前缀匹配到多个规则，不删除任何规则，返回 None。

    Args:
        fingerprint_prefix: 指纹前缀（至少 1 个字符）。

    Returns:
        被删除的完整 fingerprint，或 None（未匹配或多重匹配）。
    """
    global _dirty
    data = _load()
    rules = data.get("template_rules", {})
    matches = [fp for fp in rules if fp.startswith(fingerprint_prefix)]
    if len(matches) == 1:
        del rules[matches[0]]
        _dirty = True
        _save()
        return matches[0]
    return None


def get_stats() -> Dict[str, int]:
    """返回记忆数据的统计信息（供 /memory reset 确认提示使用）。

    Returns:
        {"aliases": N, "templates": M, "corrections": K}
    """
    data = _load()
    return {
        "aliases": len(data.get("token_aliases", {})),
        "templates": len(data.get("template_rules", {})),
        "corrections": len(data.get("match_corrections", [])),
        "column_aliases": len(data.get("column_aliases", {})),
    }


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
    global _data, _dirty, _session_new_tokens
    _data = _default_structure()
    _dirty = True
    _save()
    _session_new_tokens = []


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

    # ── 6. template_rules 通用读写 ──
    print("6. template_rules 通用读写")
    check(get_template_rules() == {}, "初始为空")
    add_template_rule("last_composite_col", "口味做法组合")
    check(get_template_rules().get("last_composite_col") == "口味做法组合", "写入后正确")
    print()

    # ── 6b. get_template_rule / save_template_rule（指纹缓存） ──
    print("6b. 模板指纹缓存（get_template_rule / save_template_rule）")
    fp1 = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    check(get_template_rule(fp1) is None, "新指纹查不到 → None")

    test_rule = {
        "field_mapping": {"菜品名称": "product_name", "规格": "size"},
        "composite_col": "口味做法组合",
        "target_col": "配料",
        "irrelevant_cols": [],
    }
    save_template_rule(fp1, test_rule)
    cached = get_template_rule(fp1)
    check(cached is not None, "写入后可查到")
    check(cached.get("composite_col") == "口味做法组合", "缓存内容正确")
    check(cached.get("target_col") == "配料", "缓存内容完整")
    print()

    # ── 6c. 持久化：reload 后指纹缓存仍在 ──
    print("6c. 指纹缓存持久化（reload 后仍存在）")
    reload()
    cached2 = get_template_rule(fp1)
    check(cached2 is not None, "reload 后缓存仍在")
    check(cached2.get("field_mapping") == test_rule["field_mapping"], "内容完整一致")
    # 不同指纹不命中
    fp2 = "ffffffffffffffffffffffffffffffff"
    check(get_template_rule(fp2) is None, "不同指纹 → None")
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

    # ── 11. edit_token ──
    print("11. edit_token 修改已有词条类型")
    add_token("测试误选词", "茶底")
    check(get_token_type("测试误选词") == "茶底", "初始类型为茶底")
    ok = edit_token("测试误选词", "奶底")
    check(ok is True, "edit_token 返回 True（词条存在）")
    check(get_token_type("测试误选词") == "奶底", "类型已改为奶底")
    # 编辑不存在的词
    ok2 = edit_token("不存在的词", "茶底")
    check(ok2 is False, "edit_token 返回 False（词条不存在）")
    print()

    # ── 12. get_new_tokens / reset_new_tokens ──
    print("12. get_new_tokens / reset_new_tokens 会话追踪")
    reset_memory()
    reset_new_tokens()
    check(len(get_new_tokens()) == 0, "初始为空")
    add_token("词A", "茶底")
    add_token("词B", "奶底")
    new_tokens = get_new_tokens()
    check(len(new_tokens) == 2, "新增 2 条记录")
    check(("词A", "茶底") in new_tokens, "词A→茶底 在列表中")
    check(("词B", "奶底") in new_tokens, "词B→奶底 在列表中")
    reset_new_tokens()
    check(len(get_new_tokens()) == 0, "reset 后清空")
    print()

    # ── 13. column_aliases 读写 ──
    print("13. column_aliases 读写")
    check(get_column_alias("菜品名称") is None, "初始无此列别名")
    add_column_alias("菜品名称", "product_name")
    check(get_column_alias("菜品名称") == "product_name", "写入后可查到")
    add_column_alias("备注", "ignore")
    check(get_column_alias("备注") == "ignore", "ignore 标记可写入")
    # list
    col_aliases = list_column_aliases()
    check(len(col_aliases) == 2, f"2 条列别名（实际 {len(col_aliases)}）")
    check(col_aliases["菜品名称"]["field"] == "product_name", "条目结构含 field")
    check("added" in col_aliases["菜品名称"], "条目结构含 added")
    # 持久化
    reload()
    check(get_column_alias("菜品名称") == "product_name", "reload 后仍存在")
    check(get_column_alias("备注") == "ignore", "reload 后 ignore 仍存在")
    # reset 清空
    reset_memory()
    check(get_column_alias("菜品名称") is None, "reset 后清空")
    print()

    # ── 14. get_stats 包含 column_aliases ──
    print("14. get_stats 包含 column_aliases")
    reset_memory()
    add_column_alias("A", "product_name")
    add_column_alias("B", "size")
    stats = get_stats()
    check(stats.get("column_aliases") == 2, f"stats 含 column_aliases=2（实际 {stats}）")
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
