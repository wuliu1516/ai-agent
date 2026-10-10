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

# 结构签名线索：示例库里没有目标题的表名/字段名（跨库不重叠），所以只能按"题目需要什么形状的 SQL"
# 去匹配示例，而不是按题目和示例的用词相似度。
SHAPE_CUES: dict[str, tuple[str, ...]] = {
    "agg": (
        "平均", "总计", "总和", "总共", "总数", "数量", "多少", "几个", "几位", "每个", "每种", "每年",
        "分别", "多少次", "次数", "几门", "几部", "几首", "总", "共",
        "average", "total", "sum", "count", "how many", "number of", "each", "per ",
    ),
    "order": (
        "最高", "最低", "最大", "最小", "最多", "最少", "排名", "前几", "第几", "第一个", "最后一个",
        "top", "highest", "lowest", "largest", "smallest", "most", "least", "rank", "first", "last",
    ),
    "neg": (
        "除了", "不包括", "不包含", "没有任何", "至少一个", "没有", "不是", "未",
        "without", "except", "not ", "no ", "none",
    ),
}

JOIN_PATTERN = re.compile(r"\bjoin\b|\bfrom\s+\w+\s*,", re.IGNORECASE)
AGG_PATTERN = re.compile(r"\b(count|sum|avg|min|max|total)\s*\(", re.IGNORECASE)
NEG_PATTERN = re.compile(r"\b(not\s+in|not\s+exists|except|!=|<>)\b", re.IGNORECASE)


def sql_shape(sql: str) -> frozenset[str]:
    """粗粒度结构签名：聚合、连接、排序、限制、否定。用于按结构而非用词匹配示例。"""
    padded = f" {sql.casefold()} "
    features: set[str] = set()
    if AGG_PATTERN.search(sql) or " group by " in padded:
        features.add("agg")
    if JOIN_PATTERN.search(sql):
        features.add("join")
    if " order by " in padded:
        features.add("order")
    if " limit " in padded or " having " in padded:
        features.add("limit")
    if NEG_PATTERN.search(sql):
        features.add("neg")
    return frozenset(features)


def question_shape(question: str) -> frozenset[str]:
    """从问题线索保守推断需要的结构；没有明确线索就不加约束。"""
    normalized = question.casefold()
    features: set[str] = set()
    for feature in ("agg", "order", "neg"):
        if any(cue in normalized for cue in SHAPE_CUES[feature]):
            features.add(feature)
    return frozenset(features)




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

    def retrieve(self, db_id: str, question: str, count: int, index_by_identifiers: bool = False) -> list[dict[str, Any]]:
        """Retrieve demonstrations.

        ``index_by_identifiers=False`` (默认) 按"题目需要的 SQL 结构"匹配示例，并强制两条示例结构互补：
        开发集与验证集的库完全不重叠，示例的表名/字段名对目标题没有可迁移性；实测中默认检索会把
        64% 的两个名额给同一个源库、且偏向复杂模板，因此按结构重排。
        ``index_by_identifiers=True`` 保留旧行为（schema 标识符加权 + 纯词面相似度），用于 A/B 对照。
        """
        if count <= 0 or not self.documents:
            return []
        normalized_question = re.sub(r"[\W_]+", "", question.casefold())
        cache_key = (db_id, normalized_question, count, index_by_identifiers)
        with self.retrieval_lock:
            cached_indices = self.retrieval_cache.get(cache_key)
        if cached_indices is not None:
            return [self.documents[index] for index in cached_indices]

        query_terms = self._text_tokens(question)
        query_terms = Counter({f"q:{term}": frequency for term, frequency in query_terms.items()})
        if index_by_identifiers:
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

        wanted = question_shape(question)
        shape_scores = {index: sql_shape(str(self.documents[index].get("_gold_sql", ""))) for index in scores}
        ranked = sorted(
            scores,
            key=lambda index: (
                -len(wanted & shape_scores[index]) if not index_by_identifiers else 0,
                -scores[index],
            ),
        )

        selected: list[dict[str, Any]] = []
        selected_indices: list[int] = []
        seen_sql: set[str] = set()
        used_shapes: set[frozenset[str]] = set()
        used_databases: set[str] = set()
        validated_candidates = 0

        def accept(document_index: int, *, require_novel: bool) -> bool:
            nonlocal validated_candidates
            record = self.documents[document_index]
            if re.sub(r"[\W_]+", "", str(record.get("question", "")).casefold()) == normalized_question:
                return False
            canonical_sql = re.sub(r"\s+", " ", str(record.get("_gold_sql", "")).strip()).rstrip(";").casefold()
            if not canonical_sql or canonical_sql in seen_sql:
                return False
            shape = shape_scores[document_index]
            database = str(record["db_id"])
            # 优先选结构不同、来源库不同的示例，让有限的名额覆盖更多写法。
            if require_novel and not index_by_identifiers:
                if shape in used_shapes or database in used_databases:
                    return False
            if validated_candidates >= self.validation_candidate_limit:
                return False
            validated_candidates += 1
            if not self._example_is_usable(record):
                return False
            seen_sql.add(canonical_sql)
            used_shapes.add(shape)
            used_databases.add(database)
            selected.append(record)
            selected_indices.append(document_index)
            return True

        # 先按"结构互补、来源库不同"挑选，名额未满再用其余候补。
        for require_novel in (True, False):
            for document_index in ranked[: self.candidate_limit]:
                if len(selected) >= count:
                    break
                if document_index in selected_indices:
                    continue
                accept(document_index, require_novel=require_novel)
            if len(selected) >= count:
                break
        with self.retrieval_lock:
            self.retrieval_cache[cache_key] = tuple(selected_indices)
        return selected
        with self.retrieval_lock:
            self.retrieval_cache[cache_key] = tuple(selected_indices)
        return selected
