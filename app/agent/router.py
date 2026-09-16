"""Classification router — determines if a question is about data or documentation.

This is the critical component for correctness. It classifies questions before
they reach the LLM, and can function without an API key using keyword matching.
"""

from app.llm.claude_client import llm_client


# Keywords that strongly indicate a data question
DATA_KEYWORDS = [
    # Chinese question words about data
    "谁", "哪些人", "多少人", "有几个", "多少", "几次",
    # Action words implying data lookup
    "查询", "统计", "列出", "显示", "查一下", "帮我找", "查",
    # Nouns referring to actual records
    "名单", "记录", "成绩", "分数",
    # Status/condition questions
    "参加", "缺席", "缺考", "不合格", "合格", "通过", "未通过",
    # Concrete specificity
    "具体", "姓名", "工号", "电话", "邮件",
    # Existence questions
    "有没有", "是否存在", "是否有人",
    # Personal names (check dynamically)
    "哪个", "哪一", "某",
    # Time/data specific
    "实际", "编号", "日期", "年份",
    # Department/role queries about people
    "部门有", "参加了", "报名了",
]

# Keywords indicating a documentation/structure question
DOC_KEYWORDS = [
    "表结构", "字段", "列", "类型", "约束", "外键", "索引", "主键",
    "数据库结构", "有几张表", "什么意思", "取值范围", "枚举",
    "允许的值", "默认值", "定义", "关系", "架构", "ER",
    "check", "哪些表", "表名", "建表",
    "schema", "结构是", "什么结构",
]


def classify(question: str) -> str:
    """
    Classify a user question as DATA_QUESTION, DOC_QUESTION, or AMBIGUOUS.

    Uses keyword-based matching first (fast, no API call needed).
    Falls back to API-based classification when available and keywords are ambiguous.

    The default bias is toward DATA_QUESTION — when in doubt, query the database.
    """
    if not question or not question.strip():
        return "AMBIGUOUS"

    q = question.strip().lower()

    # Count keyword matches
    data_score = sum(1 for kw in DATA_KEYWORDS if kw in q)
    doc_score = sum(1 for kw in DOC_KEYWORDS if kw in q)

    # Strong signal thresholds
    if data_score >= 2 and doc_score == 0:
        return "DATA_QUESTION"
    if doc_score >= 2 and data_score == 0:
        return "DOC_QUESTION"

    # Single keyword matches
    if data_score > doc_score:
        return "DATA_QUESTION"
    if doc_score > data_score:
        return "DOC_QUESTION"

    # Tie or zero for both — check for patterns
    if _is_existence_question(q):
        return "DATA_QUESTION"
    if _is_structure_question(q):
        return "DOC_QUESTION"

    # Try API classification if available
    if llm_client.available:
        result = "AMBIGUOUS"  # default
        try:
            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                llm_client.classify_quick(question)
            )
        except Exception:
            pass
        if result != "AMBIGUOUS":
            return result

    # Default: DATA_QUESTION (safety — query the DB when uncertain)
    return "DATA_QUESTION"


def _is_existence_question(q: str) -> bool:
    """Check if this is an existence/presence question about data."""
    patterns = [
        "有没有", "是否有", "存在吗", "有吗", "有...吗",
        "哪些", "谁", "什么人", "哪一", "哪些人",
        "缺考", "缺席", "不合格", "没过", "挂了",
    ]
    return any(p in q for p in patterns)


def _is_structure_question(q: str) -> bool:
    """Check if this is clearly about database structure."""
    patterns = [
        "什么表", "哪些表", "表结构", "字段是", "列是",
        "主键", "外键", "索引", "约束", "架构",
        "介绍一下", "包含哪些", "有哪些表", "表有哪些",
        "设计", "结构", "模式", "关系",
    ]
    return any(p in q for p in patterns)
