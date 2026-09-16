"""消息线格式与 LLM 客户端契约。

重点覆盖两件真实出过问题的事：

1. **工具往返**。历史实现在 OpenAI 分支把 assistant 的 `tool_calls` 丢掉了，
   下一轮请求里 `role="tool"` 的消息就成了孤儿（400），工具链在第二轮断掉。
   这里对两个 provider 都断言完整的往返形状。
2. **异步契约**。`stream()` 必须是异步生成器、`complete()` 必须是协程；
   没有 key 时必须抛 `LlmUnavailableError` 而不是静默返回空。
"""

from __future__ import annotations

import inspect
import json

import pytest

from dbchat.config import Settings
from dbchat.domain.errors import LlmUnavailableError
from dbchat.llm import LLMClient
from dbchat.llm.client import (
    CLASS_AMBIGUOUS,
    CLASS_DATA_QUESTION,
    _http_timeout,
    _sdk_http_module,
)
from dbchat.llm.events import Done, ErrorEvent, TextDelta, ThinkingDelta, ToolUse
from dbchat.llm.messages import (
    MAX_TOOL_RESULT_CHARS,
    ToolCall,
    assistant_message,
    normalize_messages,
    to_anthropic_messages,
    to_anthropic_tools,
    to_openai_messages,
    to_openai_tools,
    tool_result_message,
    truncate_tool_result,
    user_message,
)
from dbchat.llm.providers._compat import supported_kwargs

TOOL_SPEC = {
    "name": "run_sql",
    "description": "执行只读 SQL",
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string"}},
        "required": ["sql"],
    },
}


def tool_round_trip_history() -> list[object]:
    """一段典型的"问 → 调工具 → 回结果 → 收尾"历史（规范形态）。"""
    return [
        user_message("有多少学生？"),
        assistant_message(
            "",
            [ToolCall(id="call_1", name="run_sql", input={"sql": "SELECT COUNT(*) FROM students"})],
        ),
        tool_result_message("call_1", '{"row_count": 1, "content": "42"}'),
        assistant_message("共 42 名学生。"),
    ]


# ---- OpenAI 线格式 ----


def test_openai_tool_round_trip_keeps_tool_calls():
    """工具调用**不能**在转换中丢失（历史 bug 的回归测试）。"""
    messages = to_openai_messages("你是助手", tool_round_trip_history())

    assert messages[0] == {"role": "system", "content": "你是助手"}

    assistant = messages[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] is None
    calls = assistant["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["id"] == "call_1"
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "run_sql"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "sql": "SELECT COUNT(*) FROM students"
    }

    tool_msg = messages[3]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    assert "row_count" in tool_msg["content"]

    assert messages[4] == {"role": "assistant", "content": "共 42 名学生。"}


def test_openai_accepts_anthropic_shaped_history():
    """Anthropic 形状的历史（tool_use / tool_result 块）同样要能转出 tool_calls。"""
    history = [
        {"role": "user", "content": "有多少学生？"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "我来查一下"},
                {
                    "type": "tool_use",
                    "id": "call_9",
                    "name": "run_sql",
                    "input": {"sql": "SELECT 1"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_9", "content": "1"}
            ],
        },
    ]

    messages = to_openai_messages("", history)

    assistant = messages[1]
    assert assistant["content"] == "我来查一下"
    assert assistant["tool_calls"][0]["id"] == "call_9"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"sql": "SELECT 1"}
    assert messages[2] == {"role": "tool", "tool_call_id": "call_9", "content": "1"}


def test_openai_multiple_tool_calls_in_one_turn():
    """一轮里并行调用多个工具时，每个结果都要对上各自的 id。"""
    history = [
        user_message("查学生和班级"),
        assistant_message(
            "",
            [
                ToolCall(id="c1", name="run_sql", input={"sql": "SELECT 1"}),
                ToolCall(id="c2", name="table_detail", input={"table": "classes"}),
            ],
        ),
        tool_result_message("c1", "1"),
        tool_result_message("c2", "ok"),
    ]

    messages = to_openai_messages("", history)

    assert [c["id"] for c in messages[1]["tool_calls"]] == ["c1", "c2"]
    assert [(m["role"], m["tool_call_id"]) for m in messages[2:]] == [
        ("tool", "c1"),
        ("tool", "c2"),
    ]


# ---- Anthropic 线格式 ----


def test_anthropic_tool_round_trip():
    """Anthropic 侧：tool_use 块 + 紧随其后的 tool_result 块。"""
    messages = to_anthropic_messages(tool_round_trip_history())

    assert messages[0] == {"role": "user", "content": "有多少学生？"}

    assistant = messages[1]
    assert assistant["role"] == "assistant"
    blocks = assistant["content"]
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["id"] == "call_1"
    assert blocks[0]["name"] == "run_sql"
    assert blocks[0]["input"] == {"sql": "SELECT COUNT(*) FROM students"}

    results = messages[2]
    assert results["role"] == "user"
    assert results["content"][0]["type"] == "tool_result"
    assert results["content"][0]["tool_use_id"] == "call_1"
    assert "row_count" in results["content"][0]["content"]

    assert messages[3] == {"role": "assistant", "content": "共 42 名学生。"}


def test_anthropic_accepts_openai_shaped_history():
    """OpenAI 形状的历史（tool_calls + role=tool）也要能转成 Anthropic 块。"""
    history = [
        {"role": "user", "content": "你好"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c7",
                    "type": "function",
                    "function": {"name": "run_sql", "arguments": '{"sql": "SELECT 2"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c7", "content": "2"},
    ]

    messages = to_anthropic_messages(history)

    assert messages[1]["content"][0]["type"] == "tool_use"
    assert messages[1]["content"][0]["input"] == {"sql": "SELECT 2"}
    assert messages[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "c7",
        "content": "2",
    }


def test_anthropic_merges_consecutive_tool_results_into_one_user_turn():
    """Anthropic 要求 tool_result 全部并进**一条** user 消息，且角色交替。"""
    history = [
        assistant_message(
            "",
            [
                ToolCall(id="c1", name="a", input={}),
                ToolCall(id="c2", name="b", input={}),
            ],
        ),
        tool_result_message("c1", "r1"),
        tool_result_message("c2", "r2"),
    ]

    messages = to_anthropic_messages(history)

    roles = [m["role"] for m in messages]
    assert roles == ["assistant", "user"]
    assert [b["tool_use_id"] for b in messages[1]["content"]] == ["c1", "c2"]


def test_anthropic_result_lands_after_the_tool_use_it_answers():
    """tool_result 必须紧跟对应的 assistant tool_use（顺序不能乱）。"""
    history = [
        user_message("q1"),
        assistant_message("", [ToolCall(id="c1", name="t", input={})]),
        tool_result_message("c1", "r1"),
        user_message("q2"),
        assistant_message("", [ToolCall(id="c2", name="t", input={})]),
        tool_result_message("c2", "r2"),
    ]

    messages = to_anthropic_messages(history)

    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert messages[2]["content"][0]["tool_use_id"] == "c1"
    assert messages[4]["content"][0]["tool_use_id"] == "c2"


def test_normalize_messages_expands_legacy_tool_result_blocks():
    """user 消息里的 tool_result 块会被拆成独立的 tool 消息。"""
    normalized = normalize_messages(
        [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "x", "content": "out"}],
            }
        ]
    )
    assert len(normalized) == 1
    assert normalized[0].role == "tool"
    assert normalized[0].tool_call_id == "x"
    assert normalized[0].text == "out"


def test_normalize_messages_rejects_non_message():
    with pytest.raises(TypeError):
        normalize_messages([42])


# ---- 工具定义 ----


def test_tool_schema_conversion_both_providers():
    anthropic_tools = to_anthropic_tools([TOOL_SPEC])
    assert anthropic_tools == [TOOL_SPEC]

    openai_tools = to_openai_tools([TOOL_SPEC])
    assert openai_tools[0]["type"] == "function"
    assert openai_tools[0]["function"]["name"] == "run_sql"
    assert openai_tools[0]["function"]["parameters"] == TOOL_SPEC["input_schema"]


def test_tool_schema_accepts_missing_description_and_schema():
    """只有 name 的工具定义也要能渲染（缺省描述/空参数）。"""
    anthropic_tools = to_anthropic_tools([{"name": "ping"}])
    assert anthropic_tools[0]["description"] == ""
    assert anthropic_tools[0]["input_schema"] == {"type": "object", "properties": {}}
    assert to_openai_tools([{"name": "ping"}])[0]["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


def test_tool_schema_accepts_already_openai_shaped_specs():
    """历史上两种形状混着传，转换必须幂等。"""
    openai_shaped = {
        "type": "function",
        "function": {"name": "run_sql", "description": "d", "parameters": {"type": "object"}},
    }
    assert to_anthropic_tools([openai_shaped])[0]["name"] == "run_sql"
    assert to_openai_tools([openai_shaped])[0]["function"]["name"] == "run_sql"


def test_tool_schema_empty_input():
    assert to_anthropic_tools(None) == []
    assert to_openai_tools([]) == []


# ---- 工具结果截断 ----


def test_truncate_tool_result_keeps_head_and_tail():
    content = "A" * 3000 + "B" * 5000 + "C" * 500
    truncated = truncate_tool_result(content)

    assert len(truncated) < len(content)
    assert truncated.startswith("A" * 100)
    assert truncated.endswith("C" * 100)
    assert "已截断" in truncated


def test_truncate_tool_result_leaves_short_content_untouched():
    content = "短结果"
    assert truncate_tool_result(content) == content


def test_tool_result_truncation_applies_to_both_providers():
    """同一份超长结果，两个 provider 必须看到同样的截断文本。"""
    long_result = "x" * (MAX_TOOL_RESULT_CHARS * 2)
    history = [
        assistant_message("", [ToolCall(id="c1", name="t", input={})]),
        tool_result_message("c1", long_result),
    ]

    anthropic_text = to_anthropic_messages(history)[1]["content"][0]["content"]
    openai_text = to_openai_messages("", history)[1]["content"]

    assert anthropic_text == openai_text
    assert len(anthropic_text) < len(long_result)


# ---- LLM 客户端契约 ----


def bare_settings(**overrides) -> Settings:
    """没有任何 key 的配置（显式覆盖 .env，避免测试依赖本机配置）。"""
    base = {
        "llm_api_key": "",
        "anthropic_api_key": "",
        "llm_provider": "",
        "llm_api_base": "https://api.openai.com/v1",
        "llm_model": "test-model",
    }
    base.update(overrides)
    return Settings(**base)


async def test_unavailable_client_raises_llm_unavailable_error():
    """没配 key 时不该"开始流再报错"，而应在调用点直接抛。"""
    client = LLMClient(bare_settings())

    assert client.available is False
    with pytest.raises(LlmUnavailableError):
        async for _event in client.stream("sys", [user_message("hi")]):
            pass


async def test_complete_raises_when_no_key():
    client = LLMClient(bare_settings())
    with pytest.raises(LlmUnavailableError):
        await client.complete("sys", [user_message("hi")])


def test_stream_and_complete_are_async():
    """防止退回同步实现 —— 同步调用会阻塞整个事件循环。"""
    assert inspect.isasyncgenfunction(LLMClient.stream), "stream 必须是异步生成器"
    assert inspect.iscoroutinefunction(LLMClient.complete), "complete 必须是协程函数"


def test_provider_is_resolved_at_construction():
    """provider 在构造时确定，不在首次访问时才冻结。"""
    assert LLMClient(bare_settings(llm_provider="openai", llm_api_key="sk-x")).provider == "openai"
    assert (
        LLMClient(bare_settings(llm_provider="anthropic", anthropic_api_key="sk-ant-x")).provider
        == "anthropic"
    )
    # 未显式指定时按 key 前缀推断
    assert LLMClient(bare_settings(llm_api_key="sk-ant-abc")).provider == "anthropic"
    assert LLMClient(bare_settings(llm_api_key="sk-abc")).provider == "openai"


def test_available_reflects_key_presence():
    assert LLMClient(bare_settings(llm_api_key="sk-x")).available is True
    assert LLMClient(bare_settings(anthropic_api_key="sk-ant-x")).available is True
    assert LLMClient(bare_settings()).available is False


async def test_classify_falls_back_to_ambiguous_without_key():
    """没有 key 时分类退化为 AMBIGUOUS，而不是抛异常打断整个提问。"""
    client = LLMClient(bare_settings())
    assert await client.classify("有多少学生？") == CLASS_AMBIGUOUS


def test_classification_constants_match_router_contract():
    assert CLASS_DATA_QUESTION == "DATA_QUESTION"
    assert CLASS_AMBIGUOUS == "AMBIGUOUS"


# ---- SDK 兼容（两个真实踩过的坑）----


def test_supported_kwargs_drops_parameters_the_sdk_does_not_accept():
    """回归：新版 anthropic SDK 的签名里去掉了 `temperature`。

    硬传会 `TypeError: got an unexpected keyword argument 'temperature'`，
    整个 provider 直接不可用 —— 所以调用前必须按签名过滤。
    """

    def strict_create(model: str, max_tokens: int, messages: list) -> None:  # 没有 temperature
        ...

    kept = supported_kwargs(
        strict_create, model="m", max_tokens=8, messages=[], temperature=0.1
    )
    assert kept == {"model": "m", "max_tokens": 8, "messages": []}

    def tolerant_create(model: str, max_tokens: int, messages: list, temperature: float) -> None: ...

    kept = supported_kwargs(
        tolerant_create, model="m", max_tokens=8, messages=[], temperature=0.1
    )
    assert kept["temperature"] == 0.1


def test_http_timeout_uses_the_http_library_the_sdk_actually_uses():
    """回归：SDK 可能改用了改名打包的 httpx（本环境是 httpx2），

    传 `httpx.Timeout` 会被 SDK 显式拒绝（"this SDK uses httpx2"）。
    超时必须用 SDK 自己那份 HTTP 模块来构造。
    """
    import anthropic
    import openai

    settings = bare_settings(llm_api_key="sk-x", anthropic_api_key="sk-ant-x")
    for sdk in (anthropic, openai):
        http_module = _sdk_http_module(sdk)
        assert http_module is not None, f"{sdk.__name__} 的 HTTP 模块没找到"
        timeout = _http_timeout(sdk, settings)
        assert isinstance(timeout, http_module.Timeout)
        assert timeout.connect == settings.llm_connect_timeout_s


def test_client_construction_works_for_both_providers():
    """建客户端不联网，但会因为参数/超时对象不对而失败 —— 两个 provider 都要能建出来。"""
    settings = bare_settings(llm_provider="openai", llm_api_key="sk-fake")
    assert LLMClient(settings)._build_client() is not None

    anthropic_settings = bare_settings(
        llm_provider="anthropic", anthropic_api_key="sk-ant-fake"
    )
    assert LLMClient(anthropic_settings)._build_client() is not None


def test_anthropic_base_url_is_not_the_openai_default():
    """`llm_api_base` 的默认值是 OpenAI 的地址，不能当 Anthropic 网关传下去。"""
    default = LLMClient(bare_settings(llm_provider="anthropic", anthropic_api_key="sk-ant-x"))
    assert default._anthropic_base_url() is None

    custom = LLMClient(
        bare_settings(
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-x",
            llm_api_base="https://gateway.internal/anthropic",
        )
    )
    assert custom._anthropic_base_url() == "https://gateway.internal/anthropic"


# ---- 事件词汇表 ----


def test_event_types_carry_their_wire_names():
    """事件类型名与冻结的词汇表一致（上层按名字做展示映射）。"""
    assert ThinkingDelta("x").type == "thinking_delta"
    assert TextDelta("a").type == "text_delta"
    assert ToolUse(id="1", name="t").type == "tool_use"
    assert Done().type == "done"
    assert ErrorEvent("boom").type == "error"


def test_done_stop_reason_helpers():
    assert Done().has_tool_use is False
    assert Done(stop_reason="tool_use").has_tool_use is True
    assert Done(stop_reason="cancelled").cancelled is True
