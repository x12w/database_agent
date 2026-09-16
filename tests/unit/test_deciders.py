"""结果评估的行为判定 —— 空结果、重试理由、问题形状。

两个缺陷 + 一条不能回归的护栏，都是从 `tests/golden-new/*.jsonl` 的真实
抓取里读出来的，测试里的字符串也直接取自那些抓取，不是编造的：

* **空结果不认"没有"**（`empty_result.jsonl`）："有没有叫张三丰的员工参加过
  考试？"是非问句，0 行就是答案；旧评估器无条件要求重试，5 轮烧掉 173 秒，
  第 4 轮模型已经开始瞎跑 `SELECT *`（返回 15 名员工）。
* **重试判定自相矛盾**（`chart.jsonl` 第 2 轮）：模型回了
  `RETRY: 查询已返回各考试平均分` —— 重试前缀 + 成功陈述，旧解析只看前缀，
  于是白烧一整轮（约 50 秒）。
* **`NO_QUERY_HINT` 不能回归**（`pg_hospital.jsonl` 第 2 轮）："没查" 与
  "查了为空" 必须继续分开，这条把该用例从 241 秒压到 50 秒。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from dbchat.agent.deciders import (
    EMPTY_RESULT_HINT,
    NO_MATCH_REASON,
    NO_QUERY_HINT,
    evaluate_results,
    is_boolean_question,
)

# ------------------------------------------------------------------ 桩


class _LLM:
    """按脚本回复的假客户端（`complete` 是评估器唯一的调用面）。"""

    def __init__(self, replies: Sequence[str] = (), *, available: bool = True) -> None:
        self.replies = list(replies)
        self.available = available
        self.prompts: list[str] = []

    async def complete(self, system: str, messages: Sequence[Any], **kwargs: Any) -> str:
        self.prompts.append("\n".join(_text_of(message) for message in messages))
        return self.replies.pop(0) if self.replies else ""


def _text_of(message: Any) -> str:
    """评估器发的是 `{"role": ..., "content": ...}` 字典（不是 `Message`）。"""
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "text", ""))


class _Result:
    """最小 `QueryResult` 替身（评估只看行数/列名/样例行/是否被截断）。"""

    def __init__(self, rows: int, truncated: bool = False) -> None:
        self.row_count = rows
        self.columns = ["c"]
        self.rows = [{"c": index} for index in range(rows)]
        self.truncated = truncated


#: 抓取原文：`empty_result` 用例的用户问题。
EXISTENCE_QUESTION = "有没有叫张三丰的员工参加过考试？"
#: 抓取原文：`aggregate` 用例的用户问题（计数/汇总类，空结果必须重试）。
COUNT_QUESTION = "每个培训课程有多少人报名？按人数从多到少排列"
#: 抓取原文：`chart` 用例第 2 轮评估器的完整回复。
CHART_RETRY_VERDICT = "RETRY: 查询已返回各考试平均分"


# ------------------------------------------------------------------ 缺陷 A：是非问句的空结果


def test_boolean_question_shape_is_explicit() -> None:
    """问题形状由关键词判定，不看模型的脸色。"""
    assert is_boolean_question(EXISTENCE_QUESTION) is True
    assert is_boolean_question("是否有人缺考") is True
    assert is_boolean_question("张三丰参加过考试吗？") is True
    # 计数/列表/汇总类 —— 0 行说明真没查到，不能因为句尾有个"吗"就收下
    assert is_boolean_question(COUNT_QUESTION) is False
    assert is_boolean_question("能帮我查一下有多少人吗？") is False
    assert is_boolean_question("一共有多少名员工？") is False
    assert is_boolean_question("这个数据库有哪些表？") is False
    assert is_boolean_question("") is False


@pytest.mark.asyncio
async def test_existence_question_with_empty_result_is_satisfied() -> None:
    """缺陷 A：是非问句查到 0 行就是答案"没有"，不该再烧一轮。"""
    llm = _LLM()
    outcome = await evaluate_results([], EXISTENCE_QUESTION, llm=llm, ran_any_query=True)

    assert outcome.satisfactory is True
    assert outcome.ran_query is True
    assert outcome.reason == NO_MATCH_REASON
    # 理由必须说清"没有匹配的数据"，而不是"查询失败"
    assert "没有匹配的数据" in outcome.reason
    assert "失败" not in outcome.reason
    # 空结果是确定性的结论，不该问模型
    assert llm.prompts == []


@pytest.mark.asyncio
async def test_count_question_with_empty_result_still_retries() -> None:
    """缺陷 A 的护栏：该有数据的计数问题返回 0 行仍是失败。"""
    llm = _LLM()
    outcome = await evaluate_results([], COUNT_QUESTION, llm=llm, ran_any_query=True)

    assert outcome.satisfactory is False
    assert outcome.hint == EMPTY_RESULT_HINT
    assert outcome.reason != NO_MATCH_REASON
    assert llm.prompts == []


@pytest.mark.asyncio
async def test_zero_row_result_object_behaves_like_empty_list() -> None:
    """`QueryResult(row_count=0)` 与空列表走同一条分支。"""
    llm = _LLM()
    outcome = await evaluate_results([_Result(0)], COUNT_QUESTION, llm=llm, ran_any_query=True)
    assert outcome.satisfactory is False
    assert outcome.hint == EMPTY_RESULT_HINT


# ------------------------------------------------------------------ 缺陷 B：重试判定与理由


@pytest.mark.asyncio
async def test_success_statement_with_retry_prefix_is_not_a_retry() -> None:
    """缺陷 B：`chart` 第 2 轮的原文 —— 前缀是 RETRY，内容却是"已经成功"。"""
    llm = _LLM([CHART_RETRY_VERDICT])
    outcome = await evaluate_results(
        [_Result(7)], "把各门考试的平均分画成柱状图", llm=llm, ran_any_query=True
    )

    assert outcome.satisfactory is True
    assert outcome.hint == ""


@pytest.mark.asyncio
async def test_hint_without_any_change_is_not_a_retry() -> None:
    """只描述现状、没给出改动的"建议"不构成重试理由。"""
    llm = _LLM(["RETRY: 返回的结果里没有张三丰"])
    outcome = await evaluate_results([_Result(1)], COUNT_QUESTION, llm=llm, ran_any_query=True)
    assert outcome.satisfactory is True


@pytest.mark.asyncio
async def test_bare_retry_without_hint_is_not_a_retry() -> None:
    """只有 `RETRY` 两个字：没有可执行信息，重试只会重复同一件事。"""
    llm = _LLM(["RETRY:"])
    outcome = await evaluate_results([_Result(1)], COUNT_QUESTION, llm=llm, ran_any_query=True)
    assert outcome.satisfactory is True


@pytest.mark.asyncio
async def test_actionable_retry_hint_is_kept() -> None:
    """真的给出了改动的建议必须保留（护栏：别把重试一并干掉）。"""
    hint = "请改用 exam_result 表按 exam_id 分组，并加上 WHERE score IS NOT NULL"
    llm = _LLM([f"RETRY: {hint}"])
    outcome = await evaluate_results([_Result(1)], COUNT_QUESTION, llm=llm, ran_any_query=True)

    assert outcome.satisfactory is False
    assert outcome.hint == hint


@pytest.mark.asyncio
async def test_json_verdict_is_parsed() -> None:
    """首选协议是 JSON —— 两种 verdict 都要认。"""
    llm = _LLM(['{"satisfied": false, "reason": "结果只覆盖一个部门", '
                '"hint": "请按部门分组统计全部部门"}'])
    outcome = await evaluate_results([_Result(4)], COUNT_QUESTION, llm=llm, ran_any_query=True)
    assert outcome.satisfactory is False
    assert outcome.hint == "请按部门分组统计全部部门"
    assert outcome.reason == "结果只覆盖一个部门"

    llm = _LLM(['```json\n{"satisfied": true, "reason": "已覆盖全部考试"}\n```'])
    outcome = await evaluate_results([_Result(7)], "把各门考试的平均分画成柱状图", llm=llm,
                                     ran_any_query=True)
    assert outcome.satisfactory is True
    assert "已覆盖全部考试" in outcome.reason


@pytest.mark.asyncio
async def test_truncated_json_verdict_is_recovered() -> None:
    """被 token 上限截断的 JSON 不能当成"答非所问"。

    截断一律退到"有数据即充分"会把**真正的重试判定静默丢掉** ——
    实测中文 reason+hint 顶满 200 token 时就会出现这种回复。
    """
    truncated = '{"satisfied": false, "reason": "结果只覆盖一个部门", "hint": "请按部门分组'
    llm = _LLM([truncated])
    outcome = await evaluate_results([_Result(4)], COUNT_QUESTION, llm=llm, ran_any_query=True)

    assert outcome.satisfactory is False
    assert outcome.hint == "请按部门分组"
    assert outcome.reason == "结果只覆盖一个部门"


@pytest.mark.asyncio
async def test_truncated_json_without_actionable_hint_is_satisfied() -> None:
    """截断 + 建议不可执行 —— 仍然按已充分处理（两道防线各自生效）。"""
    llm = _LLM(['{"satisfied": false, "reason": "查询已返回各考试平均分", "hint": "'])
    outcome = await evaluate_results([_Result(7)], COUNT_QUESTION, llm=llm, ran_any_query=True)
    assert outcome.satisfactory is True


@pytest.mark.asyncio
async def test_evaluator_prompt_states_question_type_and_json_shape() -> None:
    """提示词要告诉模型问题类型（是非问句的答案可以是「没有」）与回复形状。"""
    llm = _LLM(['{"satisfied": true, "reason": "ok"}'])
    await evaluate_results([_Result(2)], EXISTENCE_QUESTION, llm=llm, ran_any_query=True)
    assert "是非/存在性" in llm.prompts[0]
    assert '"satisfied"' in llm.prompts[0]

    llm = _LLM(['{"satisfied": true, "reason": "ok"}'])
    await evaluate_results([_Result(2)], COUNT_QUESTION, llm=llm, ran_any_query=True)
    assert "数据/统计" in llm.prompts[0]


@pytest.mark.asyncio
async def test_unparseable_reply_falls_back_to_satisfied() -> None:
    """答非所问：有数据就算充分（历史行为，保持不回归）。"""
    llm = _LLM(["我觉得还行"])
    outcome = await evaluate_results([_Result(3)], COUNT_QUESTION, llm=llm, ran_any_query=True)
    assert outcome.satisfactory is True


# ------------------------------------------------------------------ NO_QUERY_HINT 不能回归


@pytest.mark.asyncio
async def test_no_query_still_produces_no_query_hint() -> None:
    """`pg_hospital` 第 2 轮的修复必须原样保留：没查过 → NO_QUERY_HINT。

    即使问题本身是是非问句也一样 —— "没查" 与 "查了为空" 是两件事。
    """
    for question in (COUNT_QUESTION, EXISTENCE_QUESTION):
        llm = _LLM()
        outcome = await evaluate_results([], question, llm=llm, ran_any_query=False)

        assert outcome.satisfactory is False
        assert outcome.ran_query is False
        assert outcome.hint == NO_QUERY_HINT
        assert "还没有执行任何查询" in outcome.hint
        assert outcome.reason == "本轮没有执行任何查询"


@pytest.mark.asyncio
async def test_llm_unavailable_accepts_existing_rows() -> None:
    """模型不可用时退回行数启发式（非空即充分）。"""
    llm = _LLM(available=False)
    outcome = await evaluate_results([_Result(3)], COUNT_QUESTION, llm=llm, ran_any_query=True)
    assert outcome.satisfactory is True
    assert "3" in outcome.reason


# ---------------------------------------------------------- 缺陷 5：样例条数 ≠ 结果条数


@pytest.mark.asyncio
async def test_small_result_is_given_in_full_not_as_a_sample() -> None:
    """缺陷 5：结果不大时**整份给出**，而不是给 3 行样例。

    只要出现截断，小模型就会怀疑"是不是没列全"，判结果不完整并重试
    （实测 pg_hospital 因此一度 4 重试 / 273s）。歧义只在截断时产生，
    所以 20 行以内直接全给 —— "列出 10 张表"这类问题因此再无歧义。
    """
    from dbchat.agent.deciders import _summarize

    text = "\n".join(_summarize([_Result(10)]))

    assert "共 10 行" in text, "总行数仍要出现"
    assert "全部结果如下" in text, "10 行应当整份给出"
    assert "仅展示前" not in text, "不该出现截断措辞"
    # 10 行全都出现在提示里
    assert text.count("{'c':") == 10


@pytest.mark.asyncio
async def test_large_result_is_truncated_and_says_so() -> None:
    """超过阈值的才截断，且必须写明完整性以总行为准。"""
    from dbchat.agent.deciders import _EVAL_SAMPLE_ROWS, _summarize

    rows = _EVAL_SAMPLE_ROWS + 30
    text = "\n".join(_summarize([_Result(rows)]))

    assert "全部结果如下" not in text
    assert f"仅展示前 {_EVAL_SAMPLE_ROWS} 行" in text
    assert f"共 {rows} 行" in text


@pytest.mark.asyncio
async def test_truncated_result_is_never_labelled_as_complete() -> None:
    """被行数上限截断的结果不能标"全部结果如下" —— 那是在撒谎。"""
    from dbchat.agent.deciders import _summarize

    # rows 只有 5 条、但 row_count 是 200 且标记被截断
    result = _Result(5, truncated=True)
    result.row_count = 200
    text = "\n".join(_summarize([result]))

    assert "全部结果如下" not in text
    assert "被行数上限截断" in text
    assert "实际行数可能更多" in text
