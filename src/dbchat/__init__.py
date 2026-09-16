"""dbchat —— 自然语言数据库查询 Agent。

分层（依赖方向自上而下，禁止反向导入）：

    domain      纯数据模型，零 IO
    events      事件契约（内部 AgentEvent / 线上 ChatEvent）
    db          连接、方言、内省、校验、执行
    llm         模型客户端与消息格式
    tools       工具注册表与实现
    agent       唯一的 agent 循环与各 agent 配置
    rag         文档检索
    storage     SQLite 持久化
    usecases    聊天 / 探索两套业务流程
    api         FastAPI 路由与 SSE
"""

__version__ = "0.1.0"
