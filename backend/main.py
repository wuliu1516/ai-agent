from __future__ import annotations

import json
import hashlib
import os
import random
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psutil
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv


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
GENERATION_PARAMS = {"temperature": 0, "max_tokens": 2048}
BASELINE_SYSTEM_PROMPT = """你是一个严谨的 Text-to-SQL 助手。请根据用户的问题和给出的数据库结构生成 SQL。
必须遵循以下规则：
1. 只使用提供的表名和字段名，不要臆造数据库结构。
2. 表之间需要关联时，优先遵循 schema 中给出的外键关系。
3. 使用 SQLite 方言，生成一条只读 SELECT 查询（允许 WITH ... SELECT）。
4. 最终答复只能是一条 SQL 语句；不要输出推理过程、分析说明或任何 <think> / </think> 标记。
5. 不要使用 Markdown 代码围栏，不要添加“SQL:”等前后缀文字，也不要输出多条语句。"""


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
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE(run_id, sample_index)
        )
        """
    )
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


def build_prompt(db_id: str, question: str) -> tuple[str, str]:
    user_prompt = (
        f"Database: {db_id}\n"
        "SQL dialect: SQLite\n"
        "Schema:\n"
        f"{compact_schema_prompt(db_id)}\n\n"
        f"Question:\n{question}\n\n"
        "Return one SQLite SELECT query only."
    )
    return BASELINE_SYSTEM_PROMPT, user_prompt


class FatalModelServiceError(RuntimeError):
    pass


def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After", "") if error.headers else ""
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), 30.0))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, min((retry_at - datetime.now(timezone.utc)).total_seconds(), 30.0))
            except (TypeError, ValueError, OverflowError):
                pass
    return float(2 ** attempt)


def call_chat_completion(model: str, system_prompt: str, user_prompt: str) -> str:
    parsed_base_url = urlsplit(API_BASE_URL)
    endpoint_path = parsed_base_url.path.rstrip("/")
    if not endpoint_path.endswith("/chat/completions"):
        endpoint_path += "/chat/completions"
    endpoint = urlunsplit((parsed_base_url.scheme, parsed_base_url.netloc, endpoint_path, "", ""))
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        **GENERATION_PARAMS,
    }
    if DISABLE_THINKING:
        request_payload["chat_template_kwargs"] = {"enable_thinking": False}
    request_body = json.dumps(request_payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers=headers,
        method="POST",
    )
    payload = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:600]
            message = f"模型服务返回 HTTP {error.code}: {detail}"
            if error.code in {408, 425, 429} or error.code >= 500:
                if attempt < 3:
                    delay = retry_delay(error, attempt)
                    error.close()
                    time.sleep(delay)
                    continue
                raise FatalModelServiceError(f"重试 4 次后仍失败：{message}") from error
            raise FatalModelServiceError(message) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt < 3:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                continue
            raise FatalModelServiceError(
                f"重试 4 次后仍无法连接模型服务（{type(error).__name__}）：{error}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FatalModelServiceError("模型服务返回内容不是有效 JSON") from error
    if payload is None:
        raise FatalModelServiceError("模型服务未返回有效响应")
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise FatalModelServiceError("模型服务响应缺少 choices[0].message.content") from error
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    text = str(content or "").strip()
    thinking_ends = list(re.finditer(r"</think\s*>", text, flags=re.IGNORECASE))
    if thinking_ends:
        text = text[thinking_ends[-1].end():].strip()
    text = re.sub(r"</?think\b[^>]*>", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    if not text:
        raise RuntimeError("模型没有返回 SQL")
    return text


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


def evaluate_generated_sql(db_id: str, predicted_sql: str, gold_sql: str) -> tuple[str, list[dict[str, str]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    prediction = execute_for_evaluation(db_id, predicted_sql)
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
        "parameters": parse_json(run["parameters_json"], GENERATION_PARAMS),
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
) -> None:
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with state_connection() as connection:
        connection.execute(
            """
            UPDATE experiment_items
            SET item_status = ?, evaluation_status = ?, generated_sql = ?, model = ?, latency_ms = ?,
                generation_error = ?, diagnosis_json = ?, prediction_summary_json = ?, gold_summary_json = ?,
                diff_json = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                item_status,
                evaluation_status,
                generated_sql,
                model,
                latency_ms,
                generation_error,
                json.dumps(diagnosis or [], ensure_ascii=False),
                json.dumps(prediction_summary or {}, ensure_ascii=False),
                json.dumps(gold_summary or {}, ensure_ascii=False),
                json.dumps(diff or {}, ensure_ascii=False),
                completed_at,
                item_id,
            ),
        )


def generate_and_evaluate_experiment_item(run: sqlite3.Row, item: sqlite3.Row) -> dict[str, Any]:
    started = time.monotonic()
    try:
        prompt_payload = parse_json(item["prompt"], {})
        messages = prompt_payload.get("messages", [])
        system_prompt = next((message.get("content", "") for message in messages if message.get("role") == "system"), run["system_prompt"])
        user_prompt = next((message.get("content", "") for message in messages if message.get("role") == "user"), "")
        generated_sql = call_chat_completion(run["model"], system_prompt, user_prompt)
    except Exception as error:
        message = f"{type(error).__name__}: {str(error)}"[:1200]
        return {
            "item_status": "generation_failed",
            "evaluation_status": "generation_failed",
            "generated_sql": "",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "generation_error": message,
            "diagnosis": [{"category": "generation", "title": "模型生成失败", "evidence": message}],
            "fatal": isinstance(error, FatalModelServiceError),
        }

    latency_ms = round((time.monotonic() - started) * 1000, 2)
    try:
        evaluation_status, diagnosis, prediction, reference, diff = evaluate_generated_sql(
            item["db_id"], generated_sql, item["gold_sql"]
        )
    except Exception as error:
        message = f"评测过程出错：{str(error)[:600]}"
        evaluation_status = "unjudged"
        diagnosis = [{"category": "evaluation", "title": "评测过程异常，需人工确认", "evidence": message}]
        prediction, reference, diff = {}, {}, {"error": message}
    return {
        "item_status": "completed",
        "evaluation_status": evaluation_status,
        "generated_sql": generated_sql,
        "latency_ms": latency_ms,
        "generation_error": "",
        "diagnosis": diagnosis,
        "prediction_summary": prediction,
        "gold_summary": reference,
        "diff": diff,
        "fatal": False,
    }


def persist_experiment_item_result(
    run_id: str, run: sqlite3.Row, item: sqlite3.Row, result: dict[str, Any]
) -> str | None:
    if result["item_status"] == "generation_failed":
        update_experiment_item(
            item["id"], item_status="generation_failed", evaluation_status="generation_failed",
            model=run["model"], latency_ms=result["latency_ms"],
            generation_error=result["generation_error"], diagnosis=result["diagnosis"],
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
                    item["question"], generated_sql, run["model"], run_id, latency_ms, item["prompt"], created_at,
                ),
            )
            update_cursor = connection.execute(
                """
                UPDATE experiment_items
                SET item_status = 'completed', evaluation_status = ?, generated_sql = ?, model = ?, latency_ms = ?,
                    generation_error = '', diagnosis_json = ?, prediction_summary_json = ?, gold_summary_json = ?,
                    diff_json = ?, completed_at = ?
                WHERE id = ? AND item_status = 'generating'
                """,
                (
                    evaluation_status,
                    generated_sql,
                    run["model"],
                    latency_ms,
                    json.dumps(diagnosis, ensure_ascii=False),
                    json.dumps(prediction, ensure_ascii=False),
                    json.dumps(reference, ensure_ascii=False),
                    json.dumps(diff, ensure_ascii=False),
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
    if not model:
        raise HTTPException(status_code=422, detail="模型 ID 不能为空")
    if not system_prompt:
        raise HTTPException(status_code=422, detail="System Prompt 不能为空")
    total_samples = len(RECORDS[body.split])
    if body.sample_limit == 0 or body.sample_limit >= total_samples:
        sample_indices = list(range(total_samples))
    else:
        sample_indices = sorted(random.Random(body.sample_seed).sample(range(total_samples), body.sample_limit))
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prompt_version = "sha256:" + hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
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
            (run_id, body.split, model, system_prompt, prompt_version, json.dumps({**GENERATION_PARAMS, "concurrency": MAX_GENERATION_CONCURRENCY}), body.sample_limit, body.sample_seed, os.getpid(), psutil.Process(os.getpid()).create_time(), len(sample_indices), now, now),
        )
        for index in sample_indices:
            record = RECORDS[body.split][index]
            _, user_prompt = build_prompt(record["db_id"], record.get("question", ""))
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
            f"SELECT id, sample_index, db_id, question, generated_sql, item_status, evaluation_status, generation_error, diagnosis_json, latency_ms, completed_at FROM experiment_items WHERE {where} ORDER BY sample_index LIMIT ? OFFSET ?",
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
