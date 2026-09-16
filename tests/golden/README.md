# 行为基线（golden transcripts）

用**重构前**的实现（tag `baseline`）对 9 个代表性用例抓取的完整 SSE 事件流。
重构的每一阶段都拿它做对比 —— 否则"重构没改变行为"只能靠感觉。

抓取时间：2026-09-16
模型：deepseek-v4-flash:0731（ollama.com，OpenAI 兼容协议）
用法：`venv/bin/python tools/capture_baseline.py --out tests/golden`
（需要旧实现在 8000 端口运行）

## 用例覆盖

| 用例 | 覆盖的代码路径 |
| --- | --- |
| simple_count | 简单查询，跳过分析阶段 |
| join_detail | 多表连接 |
| aggregate | GROUP BY 聚合 |
| chart | 计算 + 可视化 agent |
| structure | 结构类问题快速通道（不走 LLM） |
| temporal | 相对时间预处理（"今年"） |
| empty_result | 空结果触发的重试判定 |
| pg_hospital | PostgreSQL 方言 |
| dm_local | 达梦方言（命中缓存快路径） |

## 基线暴露的缺陷（重构必须消除）

1. **`db_queries` 恒为 0**（全部 9 个用例）。
   `orchestrator.py:354` 判断 `event["type"] == "tool_use"`，
   而 agent 层投递的是 `tool_call` —— 条件永不成立。
   界面向用户展示的"共 N 次查询"一直是错的。

2. **重试循环分不清"没查"与"查了为空"**。
   `empty_result` 用例：一个简单的是非问题烧掉 10 条 SQL、4 轮重试、308 秒。
   `_evaluate_results` 只看结果集是否为空，不看本轮是否真的查询过。

## 已知局限

- **基线本身不是"正确行为"的标杆**，只是"旧行为"的记录。
  上表两个缺陷说明旧实现在这些路径上是错的。对比时应关注
  "查询语义是否等价"，而不是"重试次数是否一致"。
- `chart` 用例的 `![...]()` 图片链接为空，是旧实现的缺陷，不是抓取丢失。
- 单次抓取约 24 分钟（9 个用例），主要耗时在重试循环与模型延迟。
