"""计算 / 绘图工具 —— `run_python`。

输入数据通过 `_input_data` 注入：编排层（`dbchat.agent.specs`）在调用前
把它塞进入参，这个字段以 `_` 开头，因此**不会**出现在 `ToolStarted` 事件里，
也不会写进对话历史（`strip_internal_keys`）。历史实现是同一段 30 行代码在
compute 与 visualize 两个 agent 里各抄了一份（`orchestrator.py:910-940`
与 `:1038-1072`），这里只剩一条路径。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dbchat.tools.registry import ToolContext, ToolOutcome
from dbchat.tools.sandbox import run_python_code

logger = logging.getLogger(__name__)

__all__ = ["RUN_PYTHON_DESCRIPTION", "RunPythonParams", "run_python"]

_CJK_FONT = "Sarasa Gothic SC"

RUN_PYTHON_DESCRIPTION = (
    "在沙箱环境中执行 Python 代码，用于数学计算、统计分析和图表生成。"
    "输入数据通过 input_data 变量传入（包含查询结果）。"
    "将计算结果赋值给 result 变量。"
    "可用的库：math, statistics, json, decimal, fractions, "
    "collections, itertools, functools, random, datetime, "
    "matplotlib (含 matplotlib.pyplot)。\n"
    "生成图表时：import matplotlib; matplotlib.use('Agg'); "
    "import matplotlib.pyplot as plt; "
    f"设置中文字体: plt.rcParams['font.sans-serif'] = ['{_CJK_FONT}']; "
    "plt.rcParams['axes.unicode_minus'] = False; "
    "保存图表: plt.savefig('chart_<描述>.png', dpi=100, bbox_inches='tight'); "
    "plt.close(); 每个图表单独创建 figure，不要使用 subplots 组合。"
    "沙箱内禁止读取文件、禁止访问网络，写出的 chart_*.png 会自动被收集展示。"
)


class RunPythonParams(BaseModel):
    """`run_python` 入参。

    `extra="allow"` 是必需的：编排层会把 `_input_data`（查询结果）连同模型
    给出的入参一起传进来，校验时不能被丢掉。
    """

    model_config = ConfigDict(extra="allow")

    code: str = Field(
        description=(
            "要执行的 Python 代码。input_data 变量包含查询结果的 rows 和 row_count。"
            "将输出赋值给 result 变量。使用 print() 进行调试输出。"
            "生成图表时保存为 chart_*.png，新文件会自动被检测和展示。"
        )
    )


async def run_python(params: RunPythonParams, ctx: ToolContext) -> ToolOutcome:
    """在子进程沙箱里执行代码。"""
    code = params.code or ""
    if not code.strip():
        return ToolOutcome.failure("未提供 Python 代码", content="错误：未提供 Python 代码。")

    # 注入的入参（内部字段，见 `dbchat.agent.specs.inject_input_data`）
    input_data = _extract_input_data(params)

    result = await run_python_code(
        code,
        input_data=input_data,
        artifacts_dir=ctx.settings.artifacts_dir,
        timeout_s=float(ctx.settings.sandbox_timeout_s),
    )

    if not result.ok:
        detail = result.error_detail or result.error
        return ToolOutcome(
            ok=False,
            summary="代码执行失败",
            content=f"{result.error}\n{result.stderr}".strip(),
            payload={"execution_ms": result.execution_ms, "error_detail": detail, **result.extra},
            error_detail=detail,
        )

    parts: list[str] = []
    if result.result_text:
        parts.append(result.result_text)
    elif result.stdout.strip():
        parts.append(result.stdout.strip())
    else:
        parts.append("代码执行完成，但未设置 result 变量。")

    if result.artifacts:
        names = ", ".join(a.url.rsplit("/", 1)[-1] for a in result.artifacts)
        parts.append(f"\n[生成的图表: {names}]")
    if result.stderr.strip():
        parts.append(f"\n[stderr]:\n{result.stderr.strip()}")

    summary = "计算完成"
    if result.artifacts:
        summary = f"生成 {len(result.artifacts)} 张图表"
    return ToolOutcome(
        ok=True,
        summary=summary,
        content="\n".join(parts),
        payload={
            "execution_ms": result.execution_ms,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "charts": len(result.artifacts),
        },
        artifacts=result.artifacts,
    )


def _extract_input_data(params: RunPythonParams) -> dict[str, Any]:
    """取出编排层注入的 `_input_data`（pydantic 模型未声明该字段）。"""
    extra = getattr(params, "__pydantic_extra__", None) or {}
    data = extra.get("_input_data")
    if isinstance(data, dict):
        return data
    return {}
