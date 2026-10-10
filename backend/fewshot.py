from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Callable


SQL_TOKEN_PATTERN = re.compile(
    r"--[^\r\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[(?:[^\]]|\]\])*\]|[\w$]+|[^\s]",
    re.UNICODE,
)


def normalized_identifier(value: str) -> str:
    return " ".join(value.casefold().split())


def sql_tokens(sql: str) -> list[str]:
    tokens: list[str] = []
    for match in SQL_TOKEN_PATTERN.finditer(sql):
        token = match.group(0)
        if token.startswith("--") or token.startswith("/*") or token.startswith("'"):
            continue
        if token.startswith('"') and token.endswith('"'):
            token = token[1:-1].replace('""', '"')
        elif token.startswith("`") and token.endswith("`"):
            token = token[1:-1].replace("``", "`")
        elif token.startswith("[") and token.endswith("]"):
            token = token[1:-1].replace("]]", "]")
        tokens.append(normalized_identifier(token))
    return tokens


def identifier_tokens(sql: str) -> set[str]:
    return {token for token in sql_tokens(sql) if re.search(r"[\w$]", token, re.UNICODE)}


def select_projects_all_columns(tokens: list[str]) -> bool:
    for select_index, token in enumerate(tokens):
        if token != "select":
            continue
        depth = 0
        for index in range(select_index + 1, len(tokens)):
            current = tokens[index]
            if current == "(":
                depth += 1
            elif current == ")":
                depth = max(0, depth - 1)
            elif current == "from" and depth == 0:
                break
            elif current == "*" and depth == 0:
                return True
    return False


def equality_column_pairs(tokens: list[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for index, token in enumerate(tokens):
        if token != "=" or index == 0 or index + 1 >= len(tokens):
            continue
        left = tokens[index - 1]
        right = tokens[index + 1]
        if index >= 2 and tokens[index - 2] == ".":
            left = tokens[index - 1]
        if index + 2 < len(tokens) and tokens[index + 2] == "." and index + 3 < len(tokens):
            right = tokens[index + 3]
        if re.search(r"[\w$]", left, re.UNICODE) and re.search(r"[\w$]", right, re.UNICODE):
            pairs.add(tuple(sorted((left, right))))
    return pairs


class FewShotRetriever:
    """In-memory BM25 retriever with validation and compact example-schema rendering."""

    def __init__(
        self,
        development_records: list[dict[str, Any]],
        schemas: dict[str, dict[str, Any]],
        open_source_db: Callable[[str], sqlite3.Connection],
        readonly_authorizer: Callable[..., int],
        *,
        candidate_limit: int = 24,
        validation_candidate_limit: int = 8,
        validation_seconds: float = 0.1,
    ) -> None:
        self.documents = list(development_records)
        self.schemas = schemas
        self.open_source_db = open_source_db
        self.readonly_authorizer = readonly_authorizer
        self.candidate_limit = candidate_limit
        self.validation_candidate_limit = validation_candidate_limit
        self.validation_seconds = validation_seconds
        self.validity_cache: dict[str, bool] = {}
        self.validity_lock = threading.Lock()
        self.retrieval_cache: dict[tuple[str, str, int], tuple[int, ...]] = {}
        self.retrieval_lock = threading.Lock()
        (
            self.postings,
            self.document_frequency,
            self.document_lengths,
            self.average_length,
        ) = self._build_index()

    @staticmethod
    def _text_tokens(text: str) -> Counter[str]:
        tokens: Counter[str] = Counter()
        for cjk_run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", text):
            if len(cjk_run) == 1:
                tokens[f"c:{cjk_run}"] += 1
            else:
                for index in range(len(cjk_run) - 1):
                    tokens[f"c:{cjk_run[index:index + 2]}"] += 1
        for word in re.findall(r"[a-z0-9]+", text.casefold()):
            tokens[f"w:{word}"] += 1
        return tokens

    def _schema_tokens(self, db_id: str) -> Counter[str]:
        schema = self.schemas.get(db_id, {})
        names = list(schema.get("table_names_original", []))
        names.extend(column for _, column in schema.get("column_names_original", []) if column != "*")
        tokens: Counter[str] = Counter()
        for name in names:
            identifier = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name)).casefold()
            for word in re.findall(r"[a-z0-9]+", identifier):
                tokens[f"s:{word}"] += 1
        return tokens

    def _build_index(self) -> tuple[dict[str, list[tuple[int, int]]], dict[str, int], list[int], float]:
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        document_frequency: Counter[str] = Counter()
        document_lengths: list[int] = []
        total_length = 0
        for document_index, record in enumerate(self.documents):
            question_terms = self._text_tokens(str(record.get("question", "")))
            terms: Counter[str] = Counter({f"q:{term}": frequency for term, frequency in question_terms.items()})
            terms.update(self._schema_tokens(record["db_id"]))
            document_length = sum(terms.values())
            document_lengths.append(document_length)
            total_length += document_length
            for term, frequency in terms.items():
                postings[term].append((document_index, frequency))
                document_frequency[term] += 1
        average_length = total_length / len(self.documents) if self.documents else 1.0
        return dict(postings), dict(document_frequency), document_lengths, average_length

    def _example_schema_prompt(self, db_id: str, sql: str) -> str:
        schema = self.schemas.get(db_id)
        if not schema:
            return "未找到该示例数据库的 schema。"

        tokens = sql_tokens(sql)
        identifiers = {token for token in tokens if re.search(r"[\w$]", token, re.UNICODE)}
        used_table_indices = {
            table_index
            for table_index, table_name in enumerate(schema.get("table_names_original", []))
            if normalized_identifier(str(table_name)) in identifiers
        }
        if not used_table_indices:
            return "该 SQL 未引用可识别的数据库表。"

        table_names = schema.get("table_names_original", [])
        column_names = schema.get("column_names_original", [])
        column_types = schema.get("column_types", [])
        primary_keys = set(schema.get("primary_keys", []))
        foreign_keys = schema.get("foreign_keys", [])
        select_all = select_projects_all_columns(tokens)

        used_columns: set[int] = set()
        for column_index, (table_index, column_name) in enumerate(column_names):
            if table_index not in used_table_indices or column_name == "*":
                continue
            if select_all or normalized_identifier(str(column_name)) in identifiers:
                used_columns.add(column_index)

        equal_pairs = equality_column_pairs(tokens)
        used_relationships: list[tuple[int, int]] = []
        for source_index, target_index in foreign_keys:
            if source_index >= len(column_names) or target_index >= len(column_names):
                continue
            source_table_index, source_column = column_names[source_index]
            target_table_index, target_column = column_names[target_index]
            if source_table_index not in used_table_indices or target_table_index not in used_table_indices:
                continue
            pair = tuple(sorted((normalized_identifier(str(source_column)), normalized_identifier(str(target_column)))) )
            if pair in equal_pairs:
                used_relationships.append((source_index, target_index))
                used_columns.update((source_index, target_index))

        foreign_key_columns = {
            column_index for pair in used_relationships for column_index in pair
        }
        table_lines: list[str] = []
        for table_index in sorted(used_table_indices):
            fields: list[str] = []
            for column_index, (owner_index, column_name) in enumerate(column_names):
                if owner_index != table_index or column_index not in used_columns:
                    continue
                column_type = column_types[column_index] if column_index < len(column_types) else ""
                tags = []
                if column_index in primary_keys:
                    tags.append("PK")
                if column_index in foreign_key_columns:
                    tags.append("FK")
                suffix = f" [{','.join(tags)}]" if tags else ""
                fields.append(f"{column_name} {column_type or 'unknown'}{suffix}")
            table_name = table_names[table_index]
            table_lines.append(f"{table_name}({', '.join(fields)})" if fields else str(table_name))

        relationships: list[str] = []
        for source_index, target_index in used_relationships:
            source_table_index, source_column = column_names[source_index]
            target_table_index, target_column = column_names[target_index]
            relationships.append(
                f"{table_names[source_table_index]}.{source_column} = "
                f"{table_names[target_table_index]}.{target_column}"
            )
        schema_text = "\n".join(table_lines)
        if relationships:
            schema_text += "\n外键关系：" + "; ".join(relationships)
        return schema_text

    def example_schema_prompt(self, db_id: str, sql: str) -> str:
        return self._example_schema_prompt(db_id, sql)

    def _example_is_usable(self, record: dict[str, Any]) -> bool:
        sample_id = str(record.get("_sample_id", ""))
        if not sample_id:
            return False
        with self.validity_lock:
            cached = self.validity_cache.get(sample_id)
        if cached is not None:
            return cached

        sql = str(record.get("_gold_sql", "")).strip()
        first_token = re.sub(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)*", "", sql, flags=re.DOTALL).casefold()
        if not first_token.startswith(("select", "with")):
            usable = False
        else:
            connection = None
            deadline = time.monotonic() + self.validation_seconds

            def validation_progress() -> int:
                return 1 if time.monotonic() >= deadline else 0

            try:
                connection = self.open_source_db(record["db_id"])
                connection.set_authorizer(self.readonly_authorizer)
                connection.set_progress_handler(validation_progress, 1000)
                preview = connection.execute(sql).fetchmany(5)
                usable = bool(preview) and any(value is not None for row in preview for value in row)
            except (sqlite3.Error, KeyError, TypeError, ValueError):
                usable = False
            finally:
                if connection is not None:
                    connection.close()

        with self.validity_lock:
            self.validity_cache[sample_id] = usable
        return usable

    def retrieve(self, db_id: str, question: str, count: int) -> list[dict[str, Any]]:
        if count <= 0 or not self.documents:
            return []
        normalized_question = re.sub(r"[\W_]+", "", question.casefold())
        cache_key = (db_id, normalized_question, count)
        with self.retrieval_lock:
            cached_indices = self.retrieval_cache.get(cache_key)
        if cached_indices is not None:
            return [self.documents[index] for index in cached_indices]

        query_terms = self._text_tokens(question)
        query_terms = Counter({f"q:{term}": frequency for term, frequency in query_terms.items()})
        for term, frequency in self._schema_tokens(db_id).items():
            query_terms[f"s:{term[2:]}"] += frequency

        document_count = len(self.documents)
        scores: defaultdict[int, float] = defaultdict(float)
        k1 = 1.2
        length_normalization = 0.75
        for term, query_frequency in query_terms.items():
            term_postings = self.postings.get(term)
            if not term_postings:
                continue
            document_frequency = self.document_frequency[term]
            inverse_frequency = math.log1p(
                (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            feature_weight = 1.25 if term.startswith("s:") else 1.0
            for document_index, term_frequency in term_postings:
                document_length = self.document_lengths[document_index]
                denominator = term_frequency + k1 * (
                    1 - length_normalization + length_normalization * document_length / self.average_length
                )
                scores[document_index] += (
                    feature_weight
                    * inverse_frequency
                    * term_frequency
                    * (k1 + 1)
                    / denominator
                    * min(query_frequency, 2)
                )

        candidates = sorted(scores, key=lambda index: scores[index], reverse=True)[:self.candidate_limit]
        selected: list[dict[str, Any]] = []
        selected_indices: list[int] = []
        seen_sql: set[str] = set()
        validated_candidates = 0
        for document_index in candidates:
            record = self.documents[document_index]
            if re.sub(r"[\W_]+", "", str(record.get("question", "")).casefold()) == normalized_question:
                continue
            canonical_sql = re.sub(r"\s+", " ", str(record.get("_gold_sql", "")).strip()).rstrip(";").casefold()
            if not canonical_sql or canonical_sql in seen_sql:
                continue
            if validated_candidates >= self.validation_candidate_limit:
                break
            validated_candidates += 1
            if not self._example_is_usable(record):
                continue
            seen_sql.add(canonical_sql)
            selected.append(record)
            selected_indices.append(document_index)
            if len(selected) >= count:
                break
        with self.retrieval_lock:
            self.retrieval_cache[cache_key] = tuple(selected_indices)
        return selected
