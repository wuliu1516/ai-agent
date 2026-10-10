"""按 JSON 给定的分组跑对照实验。用法：python backend/ab_run.py '<json>' [样本数] [划分]"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
SPEC = json.loads(sys.argv[1])
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 120
SPLIT = sys.argv[3] if len(sys.argv) > 3 else "validation"
COMMON = {
    "split": SPLIT,
    "model": "Qwen3.8-27B-FB8",
    "sample_limit": LIMIT,
    "sample_seed": 7,
    "use_target_schema": True,
    "optimization_note": "对照实验（自动脚本）",
}


def post(path: str, body: dict) -> dict:
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def wait(run_id: str) -> dict:
    while True:
        payload = get(f"/api/experiments/{run_id}")
        if payload["status"] not in {"queued", "running"}:
            return payload
        time.sleep(10)


for name, extra in SPEC.items():
    run_id = post("/api/experiments", {**COMMON, **extra})["run_id"]
    payload = wait(run_id)
    print(json.dumps({
        "group": name, "run_id": run_id, "accuracy": payload["accuracy"],
        "correct": payload["counts"]["correct"], "incorrect": payload["counts"]["incorrect"],
        "unjudged": payload["counts"]["unjudged"],
        "generation_failed": payload["counts"]["generation_failed"],
    }, ensure_ascii=False), flush=True)
