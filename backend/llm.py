"""LangChain-based gateway for the OpenAI-compatible chat service."""

from __future__ import annotations

import re
import threading
from typing import Any, Callable, TypeVar

import openai
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.exceptions import OutputParserException
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T")


class FatalModelServiceError(RuntimeError):
    pass


def clean_llm_text(content: Any) -> str:
    """Normalise a chat reply: join content blocks, strip <think> blocks and Markdown fences."""
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    text = str(content or "").strip()
    thinking_ends = list(re.finditer(r"</think\s*>", text, flags=re.IGNORECASE))
    if thinking_ends:
        text = text[thinking_ends[-1].end():].strip()
    text = re.sub(r"</?think\b[^>]*>", "", text, flags=re.IGNORECASE).strip()
    return re.sub(r"^```(?:sql|json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()


class ModelGateway:
    """Thin wrapper over ChatOpenAI: built-in retry/backoff, thinking switch, structured output."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        disable_thinking: bool = False,
        temperature: float = 0,
        max_tokens: int = 2048,
        timeout: float = 90,
        max_retries: int = 3,
    ) -> None:
        base = base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        self._base_url = base
        self._api_key = api_key or "EMPTY"
        self._extra_body = {"chat_template_kwargs": {"enable_thinking": False}} if disable_thinking else None
        self._temperature = temperature
        self._default_max_tokens = max_tokens
        self._timeout = timeout
        self._max_retries = max_retries
        self._clients: dict[tuple[str, int], ChatOpenAI] = {}
        self._lock = threading.Lock()

    def chat_model(self, model: str, max_tokens: int | None = None) -> ChatOpenAI:
        key = (model, max_tokens or self._default_max_tokens)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = ChatOpenAI(
                    model=model,
                    base_url=self._base_url,
                    api_key=self._api_key,
                    temperature=self._temperature,
                    max_tokens=key[1],
                    timeout=self._timeout,
                    max_retries=self._max_retries,
                    extra_body=self._extra_body,
                )
                self._clients[key] = client
            return client

    @staticmethod
    def _fatal(error: Exception) -> FatalModelServiceError:
        if isinstance(error, openai.APIStatusError):
            detail = str(getattr(error, "message", error))[:600]
            return FatalModelServiceError(f"模型服务返回 HTTP {error.status_code}: {detail}")
        return FatalModelServiceError(f"无法连接模型服务（{type(error).__name__}）：{str(error)[:300]}")

    def complete(self, model: str, system: str, user: str, *, max_tokens: int | None = None) -> str:
        try:
            reply = self.chat_model(model, max_tokens).invoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
        except (openai.APIStatusError, openai.APIConnectionError) as error:
            raise self._fatal(error) from error
        text = clean_llm_text(reply.content)
        if not text:
            raise RuntimeError("模型没有返回内容")
        return text

    def structured(
        self,
        model: str,
        schema: type[BaseModel],
        system: str,
        user: str,
        *,
        from_model: Callable[[Any], T],
        from_text: Callable[[str], T],
        max_tokens: int | None = None,
    ) -> T:
        """Ask for schema-constrained output; fall back to plain text + ``from_text`` if the
        server rejects response_format or the reply does not validate."""
        try:
            runnable = self.chat_model(model, max_tokens).with_structured_output(schema, method="json_schema")
            parsed = runnable.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            if parsed is not None:
                return from_model(parsed)
        except openai.BadRequestError:
            pass
        except (openai.APIStatusError, openai.APIConnectionError) as error:
            raise self._fatal(error) from error
        except (OutputParserException, ValidationError, ValueError, TypeError):
            pass
        return from_text(self.complete(model, system, user, max_tokens=max_tokens))
