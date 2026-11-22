"""OpenAI Responses API client: one streaming request (``POST /v1/responses``, ``stream: true``) over SSE.

Verified against the OpenAI docs on 2026-11-08 (see ``docs/ARCHITECTURE.md`` §1):

* ``gpt-6-luna`` supports the Responses API, streaming and function calling.
* ``reasoning.effort`` defaults to ``medium``, so ``none`` is sent explicitly.
* ``temperature`` is only valid with effort ``none``.
* ``store`` defaults to true, so it is always sent as ``false``.
* **Events used:**
  * ``response.output_text.delta`` (``delta``);
  * ``response.output_item.done`` (a complete ``function_call`` item with ``call_id``, ``name`` and the
    full ``arguments`` JSON);
  * ``response.completed`` / ``response.incomplete`` (``response.output``, ``response.usage`` with
    ``input_tokens_details.cached_tokens`` / ``cache_write_tokens`` and
    ``output_tokens_details.reasoning_tokens``);
  * ``response.failed`` / ``error``.
* **Cancelling:** close the HTTP stream. No usage is reported for an aborted stream, but tokens are
  still billed, so the caller estimates them.

Function-call arguments are only acted upon once complete (``output_item.done``); deltas are ignored.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, message: str, *, status: int | None = None, fatal: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.fatal = fatal


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @classmethod
    def from_api(cls, u: dict[str, Any] | None) -> Usage | None:
        if not u:
            return None
        inp = u.get("input_tokens_details") or {}
        out = u.get("output_tokens_details") or {}
        return cls(
            input_tokens=int(u.get("input_tokens") or 0),
            cached_tokens=int(inp.get("cached_tokens") or 0),
            cache_write_tokens=int(inp.get("cache_write_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            reasoning_tokens=int(out.get("reasoning_tokens") or 0),
        )


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallItem:
    call_id: str
    name: str
    arguments: str
    item: dict[str, Any]  # the raw output item, replayed verbatim in the follow-up request


@dataclass(frozen=True)
class Finished:
    status: str  # completed | incomplete
    output: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage | None = None
    incomplete_reason: str | None = None
    response_id: str = ""


LLMEvent = TextDelta | ToolCallItem | Finished


def build_request(
    *,
    model: str,
    instructions: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    reasoning_effort: str,
    verbosity: str | None,
    temperature: float | None,
    max_output_tokens: int,
    prompt_cache_key: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "stream": True,
        "store": False,
        "reasoning": {"effort": reasoning_effort},
        "max_output_tokens": max_output_tokens,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = False  # at most one call per round keeps tool turns simple
    if verbosity is not None:
        body["text"] = {"verbosity": verbosity}
    if temperature is not None:
        if reasoning_effort != "none":
            raise ValueError("temperature requires reasoning effort 'none'")
        body["temperature"] = temperature
    if response_format is not None:
        body.setdefault("text", {})["format"] = response_format
    if prompt_cache_key:
        body["prompt_cache_key"] = prompt_cache_key
    return body


class ResponsesClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("an OpenAI API key is required")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def retrieve_model(self, model: str) -> dict[str, Any] | None:
        """``GET /models/{model}``: None if the model does not exist or this key cannot use it."""
        r = await self._http.get(f"/models/{model}")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise LLMError(
                _error_text(r.status_code, r.text), status=r.status_code, fatal=r.status_code in (401, 403)
            )
        data: dict[str, Any] = r.json()
        return data

    async def stream(self, body: dict[str, Any]) -> AsyncIterator[LLMEvent]:
        """Yield events for one request. Closing the iterator (or cancelling the task) aborts it."""
        async with self._http.stream("POST", "/responses", json=body) as r:
            if r.status_code >= 400:
                text = (await r.aread()).decode(errors="replace")
                status = r.status_code
                raise LLMError(
                    _error_text(status, text, body.get("model")),
                    status=status,
                    fatal=status in (400, 401, 403, 404),
                )
            async for ev in _parse_sse(r.aiter_lines()):
                out = _to_event(ev)
                if out is not None:
                    yield out
                    if isinstance(out, Finished):
                        return
            raise LLMError("the response stream ended without a completion event")


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                payload = "\n".join(data)
                data = []
                if payload.strip() == "[DONE]":
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("unparsable SSE payload: %.200s", payload)
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        # "event:", "id:", "retry:" and comments are not needed: the JSON carries "type"
    if data:
        payload = "\n".join(data)
        if payload.strip() != "[DONE]":
            yield json.loads(payload)


def _to_event(ev: dict[str, Any]) -> LLMEvent | None:
    t = ev.get("type")
    if t == "response.output_text.delta":
        return TextDelta(str(ev.get("delta") or ""))
    if t == "response.output_item.done":
        item = ev.get("item") or {}
        if item.get("type") == "function_call":
            return ToolCallItem(
                call_id=str(item.get("call_id") or ""),
                name=str(item.get("name") or ""),
                arguments=str(item.get("arguments") or ""),
                item=item,
            )
        return None
    if t in ("response.completed", "response.incomplete"):
        resp = ev.get("response") or {}
        details = resp.get("incomplete_details") or {}
        return Finished(
            status=str(resp.get("status") or t.rsplit(".", 1)[-1]),
            output=list(resp.get("output") or []),
            usage=Usage.from_api(resp.get("usage")),
            incomplete_reason=details.get("reason"),
            response_id=str(resp.get("id") or ""),
        )
    if t == "response.failed":
        err = (ev.get("response") or {}).get("error") or {}
        raise LLMError(f"OpenAI response failed: {err.get('code')}: {err.get('message')}")
    if t == "error":
        raise LLMError(f"OpenAI stream error {ev.get('code')}: {ev.get('message')}")
    return None


def _error_text(status: int, text: str, model: object = None) -> str:
    try:
        err = json.loads(text).get("error") or {}
        msg = f"{err.get('code') or err.get('type') or ''}: {err.get('message') or ''}".strip(": ")
    except (ValueError, AttributeError):
        msg = text[:200]
    if status == 404 and model:
        return (
            f"OpenAI model {model!r} is not available to this API key (HTTP 404: {msg}). "
            "No other model is substituted; change llm.model and [cost.prices] if needed."
        )
    if status == 401:
        return f"OpenAI rejected the API key (HTTP 401): check OPENAI_API_KEY ({msg})"
    return f"OpenAI HTTP {status}: {msg}"


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token) for aborted streams without usage."""
    return max(1, len(text) // 4) if text else 0
