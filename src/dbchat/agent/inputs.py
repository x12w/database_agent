"""沙箱输入数据的构造与注入 —— compute 与 visualize 共用的唯一实现。

历史实现把同一段 30 行代码逐字抄了两遍（`app/agent/orchestrator.py:910-940`
与 `:1038-1072`）：把查询结果的行从 list 转成 dict、注入 `_input_data`、
在生成 `tool_call` 事件时再手动 `{k: v for k, v in tool_input.items() if k != "_input_data"}`
剥一次、写历史时又剥一次。四处剥漏一处，`_input_data` 就会跟着事件发到前端，
或者把整份查询结果塞进对话历史把上下文撑爆。

这里只有一条规则：**`_` 前缀的键是内部字段**，注入用 `inject_input_data`，
出站一律走 `strip_internal_keys`。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from dbchat.domain.query import QueryResult

__all__ = [
    "INPUT_DATA_KEY",
    "build_query_input_data",
    "input_injector",
    "inject_input_data",
]

#: 注入沙箱的查询结果字段名。
INPUT_DATA_KEY = "_input_data"

#: 只有这个工具需要查询结果注入。
INPUT_CONSUMER_TOOL = "run_python"


def build_query_input_data(results: Sequence[QueryResult]) -> dict[str, Any]:
    """把查询结果转成沙箱里 `input_data` 的形状。

    每份结果同时给两种行形态：`rows` 是 `{列名: 值}`（写代码最省事），
    `raw_rows` 是按列序的 list（快速取某一列）。两个都是历史实现的既有形状，
    提示词里也这么写的，不要改。
    """
    queries: list[dict[str, Any]] = []
    for result in results:
        columns = list(result.columns)
        queries.append(
            {
                "rows": [dict(row) for row in result.rows],
                "raw_rows": [[row.get(column) for column in columns] for row in result.rows],
                "row_count": result.row_count,
                "columns": columns,
            }
        )
    return {"queries": queries}


def inject_input_data(tool_input: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
    """把 `_input_data` 注入到 `run_python` 的入参里。

    其余工具原样返回 —— 只有 run_python 吃这份数据。
    """
    return {**tool_input, INPUT_DATA_KEY: input_data}


def input_injector(
    input_data: dict[str, Any],
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """构造 `AgentSpec.enrich_input` 用的注入器。"""
    def _inject(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_name != INPUT_CONSUMER_TOOL:
            return dict(tool_input)
        return inject_input_data(tool_input, input_data)

    return _inject
