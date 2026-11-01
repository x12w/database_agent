# dbchat — 自然语言数据库查询 Agent

用中文提问，Agent 自主探索数据库、生成并执行 SQL、必要时计算与绘图，最后用自然语言回答。
支持 **MySQL / PostgreSQL / 达梦(Dameng)**，并预留了人大金仓、openGauss、OceanBase 的接入点。

> 当前状态：v4 重构已完成。旧实现的完整快照保存在 git tag `baseline`，
> 可随时 `git diff baseline` 对照。

## 桌面应用（Linux）

日常使用优先启动桌面应用，无需打开浏览器、手动启动服务或编辑 `.env`：

```bash
./run-desktop.sh
```

首次源码安装需要 Python 3.13、Node.js 22+，并先执行：
`uv venv --python 3.13 venv && uv pip install --python venv/bin/python -e .`。
启动脚本构建界面并打开 Electron 窗口，首次进入图形设置页。

- **模型服务**：协议、地址、模型名、密钥、输出上限、温度和重试，支持测试连接。
- **数据库连接**：添加、验证、编辑和删除 MySQL / PostgreSQL / 达梦连接。
- **运行参数**：各类超时、查询行数、迭代次数、抽样和缓存。
- **知识与显示**：选择本地知识文档、检索参数、SQL 与思考过程显示、本地存储参数。

点击“保存并应用”会重启本地引擎；已保存的会话保留。
固定的 `dbchat://app` 窗口地址避免引擎端口变更导致会话存储丢失。
API 仅绑定随机回环端口，令牌由桌面主进程生成和持有；渲染器隔离及导航限制遵循
[Electron 安全建议](https://www.electronjs.org/docs/latest/tutorial/security)。

桌面数据与原服务分开，默认在 `~/.config/dbchat/data`，可在“显示与存储”打开实际目录。
它不自动读取原 `.env` 或原 `var/`，请在设置页填写模型和数据库连接。
`preferences.json` 权限为 `0600`，数据目录为 `0700`；密钥不回传界面，
但目前以明文存入当前用户的本地文件，尚未接入系统钥匙串。
计算和绘图仍需要 Linux + `bubblewrap`，以及允许用户命名空间的内核。

### 构建独立应用包

使用 uv 管理的可迁移 CPython 3.13 环境：

```bash
npm --prefix desktop ci
npm --prefix desktop run pack   # desktop/release/linux-unpacked/
npm --prefix desktop run dist   # Linux tar.gz 分发包
```

应用包内置 Python、数据库驱动和绘图库，解压后运行 `start-dbchat.sh`。
本机源码构建后，可用 `venv/bin/python desktop/scripts/install-shortcut.py`
将应用加入当前用户的应用菜单（保留已有的不同配置入口）。
在常规 Linux 桌面系统运行内置 Electron；NixOS 启动脚本使用系统安装的 Electron。
包内运行依赖固定在 `desktop/requirements-runtime.txt`，不会复制 `.env`、数据库或开发虚拟环境。
独立包使用关键词知识检索，避免捆绑数 GB 的向量模型依赖。
当前只构建和验证 Linux x64；Windows/macOS 的打包及计算沙箱尚未实现。

桌面测试：`npm --prefix desktop test`；有图形会话时执行
`npm --prefix desktop run test:app`。NixOS 可用 `DBCHAT_ELECTRON` 指定系统 Electron。
测试以临时目录启动真实窗口，验证模型连接、GUI 保存、引擎重启、会话存储与密钥脱敏。

## 兼容的 Web 服务模式

需要 Python 3.13。计算与绘图还需要 **Linux + bubblewrap (`bwrap`)**，
且内核允许创建用户命名空间。隔离环境不可用时，这两项功能会明确失败，不会退回裸进程执行。

```bash
# 1. 配置模型服务与默认数据库
cp .env.example .env && $EDITOR .env

# 2. 启动（首次会自动创建虚拟环境并安装依赖）
./run.sh
```

访问 http://localhost:8000 。管理面板在同一服务下的 `/admin`。

启动前在 `.env` 设置 `AUTH_ADMIN_TOKEN`（必填）；可用
`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成随机令牌。
浏览器会弹出登录框，用户名为 `admin`，密码为管理员令牌。
如需普通查询角色，另外设置不同的 `AUTH_USER_TOKEN`，用户名为 `user`；
普通角色不能操作管理面板、修改连接或重建缓存。API 客户端也可使用
`Authorization: Bearer <token>`。未配置管理员令牌时接口返回 503；匿名请求返回 401。

默认仅监听 `127.0.0.1`。如需远程访问，应通过 HTTPS 反向代理提供服务，
避免明文传输登录凭据。升级已有安装时，请同时检查原 `.env` 中的 `HOST`。

未配置模型服务时，仍可提问“数据库有哪些表？”等纯结构问题：优先读取缓存，
首次连接则加载数据库结构，不调用模型或启动探索。当前范围内没有可见表时，
会提示检查表筛选、访问权限或建表情况。统计、查询业务数据和绘图等请求仍需配置模型。

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

审计回归测试在 `tests/unit/test_audit_regressions.py`，覆盖匿名/越权请求、
跨站请求、沙箱越界文件与网络访问、SQL 作用域及行数限制、会话历史保护、
RAG 数据库隔离和失败状态传播。前端请求取消竞态由 `web/test/requestRace.test.ts` 覆盖。

SQL 现在必须通过完整解析与只读检查；无法识别的 SQL、未确认只读的函数、
写入型 SELECT 和未授权 schema 会被拒绝，请将查询改写为支持的形式。
MySQL/PostgreSQL 连接默认启用只读事务，达梦在每次业务查询前开启
[只读事务](https://eco.dameng.com/document/dm/zh-cn/pm/management-affairs.html)并在结束后回滚。
PostgreSQL 当前限定业务 schema 为 `public`。连接账号仍应仅拥有所需表的 SELECT 权限，
避免使用 root、超级用户或拥有文件访问权限的账号。

查询流程和文档工具共享应用 RAG 实例；管理员知识使用按数据库隔离的关键词索引，
保存后即时生效，启动时从 SQLite 恢复，不会混入其他数据库的回答。

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

## 工具

| 脚本 | 用途 |
| --- | --- |
| `tools/capture_baseline.py` | 抓取一组标准问题的完整 SSE 事件流（`--path` 指定端点） |
| `tools/compare_runs.py` | 对比两次采集 —— 能同时读新旧两套事件契约 |
| `tools/browser_check.mjs` | 用 CDP 驱动真实浏览器走完一次提问，验证界面端到端可用 |
| `tools/probe_stream.mjs` | 在浏览器里直连 SSE，隔离"后端没发"与"前端没解析" |

`tests/golden/` 是重构前的行为基线，`tests/golden/README.md` 记录了它的覆盖范围
与已知局限（它是"旧行为"的记录，不是"正确行为"的标杆）。
