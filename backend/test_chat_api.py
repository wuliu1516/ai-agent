"""Send one small OpenAI-compatible Chat Completions request using backend/.env."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
except ImportError:
    raise SystemExit(
        "缺少 python-dotenv。请先运行：.venv\\Scripts\\python.exe -m pip install -r backend\\requirements.txt"
    )


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent

# Match backend/main.py: process environment takes precedence, then backend/.env,
# then the project-root .env as a fallback.
load_dotenv(BACKEND_DIR / ".env", override=False)
load_dotenv(PROJECT_DIR / ".env", override=False)

base_url = os.environ.get("NL2SQL_API_BASE_URL", "").strip()
api_key = os.environ.get("NL2SQL_API_KEY", "").strip()
model = os.environ.get("NL2SQL_MODEL", "").strip()

from openai import OpenAI

client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = api_key
)

completion = client.chat.completions.create(
  model=model,
  messages=[{"role":"system","content":"You are a helpful assistant."},{"role":"user","content":"Which number is larger, 9.11 or 9.8?"}],
  temperature=0.5,
  top_p=1,
  max_tokens=1024,
  stream=False
)

print(completion.choices[0].message)