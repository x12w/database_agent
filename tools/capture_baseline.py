#!/usr/bin/env python3
"""抓取旧实现的端到端行为基线（golden transcripts）。

重构的每一阶段都要拿这份基线做对比：同一个问题，新旧实现应当给出
等价的查询与结论。没有它，"重构没改变行为"只能靠感觉。

用法：
    venv/bin/python tools/capture_baseline.py --out tests/golden

会把每次提问的原始 SSE 事件流落盘为 JSONL，并附一份摘要。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

BASE = "http://127.0.0.1:8000"


@dataclass
class Case:
    """一个问题 + 目标库。"""

    name: str
    db_id: str
    question: str
    #: 期望走到的路径，用于人工核对覆盖度。
    path: str


# 覆盖各条代码路径：简单查询 / 多轮迭代 / 图表 / 结构类 / 中文时间解析 / 达梦
CASES: list[Case] = [
    Case("simple_count", "1da18658", "一共有多少名员工？", "simple->sql->answer"),
    Case("join_detail", "1da18658", "列出参加过培训的员工姓名和他们的部门", "analysis->sql->answer"),
    Case("aggregate", "1da18658", "每个培训课程有多少人报名？按人数从多到少排列", "group-by"),
    Case("chart", "1da18658", "把各门考试的平均分画成柱状图", "compute/viz"),
    Case("structure", "1da18658", "这个数据库有哪些表？每张表是做什么的？", "fast-path"),
    Case("temporal", "1da18658", "今年的培训场次有哪些？", "时间预处理"),
    Case("empty_result", "1da18658", "有没有叫张三丰的员工参加过考试？", "空结果->重试判定"),
    Case("pg_hospital", "c1c24e63", "这个库里有多少张表？分别叫什么？", "PostgreSQL 方言"),
    Case("dm_local", "2463d232", "这个数据库里有哪些表？", "达梦方言"),
]


def stream(question: str, db_id: str, session_id: str, timeout: int = 420) -> list[dict]:
    """调用 /chat/stream，返回解析后的事件列表。"""
    params = urllib.parse.urlencode(
        {"message": question, "db_id": db_id, "session_id": session_id}
    )
    req = urllib.request.Request(f"{BASE}/chat/stream?{params}")
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        event_name = ""
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    payload = {"_raw": line[6:]}
                events.append({"event": event_name, "data": payload})
            elif line == "":
                event_name = ""
    return events


def summarize(events: list[dict]) -> dict:
    """提炼可用于 diff 的关键行为特征。"""
    kinds: dict[str, int] = {}
    sqls: list[str] = []
    tool_names: list[str] = []
    answer = ""
    for ev in events:
        name = ev["event"]
        kinds[name] = kinds.get(name, 0) + 1
        d = ev["data"]
        if name == "tool_call":
            tool_names.append(d.get("tool", ""))
            if isinstance(d.get("input"), dict) and "sql" in d["input"]:
                sqls.append(d["input"]["sql"])
        elif name == "text":
            answer += d.get("text", "")
        elif name == "done":
            answer = d.get("message") or answer
    return {
        "event_counts": kinds,
        "sql": sqls,
        "tools": tool_names,
        "answer": answer,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/golden")
    ap.add_argument("--only", default=None, help="只跑名字匹配的用例")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 确认服务在线
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=10) as r:
            health = json.loads(r.read())
        print(f"服务在线: {health}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"服务未启动（{BASE}）: {exc}", file=sys.stderr)
        return 2

    index = []
    for case in CASES:
        if args.only and args.only not in case.name:
            continue
        session_id = f"golden-{case.name}"
        print(f"\n=== {case.name} [{case.path}] :: {case.question}")
        started = time.time()
        try:
            events = stream(case.question, case.db_id, session_id)
        except Exception as exc:  # noqa: BLE001 - 采集脚本，失败也要继续跑完其余用例
            print(f"    失败: {type(exc).__name__}: {exc}")
            index.append({**asdict(case), "error": f"{type(exc).__name__}: {exc}"})
            continue

        elapsed = round(time.time() - started, 1)
        summary = summarize(events)
        (out_dir / f"{case.name}.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8"
        )
        (out_dir / f"{case.name}.summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index.append(
            {
                **asdict(case),
                "elapsed_s": elapsed,
                "event_counts": summary["event_counts"],
                "n_sql": len(summary["sql"]),
                "tools": summary["tools"],
                "answer_chars": len(summary["answer"]),
            }
        )
        print(f"    {elapsed}s, {len(events)} 事件, {len(summary['sql'])} 条 SQL")
        print(f"    回答: {summary['answer'][:160].replace(chr(10), ' ')}")

    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n基线已写入 {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
