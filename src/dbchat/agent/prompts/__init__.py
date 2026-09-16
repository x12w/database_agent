"""提示词层 —— 一个 agent 一组提示词，全部由显式参数驱动。

历史上（`app/agent/prompt_builder.py`，762 行）所有 prompt 函数都从全局
连接对象上读 `db.db_type` / `db.current_db_name`，且方言提示是硬编码的
"用反引号"/"不要用 SHOW"。这里：

* 方言相关文案一律来自 `Dialect.prompt_notes()` / `sql_dialect_hint()`；
* 死代码 `build_system_prompt`（被 import 但从未调用）与
  `_build_join_reference`（从未被调用）不再移植。
"""

from dbchat.agent.prompts.agents import (
    build_analysis_history_block,
    build_analysis_prompt,
    build_analysis_user_message,
    build_answer_prompt,
    build_compute_prompt,
    build_explore_prompt,
    build_nl_history,
    build_sql_prompt,
    build_visualize_prompt,
    format_computed_results,
    format_query_results,
)
from dbchat.agent.prompts.schema import build_schema_context, extract_table_refs

__all__ = [
    "build_analysis_history_block",
    "build_analysis_prompt",
    "build_analysis_user_message",
    "build_answer_prompt",
    "build_compute_prompt",
    "build_explore_prompt",
    "build_nl_history",
    "build_schema_context",
    "build_sql_prompt",
    "build_visualize_prompt",
    "extract_table_refs",
    "format_computed_results",
    "format_query_results",
]
