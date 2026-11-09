"""Convert this project's CSpider splits to LLaMA Factory Alpaca JSONL."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data" / "CSpider"
OUTPUT_ROOT = Path(__file__).resolve().parent
SPLITS = {"development": "train", "validation": "validation", "test": "test"}
INSTRUCTION = (
    "根据用户问题和给定的数据库 Schema 生成一条 SQLite 查询。"
    "只能使用 Schema 中存在的表和字段；输出只能是一条只读 SELECT 查询"
    "（允许 WITH ... SELECT），不要附加解释或 Markdown。"
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compact_schema(schema: dict[str, Any]) -> str:
    tables = schema.get("table_names_original", [])
    columns = schema.get("column_names_original", [])
    types = schema.get("column_types", [])
    primary_keys = set(schema.get("primary_keys", []))
    foreign_keys = schema.get("foreign_keys", [])
    foreign_key_columns = {index for pair in foreign_keys for index in pair}
    lines = []
    for table_index, table_name in enumerate(tables):
        fields = []
        for column_index, (owner, name) in enumerate(columns):
            if owner != table_index or name == "*":
                continue
            tags = []
            if column_index in primary_keys:
                tags.append("PK")
            if column_index in foreign_key_columns:
                tags.append("FK")
            column_type = types[column_index] if column_index < len(types) else "unknown"
            suffix = f" [{','.join(tags)}]" if tags else ""
            fields.append(f"{name} {column_type or 'unknown'}{suffix}")
        lines.append(f"{table_name}({', '.join(fields)})")
    relationships = []
    for source, target in foreign_keys:
        if source >= len(columns) or target >= len(columns):
            continue
        source_table, source_column = columns[source]
        target_table, target_column = columns[target]
        if source_table < 0 or target_table < 0:
            continue
        relationships.append(
            f"{tables[source_table]}.{source_column} = "
            f"{tables[target_table]}.{target_column}"
        )
    if relationships:
        lines.append("Foreign keys: " + "; ".join(relationships))
    return "\n".join(lines)


def convert_split(source_split: str, output_name: str, schemas: dict[str, dict[str, Any]]) -> tuple[int, int]:
    records = load_json(DATA_ROOT / f"{source_split}.json")
    excluded_path = ROOT / "backend" / "storage" / f"gold_literal_mismatch_{source_split}.json"
    excluded = {int(index) for index in load_json(excluded_path)} if excluded_path.is_file() else set()
    destination = OUTPUT_ROOT / f"{output_name}.jsonl"
    written = 0
    with destination.open("w", encoding="utf-8", newline="\n") as output:
        for index, record in enumerate(records):
            if index in excluded:
                continue
            db_id = record["db_id"]
            question = record.get("question", "").strip()
            query = record.get("query", "").strip()
            if not question or not query:
                continue
            schema = schemas.get(db_id)
            if schema is None:
                raise ValueError(f"Missing schema for db_id={db_id!r} at {source_split}[{index}]")
            row = {
                "instruction": INSTRUCTION,
                "input": f"数据库: {db_id}\nSchema:\n{compact_schema(schema)}\n\n问题: {question}",
                "output": query,
            }
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return written, len(records)


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    schema_rows = load_json(DATA_ROOT / "tables.json")
    schemas = {row["db_id"]: row for row in schema_rows}
    for source_split, output_name in SPLITS.items():
        written, total = convert_split(source_split, output_name, schemas)
        print(f"{output_name}: {written}/{total} samples written")


if __name__ == "__main__":
    main()