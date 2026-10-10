from __future__ import annotations

import json
import re
from typing import Any


QUALITY_RUBRIC_VERSION = "text2sql-rubric-v1"
QUALITY_DIMENSIONS = {
    "intent_alignment": 0.40,
    "schema_grounding": 0.25,
    "query_logic": 0.25,
    "output_constraints": 0.10,
}

QUALITY_RUBRIC_SYSTEM_PROMPT = """你是 Text-to-SQL 质量评审员。请只根据问题、目标数据库 schema 和候选 SQL 评审 SQL 是否能回答问题。
数据库执行成功不代表语义正确；请独立核对选择字段、表关联、筛选条件、聚合、排序和返回范围。不要猜测未提供的 schema。
必须返回一个 JSON 对象，不要 Markdown，不要输出推理过程。JSON 格式：
{
  "dimensions": {
    "intent_alignment": 0,
    "schema_grounding": 0,
    "query_logic": 0,
    "output_constraints": 0
  },
  "issues": ["具体且可操作的问题；若没有则为空数组"],
  "feedback": "若需修改，给出简短的修复指引；若无需修改则为空字符串"
}
每个维度给 0 到 100 的整数分。分数含义：90-100 明确满足；70-89 基本合理但有不确定点；40-69 有明显缺陷；0-39 严重错误或无法回答问题。对证据不足的语义判断不要给高分。"""


def score_quality_prompt(
    *,
    db_id: str,
    question: str,
    schema: str,
    sql: str,
    execution: dict[str, Any],
) -> str:
    if execution.get("ok"):
        evidence: dict[str, Any] = {
            "status": "执行成功",
            "columns": execution.get("columns", []),
            "row_count": execution.get("row_count", 0),
            "rows_preview": execution.get("rows", [])[:5],
        }
    else:
        evidence = {
            "status": "未能完整执行",
            "category": execution.get("category", "unknown"),
            "error": execution.get("error", "未知执行错误"),
        }
    return (
        f"目标数据库：{db_id}\nSQL 方言：SQLite\n"
        f"目标 Schema：\n{schema}\n\n"
        f"用户问题：\n{question}\n\n"
        f"候选 SQL：\n{sql}\n\n"
        f"确定性执行检查：\n{json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "请依据四个维度评审候选 SQL。执行成功只证明可执行，不等于回答正确。"
    )


def parse_quality_response(content: str) -> dict[str, Any]:
    text = re.sub(r"^\`\`\`(?:json)?\s*|\s*\`\`\`$", "", content.strip(), flags=re.IGNORECASE)
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(text[index:])
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(parsed, dict):
        raise ValueError("评分模型没有返回 JSON 对象")

    raw_dimensions = parsed.get("dimensions")
    if not isinstance(raw_dimensions, dict):
        raw_score = parsed.get("score")
        if not isinstance(raw_score, (int, float)):
            raise ValueError("评分 JSON 缺少 dimensions 或 score")
        raw_dimensions = {key: raw_score for key in QUALITY_DIMENSIONS}

    dimensions: dict[str, int] = {}
    for key in QUALITY_DIMENSIONS:
        value = raw_dimensions.get(key)
        if not isinstance(value, (int, float)):
            raise ValueError(f"评分 JSON 缺少有效维度：{key}")
        dimensions[key] = max(0, min(100, round(value)))

    score = round(sum(dimensions[key] * weight for key, weight in QUALITY_DIMENSIONS.items()))
    issues = parsed.get("issues", [])
    if not isinstance(issues, list):
        issues = [str(issues)]
    return {
        "score": max(0, min(100, score)),
        "dimensions": dimensions,
        "issues": [str(issue).strip()[:400] for issue in issues if str(issue).strip()][:8],
        "feedback": str(parsed.get("feedback", "")).strip()[:1200],
    }


def result_sanity_issues(execution: dict[str, Any]) -> list[str]:
    """Soft signals on an executable query: empty result or all-NULL result."""
    if not execution.get("ok"):
        return []
    rows = execution.get("rows") or []
    if int(execution.get("row_count", len(rows)) or 0) == 0:
        return ["查询执行成功但返回 0 行：请核对过滤值（字面值/大小写/语言）、连接条件和是否过度限制。"]
    if rows and all(all(value is None for value in row) for row in rows):
        return ["查询返回的全是 NULL：请核对聚合对象、连接方向和所选字段。"]
    return []


def validation_checks(execution: dict[str, Any]) -> dict[str, Any]:
    if execution.get("ok"):
        issues = result_sanity_issues(execution)
        return {
            "read_only": {"status": "pass", "evidence": "只读 authorizer 允许该查询"},
            "syntax": {"status": "pass", "evidence": "SQLite 成功解析并执行查询"},
            "schema": {"status": "pass", "evidence": "查询引用的表和字段可解析"},
            "execution": {"status": "pass", "evidence": f"返回 {execution.get('row_count', 0)} 行"},
            "result_sanity": {"status": "warn" if issues else "pass", "evidence": issues[0] if issues else "结果非空"},
        }

    category = str(execution.get("category", "unknown"))
    error = str(execution.get("error", "未知执行错误"))
    normalized = error.casefold()
    if category == "readonly" or "not authorized" in normalized:
        read_only_status, syntax_status, schema_status = "fail", "unknown", "unknown"
    elif any(term in normalized for term in ("no such table", "no such column", "ambiguous column")):
        read_only_status, syntax_status, schema_status = "pass", "pass", "fail"
    elif any(term in normalized for term in ("syntax error", "unrecognized token", "incomplete input", 'near "')):
        read_only_status, syntax_status, schema_status = "pass", "fail", "unknown"
    else:
        read_only_status, syntax_status, schema_status = "pass", "unknown", "unknown"

    execution_status = "partial" if execution.get("too_many_rows") else "fail"
    return {
        "read_only": {"status": read_only_status, "evidence": error},
        "syntax": {"status": syntax_status, "evidence": error},
        "schema": {"status": schema_status, "evidence": error},
        "execution": {"status": execution_status, "evidence": error},
    }


def validation_assessment(execution: dict[str, Any], execution_summary: dict[str, Any]) -> dict[str, Any]:
    """Deterministic (no-LLM) assessment used when SQL validation is on but rubric scoring is off."""
    checks = validation_checks(execution)
    validation = {"checks": checks, "execution": execution_summary}
    if deterministic_hard_failure(execution):
        error = str(execution.get("error", "SQL 未通过确定性检查"))
        return {
            "score": 0,
            "dimensions": {},
            "issues": [error[:400]],
            "feedback": f"上一版 SQL 执行报错，请据此修复：{error[:800]}",
            "score_source": "validation",
            "validation": validation,
            "needs_repair": True,
        }
    issues = result_sanity_issues(execution)
    if issues:
        return {
            "score": 60,
            "dimensions": {},
            "issues": issues,
            "feedback": issues[0],
            "score_source": "validation",
            "validation": validation,
            "needs_repair": True,
        }
    return {
        "score": 100,
        "dimensions": {},
        "issues": [],
        "feedback": "",
        "score_source": "validation",
        "validation": validation,
        "needs_repair": False,
    }


def deterministic_hard_failure(execution: dict[str, Any]) -> bool:
    return not execution.get("ok") and execution.get("category") in {"readonly", "sql_error"}


def quality_discrimination_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    correct_scores: list[int] = []
    incorrect_scores: list[int] = []
    rubric_scores: list[tuple[int, str]] = []
    scored = 0
    retries = 0
    recovered = 0
    regressed = 0
    deterministic_rejects = 0
    judge_errors = 0
    for row in rows:
        assessment = row.get("assessment", {})
        if not isinstance(assessment, dict) or not assessment.get("enabled"):
            continue
        attempts = assessment.get("attempts", [])
        if not isinstance(attempts, list) or not attempts:
            continue
        initial = attempts[0]
        if not isinstance(initial, dict):
            continue
        initial_status = str(initial.get("benchmark_status", ""))
        initial_score = initial.get("score")
        scored += 1
        if initial.get("score_source") == "deterministic":
            deterministic_rejects += 1
        elif initial.get("score_source") == "llm_rubric" and isinstance(initial_score, (int, float)):
            rubric_scores.append((round(initial_score), initial_status))
            if initial_status == "correct":
                correct_scores.append(round(initial_score))
            elif initial_status == "incorrect":
                incorrect_scores.append(round(initial_score))
        if initial.get("grader_error"):
            judge_errors += 1
        if int(assessment.get("retries_used", 0) or 0) > 0:
            retries += 1
        if len(attempts) > 1:
            selected_number = int(assessment.get("selected_attempt", 1) or 1)
            selected_number = max(1, min(selected_number, len(attempts)))
            final_status = str(attempts[selected_number - 1].get("benchmark_status", ""))
            if initial_status == "incorrect" and final_status == "correct":
                recovered += 1
            elif initial_status == "correct" and final_status == "incorrect":
                regressed += 1

    auc: float | None = None
    if correct_scores and incorrect_scores:
        wins = 0.0
        negatives_seen = 0
        ordered = sorted(rubric_scores, key=lambda item: item[0])
        index = 0
        while index < len(ordered):
            score = ordered[index][0]
            end = index
            positives_at_score = 0
            negatives_at_score = 0
            while end < len(ordered) and ordered[end][0] == score:
                if ordered[end][1] == "correct":
                    positives_at_score += 1
                elif ordered[end][1] == "incorrect":
                    negatives_at_score += 1
                end += 1
            wins += positives_at_score * (negatives_seen + 0.5 * negatives_at_score)
            negatives_seen += negatives_at_score
            index = end
        auc = round(wins / (len(correct_scores) * len(incorrect_scores)), 4)

    return {
        "scored_samples": scored,
        "rubric_scored_samples": len(rubric_scores),
        "rubric_correct_samples": len(correct_scores),
        "rubric_incorrect_samples": len(incorrect_scores),
        "rubric_auc": auc,
        "rubric_mean_correct": round(sum(correct_scores) / len(correct_scores), 2) if correct_scores else None,
        "rubric_mean_incorrect": round(sum(incorrect_scores) / len(incorrect_scores), 2) if incorrect_scores else None,
        "deterministic_rejects": deterministic_rejects,
        "judge_errors": judge_errors,
        "retry_samples": retries,
        "retry_recovered": recovered,
        "retry_regressed": regressed,
    }
