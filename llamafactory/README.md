# LLaMA Factory dataset

This directory adapts the repository's CSpider NL2SQL benchmark to LLaMA Factory's Alpaca format.

- `train.jsonl` is generated from the database-disjoint development split.
- `validation.jsonl` is generated from the database-disjoint validation split.
- `test.jsonl` is held out for final evaluation and intentionally omitted from `dataset_info.json`.
- `dataset_info.json` registers the training and validation datasets.

Each input contains the database ID, compact target schema (tables, typed columns, primary/foreign-key tags and relationships), and Chinese question. The output is the record's own `query` SQL. The separate `*_gold.sql` files are not used because they can be misaligned with the JSON records.

Examples whose gold SQL contains a non-identifier literal absent from the target database are excluded using this repository's `backend/storage/gold_literal_mismatch_*.json` audit files. If an audit file is absent, the converter includes all records with a question and query. The test split is exported for evaluation only; do not train on it.

Regenerate from the repository root after preparing `data/CSpider`:

```powershell
python llamafactory/prepare_dataset.py
```

In LLaMA Factory, select `cspider_nl2sql_train` as the dataset and `cspider_nl2sql_validation` as the evaluation dataset. Set the data directory to this directory, or copy `dataset_info.json` and the JSONL files into LLaMA Factory's `data/` directory.