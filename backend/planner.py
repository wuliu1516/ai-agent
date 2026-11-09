"""查询计划器的提示词、计划校验与修复。

三个针对实测退化原因的机制：
1. 字面值逐字照抄：计划常把问题里的中文字面值改写成英文（英语→"English"），
   实测占 validation 退化的 1/3，且库里存的到底是不是英文无法预先知道。
2. DISTINCT 检查：丢 DISTINCT 是实测最多的单一退化原因。
3. 后置校验 + 一次定向修复：计划凭空加表、漏掉筛选条件时，不直接把它喂给生成模型。
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

QUERY_PLANNING_VERSION = "complex-query-plan-v4-validated"

QUERY_PLAN_SYSTEM_PROMPT = """你是一个 Text-to-SQL 查询规划器。根据用户问题和目标 Schema，整理生成 SQL 所需的结构化计划。
只输出一个 JSON 对象，字段必须包含 target_fields、filters、tables_and_joins、aggregation_grouping、sorting_limit、distinct、literal_values。
写 plan 时按下面规则：
1. 字面值逐字照抄：问题里出现的每个取值（国家名、课程名、颜色、大奖赛名称等）必须原样复制问题中的文字，
   不要翻译、不要改写。例如问题写“英语”就写“英语”，禁止写成“English”；问题写“摩纳哥大奖赛”就写“摩纳哥大奖赛”。
   数据库里实际存的是哪种语言无法预先确定，照抄问题原文才是唯一可验证的做法。
2. distinct 字段：当问题问“哪些/有没有/每个”这类可能产生重复行的列表时填 true，否则填 false。
3. tables_and_joins 只列回答问题真正需要的表；没有必要的表不要加进去。
4. filters 必须覆盖问题里的每一个取值和筛选条件；没有相关内容时用空数组。
严格依据问题和 Schema；不确定时明确写出不确定，不要臆造表、字段、值或关联关系。"""


class QueryPlan(BaseModel):
    target_fields: list[str] = Field(default_factory=list, description="SELECT 中只应出现的、回答问题所需的列或表达式")
    filters: list[str] = Field(default_factory=list, description="WHERE/HAVING 条件，字面值必须与问题原文逐字一致")
    tables_and_joins: list[str] = Field(default_factory=list, description="需要的表及 JOIN 条件（遵循外键）")
    aggregation_grouping: list[str] = Field(default_factory=list, description="聚合函数、GROUP BY；无则留空")
    sorting_limit: list[str] = Field(default_factory=list, description="ORDER BY 与 LIMIT；无则留空")
    distinct: bool = Field(default=False, description="结果是否需要去重（问题问“哪些/有没有”且涉及一对多连接时为 true）")
    literal_values: list[str] = Field(default_factory=list, description="问题中所有取值字面值，逐字照抄，禁止翻译")


QUERY_PLAN_KEYS: tuple[str, ...] = tuple(QueryPlan.model_fields)
LIST_KEYS: frozenset[str] = frozenset({
    "target_fields", "filters", "tables_and_joins", "aggregation_grouping", "sorting_limit", "literal_values",
})

PLAN_CITATION = "查询计划（由模型整理，仅供参考；若与问题或 Schema 不一致，以问题和 Schema 为准）"

# 明确的时间/日期写法不参与“字面值”检查（日期常出现在过滤条件里，且形式上简单）。
_DATE_LIKE = re.compile(r"^\d{4}(?:[-/年]\d{1,2})?(?:[-/月]\d{1,2}日?)?$|^\d+(?:\.\d+)?$")

# 值只认引号里的东西：模型写取值时一定带引号，而 SQL 关键字、表名字段名不会。
# 早期版本把未加引号的拉丁词也当取值，结果 COUNT/Students/distinct 全部误报，精度不可用。
_QUOTED_VALUE = re.compile(r"'([^']{1,40})'|\"([^\"]{1,40})\"")
_CJK_RUN = re.compile(r"[一-鿿]{2,8}")
_CJK = re.compile(r"[一-鿿]")


def quoted_values(text: str) -> set[str]:
    """文本里所有引号包裹的取值。"""
    values: set[str] = set()
    for match in _QUOTED_VALUE.finditer(text):
        token = (match.group(1) or match.group(2) or "").strip()
        if token and not _DATE_LIKE.match(token):
            values.add(token)
    return values


def plan_payload(plan: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in plan.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(item) for item in value)
    return " ".join(parts)


def cjk_terms(question: str) -> set[str]:
    """问题里的中文片段，用于检查计划是否漏了取值。召回有限，只当提示用。"""
    return {run for run in _CJK_RUN.findall(question)}


def find_translated_values(question: str, text: str) -> list[str]:
    """文本里出现了问题中没有的纯拉丁引号取值，而问题本身是中文——疑似把中文字面值翻译成了英文。

    只统计引号里的取值：模型写取值一定带引号，而 SQL 关键字、表名字段名不带引号，
    早期版本把未加引号的拉丁词也算进来，COUNT/Students/distinct 全部误报，精度不可用。
    """
    if not _CJK.search(question):
        return []
    question_folded = question.casefold()
    suspicious = [
        value
        for value in quoted_values(text)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9 _\-]*", value) and value.casefold() not in question_folded
    ]
    return sorted(set(suspicious))


def find_translated_literals(question: str, plan: dict[str, Any]) -> list[str]:
    return find_translated_values(question, plan_payload(plan))


def keeps_question_literals(question: str, sql: str) -> bool:
    """生成的 SQL 是否保住了问题里的取值：既没有把中文值换成外文，也没丢掉带引号的取值。"""
    if not sql:
        return True
    if find_translated_values(question, sql):
        return False
    folded = sql.casefold()
    return all(value.casefold() in folded for value in quoted_values(question))


def build_query_plan_prompt(user_prompt: str) -> str:
    context = re.sub(
        r"\s*Return one SQLite SELECT query only\.?\s*$",
        "",
        user_prompt,
        flags=re.IGNORECASE,
    ).rstrip()
    return (
        f"{context}\n\n"
        "请先整理查询计划，不要生成 SQL。"
        "字面值必须逐字照抄问题原文，禁止翻译成其他语言；"
        "target_fields 只列回答问题必需的列；filters 覆盖问题里的每个取值；"
        "tables_and_joins 只列必要时表；aggregation_grouping、sorting_limit 没有则留空数组。"
    )


def build_plan_repair_prompt(user_prompt: str, plan: dict[str, Any], errors: list[str]) -> str:
    return (
        f"{build_query_plan_prompt(user_prompt)}\n\n"
        f"上一版计划存在以下错误：\n"
        + "\n".join(f"- {error}" for error in errors[:8])
        + f"\n\n上一版计划：\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
        "请输出修正后的计划 JSON。"
    )


def normalize_query_plan(raw_plan: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_plan.strip(), flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict) and isinstance(parsed.get("query_plan"), dict):
                parsed = parsed["query_plan"]
            if isinstance(parsed, dict) and any(key in parsed for key in QUERY_PLAN_KEYS):
                fields: dict[str, Any] = {}
                for key in QUERY_PLAN_KEYS:
                    value = parsed.get(key, [] if key in LIST_KEYS else False)
                    if value is None:
                        value = [] if key in LIST_KEYS else False
                    if key == "distinct":
                        fields[key] = bool(value)
                    elif key in LIST_KEYS:
                        fields[key] = value if isinstance(value, list) else [str(value)]
                    else:
                        fields[key] = str(value)
                return fields
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"raw_plan": cleaned[:6_000]}


def plan_from_model(parsed: QueryPlan) -> dict[str, Any]:
    return parsed.model_dump()


def validate_plan(question: str, plan: dict[str, Any]) -> list[str]:
    """阻塞性问题：只用高精度信号（引号取值的翻译），避免误报触发无意义的修复调用。"""
    return [
        f"计划里的取值 '{value}' 疑似把问题中的中文取值翻译成了英文，必须逐字照抄问题原文"
        for value in find_translated_literals(question, plan)
    ]


def plan_warnings(question: str, plan: dict[str, Any]) -> list[str]:
    """提示性问题：问题里的中文片段没有出现在计划中。召回有限，只记录不阻塞。"""
    payload = plan_payload(plan)
    declared = {str(value).strip() for value in plan.get("literal_values", []) if str(value).strip()}
    missing = [term for term in sorted(cjk_terms(question)) if term not in payload and term not in declared]
    return [f"问题中的片段 '{term}' 未出现在计划里，请确认是否漏了筛选条件" for term in missing[:5]]


def build_prompt_with_query_plan(user_prompt: str, plan: dict[str, Any]) -> str:
    return (
        f"{user_prompt}\n\n"
        f"{PLAN_CITATION}：\n"
        f"{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
        "根据问题、目标 Schema 和上述计划生成 SQL；再次核对字段、过滤值、连接条件、聚合与排序，只输出一条 SQLite SELECT。"
    )
