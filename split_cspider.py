"""Create database-disjoint development, validation, and test CSpider sets."""

import json
import os
import random
import shutil
from collections import defaultdict
from pathlib import Path


SOURCE = Path(os.environ.get("CSPIDER_SOURCE_DIR", r"D:\dataset\CSpider"))
DESTINATION = Path(__file__).resolve().parent / "data" / "CSpider"
SEED = 42
VALIDATION_FRACTION = 0.10


def read_split(name):
    records = json.loads((SOURCE / f"{name}.json").read_text(encoding="utf-8"))
    gold = (SOURCE / f"{name}_gold.sql").read_text(encoding="utf-8").splitlines()
    if len(records) != len(gold):
        raise ValueError(f"{name}: {len(records)} records but {len(gold)} SQL lines")
    return records, gold


def write_split(name, records, gold):
    (DESTINATION / f"{name}.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (DESTINATION / f"{name}_gold.sql").write_text(
        "\n".join(gold) + "\n", encoding="utf-8"
    )


def main():
    train, train_gold = read_split("train")
    test, test_gold = read_split("dev")
    by_database = defaultdict(list)
    for index, record in enumerate(train):
        by_database[record["db_id"]].append(index)

    database_ids = sorted(by_database)
    random.Random(SEED).shuffle(database_ids)
    validation_ids = set()
    target = round(len(train) * VALIDATION_FRACTION)
    for database_id in database_ids:
        if len(validation_ids) >= len(database_ids) or sum(
            len(by_database[item]) for item in validation_ids
        ) >= target:
            break
        validation_ids.add(database_id)

    development_indices = [i for i, row in enumerate(train) if row["db_id"] not in validation_ids]
    validation_indices = [i for i, row in enumerate(train) if row["db_id"] in validation_ids]
    if not development_indices or not validation_indices:
        raise ValueError("Split produced an empty partition")

    DESTINATION.mkdir(parents=True, exist_ok=True)
    write_split("development", [train[i] for i in development_indices], [train_gold[i] for i in development_indices])
    write_split("validation", [train[i] for i in validation_indices], [train_gold[i] for i in validation_indices])
    write_split("test", test, test_gold)
    for filename in ("tables.json", "char_emb.txt", "README.txt"):
        shutil.copy2(SOURCE / filename, DESTINATION / filename)
    shutil.copytree(SOURCE / "database", DESTINATION / "database", dirs_exist_ok=True)
    print(f"development: {len(development_indices)} records, {len(database_ids) - len(validation_ids)} databases")
    print(f"validation: {len(validation_indices)} records, {len(validation_ids)} databases")
    print(f"test: {len(test)} records, {len({row['db_id'] for row in test})} databases")


if __name__ == "__main__":
    main()
