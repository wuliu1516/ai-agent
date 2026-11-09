"""识别并排除“金标 SQL 里的字面值在目标库中不存在”的样本。

背景（实测）：本数据集是中文提问 + 英文数据。金标 SQL 里写的中文取值（如
``WHERE course_name = "英语"``）在库里并不存在，库里存的是 ``English``。于是：
金标必然返回 0 行；模型照抄中文 → 也 0 行 → 空对空被误判为正确；模型写库里的英文
→ 有数据 → 被误判为错误。这类样本测不出任何东西，应从测评集中排除。

判定方式：把金标 SQL 里每个“像取值”的引号串，逐个在目标库的所有列上做精确存在性
查询（不枚举、不抽样），全部不存在的即为字面值不匹配样本。
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def _dataset():
    """惰性导入，避免与 backend.main 形成循环依赖。"""
    from backend.main import RECORDS, SCHEMAS, STATE_DB as state_db

    return RECORDS, SCHEMAS, Path(state_db).parent



_QUOTED = re.compile(r"'([^']{1,60})'|\"([^\"]{1,60})\"")
_NUMERIC_LIKE = re.compile(r"[\d\s\-:/%.]+")


def gold_literals(sql: str) -> list[str]:
    """金标 SQL 里所有“像取值”的引号串（过滤掉纯数字/日期和已知标识符）。"""
    values: list[str] = []
    for match in _QUOTED.finditer(sql or ""):
        value = (match.group(1) or match.group(2) or "").strip()
        if not value or _NUMERIC_LIKE.fullmatch(value):
            continue
        values.append(value)
    return values


def identifier_set(db_id: str) -> set[str]:
    _, schemas, _ = _dataset()
    schema = schemas.get(db_id, {})
    names = list(schema.get("table_names_original", []))
    names.extend(column for _, column in schema.get("column_names_original", []))
    return {str(name).casefold() for name in names}


@functools.lru_cache(maxsize=64)
def all_columns(db_id: str) -> tuple[tuple[str, str], ...]:
    """目标库的全部列（不按声明类型过滤：SQLite 是动态类型，声明为数值的列也可能存文本）。"""
    _, schemas, _ = _dataset()
    schema = schemas.get(db_id)
    if not schema:
        return ()
    columns: list[tuple[str, str]] = []
    for table_index, table_name in enumerate(schema.get("table_names_original", [])):
        for owner_index, column_name in schema.get("column_names_original", []):
            if owner_index != table_index or column_name == "*":
                continue
            columns.append((str(table_name), str(column_name)))
    return tuple(columns)


@functools.lru_cache(maxsize=16)
def _connection(db_id: str) -> sqlite3.Connection | None:
    from backend.main import open_source_db, readonly_authorizer

    try:
        connection = open_source_db(db_id)
        connection.set_authorizer(readonly_authorizer)
        return connection
    except Exception:
        return None


@functools.lru_cache(maxsize=300_000)
def literal_exists(db_id: str, literal: str) -> bool | None:
    """字面值是否在目标库的任意列中出现过；None 表示无法判定（库打不开）。"""
    connection = _connection(db_id)
    if connection is None:
        return None
    for table, column in all_columns(db_id):
        try:
            if connection.execute(
                f'SELECT 1 FROM "{table}" WHERE "{column}" = ? LIMIT 1', (literal,)
            ).fetchone():
                return True
        except sqlite3.Error:
            continue
    return False


def unmatched_gold_literals(db_id: str, sql: str) -> list[str]:
    """金标 SQL 里在库中找不到的字面值。"""
    identifiers = identifier_set(db_id)
    missing: list[str] = []
    for literal in gold_literals(sql):
        if literal.casefold() in identifiers:
            continue
        if literal_exists(db_id, literal) is False:
            missing.append(literal)
    return missing


def cache_path(split: str) -> Path:
    _, _, storage = _dataset()
    return storage / f"gold_literal_mismatch_{split}.json"


def compute_split(split: str, *, force: bool = False) -> dict[str, list[str]]:
    """返回 {样本序号: [缺失的字面值]}；结果写入缓存文件。"""
    path = cache_path(split)
    if path.is_file() and not force:
        return {int(key): value for key, value in json.loads(path.read_text(encoding="utf-8")).items()}

    records, _, _ = _dataset()
    result: dict[str, list[str]] = {}
    for index, record in enumerate(records[split]):
        missing = unmatched_gold_literals(record["db_id"], record.get("_gold_sql", ""))
        if missing:
            result[str(index)] = missing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return {int(key): value for key, value in result.items()}


def excluded_indices(split: str) -> dict[int, list[str]]:
    """该划分中应排除的样本序号（带缓存）。"""
    return compute_split(split)


def main() -> None:
    force = "--force" in sys.argv[1:]
    records, _, _ = _dataset()
    targets = [arg for arg in sys.argv[1:] if not arg.startswith("-")] or list(records)
    for split in targets:
        excluded = compute_split(split, force=force)
        total = len(records[split])
        print(f"{split}: 排除 {len(excluded)}/{total} = {len(excluded) / max(total, 1):.1%}")
        for index, missing in list(excluded.items())[:5]:
            print(f"    #{index} {records[split][index]['db_id']}: {missing[:3]}")


if __name__ == "__main__":
    main()
