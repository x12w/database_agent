"""工具注册表与 agent 配置的一致性。

三件事必须永远成立，否则线上会出现"静默降级"：

1. 每个 `AgentSpec` 引用的工具都在注册表里 —— 历史实现用
   `get_tools(name).get(classification, mapping["AMBIGUOUS"])`，工具名写错
   只会悄悄退回另一套工具；
2. 每个注册的工具名都在 `events.project.TOOL_VIEW` 里 —— 缺一条映射，
   线上事件就会落到 `_FALLBACK_VIEW`（"正在计算"），用户看到错误的动作类型；
3. `query_database` 的描述必须按方言渲染 —— 历史实现把
   "标准 MySQL 语法，表名和列名用反引号"写死在两个 provider 的工具定义里，
   连到 PostgreSQL/达梦时模型收到的仍是 MySQL 提示。
"""

from __future__ import annotations

import pytest

from dbchat.agent import specs as spec_module
from dbchat.config import Settings
from dbchat.db.dialects import registered_kinds
from dbchat.domain.profile import DialectKind
from dbchat.domain.schema import CatalogScope
from dbchat.events.project import TOOL_VIEW
from dbchat.tools import AGENT_NAMES, build_registry

DIALECTS = (DialectKind.MYSQL, DialectKind.POSTGRESQL, DialectKind.DAMENG)


def _dialect(kind: DialectKind):
    from dbchat.db.dialects import get_dialect

    return get_dialect(kind)


def _snapshot():
    """一份最小可用的结构快照（含外键与抽样行）。"""
    from dbchat.domain.schema import ColumnMeta, ForeignKeyMeta, SchemaSnapshot, TableMeta

    employee = TableMeta(
        name="employee",
        comment="员工",
        row_count=12,
        columns=[
            ColumnMeta(name="id", data_type="int", column_type="int", is_primary_key=True),
            ColumnMeta(name="f_status", data_type="varchar", column_type="varchar(4)"),
            ColumnMeta(name="dept_id", data_type="int", column_type="int"),
        ],
        sample_rows=[{"id": 1, "f_status": "01", "dept_id": 3}],
    )
    department = TableMeta(
        name="department",
        comment="部门",
        row_count=4,
        columns=[
            ColumnMeta(name="id", data_type="int", column_type="int", is_primary_key=True),
            ColumnMeta(name="name", data_type="varchar", column_type="varchar(32)"),
        ],
        sample_rows=[{"id": 3, "name": "技术部"}],
    )
    return SchemaSnapshot(
        db_name="demo",
        tables={"employee": employee, "department": department},
        foreign_keys=[
            ForeignKeyMeta(
                table="employee",
                column="dept_id",
                ref_table="department",
                ref_column="id",
            )
        ],
    )


@pytest.mark.asyncio
async def test_every_spec_tool_resolves_in_registry() -> None:
    """每个 spec 的工具名都必须在注册表里，且确实对该 agent 可见。"""
    registry = build_registry(Settings())
    settings = Settings()
    scope = CatalogScope()

    built = {
        "analysis": spec_module.analysis_spec(settings, _dialect(DialectKind.MYSQL), registry),
        "sql": await spec_module.sql_spec(
            settings,
            registry=registry,
            dialect=_dialect(DialectKind.MYSQL),
            snapshot=_snapshot(),
            scope=scope,
        ),
        "compute": spec_module.compute_spec(
            settings, registry=registry, question="q", results=()
        ),
        "visualize": spec_module.visualize_spec(
            settings, registry=registry, question="q", results=()
        ),
        "answer": spec_module.answer_spec(settings, question="q", results=()),
        "explore": await spec_module.explore_spec(
            settings, registry=registry, dialect=_dialect(DialectKind.MYSQL), db_name="demo"
        ),
    }

    assert set(built) == set(AGENT_NAMES)
    for name, spec in built.items():
        assert spec.name == name
        assert spec.max_iterations >= 1
        assert spec.system_prompt.strip(), f"{name} 的系统提示词为空"
        for tool in spec.tools:
            registry.require(tool)  # 未注册会抛 ToolError
        assert set(spec.tools) <= set(registry.for_agent(name))
    # 回答型 agent 不再是特例：它就是"一次调用、没有工具"
    assert built["answer"].tools == ()
    assert built["answer"].max_iterations == 1
    assert built["answer"].emit_text is True


@pytest.mark.asyncio
async def test_spec_tools_helper_matches_registry() -> None:
    registry = build_registry(Settings())
    spec = spec_module.analysis_spec(Settings(), _dialect(DialectKind.MYSQL), registry)
    assert spec_module.spec_tools(spec, registry) == spec.tools


def test_every_registry_tool_is_in_tool_view() -> None:
    """工具名与出站映射必须一一对应（缺映射会静默显示成"正在计算"）。"""
    registry = build_registry(Settings())
    missing = [name for name in registry.names if name not in TOOL_VIEW]
    assert missing == []
    # 反向：TOOL_VIEW 里的名字也该有实现（防止映射表留下已删除工具）
    stale = [name for name in TOOL_VIEW if registry.get(name) is None]
    assert stale == []


def test_every_agent_has_at_least_one_tool_or_is_the_answerer() -> None:
    registry = build_registry(Settings())
    for agent in AGENT_NAMES:
        tools = registry.for_agent(agent)
        if agent == "answer":
            continue
        assert tools, f"agent {agent} 没有任何可见工具"


def test_registered_kinds_are_covered_by_the_description_rendering() -> None:
    """方言注册表里的每个实现都能渲染出工具描述（新增国产库不会漏）。"""
    registry = build_registry(Settings())
    spec = registry.require("query_database")
    for kind in registered_kinds():
        description = spec.definition(_dialect(kind))["description"]
        assert description.strip()
        assert spec.definition(_dialect(kind))["input_schema"]["required"] == ["sql"]


def test_query_database_description_differs_per_dialect() -> None:
    """`query_database` 的描述必须按方言渲染，且不能把 MySQL 的反引号带给别的库。"""
    registry = build_registry(Settings())
    spec = registry.require("query_database")

    rendered = {kind: spec.definition(_dialect(kind))["description"] for kind in DIALECTS}

    # 三个方言之下的描述互不相同
    assert len(set(rendered.values())) == len(DIALECTS)

    mysql = rendered[DialectKind.MYSQL]
    pg = rendered[DialectKind.POSTGRESQL]
    dameng = rendered[DialectKind.DAMENG]

    assert "反引号" in mysql
    assert "`employee`" in mysql

    for text in (pg, dameng):
        assert "反引号" not in text
        assert "MySQL" not in text
        assert "双引号" in text

    assert "PostgreSQL" in pg
    assert "达梦" in dameng
    # 行数限制的写法也得跟着方言走（达梦不认 LIMIT）
    assert "LIMIT 100" in mysql
    assert "FETCH FIRST 100 ROWS ONLY" in dameng
    assert "LIMIT 100" in pg


def test_tool_definitions_are_provider_neutral_canonical_shape() -> None:
    """注册表默认渲染 Anthropic 形状；OpenAI 形状由 `llm.messages` 转换。"""
    registry = build_registry(Settings())
    dialect = _dialect(DialectKind.MYSQL)
    names = ("query_database", "run_python")
    anthropic = registry.definitions(names, dialect)
    openai = registry.definitions(names, dialect, provider="openai")

    assert [item["name"] for item in anthropic] == list(names)
    assert all("input_schema" in item for item in anthropic)
    assert [item["function"]["name"] for item in openai] == list(names)
    assert all("parameters" in item["function"] for item in openai)


def test_pydantic_schema_has_no_pydantic_noise() -> None:
    """JSON Schema 由 pydantic 推导，但 `title` 这类噪声要剔掉。"""
    registry = build_registry(Settings())
    schema = registry.require("sample_data").definition(_dialect(DialectKind.MYSQL))["input_schema"]
    assert "title" not in schema
    assert set(schema["properties"]) == {"table_name", "limit"}
    assert schema["required"] == ["table_name"]


def test_all_tool_names_referenced_by_specs_exist() -> None:
    """`build_specs` 里出现的工具名同样要能解析（防止漏改一处）。"""
    registry = build_registry(Settings())
    for spec in spec_module.build_specs(Settings(), registry=registry).values():
        for tool in spec.tools:
            registry.require(tool)


@pytest.mark.asyncio
async def test_sql_spec_prompt_carries_schema_context() -> None:
    """SQL agent 的提示词里必须真的带上结构上下文（大库压缩后仍在）。"""
    registry = build_registry(Settings())
    spec = await spec_module.sql_spec(
        Settings(),
        registry=registry,
        dialect=_dialect(DialectKind.POSTGRESQL),
        snapshot=_snapshot(),
        scope=CatalogScope(),
        analysis_plan="## 查询计划\n- 查询 `employee`",
    )
    assert "employee" in spec.system_prompt
    assert "department" in spec.system_prompt
    # 方言提示跟着方言走，不带反引号
    assert "反引号" not in spec.system_prompt
    assert "查询计划" in spec.system_prompt
