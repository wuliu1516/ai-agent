from __future__ import annotations

import json
import hashlib
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import quote, urlsplit, urlunsplit

from langgraph.graph import END, START, StateGraph
import psutil
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from .fewshot import FewShotRetriever
from .llm import FatalModelServiceError, ModelGateway
from .quality import (
    QUALITY_RUBRIC_SYSTEM_PROMPT,
    QUALITY_RUBRIC_VERSION,
    deterministic_hard_failure,
    parse_quality_response,
    quality_discrimination_summary,
    score_quality_prompt,
    validation_assessment,
    validation_checks,
)


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "backend" / ".env", override=False)
load_dotenv(ROOT / ".env", override=False)
DATA_ROOT = ROOT / "data" / "CSpider"
DATABASE_ROOT = DATA_ROOT / "database"
SPLITS = {
    "development": {"label": "开发集", "description": "模型开发与调试"},
    "validation": {"label": "验证集", "description": "模型选择与验证"},
    "test": {"label": "测试集", "description": "基准评测"},
}
STATE_DB = Path(os.environ.get("NL2SQL_STATE_DB", ROOT / "backend" / "storage" / "workbench.sqlite3"))
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "NL2SQL_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://127.0.0.1:4173"
    ).split(",")
    if origin.strip()
]
API_BASE_URL = os.environ.get("NL2SQL_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
API_KEY = os.environ.get("NL2SQL_API_KEY", "").strip()
DEFAULT_MODEL = os.environ.get("NL2SQL_MODEL", "").strip()
DISABLE_THINKING = os.environ.get("NL2SQL_DISABLE_THINKING", "false").strip().lower() in {"1", "true", "yes", "on"}
MAX_EXPERIMENT_SAMPLES = 10_000
MAX_GENERATION_CONCURRENCY = 6
MAX_CONSECUTIVE_MODEL_FAILURES = 3
MAX_EVAL_ROWS = 5_000
MAX_EVAL_SECONDS = 8
RESULT_PREVIEW_ROWS = 12
FEWSHOT_EXAMPLE_COUNT = 2
FEWSHOT_RETRIEVAL_VERSION = "bm25-char-ngrams-v4-structural"
FEWSHOT_LEGACY_VERSION = "bm25-char-ngrams-v3-question-sql-only"
QUERY_PLANNING_VERSION = "complex-query-plan-v3-structured"
# 候选选择余量：修订版分数要高出这么多才替换初版，避免评分噪声把正确结果换掉。
SELECTION_MARGIN = 10
GENERATION_PARAMS = {"temperature": 0, "max_tokens": 2048}
QUERY_PLAN_SYSTEM_PROMPT = """你是一个 Text-to-SQL 查询规划器。根据用户问题和目标 Schema，整理生成 SQL 所需的结构化计划。
只输出一个 JSON 对象，字段必须包含 target_fields、filters、tables_and_joins、aggregation_grouping、sorting_limit。每个字段用简短字符串或字符串数组表达；没有相关内容时用空字符串或空数组。不要输出 SQL、Markdown 或解释。严格依据问题和 Schema；不确定时明确写出不确定，不要臆造表、字段、值或关联关系。"""
BASELINE_SYSTEM_PROMPT = """你是一个严谨的 Text-to-SQL 助手。请根据用户的问题和给出的数据库结构生成 SQL。
必须遵循以下规则：
1. 只使用提供的表名和字段名，不要臆造数据库结构。
2. 表之间需要关联时，优先遵循 schema 中给出的外键关系。
3. 使用 SQLite 方言，生成一条只读 SELECT 查询（允许 WITH ... SELECT）。
4. 最终答复只能是一条 SQL 语句；不要输出推理过程、分析说明或任何 <think> / </think> 标记。
5. SELECT 只包含回答问题所需的字段，不要额外添加姓名、ID 或描述列。
6. 不要使用 Markdown 代码围栏，不要添加“SQL:”等前后缀文字，也不要输出多条语句。"""


def public_table_count(schema: dict[str, Any]) -> int:
    return sum(
        1
        for table_name in schema.get("table_names_original", [])
        if not str(table_name).casefold().startswith("sqlite_")
    )


def load_benchmark() -> tuple[
    dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], dict[str, dict[str, int]]
]:
    records_by_split: dict[str, list[dict[str, Any]]] = {}
    gold_file_audit: dict[str, dict[str, int]] = {}
    for split in SPLITS:
        records_path = DATA_ROOT / f"{split}.json"
        gold_path = DATA_ROOT / f"{split}_gold.sql"
        if not records_path.is_file() or not gold_path.is_file():
            raise RuntimeError(f"缺少数据集文件：{records_path.name} 或 {gold_path.name}")

        records = json.loads(records_path.read_text(encoding="utf-8"))
        gold_lines = gold_path.read_text(encoding="utf-8-sig").splitlines()
        if len(records) != len(gold_lines):
            raise RuntimeError(
                f"{split} 样本有 {len(records)} 条，但金标 SQL 有 {len(gold_lines)} 行"
            )

        seen_fingerprints: Counter[str] = Counter()
        aligned_database_ids = 0
        for index, record in enumerate(records):
            gold_query = str(record.get("query", "")).strip()
            if not gold_query:
                raise RuntimeError(f"{split} 第 {index + 1} 条样本缺少 query 金标 SQL")
            _, separator, gold_db_id = gold_lines[index].rstrip("\r\n").rpartition("\t")
            if separator and gold_db_id.strip() == record["db_id"]:
                aligned_database_ids += 1
            canonical_record = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(canonical_record.encode("utf-8")).hexdigest()
            duplicate_number = seen_fingerprints[fingerprint]
            seen_fingerprints[fingerprint] += 1
            record["_index"] = index
            record["_sample_id"] = hashlib.sha256(
                f"{split}:{fingerprint}:{duplicate_number}".encode("utf-8")
            ).hexdigest()
            record["_gold_sql"] = gold_query.strip()
        records_by_split[split] = records
        gold_file_audit[split] = {
            "rows": len(gold_lines),
            "database_id_matches": aligned_database_ids,
            "database_id_mismatches": len(gold_lines) - aligned_database_ids,
        }

    schema_rows = json.loads((DATA_ROOT / "tables.json").read_text(encoding="utf-8"))
    schemas_by_database = {row["db_id"]: row for row in schema_rows}
    return records_by_split, schemas_by_database, gold_file_audit


RECORDS, SCHEMAS, GOLD_FILE_AUDIT = load_benchmark()
DB_SAMPLE_COUNTS: dict[str, Counter[str]] = defaultdict(Counter)
SAMPLES_BY_FINGERPRINT: dict[tuple[str, str], dict[str, Any]] = {}
for split, records in RECORDS.items():
    for record in records:
        DB_SAMPLE_COUNTS[record["db_id"]][split] += 1
        SAMPLES_BY_FINGERPRINT[(split, record["_sample_id"])] = record


_STATE_SCHEMA_LOCK = threading.Lock()
_STATE_SCHEMA_READY = False


def initialize_state_schema() -> None:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STATE_DB, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN EXCLUSIVE")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            split TEXT NOT NULL,
            sample_index INTEGER NOT NULL,
            sample_fingerprint TEXT NOT NULL DEFAULT '',
            db_id TEXT NOT NULL DEFAULT '',
            question TEXT NOT NULL DEFAULT '',
            generated_sql TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            latency_ms REAL,
            prompt TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(generated_results)").fetchall()
    }
    for name, definition in (
        ("sample_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("db_id", "TEXT NOT NULL DEFAULT ''"),
        ("question", "TEXT NOT NULL DEFAULT ''"),
        ("prompt", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE generated_results ADD COLUMN {name} {definition}")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_results_sample ON generated_results(split, sample_index, id DESC)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_results_fingerprint ON generated_results(split, sample_fingerprint, id DESC)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_results_created ON generated_results(created_at DESC)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_runs (
            id TEXT PRIMARY KEY,
            split TEXT NOT NULL,
            model TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            parameters_json TEXT NOT NULL DEFAULT '{}',
            sample_limit INTEGER NOT NULL,
            sample_seed INTEGER NOT NULL DEFAULT 42,
            worker_pid INTEGER NOT NULL DEFAULT 0,
            worker_started_at REAL NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(experiment_runs)").fetchall()}
    if "parameters_json" not in run_columns:
        connection.execute("ALTER TABLE experiment_runs ADD COLUMN parameters_json TEXT NOT NULL DEFAULT '{}' ")
    if "worker_pid" not in run_columns:
        connection.execute("ALTER TABLE experiment_runs ADD COLUMN worker_pid INTEGER NOT NULL DEFAULT 0")
    if "worker_started_at" not in run_columns:
        connection.execute("ALTER TABLE experiment_runs ADD COLUMN worker_started_at REAL NOT NULL DEFAULT 0")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            split TEXT NOT NULL,
            sample_index INTEGER NOT NULL,
            sample_fingerprint TEXT NOT NULL,
            db_id TEXT NOT NULL,
            question TEXT NOT NULL,
            gold_sql TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '',
            generated_sql TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            latency_ms REAL,
            item_status TEXT NOT NULL DEFAULT 'queued',
            evaluation_status TEXT NOT NULL DEFAULT 'pending',
            generation_error TEXT NOT NULL DEFAULT '',
            diagnosis_json TEXT NOT NULL DEFAULT '[]',
            prediction_summary_json TEXT NOT NULL DEFAULT '{}',
            gold_summary_json TEXT NOT NULL DEFAULT '{}',
            diff_json TEXT NOT NULL DEFAULT '{}',
            quality_assessment_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE(run_id, sample_index)
        )
        """
    )
    item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(experiment_items)").fetchall()}
    if "quality_assessment_json" not in item_columns:
        connection.execute("ALTER TABLE experiment_items ADD COLUMN quality_assessment_json TEXT NOT NULL DEFAULT '{}'")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_experiment_runs_created ON experiment_runs(created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_experiment_items_run ON experiment_items(run_id, sample_index)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_experiment_items_status ON experiment_items(run_id, evaluation_status)")
    connection.commit()
    connection.close()


def open_state_db() -> sqlite3.Connection:
    global _STATE_SCHEMA_READY
    with _STATE_SCHEMA_LOCK:
        if not _STATE_SCHEMA_READY:
            initialize_state_schema()
            _STATE_SCHEMA_READY = True
    connection = sqlite3.connect(STATE_DB, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def state_connection():
    connection = open_state_db()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def result_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "split": row["split"],
        "sample_index": row["sample_index"],
        "sample_fingerprint": row["sample_fingerprint"],
        "db_id": row["db_id"],
        "question": row["question"],
        "generated_sql": row["generated_sql"],
        "model": row["model"],
        "run_id": row["run_id"],
        "latency_ms": row["latency_ms"],
        "prompt": row["prompt"] if "prompt" in row.keys() else "",
        "created_at": row["created_at"],
    }


def result_count() -> int:
    with state_connection() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM generated_results").fetchone()[0])


def latest_results(split: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    with state_connection() as connection:
        if split:
            rows = connection.execute(
                """
                SELECT r.* FROM generated_results r
                JOIN (SELECT MAX(id) AS id FROM generated_results WHERE split = ? GROUP BY sample_fingerprint) latest
                  ON r.id = latest.id
                """,
                (split,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT r.* FROM generated_results r
                JOIN (SELECT MAX(id) AS id FROM generated_results GROUP BY split, sample_fingerprint) latest
                  ON r.id = latest.id
                """
            ).fetchall()
    return {(row["split"], row["sample_fingerprint"]): result_from_row(row) for row in rows}


def sample_payload(split: str, record: dict[str, Any], latest: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "split": split,
        "index": record["_index"],
        "sample_no": record["_index"] + 1,
        "sample_id": record["_sample_id"],
        "db_id": record["db_id"],
        "question": record.get("question", ""),
        "query": record.get("query", ""),
        "gold_sql": record["_gold_sql"],
        "gold_sql_source": "CSpider JSON query 字段",
        "latest_result": latest,
    }


def validate_sample(split: str, sample_index: int) -> dict[str, Any]:
    if split not in RECORDS:
        raise HTTPException(status_code=404, detail="未找到该数据集")
    if sample_index < 0 or sample_index >= len(RECORDS[split]):
        raise HTTPException(status_code=404, detail="未找到该样本")
    return RECORDS[split][sample_index]


def database_file(db_id: str) -> Path:
    if db_id not in SCHEMAS:
        raise HTTPException(status_code=404, detail="未找到该数据库")
    path = (DATABASE_ROOT / db_id / f"{db_id}.sqlite").resolve()
    try:
        path.relative_to(DATABASE_ROOT.resolve())
    except ValueError as error:
        raise HTTPException(status_code=404, detail="未找到该数据库") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="该数据库缺少 SQLite 文件")
    return path


def open_source_db(db_id: str) -> sqlite3.Connection:
    path = database_file(db_id)
    uri_path = quote(path.as_posix(), safe="/:")
    try:
        connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as error:
        raise HTTPException(status_code=500, detail=f"无法读取数据库：{error}") from error


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def list_sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class GeneratedResultInput(BaseModel):
    generated_sql: str = Field(min_length=1, max_length=500_000)
    model: str = Field(default="", max_length=250)
    run_id: str = Field(default="", max_length=250)
    sample_id: str | None = Field(default=None, max_length=64)
    latency_ms: float | None = Field(default=None, ge=0)
    prompt: str = Field(default="", max_length=1_000_000)


class ExperimentInput(BaseModel):
    split: str = Field(pattern="^(development|validation|test)$")
    model: str = Field(min_length=1, max_length=250)
    sample_limit: int = Field(default=20, ge=0, le=MAX_EXPERIMENT_SAMPLES)
    sample_seed: int = Field(default=42, ge=0, le=2_147_483_647)
    use_few_shot: bool = True
    few_shot_mode: str = Field(default="structural", pattern="^(structural|legacy)$")
    few_shot_examples: int = Field(default=2, ge=1, le=6)
    use_target_schema: bool = True
    use_query_planning: bool = False
    use_sql_validation: bool = False
    use_quality_scoring: bool = False
    use_langgraph: bool = True
    quality_score_threshold: int = Field(default=70, ge=0, le=100)
    quality_retry_limit: int = Field(default=1, ge=0, le=2)
    quality_judge_model: str = Field(default="", max_length=250)
    optimization_note: str = Field(default="", max_length=2_000)
    system_prompt: str = Field(default=BASELINE_SYSTEM_PROMPT, min_length=1, max_length=30_000)


def compact_schema_prompt(db_id: str) -> str:
    schema = SCHEMAS[db_id]
    table_names = schema.get("table_names_original", [])
    column_names = schema.get("column_names_original", [])
    column_types = schema.get("column_types", [])
    primary_keys = set(schema.get("primary_keys", []))
    foreign_keys = schema.get("foreign_keys", [])
    foreign_key_columns = {column_index for pair in foreign_keys for column_index in pair}
    table_lines: list[str] = []
    for table_index, table_name in enumerate(table_names):
        fields = []
        for column_index, (owner_index, column_name) in enumerate(column_names):
            if owner_index != table_index or column_name == "*":
                continue
            column_type = column_types[column_index] if column_index < len(column_types) else ""
            tags = []
            if column_index in primary_keys:
                tags.append("PK")
            if column_index in foreign_key_columns:
                tags.append("FK")
            suffix = f" [{','.join(tags)}]" if tags else ""
            fields.append(f"{column_name} {column_type or 'unknown'}{suffix}")
        table_lines.append(f"{table_name}({', '.join(fields)})")
    relationships = []
    for source_index, target_index in foreign_keys:
        if source_index >= len(column_names) or target_index >= len(column_names):
            continue
        source_table_index, source_column = column_names[source_index]
        target_table_index, target_column = column_names[target_index]
        if source_table_index < 0 or target_table_index < 0:
            continue
        relationships.append(
            f"{table_names[source_table_index]}.{source_column} = {table_names[target_table_index]}.{target_column}"
        )
    schema_text = "\n".join(table_lines)
    if relationships:
        schema_text += "\nForeign keys: " + "; ".join(relationships)
    return schema_text


def build_prompt(
    db_id: str,
    question: str,
    few_shot_count: int = 0,
    include_target_schema: bool = True,
    few_shot_mode: str = "structural",
) -> tuple[str, str]:
    demonstrations = (
        FEWSHOT_RETRIEVER.retrieve(
            db_id, question, few_shot_count, index_by_identifiers=few_shot_mode == "legacy"
        )
        if few_shot_count
        else []
    )
    prompt_sections: list[str] = []
    if demonstrations:
        examples = [
            "以下 Few-shot 示例来自 development 集，每条只包含自然语言问题和 SQL，不附示例 Schema。"
            "请学习问题到 SQL 的映射；目标 SQL 必须依据本题目标 Schema 编写，"
            "不要照搬示例里的表名、字段名或具体字面值。"
        ]
        for number, record in enumerate(demonstrations, start=1):
            examples.append(
                f"示例 {number}\n"
                f"问题：{record.get('question', '')}\n"
                f"SQL：{record.get('_gold_sql', '')}"
            )
        prompt_sections.append("\n\n".join(examples))
    target_context = f"Database: {db_id}\n" if include_target_schema else ""
    schema_context = f"Schema:\n{compact_schema_prompt(db_id)}\n\n" if include_target_schema else ""
    prompt_sections.append(
        f"{target_context}SQL dialect: SQLite\n"
        f"{schema_context}Question:\n{question}\n\n"
        "Return one SQLite SELECT query only."
    )
    user_prompt = "\n\n".join(prompt_sections)
    return BASELINE_SYSTEM_PROMPT, user_prompt


COMPLEX_QUERY_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("聚合或分组", ("平均", "总计", "总和", "总共", "一共有", "总数", "每个", "每种", "每年", "分别", "average", "total", "sum", "how many", "number of", "per ", "each ", "group by")),
    ("比较或排名", ("最高", "最低", "最大", "最小", "最多", "最少", "排名", "前几", "第几", "超过", "至少", "至多", "之间", "highest", "lowest", "most", "least", "top ", "rank", "more than", "less than", "at least", "at most", "between")),
    ("多条件组合", ("并且", "同时", "以及", "或者", "both ", "either ", " and ", " or ")),
    ("嵌套或排除", ("除了", "不包括", "不包含", "没有任何", "没有", "至少一个", "all of", "none of", "without", "except", "not any")),
)
# 聚合/分组、比较/排名、嵌套/排除任一命中就值得规划；只有"多条件组合"这类弱信号需要两条线索同时命中。
STRONG_COMPLEX_REASONS = {"聚合或分组", "比较或排名", "嵌套或排除"}
MIN_COMPLEX_REASONS = 2


def complex_query_reasons(question: str) -> list[str]:
    normalized = f" {question.casefold()} "
    return [
        reason
        for reason, cues in COMPLEX_QUERY_CUES
        if any(cue in normalized for cue in cues)
    ]


def should_plan(reasons: list[str]) -> bool:
    return len(reasons) >= MIN_COMPLEX_REASONS or bool(STRONG_COMPLEX_REASONS & set(reasons))


class QueryPlan(BaseModel):
    target_fields: list[str] = Field(default_factory=list, description="SELECT 中只应出现的、回答问题所需的列或表达式")
    filters: list[str] = Field(default_factory=list, description="WHERE/HAVING 条件，字面值必须与问题原文一致")
    tables_and_joins: list[str] = Field(default_factory=list, description="需要的表及 JOIN 条件（遵循外键）")
    aggregation_grouping: list[str] = Field(default_factory=list, description="聚合函数、GROUP BY；无则留空")
    sorting_limit: list[str] = Field(default_factory=list, description="ORDER BY 与 LIMIT；无则留空")


QUERY_PLAN_KEYS = tuple(QueryPlan.model_fields)


def build_query_plan_prompt(user_prompt: str) -> str:
    context = re.sub(
        r"\s*Return one SQLite SELECT query only\.?\s*$",
        "",
        user_prompt,
        flags=re.IGNORECASE,
    ).rstrip()
    return (
        f"{context}\n\n"
        "请先整理查询计划，不要生成 SQL。字段含义：target_fields 只列回答问题必需的列；filters 中的字面值照抄问题原文；"
        "tables_and_joins 只列必要的表并遵循外键；aggregation_grouping、sorting_limit 没有则留空数组。"
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
                    value = parsed.get(key, [])
                    if value is None:
                        value = []
                    fields[key] = value if isinstance(value, (str, list)) else str(value)
                return fields
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"raw_plan": cleaned[:6_000]}


def build_prompt_with_query_plan(user_prompt: str, query_plan: dict[str, Any]) -> str:
    return (
        f"{user_prompt}\n\n"
        "查询计划（由模型整理，仅供参考；若与问题或 Schema 不一致，以问题和 Schema 为准）：\n"
        f"{json.dumps(query_plan, ensure_ascii=False, indent=2)}\n\n"
        "根据问题、目标 Schema 和上述计划生成 SQL；再次核对字段、过滤值、连接条件、聚合与排序，只输出一条 SQLite SELECT。"
    )


MODEL_GATEWAY = ModelGateway(
    API_BASE_URL,
    API_KEY,
    disable_thinking=DISABLE_THINKING,
    temperature=GENERATION_PARAMS["temperature"],
    max_tokens=GENERATION_PARAMS["max_tokens"],
)


def call_chat_completion(
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int | None = None,
) -> str:
    return MODEL_GATEWAY.complete(model, system_prompt, user_prompt, max_tokens=max_tokens)


def readonly_authorizer(action: int, arg1: str | None, arg2: str | None, database: str | None, trigger: str | None) -> int:
    return sqlite3.SQLITE_DENY if action in READONLY_BLOCKED_ACTIONS else sqlite3.SQLITE_OK


READONLY_BLOCKED_ACTIONS = {
    getattr(sqlite3, name)
    for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_ALTER_TABLE",
        "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_PRAGMA",
        "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT",
    )
    if hasattr(sqlite3, name)
}


def open_fewshot_source_db(db_id: str) -> sqlite3.Connection:
    try:
        return open_source_db(db_id)
    except HTTPException as error:
        raise sqlite3.OperationalError(str(error.detail)) from error


FEWSHOT_RETRIEVER = FewShotRetriever(
    RECORDS.get("development", []),
    SCHEMAS,
    open_fewshot_source_db,
    readonly_authorizer,
)


def execute_for_evaluation(db_id: str, sql: str) -> dict[str, Any]:
    first_token = re.sub(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)*", "", sql, flags=re.DOTALL).casefold()
    if not first_token.startswith(("select", "with")):
        return {"ok": False, "error": "只允许执行 SELECT 或 WITH 查询", "category": "readonly"}
    try:
        connection = open_source_db(db_id)
    except HTTPException as error:
        return {"ok": False, "error": error.detail, "category": "database"}
    started = time.monotonic()
    deadline_hit = False

    def progress() -> int:
        nonlocal deadline_hit
        if time.monotonic() - started >= MAX_EVAL_SECONDS:
            deadline_hit = True
            return 1
        return 0

    try:
        connection.set_authorizer(readonly_authorizer)
        connection.set_progress_handler(progress, 1000)
        cursor = connection.execute(sql)
        columns = [description[0] for description in (cursor.description or [])]
        fetched = cursor.fetchmany(MAX_EVAL_ROWS + 1)
        if len(fetched) > MAX_EVAL_ROWS:
            return {
                "ok": False,
                "too_many_rows": True,
                "error": f"查询结果超过 {MAX_EVAL_ROWS} 行上限，未完成比较",
                "category": "row_limit",
                "columns": columns,
            }
        rows = [[json_value(value) for value in row] for row in fetched]
        return {
            "ok": True,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except sqlite3.Error as error:
        message = str(error)
        if deadline_hit or "interrupted" in message.casefold():
            return {"ok": False, "timed_out": True, "error": f"查询超过 {MAX_EVAL_SECONDS} 秒时间上限", "category": "timeout"}
        if "not authorized" in message.casefold():
            return {"ok": False, "error": "查询触发只读安全限制", "category": "readonly"}
        return {"ok": False, "error": message, "category": "sql_error"}
    finally:
        connection.close()


def result_summary(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        return {key: value for key, value in result.items() if key not in {"rows"}}
    return {
        "ok": True,
        "columns": result["columns"],
        "row_count": result["row_count"],
        "rows": result["rows"][:RESULT_PREVIEW_ROWS],
        "truncated_preview": result["row_count"] > RESULT_PREVIEW_ROWS,
        "elapsed_ms": result["elapsed_ms"],
    }


def normalized_sql_clause(sql: str, clause: str, stop_clauses: tuple[str, ...]) -> str:
    match = re.search(rf"\b{clause}\b(.*?)(?=\b(?:{'|'.join(stop_clauses)})\b|$)", sql, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip().casefold()


def has_top_level_order_by(sql: str) -> bool:
    visible: list[str] = []
    index = 0
    depth = 0
    quote_end = ""
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if quote_end:
            if char == quote_end:
                if next_char == quote_end and quote_end in {"'", '"', "`"}:
                    visible.extend("  ")
                    index += 2
                    continue
                quote_end = ""
            visible.append(" ")
            index += 1
            continue
        if char == "-" and next_char == "-":
            while index < len(sql) and sql[index] != "\n":
                visible.append(" ")
                index += 1
            continue
        if char == "/" and next_char == "*":
            visible.extend("  ")
            index += 2
            while index < len(sql) and not (sql[index] == "*" and index + 1 < len(sql) and sql[index + 1] == "/"):
                visible.append(" ")
                index += 1
            if index < len(sql):
                visible.extend("  ")
                index += 2
            continue
        if char in {"'", '"', "`", "["}:
            quote_end = "]" if char == "[" else char
            visible.append(" ")
            index += 1
            continue
        if char == "(":
            depth += 1
            visible.append(" ")
        elif char == ")":
            depth = max(0, depth - 1)
            visible.append(" ")
        else:
            visible.append(char if depth == 0 else " ")
        index += 1
    return bool(re.search(r"\bORDER\s+BY\b", "".join(visible), re.IGNORECASE))


def execution_error_diagnosis(error: str, category: str) -> dict[str, str]:
    normalized = error.casefold()
    if category == "readonly" or "not authorized" in normalized:
        title, reason_category = "生成 SQL 违反只读查询约束", "readonly"
    elif "no such table" in normalized:
        title, reason_category = "可能引用了不存在的表", "schema_table"
    elif "no such column" in normalized or "ambiguous column" in normalized:
        title, reason_category = "可能引用了错误或有歧义的字段", "schema_column"
    elif "syntax error" in normalized or "unrecognized token" in normalized or "incomplete input" in normalized:
        title, reason_category = "SQL 语法错误", "syntax"
    elif "aggregate" in normalized or "group by" in normalized:
        title, reason_category = "可能存在聚合或分组用法问题", "aggregation"
    else:
        title, reason_category = "生成 SQL 无法执行", category
    return {"category": reason_category, "title": title, "evidence": error}


def mismatch_reasons(predicted_sql: str, gold_sql: str, difference: dict[str, Any]) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    pred_tables = {name.casefold() for name in re.findall(r"\b(?:FROM|JOIN)\s+[`\"\[]?([\w.]+)", predicted_sql, re.IGNORECASE)}
    gold_tables = {name.casefold() for name in re.findall(r"\b(?:FROM|JOIN)\s+[`\"\[]?([\w.]+)", gold_sql, re.IGNORECASE)}
    if pred_tables != gold_tables:
        reasons.append({"category": "tables_joins", "title": "可能涉及表或关联路径", "evidence": f"生成 SQL 表集合：{', '.join(sorted(pred_tables)) or '未识别'}；金标表集合：{', '.join(sorted(gold_tables)) or '未识别'}"})
    pred_select = normalized_sql_clause(predicted_sql, "SELECT", ("FROM",))
    gold_select = normalized_sql_clause(gold_sql, "SELECT", ("FROM",))
    if pred_select != gold_select:
        reasons.append({"category": "select", "title": "可能涉及选择字段或聚合表达式", "evidence": f"生成：{pred_select[:180] or '未识别'}；金标：{gold_select[:180] or '未识别'}"})
    pred_where = normalized_sql_clause(predicted_sql, "WHERE", ("GROUP", "HAVING", "ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT"))
    gold_where = normalized_sql_clause(gold_sql, "WHERE", ("GROUP", "HAVING", "ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT"))
    if pred_where != gold_where:
        reasons.append({"category": "where", "title": "可能涉及筛选条件", "evidence": f"生成：{pred_where[:180] or '无 WHERE'}；金标：{gold_where[:180] or '无 WHERE'}"})
    pred_group = normalized_sql_clause(predicted_sql, "GROUP BY", ("HAVING", "ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT"))
    gold_group = normalized_sql_clause(gold_sql, "GROUP BY", ("HAVING", "ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT"))
    pred_having = normalized_sql_clause(predicted_sql, "HAVING", ("ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT"))
    gold_having = normalized_sql_clause(gold_sql, "HAVING", ("ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT"))
    if pred_group != gold_group or pred_having != gold_having:
        reasons.append({"category": "aggregation", "title": "可能涉及分组或聚合条件", "evidence": f"生成 GROUP BY：{pred_group or '无'}；金标：{gold_group or '无'}"})
    pred_order = normalized_sql_clause(predicted_sql, "ORDER BY", ("LIMIT", "UNION", "INTERSECT", "EXCEPT"))
    gold_order = normalized_sql_clause(gold_sql, "ORDER BY", ("LIMIT", "UNION", "INTERSECT", "EXCEPT"))
    pred_limit = normalized_sql_clause(predicted_sql, "LIMIT", ("OFFSET", "UNION", "INTERSECT", "EXCEPT"))
    gold_limit = normalized_sql_clause(gold_sql, "LIMIT", ("OFFSET", "UNION", "INTERSECT", "EXCEPT"))
    if pred_order != gold_order or pred_limit != gold_limit:
        reasons.append({"category": "order_limit", "title": "可能涉及排序或返回条数", "evidence": f"生成 ORDER/LIMIT：{pred_order or '无排序'} / {pred_limit or '无限制'}；金标：{gold_order or '无排序'} / {gold_limit or '无限制'}"})
    gold_ordered = has_top_level_order_by(gold_sql)
    row_match = difference.get("row_match", {})
    if not reasons:
        reasons.append({"category": "semantic", "title": "结果不同，原因需人工确认", "evidence": f"生成 SQL 返回 {difference.get('predicted_row_count', 0)} 行，金标 SQL 返回 {difference.get('gold_row_count', 0)} 行；请结合首个结果差异核对。"})
    reasons.append({
        "category": "evaluation_evidence",
        "title": "执行结果不一致",
        "evidence": f"按{'有序行' if gold_ordered else '忽略顺序的多重集'}比较；首个差异：{json.dumps(row_match, ensure_ascii=False)[:360]}",
    })
    return reasons


def evaluate_generated_sql(
    db_id: str,
    predicted_sql: str,
    gold_sql: str,
    prediction: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, str]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    prediction = prediction if prediction is not None else execute_for_evaluation(db_id, predicted_sql)
    if not prediction.get("ok"):
        title = "生成 SQL 超时" if prediction.get("timed_out") else "生成 SQL 超过结果行数上限" if prediction.get("too_many_rows") else "生成 SQL 无法执行"
        if prediction.get("category") in {"sql_error", "readonly"}:
            return "incorrect", [execution_error_diagnosis(prediction.get("error", "未知执行错误"), prediction.get("category", "sql_error"))], result_summary(prediction), {}, {"error": prediction.get("error", "")}
        return "unjudged", [
            {"category": prediction.get("category", "sql_error"), "title": title, "evidence": prediction.get("error", "未知执行错误")}
        ], result_summary(prediction), {}, {"error": prediction.get("error", "")}
    reference = execute_for_evaluation(db_id, gold_sql)
    if not reference.get("ok"):
        return "unjudged", [{"category": "gold_sql", "title": "金标 SQL 无法执行，当前样本无法判定", "evidence": reference.get("error", "未知金标执行错误")}], result_summary(prediction), result_summary(reference), {"error": reference.get("error", "")}
    ordered = has_top_level_order_by(gold_sql)
    predicted_rows = [tuple(row) for row in prediction["rows"]]
    gold_rows = [tuple(row) for row in reference["rows"]]
    if ordered:
        equal = predicted_rows == gold_rows
    else:
        equal = Counter(predicted_rows) == Counter(gold_rows)
    if equal:
        note = "预测与金标的查询结果一致。此判定只说明在当前数据库实例上执行结果一致。"
        return "correct", [{"category": "match", "title": "执行结果一致", "evidence": f"{len(predicted_rows)} 行；{'保留排序' if ordered else '忽略行顺序并保留重复行'}比较。{note}"}], result_summary(prediction), result_summary(reference), {"ordered": ordered, "predicted_row_count": len(predicted_rows), "gold_row_count": len(gold_rows), "row_match": {"equal": True}}
    row_match: dict[str, Any]
    if ordered:
        first_diff = next((index for index, pair in enumerate(zip(predicted_rows, gold_rows)) if pair[0] != pair[1]), min(len(predicted_rows), len(gold_rows)))
        row_match = {"row_index": first_diff, "generated": prediction["rows"][first_diff] if first_diff < len(prediction["rows"]) else "<无此行>", "gold": reference["rows"][first_diff] if first_diff < len(reference["rows"]) else "<无此行>"}
    else:
        pred_counter = Counter(predicted_rows)
        gold_counter = Counter(gold_rows)
        generated_only = list((pred_counter - gold_counter).elements())
        gold_only = list((gold_counter - pred_counter).elements())
        row_match = {"generated_only": json_value(generated_only[0]) if generated_only else None, "gold_only": json_value(gold_only[0]) if gold_only else None}
    difference = {"ordered": ordered, "predicted_row_count": len(predicted_rows), "gold_row_count": len(gold_rows), "row_match": row_match}
    return "incorrect", mismatch_reasons(predicted_sql, gold_sql, difference), result_summary(prediction), result_summary(reference), difference


def score_generated_sql(
    model: str,
    db_id: str,
    question: str,
    sql: str,
    execution: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    execution = execution if execution is not None else execute_for_evaluation(db_id, sql)
    checks = validation_checks(execution)
    validation = {
        "checks": checks,
        "execution": result_summary(execution),
    }
    if deterministic_hard_failure(execution):
        error = str(execution.get("error", "SQL 未通过确定性检查"))
        return (
            {
                "score": 0,
                "dimensions": {
                    "intent_alignment": 0,
                    "schema_grounding": 0,
                    "query_logic": 0,
                    "output_constraints": 0,
                },
                "issues": [error[:400]],
                "feedback": f"先修复 SQLite 校验错误：{error[:800]}",
                "score_source": "deterministic",
                "validation": validation,
            },
            execution,
        )

    started = time.monotonic()
    try:
        raw_score = call_chat_completion(
            model,
            QUALITY_RUBRIC_SYSTEM_PROMPT,
            score_quality_prompt(
                db_id=db_id,
                question=question,
                schema=compact_schema_prompt(db_id),
                sql=sql,
                execution=execution,
            ),
            max_tokens=700,
        )
        score = parse_quality_response(raw_score)
        score.update(
            {
                "score_source": "llm_rubric",
                "validation": validation,
                "grader_latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
        return score, execution
    except Exception as error:
        return (
            {
                "score": None,
                "dimensions": {},
                "issues": [],
                "feedback": "",
                "score_source": "llm_rubric",
                "validation": validation,
                "grader_error": f"{type(error).__name__}: {str(error)[:800]}",
                "grader_latency_ms": round((time.monotonic() - started) * 1000, 2),
            },
            execution,
        )


def build_quality_retry_prompt(
    original_prompt: str,
    previous_sql: str,
    assessment: dict[str, Any],
    attempt_number: int,
) -> str:
    feedback = str(assessment.get("feedback", "")).strip()
    issues = assessment.get("issues", [])
    if issues:
        feedback = (feedback + "\n" if feedback else "") + "\n".join(f"- {issue}" for issue in issues)
    if assessment.get("grader_error"):
        feedback = f"评分服务异常，无法提供语义反馈：{assessment['grader_error']}"
    if not feedback:
        feedback = "SQL 质量分低于通过阈值，请重新检查问题语义、schema、关联条件、筛选与聚合逻辑。"
    return (
        f"{original_prompt}\n\n"
        f"上一版 SQL（第 {attempt_number - 1} 次尝试）：\n{previous_sql}\n\n"
        f"质量反馈（评分 {assessment.get('score', '未知')}/100）：\n{feedback}\n\n"
        "请根据反馈修订 SQL。仍然只输出一条 SQLite 只读 SELECT 查询，不要附解释或 Markdown。"
    )


app = FastAPI(title="CSpider NL2SQL Workbench API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def process_is_running(pid: int, started_at: float) -> bool:
    if pid <= 0 or started_at <= 0:
        return False
    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - started_at) < 0.001
    except psutil.AccessDenied:
        return True
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def mark_queued_items_not_run(connection: sqlite3.Connection, run_id: str, evidence: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    diagnosis = json.dumps(
        [{"category": "not_run", "title": "该样本未运行", "evidence": evidence}],
        ensure_ascii=False,
    )
    connection.execute(
        """
        UPDATE experiment_items
        SET item_status = 'not_run', evaluation_status = 'not_run', diagnosis_json = ?, completed_at = ?
        WHERE run_id = ? AND item_status = 'queued'
        """,
        (diagnosis, now, run_id),
    )


def recover_interrupted_experiments() -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    interruption = "后端进程已退出，运行中样本被中断；请创建新批次重新运行。"
    with state_connection() as connection:
        active_runs = connection.execute(
            "SELECT id, worker_pid, worker_started_at FROM experiment_runs WHERE status IN ('queued', 'running')"
        ).fetchall()
        for run in active_runs:
            if process_is_running(int(run["worker_pid"] or 0), float(run["worker_started_at"] or 0)):
                continue
            connection.execute(
                """
                UPDATE experiment_items
                SET item_status = 'interrupted', evaluation_status = 'interrupted',
                    generation_error = ?, diagnosis_json = ?, completed_at = ?
                WHERE run_id = ? AND item_status = 'generating'
                """,
                (
                    interruption,
                    json.dumps([{"category": "interrupted", "title": "后端重启导致运行中断", "evidence": interruption}], ensure_ascii=False),
                    now,
                    run["id"],
                ),
            )
            mark_queued_items_not_run(connection, run["id"], "后端重启前该样本尚未开始；请创建新批次重新运行。")
            connection.execute(
                """
                UPDATE experiment_runs
                SET status = 'failed', error_message = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (interruption, now, run["id"]),
            )


@app.on_event("startup")
def recover_interrupted_runs_on_startup() -> None:
    recover_interrupted_experiments()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "CSpider NL2SQL Workbench"}


def parse_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def experiment_summary(run_id: str, include_prompt: bool = False) -> dict[str, Any] | None:
    with state_connection() as connection:
        run = connection.execute("SELECT * FROM experiment_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return None
        counts = connection.execute(
            "SELECT evaluation_status, COUNT(*) AS count FROM experiment_items WHERE run_id = ? GROUP BY evaluation_status",
            (run_id,),
        ).fetchall()
        item_states = connection.execute(
            "SELECT item_status, COUNT(*) AS count FROM experiment_items WHERE run_id = ? GROUP BY item_status",
            (run_id,),
        ).fetchall()
        parameters = parse_json(run["parameters_json"], GENERATION_PARAMS)
        quality_rows = (
            connection.execute(
                "SELECT evaluation_status, quality_assessment_json FROM experiment_items WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            if parameters.get("quality_scoring")
            else []
        )
    status_counts = {row["evaluation_status"]: row["count"] for row in counts}
    item_state_counts = {row["item_status"]: row["count"] for row in item_states}
    correct = status_counts.get("correct", 0)
    incorrect = status_counts.get("incorrect", 0)
    evaluated = correct + incorrect
    payload = {
        "run_id": run["id"],
        "split": run["split"],
        "model": run["model"],
        "prompt_version": run["prompt_version"],
        "parameters": parameters,
        "sample_limit": run["sample_limit"],
        "full_dataset": run["total_count"] == len(RECORDS.get(run["split"], [])),
        "sample_seed": run["sample_seed"],
        "total_count": run["total_count"],
        "status": run["status"],
        "cancel_requested": bool(run["cancel_requested"]),
        "error_message": run["error_message"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "counts": {
            "correct": correct,
            "incorrect": incorrect,
            "unjudged": status_counts.get("unjudged", 0),
            "generation_failed": status_counts.get("generation_failed", 0),
            "interrupted": status_counts.get("interrupted", 0),
            "not_run": status_counts.get("not_run", 0),
            "pending": status_counts.get("pending", 0),
            "queued": item_state_counts.get("queued", 0),
            "generating": item_state_counts.get("generating", 0),
            "evaluated": evaluated,
        },
        "accuracy": round(correct / evaluated, 4) if evaluated else None,
    }
    if parameters.get("quality_scoring"):
        payload["quality_summary"] = quality_discrimination_summary(
            [
                {
                    "evaluation_status": row["evaluation_status"],
                    "assessment": parse_json(row["quality_assessment_json"], {}),
                }
                for row in quality_rows
            ]
        )
    if include_prompt:
        payload["system_prompt"] = run["system_prompt"]
    return payload


def update_experiment_item(
    item_id: int,
    *,
    item_status: str,
    evaluation_status: str,
    generated_sql: str = "",
    model: str = "",
    latency_ms: float | None = None,
    generation_error: str = "",
    diagnosis: list[dict[str, str]] | None = None,
    prediction_summary: dict[str, Any] | None = None,
    gold_summary: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    quality_assessment: dict[str, Any] | None = None,
    prompt: str | None = None,
) -> None:
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with state_connection() as connection:
        connection.execute(
            """
            UPDATE experiment_items
            SET item_status = ?, evaluation_status = ?, generated_sql = ?, model = ?, latency_ms = ?,
                generation_error = ?, prompt = COALESCE(?, prompt), diagnosis_json = ?, prediction_summary_json = ?, gold_summary_json = ?,
                diff_json = ?, quality_assessment_json = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                item_status,
                evaluation_status,
                generated_sql,
                model,
                latency_ms,
                generation_error,
                prompt,
                json.dumps(diagnosis or [], ensure_ascii=False),
                json.dumps(prediction_summary or {}, ensure_ascii=False),
                json.dumps(gold_summary or {}, ensure_ascii=False),
                json.dumps(diff or {}, ensure_ascii=False),
                json.dumps(quality_assessment or {}, ensure_ascii=False),
                completed_at,
                item_id,
            ),
        )


class SQLReviewGraphState(TypedDict, total=False):
    run: Any
    item: Any
    model: str
    system_prompt: str
    user_prompt: str
    generation_prompt: str
    current_prompt: str
    db_id: str
    question: str
    gold_sql: str
    quality_enabled: bool
    validation_enabled: bool
    scoring_enabled: bool
    query_planning_enabled: bool
    query_plan: dict[str, Any]
    retry_limit: int
    attempt_limit: int
    score_threshold: int
    judge_model: str
    attempt_number: int
    candidate_sql: str
    execution: dict[str, Any]
    assessment: dict[str, Any]
    candidates: list[dict[str, Any]]
    quality_attempts: list[dict[str, Any]]
    retry_generation_errors: list[str]
    generation_time_ms: float
    grader_time_ms: float
    started_at: float
    generation_error: str
    fatal: bool
    result: dict[str, Any]


def _sql_review_route_start(state: SQLReviewGraphState) -> str:
    query_plan = state.get("query_plan", {})
    return "plan" if state.get("query_planning_enabled") and query_plan.get("triggered") else "generate"


def _sql_review_plan(state: SQLReviewGraphState) -> dict[str, Any]:
    started = time.monotonic()
    planner_prompt = build_query_plan_prompt(state["user_prompt"])
    snapshot = {"system": QUERY_PLAN_SYSTEM_PROMPT, "user": planner_prompt}
    try:
        plan = MODEL_GATEWAY.structured(
            state["model"],
            QueryPlan,
            QUERY_PLAN_SYSTEM_PROMPT,
            planner_prompt,
            from_model=lambda parsed: parsed.model_dump(),
            from_text=normalize_query_plan,
            max_tokens=700,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        query_plan = {
            **state.get("query_plan", {}),
            "status": "generated_unstructured" if "raw_plan" in plan else "generated",
            "plan": plan,
            "latency_ms": elapsed_ms,
            "planner_prompt": snapshot,
        }
        # 非结构化的原始计划容易误导生成，直接回退到无计划提示词。
        generation_prompt = (
            state["user_prompt"] if "raw_plan" in plan else build_prompt_with_query_plan(state["user_prompt"], plan)
        )
    except Exception as error:
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        query_plan = {
            **state.get("query_plan", {}),
            "status": "failed_fallback",
            "error": f"{type(error).__name__}: {str(error)[:800]}",
            "latency_ms": elapsed_ms,
            "planner_prompt": snapshot,
        }
        generation_prompt = state["user_prompt"]
    return {
        "query_plan": query_plan,
        "generation_prompt": generation_prompt,
        "current_prompt": generation_prompt,
        "generation_time_ms": state.get("generation_time_ms", 0.0) + elapsed_ms,
    }


def _sql_review_generate(state: SQLReviewGraphState) -> dict[str, Any]:
    started = time.monotonic()
    try:
        candidate_sql = call_chat_completion(
            state["model"], state["system_prompt"], state["current_prompt"]
        )
    except Exception as error:
        message = f"{type(error).__name__}: {str(error)}"[:1200]
        updates: dict[str, Any] = {
            "generation_error": message,
            "fatal": isinstance(error, FatalModelServiceError),
        }
        if state.get("candidates"):
            updates["retry_generation_errors"] = [
                *state.get("retry_generation_errors", []), message
            ]
            updates["fatal"] = False
        return updates
    return {
        "candidate_sql": candidate_sql,
        "generation_error": "",
        "generation_time_ms": state.get("generation_time_ms", 0.0)
        + (time.monotonic() - started) * 1000,
    }


def _sql_review_validate(state: SQLReviewGraphState) -> dict[str, Any]:
    return {"execution": execute_for_evaluation(state["db_id"], state["candidate_sql"])}


def _sql_review_score(state: SQLReviewGraphState) -> dict[str, Any]:
    candidate_sql = state["candidate_sql"]
    execution = state["execution"]
    assessment: dict[str, Any] = {}
    quality_attempts = list(state.get("quality_attempts", []))
    grader_time_ms = state.get("grader_time_ms", 0.0)
    if state["scoring_enabled"]:
        grader_started = time.monotonic()
        assessment, execution = score_generated_sql(
            state["judge_model"],
            state["db_id"],
            state["question"],
            candidate_sql,
            execution=execution,
        )
        grader_time_ms += float(assessment.get("grader_latency_ms", 0) or 0)
        assessment["grading_wall_time_ms"] = round((time.monotonic() - grader_started) * 1000, 2)
    elif state["validation_enabled"]:
        assessment = validation_assessment(execution, result_summary(execution))
    if assessment:
        assessment["attempt"] = state["attempt_number"]
        assessment["sql"] = candidate_sql
        quality_attempts.append(assessment)

    candidate = {
        "sql": candidate_sql,
        "execution": execution,
        "assessment": assessment,
        "attempt_number": state["attempt_number"],
    }
    return {
        "execution": execution,
        "assessment": assessment,
        "candidates": [*state.get("candidates", []), candidate],
        "quality_attempts": quality_attempts,
        "grader_time_ms": grader_time_ms,
    }


def _sql_review_after_generate(state: SQLReviewGraphState) -> str:
    return "finalize" if state.get("generation_error") else "validate"


def _sql_review_after_score(state: SQLReviewGraphState) -> str:
    if not state["quality_enabled"] or state["attempt_number"] >= state["attempt_limit"]:
        return "finalize"
    assessment = state.get("assessment", {})
    if assessment.get("score_source") == "validation":
        return "prepare_retry" if assessment.get("needs_repair") else "finalize"
    score = assessment.get("score")
    if score is None or score >= state["score_threshold"]:
        return "finalize"
    return "prepare_retry"


def _sql_review_prepare_retry(state: SQLReviewGraphState) -> dict[str, Any]:
    candidate = state["candidates"][-1]
    next_attempt = state["attempt_number"] + 1
    return {
        "current_prompt": build_quality_retry_prompt(
            state.get("generation_prompt", state["user_prompt"]),
            candidate["sql"],
            candidate["assessment"],
            next_attempt,
        ),
        "attempt_number": next_attempt,
        "assessment": {},
        "candidate_sql": "",
        "generation_error": "",
    }


def _sql_review_finalize(state: SQLReviewGraphState) -> dict[str, Any]:
    total_started = state["started_at"]
    candidates = list(state.get("candidates", []))
    if not candidates:
        message = state.get("generation_error") or "模型没有生成候选 SQL"
        return {
            "result": {
                "item_status": "generation_failed",
                "evaluation_status": "generation_failed",
                "generated_sql": "",
                "latency_ms": round((time.monotonic() - total_started) * 1000, 2),
                "generation_error": message,
                "diagnosis": [
                    {"category": "generation", "title": "模型生成失败", "evidence": message}
                ],
                "quality_assessment": {},
                "query_plan": state.get("query_plan", {}),
                "generation_prompt": state.get("generation_prompt", state["user_prompt"]),
                "fatal": state.get("fatal", False),
            }
        }

    def candidate_rank(candidate: dict[str, Any]) -> tuple[int, int]:
        execution_result = candidate["execution"]
        if execution_result.get("ok"):
            validity_rank = 2
        elif deterministic_hard_failure(execution_result):
            validity_rank = 0
        else:
            validity_rank = 1
        raw_score = candidate["assessment"].get("score")
        score_rank = int(raw_score) if isinstance(raw_score, (int, float)) else -1
        return validity_rank, score_rank

    # 保守选择：只有明显更好时才采用修订版，避免评分噪声把原本正确的 SQL 换掉。
    selected = candidates[0]
    for candidate in candidates[1:]:
        best_rank, best_score = candidate_rank(selected)
        rank, score = candidate_rank(candidate)
        if rank > best_rank or (rank == best_rank and score >= best_score + SELECTION_MARGIN):
            selected = candidate
    quality_assessment: dict[str, Any] = {}
    retry_generation_errors = state.get("retry_generation_errors", [])
    if state["quality_enabled"]:
        for candidate in candidates:
            benchmark_result = evaluate_generated_sql(
                state["db_id"],
                candidate["sql"],
                state["gold_sql"],
                prediction=candidate["execution"],
            )
            candidate["benchmark_result"] = benchmark_result
            candidate["assessment"]["benchmark_status"] = benchmark_result[0]
        selected_index = candidates.index(selected)
        quality_attempts = state.get("quality_attempts", [])
        quality_assessment = {
            "enabled": True,
            "rubric_version": QUALITY_RUBRIC_VERSION,
            "threshold": state["score_threshold"],
            "retry_limit": state["retry_limit"],
            "retries_used": max(0, len(candidates) - 1 + len(retry_generation_errors)),
            "judge_model": state["judge_model"],
            "selected_attempt": selected_index + 1,
            "score": selected["assessment"].get("score"),
            "score_source": selected["assessment"].get("score_source", "unknown"),
            "initial_score": quality_attempts[0].get("score") if quality_attempts else None,
            "initial_score_source": quality_attempts[0].get("score_source", "unknown")
            if quality_attempts
            else "unknown",
            "grader_latency_ms": round(state.get("grader_time_ms", 0.0), 2),
            "attempts": quality_attempts,
            "retry_generation_errors": retry_generation_errors,
        }

    generated_sql = selected["sql"]
    try:
        if state["quality_enabled"]:
            evaluation_status, diagnosis, prediction, reference, diff = selected[
                "benchmark_result"
            ]
        else:
            evaluation_status, diagnosis, prediction, reference, diff = evaluate_generated_sql(
                state["db_id"],
                generated_sql,
                state["gold_sql"],
                prediction=selected["execution"],
            )
    except Exception as error:
        message = f"评测过程出错：{str(error)[:600]}"
        evaluation_status = "unjudged"
        diagnosis = [
            {"category": "evaluation", "title": "评测过程异常，需人工确认", "evidence": message}
        ]
        prediction, reference, diff = {}, {}, {"error": message}

    return {
        "result": {
            "item_status": "completed",
            "evaluation_status": evaluation_status,
            "generated_sql": generated_sql,
            "latency_ms": round(state.get("generation_time_ms", 0.0), 2),
            "generation_error": "",
            "diagnosis": diagnosis,
            "prediction_summary": prediction,
            "gold_summary": reference,
            "diff": diff,
            "quality_assessment": quality_assessment,
            "query_plan": state.get("query_plan", {}),
            "generation_prompt": state.get("generation_prompt", state["user_prompt"]),
            "fatal": False,
        }
    }


@lru_cache(maxsize=1)
def get_sql_review_graph():
    graph = StateGraph(SQLReviewGraphState)
    graph.add_node("plan", _sql_review_plan)
    graph.add_node("generate", _sql_review_generate)
    graph.add_node("validate", _sql_review_validate)
    graph.add_node("score", _sql_review_score)
    graph.add_node("prepare_retry", _sql_review_prepare_retry)
    graph.add_node("finalize", _sql_review_finalize)
    graph.add_conditional_edges(
        START,
        _sql_review_route_start,
        {"plan": "plan", "generate": "generate"},
    )
    graph.add_edge("plan", "generate")
    graph.add_conditional_edges(
        "generate",
        _sql_review_after_generate,
        {"validate": "validate", "finalize": "finalize"},
    )
    graph.add_edge("validate", "score")
    graph.add_conditional_edges(
        "score",
        _sql_review_after_score,
        {"prepare_retry": "prepare_retry", "finalize": "finalize"},
    )
    graph.add_edge("prepare_retry", "generate")
    graph.add_edge("finalize", END)
    return graph.compile()


def run_sql_review_linear(state: SQLReviewGraphState) -> SQLReviewGraphState:
    """Same nodes as the StateGraph, driven by a plain loop (use_langgraph=false)."""
    state = dict(state)

    def step(node: Any) -> None:
        state.update(node(state))

    if _sql_review_route_start(state) == "plan":
        step(_sql_review_plan)
    while True:
        step(_sql_review_generate)
        if _sql_review_after_generate(state) == "finalize":
            break
        step(_sql_review_validate)
        step(_sql_review_score)
        if _sql_review_after_score(state) == "finalize":
            break
        step(_sql_review_prepare_retry)
    step(_sql_review_finalize)
    return state  # type: ignore[return-value]


def generate_and_evaluate_experiment_item(run: sqlite3.Row, item: sqlite3.Row) -> dict[str, Any]:
    total_started = time.monotonic()
    try:
        prompt_payload = parse_json(item["prompt"], {})
        messages = prompt_payload.get("messages", [])
        system_prompt = next(
            (message.get("content", "") for message in messages if message.get("role") == "system"),
            run["system_prompt"],
        )
        user_prompt = next(
            (message.get("content", "") for message in messages if message.get("role") == "user"),
            "",
        )
    except Exception as error:
        message = f"{type(error).__name__}: {str(error)}"[:1200]
        return {
            "item_status": "generation_failed",
            "evaluation_status": "generation_failed",
            "generated_sql": "",
            "latency_ms": round((time.monotonic() - total_started) * 1000, 2),
            "generation_error": message,
            "diagnosis": [{"category": "generation", "title": "模型生成失败", "evidence": message}],
            "quality_assessment": {},
            "fatal": isinstance(error, FatalModelServiceError),
        }

    parameters = parse_json(run["parameters_json"], {})
    scoring_enabled = bool(parameters.get("quality_scoring"))
    validation_enabled = bool(parameters.get("sql_validation")) or scoring_enabled
    quality_enabled = validation_enabled
    query_planning_enabled = bool(parameters.get("query_planning"))
    query_plan_reasons = complex_query_reasons(item["question"])
    plan_triggered = query_planning_enabled and should_plan(query_plan_reasons)
    query_plan = {
        "enabled": query_planning_enabled,
        "triggered": plan_triggered,
        "status": "pending" if plan_triggered else "not_complex" if query_planning_enabled else "disabled",
        "trigger_reasons": query_plan_reasons,
    }
    retry_limit = int(parameters.get("quality_retry_limit", 1)) if quality_enabled else 0
    score_threshold = int(parameters.get("quality_score_threshold", 70))
    judge_model = str(parameters.get("quality_judge_model", "")).strip() or run["model"]
    initial_state: SQLReviewGraphState = {
        **{
            "run": run,
            "item": item,
            "model": run["model"],
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "generation_prompt": user_prompt,
            "current_prompt": user_prompt,
            "db_id": item["db_id"],
            "question": item["question"],
            "gold_sql": item["gold_sql"],
            "quality_enabled": quality_enabled,
            "validation_enabled": validation_enabled,
            "scoring_enabled": scoring_enabled,
            "query_planning_enabled": query_planning_enabled,
            "query_plan": query_plan,
            "retry_limit": retry_limit,
            "attempt_limit": retry_limit + 1 if quality_enabled else 1,
            "score_threshold": score_threshold,
            "judge_model": judge_model,
            "attempt_number": 1,
            "candidates": [],
            "quality_attempts": [],
            "retry_generation_errors": [],
            "generation_time_ms": 0.0,
            "grader_time_ms": 0.0,
            "started_at": total_started,
            "fatal": False,
        }
    }
    if parameters.get("langgraph", True):
        state = get_sql_review_graph().invoke(initial_state)
    else:
        state = run_sql_review_linear(initial_state)
    return state["result"]

def persist_experiment_item_result(
    run_id: str, run: sqlite3.Row, item: sqlite3.Row, result: dict[str, Any]
) -> str | None:
    prompt_payload = parse_json(item["prompt"], {})
    if not isinstance(prompt_payload, dict):
        prompt_payload = {}
    messages = prompt_payload.get("messages", [])
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user":
                message["content"] = result.get("generation_prompt", message.get("content", ""))
                break
    prompt_payload["query_plan"] = result.get("query_plan", {})
    prompt_snapshot = json.dumps(prompt_payload, ensure_ascii=False)
    if result["item_status"] == "generation_failed":
        update_experiment_item(
            item["id"], item_status="generation_failed", evaluation_status="generation_failed",
            model=run["model"], latency_ms=result["latency_ms"],
            generation_error=result["generation_error"], diagnosis=result["diagnosis"],
            quality_assessment=result.get("quality_assessment", {}),
            prompt=prompt_snapshot,
        )
        return None

    diagnosis = result["diagnosis"]
    generated_sql = result["generated_sql"]
    latency_ms = result["latency_ms"]
    evaluation_status = result["evaluation_status"]
    prediction = result["prediction_summary"]
    reference = result["gold_summary"]
    diff = result["diff"]
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with state_connection() as connection:
            connection.execute(
                """
                INSERT INTO generated_results
                    (split, sample_index, sample_fingerprint, db_id, question, generated_sql, model, run_id, latency_ms, prompt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["split"], item["sample_index"], item["sample_fingerprint"], item["db_id"],
                    item["question"], generated_sql, run["model"], run_id, latency_ms, prompt_snapshot, created_at,
                ),
            )
            update_cursor = connection.execute(
                """
                UPDATE experiment_items
                SET prompt = ?, item_status = 'completed', evaluation_status = ?, generated_sql = ?, model = ?, latency_ms = ?,
                    generation_error = '', diagnosis_json = ?, prediction_summary_json = ?, gold_summary_json = ?,
                    diff_json = ?, quality_assessment_json = ?, completed_at = ?
                WHERE id = ? AND item_status = 'generating'
                """,
                (
                    prompt_snapshot,
                    evaluation_status,
                    generated_sql,
                    run["model"],
                    latency_ms,
                    json.dumps(diagnosis, ensure_ascii=False),
                    json.dumps(prediction, ensure_ascii=False),
                    json.dumps(reference, ensure_ascii=False),
                    json.dumps(diff, ensure_ascii=False),
                    json.dumps(result.get("quality_assessment", {}), ensure_ascii=False),
                    created_at,
                    item["id"],
                ),
            )
            if update_cursor.rowcount != 1:
                raise RuntimeError("批次样本记录不存在，结果未能原子保存")
    except Exception as error:
        message = f"生成成功，但生成历史与批次结果的原子保存失败：{str(error)[:300]}"
        storage_diagnosis = diagnosis + [{"category": "storage", "title": "结果保存失败", "evidence": message}]
        update_experiment_item(
            item["id"], item_status="interrupted", evaluation_status="interrupted",
            generated_sql=generated_sql, model=run["model"], latency_ms=latency_ms,
            generation_error=message, diagnosis=storage_diagnosis,
            prediction_summary=prediction, gold_summary=reference, diff=diff,
            quality_assessment=result.get("quality_assessment", {}),
            prompt=prompt_snapshot,
        )
        return message
    return None


def run_experiment(run_id: str) -> None:
    executor = ThreadPoolExecutor(max_workers=MAX_GENERATION_CONCURRENCY, thread_name_prefix="nl2sql-run")
    futures: dict[Any, tuple[sqlite3.Row, sqlite3.Row]] = {}
    fatal_error = ""
    consecutive_model_failures = 0
    try:
        with state_connection() as connection:
            connection.execute(
                "UPDATE experiment_runs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id),
            )
        while True:
            submissions: list[tuple[sqlite3.Row, sqlite3.Row]] = []
            with state_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute("SELECT * FROM experiment_runs WHERE id = ?", (run_id,)).fetchone()
                if not run:
                    return
                if run["status"] not in {"queued", "running"}:
                    return
                stopping = bool(fatal_error or run["cancel_requested"])
                if not stopping:
                    available_slots = MAX_GENERATION_CONCURRENCY - len(futures)
                    if available_slots > 0:
                        queued_items = connection.execute(
                            "SELECT * FROM experiment_items WHERE run_id = ? AND item_status = 'queued' ORDER BY sample_index LIMIT ?",
                            (run_id, available_slots),
                        ).fetchall()
                        for item in queued_items:
                            updated = connection.execute(
                                "UPDATE experiment_items SET item_status = 'generating' WHERE id = ? AND item_status = 'queued'",
                                (item["id"],),
                            )
                            if updated.rowcount == 1:
                                submissions.append((run, item))

                if not futures and not submissions:
                    if fatal_error:
                        terminal_status = "failed"
                        mark_queued_items_not_run(connection, run_id, "模型服务持续失败，批次已停止；此样本尚未运行。")
                    elif run["cancel_requested"]:
                        terminal_status = "cancelled"
                        mark_queued_items_not_run(connection, run_id, "批次已停止；停止时尚未开始的样本不会继续运行。")
                    else:
                        terminal_status = "completed"
                    connection.execute(
                        "UPDATE experiment_runs SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                        (terminal_status, fatal_error if terminal_status == "failed" else "", datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id),
                    )
                    break

            for run_snapshot, item in submissions:
                future = executor.submit(generate_and_evaluate_experiment_item, run_snapshot, item)
                futures[future] = (run_snapshot, item)

            if futures:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                completed_results: list[dict[str, Any]] = []
                for future in completed:
                    run_snapshot, item = futures.pop(future)
                    result = future.result()
                    persistence_error = persist_experiment_item_result(run_id, run_snapshot, item, result)
                    completed_results.append({"result": result, "persistence_error": persistence_error})
                    if persistence_error and not fatal_error:
                        fatal_error = persistence_error
                if not fatal_error and completed_results:
                    failed_results = [entry["result"] for entry in completed_results if entry["result"]["fatal"]]
                    if failed_results and len(failed_results) == len(completed_results):
                        consecutive_model_failures += len(failed_results)
                        if consecutive_model_failures >= MAX_CONSECUTIVE_MODEL_FAILURES:
                            fatal_error = (
                                f"模型服务连续 {consecutive_model_failures} 个样本运行失败；"
                                f"最后错误：{failed_results[-1]['generation_error']}"
                            )
                    else:
                        consecutive_model_failures = 0
    except Exception as error:
        executor.shutdown(wait=True)
        message = f"批次运行异常：{str(error)[:800]}"
        with state_connection() as connection:
            interruption = "批次异常停止，当前样本未能完成评测保存。"
            connection.execute(
                """
                UPDATE experiment_items
                SET item_status = 'interrupted', evaluation_status = 'interrupted', generation_error = ?,
                    diagnosis_json = ?, completed_at = ?
                WHERE run_id = ? AND item_status = 'generating'
                """,
                (
                    interruption,
                    json.dumps([{"category": "interrupted", "title": "当前样本运行中断", "evidence": message}], ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    run_id,
                ),
            )
            mark_queued_items_not_run(connection, run_id, "批次因后端错误停止；此样本尚未运行。")
            connection.execute(
                "UPDATE experiment_runs SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                (message, datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id),
            )
    finally:
        executor.shutdown(wait=True)


@app.get("/api/experiment-config")
def get_experiment_config() -> dict[str, Any]:
    configuration_error = ""
    try:
        parsed_url = urlsplit(API_BASE_URL)
        port = parsed_url.port
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            configuration_error = "NL2SQL_API_BASE_URL 必须是包含主机名的 HTTP 或 HTTPS URL。"
        elif parsed_url.query or parsed_url.fragment:
            configuration_error = "NL2SQL_API_BASE_URL 不应包含 query string 或 fragment。"
    except ValueError:
        parsed_url = urlsplit("")
        port = None
        configuration_error = "NL2SQL_API_BASE_URL 格式无效，请检查主机名和端口。"
    hostname = parsed_url.hostname or ""
    safe_netloc = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if port is not None:
        safe_netloc += f":{port}"
    safe_base_url = urlunsplit((parsed_url.scheme, safe_netloc, parsed_url.path, "", ""))
    return {
        "provider_ready": not configuration_error,
        "configuration_error": configuration_error,
        "base_url": safe_base_url,
        "default_model": DEFAULT_MODEL,
        "default_system_prompt": BASELINE_SYSTEM_PROMPT,
        "max_samples": MAX_EXPERIMENT_SAMPLES,
        "max_concurrency": MAX_GENERATION_CONCURRENCY,
        "few_shot_example_count": FEWSHOT_EXAMPLE_COUNT,
        "few_shot_source_split": "development",
        "query_planning_version": QUERY_PLANNING_VERSION,
    }


@app.post("/api/experiments", status_code=202)
def create_experiment(body: ExperimentInput, background_tasks: BackgroundTasks) -> dict[str, Any]:
    recover_interrupted_experiments()
    try:
        parsed_url = urlsplit(API_BASE_URL)
        _ = parsed_url.port
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError("URL 必须包含 HTTP/HTTPS scheme 与主机名")
        if parsed_url.query or parsed_url.fragment:
            raise ValueError("URL 不应包含 query string 或 fragment")
    except ValueError as error:
        raise HTTPException(status_code=503, detail=f"NL2SQL_API_BASE_URL 无效：{error}") from error
    model = body.model.strip()
    system_prompt = body.system_prompt.strip()
    quality_judge_model = body.quality_judge_model.strip() or model
    use_validation = body.use_sql_validation or body.use_quality_scoring
    if not model:
        raise HTTPException(status_code=422, detail="模型 ID 不能为空")
    if not system_prompt:
        raise HTTPException(status_code=422, detail="System Prompt 不能为空")
    total_samples = len(RECORDS[body.split])
    few_shot_enabled = body.use_few_shot and body.split != "development"
    few_shot_count = body.few_shot_examples if few_shot_enabled else 0
    few_shot_retrieval_version = (
        FEWSHOT_LEGACY_VERSION if body.few_shot_mode == "legacy" else FEWSHOT_RETRIEVAL_VERSION
    ) if few_shot_enabled else ""
    target_schema_enabled = body.use_target_schema
    if body.sample_limit == 0 or body.sample_limit >= total_samples:
        sample_indices = list(range(total_samples))
    else:
        sample_indices = sorted(random.Random(body.sample_seed).sample(range(total_samples), body.sample_limit))
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prompt_fingerprint = json.dumps(
        {
            "system_prompt": system_prompt,
            "few_shot_enabled": few_shot_enabled,
            "few_shot_count": few_shot_count,
            "few_shot_source_split": "development" if few_shot_enabled else "",
            "few_shot_retrieval_version": few_shot_retrieval_version,
            "few_shot_mode": body.few_shot_mode if few_shot_enabled else "",
            "target_schema_enabled": target_schema_enabled,
            "query_planning": body.use_query_planning,
            "query_planning_version": QUERY_PLANNING_VERSION if body.use_query_planning else "",
            "sql_validation": use_validation,
            "langgraph": body.use_langgraph,
            "quality_scoring": body.use_quality_scoring,
            "quality_rubric_version": QUALITY_RUBRIC_VERSION if body.use_quality_scoring else "",
            "quality_score_threshold": body.quality_score_threshold if body.use_quality_scoring else None,
            "quality_retry_limit": body.quality_retry_limit if use_validation else 0,
            "quality_judge_model": quality_judge_model if body.use_quality_scoring else "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    prompt_version = "sha256:" + hashlib.sha256(prompt_fingerprint.encode("utf-8")).hexdigest()[:12]
    run_parameters = {
        **GENERATION_PARAMS,
        "concurrency": MAX_GENERATION_CONCURRENCY,
        "sql_review_engine": "langgraph-stategraph-v1" if body.use_langgraph else "linear-loop-v1",
        "langgraph": body.use_langgraph,
        "sql_validation": use_validation,
        "few_shot_requested": body.use_few_shot,
        "few_shot_enabled": few_shot_enabled,
        "few_shot_count": few_shot_count,
        "few_shot_source_split": "development" if few_shot_enabled else "",
        "few_shot_retrieval_version": few_shot_retrieval_version,
        "few_shot_mode": body.few_shot_mode if few_shot_enabled else "",
        "target_schema_enabled": target_schema_enabled,
        "query_planning": body.use_query_planning,
        "query_planning_version": QUERY_PLANNING_VERSION if body.use_query_planning else "",
        "quality_scoring": body.use_quality_scoring,
        "quality_rubric_version": QUALITY_RUBRIC_VERSION if body.use_quality_scoring else "",
        "quality_score_threshold": body.quality_score_threshold,
        "quality_retry_limit": body.quality_retry_limit if use_validation else 0,
        "quality_judge_model": quality_judge_model if body.use_quality_scoring else "",
        "optimization_note": body.optimization_note.strip(),
        "optimization_log": [
            {
                "id": "select-projection-v1",
                "title": "精简 SELECT 字段",
                "applied": True,
                "detail": "提示词要求只返回回答问题所需的字段，减少额外投影列。",
            },
            {
                "id": few_shot_retrieval_version or FEWSHOT_RETRIEVAL_VERSION,
                "title": "相似示例检索（Few-shot）",
                "applied": few_shot_enabled,
                "detail": (
                    (
                        f"按题目所需 SQL 结构（聚合/连接/排序/否定线索）从 development 集检索 {few_shot_count} 个示例，"
                        "并强制两条示例结构互补、尽量来自不同源库；"
                        if body.few_shot_mode != "legacy"
                        else f"按中文字符片段和 schema 标识符，从 development 集检索 {few_shot_count} 个示例（旧版检索，用于对照）；"
                    )
                    + "每条示例只包含问题与 SQL，不附示例 Schema；候选 SQL 需只读执行成功且返回非空、非全 NULL 结果。"
                    if few_shot_enabled
                    else "本轮未启用：用户关闭该选项。"
                    if not body.use_few_shot
                    else "本轮未启用：development 集同时作为示例来源，为避免泄漏自动关闭。"
                ),
            },
            {
                "id": "no-thinking-output-v1",
                "title": "SQL 输出清理",
                "applied": True,
                "detail": (
                    "请求关闭 thinking，并清理返回内容里的 think 标记与 Markdown 围栏。"
                    if DISABLE_THINKING
                    else "清理返回内容里的 think 标记与 Markdown 围栏。"
                ),
            },
            {
                "id": "target-schema-context-v1",
                "title": "目标题 Schema 上下文",
                "applied": target_schema_enabled,
                "detail": (
                    "向生成模型提供目标数据库的精简表、字段与外键关系。"
                    if target_schema_enabled
                    else "本轮未向生成模型提供目标数据库名、表结构或外键。评分和 SQL 校验仍使用真实数据库结构。"
                ),
            },
            {
                "id": QUERY_PLANNING_VERSION,
                "title": "复杂问题查询计划节点",
                "applied": body.use_query_planning,
                "detail": (
                    "遇到聚合/分组、比较/排名、多条件组合或嵌套/排除提示时，先生成包含目标字段、过滤、表与连接、聚合/分组、排序/数量限制的 JSON 计划，再将计划交给 SQL 生成节点；计划失败时回退直接生成。"
                    if body.use_query_planning
                    else "本轮未启用；可通过批次配置单独打开。"
                ),
            },
            {
                "id": "langgraph-stategraph-v1",
                "title": "LangGraph SQL 质控流程",
                "applied": body.use_langgraph,
                "detail": (
                    "用 StateGraph 编排 SQL 生成、只读检查、语义评分、条件重试与候选选择。"
                    if body.use_langgraph
                    else "本轮未启用：改用等价的顺序循环执行相同节点。"
                ),
            },
            {
                "id": "sql-validation-v1",
                "title": "SQL 执行校验与自动修复",
                "applied": use_validation,
                "detail": (
                    "只读执行候选 SQL；报错、返回 0 行或全 NULL 时把错误作为反馈让模型修订，最多 "
                    f"{body.quality_retry_limit} 次，并在候选中优先采用可执行且非空的结果（不调用评分模型）。"
                    if use_validation and not body.use_quality_scoring
                    else "与语义评分共用同一重试环节。" if use_validation else "本轮未启用。"
                ),
            },
            {
                "id": QUALITY_RUBRIC_VERSION,
                "title": "SQL 校验与语义评分",
                "applied": body.use_quality_scoring,
                "detail": (
                    f"先执行 SQLite 只读、语法、schema 校验，再由 {quality_judge_model} 按问题匹配、schema grounding、查询逻辑、输出约束四项加权评分；"
                    f"低于 {body.quality_score_threshold} 分时最多修订 {body.quality_retry_limit} 次。"
                    if body.use_quality_scoring
                    else "本轮未启用。"
                ),
            },
        ],
    }
    with state_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active_count = int(connection.execute("SELECT COUNT(*) FROM experiment_runs WHERE status IN ('queued', 'running')").fetchone()[0])
        if active_count:
            raise HTTPException(status_code=409, detail="已有批量运行正在执行；请等待完成或停止后再启动下一轮")
        connection.execute(
            """
            INSERT INTO experiment_runs
                (id, split, model, system_prompt, prompt_version, parameters_json, sample_limit, sample_seed, worker_pid, worker_started_at, total_count, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (run_id, body.split, model, system_prompt, prompt_version, json.dumps(run_parameters), body.sample_limit, body.sample_seed, os.getpid(), psutil.Process(os.getpid()).create_time(), len(sample_indices), now, now),
        )
        for index in sample_indices:
            record = RECORDS[body.split][index]
            _, user_prompt = build_prompt(
                record["db_id"],
                record.get("question", ""),
                few_shot_count=few_shot_count,
                include_target_schema=target_schema_enabled,
            )
            prompt_snapshot = json.dumps(
                {"messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]},
                ensure_ascii=False,
            )
            connection.execute(
                """
                INSERT INTO experiment_items
                    (run_id, split, sample_index, sample_fingerprint, db_id, question, gold_sql, prompt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, body.split, index, record["_sample_id"], record["db_id"], record.get("question", ""), record["_gold_sql"], prompt_snapshot, now),
            )
    background_tasks.add_task(run_experiment, run_id)
    return experiment_summary(run_id, include_prompt=False) or {"run_id": run_id, "status": "queued"}


@app.get("/api/experiments")
def get_experiments(limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    recover_interrupted_experiments()
    with state_connection() as connection:
        runs = connection.execute("SELECT id FROM experiment_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    items = [summary for row in runs if (summary := experiment_summary(row["id"])) is not None]
    return {"items": items}


@app.get("/api/experiment-analytics/full-test-problem-categories")
def get_full_test_problem_categories(
    split: str = Query("test", pattern="^(test|validation)$"),
    run_a_id: str = Query("", max_length=100),
    run_b_id: str = Query("", max_length=100),
) -> dict[str, Any]:
    recover_interrupted_experiments()
    full_dataset_count = len(RECORDS.get(split, []))
    with state_connection() as connection:
        available_rows = connection.execute(
            """
            SELECT id, model, prompt_version, created_at, total_count
            FROM experiment_runs
            WHERE split = ? AND status = 'completed' AND total_count = ?
            ORDER BY created_at DESC
            """,
            (split, full_dataset_count),
        ).fetchall()

        available_runs = [{"run_id": row["id"], **dict(row)} for row in available_rows]
        available_by_id = {run["run_id"]: run for run in available_runs}
        selected_a = available_by_id.get(run_a_id) or (available_runs[0] if available_runs else None)
        selected_b = available_by_id.get(run_b_id) if run_b_id != (selected_a or {}).get("id") else None
        if selected_b is None:
            selected_b = next(
                (run for run in available_runs if run["id"] != (selected_a or {}).get("id")),
                None,
            )
        runs = [run for run in (selected_a, selected_b) if run is not None]

        category_counts: defaultdict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "title": "",
                "incorrect": [set(), set()],
                "unjudged": [set(), set()],
                "generation_failed": [set(), set()],
            }
        )
        for run_index, run in enumerate(runs):
            rows = connection.execute(
                """
                SELECT sample_index, evaluation_status, diagnosis_json
                FROM experiment_items
                WHERE run_id = ? AND evaluation_status IN ('incorrect', 'unjudged', 'generation_failed')
                """,
                (run["id"],),
            ).fetchall()
            for row in rows:
                status = str(row["evaluation_status"])
                diagnoses = parse_json(row["diagnosis_json"], [])
                if not isinstance(diagnoses, list):
                    diagnoses = []
                seen_categories: set[str] = set()
                for diagnosis in diagnoses:
                    if not isinstance(diagnosis, dict):
                        continue
                    category = str(diagnosis.get("category", "unknown"))
                    if category in {"match", "evaluation_evidence"} or category in seen_categories:
                        continue
                    seen_categories.add(category)
                    counts = category_counts[category]
                    counts["title"] = counts["title"] or str(diagnosis.get("title", category))
                    counts[status][run_index].add(int(row["sample_index"]))
                if status == "generation_failed" and not seen_categories:
                    category_counts["generation_failed"]["title"] = "模型生成失败"
                    category_counts["generation_failed"]["generation_failed"][run_index].add(int(row["sample_index"]))

    run_summaries = [experiment_summary(run["id"]) for run in runs]
    categories = []
    for category, counts in category_counts.items():
        per_run = []
        for run_index in range(len(runs)):
            incorrect = len(counts["incorrect"][run_index])
            unjudged = len(counts["unjudged"][run_index])
            generation_failed = len(counts["generation_failed"][run_index])
            per_run.append({
                "incorrect": incorrect,
                "unjudged": unjudged,
                "generation_failed": generation_failed,
                "total": incorrect + unjudged + generation_failed,
            })
        run_a_count = per_run[0]["total"] if per_run else 0
        run_b_count = per_run[1]["total"] if len(per_run) > 1 else 0
        categories.append({
            "category": category,
            "title": counts["title"] or category,
            "runs": per_run,
            "delta": run_a_count - run_b_count,
        })
    categories.sort(key=lambda item: (-max((run["total"] for run in item["runs"]), default=0), item["title"]))
    return {
        "split": split,
        "available_runs": available_runs,
        "selected_run_ids": [run["id"] for run in runs],
        "runs": [summary for summary in run_summaries if summary is not None],
        "categories": categories,
        "category_method": "每个诊断类别按受影响样本数计数；同一样本可以属于多个问题类别；执行差异证据不作为问题类别重复统计。",
    }


@app.get("/api/experiments/{run_id}")
def get_experiment(run_id: str) -> dict[str, Any]:
    recover_interrupted_experiments()
    summary = experiment_summary(run_id, include_prompt=True)
    if not summary:
        raise HTTPException(status_code=404, detail="未找到该批量运行")
    return summary


@app.get("/api/experiments/{run_id}/items")
def get_experiment_items(
    run_id: str,
    status: str = Query("all", pattern="^(all|pending|correct|incorrect|unjudged|generation_failed|interrupted|not_run)$"),
    db_id: str = Query("", max_length=200),
    q: str = Query("", max_length=300),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
) -> dict[str, Any]:
    recover_interrupted_experiments()
    if not experiment_summary(run_id):
        raise HTTPException(status_code=404, detail="未找到该批量运行")
    clauses = ["run_id = ?"]
    values: list[Any] = [run_id]
    if status == "pending":
        clauses.append("evaluation_status = 'pending'")
    elif status != "all":
        clauses.append("evaluation_status = ?")
        values.append(status)
    if db_id.strip():
        clauses.append("lower(db_id) LIKE ?")
        values.append("%" + db_id.strip().casefold() + "%")
    if q.strip():
        clauses.append("(lower(question) LIKE ? OR lower(generated_sql) LIKE ? OR lower(gold_sql) LIKE ? OR lower(diagnosis_json) LIKE ?)")
        term = "%" + q.strip().casefold() + "%"
        values.extend([term, term, term, term])
    where = " AND ".join(clauses)
    with state_connection() as connection:
        total = int(connection.execute(f"SELECT COUNT(*) FROM experiment_items WHERE {where}", values).fetchone()[0])
        rows = connection.execute(
            f"SELECT id, sample_index, db_id, question, generated_sql, item_status, evaluation_status, generation_error, diagnosis_json, quality_assessment_json, latency_ms, completed_at FROM experiment_items WHERE {where} ORDER BY sample_index LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        ).fetchall()
    items = []
    for row in rows:
        diagnosis = parse_json(row["diagnosis_json"], [])
        items.append({
            "item_id": row["id"],
            "sample_index": row["sample_index"],
            "db_id": row["db_id"],
            "question": row["question"],
            "generated_sql": row["generated_sql"],
            "item_status": row["item_status"],
            "evaluation_status": row["evaluation_status"],
            "generation_error": row["generation_error"],
            "diagnosis": diagnosis,
            "reason_title": diagnosis[0].get("title", "") if diagnosis else "",
            "quality_score": parse_json(row["quality_assessment_json"], {}).get("score"),
            "latency_ms": row["latency_ms"],
            "completed_at": row["completed_at"],
        })
    return {"items": items, "page": page, "page_size": page_size, "total": total, "pages": (total + page_size - 1) // page_size}


@app.get("/api/experiments/{run_id}/items/{item_id}")
def get_experiment_item(run_id: str, item_id: int) -> dict[str, Any]:
    recover_interrupted_experiments()
    with state_connection() as connection:
        row = connection.execute("SELECT * FROM experiment_items WHERE run_id = ? AND id = ?", (run_id, item_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="未找到该运行样本")
    return {
        "item_id": row["id"],
        "run_id": row["run_id"],
        "split": row["split"],
        "sample_index": row["sample_index"],
        "db_id": row["db_id"],
        "question": row["question"],
        "gold_sql": row["gold_sql"],
        "prompt": parse_json(row["prompt"], row["prompt"]),
        "generated_sql": row["generated_sql"],
        "model": row["model"],
        "latency_ms": row["latency_ms"],
        "item_status": row["item_status"],
        "evaluation_status": row["evaluation_status"],
        "generation_error": row["generation_error"],
        "diagnosis": parse_json(row["diagnosis_json"], []),
        "prediction_summary": parse_json(row["prediction_summary_json"], {}),
        "gold_summary": parse_json(row["gold_summary_json"], {}),
        "diff": parse_json(row["diff_json"], {}),
        "quality_assessment": parse_json(row["quality_assessment_json"], {}),
        "completed_at": row["completed_at"],
    }


@app.post("/api/experiments/{run_id}/cancel")
def cancel_experiment(run_id: str) -> dict[str, Any]:
    recover_interrupted_experiments()
    with state_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE experiment_runs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status IN ('queued', 'running')",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id),
        )
        if updated.rowcount == 0 and not connection.execute(
            "SELECT 1 FROM experiment_runs WHERE id = ?", (run_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="未找到该批量运行")
    return experiment_summary(run_id) or {"run_id": run_id}


@app.get("/api/datasets")
def get_datasets() -> dict[str, Any]:
    datasets = []
    for split, description in SPLITS.items():
        records = RECORDS[split]
        databases = {record["db_id"] for record in records}
        datasets.append(
            {
                "key": split,
                "label": description["label"],
                "description": description["description"],
                "sample_count": len(records),
                "database_count": len(databases),
                "table_count": sum(public_table_count(SCHEMAS[db_id]) for db_id in databases),
                "gold_file_alignment": GOLD_FILE_AUDIT[split],
                "gold_sql_source": "JSON.query",
            }
        )
    return {
        "datasets": datasets,
        "total_samples": sum(len(records) for records in RECORDS.values()),
        "database_count": len(SCHEMAS),
        "table_count": sum(public_table_count(schema) for schema in SCHEMAS.values()),
        "result_count": result_count(),
    }


@app.get("/api/samples")
def get_samples(
    split: str = Query(..., description="development、validation 或 test"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    q: str = Query("", max_length=300),
    db_id: str = Query("", max_length=200),
    result_status: str = Query("all", pattern="^(all|pending|generated)$"),
) -> dict[str, Any]:
    if split not in RECORDS:
        raise HTTPException(status_code=404, detail="未找到该数据集")
    latest = latest_results(split)
    search = q.strip().casefold()
    database_filter = db_id.strip().casefold()
    matches = []
    for record in RECORDS[split]:
        index = record["_index"]
        recent = latest.get((split, record["_sample_id"]))
        if result_status == "pending" and recent:
            continue
        if result_status == "generated" and not recent:
            continue
        if database_filter and database_filter not in record["db_id"].casefold():
            continue
        if search:
            haystack = " ".join((record.get("question", ""), record.get("query", ""), record["_gold_sql"], record["db_id"])).casefold()
            if search not in haystack:
                continue
        matches.append(sample_payload(split, record, recent))

    total = len(matches)
    start = (page - 1) * page_size
    return {
        "items": matches[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": (total + page_size - 1) // page_size,
    }


@app.get("/api/samples/{split}/{sample_index}")
def get_sample(split: str, sample_index: int) -> dict[str, Any]:
    record = validate_sample(split, sample_index)
    with state_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM generated_results WHERE split = ? AND sample_fingerprint = ? ORDER BY id DESC",
            (split, record["_sample_id"]),
        ).fetchall()
    results = []
    for row in rows:
        result = result_from_row(row)
        result["sample_index"] = record["_index"]
        results.append(result)
    return {
        **sample_payload(split, record, results[0] if results else None),
        "result_history": results,
        "sql_structure": record.get("sql", {}),
    }


@app.post("/api/samples/{split}/{sample_index}/results", status_code=201)
def save_generated_result(split: str, sample_index: int, body: GeneratedResultInput) -> dict[str, Any]:
    record = validate_sample(split, sample_index)
    if body.sample_id and body.sample_id != record["_sample_id"]:
        raise HTTPException(status_code=409, detail="样本内容已变化，请重新读取后再保存生成结果")
    generated_sql = body.generated_sql.strip()
    if not generated_sql:
        raise HTTPException(status_code=422, detail="生成 SQL 不能为空")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with state_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO generated_results
                (split, sample_index, sample_fingerprint, db_id, question, generated_sql, model, run_id, latency_ms, prompt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                split,
                sample_index,
                record["_sample_id"],
                record["db_id"],
                record.get("question", ""),
                generated_sql,
                body.model.strip(),
                body.run_id.strip(),
                body.latency_ms,
                body.prompt,
                created_at,
            ),
        )
        row = connection.execute("SELECT * FROM generated_results WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return result_from_row(row)


@app.get("/api/databases")
def get_databases(
    split: str = Query("", max_length=30),
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    if split and split not in RECORDS:
        raise HTTPException(status_code=404, detail="未找到该数据集")
    search = q.strip().casefold()
    items = []
    for db_id, schema in SCHEMAS.items():
        split_counts = DB_SAMPLE_COUNTS.get(db_id, Counter())
        sample_count = split_counts.get(split, 0) if split else sum(split_counts.values())
        if search and search not in db_id.casefold():
            continue
        items.append(
            {
                "db_id": db_id,
                "table_count": public_table_count(schema),
                "sample_count": sample_count,
                "split_counts": {key: split_counts.get(key, 0) for key in SPLITS},
            }
        )
    items.sort(key=lambda item: item["db_id"].casefold())
    total = len(items)
    start = (page - 1) * page_size
    return {"items": items[start : start + page_size], "page": page, "page_size": page_size, "total": total}


@app.get("/api/databases/{db_id}")
def get_database(db_id: str) -> dict[str, Any]:
    schema = SCHEMAS.get(db_id)
    if not schema:
        raise HTTPException(status_code=404, detail="未找到该数据库")
    connection = open_source_db(db_id)
    try:
        table_names = list_sqlite_tables(connection)
        tables = []
        for table_name in table_names:
            pragma_table = quote_identifier(table_name)
            column_rows = connection.execute(f"PRAGMA table_info({pragma_table})").fetchall()
            foreign_key_rows = connection.execute(f"PRAGMA foreign_key_list({pragma_table})").fetchall()
            foreign_keys = [
                {"column": row["from"], "table": row["table"], "referenced_column": row["to"]}
                for row in foreign_key_rows
            ]
            row_count = connection.execute(f"SELECT COUNT(*) FROM {pragma_table}").fetchone()[0]
            tables.append(
                {
                    "name": table_name,
                    "row_count": row_count,
                    "columns": [
                        {
                            "name": row["name"],
                            "type": row["type"] or "—",
                            "nullable": not bool(row["notnull"]),
                            "primary_key_order": row["pk"],
                            "default": row["dflt_value"],
                            "foreign_key": next((fk for fk in foreign_keys if fk["column"] == row["name"]), None),
                        }
                        for row in column_rows
                    ],
                    "foreign_keys": foreign_keys,
                }
            )
    except sqlite3.Error as error:
        raise HTTPException(status_code=500, detail=f"读取数据库 schema 失败：{error}") from error
    finally:
        connection.close()
    counts = DB_SAMPLE_COUNTS.get(db_id, Counter())
    return {
        "db_id": db_id,
        "sample_count": sum(counts.values()),
        "split_counts": {key: counts.get(key, 0) for key in SPLITS},
        "tables": tables,
        "source": "SQLite",
    }


@app.get("/api/databases/{db_id}/tables/{table_name}/rows")
def get_table_rows(
    db_id: str,
    table_name: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    connection = open_source_db(db_id)
    try:
        if table_name not in list_sqlite_tables(connection):
            raise HTTPException(status_code=404, detail="未找到该数据表")
        quoted_table = quote_identifier(table_name)
        total = int(connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0])
        cursor = connection.execute(f"SELECT * FROM {quoted_table} LIMIT ? OFFSET ?", (limit, offset))
        rows = cursor.fetchall()
        return {
            "db_id": db_id,
            "table_name": table_name,
            "columns": [item[0] for item in cursor.description or []],
            "rows": [[json_value(value) for value in row] for row in rows],
            "limit": limit,
            "offset": offset,
            "total": total,
        }
    except sqlite3.Error as error:
        raise HTTPException(status_code=500, detail=f"读取表数据失败：{error}") from error
    finally:
        connection.close()


@app.get("/api/results")
def get_results(
    split: str = Query("", max_length=30),
    db_id: str = Query("", max_length=200),
    q: str = Query("", max_length=300),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
) -> dict[str, Any]:
    if split and split not in RECORDS:
        raise HTTPException(status_code=404, detail="未找到该数据集")
    with state_connection() as connection:
        rows = connection.execute("SELECT * FROM generated_results ORDER BY id DESC").fetchall()
    search = q.strip().casefold()
    matches = []
    for row in rows:
        result = result_from_row(row)
        if split and result["split"] != split:
            continue
        record = SAMPLES_BY_FINGERPRINT.get((result["split"], result["sample_fingerprint"]))
        db_id_value = record["db_id"] if record else result["db_id"]
        question_value = record.get("question", "") if record else result["question"]
        gold_sql_value = record["_gold_sql"] if record else ""
        if db_id and db_id.casefold() not in db_id_value.casefold():
            continue
        if search:
            searchable = " ".join((question_value, gold_sql_value, result["generated_sql"], result["model"], db_id_value)).casefold()
            if search not in searchable:
                continue
        matches.append(
            {
                **result,
                "sample_index": record["_index"] if record else result["sample_index"],
                "db_id": db_id_value,
                "question": question_value,
                "gold_sql": gold_sql_value,
                "sample_available": record is not None,
                "sample_status": (
                    "current" if record else "legacy_unlinked" if not result["sample_fingerprint"] else "source_changed"
                ),
            }
        )
    total = len(matches)
    start = (page - 1) * page_size
    return {
        "items": matches[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": (total + page_size - 1) // page_size,
    }
