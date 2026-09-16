"""问题分类 —— 关键词优先，模型兜底。

修掉一个**静默失效**的缺陷：历史实现 `app/agent/router.py:83` 在已经运行的
事件循环里调 `asyncio.get_event_loop().run_until_complete(...)`，必然抛
`RuntimeError`，紧跟着的 `except Exception: pass`（`:87`）把它吞掉 ——
"LLM 分类"这条分支在聊天路径里**从未真正执行过**。

这里的 `classify()` 是协程，由管道 `await`；模型分类失败会记 warning
（`LLMClient.classify` 内部已经降级为 `AMBIGUOUS`），而不是静默吞掉。

关键词表与"含糊时倾向数据问题"的默认偏置都保留：默认去查数据库比默认
答"我不知道"对用户更有用。
"""

from __future__ import annotations

import logging

from dbchat.agent.structure import is_structure_question
from dbchat.domain.errors import LlmError
from dbchat.llm.client import (
    CLASS_AMBIGUOUS,
    CLASS_DATA_QUESTION,
    CLASS_DOC_QUESTION,
    LLMClient,
)

__all__ = [
    "CLASS_AMBIGUOUS",
    "CLASS_DATA_QUESTION",
    "CLASS_DOC_QUESTION",
    "DATA_KEYWORDS",
    "DOC_KEYWORDS",
    "classify",
    "is_existence_question",
]

logger = logging.getLogger(__name__)

#: 强烈指向"数据问题"的关键词。
DATA_KEYWORDS: tuple[str, ...] = (
    # 疑问词
    "谁", "哪些人", "多少人", "有几个", "多少", "几次",
    # 动作词
    "查询", "统计", "列出", "显示", "查一下", "帮我找", "查",
    # 记录类名词
    "名单", "记录", "成绩", "分数",
    # 状态/条件
    "参加", "缺席", "缺考", "不合格", "合格", "通过", "未通过",
    # 具体性
    "具体", "姓名", "工号", "电话", "邮件",
    # 存在性
    "有没有", "是否存在", "是否有人",
    # 指代
    "哪个", "哪一", "某",
    # 时间/数据
    "实际", "编号", "日期", "年份",
    # 部门/角色
    "部门有", "参加了", "报名了",
)

#: 指向"文档/结构问题"的关键词。
DOC_KEYWORDS: tuple[str, ...] = (
    "表结构", "字段", "列", "类型", "约束", "外键", "索引", "主键",
    "数据库结构", "有几张表", "什么意思", "取值范围", "枚举",
    "允许的值", "默认值", "定义", "关系", "架构", "ER",
    "check", "哪些表", "表名", "建表",
    "schema", "结构是", "什么结构",
)

#: 存在性问句的模式。
_EXISTENCE_PATTERNS: tuple[str, ...] = (
    "有没有", "是否有", "存在吗", "有吗", "有...吗",
    "哪些", "谁", "什么人", "哪一", "哪些人",
    "缺考", "缺席", "不合格", "没过", "挂了",
)

def is_existence_question(question: str) -> bool:
    """是否在问"有没有"（存在性）。"""
    return any(pattern in question for pattern in _EXISTENCE_PATTERNS)


async def classify(question: str, *, llm: LLMClient) -> str:
    """把问题分成 `DATA_QUESTION` / `DOC_QUESTION` / `AMBIGUOUS`。

    Raises:
        不抛异常 —— 分类是尽力而为，失败一律退回 `AMBIGUOUS`，由调用方按
        "倾向数据问题"处理。
    """
    if not question or not question.strip():
        return CLASS_AMBIGUOUS

    lowered = question.strip().lower()
    data_score = sum(1 for keyword in DATA_KEYWORDS if keyword in lowered)
    doc_score = sum(1 for keyword in DOC_KEYWORDS if keyword in lowered)

    if data_score >= 2 and doc_score == 0:
        return CLASS_DATA_QUESTION
    if doc_score >= 2 and data_score == 0:
        return CLASS_DOC_QUESTION
    if data_score > doc_score:
        return CLASS_DATA_QUESTION
    if doc_score > data_score:
        return CLASS_DOC_QUESTION

    if is_existence_question(lowered):
        return CLASS_DATA_QUESTION
    if is_structure_question(lowered):
        return CLASS_DOC_QUESTION

    # 关键词分不出胜负 —— 交给模型（这一步以前永远走不到）。
    try:
        result = await llm.classify(question)
    except LlmError as exc:
        logger.warning(
            "模型分类失败，按关键词默认倾向处理: %s", exc.detail or exc.user_message
        )
        return CLASS_DATA_QUESTION

    if result != CLASS_AMBIGUOUS:
        logger.debug("模型分类结果: %s", result)
        return result

    # 默认倾向数据问题：含糊时去查库，比答"我不知道"有用。
    return CLASS_DATA_QUESTION
