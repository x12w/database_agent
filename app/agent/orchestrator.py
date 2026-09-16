"""Main agent orchestrator — three-agent pipeline for data questions.

Pipeline for DATA_QUESTION / AMBIGUOUS:
    1. SQL Agent (query_database + explore tools) → raw query results
    2. Compute Agent (run_python tool, conditional) → computed results
    3. NL Response Agent (no tools) → natural language answer

DOC_QUESTION uses a single agent with search_documentation.
New databases are auto-explored before first query.
"""

import json
import os
import re
import time
from typing import AsyncGenerator, Optional

from app.config import DatabaseProfile, config
from app.agent.router import classify
from app.agent.prompt_builder import (
    build_system_prompt,
    build_sql_agent_prompt,
    build_compute_agent_prompt,
    build_visualization_agent_prompt,
    build_nl_agent_prompt,
    build_explore_agent_prompt,
)
from app.agent.tools import (
    execute_tool,
    get_tools,
)
from app.database.connection import db
from app.database.schema_cache import schema_manager, SchemaSnapshot
from app.database.query_executor import execute_query, format_query_result, QueryExecutionError
from app.llm.claude_client import llm_client
from app.rag.retriever import retrieve_context, retrieve_chunks


MAX_TOOL_ITERATIONS = 3
MAX_ANALYSIS_ITERATIONS = 6
MAX_COMPUTE_ITERATIONS = 1
MAX_VISUALIZATION_ITERATIONS = 2
MAX_QUERY_ROUNDS = 5  # Max iterative query rounds (try→fail→retry with alternatives)
EXPLORATION_DIR = "data/explorations"
COMPACT_SCHEMA_THRESHOLD = 20  # Tables threshold for large DB mode

# Time-relative keywords that trigger automatic time resolution
_TEMPORAL_KEYWORDS = [
    "今年", "本年", "去年", "前年", "明年",
    "本月", "这个月", "上个月", "下个月",
    "本周", "这周", "上周", "下周",
    "今天", "昨天", "明天", "当前", "现在",
    "上半年", "下半年", "第一季度", "第二季度", "第三季度", "第四季度",
    "最近", "近期", "近三年", "近五年",
]


def _resolve_temporal_context(question: str) -> str:
    """Pre-process: resolve time-relative expressions using system clock.

    This runs BEFORE any agent, so the LLM cannot ignore it.
    Returns a context string to prepend to the user question, or empty string.
    """
    import datetime
    has_temporal = any(kw in question for kw in _TEMPORAL_KEYWORDS)
    if not has_temporal:
        return ""

    now = datetime.datetime.now()
    lines = [
        f"[系统时间参考] 当前时间是 {now.year}年{now.month}月{now.day}日，"
        f"年份={now.year}，月份={now.month}，季度=Q{(now.month-1)//3+1}。"
        f"以下时间表述均基于此计算，不需要再搜索确认时间。",
    ]

    # Compute common relative periods
    if "去年" in question:
        lines.append(f"「去年」= {now.year - 1}年")
    if "今年" in question or "本年" in question:
        lines.append(f"「今年」= {now.year}年")
    if "上半年" in question:
        lines.append(f"「上半年」= {now.year}年1月至6月")
    if "下半年" in question:
        lines.append(f"「下半年」= {now.year}年7月至12月")
    if "本月" in question or "这个月" in question:
        lines.append(f"「本月」= {now.year}年{now.month}月")
    if "上个月" in question:
        prev = now.replace(day=1) - datetime.timedelta(days=1)
        lines.append(f"「上个月」= {prev.year}年{prev.month}月")
    if "第一季度" in question:
        lines.append(f"「第一季度」= {now.year}年1月至3月")
    if "第二季度" in question:
        lines.append(f"「第二季度」= {now.year}年4月至6月")
    if "第三季度" in question:
        lines.append(f"「第三季度」= {now.year}年7月至9月")
    if "第四季度" in question:
        lines.append(f"「第四季度」= {now.year}年10月至12月")

    return "\n".join(lines)

# Simple query patterns that can skip the Analysis Agent
SIMPLE_QUERY_PATTERNS = [
    # Direct count queries
    r'有多少\S*(人|个|条|张|名|位|次)',
    r'\S+的数量',
    r'有几个\S+',
    r'一共\S*多少',
    # Simple list queries
    r'列出所有\S+',
    r'所有\S+的列表',
    r'查看\S+表',
    r'显示\S+表',
    # Direct entity queries (well-known patterns)
    r'^(员工|部门|课程|考试|培训).{0,5}(列表|有哪些|多少)',
    r'^(查询|查)\s*\S+(表|\s*$)',
]

# Structure-related keywords — these bypass the LLM entirely
STRUCTURE_KEYWORDS = [
    "有哪些表", "什么表", "表结构", "数据库架构", "有哪些字段",
    "有哪些列", "什么字段", "什么列", "包含哪些表", "数据库结构",
    "介绍一下数据库", "数据库介绍", "schema", "有哪些数据表",
    "数据库里有", "数据库包含", "表关系", "外键关系", "数据库设计",
]

# Keywords that indicate the user wants computation/statistics


async def process_message(
    message: str,
    conversation_history: list = None,
    db_profile: DatabaseProfile = None,
    table_filter: list = None,
) -> AsyncGenerator[dict, None]:
    """
    Process a user message and stream the response.

    Args:
        message: User's natural language question.
        conversation_history: Previous messages for this session.
        db_profile: Database profile to connect to. If None, uses default.
        table_filter: Optional list of table names to restrict operations to.

    Yields SSE events:
        - status: phase transitions
        - thinking: agent reasoning
        - tool_call: SQL or Python about to execute
        - tool_result: query or compute results
        - text: response text deltas
        - done: completion with metadata
        - error: error information
    """
    if conversation_history is None:
        conversation_history = []

    # Set global table filter BEFORE any schema operations
    # Only update if a filter is explicitly provided to avoid clearing
    # a filter that was set by rebuild-cache or previous operations
    if table_filter is not None:
        from app.database.schema_cache import set_active_table_filter, get_active_table_filter
        set_active_table_filter(table_filter)
    else:
        from app.database.schema_cache import get_active_table_filter
        existing = get_active_table_filter()
        if existing:
            table_filter = existing
            print(f"[Orchestrator] Using existing global filter: {len(existing)} tables")

    start_time = time.time()
    db_queries = 0

    # Step 0: Connect to the right database
    if db_profile:
        db.connect_to(db_profile)
    elif not db.current_profile:
        from app.config import config
        db.connect_to(config.get_default_profile())

    # Step 0.5: Auto-explore if needed
    if db_profile and not _is_explored(db_profile.id):
        yield {"type": "status", "phase": "explore", "message": f"首次连接数据库 {db_profile.name}，开始探索..."}
        async for event in explore_database(db_profile, table_filter):
            yield event
        yield {"type": "status", "phase": "explore", "message": "探索完成"}

    # Step 1: Schema freshness check (builds cache if needed)
    yield {"type": "status", "phase": "schema_check", "message": "加载数据库结构..."}
    snapshot = None
    if db.current_profile:
        cache = schema_manager.get_cache(db.current_profile.id)
        if cache.snapshot is None:
            # First load — inform user it may take time for large databases
            yield {"type": "status", "phase": "schema_check", "message": "首次加载数据库结构，大数据库可能需要一些时间..."}
        # Pass table_filter — if set, only filtered tables are cached and returned
        snapshot = schema_manager.ensure_fresh(db.current_profile.id, table_filter)
        n_tables = len(snapshot.tables) if snapshot else 0
        if table_filter and snapshot:
            yield {"type": "status", "phase": "schema_check",
                   "message": f"表筛选模式 — 仅 {n_tables}/{len(table_filter)} 张选定表"}
        yield {"type": "status", "phase": "schema_check", "message": f"数据库结构加载完成（{n_tables} 张表）"}

    # Step 1.5: Resolve time-relative expressions BEFORE agents run
    # This is deterministic pre-processing — cannot be ignored by the LLM
    resolved_context = _resolve_temporal_context(message)

    # Step 2: Classify the question
    yield {"type": "status", "phase": "classify", "message": "分析问题类型..."}
    classification = classify(message)
    # Inject resolved context into classification result for display
    if resolved_context:
        classification = f"{classification}（已解析时间参考）"
    yield {
        "type": "status",
        "phase": "classify",
        "message": f"分类结果: {classification}",
    }

    # Build messages for the LLM — inject resolved time context into user message
    if resolved_context:
        messages = list(conversation_history)
        messages.append({"role": "user", "content": f"[系统时间参考]\n{resolved_context}\n\n{message}"})
    else:
        messages = list(conversation_history)
        messages.append({"role": "user", "content": message})

    # Step 3: Unified three-agent pipeline for all question types
    # The SQL Agent has both query_database and exploration tools,
    # so it handles DATA_QUESTION, DOC_QUESTION, and AMBIGUOUS alike.

    # Step 4: Limited mode fallback (no API key)
    if not llm_client.available:
        limited_text = []
        async for event in _limited_mode_response(message, classification):
            if event["type"] == "query_result":
                db_queries += 1
            elif event["type"] == "text":
                limited_text.append(event["text"])
            yield event
        _save_conversation(conversation_history, messages, "".join(limited_text))
        elapsed_ms = round((time.time() - start_time) * 1000)
        yield {
            "type": "done",
            "classification": classification,
            "db_queries": db_queries,
            "response_time_ms": elapsed_ms,
            "limited_mode": True,
        }
        return

    # ============================================================
    # Step 5: Fast path for structure questions — no LLM needed
    # ============================================================
    if _is_structure_question(message) and snapshot and len(snapshot.tables) > 0:
        yield {"type": "status", "phase": "fast_path", "message": "结构类问题，直接返回缓存数据..."}
        response_text = _build_structure_response(message, snapshot, db_profile)
        if response_text:
            async for chunk in _stream_text(response_text):
                yield chunk
            _save_conversation(conversation_history, messages, response_text)
            elapsed_ms = round((time.time() - start_time) * 1000)
            yield {
                "type": "done",
                "classification": classification,
                "db_queries": 0,
                "response_time_ms": elapsed_ms,
                "message": response_text,
            }
            return

    # ============================================================
    # Step 6: Iterative multi-round pipeline
    # ============================================================
    # The system loops up to MAX_QUERY_ROUNDS times: explore → query → evaluate.
    # If results are empty or key columns are all NULL, it refines and retries.
    # ============================================================

    # Orchestrator overview card — shows the plan to the user
    yield {"type": "progress", "phase": "🧠", "title": "Orchestrator 分析中",
           "status": "running", "detail": f"问题分类: {classification} | 数据库: {snapshot.db_name if snapshot else '?'} | {n_tables} 张表"}

    # Load exploration report for context
    exploration_context = None
    if db_profile and _is_explored(db_profile.id):
        exp_path = os.path.join(EXPLORATION_DIR, f"{db_profile.id}.json")
        try:
            with open(exp_path, "r", encoding="utf-8") as f:
                exp_data = json.load(f)
            exploration_context = exp_data.get("report", "")
        except Exception:
            pass

    n_tables = len(snapshot.tables) if snapshot else 0
    # Prepend resolved time context for all agents
    effective_message = f"{resolved_context}\n\n用户问题: {message}" if resolved_context else message
    if resolved_context:
        yield {"type": "progress", "phase": "🕐", "title": "时间解析",
               "status": "done", "detail": resolved_context.replace('\n', ' | ')}

    all_query_results = []
    analysis_plan = ""
    refinement_hint = ""
    is_simple = _is_simple_query(message, n_tables)
    chart_images = []
    computed_results = []
    analysis_result = {"plan": "", "events": [], "error": None}

    for query_round in range(1, MAX_QUERY_ROUNDS + 1):
        round_events = []

        # ---- Progress card in chat area ----
        if query_round == 1:
            yield {"type": "progress", "phase": "🔍", "title": "开始查询",
                   "status": "running", "detail": f"最多 {MAX_QUERY_ROUNDS} 轮迭代"}
        else:
            yield {"type": "progress", "phase": "🔄", "title": f"第 {query_round} 轮重试",
                   "status": "running", "detail": refinement_hint}

        # ---- Phase 0: Analysis Agent ----
        if is_simple:
            yield {"type": "progress", "phase": "⚡", "title": "简单查询",
                   "status": "running", "detail": "跳过分析，直接生成 SQL"}
        elif query_round == 1:
            yield {"type": "status", "phase": "analysis", "message": "Analysis Agent 分析问题并规划查询..."}
            analysis_result = await _run_analysis_agent(effective_message, snapshot, exploration_context, conversation_history)
            for event in analysis_result["events"]:
                round_events.append(event); yield event
            analysis_plan = analysis_result.get("plan", "")
        else:
            # Retry round: feed refinement hint
            refined_question = f"{effective_message}\n\n[上轮提示] {refinement_hint}"
            yield {"type": "status", "phase": "analysis", "message": f"第 {query_round} 轮分析（优化方案）..."}
            analysis_result = await _run_analysis_agent(refined_question, snapshot, exploration_context, conversation_history)
            for event in analysis_result["events"]:
                round_events.append(event); yield event
            analysis_plan = analysis_result.get("plan", "")

        if analysis_result.get("error"):
            yield {"type": "progress", "phase": "⚠️", "title": "分析未完成",
                   "status": "fail", "detail": "直接进入查询阶段"}
            analysis_plan = ""
        elif analysis_plan:
            yield {"type": "progress", "phase": "📋", "title": "分析完成",
                   "status": "done", "detail": "查询计划已生成"}
        else:
            yield {"type": "progress", "phase": "📋", "title": "分析完成",
                   "status": "done", "detail": "无特定计划，直接查询"}

        # ---- Phase 1: SQL Agent ----
        yield {"type": "progress", "phase": "🔍", "title": "SQL Agent",
               "status": "running", "detail": "生成并执行查询..."}

        sql_result = await _run_sql_agent(message, conversation_history, snapshot, exploration_context, analysis_plan, table_filter)
        for event in sql_result["events"]:
            if event["type"] == "tool_use" and event.get("name") == "query_database":
                db_queries += 1
            yield event

        if sql_result["error"]:
            yield {"type": "progress", "phase": "❌", "title": f"第 {query_round} 轮失败",
                   "status": "fail", "detail": str(sql_result['error'])[:200]}
            if query_round < MAX_QUERY_ROUNDS:
                refinement_hint = f"上一轮查询出错: {sql_result['error']}。请修正 SQL 后重试。"
                continue
            else:
                break

        round_results = sql_result["query_results"]
        all_query_results.extend(round_results)

        # ---- Evaluate results ----
        evaluation = await _evaluate_results(round_results, message)
        if evaluation["satisfactory"]:
            yield {"type": "progress", "phase": "✅", "title": f"第 {query_round} 轮完成",
                   "status": "done", "detail": evaluation['reason']}
            break
        else:
            yield {"type": "progress", "phase": "⚠️", "title": f"第 {query_round} 轮不充分",
                   "status": "fail", "detail": evaluation['reason']}
            if query_round < MAX_QUERY_ROUNDS:
                refinement_hint = evaluation["hint"]
            else:
                yield {"type": "progress", "phase": "ℹ️", "title": "已尝试所有方案",
                       "status": "done", "detail": "将基于现有结果回答"}

    # ============================================================
    # Post-loop: Planning + Compute/Viz + NL (unchanged)
    # ============================================================
    query_results = all_query_results

    plan = await _plan_computation(message, query_results)
    needs_compute = plan.get("needs_computation", False)
    needs_chart = plan.get("needs_chart", False)
    if needs_compute:
        yield {"type": "progress", "phase": "🧮", "title": "Compute Agent",
               "status": "running", "detail": plan.get('strategy', '计算中...')}
    if needs_chart:
        yield {"type": "progress", "phase": "📊", "title": "Visualization Agent",
               "status": "running", "detail": plan.get('chart_strategy', '生成图表...')}

    if needs_compute:
        yield {"type": "status", "phase": "compute_agent", "message": "Compute Agent 计算中..."}
        compute_result = await _run_compute_agent(message, query_results, plan.get("strategy", ""),
                                                   chart_strategy="", enable_charts=False)
        for event in compute_result["events"]: yield event
        if compute_result["computed_results"]: computed_results = compute_result["computed_results"]

    if needs_chart:
        yield {"type": "status", "phase": "viz_agent", "message": "Visualization Agent 生成图表..."}
        viz_result = await _run_visualization_agent(message, query_results, chart_strategy=plan.get("chart_strategy", ""))
        for event in viz_result["events"]: yield event
        if viz_result.get("chart_images"):
            chart_images = viz_result["chart_images"]
            yield {"type": "chart", "images": chart_images}

    # Orchestrator summary
    total_results = sum(qr.get("result", qr).get("row_count", 0) for qr in query_results
                       if isinstance(qr.get("result", qr), dict))
    yield {"type": "progress", "phase": "✅", "title": "查询完成",
           "status": "done",
           "detail": f"共 {db_queries} 次查询, {total_results} 条结果, {round((time.time()-start_time)*1000)}ms"}

    # Phase 4: NL Response Agent
    yield {"type": "progress", "phase": "💬", "title": "NL Agent",
           "status": "running", "detail": "生成自然语言回答..."}

    nl_chunks = []
    full_text = ""
    try:
        async for event in _run_nl_agent(message, query_results, computed_results, conversation_history, chart_images):
            if event["type"] == "text":
                nl_chunks.append(event["text"]); yield event
            elif event["type"] == "error":
                yield event
            else:
                yield event

        full_text = _strip_sql_from_text("".join(nl_chunks))
    except Exception as e:
        yield {"type": "error", "message": f"生成回答时出错: {e}"}
        full_text = "".join(nl_chunks) if nl_chunks else f"抱歉，处理您的请求时遇到了问题。请重试。"
        yield {"type": "text", "text": full_text}
    finally:
        # ALWAYS save conversation — even if NL agent failed.
        # Without this, next turn sees unanswered history and repeats previous answers.
        _save_conversation(conversation_history, messages, full_text)

    elapsed_ms = round((time.time() - start_time) * 1000)
    yield {
        "type": "done",
        "classification": classification,
        "db_queries": db_queries,
        "response_time_ms": elapsed_ms,
        "message": full_text,
        "images": chart_images if chart_images else [],
    }


async def _evaluate_results(query_results: list, question: str) -> dict:
    """Use LLM to judge whether query results satisfactorily answer the question.

    This is general — the LLM can detect ANY kind of failure (empty, incomplete,
    wrong columns, wrong interpretation, etc.) and suggest specific fixes.
    """
    if not query_results:
        return {
            "satisfactory": False,
            "reason": "查询未返回任何结果",
            "hint": "查询失败。请用 get_schema_summary 确认表结构，修正 SQL 后重试。"
        }
    if not llm_client.available:
        # Fallback: basic heuristics
        total = 0
        for qr in query_results:
            r = qr.get("result", qr)
            if isinstance(r, dict):
                total += r.get("row_count", 0)
        return {"satisfactory": total > 0, "reason": f"共 {total} 条",
                "hint": "" if total > 0 else "结果为空，请检查 WHERE 条件或尝试替代列。"}

    # Summarize results compactly
    summary_parts = []
    for i, qr in enumerate(query_results):
        result = qr.get("result", qr)
        if isinstance(result, dict):
            row_count = result.get("row_count", 0)
            columns = result.get("columns", [])
            rows = result.get("rows", [])
            sample = rows[:3] if rows else []
            summary_parts.append(
                f"查询{i+1}: {row_count}行, 列={columns}, 样例={sample}"
            )

    prompt = (
        "判断以下查询结果是否已经充分回答了用户问题。\n\n"
        "评估标准：\n"
        "1. 结果完整回答了用户问题（数量、统计、列表等都已提供）→ SATISFIED\n"
        "2. 结果为空、不完整、仅采样未聚合、或含糊其辞（如「无法确认」）→ RETRY\n"
        "3. 重试建议必须具体：指出缺少什么信息，建议查询哪些表/列，用什么条件\n\n"
        "核心原则：结果必须能直接回答用户问题才算充分，否则就重试。\n\n"
        f"用户问题: {question}\n\n"
        f"查询结果:\n" + "\n".join(summary_parts) + "\n\n"
        "只回复一行：SATISFIED 或 RETRY: <基于数据库内容的改进建议>"
    )

    try:
        if llm_client.provider == "anthropic":
            import anthropic
            resp = llm_client._client.messages.create(
                model=config.LLM_MODEL, max_tokens=200, temperature=0,
                system="You are a query result evaluator. Respond with one line.",
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
        else:
            resp = llm_client._client.chat.completions.create(
                model=config.LLM_MODEL, max_tokens=200, temperature=0,
                messages=[
                    {"role": "system", "content": "You are a query result evaluator. Respond with one line."},
                    {"role": "user", "content": prompt},
                ],
            )
            text = resp.choices[0].message.content.strip()

        if text.upper().startswith("SATISFIED"):
            total = sum(qr.get("result", qr).get("row_count", 0) for qr in query_results
                       if isinstance(qr.get("result", qr), dict))
            return {"satisfactory": True, "reason": f"共 {total} 条结果", "hint": ""}
        elif text.upper().startswith("RETRY:"):
            hint = text[6:].strip()
            return {"satisfactory": False, "reason": "LLM 判定结果不充分", "hint": hint}
        else:
            # Unexpected response — fallback to heuristics
            total = sum(qr.get("result", qr).get("row_count", 0) for qr in query_results
                       if isinstance(qr.get("result", qr), dict))
            return {"satisfactory": total > 0, "reason": f"共 {total} 条",
                    "hint": "" if total > 0 else "结果为空，请重试。"}
    except Exception as e:
        print(f"[Evaluator] LLM call failed: {e}, using heuristic fallback")
        total = sum(qr.get("result", qr).get("row_count", 0) for qr in query_results
                   if isinstance(qr.get("result", qr), dict))
        return {"satisfactory": total > 0, "reason": f"共 {total} 条",
                "hint": "" if total > 0 else "结果为空，请重试。"}


# ---- Three Agent Implementations ----


async def _run_analysis_agent(user_question: str, snapshot: SchemaSnapshot = None, exploration_report: str = None, conversation_history: list = None) -> dict:
    """Phase 0: Analysis & Planning Agent — explores schema, infers meaning, creates a query plan.

    This agent uses schema exploration tools (get_schema_summary, get_table_detail,
    analyze_column, find_related_tables) to understand the database structure and
    infer column meaning from actual data samples. It does NOT query data.

    The output plan is passed to the SQL Agent to guide query generation.

    Args:
        user_question: The current user question.
        snapshot: Schema snapshot for the current database (may be None).
        exploration_report: Previous exploration report for business context.
        conversation_history: Previous user/assistant messages for multi-turn context.

    Returns:
        {"plan": str, "events": [...], "error": str|None}
    """
    from app.agent.prompt_builder import build_analysis_agent_prompt

    system_prompt = build_analysis_agent_prompt()

    # Build conversation history block (compact numbered format)
    history_block = _format_analysis_history(conversation_history)

    # Retrieve RAG context — docs + admin clarifications
    rag_context = ""
    try:
        rag_context = retrieve_context(user_question, top_k=3)
    except Exception:
        pass

    # Build analysis-specific user message
    analysis_message = (
        f"请分析以下用户问题，探索相关数据库结构，并生成查询计划：\n\n"
        f"**用户问题**: {user_question}\n\n"
    )
    if history_block:
        analysis_message += (
            f"**对话历史**（本轮之前的问题和回答，用于理解上下文和代词引用）:\n"
            f"{history_block}\n\n"
        )
    if rag_context:
        analysis_message += (
            f"## 相关知识库内容（文档 + 管理员说明）\n\n"
            f"{rag_context}\n\n"
            f"请优先使用以上知识库内容来理解列含义和编码值映射。\n\n"
        )
    analysis_message += (
        f"请快速完成分析：概览 → 深入 2-3 张相关表 → 立即输出计划。"
        f"最终计划放在 ```plan 代码块中。"
    )

    messages = [{"role": "user", "content": analysis_message}]
    tools = get_tools("analysis_agent", llm_client.provider)
    events = []
    all_text = []

    for iteration in range(MAX_ANALYSIS_ITERATIONS):
        tool_results_in_iter = []

        async for event in llm_client.stream_chat(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
        ):
            if event["type"] == "error":
                return {"plan": "", "events": events, "error": event["message"]}

            elif event["type"] == "thinking_delta":
                events.append({"type": "thinking", "text": event["text"]})

            elif event["type"] == "tool_use":
                tool_name = event["name"]
                tool_input = event["input"]
                tool_id = event["id"]

                events.append({
                    "type": "tool_call",
                    "tool": tool_name,
                    "input": tool_input,
                    "tool_id": tool_id,
                })

                result = execute_tool(tool_name, tool_input)

                # Emit admin_question SSE event if agent asked the admin
                admin_q = result.get("_admin_question")
                if admin_q:
                    events.append({
                        "type": "admin_question",
                        "question_id": admin_q["question_id"],
                        "table_name": admin_q["table_name"],
                        "column_name": admin_q["column_name"],
                        "question": admin_q["question"],
                    })

                events.append({
                    "type": "tool_result",
                    "tool": tool_name,
                    "output": result.get("content", ""),
                    "success": result.get("success", False),
                    "raw_result": result.get("raw_result"),
                })

                tool_results_in_iter.append({
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "result": result,
                })

            elif event["type"] == "text_delta":
                all_text.append(event["text"])

            elif event["type"] == "done":
                if tool_results_in_iter:
                    _add_tool_messages(messages, tool_results_in_iter)
                    break  # continue loop to process tool results
                else:
                    # Analysis complete — extract plan from response
                    full_text = "".join(all_text)
                    plan = _extract_plan(full_text)
                    return {"plan": plan, "events": events, "error": None}

    # Max iterations reached — try to extract plan from accumulated text
    full_text = "".join(all_text)
    plan = _extract_plan(full_text)
    return {"plan": plan, "events": events, "error": None}


def _extract_plan(text: str) -> str:
    """Extract the plan from the Analysis Agent's response.

    Looks for content inside ```plan ... ``` code blocks, or returns the full text
    if no plan block is found.
    """
    import re
    # Try ```plan ... ``` block
    match = re.search(r'```plan\s*\n(.*?)```', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fallback: try any code block
    match = re.search(r'```\s*\n(.*?)```', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Last resort: return the full text (truncated)
    return text[:3000]


def _format_analysis_history(conversation_history: list) -> str:
    """Format conversation history compactly for the Analysis Agent.

    Uses a numbered Q/A format that's concise and easy for the LLM to parse.
    Only includes string-content user and assistant messages (no tool calls).
    Returns empty string if history is empty or None.

    The history helps the Analysis Agent:
    - Resolve pronoun references ("他", "那个部门")
    - Understand follow-up context
    - Avoid re-exploring already-known tables
    """
    if not conversation_history:
        return ""

    filtered = []
    for msg in conversation_history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            filtered.append((role, content))

    if not filtered:
        return ""

    lines = []
    turn = 1
    # Walk through filtered messages, grouping user+assistant pairs
    for role, content in filtered:
        if role == "user":
            lines.append(f"Q{turn}: {content}")
        elif role == "assistant":
            # Truncate long assistant responses to keep context compact
            truncated = content[:200] + "..." if len(content) > 200 else content
            lines.append(f"A{turn}: {truncated}")
            turn += 1

    return "\n".join(lines)


async def _run_sql_agent(user_question: str, conversation_history: list = None, snapshot: SchemaSnapshot = None, exploration_report: str = None, analysis_plan: str = "", table_filter: list = None) -> dict:
    """Phase 1: SQL Agent — generates and executes SQL queries.

    Args:
        user_question: The current user question.
        conversation_history: Previous user/assistant exchanges for context.
        snapshot: Schema snapshot for the current database.
        exploration_report: Previous exploration report for business context.
        analysis_plan: Plan from the Analysis Agent — shown prominently in the prompt.
        table_filter: Optional list of allowed table names.

    Returns:
        {"query_results": [...], "events": [...], "error": str|None}
    """
    # Set active table filter for query validation
    import app.agent.tools as agent_tools
    agent_tools._active_table_filter = table_filter

    system_prompt = build_sql_agent_prompt(snapshot, exploration_report, analysis_plan, table_filter)

    # Build messages with conversation history for context (pronoun resolution, etc.)
    messages = []
    if conversation_history:
        # Only include user and assistant text messages (not tool calls)
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_question})
    tools = get_tools("sql_agent", llm_client.provider)
    query_results = []
    events = []

    for iteration in range(MAX_TOOL_ITERATIONS):
        tool_results_in_iter = []

        async for event in llm_client.stream_chat(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
        ):
            if event["type"] == "error":
                return {"query_results": query_results, "events": events, "error": event["message"]}

            elif event["type"] == "thinking_delta":
                events.append({"type": "thinking", "text": event["text"]})

            elif event["type"] == "tool_use":
                tool_name = event["name"]
                tool_input = event["input"]
                tool_id = event["id"]

                events.append({
                    "type": "tool_call",
                    "tool": tool_name,
                    "input": tool_input,
                    "tool_id": tool_id,
                })

                # Execute
                result = execute_tool(tool_name, tool_input)
                query_results.append({
                    "sql": tool_input.get("sql", tool_input.get("code", "")),
                    "result": result.get("raw_result", result),
                    "tool_name": tool_name,
                })

                events.append({
                    "type": "tool_result",
                    "tool": tool_name,
                    "output": result.get("content", ""),
                    "success": result.get("success", False),
                    "raw_result": result.get("raw_result"),
                })

                tool_results_in_iter.append({
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "result": result,
                })

            elif event["type"] == "text_delta":
                # SQL agent shouldn't produce much text, but capture it
                pass

            elif event["type"] == "done":
                if tool_results_in_iter:
                    _add_tool_messages(messages, tool_results_in_iter)
                    break  # continue loop to process tool results
                else:
                    # SQL agent done — no more tools
                    return {"query_results": query_results, "events": events, "error": None}

    return {"query_results": query_results, "events": events, "error": None}


async def _run_compute_agent(user_question: str, query_results: list, strategy: str = "", chart_strategy: str = "", enable_charts: bool = False) -> dict:
    """Phase 2: Compute Agent — runs Python for math/statistics and chart generation.

    Args:
        user_question: The original user question.
        query_results: Results from the SQL agent.
        strategy: Optional computation strategy from the planning agent.
        chart_strategy: Optional chart generation strategy.
        enable_charts: Whether to enable chart generation with matplotlib.

    Returns:
        {"computed_results": [...], "events": [...], "chart_images": [...]}
    """
    system_prompt = build_compute_agent_prompt(user_question, query_results, chart_strategy, enable_charts)
    if strategy:
        system_prompt += f"\n\n规划建议: {strategy}"
    user_msg = "请对以上查询结果进行计算" + ("并生成图表。" if enable_charts else "处理。")
    messages = [{"role": "user", "content": user_msg}]
    tools = get_tools("compute_agent", llm_client.provider)
    computed_results = []
    chart_images = []
    events = []

    # Prepare input data for run_python tool
    # Convert rows to dicts (column_name → value) for easy access in Python code
    input_data = {
        "queries": []
    }
    for qr in query_results:
        result = qr.get("result", {})
        if isinstance(result, dict):
            columns = result.get("columns", [])
            rows = result.get("rows", [])
            # Convert each row list to a dict with column names as keys
            dict_rows = []
            for row in rows:
                row_dict = {}
                for i, col_name in enumerate(columns):
                    row_dict[col_name] = row[i] if i < len(row) else None
                dict_rows.append(row_dict)
            input_data["queries"].append({
                "rows": dict_rows,           # dict format: [{"col": val, ...}, ...]
                "raw_rows": rows,             # list format: [[val, ...], ...]
                "row_count": result.get("row_count", 0),
                "columns": columns,
            })
        else:
            input_data["queries"].append({
                "rows": [],
                "raw_rows": [],
                "row_count": 0,
                "columns": [],
            })

    for iteration in range(MAX_COMPUTE_ITERATIONS):
        tool_results_in_iter = []

        async for event in llm_client.stream_chat(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
        ):
            if event["type"] == "error":
                return {"computed_results": computed_results, "events": events, "chart_images": chart_images}

            elif event["type"] == "thinking_delta":
                events.append({"type": "thinking", "text": event["text"]})

            elif event["type"] == "tool_use":
                tool_name = event["name"]
                tool_input = dict(event["input"])
                tool_id = event["id"]

                # Inject input_data for run_python
                if tool_name == "run_python":
                    tool_input["_input_data"] = input_data

                events.append({
                    "type": "tool_call",
                    "tool": tool_name,
                    "input": {k: v for k, v in tool_input.items() if k != "_input_data"},
                    "tool_id": tool_id,
                })

                result = execute_tool(tool_name, tool_input)
                computed_results.append({
                    "content": result.get("content", ""),
                    "success": result.get("success", False),
                    "tool_name": tool_name,
                    "chart_files": result.get("chart_files", []),
                })

                # Collect chart files
                for cf in result.get("chart_files", []):
                    chart_images.append(f"/static/charts/{cf}")

                events.append({
                    "type": "tool_result",
                    "tool": tool_name,
                    "output": result.get("content", ""),
                    "success": result.get("success", False),
                })

                # Strip _input_data before storing in messages (it's large and not needed for context)
                clean_input = {k: v for k, v in tool_input.items() if k != "_input_data"}
                tool_results_in_iter.append({
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "tool_input": clean_input,
                    "result": result,
                })

            elif event["type"] == "text_delta":
                pass

            elif event["type"] == "done":
                if tool_results_in_iter:
                    _add_tool_messages(messages, tool_results_in_iter)
                    break
                else:
                    return {"computed_results": computed_results, "events": events, "chart_images": chart_images}

    return {"computed_results": computed_results, "events": events, "chart_images": chart_images}


async def _run_visualization_agent(
    user_question: str,
    query_results: list,
    chart_strategy: str = "",
) -> dict:
    """Dedicated Visualization Agent — generates charts only, no computation.

    Triggered by _plan_computation() returning needs_chart=True.
    Has 2 iterations to allow retry on failure.

    Args:
        user_question: The original user question.
        query_results: Results from the SQL agent.
        chart_strategy: Optional chart strategy from the planning agent.

    Returns:
        {"computed_results": [...], "events": [...], "chart_images": [...]}
    """
    system_prompt = build_visualization_agent_prompt(
        user_question, query_results, chart_strategy
    )
    messages = [{"role": "user", "content": "请根据以上查询结果生成图表。只生成图表，不进行计算或分析。"}]
    tools = get_tools("visualization_agent", llm_client.provider)
    computed_results = []
    chart_images = []
    events = []

    # Prepare input_data from query_results (same structure as compute agent)
    input_data = {"queries": []}
    for qr in query_results:
        result = qr.get("result", {})
        if isinstance(result, dict):
            columns = result.get("columns", [])
            rows = result.get("rows", [])
            dict_rows = []
            for row in rows:
                row_dict = {}
                for i, col_name in enumerate(columns):
                    row_dict[col_name] = row[i] if i < len(row) else None
                dict_rows.append(row_dict)
            input_data["queries"].append({
                "rows": dict_rows,
                "raw_rows": rows,
                "row_count": result.get("row_count", 0),
                "columns": columns,
            })
        else:
            input_data["queries"].append({
                "rows": [],
                "raw_rows": [],
                "row_count": 0,
                "columns": [],
            })

    for iteration in range(MAX_VISUALIZATION_ITERATIONS):
        tool_results_in_iter = []

        async for event in llm_client.stream_chat(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
        ):
            if event["type"] == "error":
                return {
                    "computed_results": computed_results,
                    "events": events,
                    "chart_images": chart_images,
                }

            elif event["type"] == "thinking_delta":
                events.append({"type": "thinking", "text": event["text"]})

            elif event["type"] == "tool_use":
                tool_name = event["name"]
                tool_input = dict(event["input"])
                tool_id = event["id"]

                if tool_name == "run_python":
                    tool_input["_input_data"] = input_data

                events.append({
                    "type": "tool_call",
                    "tool": tool_name,
                    "input": {k: v for k, v in tool_input.items() if k != "_input_data"},
                    "tool_id": tool_id,
                })

                result = execute_tool(tool_name, tool_input)
                computed_results.append({
                    "content": result.get("content", ""),
                    "success": result.get("success", False),
                    "tool_name": tool_name,
                    "chart_files": result.get("chart_files", []),
                })

                # Collect chart file URLs
                for cf in result.get("chart_files", []):
                    chart_images.append(f"/static/charts/{cf}")

                events.append({
                    "type": "tool_result",
                    "tool": tool_name,
                    "output": result.get("content", ""),
                    "success": result.get("success", False),
                })

                clean_input = {k: v for k, v in tool_input.items() if k != "_input_data"}
                tool_results_in_iter.append({
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "tool_input": clean_input,
                    "result": result,
                })

            elif event["type"] == "text_delta":
                pass

            elif event["type"] == "done":
                if tool_results_in_iter:
                    _add_tool_messages(messages, tool_results_in_iter)
                    break  # Continue loop to retry if needed
                else:
                    return {
                        "computed_results": computed_results,
                        "events": events,
                        "chart_images": chart_images,
                    }

    return {
        "computed_results": computed_results,
        "events": events,
        "chart_images": chart_images,
    }


async def _run_nl_agent(
    user_question: str,
    query_results: list,
    computed_results: list = None,
    conversation_history: list = None,
    chart_images: list = None,
) -> AsyncGenerator[dict, None]:
    """Phase 4: NL Response Agent — pure natural language, NO tools.

    Yields: thinking, text events
    """
    system_prompt = build_nl_agent_prompt(user_question, query_results, computed_results or [], chart_images)

    # Build messages with conversation history for context
    messages = []
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_question})

    # No tools — NL agent cannot generate SQL or Python
    tools = get_tools("nl_agent", llm_client.provider)

    async for event in llm_client.stream_chat(
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
    ):
        if event["type"] == "error":
            yield {"type": "error", "message": event["message"]}
            return

        elif event["type"] == "thinking_delta":
            yield {"type": "thinking", "text": event["text"]}

        elif event["type"] == "text_delta":
            yield {"type": "text", "text": event["text"]}

        elif event["type"] == "tool_use":
            # Should not happen since NL agent has no tools,
            # but if LLM tries to call a tool anyway, ignore it
            yield {
                "type": "thinking",
                "text": f"[NL Agent 试图调用工具 {event['name']}，已忽略]",
            }

        elif event["type"] == "done":
            return


async def _run_legacy_agent(
    system_prompt: str,
    messages: list,
    tools: list,
) -> AsyncGenerator[dict, None]:
    """Legacy single-agent loop for DOC_QUESTION."""
    all_text = []

    for iteration in range(MAX_TOOL_ITERATIONS):
        tool_results_in_iter = []

        async for event in llm_client.stream_chat(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
        ):
            if event["type"] == "error":
                yield event
                return

            elif event["type"] == "thinking_delta":
                yield {"type": "thinking", "text": event["text"]}

            elif event["type"] == "tool_use":
                tool_name = event["name"]
                tool_input = event["input"]
                tool_id = event["id"]

                yield {
                    "type": "tool_call",
                    "tool": tool_name,
                    "input": tool_input,
                    "tool_id": tool_id,
                }

                result = execute_tool(tool_name, tool_input)

                yield {
                    "type": "tool_result",
                    "tool": tool_name,
                    "output": result.get("content", ""),
                    "success": result.get("success", False),
                    "raw_result": result.get("raw_result"),
                }

                tool_results_in_iter.append({
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "result": result,
                })

            elif event["type"] == "text_delta":
                all_text.append(event["text"])
                yield {"type": "text", "text": event["text"]}

            elif event["type"] == "done":
                if tool_results_in_iter:
                    _add_tool_messages(messages, tool_results_in_iter)
                    break
                else:
                    return

    return


# ---- Helpers ----


async def explore_database(db_profile: DatabaseProfile, table_filter: list = None) -> AsyncGenerator[dict, None]:
    """Adaptive exploration agent — strategy depends on database size.

    Small DB (≤20 tables): full LLM-driven exploration with detailed sampling.
    Large DB (>20 tables): fast schema build + priority sampling + background exploration.
    Results are persisted to data/explorations/{profile_id}.json.

    Args:
        table_filter: Optional list of allowed table names — exploration only covers these.
    """
    from app.database.schema_cache import build_schema_snapshot
    from app.agent.background_explorer import (
        background_explorer, LARGE_DB_THRESHOLD, FAST_EXPLORE_SAMPLE_COUNT,
        _score_table, _analyze_fk_counts, get_exploration_report,
    )

    db.connect_to(db_profile)

    # Step 1: Quick table count to decide strategy
    if table_filter:
        n_tables = len(table_filter)
        yield {"type": "status", "phase": "explore",
               "message": f"筛选模式 — 仅处理 {n_tables} 张选定的表"}
    else:
        try:
            with db.cursor() as cur:
                if db.db_type in ("dameng", "dm", "dm8"):
                    cur.execute("SELECT COUNT(*) AS cnt FROM ALL_TABLES WHERE OWNER = USER AND TABLE_NAME NOT LIKE '##%' AND TABLE_NAME NOT LIKE 'SREF%'")
                elif db.db_type == "postgresql":
                    cur.execute("SELECT COUNT(*) AS cnt FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')")
                else:
                    cur.execute("SELECT COUNT(*) AS cnt FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'", (db_profile.database,))
                row = cur.fetchone()
                n_tables = row["cnt"] if row else 0
        except Exception:
            n_tables = 0
        yield {"type": "status", "phase": "explore", "message": f"检测到 {n_tables} 张表，选择探索策略..."}

    if n_tables <= LARGE_DB_THRESHOLD:
        # ================================================================
        # SMALL DATABASE: full LLM-driven exploration (original behavior)
        # ================================================================
        yield {"type": "status", "phase": "explore", "message": f"小型数据库，进行详细探索..."}
        try:
            system_prompt = build_explore_agent_prompt()
            messages = [{"role": "user", "content": f"请探索数据库 {db_profile.database} 并生成概览报告。"}]
            tools = get_tools("explore_agent", llm_client.provider)
            all_text = []

            for iteration in range(MAX_TOOL_ITERATIONS):
                tool_results_in_iter = []

                async for event in llm_client.stream_chat(
                    system_prompt=system_prompt, messages=messages, tools=tools,
                ):
                    if event["type"] == "error":
                        yield {"type": "error", "message": event["message"]}
                        # Fallback: mark as explored with minimal report
                        _save_exploration(db_profile.id, f"# 数据库 {db_profile.database}\n\n探索过程中出错，已生成基础 schema 索引。")
                        _force_schema_refresh(db_profile.id)
                        return
                    elif event["type"] == "thinking_delta":
                        yield {"type": "thinking", "text": event["text"]}
                    elif event["type"] == "tool_use":
                        tool_name, tool_input, tool_id = event["name"], event["input"], event["id"]
                        if tool_name == "run_python":
                            tool_input = dict(tool_input)
                        yield {"type": "tool_call", "tool": tool_name, "input": tool_input, "tool_id": tool_id}
                        try:
                            result = execute_tool(tool_name, tool_input)
                        except Exception as tool_err:
                            result = {"success": False, "content": f"工具执行错误: {tool_err}"}
                        yield {"type": "tool_result", "tool": tool_name, "output": result.get("content", ""), "success": result.get("success", False), "raw_result": result.get("raw_result")}
                        tool_results_in_iter.append({"tool_id": tool_id, "tool_name": tool_name, "tool_input": tool_input if isinstance(tool_input, dict) else {"input": tool_input}, "result": result})
                    elif event["type"] == "text_delta":
                        all_text.append(event["text"])
                        yield {"type": "text", "text": event["text"]}
                    elif event["type"] == "done":
                        if tool_results_in_iter:
                            _add_tool_messages(messages, tool_results_in_iter)
                            break
                        else:
                            report = "".join(all_text)
                            _save_exploration(db_profile.id, report)
                            _force_schema_refresh(db_profile.id)
                            return

            report = "".join(all_text)
            if report:
                _save_exploration(db_profile.id, report)
            _force_schema_refresh(db_profile.id)
        except Exception as e:
            yield {"type": "error", "message": f"探索过程异常: {str(e)}"}
            # Minimal fallback
            _save_exploration(db_profile.id, f"# 数据库 {db_profile.database}\n\n探索异常: {e}\n基础 schema 索引已生成。")
            _force_schema_refresh(db_profile.id)

    else:
        # ================================================================
        # LARGE DATABASE: fast schema + priority sampling + background
        # ================================================================
        yield {"type": "status", "phase": "explore", "message": f"大数据库（{n_tables} 张表），采用快速探索 + 后台补充模式..."}

        # Step 2: Build fast schema (no samples) — with timeout protection
        yield {"type": "status", "phase": "explore", "message": "正在构建数据库结构索引..."}
        import concurrent.futures
        snapshot = None
        try:
            # Run schema build in a thread with 60s timeout
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(build_schema_snapshot, db_profile.database, True, None, table_filter)
                snapshot = future.result(timeout=60)
        except concurrent.futures.TimeoutError:
            yield {"type": "error", "message": "数据库结构索引构建超时（60 秒），可能是数据库过大或连接缓慢"}
            return
        except Exception as e:
            yield {"type": "error", "message": f"数据库结构索引构建失败: {str(e)}"}
            return

        if not snapshot or len(snapshot.tables) == 0:
            yield {"type": "error", "message": "无法读取数据库表结构，请检查连接和权限"}
            return
        _force_schema_refresh(db_profile.id)

        # Step 3: Score and prioritize tables
        fk_counts = _analyze_fk_counts(snapshot.foreign_keys)
        scored = []
        for tname, meta in snapshot.tables.items():
            score = _score_table(tname, meta, fk_counts)
            scored.append((tname, score))
        scored.sort(key=lambda x: x[1], reverse=True)

        # Step 4: Sample top-N priority tables with per-table timeout
        top_tables = scored[:FAST_EXPLORE_SAMPLE_COUNT]
        yield {"type": "status", "phase": "explore", "message": f"正在采样 {len(top_tables)} 张高优先级表..."}

        from app.database.schema_cache import ensure_table_samples
        sampled_info = []
        failed_tables = []
        for i, (tname, score) in enumerate(top_tables):
            try:
                # Per-table timeout of 10 seconds for sample loading
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(ensure_table_samples, tname)
                    ok = future.result(timeout=10)
                meta = snapshot.tables.get(tname)
                if ok and meta and meta.sample_rows:
                    col_names = [c.name for c in meta.columns[:6]]
                    sampled_info.append(
                        f"- **{tname}** ({meta.row_count} 行, 优先级 {score:.0f}): "
                        f"{', '.join(col_names)}"
                    )
                elif not ok:
                    failed_tables.append(tname)
            except concurrent.futures.TimeoutError:
                failed_tables.append(tname)
                print(f"[Explore] Timeout sampling table {tname}, skipped")
            except Exception as e:
                failed_tables.append(tname)
                print(f"[Explore] Error sampling table {tname}: {e}")
            if (i + 1) % 3 == 0:
                yield {"type": "status", "phase": "explore", "message": f"采样进度: {i+1}/{len(top_tables)} 张表..."}

        if failed_tables:
            yield {"type": "status", "phase": "explore",
                   "message": f"采样完成（{len(top_tables) - len(failed_tables)} 成功, {len(failed_tables)} 跳过: {', '.join(failed_tables[:5])}...）"}
        else:
            yield {"type": "status", "phase": "explore", "message": f"高优先级表采样完成"}

        # Step 5: Generate brief exploration report
        report_lines = [
            f"# 数据库 {db_profile.database} 探索报告（快速模式）",
            f"",
            f"**表总数**: {len(snapshot.tables)} | **外键关系**: {len(snapshot.foreign_keys)}",
            f"**探索模式**: 快速（已采样 {len(top_tables)} 张高优先级表，剩余在后台继续）",
            f"",
            f"## 高优先级表概览",
        ]
        report_lines.extend(sampled_info)

        if snapshot.foreign_keys:
            report_lines.append(f"\n## 主要外键关系")
            for fk in snapshot.foreign_keys[:20]:
                report_lines.append(
                    f"- `{fk.table_name}`.{fk.column_name} → "
                    f"`{fk.referenced_table}`.{fk.referenced_column}"
                )
            if len(snapshot.foreign_keys) > 20:
                report_lines.append(f"- ...及其他 {len(snapshot.foreign_keys) - 20} 个外键关系")

        report = "\n".join(report_lines)
        _save_exploration(db_profile.id, report)

        # Step 6: Start background exploration for remaining tables
        yield {"type": "status", "phase": "explore", "message": f"启动后台探索（剩余 {len(snapshot.tables) - len(top_tables)} 张表将在后台逐步完成）..."}
        background_explorer.start(db_profile.id, snapshot.tables, snapshot.foreign_keys)

        yield {"type": "status", "phase": "explore", "message": f"探索完成 — {len(snapshot.tables)} 张表已索引，{len(top_tables)} 张高优先级表已采样"}


def _strip_sql_from_text(text: str) -> str:
    """Remove SQL statements from NL agent output.

    Handles both line-separated and inline SQL patterns.
    """
    import re

    # Pattern 1: Remove SQL blocks that start with "SQL" prefix
    text = re.sub(
        r'\bSQL\s*(?:SELECT|WITH)\b.*?(?:LIMIT\s+\d+|\);)',
        '', text, flags=re.IGNORECASE | re.DOTALL
    )

    # Pattern 2: Remove standalone SQL statements
    # Matches from SELECT to either LIMIT N, );, or end-of-string
    text = re.sub(
        r'\bSELECT\s+.*?\bFROM\b.*?(?:LIMIT\s+\d+|\);)',
        '', text, flags=re.IGNORECASE | re.DOTALL
    )

    # Pattern 3: Line-level — remove lines that are purely SQL
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^\s*(?:SQL|--)\s', stripped, re.IGNORECASE):
            continue
        if re.match(r'^\s*(?:SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|WITH)\b', stripped, re.IGNORECASE):
            continue
        if re.match(r'^\s*(?:FROM|WHERE|JOIN|GROUP\s+BY|ORDER\s+BY|HAVING|UNION|LIMIT|OFFSET)\b', stripped, re.IGNORECASE):
            continue
        if re.match(r'^\s*"[^"]+"\."[^"]+"', stripped):
            continue
        cleaned.append(line)

    result = '\n'.join(cleaned)
    # Collapse multiple blank lines and spaces
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = re.sub(r' {2,}', ' ', result)
    return result.strip()


def _force_schema_refresh(profile_id: str):
    """Force a schema cache rebuild for a profile. Retries once on failure."""
    schema_manager.invalidate(profile_id)
    for attempt in range(2):
        try:
            snap = schema_manager.ensure_fresh(profile_id)
            if snap and len(snap.tables) > 0:
                return
        except Exception as e:
            print(f"[Explore] Schema refresh attempt {attempt+1} failed: {e}")
    print(f"[Explore] WARNING: Schema cache may not be ready for {profile_id}")


def _is_explored(profile_id: str) -> bool:
    """Check if a database profile has already been explored."""
    path = os.path.join(EXPLORATION_DIR, f"{profile_id}.json")
    return os.path.exists(path)


def _save_exploration(profile_id: str, report: str):
    """Save the exploration report to disk."""
    os.makedirs(EXPLORATION_DIR, exist_ok=True)
    path = os.path.join(EXPLORATION_DIR, f"{profile_id}.json")
    data = {
        "profile_id": profile_id,
        "db_name": db.current_db_name,
        "explored_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report": report,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


async def _plan_computation(question: str, query_results: list) -> dict:
    """Use a lightweight LLM call to decide if computation and/or visualization is needed.

    Returns: {"needs_computation": bool, "strategy": str, "needs_chart": bool, "chart_strategy": str}
    """
    if not query_results or not llm_client.available:
        return {"needs_computation": False, "strategy": "", "needs_chart": False, "chart_strategy": ""}

    # Summarize query results for the planner
    summary_parts = []
    for i, qr in enumerate(query_results):
        result = qr.get("result", qr)
        if isinstance(result, dict):
            row_count = result.get("row_count", 0)
            columns = result.get("columns", [])
            rows = result.get("rows", [])
            sample = rows[:2] if rows else []
            summary_parts.append(
                f"查询{i+1}: {row_count}行, 列={columns}, "
                f"样例={sample}"
            )

    if not summary_parts:
        return {"needs_computation": False, "strategy": "", "needs_chart": False, "chart_strategy": ""}

    prompt = (
        "判断以下查询结果是否需要进行数学计算和/或图表可视化。\n\n"
        f"用户问题: {question}\n\n"
        f"查询结果概要:\n" + "\n".join(summary_parts) + "\n\n"
        "## 计算判断\n"
        "如果需要进行统计、聚合、排序、计算比率/百分比/平均值等，回复: COMPUTE: <简短思路>\n\n"
        "## 可视化判断\n"
        "如果数据满足以下任一条件，建议生成图表：\n"
        "- 有类别+数值的数据对（如各部门人数、各考试通过率）→ 柱状图或饼图\n"
        "- 有时间序列（日期/年份作为X轴，数值作为Y轴）→ 折线图\n"
        "- 有两个数值列 → 散点图\n"
        "- 有多行数值 → 直方图\n"
        "- 用户明确要求图表、趋势分析、可视化\n"
        "如果需要图表，回复: CHART: <图表类型和说明>\n\n"
        "如果既需要计算也需要图表，回复: COMPUTE+CHART: <计算思路> | <图表说明>\n\n"
        "如果都不需要，回复: SKIP\n\n"
        "只回复一行。"
    )

    try:
        if llm_client.provider == "anthropic":
            import anthropic
            resp = llm_client._client.messages.create(
                model=config.LLM_MODEL,
                max_tokens=150, temperature=0,
                system="You are a computation and visualization planner. Respond with exactly one line.",
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
        else:
            resp = llm_client._client.chat.completions.create(
                model=config.LLM_MODEL,
                max_tokens=150, temperature=0,
                messages=[
                    {"role": "system", "content": "You are a computation and visualization planner. Respond with exactly one line."},
                    {"role": "user", "content": prompt},
                ],
            )
            text = resp.choices[0].message.content.strip()

        needs_computation = False
        needs_chart = False
        strategy = ""
        chart_strategy = ""

        text_upper = text.upper()
        if text_upper.startswith("COMPUTE+CHART:"):
            parts = text[15:].strip().split("|", 1)
            strategy = parts[0].strip() if len(parts) > 0 else ""
            chart_strategy = parts[1].strip() if len(parts) > 1 else ""
            needs_computation = True
            needs_chart = True
        elif text_upper.startswith("COMPUTE:"):
            strategy = text[8:].strip()
            needs_computation = True
        elif text_upper.startswith("CHART:"):
            chart_strategy = text[6:].strip()
            needs_chart = True
        elif text_upper.startswith("SKIP"):
            pass
        else:
            # Fallback: check for keywords in the question
            needs_computation = any(kw in question for kw in ["平均", "率", "占比", "统计", "计算", "排名", "对比", "比率"])
            needs_chart = any(kw in question for kw in ["图表", "图", "柱状图", "饼图", "折线图", "可视化", "趋势", "分布"])

        return {
            "needs_computation": needs_computation,
            "strategy": strategy,
            "needs_chart": needs_chart,
            "chart_strategy": chart_strategy,
        }
    except Exception as e:
        print(f"[Planner] LLM call failed: {e}, falling back to keyword check")
        needs_computation = any(kw in question for kw in ["平均", "率", "占比", "统计", "计算", "排名", "对比", "比率"])
        needs_chart = any(kw in question for kw in ["图表", "图", "柱状图", "饼图", "折线图", "可视化", "趋势", "分布"])
        return {
            "needs_computation": needs_computation,
            "strategy": "",
            "needs_chart": needs_chart,
            "chart_strategy": "",
        }


def _is_simple_query(question: str, n_tables: int = 0) -> bool:
    """Check if a query is simple enough to skip the Analysis Agent.

    Simple queries are those where:
    1. The intent is unambiguous (count, list, show)
    2. The target is well-understood (common entities)
    3. Analysis Agent would add latency without much benefit

    On small databases (≤20 tables), we ALWAYS skip for simple patterns
    since the SQL Agent can see all tables at once.
    On large databases, we still skip simple queries — the SQL Agent gets
    full schema context and can figure out the right table.
    """
    import re
    q = question.strip()

    # Must not be a complex query requiring analysis
    complex_indicators = [
        "对比", "比较", "关联", "统计", "分析", "趋势", "变化",
        "排名", "排序", "占比", "比率", "百分比", "平均",
        "同时", "并且", "以及", "还有", "另外",
        "哪些人既", "既.*又", "不.*也不",
        "分别", "各自", "每.*的",
    ]
    for ci in complex_indicators:
        if ci in q:
            return False

    # Check against simple query patterns
    for pattern in SIMPLE_QUERY_PATTERNS:
        if re.search(pattern, q):
            return True

    # Also simple if the question is very short (<8 chars) and not complex
    # But exclude name/entity-specific queries which need analysis to map correctly
    short_entity_indicators = ['成绩', '分数', '培训', '考试', '证书', '缺考', '缺席']
    if len(q) < 8 and not any(ci in q for ci in complex_indicators) and not any(ei in q for ei in short_entity_indicators):
        return True

    return False


def _is_structure_question(question: str) -> bool:
    """Check if this is PURELY a structure question that can be answered from cache.

    Only matches questions that ask ABOUT the database itself, not data queries.
    """
    q = question.strip().lower()
    # Must match a structure keyword
    if not any(kw in q for kw in STRUCTURE_KEYWORDS):
        return False
    # Must NOT be clearly asking for data records
    data_indicators = ["查询", "统计", "多少", "谁", "哪些人", "成绩", "缺考", "参加", "报名", "名单", "分数"]
    for di in data_indicators:
        if di in q:
            return False
    return True


def _build_structure_response(question: str, snapshot: SchemaSnapshot, db_profile=None) -> str:
    """Build a response for structure questions directly from cached data."""
    from app.agent.prompt_builder import _build_schema_context

    if not snapshot:
        return ""

    db_name = snapshot.db_name or db.current_db_name
    lines = [f"## {db_name} 数据库架构\n"]

    # Table overview
    lines.append(f"共 **{len(snapshot.tables)}** 张表，**{len(snapshot.foreign_keys)}** 个外键关系：\n")
    lines.append("| 表名 | 行数 | 列数 | 说明 |")
    lines.append("|------|------|------|------|")
    for tname, meta in snapshot.tables.items():
        comment = meta.comment or "-"
        lines.append(f"| {tname} | {meta.row_count} | {len(meta.columns)} | {comment} |")

    # Foreign keys
    if snapshot.foreign_keys:
        lines.append(f"\n### 表关系\n")
        for fk in snapshot.foreign_keys:
            lines.append(
                f"- `{fk.table_name}`.`{fk.column_name}` → "
                f"`{fk.referenced_table}`.`{fk.referenced_column}`"
            )

    # Value distributions
    if snapshot.summary and snapshot.summary.table_summaries:
        lines.append(f"\n### 数据分布\n")
        for tname, info in snapshot.summary.table_summaries.items():
            sample_vals = info.get("sample_values", {})
            if sample_vals:
                lines.append(f"**{tname}** ({info.get('row_count', '?')} 行):")
                for col, vals in sample_vals.items():
                    if vals:
                        val_str = ", ".join(str(v)[:20] for v in vals[:5])
                        lines.append(f"  - `{col}`: {val_str}")

    # Load exploration report for deeper insight
    if db_profile:
        exp_path = os.path.join(EXPLORATION_DIR, f"{db_profile.id}.json")
        try:
            with open(exp_path, "r", encoding="utf-8") as f:
                exp_data = json.load(f)
            report = exp_data.get("report", "")
            if report:
                lines.append(f"\n### 探索报告\n{report[:2000]}")
        except Exception:
            pass

    lines.append(f"\n*来源: 数据库 schema 缓存*")
    return "\n".join(lines)


async def _stream_text(text: str) -> AsyncGenerator[dict, None]:
    """Stream text in small chunks to simulate typing."""
    chunk_size = 100
    for i in range(0, len(text), chunk_size):
        yield {"type": "text", "text": text[i:i+chunk_size]}


def _save_conversation(conversation_history: list, messages: list, assistant_text: str):
    """Persist the full conversation context back to history.

    Only saves user messages and NL agent responses (no SQL/compute intermediates).

    IMPORTANT: This must be called after every response, even on errors.
    Without it, the LLM sees unanswered history on the next turn and repeats answers.
    """
    conversation_history.clear()
    conversation_history.extend(messages)
    # Always append an assistant response — even if empty (prevents context loss)
    conversation_history.append({
        "role": "assistant",
        "content": assistant_text if assistant_text else "（系统：处理请求时遇到问题）"
    })
    # Keep history manageable (max 20 messages)
    if len(conversation_history) > 20:
        conversation_history[:] = conversation_history[-20:]


MAX_TOOL_RESULT_CHARS = 4000  # Max chars per tool result in conversation history


def _truncate_tool_result(content: str, tool_name: str = "") -> str:
    """Truncate large tool results to avoid context overflow.

    For schema_summary and table_detail tools, the output can be enormous
    on large databases. We keep the beginning (overview) and end (FK refs).
    """
    if len(content) <= MAX_TOOL_RESULT_CHARS:
        return content
    # Keep first 3000 chars (overview + key details) + last 500 chars (FK/relation info)
    keep_head = 3000
    keep_tail = 500
    truncated = (
        content[:keep_head]
        + f"\n\n... [已截断 {len(content) - keep_head - keep_tail} 字符，完整结果可重新调用工具获取] ...\n\n"
        + content[-keep_tail:]
    )
    return truncated


def _add_tool_messages(messages: list, tool_results: list):
    """Add tool use and tool result messages for the next API call."""
    provider = llm_client.provider

    if provider == "openai":
        _add_tool_messages_openai(messages, tool_results)
    else:
        _add_tool_messages_anthropic(messages, tool_results)


def _add_tool_messages_anthropic(messages: list, tool_results: list):
    """Anthropic format: content blocks with tool_use and tool_result types."""
    content_blocks = []
    result_blocks = []

    for tr in tool_results:
        content_blocks.append({
            "type": "tool_use",
            "id": tr["tool_id"],
            "name": tr["tool_name"],
            "input": tr["tool_input"],
        })
        result_content = tr["result"].get("content", "")
        result_content = _truncate_tool_result(result_content, tr["tool_name"])
        result_blocks.append({
            "type": "tool_result",
            "tool_use_id": tr["tool_id"],
            "content": result_content,
        })

    messages.append({"role": "assistant", "content": content_blocks})
    messages.append({"role": "user", "content": result_blocks})


def _add_tool_messages_openai(messages: list, tool_results: list):
    """OpenAI format: assistant with tool_calls, then tool role messages."""
    tool_calls = []
    tool_msgs = []

    for tr in tool_results:
        tool_calls.append({
            "id": tr["tool_id"],
            "type": "function",
            "function": {
                "name": tr["tool_name"],
                "arguments": json.dumps(tr["tool_input"], ensure_ascii=False),
            },
        })
        result_content = tr["result"].get("content", "")
        result_content = _truncate_tool_result(result_content, tr["tool_name"])
        tool_msgs.append({
            "role": "tool",
            "tool_call_id": tr["tool_id"],
            "content": result_content,
        })

    messages.append({"role": "assistant", "tool_calls": tool_calls})
    messages.extend(tool_msgs)


# ---- Limited Mode (No API Key) ----

async def _limited_mode_response(message: str, classification: str) -> AsyncGenerator[dict, None]:
    """
    Handle queries without LLM using template-based SQL generation and RAG.
    """
    yield {"type": "thinking", "text": "运行在关键词匹配模式（未设置 LLM_API_KEY）"}

    if classification == "DOC_QUESTION":
        # Use RAG directly for documentation questions
        yield {"type": "status", "phase": "retrieve", "message": "检索文档..."}
        chunks = retrieve_chunks(message, top_k=3)
        if chunks:
            yield {"type": "thinking", "text": f"从文档中找到 {len(chunks)} 个相关片段"}
            parts = []
            for i, (chunk, score) in enumerate(chunks):
                parts.append(f"### 文档片段 {i+1}（相关度: {score:.2f}）\n\n{chunk}")
            text = "\n\n---\n\n".join(parts)
            yield {"type": "text", "text": text}
            yield {"type": "text", "text": "\n\n---\n*来源: 文档检索*"}
        else:
            yield {"type": "text", "text": "文档中未找到相关信息。请尝试更具体的查询。"}
        return

    # DATA_QUESTION or AMBIGUOUS: Generate SQL from templates
    sql = _generate_sql(message)

    if sql:
        yield {"type": "tool_call", "tool": "query_database", "input": {"sql": sql}}
        try:
            result = execute_query(sql)
            formatted = format_query_result(result)
            yield {
                "type": "tool_result",
                "tool": "query_database",
                "output": formatted,
                "success": True,
                "raw_result": result,
            }
            yield {"type": "query_result", "result": result}
            response = _format_limited_response(message, result, sql)
            yield {"type": "text", "text": response}
        except QueryExecutionError as e:
            yield {"type": "text", "text": f"查询执行失败：{e.message}"}
    else:
        yield {
            "type": "text",
            "text": (
                "抱歉，在关键词模式下我无法理解这个问题。\n\n"
                "请尝试更具体的提问方式，例如：\n"
                "- \"查询所有缺考人员\"\n"
                "- \"2025年有哪些培训\"\n"
                "- \"张三的考试成绩\"\n"
                "- \"技术部有哪些人\"\n"
                "- \"employee 表有哪些字段\"\n\n"
                "或在 .env 文件中设置 LLM_API_KEY 以启用 AI 模式。"
            ),
        }


def _generate_sql(question: str) -> str:
    """Generate SQL from common query patterns using keyword matching."""
    q = question.strip()

    # Pattern: 缺考 / 缺席
    if any(w in q for w in ["缺考", "缺席"]):
        if "原因" in q or "为什么" in q:
            return (
                "SELECT e.employee_no, e.employee_name, "
                "d.employee_status as current_status, "
                "ex.exam_name, er.result_status, er.absent_reason "
                "FROM exam_result er "
                "JOIN employee e ON er.employee_id = e.id "
                "JOIN exam ex ON er.exam_id = ex.id "
                "WHERE er.result_status IN ('缺考', '缺席')"
            )
        return (
            "SELECT e.employee_no, e.employee_name, e.department_name, "
            "ex.exam_name, er.result_status, er.absent_reason "
            "FROM exam_result er "
            "JOIN employee e ON er.employee_id = e.id "
            "JOIN exam ex ON er.exam_id = ex.id "
            "WHERE er.result_status IN ('缺考', '缺席', '作弊')"
        )

    # Pattern: 某年度培训列表
    year_match = re.search(r'(\d{4})\s*年', q)
    if year_match and any(w in q for w in ["培训", "举办了", "有哪些"]):
        year = year_match.group(1)
        return (
            f"SELECT s.session_no, s.session_name, c.course_name, "
            f"s.start_date, s.end_date, s.session_status, s.training_method "
            f"FROM training_session s "
            f"JOIN training_course c ON s.course_id = c.id "
            f"WHERE s.training_year = {year} "
            f"ORDER BY s.start_date"
        )

    # Pattern: 某人成绩/某人考试
    name_match = re.search(r'(EMP\d+|张三|李四|王五|赵六|孙七|周八|吴九|郑十|钱十一|陈十二|刘十三|黄十四|林十五|杨十六|何十七)', q)
    if name_match and any(w in q for w in ["成绩", "考试", "分数", "合格", "参加"]):
        name = name_match.group(1)
        if name.startswith("EMP"):
            where = f"e.employee_no = '{name}'"
        else:
            where = f"e.employee_name = '{name}'"
        return (
            f"SELECT e.employee_no, e.employee_name, ex.exam_name, "
            f"er.attempt_no, er.score, er.result_status, er.is_best_result "
            f"FROM exam_result er "
            f"JOIN employee e ON er.employee_id = e.id "
            f"JOIN exam ex ON er.exam_id = ex.id "
            f"WHERE {where} "
            f"ORDER BY ex.exam_date, er.attempt_no"
        )

    # Pattern: 某人培训记录
    if name_match and any(w in q for w in ["培训", "参加了", "参训", "报名"]):
        name = name_match.group(1)
        if name.startswith("EMP"):
            where = f"e.employee_no = '{name}'"
        else:
            where = f"e.employee_name = '{name}'"
        return (
            f"SELECT e.employee_no, e.employee_name, "
            f"s.training_year, s.session_name, c.course_name, "
            f"en.attendance_hours, en.attendance_rate, en.completion_status "
            f"FROM training_enrollment en "
            f"JOIN employee e ON en.employee_id = e.id "
            f"JOIN training_session s ON en.session_id = s.id "
            f"JOIN training_course c ON s.course_id = c.id "
            f"WHERE {where} "
            f"ORDER BY s.training_year DESC"
        )

    # Pattern: 通过率
    if "通过率" in q or "合格率" in q:
        return (
            "SELECT ex.exam_name, "
            "COUNT(*) AS total_participants, "
            "SUM(CASE WHEN er.result_status = '合格' THEN 1 ELSE 0 END) AS passed, "
            "ROUND(SUM(CASE WHEN er.result_status = '合格' THEN 1 ELSE 0 END) / COUNT(*) * 100, 2) AS pass_rate "
            "FROM exam_result er "
            "JOIN exam ex ON er.exam_id = ex.id "
            "WHERE er.result_status IN ('合格', '不合格') "
            "GROUP BY ex.exam_name "
            "ORDER BY pass_rate DESC"
        )

    # Pattern: 部门人员
    dept_match = re.search(r'(技术部|人事部|财务部|管理部|安全部)', q)
    if dept_match and any(w in q for w in ["有哪些人", "多少人", "人员", "员工", "名单"]):
        dept = dept_match.group(1)
        status_filter = ""
        if "在职" in q:
            status_filter = " AND employee_status = '在职'"
        elif "离职" in q:
            status_filter = " AND employee_status = '离职'"
        return (
            f"SELECT employee_no, employee_name, position_name, "
            f"employee_status, hire_date "
            f"FROM employee "
            f"WHERE department_name = '{dept}'{status_filter} "
            f"ORDER BY employee_no"
        )

    # Pattern: 证书
    if "证书" in q:
        return (
            "SELECT e.employee_no, e.employee_name, c.course_name, "
            "en.certificate_no, en.certificate_date "
            "FROM training_enrollment en "
            "JOIN employee e ON en.employee_id = e.id "
            "JOIN training_session s ON en.session_id = s.id "
            "JOIN training_course c ON s.course_id = c.id "
            "WHERE en.certificate_no IS NOT NULL "
            "ORDER BY en.certificate_date DESC"
        )

    # Pattern: 某个考试的成绩列表
    if "成绩" in q or "分数" in q:
        return (
            "SELECT e.employee_no, e.employee_name, ex.exam_name, "
            "er.attempt_no, er.score, er.result_status, "
            "CASE WHEN er.is_best_result = 1 THEN '★' ELSE '' END AS best "
            "FROM exam_result er "
            "JOIN employee e ON er.employee_id = e.id "
            "JOIN exam ex ON er.exam_id = ex.id "
            "ORDER BY ex.exam_date DESC, er.score DESC "
            "LIMIT 50"
        )

    # Pattern: 人员列表
    if any(w in q for w in ["有哪些人", "人员列表", "所有人员", "全部人员", "员工列表"]):
        return (
            "SELECT employee_no, employee_name, gender, department_name, "
            "position_name, employee_status, hire_date "
            "FROM employee "
            "ORDER BY employee_no"
        )

    # Pattern: 课程列表
    if any(w in q for w in ["有哪些课程", "课程列表", "培训课程"]):
        return (
            "SELECT course_code, course_name, course_category, "
            "standard_hours, course_level, is_active "
            "FROM training_course "
            "ORDER BY course_code"
        )

    # Pattern: 统计
    if "统计" in q and "部门" in q:
        return (
            "SELECT e.department_name, "
            "COUNT(DISTINCT en.employee_id) AS participant_count, "
            "COUNT(*) AS enrollment_count, "
            "SUM(CASE WHEN en.completion_status = '已完成' THEN 1 ELSE 0 END) AS completed "
            "FROM training_enrollment en "
            "JOIN employee e ON en.employee_id = e.id "
            "GROUP BY e.department_name "
            "ORDER BY participant_count DESC"
        )

    # Fallback: try as a general keyword query
    return _fallback_sql(q)


def _fallback_sql(question: str) -> str:
    """Generate a fallback SQL when no pattern matches."""
    # Check if any table or column names are mentioned
    tables_in_q = []
    for tname in ["employee", "training_course", "training_session",
                   "training_enrollment", "exam", "exam_result"]:
        if tname in question.lower():
            tables_in_q.append(tname)

    if tables_in_q:
        table = tables_in_q[0]
        return f"SELECT * FROM {table} LIMIT 20"

    # Last resort: query the most likely table
    if any(w in question for w in ["人", "员工", "人员", "部门", "工号"]):
        return "SELECT employee_no, employee_name, department_name, position_name, employee_status FROM employee ORDER BY employee_no"
    if any(w in question for w in ["考试", "成绩", "分数", "合格"]):
        return "SELECT e.employee_no, e.employee_name, ex.exam_name, er.score, er.result_status FROM exam_result er JOIN employee e ON er.employee_id = e.id JOIN exam ex ON er.exam_id = ex.id ORDER BY ex.exam_date DESC LIMIT 30"
    if any(w in question for w in ["培训", "课程", "场次"]):
        return "SELECT s.session_no, s.session_name, c.course_name, s.start_date, s.end_date, s.session_status FROM training_session s JOIN training_course c ON s.course_id = c.id ORDER BY s.start_date DESC"

    return ""


def _format_limited_response(question: str, result: dict, sql: str) -> str:
    """Format a natural language response for limited mode."""
    parts = []

    # Show the SQL
    parts.append(f"```sql\n{sql}\n```\n")

    if result["row_count"] == 0:
        parts.append("查询结果为空，没有找到匹配的记录。")
        return "\n".join(parts)

    parts.append(f"查询返回 **{result['row_count']}** 条记录（耗时 {result['execution_ms']}ms）：\n")

    # Build a markdown table
    if result["columns"] and result["rows"]:
        cols = result["columns"]
        # Table header
        parts.append("| " + " | ".join(cols) + " |")
        parts.append("|" + "|".join(["---"] * len(cols)) + "|")
        # Table rows
        for row in result["rows"]:
            formatted = [str(v) if v is not None else "NULL" for v in row]
            parts.append("| " + " | ".join(formatted) + " |")

    parts.append(f"\n*来源: 数据库查询（关键词匹配模式）*")
    return "\n".join(parts)


# ---- Proactive Exploration ----

async def run_proactive_exploration(
    db_profile: DatabaseProfile = None,
    table_filter: list = None,
) -> AsyncGenerator[dict, None]:
    """Proactively explore the database and ask admin about all ambiguous columns.

    This is NOT triggered by a user query. It systematically scans every table,
    identifies columns with encoded/abbreviated values that haven't been clarified,
    and asks the admin about them.

    The goal is to achieve complete understanding of the database — every column
    with encoded values should eventually have an admin clarification.

    Yields SSE events:
        - progress: exploration phase and status
        - admin_question: when a new question is submitted to admin
        - status: phase transitions
        - done: completion with summary
    """
    from app.rag.admin_clarifications import clarification_store
    from app.rag.retriever import retrieve_context
    from app.agent.tools import execute_tool
    from app.database.schema_cache import set_active_table_filter

    start_time = time.time()

    try:
        # Connect to the right database
        if db_profile:
            db.connect_to(db_profile)

        # Get profile_id for per-profile clarification scoping
        profile_id = db.current_profile.id if db.current_profile else "default"

        # Ensure fresh schema
        set_active_table_filter(table_filter)
        snapshot = schema_manager.ensure_fresh(db.current_profile.id if db.current_profile else None, table_filter)
        if not snapshot or not snapshot.tables:
            yield {"type": "error", "message": "无法加载数据库 schema — 数据库是否已连接？"}
            return
    except Exception as e:
        yield {"type": "error", "message": f"数据库连接失败: {e}"}
        return

    tables = snapshot.tables
    total_tables = len(tables)
    total_columns = sum(len(meta.columns) for meta in tables.values())

    # Get known columns (already answered or pending)
    known_columns = clarification_store.get_known_columns(profile_id)

    # Collect candidate columns — analyze in real-time, not dependent on pre-built profiles
    candidates = []
    for tname, meta in tables.items():
        # Get sample rows from schema cache if available, otherwise load on demand
        sample_rows = meta.sample_rows if meta.sample_rows else []
        if not sample_rows:
            try:
                from app.database.schema_cache import ensure_table_samples
                ensure_table_samples(tname)
                sample_rows = meta.sample_rows
            except Exception:
                pass

        for col in meta.columns:
            key = (tname.lower(), col.name.lower())
            if key in known_columns:
                continue  # Already asked or answered

            # Get sample values for this column
            cname = col.name
            sample_vals = []
            for row in sample_rows[:5]:
                v = row.get(cname)
                if v is not None:
                    sample_vals.append(str(v)[:50])

            # --- Heuristic: is this column ambiguous? ---

            # Skip columns whose name clearly explains their purpose
            name_lower = cname.lower()
            is_self_explanatory = (
                name_lower in ('id', 'name', 'email', 'phone', 'address', 'comment',
                              'description', 'remark', 'note', 'created_at', 'updated_at',
                              'create_time', 'update_time', 'date', 'time') or
                name_lower.endswith('_id') or  # Foreign key — relationship is clear
                name_lower.endswith('_name') or  # Name column — self-explanatory
                name_lower.endswith('_time') or
                name_lower.endswith('_date') or
                name_lower.endswith('_at') or
                '一' <= cname[0] <= '鿿'  # Chinese column name = self-explanatory
            )
            if is_self_explanatory:
                continue

            dt = col.data_type.lower() if col.data_type else ""

            # Skip purely numeric types that aren't codes (float, double, decimal)
            if dt in ('float', 'double', 'decimal', 'numeric', 'real'):
                continue

            # Determine ambiguity based on data type + samples
            is_ambiguous = False
            reason = ""

            if dt in ('enum', 'set'):
                # Enums always need clarification — what do the values mean?
                is_ambiguous = True
                reason = "枚举类型"

            elif dt in ('tinyint', 'smallint', 'int', 'bigint', 'integer') and sample_vals:
                # Integer columns: check if they look like codes (small range, non-sequential)
                try:
                    nums = [int(v) for v in sample_vals]
                    if len(set(nums)) <= 10 and max(nums) < 100:
                        is_ambiguous = True
                        reason = f"小范围整数编码: {sorted(set(nums))[:8]}"
                except ValueError:
                    pass

            elif dt in ('varchar', 'char', 'varchar2', 'nvarchar2', 'text') and sample_vals:
                # String columns: check if they look like codes
                all_short_ascii = all(
                    len(v) <= 10 and all(c.isascii() for c in v)
                    for v in sample_vals
                )
                all_numeric_str = all(v.isdigit() for v in sample_vals)
                has_chinese = any('一' <= c <= '鿿' for v in sample_vals for c in v)

                if has_chinese:
                    # Chinese values are self-explanatory
                    continue
                elif all_numeric_str and len(sample_vals) > 1:
                    # Pure digit strings — might be codes like '01', '02'
                    if any(len(v) <= 4 for v in sample_vals):
                        is_ambiguous = True
                        reason = f"数字编码: {sample_vals[:5]}"
                elif all_short_ascii:
                    # Short ASCII strings — likely codes
                    is_ambiguous = True
                    reason = f"短编码/缩写: {sample_vals[:5]}"

            elif dt in ('varchar', 'char', 'varchar2', 'nvarchar2', 'text') and not sample_vals:
                # No sample data — check column name for code-like patterns
                if (len(cname) <= 5 and cname.isascii() and cname.isupper()) or \
                   '_' in cname and any(p in name_lower for p in ('status', 'type', 'code', 'level', 'flag', 'cd_')):
                    is_ambiguous = True
                    reason = f"列名疑似编码: {cname}"

            if is_ambiguous:
                candidates.append({
                    "table_name": tname,
                    "column_name": cname,
                    "reason": reason,
                    "sample_vals": sample_vals,
                    "column_type": col.column_type,
                })

    yield {
        "type": "progress",
        "phase": "🔍",
        "title": "主动探索数据库",
        "status": "running",
        "detail": f"共 {total_tables} 张表, {total_columns} 列, 发现 {len(candidates)} 个待确认的编码列",
    }

    if not candidates:
        # Re-emit pending questions so frontend can display them
        pending = clarification_store.get_pending(profile_id)
        pending_count = len(pending)

        if pending_count > 0:
            yield {
                "type": "progress",
                "phase": "⏳",
                "title": "等待管理员回答",
                "status": "running",
                "detail": f"所有编码列已提问，{pending_count} 个问题等待回答中",
            }
            # Re-emit each pending question as admin_question event
            for entry in pending:
                yield {
                    "type": "admin_question",
                    "question_id": entry.question_id,
                    "table_name": entry.table_name,
                    "column_name": entry.column_name,
                    "question": entry.question,
                    "agent_guess": "",
                    "sample_values": entry.sample_values,
                    "context_data": [],
                }
        else:
            yield {
                "type": "progress",
                "phase": "✅",
                "title": "探索完成",
                "status": "done",
                "detail": "数据库已完全理解，所有列均已确认！",
            }

        elapsed_ms = round((time.time() - start_time) * 1000)
        yield {
            "type": "done",
            "candidates_found": 0,
            "questions_asked": 0,
            "pending_count": pending_count,
            "response_time_ms": elapsed_ms,
        }
        return

    # Use LLM to analyze candidates and formulate questions
    # Group candidates by table for efficient exploration
    by_table = {}
    for c in candidates:
        by_table.setdefault(c["table_name"], []).append(c)

    questions_asked = 0
    tables_scanned = 0

    for tname, cols in list(by_table.items())[:20]:  # Cap at 20 tables per run
        tables_scanned += 1
        meta = tables.get(tname)
        if not meta:
            continue

        yield {
            "type": "progress",
            "phase": "🔍",
            "title": f"探索表: {tname}",
            "status": "running",
            "detail": f"({tables_scanned}/{len(by_table)}) {len(cols)} 个编码列待分析",
        }

        # Query sample rows for data context (shows column values in context of other columns)
        sample_rows_for_context = []
        try:
            quote = db.quote
            cols_sql = ", ".join(quote(c["column_name"]) for c in cols[:3])
            # Also include a few key columns for context (name, id, etc.)
            all_col_names = [col.name for col in meta.columns]
            context_cols = []
            for cn in all_col_names:
                if cn.lower().endswith(('name', '_name', '_no')) and cn not in [c["column_name"] for c in cols]:
                    context_cols.append(cn)
            context_cols_sql = ", ".join(quote(cn) for cn in context_cols[:3])
            select_cols = f"{cols_sql}, {context_cols_sql}" if context_cols_sql else cols_sql
            with db.cursor() as cursor:
                cursor.execute(f"SELECT {select_cols} FROM {quote(tname)} LIMIT 3")
                sample_rows_for_context = [dict(row) for row in cursor.fetchall()]
        except Exception:
            pass

        for c in cols:
            # Re-check if already clarified
            if (tname.lower(), c["column_name"].lower()) in clarification_store.get_known_columns(profile_id):
                continue

            sample_str = ", ".join(str(v)[:30] for v in c["sample_vals"][:5])

            # Format sample rows as contextual data display
            context_display = ""
            if sample_rows_for_context:
                context_lines = ["```"]
                headers = list(sample_rows_for_context[0].keys()) if sample_rows_for_context else []
                context_lines.append(" | ".join(headers))
                context_lines.append(" | ".join(["---"] * len(headers)))
                for row in sample_rows_for_context[:3]:
                    vals = [str(row.get(h, ""))[:30] for h in headers]
                    context_lines.append(" | ".join(vals))
                context_lines.append("```")
                context_display = "\n".join(context_lines)

            # Use LLM to formulate the question with guess and options
            question = ""
            agent_guess = ""
            if llm_client.available:
                try:
                    q_prompt = (
                        f"你在探索数据库表 `{tname}`，发现了一个含义不明的列。\n\n"
                        f"**列名**: `{c['column_name']}`\n"
                        f"**类型**: {c['column_type']}\n"
                        f"**样本值**: {sample_str}\n\n"
                    )
                    if context_display:
                        q_prompt += f"**相关数据上下文**（该列与其他列一起展示）:\n{context_display}\n\n"
                    q_prompt += (
                        f"请分析这些数据，尝试**推测**这个列的业务含义。然后输出两部分：\n\n"
                        f"## 推测\n"
                        f"基于上下文给出你的推测：这些编码值可能代表什么？给出具体映射。\n"
                        f"例如：'01=在职, 02=离职' 或 'A=优秀, B=良好, C=及格, D=不及格'\n\n"
                        f"## 问题\n"
                        f"用一句话向管理员确认，包含你的推测。"
                    )
                    q_msgs = [{"role": "user", "content": q_prompt}]
                    q_text = ""
                    async for event in llm_client.stream_chat(
                        system_prompt="你是数据库分析专家。分析数据上下文，推测编码列含义，用中文输出。",
                        messages=q_msgs,
                        tools=[],
                    ):
                        if event["type"] == "text_delta":
                            q_text += event.get("text", "")
                        elif event["type"] == "done":
                            q_text = event.get("content", "") or q_text
                    full_response = q_text.strip()

                    # Parse the response into guess and question
                    if "## 推测" in full_response and "## 问题" in full_response:
                        parts = full_response.split("## 问题")
                        agent_guess = parts[0].replace("## 推测", "").strip()
                        question = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        agent_guess = full_response[:500]
                        question = full_response
                except Exception:
                    pass

            if not question:
                question = f"`{tname}` 表的 `{c['column_name']}` 列包含值 [{sample_str}]，请问这些值分别代表什么含义？"

            # Submit the question with guess
            result = execute_tool("ask_admin", {
                "table_name": tname,
                "column_name": c["column_name"],
                "question": question,
            })

            questions_asked += 1

            admin_q = result.get("_admin_question")
            if admin_q:
                # Include rich context in the SSE event
                yield {
                    "type": "admin_question",
                    "question_id": admin_q["question_id"],
                    "table_name": admin_q["table_name"],
                    "column_name": admin_q["column_name"],
                    "question": admin_q["question"],
                    "agent_guess": agent_guess,
                    "sample_values": c["sample_vals"][:8],
                    "context_data": sample_rows_for_context[:3] if sample_rows_for_context else [],
                }

            yield {
                "type": "progress",
                "phase": "❓",
                "title": f"提问: {tname}.{c['column_name']}",
                "status": "running",
                "detail": f"({questions_asked}/{len(candidates)}) {question[:100]}",
            }

        yield {
            "type": "progress",
            "phase": "✅",
            "title": f"完成表: {tname}",
            "status": "done",
            "detail": f"已分析 {len(cols)} 个编码列",
        }

    pending_count = clarification_store.get_pending_count(profile_id)
    elapsed_ms = round((time.time() - start_time) * 1000)

    yield {
        "type": "progress",
        "phase": "✅",
        "title": "探索完成",
        "status": "done",
        "detail": (
            f"扫描 {tables_scanned} 张表, "
            f"发现 {len(candidates)} 个编码列, "
            f"提问 {questions_asked} 个, "
            f"当前 {pending_count} 个待确认"
        ),
    }

    yield {
        "type": "done",
        "candidates_found": len(candidates),
        "questions_asked": questions_asked,
        "pending_count": pending_count,
        "response_time_ms": elapsed_ms,
    }
