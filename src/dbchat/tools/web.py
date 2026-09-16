"""联网检索工具。

历史实现 `_search_web`（`app/agent/tools.py:284`）用的是同步 `httpx.Client`，
直接跑在事件循环上：最坏情况（Bing 超时 10s + DuckDuckGo 超时 8s）会把
整个事件循环堵 18 秒。这里整体下到线程池（`anyio.to_thread.run_sync`），
抓取与解析逻辑不变。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import anyio
import httpx
from pydantic import BaseModel, Field

from dbchat.tools.registry import ToolContext, ToolOutcome

logger = logging.getLogger(__name__)

__all__ = ["SEARCH_WEB_DESCRIPTION", "SearchWebParams", "search_web"]

SEARCH_WEB_DESCRIPTION = (
    "搜索互联网获取外部知识（地理位置编码、行业分类标准、专业术语定义等）。"
    "注意：当前时间已由系统提供（见 [系统时间参考]），不要搜索时间日期。"
    "搜索时使用用户问题中的原始关键词，不要附加你猜测的年份或数值。"
    "不要用于查询数据库中的数据。"
)

_BING_TIMEOUT_S = 10.0
_DDG_TIMEOUT_S = 8.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class SearchWebParams(BaseModel):
    """联网检索入参。"""

    query: str = Field(description="搜索关键词，用用户问题中的原始表述")


async def search_web(params: SearchWebParams, ctx: ToolContext) -> ToolOutcome:
    """检索互联网（Bing 优先，DuckDuckGo 兜底）。"""
    query = (params.query or "").strip()
    if not query:
        return ToolOutcome.failure("未提供搜索关键词", content="错误：未提供搜索关键词。")

    content = await anyio.to_thread.run_sync(_search_sync, query)
    if not content:
        return ToolOutcome(
            ok=True,
            summary="未找到相关信息",
            content=f"未找到与 '{query}' 相关的信息。请尝试更具体的关键词。",
            payload={"query": query, "matches": 0},
        )
    return ToolOutcome(
        ok=True,
        summary="检索到外部资料",
        content=content,
        payload={"query": query, "matches": 1},
    )


def _search_sync(query: str) -> str:
    """同步检索（在线程池里执行）。"""
    parts: list[str] = []
    _bing(query, parts)
    if not parts:
        _duckduckgo(query, parts)
    return "\n".join(parts)


def _bing(query: str, parts: list[str]) -> None:
    """Bing 检索（国内可达，无需 key）。"""
    try:
        with httpx.Client(timeout=_BING_TIMEOUT_S, follow_redirects=True) as client:
            response = client.get(
                "https://cn.bing.com/search",
                params={"q": query, "setlang": "zh-Hans", "count": 10},
                headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            )
            response.raise_for_status()
            html = response.text

        results = _parse_bing(html)
        if results:
            parts.append(f"**Bing 搜索结果（{query}）**:")
            parts.extend(results)

        answer = re.search(
            r'<div[^>]*class="[^"]*b_ans[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL
        )
        if answer:
            text = re.sub(r"<[^>]+>", " ", answer.group(1)).strip()
            text = re.sub(r"\s+", " ", text)[:500]
            if len(text) > 10:
                parts.insert(0, f"**直接回答**: {text}")
    except httpx.HTTPError as exc:
        # 检索失败要退到下一个来源，但必须留日志 —— 历史实现这里 `except: pass`。
        logger.info("Bing 检索失败，改用备用来源: %s: %s", type(exc).__name__, exc)


def _parse_bing(html: str) -> list[str]:
    results: list[str] = []
    items: list[Any] = re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.DOTALL)
    if not items and re.findall(r'<ol[^>]*id="b_results"[^>]*>(.*?)</ol>', html, re.DOTALL):
        items = re.findall(
            r'<h2[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.DOTALL
        )

    for index, item in enumerate(items[:8], 1):
        if isinstance(item, tuple):
            url, title = item
            clean_title = re.sub(r"<[^>]+>", "", title).strip()
            results.append(f"{index}. **{clean_title}**\n   {url}")
            continue
        snippet = re.findall(r"<p[^>]*>(.*?)</p>", item, re.DOTALL)
        title_match = re.search(r"<h2[^>]*>.*?<a[^>]*>(.*?)</a>", item, re.DOTALL)
        title_text = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else ""
        description = re.sub(r"<[^>]+>", "", snippet[0]).strip() if snippet else ""
        entry = f"{index}. **{title_text}**" if title_text else f"{index}."
        if description:
            entry += f"\n   {description[:300]}"
        results.append(entry)
    return results


def _duckduckgo(query: str, parts: list[str]) -> None:
    """DuckDuckGo 兜底（Bing 无结果时）。"""
    try:
        with httpx.Client(timeout=_DDG_TIMEOUT_S) as client:
            response = client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
                headers={"User-Agent": "MySQL-Query-Agent/1.0"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("DuckDuckGo 检索失败: %s: %s", type(exc).__name__, exc)
        return

    if not isinstance(data, dict):
        return
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
