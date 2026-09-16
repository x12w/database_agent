# dbchat — 自然语言数据库查询 Agent

用中文提问，Agent 自主探索数据库、生成并执行 SQL、必要时计算与绘图，最后用自然语言回答。
支持 **MySQL / PostgreSQL / 达梦(Dameng)**，并预留了人大金仓、openGauss、OceanBase 的接入点。

> 当前状态：正在从 v3（单体 `app/` 目录）重构为 v4（`src/dbchat/` 分层包 + `web/` 前端）。
> 重构前的完整快照保存在 git tag `baseline`。

## 快速开始

```bash
# 1. 配置模型服务与默认数据库
cp .env.example .env && $EDITOR .env

# 2. 启动（首次会自动创建虚拟环境并安装依赖）
./run.sh
```

访问 http://localhost:8000 。管理面板在同一服务下的 `/admin`。

## 架构

依赖方向自上而下，禁止反向导入：

| 层 | 目录 | 职责 |
| --- | --- | --- |
| 领域 | `src/dbchat/domain/` | 纯数据模型，零 IO |
| 事件 | `src/dbchat/events/` | 内部事件与线上事件两层契约 |
| 数据库 | `src/dbchat/db/` | 连接、**方言**、内省、SQL 校验、执行 |
| 模型 | `src/dbchat/llm/` | 异步流式客户端与消息格式 |
| 工具 | `src/dbchat/tools/` | 工具注册表（定义唯一来源） |
| Agent | `src/dbchat/agent/` | 唯一的 agent 循环 + 各 agent 配置 |
| 检索 | `src/dbchat/rag/` | 文档索引与检索 |
| 存储 | `src/dbchat/storage/` | SQLite 会话、配置、澄清记录 |
| 用例 | `src/dbchat/usecases/` | 聊天 / 主动探索两条业务流程 |
| 接口 | `src/dbchat/api/` | FastAPI 路由与 SSE |

### 三条硬约束

1. **`db_type` 分支不得出现在 `db/dialects/` 之外。**
   所有方言差异收敛到 `Dialect` 子类。新增一个国产库 = 新建一个模块 + 注册一行。

2. **内部信息不得进入线上事件。**
   `AgentEvent`（内部，可含原始 SQL/工具名/行数据）只能经
   `events/project.py` 投影为 `ChatEvent`（线上）。原始 SQL、工具内部名、
   问题分类枚举不是 `ChatEvent` 的字段，想泄漏必须显式改契约。

3. **异步路径上不得有阻塞调用。**
   DB 驱动、向量编码、网络请求统一走 `anyio.to_thread.run_sync`。

## 开发

```bash
uv venv --python 3.13 venv          # 解释器固定 3.13：dmPython 只发布 cp313 ABI
uv pip install -e ".[dev]"
pytest tests/unit -q                 # 不依赖外部服务
pytest -m live_mysql -q              # 需要真实数据库
ruff check src tests
```

集成测试需要以下数据库在线（`docker start` 即可）：

| 容器 | 端口 | 用途 |
| --- | --- | --- |
| `mysql-training` | 3307 | 培训考试库 |
| `mysql-ecommerce` | 3308 | 电商库 |
| `postgres-hospital` | 5433 | 医院库 |
| `dameng-db` | 5237 | 达梦 |

## 版本管理说明

`data/` 与 `var/` 下的运行时状态不进版本控制，其中 `profiles.json` 含明文密码，
仓库里只保留脱敏的 `data/profiles.example.json`。
