"""Typed tool registry for LLM function calls (OpenAI Responses ``function_call`` → ``function_call_output``).

* Arguments are a pydantic model; its JSON schema is what the LLM sees, and it validates every call.
* Each call has the provider's call id (``FunctionCall.id``) plus our own ``request_id`` for logs.
* Duplicate call ids never execute twice: an in-flight duplicate awaits the same task, a finished one
  returns the cached result. Side-effecting tools get the call id as their idempotency key.
* Timeouts and handler exceptions become an error result the LLM can talk about; nothing raises out.
* ``cancel_all`` cancels in-flight calls (barge-in, disconnect, shutdown). A cancelled call produces no
  side effect afterwards: handlers must check ``ctx.cancelled`` before acting, and tools that need a
  committed turn (``requires_committed_turn``) are refused when the turn was superseded.
* ``Effect.PHYSICAL`` tools are refused at registration unless ``allow_physical_tools=True`` — which
  this delivery never sets — and must require a committed turn. The LLM itself only runs on a confirmed
  Flux ``EndOfTurn``, so no tool call can come from a speculative transcript. Talk Mode is NOT a tool:
  audio playback drives it.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

A = TypeVar("A", bound=BaseModel)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Effect(enum.Enum):
    NONE = "none"  # pure: time, maths
    READ = "read"  # reads external state (robot status, behavior list)
    VIRTUAL = "virtual"  # changes non-physical state (settings, memory)
    PHYSICAL = "physical"  # moves the robot — refused in this delivery


class ToolRegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class FunctionCall:
    id: str
    name: str
    arguments: str | dict[str, Any]
    client_side: bool = True


@dataclass(frozen=True)
class FunctionResult:
    id: str
    name: str
    ok: bool
    content: str  # JSON text sent back to the LLM
    duplicate: bool = False
    elapsed_s: float = 0.0
    output: list[dict[str, Any]] | None = None  # multimodal Responses tool output; never logged


@dataclass(frozen=True)
class ToolOutput:
    metadata: dict[str, Any]
    parts: list[dict[str, Any]]


@dataclass
class ToolContext:
    call_id: str
    request_id: str
    turn_id: int
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    fingerprint: str = ""  # hash of tool name + arguments

    @property
    def idempotency_key(self) -> str:
        """Same call (id, tool and arguments) -> same key, so the robot never repeats a movement."""
        return f"call-{self.call_id}-{self.fingerprint[:10]}" if self.fingerprint else f"call-{self.call_id}"


def call_key(call: FunctionCall) -> str:
    """Dedupe key: a call is a duplicate only if its id, tool name and arguments all match."""
    raw: Any = call.arguments
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            pass
    canon = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{call.name}\n{canon}".encode()).hexdigest()
    return f"{call.id}:{digest}"


Handler = Callable[[A, ToolContext], Awaitable[Any]]
AnyHandler = Callable[[Any, ToolContext], Awaitable[Any]]


@dataclass(frozen=True)
class Tool(Generic[A]):
    name: str
    description: str
    args_model: type[A]
    handler: Handler[A]
    effect: Effect = Effect.NONE
    timeout_s: float = 5.0
    requires_committed_turn: bool = False
    # Adjusts the JSON schema shown to the LLM on every request (e.g. an enum of the robot behaviors
    # available right now). Arguments are still validated by ``args_model`` and the handler.
    schema_hook: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def parameters_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = _strip_titles(self.args_model.model_json_schema())
        return self.schema_hook(schema) if self.schema_hook is not None else schema


def _strip_titles(node: Any) -> Any:
    """Make pydantic's schema LLM-friendly: drop titles, collapse ``anyOf: [X, null]`` to ``X``
    (optional-ness is expressed by not being in ``required``) and drop ``default: null``."""
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
            inner = next(s for s in any_of if s != {"type": "null"})
            node = {**inner, **{k: v for k, v in node.items() if k != "anyOf"}}
        return {
            k: _strip_titles(v)
            for k, v in node.items()
            if k != "title" and not (k == "default" and v is None)
        }
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def _err(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": {"code": code, "message": message}})


class ToolRegistry:
    def __init__(self, *, allow_physical_tools: bool = False, result_cache_size: int = 256) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        self._allow_physical = allow_physical_tools
        self._inflight: dict[str, tuple[asyncio.Task[FunctionResult], ToolContext]] = {}
        self._done: OrderedDict[str, FunctionResult] = OrderedDict()
        self._cache_size = result_cache_size
        self.turn_id = 0  # bumped by the session when the user starts a new turn (barge-in)
        self.executions = 0

    # ------------------------------------------------------------------ registration
    def register(self, tool: Tool[Any]) -> Tool[Any]:
        if not NAME_RE.match(tool.name):
            raise ToolRegistrationError(f"invalid tool name {tool.name!r}")
        if tool.name in self._tools:
            raise ToolRegistrationError(f"tool {tool.name!r} already registered")
        if tool.effect is Effect.PHYSICAL and not self._allow_physical:
            raise ToolRegistrationError(
                f"tool {tool.name!r} moves the robot; physical tools are disabled in this delivery"
            )
        if tool.effect is Effect.PHYSICAL and not tool.requires_committed_turn:
            raise ToolRegistrationError(f"physical tool {tool.name!r} must set requires_committed_turn")
        if tool.timeout_s <= 0:
            raise ToolRegistrationError("timeout_s must be positive")
        tool.parameters_schema()  # fail early on a broken model
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        args_model: type[A],
        *,
        effect: Effect = Effect.NONE,
        timeout_s: float = 5.0,
        requires_committed_turn: bool = False,
        schema_hook: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> Callable[[Handler[A]], Handler[A]]:
        def deco(fn: Handler[A]) -> Handler[A]:
            self.register(
                Tool(
                    name, description, args_model, fn, effect, timeout_s, requires_committed_turn, schema_hook
                )
            )
            return fn

        return deco

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        """Function tools for the Responses API ``tools`` field (validated again locally on every call)."""
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters_schema(),
                "strict": False,  # optional fields stay optional; pydantic validates the complete arguments
            }
            for t in self._tools.values()
        ]

    def action_catalog(self) -> list[dict[str, Any]]:
        """Only mutations are response actions; read-only information is prepared before inference."""
        return [
            tool
            for tool in self.openai_tools()
            if self._tools[tool["name"]].effect in (Effect.PHYSICAL, Effect.VIRTUAL)
        ]

    # ------------------------------------------------------------------ execution
    def new_turn(self) -> int:
        """The user started speaking again: calls from the previous turn are no longer committed."""
        self.turn_id += 1
        for _, (task, ctx) in list(self._inflight.items()):
            tool = self._tools.get(task.get_name())
            if tool is not None and tool.requires_committed_turn:
                ctx.cancelled.set()
                task.cancel()
        return self.turn_id

    async def execute(self, call: FunctionCall) -> FunctionResult:
        key = call_key(call)
        if key in self._done:
            cached = self._done[key]
            log.info("duplicate function call %s (%s) — returning cached result", call.id, call.name)
            return FunctionResult(
                cached.id, cached.name, cached.ok, cached.content, duplicate=True, output=cached.output
            )
        if key in self._inflight:
            log.info("duplicate in-flight function call %s — awaiting the original", call.id)
            res = await asyncio.shield(self._inflight[key][0])
            return FunctionResult(res.id, res.name, res.ok, res.content, duplicate=True, output=res.output)
        if any(k.split(":", 1)[0] == call.id for k in (*self._done, *self._inflight)):
            log.warning(
                "call id %s reused for a different call (%s): treated as a new call", call.id, call.name
            )

        ctx = ToolContext(
            call_id=call.id,
            request_id=uuid.uuid4().hex[:12],
            turn_id=self.turn_id,
            fingerprint=key.split(":", 1)[1],
        )
        task = asyncio.create_task(self._run(call, ctx), name=call.name)
        self._inflight[key] = (task, ctx)
        try:
            res = await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                raise  # the caller itself was cancelled; the task's own outcome is still recorded
            res = FunctionResult(call.id, call.name, False, _err("cancelled", "the call was cancelled"))
        finally:
            self._inflight.pop(key, None)
        self._remember(key, res)
        return res

    async def _run(self, call: FunctionCall, ctx: ToolContext) -> FunctionResult:
        t0 = time.monotonic()

        def done(ok: bool, content: str) -> FunctionResult:
            return FunctionResult(call.id, call.name, ok, content, elapsed_s=time.monotonic() - t0)

        tool = self._tools.get(call.name)
        if tool is None:
            return done(False, _err("unknown_tool", f"no tool named {call.name!r}"))
        try:
            raw: Any = call.arguments
            if isinstance(raw, str):
                raw = json.loads(raw) if raw.strip() else {}  # LLMs send "" for no-argument tools
            if raw is None:
                raw = {}
            args = tool.args_model.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            return done(False, _err("invalid_arguments", _short_validation(e)))
        if tool.requires_committed_turn and ctx.turn_id != self.turn_id:
            return done(False, _err("superseded", "the user changed the request; not executed"))
        self.executions += 1
        log.info("tool %s start (call=%s req=%s)", tool.name, call.id, ctx.request_id)
        try:
            value = await asyncio.wait_for(tool.handler(args, ctx), tool.timeout_s)
        except TimeoutError:
            ctx.cancelled.set()
            return done(False, _err("timeout", f"{tool.name} did not finish within {tool.timeout_s:g}s"))
        except asyncio.CancelledError:
            ctx.cancelled.set()
            return done(False, _err("cancelled", "the call was cancelled"))
        except Exception as e:
            log.exception("tool %s failed", tool.name)
            return done(False, _err("tool_error", f"{type(e).__name__}: {e}"))
        parts = value.parts if isinstance(value, ToolOutput) else None
        value = value.metadata if isinstance(value, ToolOutput) else value
        try:
            content = json.dumps({"ok": True, "result": value}, default=str)
        except (TypeError, ValueError) as e:
            return done(False, _err("bad_result", str(e)))
        return FunctionResult(
            call.id, call.name, True, content, elapsed_s=time.monotonic() - t0, output=parts
        )

    def cancel(self, call_ids: list[str] | tuple[str, ...], reason: str) -> int:
        """Cancel specific in-flight calls."""
        n = 0
        for key, entry in list(self._inflight.items()):
            if key.split(":", 1)[0] in call_ids:
                entry[1].cancelled.set()
                if not entry[0].done():
                    entry[0].cancel()
                    n += 1
        if n:
            log.info("cancelled %d tool call(s): %s", n, reason)
        return n

    def cancel_all(self, reason: str) -> int:
        n = 0
        for _, (task, ctx) in list(self._inflight.items()):
            ctx.cancelled.set()
            if not task.done():
                task.cancel()
                n += 1
        if n:
            log.info("cancelled %d in-flight tool call(s): %s", n, reason)
        return n

    def _remember(self, key: str, res: FunctionResult) -> None:
        self._done[key] = res
        while len(self._done) > self._cache_size:
            self._done.popitem(last=False)


def _short_validation(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'arguments'}: {err['msg']}" for err in e.errors()
        )
    return str(e)
