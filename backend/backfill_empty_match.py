"""把「金标为空且生成 SQL 也为空」的历史样本重新归类为 empty_match。

这类样本的“结果一致”不携带信息（空集与空集恒相等），此前被计入正确率。
本脚本只读 prediction_summary_json / gold_summary_json 重新归类，不重跑任何 SQL。

用法：
    python backend/backfill_empty_match.py            # 试运行，只打印将要变更的数量
    python backend/backfill_empty_match.py --apply    # 实际写入
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.main import STATE_DB, open_state_db  # noqa: E402

EMPTY_NOTE = "空结果 vs 空金标（不计入正确率）：由 backfill_empty_match.py 从历史记录重新归类。"


def row_count(summary_json: str) -> int | None:
    """能确定行数就返回行数，否则返回 None（例如摘要缺失或执行失败）。"""
    try:
        summary = json.loads(summary_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(summary, dict) or not summary.get("ok"):
        return None
    count = summary.get("row_count")
    return count if isinstance(count, int) else None


def backfill(apply: bool) -> None:
    if not Path(STATE_DB).is_file():
        raise SystemExit(f"状态库不存在：{STATE_DB}")
    connection = open_state_db()

    rows = connection.execute(
        """
        SELECT id, run_id, split, evaluation_status, gold_sql, generated_sql,
               diagnosis_json, gold_summary_json, prediction_summary_json, diff_json
        FROM experiment_items
        WHERE evaluation_status IN ('correct', 'incorrect')
        """
    ).fetchall()

    changed: list[int] = []
    skipped_no_summary = 0
    by_split: dict[str, int] = {}
    by_previous: dict[str, int] = {}
    for row in rows:
        gold_count = row_count(row["gold_summary_json"])
        prediction_count = row_count(row["prediction_summary_json"])
        if gold_count is None or prediction_count is None:
            skipped_no_summary += 1
            continue
        if gold_count != 0 or prediction_count != 0:
            continue
        # 只有此前被判为 correct 的才需要改写；incorrect 本来就不计入正确率。
        if row["evaluation_status"] != "correct":
            continue
        changed.append(row["id"])
        by_split[row["split"]] = by_split.get(row["split"], 0) + 1
        by_previous[row["evaluation_status"]] = by_previous.get(row["evaluation_status"], 0) + 1

    print(f"扫描 {len(rows)} 条已判定样本，其中跳过 {skipped_no_summary} 条（摘要缺失或执行失败）")
    print(f"将重新归类为 empty_match：{len(changed)} 条")
    for split, count in sorted(by_split.items()):
        print(f"  {split}: {count}")

    if not changed:
        print("没有需要变更的记录。")
        connection.close()
        return
    if not apply:
        print("\n这是试运行。加 --apply 才会写入。")
        connection.close()
        return

    for start in range(0, len(changed), 500):
        batch = changed[start:start + 500]
        placeholders = ",".join("?" * len(batch))
        connection.execute(
            f"""
            UPDATE experiment_items
            SET evaluation_status = 'empty_match',
                diagnosis_json = json_insert(
                    CASE WHEN json_valid(diagnosis_json) THEN diagnosis_json ELSE '[]' END,
                    '$[#]',
                    json_object('category', 'empty_match', 'title', '空结果与空金标（不计入正确率）', 'evidence', ?)
                )
            WHERE id IN ({placeholders})
            """,
            (EMPTY_NOTE, *batch),
        )
    connection.commit()
    remaining = connection.execute(
        "SELECT COUNT(*) FROM experiment_items WHERE evaluation_status = 'empty_match'"
    ).fetchone()[0]
    connection.close()
    print(f"已写入。当前 empty_match 共 {remaining} 条。")


if __name__ == "__main__":
    backfill("--apply" in sys.argv[1:])
