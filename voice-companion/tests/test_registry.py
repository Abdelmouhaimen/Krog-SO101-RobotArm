import asyncio
import json

import pytest
from pydantic import BaseModel, Field

from so101_voice.tools.builtin import register_builtin_tools
from so101_voice.tools.registry import (
    Effect,
    FunctionCall,
    Tool,
    ToolContext,
    ToolRegistrationError,
    ToolRegistry,
)


class CountArgs(BaseModel):
    amount: int = Field(ge=1, le=10)


class NoArgs(BaseModel):
    pass


def make_registry():
    reg = ToolRegistry()
    state = {"count": 0, "keys": []}

    @reg.tool("mock_increment", "Add to a counter (side effect).", CountArgs, effect=Effect.VIRTUAL)
    async def inc(args: CountArgs, ctx: ToolContext):
        await asyncio.sleep(0.02)
        if ctx.cancelled.is_set():
            return None
        state["count"] += args.amount
        state["keys"].append(ctx.idempotency_key)
        return {"count": state["count"]}

    @reg.tool("slow", "Never finishes in time.", NoArgs, timeout_s=0.05)
    async def slow(args, ctx):
        await asyncio.sleep(10)

    @reg.tool("boom", "Raises.", NoArgs)
    async def boom(args, ctx):
        raise RuntimeError("kaput")

    @reg.tool("committed", "Needs a committed turn.", NoArgs, requires_committed_turn=True)
    async def committed(args, ctx):
        await asyncio.sleep(0.2)
        state["count"] += 100
        return "done"

    return reg, state


def content(res):
    return json.loads(res.content)


async def test_success_and_schema():
    reg, state = make_registry()
    defs = {d["name"]: d for d in reg.openai_tools()}
    assert defs["mock_increment"]["type"] == "function"  # Responses API function tool shape
    assert defs["mock_increment"]["parameters"]["properties"]["amount"]["maximum"] == 10
    assert "title" not in json.dumps(defs)
    res = await reg.execute(FunctionCall("c1", "mock_increment", '{"amount": 2}'))
    assert res.ok and content(res) == {"ok": True, "result": {"count": 2}}
    assert len(state["keys"]) == 1 and state["keys"][0].startswith("call-c1-")  # id + call fingerprint


@pytest.mark.parametrize(
    "name,args,code",
    [
        ("mock_increment", '{"amount": 99}', "invalid_arguments"),
        ("mock_increment", "{not json", "invalid_arguments"),
        ("mock_increment", "[1]", "invalid_arguments"),
        ("nope", "{}", "unknown_tool"),
        ("slow", "{}", "timeout"),
        ("boom", "", "tool_error"),
    ],
)
async def test_failures_become_error_results(name, args, code):
    reg, state = make_registry()
    res = await reg.execute(FunctionCall("x", name, args))
    assert not res.ok and content(res)["error"]["code"] == code
    assert state["count"] == 0


async def test_duplicate_call_ids_execute_once_inflight_and_after():
    reg, state = make_registry()
    a, b = await asyncio.gather(
        reg.execute(FunctionCall("dup", "mock_increment", '{"amount": 1}')),
        reg.execute(FunctionCall("dup", "mock_increment", '{"amount": 1}')),
    )
    c = await reg.execute(FunctionCall("dup", "mock_increment", '{"amount": 1}'))
    assert state["count"] == 1 and reg.executions == 1
    assert sorted([a.duplicate, b.duplicate]) == [False, True] and c.duplicate
    assert a.content == b.content == c.content


async def test_cancel_specific_call_prevents_side_effect():
    reg, state = make_registry()
    t = asyncio.create_task(reg.execute(FunctionCall("c9", "mock_increment", '{"amount": 5}')))
    await asyncio.sleep(0.005)
    assert reg.cancel(["c9"], "turn cancelled") == 1
    res = await t
    assert not res.ok and content(res)["error"]["code"] == "cancelled"
    await asyncio.sleep(0.05)
    assert state["count"] == 0


async def test_new_turn_cancels_committed_tools_and_refuses_stale():
    reg, state = make_registry()
    t = asyncio.create_task(reg.execute(FunctionCall("k1", "committed", "{}")))
    await asyncio.sleep(0.01)
    reg.new_turn()  # user barged in
    res = await t
    assert content(res)["error"]["code"] == "cancelled" and state["count"] == 0


async def test_cancel_all():
    reg, _ = make_registry()
    t = asyncio.create_task(reg.execute(FunctionCall("s1", "slow", "{}")))
    await asyncio.sleep(0.01)
    assert reg.cancel_all("disconnect") == 1
    assert not (await t).ok


def test_registration_rules():
    reg = ToolRegistry()

    async def h(a, c):
        return None

    with pytest.raises(ToolRegistrationError):
        reg.register(Tool("bad name!", "d", NoArgs, h))
    with pytest.raises(ToolRegistrationError, match="physical"):
        reg.register(
            Tool(
                "go_rest",
                "moves",
                NoArgs,
                h,
                effect=Effect.PHYSICAL,
                requires_committed_turn=True,
            )
        )
    allowed = ToolRegistry(allow_physical_tools=True)
    with pytest.raises(ToolRegistrationError, match="requires_committed_turn"):
        allowed.register(Tool("go_rest", "moves", NoArgs, h, effect=Effect.PHYSICAL))
    allowed.register(
        Tool("go_rest", "moves", NoArgs, h, effect=Effect.PHYSICAL, requires_committed_turn=True)
    )
    assert allowed.openai_tools()[0]["name"] == "go_rest"
    reg.register(Tool("t", "d", NoArgs, h))
    with pytest.raises(ToolRegistrationError):
        reg.register(Tool("t", "d", NoArgs, h))


async def test_builtin_time_tool_is_harmless_and_validated():
    reg = ToolRegistry()
    register_builtin_tools(reg)
    assert reg.names == ["get_local_time"]
    res = await reg.execute(FunctionCall("t1", "get_local_time", '{"timezone": "Europe/Paris"}'))
    assert res.ok and "+01:00" in content(res)["result"]["iso"] or "+02:00" in content(res)["result"]["iso"]
    bad = await reg.execute(FunctionCall("t2", "get_local_time", '{"timezone": "Mars/Olympus"}'))
    assert content(bad)["error"]["code"] == "invalid_arguments"
    ok = await reg.execute(FunctionCall("t3", "get_local_time", "{}"))
    assert ok.ok


async def test_a_reused_call_id_with_different_arguments_is_a_new_call():
    """Only an identical call (id, tool and arguments) is a duplicate; the idempotency keys differ."""
    reg, state = make_registry()
    a = await reg.execute(FunctionCall("same", "mock_increment", '{"amount": 1}'))
    b = await reg.execute(FunctionCall("same", "mock_increment", '{"amount": 2}'))
    c = await reg.execute(
        FunctionCall("same", "mock_increment", '{ "amount": 2 }')
    )  # same call, other spacing
    assert not a.duplicate and not b.duplicate and c.duplicate
    assert state["count"] == 3 and len(set(state["keys"])) == 2
