#!/usr/bin/env python3
"""对比新旧两次采集的行为差异。

两边的**事件契约不同**（v3 用 tool_call/text/done，v4 用
activity.*/answer.delta/run.finished），所以不能直接 diff 事件名，
必须按语义维度对齐后比较。

用法：
    venv/bin/python tools/compare_runs.py tests/golden tests/golden-new
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def analyze_v3(events: list[dict]) -> dict:
    """旧契约：tool_call / text / done / progress。"""
    sqls, tools, answer = [], [], ""
    rounds = 0
    for ev in events:
        name, d = ev["event"], ev["data"]
        if name == "tool_call":
            tools.append(d.get("tool", ""))
            inp = d.get("input")
            if isinstance(inp, dict) and inp.get("sql"):
                sqls.append(inp["sql"])
        elif name == "progress" and "轮重试" in str(d.get("title", "")):
            rounds += 1
        elif name == "text":
            answer += d.get("text", "")
        elif name == "done":
            answer = d.get("message") or answer
    done = next((e["data"] for e in events if e["event"] == "done"), {})
    return {
        "sql_count": len(sqls),
        "tool_calls": len(tools),
        "retries": rounds,
        "queries_reported": done.get("db_queries"),
        "elapsed_ms": done.get("response_time_ms"),
        "answer": answer,
        "errors": [e["data"].get("message", "") for e in events if e["event"] == "error"],
    }


def analyze_v4(events: list[dict]) -> dict:
    """新契约：activity.* / answer.delta / run.finished / stage.retried。"""
    answer = ""
    activities: dict[str, dict] = {}
    retries = 0
    errors = []
    finished = {}
    for ev in events:
        name, d = ev["event"], ev["data"]
        if name == "activity.started":
            activities[d["activity_id"]] = {"kind": d.get("kind"), "target": d.get("target")}
        elif name == "activity.finished":
            activities.setdefault(d["activity_id"], {}).update(d)
        elif name == "stage.retried":
            retries += 1
        elif name == "answer.delta":
            answer += d.get("text", "")
        elif name == "run.finished":
            finished = d
        elif name == "run.failed":
            errors.append(d.get("message", ""))
            finished = d
    done_acts = [a for a in activities.values() if a.get("kind") == "query"]
    # 新契约默认不向用户暴露 SQL（expose_sql_to_user=false），
    # 查询次数改从 run.finished.stats 取 —— 这才是修好后的真实计数。
    stats = finished.get("stats") or {}
    return {
        "sql_count": None,  # 契约刻意不携带 SQL
        "query_activities": len(done_acts),
        "tool_calls": len(activities),
        "retries": retries,
        "queries_reported": stats.get("queries"),
        "rows_reported": stats.get("rows"),
        "elapsed_ms": finished.get("elapsed_ms"),
        "answer": answer or finished.get("answer_markdown", ""),
        "errors": errors,
        "status": finished.get("status"),
    }


def main() -> int:
    old_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/golden")
    new_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "tests/golden-new")

    names = sorted({p.stem for p in old_dir.glob("*.jsonl")} | {p.stem for p in new_dir.glob("*.jsonl")})
    if not names:
        print("没有找到采集数据", file=sys.stderr)
        return 2

    hdr = f"{'用例':14} {'v3耗时':>8} {'v4耗时':>8} {'v3重试':>7} {'v4重试':>7} {'v3查询计数':>10} {'v4查询计数':>10} {'v4状态':>8}"
    print(hdr)
    print("-" * len(hdr.encode("gbk", errors="ignore")))

    totals = {"v3_ms": 0, "v4_ms": 0, "v3_retries": 0, "v4_retries": 0}
    rows = []
    for n in names:
        v3 = analyze_v3(load(old_dir / f"{n}.jsonl"))
        v4 = analyze_v4(load(new_dir / f"{n}.jsonl"))
        rows.append((n, v3, v4))
        totals["v3_ms"] += v3["elapsed_ms"] or 0
        totals["v4_ms"] += v4["elapsed_ms"] or 0
        totals["v3_retries"] += v3["retries"]
        totals["v4_retries"] += v4["retries"]
        print(
            f"{n:14} {(v3['elapsed_ms'] or 0)/1000:>7.0f}s {(v4['elapsed_ms'] or 0)/1000:>7.0f}s "
            f"{v3['retries']:>7} {v4['retries']:>7} {str(v3['queries_reported']):>10} "
            f"{str(v4['queries_reported']):>10} {str(v4.get('status') or '-'):>8}"
        )

    print()
    print(f"总耗时:  v3 {totals['v3_ms']/1000:.0f}s  →  v4 {totals['v4_ms']/1000:.0f}s")
    print(f"总重试:  v3 {totals['v3_retries']}  →  v4 {totals['v4_retries']}")
    print()

    # 查询计数缺陷：v3 恒为 0
    v3_zero = sum(1 for _, v3, _ in rows if v3["queries_reported"] == 0)
    v4_nonzero = sum(1 for _, _, v4 in rows if (v4["queries_reported"] or 0) > 0)
    print(f"查询计数: v3 报 0 的用例 {v3_zero}/{len(rows)}；v4 报正数的用例 {v4_nonzero}/{len(rows)}")
    print()

    print("=== 答案对照（前 100 字）===")
    for n, v3, v4 in rows:
        a3 = " ".join(v3["answer"].split())[:100]
        a4 = " ".join(v4["answer"].split())[:100]
        print(f"\n[{n}]")
        print(f"  v3: {a3 or '(空)'}")
        print(f"  v4: {a4 or '(空)'}")
        if v4["errors"]:
            print(f"  v4 错误: {v4['errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
