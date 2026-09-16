"""工具层 —— 工具定义的唯一来源与执行入口。

对外只暴露三件事：

* `build_registry()` 组装全部工具（每个工具一条记录，含可见范围）；
* `ToolSpec` 的 JSON Schema 由 pydantic 模型推导，与运行时校验同源；
* `ToolRegistry.run()` 把**任何**异常变成一条失败结果（绝不炸掉整次提问）。

工具名到线上事件的映射在 `dbchat.events.project.TOOL_VIEW` —— 新增工具时
两边都要改，`tests/unit/test_specs.py` 会强制这一点。
"""

from __future__ import annotations

from functools import lru_cache

from dbchat.config import Settings, get_settings
from dbchat.tools import compute, database, docs, schema_tools, web
from dbchat.tools.registry import (
    AGENT_ANALYSIS,
    AGENT_ANSWER,
    AGENT_COMPUTE,
    AGENT_EXPLORE,
    AGENT_NAMES,
    AGENT_SQL,
    AGENT_VISUALIZE,
    MAX_PARALLEL_TOOL_CALLS,
    ToolContext,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
    db_timeout,
    llm_timeout,
    sandbox_timeout,
    search_timeout,
    static_description,
    strip_internal_keys,
)

__all__ = [
    "AGENT_ANALYSIS",
    "AGENT_ANSWER",
    "AGENT_COMPUTE",
    "AGENT_EXPLORE",
    "AGENT_NAMES",
    "AGENT_SQL",
    "AGENT_VISUALIZE",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "build_registry",
    "get_registry",
    "strip_internal_keys",
]


def _specs() -> list[ToolSpec]:
    """全部工具的唯一定义处。"""
    sql_agents = frozenset({AGENT_SQL})
    # 分析 agent 看结构但**不**看 query_database —— 它只做计划，不取数。
    analysis_agents = frozenset({AGENT_ANALYSIS})
    compute_agents = frozenset({AGENT_COMPUTE, AGENT_VISUALIZE})
    explore_agents = frozenset({AGENT_EXPLORE})
    docs_agents = frozenset({AGENT_SQL, AGENT_ANALYSIS, AGENT_ANSWER, AGENT_EXPLORE})

    return [
        ToolSpec(
            name="query_database",
            label="查询数据",
            params=database.QueryDatabaseParams,
            handler=database.query_database,
            description=database.query_tool_description,
            agents=sql_agents | explore_agents,
            timeout=db_timeout,
            needs_database=True,
        ),
        ToolSpec(
            name="run_python",
            label="执行计算",
            params=compute.RunPythonParams,
            handler=compute.run_python,
            description=static_description(compute.RUN_PYTHON_DESCRIPTION),
            agents=compute_agents | explore_agents,
            timeout=sandbox_timeout,
        ),
        ToolSpec(
            name="list_tables",
            label="列出数据表",
            params=schema_tools.EmptyParams,
            handler=schema_tools.list_tables,
            description=static_description(
                "列出当前数据库中所有表和视图，包含行数估算。用于了解数据库的整体结构。"
            ),
            agents=explore_agents | sql_agents,
            timeout=db_timeout,
        ),
        ToolSpec(
            name="describe_table",
            label="查看表结构",
            params=schema_tools.DescribeTableParams,
            handler=schema_tools.describe_table,
            description=static_description(
                "查看指定表的结构信息：列名、数据类型、是否可空、键类型、注释，"
                "以及该表的外键关系。用于深入了解表结构。"
            ),
            agents=explore_agents | sql_agents,
            timeout=db_timeout,
        ),
        ToolSpec(
            name="sample_data",
            label="抽样查看数据",
            params=schema_tools.SampleDataParams,
            handler=schema_tools.sample_data,
            description=static_description(
                "从指定表中抽样查看数据，了解实际数据内容和格式。"
                "可用于发现列的值分布、数据模式等。默认返回 5 行。"
            ),
            agents=explore_agents | sql_agents,
            timeout=db_timeout,
            needs_database=True,
        ),
        ToolSpec(
            name="get_schema_summary",
            label="获取结构概览",
            params=schema_tools.EmptyParams,
            handler=schema_tools.get_schema_summary,
            description=static_description(
                "【推荐优先使用】获取完整的数据库结构概览。一次调用返回："
                "所有表（含列、类型、键、注释）、每张表的抽样数据、"
                "所有外键关系（含预构建的 JOIN 语句）、值分布统计。"
                "这是最高效的工具 —— 替代多次 list_tables + describe_table + sample_data 调用。"
            ),
            agents=analysis_agents | sql_agents,
            timeout=llm_timeout,
        ),
        ToolSpec(
            name="get_table_detail",
            label="查看表详情",
            params=schema_tools.TableNameParams,
            handler=_table_detail,
            description=static_description(
                "获取单张表的完整信息：列定义、抽样数据、关联表（含 JOIN 示例）、值分布。"
                "比 describe_table + sample_data 更全面。"
            ),
            agents=analysis_agents | sql_agents,
            timeout=llm_timeout,
        ),
        ToolSpec(
            name="analyze_column",
            label="分析列取值",
            params=schema_tools.AnalyzeColumnParams,
            handler=schema_tools.analyze_column,
            description=static_description(
                "对指定列进行统计分析：最小值、最大值、平均值、唯一值数量、"
                "NULL 数量、Top 10 值分布。适用于数值列和分类列。"
            ),
            agents=analysis_agents | sql_agents,
            timeout=db_timeout,
            needs_database=True,
        ),
        ToolSpec(
            name="find_related_tables",
            label="查找关联表",
            params=schema_tools.FindRelatedParams,
            handler=schema_tools.find_related_tables,
            description=static_description(
                "查找与指定表有外键关系的所有表，返回预构建的 JOIN 语句（单跳）。"
                "用于了解表之间的直接关联关系。如需跨多张表联查，用 find_join_path。"
            ),
            agents=analysis_agents | sql_agents,
            timeout=db_timeout,
        ),
        ToolSpec(
            name="find_join_path",
            label="查找联查路径",
            params=schema_tools.FindJoinPathParams,
            handler=schema_tools.find_join_path,
            description=static_description(
                "查找两个表之间的最短 FK 连接路径（多跳遍历）。"
                "返回从 source_table 到 target_table 的完整 JOIN 链（含每跳 SQL）。"
                "用于规划跨多张表的复杂联查。"
            ),
            agents=analysis_agents | sql_agents,
            timeout=db_timeout,
        ),
        ToolSpec(
            name="get_suggested_questions",
            label="推荐问题",
            params=schema_tools.EmptyParams,
            handler=schema_tools.get_suggested_questions,
            description=static_description(
                "获取针对当前数据库的推荐查询问题（由 AI 基于数据库结构自动生成）。"
                "当用户不确定要问什么时使用。"
            ),
            agents=frozenset({AGENT_ANSWER, AGENT_EXPLORE}),
            timeout=db_timeout,
        ),
        ToolSpec(
            name="search_documentation",
            label="检索文档",
            params=docs.SearchDocsParams,
            handler=docs.search_documentation,
            description=static_description(docs.SEARCH_DOCS_DESCRIPTION),
            agents=docs_agents,
            timeout=llm_timeout,
        ),
        ToolSpec(
            name="search_web",
            label="联网检索",
            params=web.SearchWebParams,
            handler=web.search_web,
            description=static_description(web.SEARCH_WEB_DESCRIPTION),
            agents=frozenset({AGENT_SQL, AGENT_ANALYSIS}),
            timeout=search_timeout,
        ),
        ToolSpec(
            name="ask_admin",
            label="向管理员确认",
            params=docs.AskAdminParams,
            handler=docs.ask_admin,
            description=static_description(docs.ASK_ADMIN_DESCRIPTION),
            agents=analysis_agents | frozenset({AGENT_EXPLORE}),
            timeout=db_timeout,
        ),
    ]


async def _table_detail(params: schema_tools.TableNameParams, ctx: ToolContext) -> ToolOutcome:
    """`get_table_detail` —— 结构 + 关联表 + 抽样，一次给全。

    与 `describe_table` 共用同一份快照数据，区别只在"是否把关联表与
    值分布一起端上来"。
    """
    outcome = await schema_tools.describe_table(
        schema_tools.DescribeTableParams(table_name=params.table_name), ctx
    )
    if not outcome.ok:
        return outcome
    related = await schema_tools.find_related_tables(
        schema_tools.FindRelatedParams(table_name=params.table_name), ctx
    )
    if related.ok:
        outcome.content = f"{outcome.content}\n\n{related.content}"
    return outcome


def build_registry(
    settings: Settings | None = None,
    *,
    max_parallel: int = MAX_PARALLEL_TOOL_CALLS,
) -> ToolRegistry:
    """组装全部工具。测试可以传 `max_parallel=1` 关掉并发。"""
    return ToolRegistry(_specs(), max_parallel=max_parallel, settings=settings)


@lru_cache(maxsize=1)
def _cached_registry() -> ToolRegistry:
    """进程内共享的注册表（工具定义与 `Settings` 无关，超时在调用时才取值）。

    每次调用的可变状态都在 `ToolContext` 里，因此共享注册表是安全的 ——
    历史实现的 `_active_table_filter` 那种跨请求串扰在这里没有落脚点。
    """
    return build_registry(get_settings())


def get_registry(settings: Settings | None = None) -> ToolRegistry:
    """取注册表；显式传 `settings`（测试/多配置）时新建一个，不复用缓存。"""
    if settings is not None:
        return build_registry(settings)
    return _cached_registry()
