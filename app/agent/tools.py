"""Tool definitions for the agent.

Low-level tools:
- query_database: Execute read-only SQL
- run_python: Execute sandboxed Python
- list_tables / describe_table / sample_data: Raw exploration (for explore agent)

High-level skills (pre-computed, one call replaces many):
- get_schema_summary: Full database overview (tables, columns, relations, samples, distributions)
- get_table_detail: Complete info about one table
- analyze_column: Statistical analysis of a column
- find_related_tables: FK traversal with JOIN examples
- get_suggested_questions: Pre-generated question suggestions
- search_documentation: Search RAG docs
"""

import json
import os
import sys
import io
import traceback
import signal
import builtins as __builtins__
from typing import Any

import httpx

from app.database.connection import db
from app.database.query_executor import execute_query, format_query_result, QueryExecutionError
from app.rag.retriever import retrieve_chunks
from app.agent.skills import (
    skill_schema_summary,
    skill_table_detail,
    skill_analyze_column,
    skill_find_related,
    skill_suggested_questions,
    skill_find_join_path,
)


# ---- Safe Python Execution ----

# Charts output directory
_CHARTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static", "charts"))
os.makedirs(_CHARTS_DIR, exist_ok=True)

# Active table filter (set by orchestrator before SQL agent runs)
_active_table_filter: list = None

# Pre-loaded modules for the compute agent (imported eagerly — must always work)
_PRELOADED_MODULES = {
    "math": __import__("math"),
    "statistics": __import__("statistics"),
    "json": __import__("json"),
    "decimal": __import__("decimal"),
    "fractions": __import__("fractions"),
    "collections": __import__("collections"),
    "itertools": __import__("itertools"),
    "functools": __import__("functools"),
    "random": __import__("random"),
    "datetime": __import__("datetime"),
    "time": __import__("time"),
}

# Lazy-import modules (only loaded when sandbox code actually imports them)
# These may fail in some environments — the error surfaces at sandbox execution time
_LAZY_MODULES = {
    "matplotlib",
    "matplotlib.pyplot",
}

# Combined allowed-module cache: name → module (pre-loaded) or None (lazy, not yet loaded)
_ALLOWED_MODULES = dict(_PRELOADED_MODULES)
_ALLOWED_MODULES.update({name: None for name in _LAZY_MODULES})

# In-memory cache for lazy-loaded modules
_lazy_module_cache = {}

def _restricted_import(name, *args, **kwargs):
    """Only allow imports of pre-approved modules. Lazy-loads matplotlib on demand."""
    top_level = name.split(".")[0]
    if top_level in _ALLOWED_MODULES:
        # Check cache first
        if name in _lazy_module_cache:
            return _lazy_module_cache[name]
        # Already pre-loaded
        if top_level in _PRELOADED_MODULES:
            return __import__(name, *args, **kwargs)
        # Lazy load
        mod = __import__(name, *args, **kwargs)
        _lazy_module_cache[name] = mod
        return mod
    raise ImportError(f"不允许导入模块: {name}（仅允许 math, statistics, json, decimal, fractions, collections, itertools, functools, random, datetime, time, matplotlib）")


def _restricted_open(file, mode="r", *args, **kwargs):
    """Only allow writing files to the charts directory. All reads are denied."""
    write_modes = {"w", "wb", "a", "ab", "x", "xb"}
    if mode not in write_modes:
        raise PermissionError(f"不允许读取文件。仅支持写入图表文件（mode in {write_modes}）")

    # Resolve paths relative to _CHARTS_DIR
    file_path = os.path.abspath(os.path.join(_CHARTS_DIR, file))
    # Prevent path traversal escaping the charts dir
    if not file_path.startswith(_CHARTS_DIR + os.sep) and file_path != _CHARTS_DIR:
        raise PermissionError(f"不允许写入图表目录之外的文件: {file}")

    # Ensure the directory exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    return __builtins__.open(file_path, mode, *args, **kwargs)


# Safe builtins — restricted open, no eval, exec, compile
_SAFE_BUILTINS = {
    "__import__": _restricted_import,
    "open": _restricted_open,
    "True": True, "False": False, "None": None,
    "abs": abs, "all": all, "any": any, "ascii": ascii,
    "bin": bin, "bool": bool, "bytearray": bytearray, "bytes": bytes,
    "chr": chr, "complex": complex, "dict": dict, "divmod": divmod,
    "enumerate": enumerate, "filter": filter, "float": float,
    "format": format, "frozenset": frozenset, "getattr": getattr,
    "hasattr": hasattr, "hash": hash, "hex": hex, "id": id,
    "int": int, "isinstance": isinstance, "issubclass": issubclass,
    "iter": iter, "len": len, "list": list, "map": map, "max": max,
    "min": min, "next": next, "object": object, "oct": oct,
    "ord": ord, "pow": pow, "print": print, "range": range,
    "repr": repr, "reversed": reversed, "round": round, "set": set,
    "slice": slice, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
    "Exception": Exception,
}


class _TimeoutError(Exception):
    """Raised when Python execution exceeds the time limit."""
    pass


def _timeout_handler(signum, frame):
    raise _TimeoutError("Python 代码执行超时（超过 15 秒限制）")


def _cleanup_charts():
    """Remove old chart files, keeping only the 50 most recent."""
    try:
        if not os.path.isdir(_CHARTS_DIR):
            return
        files = [os.path.join(_CHARTS_DIR, f) for f in os.listdir(_CHARTS_DIR)
                 if f.endswith('.png')]
        if len(files) <= 50:
            return
        files.sort(key=lambda f: os.path.getmtime(f))
        for f in files[:-50]:
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception:
        pass  # Cleanup failures are non-critical


def _execute_python(code: str, input_data: dict = None) -> dict:
    """Execute Python code in a sandboxed environment.

    Args:
        code: Python code to execute.
        input_data: Variables to inject into the execution namespace.
                    Typically contains query results as ``input_data``.

    Returns:
        dict with keys: success, content, stdout, stderr
    """
    if input_data is None:
        input_data = {}

    # Build restricted globals
    restricted_globals = {"__builtins__": _SAFE_BUILTINS}
    restricted_globals.update(_PRELOADED_MODULES)
    # matplotlib is injected on-demand by _restricted_import when sandbox code imports it

    # Build local namespace with input data
    local_vars = {"input_data": input_data, "result": None}

    # Capture stdout
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    try:
        # Compile the code first — reject dangerous constructs at AST level
        tree = compile(code, "<python_tool>", "exec")

        # Snapshot charts directory before execution
        charts_before = set(os.listdir(_CHARTS_DIR)) if os.path.isdir(_CHARTS_DIR) else set()

        # Set timeout (15s — matplotlib needs extra time for first import)
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(15)

        try:
            # Change CWD to charts dir so matplotlib savefig writes there
            old_cwd = os.getcwd()
            os.chdir(_CHARTS_DIR)

            # Redirect stdout/stderr
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = stdout_buf
            sys.stderr = stderr_buf

            try:
                exec(tree, restricted_globals, local_vars)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                os.chdir(old_cwd)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        stdout_text = stdout_buf.getvalue()
        stderr_text = stderr_buf.getvalue()

        # Get the result
        result_value = local_vars.get("result")

        # Detect new chart files created during execution
        charts_after = set(os.listdir(_CHARTS_DIR)) if os.path.isdir(_CHARTS_DIR) else set()
        new_chart_files = sorted(charts_after - charts_before)
        # Cleanup: keep only the 50 most recent charts
        _cleanup_charts()

        # Build content
        parts = []
        if result_value is not None:
            if isinstance(result_value, (dict, list, tuple)):
                parts.append(json.dumps(result_value, ensure_ascii=False, indent=2, default=str))
            else:
                parts.append(str(result_value))
        elif stdout_text.strip():
            parts.append(stdout_text.strip())
        else:
            parts.append("代码执行完成，但未设置 result 变量。")

        if new_chart_files:
            parts.append(f"\n[生成的图表: {', '.join(new_chart_files)}]")

        if stderr_text.strip():
            parts.append(f"\n[stderr]:\n{stderr_text.strip()}")

        return {
            "success": True,
            "tool_name": "run_python",
            "content": "\n".join(parts),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "chart_files": new_chart_files,
        }

    except _TimeoutError as e:
        return {
            "success": False,
            "tool_name": "run_python",
            "content": f"执行超时：{str(e)}",
        }
    except SyntaxError as e:
        return {
            "success": False,
            "tool_name": "run_python",
            "content": f"Python 语法错误：{e.msg}（第 {e.lineno} 行）",
        }
    except Exception as e:
        return {
            "success": False,
            "tool_name": "run_python",
            "content": f"执行错误：{type(e).__name__}: {str(e)}",
        }


# ---- Web Search ----

def _search_web(query: str) -> dict:
    """Search the web using Bing (accessible in China, no API key needed).

    Falls back to DuckDuckGo if Bing fails.
    """
    import re
    parts = []

    # ---- Method 1: Bing search (works in China, no key needed) ----
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(
                "https://cn.bing.com/search",
                params={"q": query, "setlang": "zh-Hans", "count": 10},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }
            )
            resp.raise_for_status()
            html = resp.text

            # Extract Bing result snippets
            results = []
            # Bing search result items
            items = re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.DOTALL)
            if not items:
                items = re.findall(r'<ol[^>]*id="b_results"[^>]*>(.*?)</ol>', html, re.DOTALL)
                if items:
                    # Parse individual results from the ol
                    items = re.findall(r'<h2[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.DOTALL)

            for i, item in enumerate(items[:8]):
                if isinstance(item, tuple):
                    url, title = item
                    title = re.sub(r'<[^>]+>', '', title).strip()
                    results.append(f"{i+1}. **{title}**\n   {url}")
                elif isinstance(item, str):
                    snippet = re.findall(r'<p[^>]*>(.*?)</p>', item, re.DOTALL)
                    title_m = re.search(r'<h2[^>]*>.*?<a[^>]*>(.*?)</a>', item, re.DOTALL)
                    title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""
                    desc = re.sub(r'<[^>]+>', '', snippet[0]).strip() if snippet else ""
                    entry = f"{i+1}. **{title}**" if title else f"{i+1}."
                    if desc:
                        entry += f"\n   {desc[:300]}"
                    results.append(entry)

            if results:
                parts.append(f"**Bing 搜索结果（{query}）**:")
                parts.extend(results)

            # Also try to extract direct answer box
            answer = re.search(r'<div[^>]*class="[^"]*b_ans[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
            if answer:
                answer_text = re.sub(r'<[^>]+>', ' ', answer.group(1)).strip()
                answer_text = re.sub(r'\s+', ' ', answer_text)[:500]
                if answer_text and len(answer_text) > 10:
                    parts.insert(0, f"**直接回答**: {answer_text}")

    except Exception as e:
        pass  # Fall through to DuckDuckGo

    # ---- Method 2: DuckDuckGo API (backup) ----
    if not parts:
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
                    headers={"User-Agent": "MySQL-Query-Agent/1.0"}
                )
                resp.raise_for_status()
                data = resp.json()

            abstract = data.get("Abstract", "")
            if abstract:
                parts.append(f"**摘要**: {abstract}")
            answer = data.get("Answer", "")
            if answer:
                parts.append(f"\n**直接回答**: {answer}")
            related = data.get("RelatedTopics", [])
            if related and not abstract:
                parts.append("**相关内容**:")
                for topic in related[:5]:
                    if isinstance(topic, dict) and topic.get("Text"):
                        parts.append(f"- {topic['Text'][:200]}")
        except Exception:
            pass

    if not parts:
        return {"success": True, "tool_name": "search_web",
                "content": f"未找到与 '{query}' 相关的信息。请尝试更具体的关键词。"}

    return {"success": True, "tool_name": "search_web",
            "content": "\n".join(parts),
            "raw_result": {"query": query}}


# ---- Tool Schemas ----

# Tool schemas in Anthropic format
TOOLS = [
    {
        "name": "query_database",
        "description": (
            "对当前连接的 MySQL 数据库执行只读 SQL 查询。"
            "使用此工具回答关于实际数据的问题：谁、什么、何时、多少人、"
            "具体记录、统计数据等。只允许 SELECT 语句。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "要执行的 SQL SELECT 查询语句。"
                        "必须使用标准 MySQL 语法。表名和列名用反引号括起来。"
                        "可使用 JOIN、WHERE、GROUP BY、ORDER BY、LIMIT 等。"
                    ),
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "在沙箱环境中执行 Python 代码，用于数学计算、统计分析和图表生成。"
            "输入数据通过 input_data 变量传入（包含查询结果）。"
            "将计算结果赋值给 result 变量。"
            "可用的库：math, statistics, json, decimal, fractions, "
            "collections, itertools, functools, random, datetime, "
            "matplotlib (含 matplotlib.pyplot)。\n"
            "生成图表时：import matplotlib; matplotlib.use('Agg'); "
            "import matplotlib.pyplot as plt; "
            "设置中文字体: plt.rcParams['font.sans-serif'] = ['Sarasa Gothic SC']; "
            "plt.rcParams['axes.unicode_minus'] = False; "
            "保存图表: plt.savefig('chart_<描述>.png', dpi=100, bbox_inches='tight'); "
            "plt.close(); 每个图表单独创建 figure，不要使用 subplots 组合。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "要执行的 Python 代码。input_data 变量包含查询结果的 rows 和 row_count。"
                        "将输出赋值给 result 变量。使用 print() 进行调试输出。"
                        "生成图表时，保存为 chart_*.png，新文件会自动被检测和展示。"
                    ),
                }
            },
            "required": ["code"],
        },
    },
    {
        "name": "list_tables",
        "description": (
            "列出当前数据库中所有表和视图，包含行数估算。"
            "用于了解数据库的整体结构。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "describe_table",
        "description": (
            "查看指定表的结构信息：列名、数据类型、是否可空、键类型、注释，"
            "以及该表的外键关系。用于深入了解表结构。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "要查看的表名",
                }
            },
            "required": ["table_name"],
        },
    },
    {
        "name": "sample_data",
        "description": (
            "从指定表中抽样查看数据，了解实际数据内容和格式。"
            "可用于发现列的值分布、数据模式等。默认返回 5 行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "要抽样的表名",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回的行数（默认 5）",
                },
            },
            "required": ["table_name"],
        },
    },
    {
        "name": "get_schema_summary",
        "description": (
            "【推荐优先使用】获取完整的数据库结构概览。一次调用返回："
            "所有表（含列、类型、键、注释）、每张表的抽样数据、"
            "所有外键关系（含预构建的 JOIN 语句）、值分布统计。"
            "这是最高效的工具——替代多次 list_tables + describe_table + sample_data 调用。"
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_table_detail",
        "description": (
            "获取单张表的完整信息：列定义、抽样数据、关联表（含 JOIN 示例）、值分布。"
            "比 describe_table + sample_data 更全面。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table_name": {"type": "string", "description": "表名"}},
            "required": ["table_name"],
        },
    },
    {
        "name": "analyze_column",
        "description": (
            "对指定列进行统计分析：最小值、最大值、平均值、唯一值数量、"
            "NULL 数量、Top 10 值分布。适用于数值列和分类列。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": "表名"},
                "column_name": {"type": "string", "description": "列名"},
            },
            "required": ["table_name", "column_name"],
        },
    },
    {
        "name": "find_related_tables",
        "description": (
            "查找与指定表有外键关系的所有表，返回预构建的 JOIN 语句（单跳）。"
            "用于了解表之间的直接关联关系。如需跨多张表联查，用 find_join_path。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table_name": {"type": "string", "description": "表名"}},
            "required": ["table_name"],
        },
    },
    {
        "name": "find_join_path",
        "description": (
            "查找两个表之间的最短 FK 连接路径（多跳遍历）。"
            "返回从 source_table 到 target_table 的完整 JOIN 链（含每跳 SQL）。"
            "用于规划跨多张表的复杂联查。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_table": {"type": "string", "description": "起始表名"},
                "target_table": {"type": "string", "description": "目标表名"},
            },
            "required": ["source_table", "target_table"],
        },
    },
    {
        "name": "get_suggested_questions",
        "description": (
            "获取针对当前数据库的推荐查询问题（由 AI 基于数据库结构自动生成）。"
            "当用户不确定要问什么时使用。"
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ask_admin",
        "description": (
            "向数据库管理员提问，获取模糊列的真实业务含义或编码值映射。"
            "仅在通过自主探索和 search_documentation 都无法确定列含义时使用。"
            "适用场景：列名是缩写/编码（如 F_STATUS、COL_003），"
            "且样本值是编码或缩写（如 '01'/'02'/'03'、'A'/'B'/'C'），"
            "无法仅从样本值推断含义。"
            "提问应清晰、包含上下文（样本值、你的推测）。"
            "不适用于：含义自明的列（如样本值本身就是中文部门名称、人名等）。"
            "工具会立即返回（不等待管理员回答），答案将在后续查询中通过 RAG 提供。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "表名",
                },
                "column_name": {
                    "type": "string",
                    "description": "列名。如果是关于整张表的问题则留空。",
                },
                "question": {
                    "type": "string",
                    "description": "清晰的问题，包含上下文和样本值。例如：'employee 表的 F_STATUS 列包含值 01/02/03，这些编码分别代表什么含义？'",
                },
            },
            "required": ["table_name", "question"],
        },
    },
    {
        "name": "search_documentation",
        "description": (
            "搜索数据库文档和管理员说明以获取信息：表结构、列含义、编码值映射、"
            "业务规则等。管理员可能已经回答过关于某些列的说明——"
            "对列含义不确定时应优先使用此工具搜索。"
            "返回最相关的文档片段和管理员说明。"
        ),

        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关于数据库结构的搜索查询",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_web",
        "description": (
            "搜索互联网获取外部知识（地理位置编码、行业分类标准、专业术语定义等）。"
            "注意：当前时间已由系统提供（见 [系统时间参考]），不要搜索时间日期。"
            "搜索时使用用户问题中的原始关键词，不要附加你猜测的年份或数值。"
            "不要用于查询数据库中的数据。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如 '北京时间现在几点'、'2025年GDP'",
                }
            },
            "required": ["query"],
        },
    },
]

# OpenAI-format tool definitions
OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "对当前连接的 MySQL 数据库执行只读 SQL 查询。"
                "使用此工具回答关于实际数据的问题：谁、什么、何时、多少人、"
                "具体记录、统计数据等。只允许 SELECT 语句。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            "要执行的 SQL SELECT 查询语句。"
                            "必须使用标准 MySQL 语法。表名和列名用反引号括起来。"
                            "可使用 JOIN、WHERE、GROUP BY、ORDER BY、LIMIT 等。"
                        ),
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "在沙箱环境中执行 Python 代码，用于数学计算、统计分析和图表生成。"
                "输入数据通过 input_data 变量传入（包含查询结果）。"
                "将计算结果赋值给 result 变量。"
                "可用的库：math, statistics, json, decimal, fractions, "
                "collections, itertools, functools, random, datetime, "
                "matplotlib (含 matplotlib.pyplot)。"
                "生成图表时：matplotlib.use('Agg'); 中文字体设为 Sarasa Gothic SC; "
                "保存为 chart_*.png 文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "要执行的 Python 代码。input_data 变量包含查询结果的 rows 和 row_count。"
                            "将输出赋值给 result 变量。"
                        ),
                    }
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": (
                "列出当前数据库中所有表和视图，包含行数估算。"
                "用于了解数据库的整体结构。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": (
                "查看指定表的结构信息：列名、数据类型、是否可空、键类型、注释，"
                "以及该表的外键关系。用于深入了解表结构。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "要查看的表名",
                    }
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sample_data",
            "description": (
                "从指定表中抽样查看数据，了解实际数据内容和格式。"
                "可用于发现列的值分布、数据模式等。默认返回 5 行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "要抽样的表名",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回的行数（默认 5）",
                    },
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema_summary",
            "description": "【推荐优先使用】获取完整的数据库结构概览。一次调用返回所有表、列、抽样数据、外键关系和 JOIN 示例、值分布。替代多次 list_tables + describe_table + sample_data。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_table_detail",
            "description": "获取单张表的完整信息：列定义、抽样数据、关联表和 JOIN 示例、值分布。",
            "parameters": {
                "type": "object",
                "properties": {"table_name": {"type": "string", "description": "表名"}},
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_column",
            "description": "对指定列进行统计分析：最小值、最大值、平均值、唯一值数量、NULL 数量、Top 10 值分布。",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "表名"},
                    "column_name": {"type": "string", "description": "列名"},
                },
                "required": ["table_name", "column_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_related_tables",
            "description": "查找与指定表有外键关系的所有表，返回预构建的 JOIN 语句（单跳）。用于了解表之间的直接关联关系。如需跨多张表联查，用 find_join_path。",
            "parameters": {
                "type": "object",
                "properties": {"table_name": {"type": "string", "description": "表名"}},
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_join_path",
            "description": "查找两个表之间的最短 FK 连接路径（多跳遍历）。返回完整 JOIN 链。用于规划跨多张表的复杂联查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_table": {"type": "string", "description": "起始表名"},
                    "target_table": {"type": "string", "description": "目标表名"},
                },
                "required": ["source_table", "target_table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_suggested_questions",
            "description": "获取针对当前数据库的推荐查询问题（由 AI 基于数据库结构自动生成）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_admin",
            "description": (
                "向数据库管理员提问，获取模糊列的真实业务含义或编码值映射。"
                "仅在通过自主探索和 search_documentation 都无法确定列含义时使用。"
                "适用场景：列名是缩写/编码且样本值是编码或缩写无法推断含义。"
                "提问应清晰、包含上下文（样本值、你的推测）。"
                "工具会立即返回（不等待管理员回答），答案将在后续查询中通过 RAG 提供。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "表名"},
                    "column_name": {"type": "string", "description": "列名。如果是关于整张表的问题则留空。"},
                    "question": {"type": "string", "description": "清晰的问题，包含上下文和样本值"},
                },
                "required": ["table_name", "question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documentation",
            "description": (
                "搜索数据库文档和管理员说明以获取信息：表结构、列含义、编码值映射、"
                "业务规则等。管理员可能已经回答过关于某些列的说明——"
                "对列含义不确定时应优先使用此工具搜索。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "关于数据库结构的搜索查询",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "搜索互联网获取外部知识（地理位置编码、行业分类等）。当前时间由系统提供，不要搜索时间。搜索时使用原始关键词，不附加猜测值。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        },
    },
]

# Tool subsets for each agent role
# TOOLS layout: [0]=query_database, [1]=run_python, [2]=list_tables,
#   [3]=describe_table, [4]=sample_data,
#   [5]=get_schema_summary, [6]=get_table_detail, [7]=analyze_column,
#   [8]=find_related_tables, [9]=find_join_path, [10]=get_suggested_questions,
#   [11]=ask_admin, [12]=search_documentation, [13]=search_web

# SQL Agent: query_database + high-level skills + search (RAG, web)
SQL_AGENT_TOOLS = [TOOLS[0], TOOLS[5], TOOLS[6], TOOLS[7], TOOLS[8], TOOLS[9], TOOLS[12], TOOLS[13]]
OPENAI_SQL_AGENT_TOOLS = [OPENAI_TOOLS[0], OPENAI_TOOLS[5], OPENAI_TOOLS[6], OPENAI_TOOLS[7], OPENAI_TOOLS[8], OPENAI_TOOLS[9], OPENAI_TOOLS[12], OPENAI_TOOLS[13]]

# Compute Agent: only run_python
COMPUTE_AGENT_TOOLS = [TOOLS[1]]
OPENAI_COMPUTE_AGENT_TOOLS = [OPENAI_TOOLS[1]]

# Visualization Agent: only run_python (same tool, dedicated chart prompt)
VISUALIZATION_AGENT_TOOLS = [TOOLS[1]]
OPENAI_VISUALIZATION_AGENT_TOOLS = [OPENAI_TOOLS[1]]

# Explore Agent: only low-level tools (no skills that depend on schema cache)
EXPLORE_AGENT_TOOLS = [TOOLS[2], TOOLS[3], TOOLS[4], TOOLS[0], TOOLS[1]]
OPENAI_EXPLORE_AGENT_TOOLS = [OPENAI_TOOLS[2], OPENAI_TOOLS[3], OPENAI_TOOLS[4], OPENAI_TOOLS[0], OPENAI_TOOLS[1]]

# Analysis Agent: schema exploration + web search + RAG search + admin Q&A (NO query_database)
ANALYSIS_AGENT_TOOLS = [TOOLS[5], TOOLS[6], TOOLS[7], TOOLS[8], TOOLS[9], TOOLS[11], TOOLS[12], TOOLS[13]]
OPENAI_ANALYSIS_AGENT_TOOLS = [OPENAI_TOOLS[5], OPENAI_TOOLS[6], OPENAI_TOOLS[7], OPENAI_TOOLS[8], OPENAI_TOOLS[9], OPENAI_TOOLS[11], OPENAI_TOOLS[12], OPENAI_TOOLS[13]]

# Doc Agent: search + skills
DOC_TOOLS = [TOOLS[12], TOOLS[5], TOOLS[6]]
OPENAI_DOC_TOOLS = [OPENAI_TOOLS[12], OPENAI_TOOLS[5], OPENAI_TOOLS[6]]

# Ambiguous: query + skills + search (NO ask_admin for ambiguous)
AMBIGUOUS_TOOLS = [TOOLS[0], TOOLS[5], TOOLS[6], TOOLS[7], TOOLS[8], TOOLS[9], TOOLS[12], TOOLS[13]]
OPENAI_AMBIGUOUS_TOOLS = [OPENAI_TOOLS[0], OPENAI_TOOLS[5], OPENAI_TOOLS[6], OPENAI_TOOLS[7], OPENAI_TOOLS[8], OPENAI_TOOLS[9], OPENAI_TOOLS[12], OPENAI_TOOLS[13]]

# Empty tools for NL Agent
NO_TOOLS = []
OPENAI_NO_TOOLS = []


def get_tools(classification: str, provider: str = "anthropic") -> list:
    """Get the appropriate tool list based on classification and provider.

    Classification can be: DATA_QUESTION, DOC_QUESTION, AMBIGUOUS,
    or special agent names: 'sql_agent', 'compute_agent', 'visualization_agent',
    'nl_agent', 'explore_agent', 'analysis_agent', 'doc_agent'.
    """
    if provider == "openai":
        mapping = {
            "sql_agent": OPENAI_SQL_AGENT_TOOLS,
            "compute_agent": OPENAI_COMPUTE_AGENT_TOOLS,
            "visualization_agent": OPENAI_VISUALIZATION_AGENT_TOOLS,
            "explore_agent": OPENAI_EXPLORE_AGENT_TOOLS,
            "analysis_agent": OPENAI_ANALYSIS_AGENT_TOOLS,
            "nl_agent": OPENAI_NO_TOOLS,
            "doc_agent": OPENAI_DOC_TOOLS,
            "DATA_QUESTION": OPENAI_SQL_AGENT_TOOLS,
            "DOC_QUESTION": OPENAI_DOC_TOOLS,
            "AMBIGUOUS": OPENAI_AMBIGUOUS_TOOLS,
        }
    else:
        mapping = {
            "sql_agent": SQL_AGENT_TOOLS,
            "compute_agent": COMPUTE_AGENT_TOOLS,
            "visualization_agent": VISUALIZATION_AGENT_TOOLS,
            "explore_agent": EXPLORE_AGENT_TOOLS,
            "analysis_agent": ANALYSIS_AGENT_TOOLS,
            "nl_agent": NO_TOOLS,
            "doc_agent": DOC_TOOLS,
            "DATA_QUESTION": SQL_AGENT_TOOLS,
            "DOC_QUESTION": DOC_TOOLS,
            "AMBIGUOUS": AMBIGUOUS_TOOLS,
        }
    return mapping.get(classification, mapping["AMBIGUOUS"])


def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """
    Execute a tool call and return the result.

    Returns:
        dict with keys: success, content, tool_name
    """
    try:
        if tool_name == "query_database":
            sql = tool_input.get("sql", "")
            if not sql:
                return {
                    "success": False,
                    "tool_name": "query_database",
                    "content": "错误：未提供 SQL 查询语句。",
                }
            # Pass schema snapshot + table filter for pre-flight validation
            from app.database.schema_cache import schema_manager
            snap = schema_manager.snapshot
            result = execute_query(sql, snapshot=snap, table_filter=_active_table_filter)
            formatted = format_query_result(result)
            return {
                "success": True,
                "tool_name": "query_database",
                "content": formatted,
                "raw_result": result,
            }

        elif tool_name == "run_python":
            code = tool_input.get("code", "")
            if not code:
                return {
                    "success": False,
                    "tool_name": "run_python",
                    "content": "错误：未提供 Python 代码。",
                }
            # input_data is pre-set by the orchestrator before calling
            input_data = tool_input.get("_input_data", {})
            result = _execute_python(code, input_data)
            # Keep chart_files in result — orchestrator collects them
            if "chart_files" not in result:
                result["chart_files"] = []
            return result

        elif tool_name == "list_tables":
            return _list_tables()

        elif tool_name == "describe_table":
            table_name = tool_input.get("table_name", "")
            if not table_name:
                return {"success": False, "tool_name": "describe_table", "content": "错误：未提供表名。"}
            return _describe_table(table_name)

        elif tool_name == "sample_data":
            table_name = tool_input.get("table_name", "")
            limit = tool_input.get("limit", 5)
            if not table_name:
                return {"success": False, "tool_name": "sample_data", "content": "错误：未提供表名。"}
            return _sample_data(table_name, int(limit))

        elif tool_name == "get_schema_summary":
            return skill_schema_summary()

        elif tool_name == "get_table_detail":
            return skill_table_detail(tool_input.get("table_name", ""))

        elif tool_name == "analyze_column":
            return skill_analyze_column(
                tool_input.get("table_name", ""),
                tool_input.get("column_name", ""),
            )

        elif tool_name == "find_related_tables":
            return skill_find_related(tool_input.get("table_name", ""))

        elif tool_name == "find_join_path":
            return skill_find_join_path(
                tool_input.get("source_table", ""),
                tool_input.get("target_table", ""),
            )

        elif tool_name == "get_suggested_questions":
            return skill_suggested_questions()

        elif tool_name == "search_web":
            query = tool_input.get("query", "")
            if not query:
                return {"success": False, "tool_name": "search_web", "content": "错误：未提供搜索关键词。"}
            return _search_web(query)

        elif tool_name == "ask_admin":
            table_name = tool_input.get("table_name", "")
            column_name = tool_input.get("column_name", "")
            question = tool_input.get("question", "")
            if not table_name or not question:
                return {
                    "success": False,
                    "tool_name": "ask_admin",
                    "content": "错误：未提供表名或问题。",
                }

            # Get current profile_id
            profile_id = db.current_profile.id if db.current_profile else "default"

            # Check if already answered
            try:
                from app.rag.admin_clarifications import clarification_store
                existing = clarification_store.get_clarification(profile_id, table_name, column_name)
                if existing:
                    answers = []
                    for e in existing:
                        label = f"`{e.table_name}`.`{e.column_name}`" if e.column_name else f"`{e.table_name}`"
                        answers.append(f"{label}: {e.answer}")
                    return {
                        "success": True,
                        "tool_name": "ask_admin",
                        "content": "管理员已说明过该列含义：\n" + "\n".join(answers),
                    }

                # Save as pending question
                qid = clarification_store.add_question(
                    profile_id=profile_id,
                    table_name=table_name,
                    column_name=column_name,
                    question=question,
                )
            except Exception as e:
                return {
                    "success": False,
                    "tool_name": "ask_admin",
                    "content": f"保存问题时出错: {e}",
                }

            return {
                "success": True,
                "tool_name": "ask_admin",
                "content": (
                    f"已向管理员提交问题（ID: {qid}）。"
                    f"管理员回答后，后续查询将自动使用该信息。"
                    f"当前查询请基于已有信息做出最佳努力的回答。"
                ),
                "_admin_question": {
                    "question_id": qid,
                    "table_name": table_name,
                    "column_name": column_name,
                    "question": question,
                },
            }

        elif tool_name == "search_documentation":
            query = tool_input.get("query", "")
            if not query:
                return {
                    "success": False,
                    "tool_name": "search_documentation",
                    "content": "错误：未提供搜索查询。",
                }
            chunks = retrieve_chunks(query)
            if not chunks:
                return {
                    "success": True,
                    "tool_name": "search_documentation",
                    "content": "文档中未找到相关信息。",
                }
            parts = []
            for i, (chunk, score) in enumerate(chunks):
                parts.append(f"--- 文档片段 {i+1}（相关度: {score:.2f}）---")
                parts.append(chunk)
            return {
                "success": True,
                "tool_name": "search_documentation",
                "content": "\n\n".join(parts),
            }

        else:
            return {
                "success": False,
                "tool_name": tool_name,
                "content": f"未知工具：{tool_name}",
            }

    except QueryExecutionError as e:
        error_content = f"查询执行失败：{e.message}\nSQL: {e.sql}"
        if e.mysql_error:
            error_content += f"\n数据库错误: {e.mysql_error}"
        # Include schema validation hints (column/table suggestions)
        if e.schema_errors:
            error_content += f"\n提示: {'; '.join(e.schema_errors)}"
        return {
            "success": False,
            "tool_name": tool_name,
            "content": error_content,
        }
    except Exception as e:
        return {
            "success": False,
            "tool_name": tool_name,
            "content": f"工具执行出错：{str(e)}",
        }


# ---- Schema Exploration Functions ----


def _list_tables() -> dict:
    """List all tables and views in the current database. Works with MySQL and PostgreSQL."""
    import time
    start = time.time()
    db_name = db.current_db_name
    is_pg = (db.db_type == "postgresql")
    is_dm = (db.db_type in ("dameng", "dm", "dm8"))

    try:
        with db.cursor() as cursor:
            if is_dm:
                cursor.execute(
                    "SELECT TABLE_NAME, "
                    "'BASE TABLE' AS TABLE_TYPE, "
                    "NUM_ROWS AS TABLE_ROWS, "
                    "COALESCE((SELECT COMMENTS FROM ALL_TAB_COMMENTS "
                    "WHERE TABLE_NAME = t.TABLE_NAME AND OWNER = t.OWNER), '') AS TABLE_COMMENT "
                    "FROM ALL_TABLES t "
                    "WHERE TABLE_NAME NOT LIKE '##%' "
                    "AND TABLE_NAME NOT LIKE 'SREF%' "
                    "AND OWNER = USER "
                    "ORDER BY TABLE_NAME"
                )
            elif is_pg:
                cursor.execute(
                    "SELECT t.tablename AS table_name, 'BASE TABLE' AS table_type, "
                    "COALESCE(s.n_live_tup, 0) AS table_rows, "
                    "COALESCE(obj_description(pc.oid), '') AS table_comment "
                    "FROM pg_catalog.pg_tables t "
                    "LEFT JOIN pg_catalog.pg_class pc ON pc.relname = t.tablename "
                    "LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relname = t.tablename "
                    "AND s.schemaname = t.schemaname "
                    "WHERE t.schemaname NOT IN ('pg_catalog', 'information_schema') "
                    "ORDER BY t.tablename"
                )
            else:
                cursor.execute(
                    "SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, TABLE_COMMENT "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
                    "ORDER BY TABLE_NAME",
                    (db_name,),
                )
            rows = cursor.fetchall()
        elapsed = round((time.time() - start) * 1000, 1)

        # Respect global table filter
        from app.database.schema_cache import get_active_table_filter
        tf = get_active_table_filter()
        if tf:
            tf_set = {t.lower() for t in tf}
            rows = [r for r in rows if r["TABLE_NAME"].lower() in tf_set]

        if not rows:
            return {
                "success": True,
                "tool_name": "list_tables",
                "content": f"数据库 {db_name} 中没有找到表。",
                "raw_result": {"tables": [], "count": 0, "row_count": 0, "execution_ms": elapsed},
            }

        filter_note = f"（筛选 {len(tf)} 张）" if tf else ""
        lines = [f"数据库 {db_name} 包含以下表{filter_note}：\n"]
        lines.append("| 表名 | 类型 | 行数（估算） | 注释 |")
        lines.append("|------|------|------------|------|")
        for r in rows:
            lines.append(
                f"| {r['TABLE_NAME']} | {r.get('TABLE_TYPE', 'TABLE')} | "
                f"{r.get('TABLE_ROWS', '?') or '?'} | {r.get('TABLE_COMMENT', '') or '-'} |"
            )

        return {
            "success": True,
            "tool_name": "list_tables",
            "content": "\n".join(lines),
            "raw_result": {"tables": [r["TABLE_NAME"] for r in rows], "count": len(rows), "row_count": len(rows), "execution_ms": elapsed},
        }
    except Exception as e:
        return {
            "success": False,
            "tool_name": "list_tables",
            "content": f"列出表失败：{str(e)}",
        }


def _describe_table(table_name: str) -> dict:
    """Show column details and foreign keys for a table. Works with MySQL and PostgreSQL."""
    # Respect global table filter
    from app.database.schema_cache import get_active_table_filter
    tf = get_active_table_filter()
    if tf and table_name.lower() not in {t.lower() for t in tf}:
        return {"success": False, "tool_name": "describe_table",
                "content": f"表 {table_name} 不在当前筛选范围内。可用表: {', '.join(tf)}"}

    db_name = db.current_db_name
    is_pg = (db.db_type == "postgresql")
    is_dm = (db.db_type in ("dameng", "dm", "dm8"))
    quote = db.quote

    try:
        # Get columns
        with db.cursor() as cursor:
            if is_dm:
                cursor.execute(
                    "SELECT c.COLUMN_NAME, c.DATA_TYPE, "
                    "c.DATA_TYPE || '(' || c.DATA_LENGTH || ')' AS COLUMN_TYPE, "
                    "c.NULLABLE AS IS_NULLABLE, "
                    "CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'PRI' ELSE '' END AS COLUMN_KEY, "
                    "c.DATA_DEFAULT AS COLUMN_DEFAULT, "
                    "COALESCE(cm.COMMENTS, '') AS COLUMN_COMMENT, "
                    "c.COLUMN_ID AS ORDINAL_POSITION "
                    "FROM ALL_TAB_COLUMNS c "
                    "LEFT JOIN ALL_COL_COMMENTS cm "
                    "ON c.TABLE_NAME = cm.TABLE_NAME AND c.COLUMN_NAME = cm.COLUMN_NAME AND c.OWNER = cm.OWNER "
                    "LEFT JOIN ("
                    "SELECT acc.TABLE_NAME, acc.COLUMN_NAME "
                    "FROM ALL_CONS_COLUMNS acc "
                    "JOIN ALL_CONSTRAINTS ac ON acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME "
                    "AND acc.OWNER = ac.OWNER "
                    "WHERE ac.CONSTRAINT_TYPE = 'P' AND acc.OWNER = USER"
                    ") pk ON c.TABLE_NAME = pk.TABLE_NAME AND c.COLUMN_NAME = pk.COLUMN_NAME "
                    "WHERE c.OWNER = USER AND c.TABLE_NAME = %s ORDER BY c.COLUMN_ID",
                    (table_name.upper(),) if table_name else (table_name,),
                )
            elif is_pg:
                cursor.execute(
                    "SELECT c.COLUMN_NAME, c.DATA_TYPE, "
                    "CASE WHEN c.CHARACTER_MAXIMUM_LENGTH IS NOT NULL "
                    "THEN c.DATA_TYPE || '(' || c.CHARACTER_MAXIMUM_LENGTH || ')' "
                    "WHEN c.NUMERIC_PRECISION IS NOT NULL "
                    "THEN c.DATA_TYPE || '(' || c.NUMERIC_PRECISION || ',' || COALESCE(c.NUMERIC_SCALE,0) || ')' "
                    "ELSE c.DATA_TYPE END AS COLUMN_TYPE, "
                    "c.IS_NULLABLE, "
                    "CASE WHEN tc.CONSTRAINT_TYPE = 'PRIMARY KEY' THEN 'PRI' "
                    "WHEN tc2.CONSTRAINT_TYPE = 'UNIQUE' THEN 'UNI' ELSE '' END AS COLUMN_KEY, "
                    "c.COLUMN_DEFAULT, "
                    "COALESCE(pg_catalog.col_description(pc.oid, c.ORDINAL_POSITION), '') AS COLUMN_COMMENT, "
                    "c.ORDINAL_POSITION "
                    "FROM information_schema.COLUMNS c "
                    "JOIN pg_catalog.pg_class pc ON pc.relname = c.TABLE_NAME "
                    "LEFT JOIN information_schema.KEY_COLUMN_USAGE kcu "
                    "ON kcu.TABLE_NAME = c.TABLE_NAME AND kcu.COLUMN_NAME = c.COLUMN_NAME "
                    "AND kcu.TABLE_SCHEMA NOT IN ('pg_catalog', 'information_schema') "
                    "LEFT JOIN information_schema.TABLE_CONSTRAINTS tc "
                    "ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY' "
                    "LEFT JOIN information_schema.TABLE_CONSTRAINTS tc2 "
                    "ON tc2.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND tc2.CONSTRAINT_TYPE = 'UNIQUE' "
                    "WHERE c.TABLE_NAME = %s "
                    "AND c.TABLE_SCHEMA NOT IN ('pg_catalog', 'information_schema') "
                    "ORDER BY c.ORDINAL_POSITION",
                    (table_name,),
                )
            else:
                cursor.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, "
                    "COLUMN_KEY, COLUMN_DEFAULT, COLUMN_COMMENT, ORDINAL_POSITION "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION",
                    (db_name, table_name),
                )
            cols = cursor.fetchall()

        if not cols:
            return {
                "success": False,
                "tool_name": "describe_table",
                "content": f"表 {table_name} 不存在于数据库 {db_name} 中。",
            }

        lines = [f"## 表: {table_name}\n"]
        lines.append(f"数据库: {db_name} | 列数: {len(cols)}\n")
        lines.append("| # | 列名 | 类型 | 可空 | 键 | 默认值 | 注释 |")
        lines.append("|---|------|------|------|----|--------|------|")
        for c in cols:
            key = c["COLUMN_KEY"] or "-"
            nullable = "是" if c["IS_NULLABLE"] == "YES" else "否"
            default = str(c.get("COLUMN_DEFAULT", "")) if c.get("COLUMN_DEFAULT") is not None else "NULL"
            comment = c.get("COLUMN_COMMENT", "") or "-"
            lines.append(
                f"| {c['ORDINAL_POSITION']} | {c['COLUMN_NAME']} | "
                f"{c['COLUMN_TYPE']} | {nullable} | {key} | {default} | {comment} |"
            )

        # Get foreign keys
        fks = []
        with db.cursor() as cursor:
            if is_dm:
                cursor.execute(
                    "SELECT ac.CONSTRAINT_NAME, acc.COLUMN_NAME, "
                    "ac_r.TABLE_NAME AS REFERENCED_TABLE_NAME, "
                    "acc_r.COLUMN_NAME AS REFERENCED_COLUMN_NAME "
                    "FROM ALL_CONSTRAINTS ac "
                    "JOIN ALL_CONS_COLUMNS acc "
                    "ON ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME AND ac.OWNER = acc.OWNER "
                    "LEFT JOIN ALL_CONSTRAINTS ac_r "
                    "ON ac.R_CONSTRAINT_NAME = ac_r.CONSTRAINT_NAME AND ac.R_OWNER = ac_r.OWNER "
                    "LEFT JOIN ALL_CONS_COLUMNS acc_r "
                    "ON ac_r.CONSTRAINT_NAME = acc_r.CONSTRAINT_NAME AND ac_r.OWNER = acc_r.OWNER "
                    "WHERE ac.CONSTRAINT_TYPE = 'R' AND ac.OWNER = USER "
                    "AND ac.TABLE_NAME = %s",
                    (table_name.upper(),) if table_name else (table_name,),
                )
            elif is_pg:
                cursor.execute(
                    "SELECT tc.CONSTRAINT_NAME, kcu.COLUMN_NAME, "
                    "ccu.TABLE_NAME AS REFERENCED_TABLE_NAME, "
                    "ccu.COLUMN_NAME AS REFERENCED_COLUMN_NAME "
                    "FROM information_schema.TABLE_CONSTRAINTS tc "
                    "JOIN information_schema.KEY_COLUMN_USAGE kcu "
                    "ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                    "JOIN information_schema.REFERENTIAL_CONSTRAINTS rc "
                    "ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME "
                    "JOIN information_schema.CONSTRAINT_COLUMN_USAGE ccu "
                    "ON rc.UNIQUE_CONSTRAINT_NAME = ccu.CONSTRAINT_NAME "
                    "WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY' "
                    "AND tc.TABLE_NAME = %s",
                    (table_name,),
                )
            else:
                cursor.execute(
                    "SELECT CONSTRAINT_NAME, COLUMN_NAME, "
                    "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                    "FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "AND REFERENCED_TABLE_NAME IS NOT NULL",
                    (db_name, table_name),
                )
            fks = cursor.fetchall()

        if fks:
            lines.append("\n### 外键关系")
            for fk in fks:
                lines.append(
                    f"- `{fk['COLUMN_NAME']}` → "
                    f"`{fk['REFERENCED_TABLE_NAME']}`.`{fk['REFERENCED_COLUMN_NAME']}`"
                )

        # Get row count
        with db.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS cnt FROM {quote(table_name)}")
            rc = cursor.fetchone()
            if rc:
                lines.append(f"\n行数: {rc['cnt']}")

        # Build structured raw_result for NL agent
        col_data = []
        for c in cols:
            col_data.append([
                c["ORDINAL_POSITION"], c["COLUMN_NAME"], c["COLUMN_TYPE"],
                "是" if c["IS_NULLABLE"] == "YES" else "否",
                c["COLUMN_KEY"] or "-",
                str(c.get("COLUMN_DEFAULT", "")) if c.get("COLUMN_DEFAULT") is not None else "NULL",
                c.get("COLUMN_COMMENT", "") or "-",
            ])
        return {
            "success": True,
            "tool_name": "describe_table",
            "content": "\n".join(lines),
            "raw_result": {
                "table_name": table_name,
                "columns": ["序号", "列名", "类型", "可空", "键", "默认值", "注释"],
                "rows": col_data,
                "row_count": len(cols),
                "foreign_keys": [f"{fk['COLUMN_NAME']} → {fk['REFERENCED_TABLE_NAME']}.{fk['REFERENCED_COLUMN_NAME']}" for fk in fks],
            },
        }
    except Exception as e:
        return {
            "success": False,
            "tool_name": "describe_table",
            "content": f"查看表结构失败：{str(e)}",
        }


def _sample_data(table_name: str, limit: int = 5) -> dict:
    """Sample rows from a table."""
    # Respect global table filter
    from app.database.schema_cache import get_active_table_filter
    tf = get_active_table_filter()
    if tf and table_name.lower() not in {t.lower() for t in tf}:
        return {"success": False, "tool_name": "sample_data",
                "content": f"表 {table_name} 不在当前筛选范围内。可用表: {', '.join(tf)}"}

    try:
        with db.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {db.quote(table_name)} LIMIT {limit}")
            columns = [col[0] for col in cursor.description] if cursor.description else []
            rows = cursor.fetchall()

        if not rows:
            return {
                "success": True,
                "tool_name": "sample_data",
                "content": f"表 {table_name} 中没有数据。",
                "raw_result": {"table_name": table_name, "columns": [], "rows": [], "row_count": 0},
            }

        lines = [f"## 表 {table_name} 的抽样数据（{len(rows)} 行）\n"]
        # Table header
        lines.append("| " + " | ".join(columns) + " |")
        lines.append("|" + "|".join(["---"] * len(columns)) + "|")
        # Rows
        row_list = []
        for row in rows:
            formatted = [str(row.get(col, "NULL")) if row.get(col) is not None else "NULL" for col in columns]
            lines.append("| " + " | ".join(formatted) + " |")
            row_list.append(formatted)

        return {
            "success": True,
            "tool_name": "sample_data",
            "content": "\n".join(lines),
            "raw_result": {"table_name": table_name, "columns": columns, "rows": row_list, "row_count": len(rows)},
        }
    except Exception as e:
        return {
            "success": False,
            "tool_name": "sample_data",
            "content": f"抽样数据失败：{str(e)}",
        }
