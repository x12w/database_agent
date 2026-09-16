# dbchat 前端

自然语言数据库查询助手的前端（Vue 3 + Vite + TypeScript）。

**无 CDN 依赖**：markdown 渲染器（`marked`）与 sanitizer（`dompurify`）是本地
npm 依赖，字体走系统字体栈。离线机器上 `npm install` 之后即可开发与构建。

## 命令

```bash
npm install          # 安装依赖
npm run dev          # 开发服务器 http://localhost:5173（/api 代理到 :8000）
npm run build        # 类型检查 + 生产构建 → web/dist
npm run preview      # 预览构建产物
npm run typecheck    # 只做类型检查（vue-tsc --noEmit）
npm test             # 归约层单元测试（node --test，无需浏览器）
npm run smoke        # 渲染冒烟测试（Node 里 SSR 真实组件，检查渲染出的 HTML）
npm run check:types  # 事件契约漂移检查（读后端 pydantic 源码）
```

`npm run build` 会先跑 `vue-tsc --noEmit`，类型不过就不产出构建物。

## 路由

| 路径 | 内容 |
| --- | --- |
| `/` | 查询界面（步骤流 + 回答 + 会话历史 + 表筛选） |
| `/admin` | 管理面板（待回答 / 已回答 / 数据库探索） |
| `/dev` | **开发夹具**：无后端预览步骤流 |

三条路径都需要服务端回落到 `index.html`（构建产物是单页应用）。

### 无后端预览：`/dev`

`/dev` 把合成的事件序列喂进与线上**完全相同**的归约函数（`src/core/run.ts`
的 `applyEvent`）与组件（`RunView` / `StepStream` / `StepRow`），因此看到的就是
后端接上以后的真实渲染。页面顶部可以：

- 选择场景：长任务（含重试与图表）/ 需要用户确认 / 失败（可重试）/ 短任务；
- 调倍速（1×–16×）、播放 / 暂停 / 单步 / 跳到结尾 / 重置；
- 观察运行中的行：转圈的字形、滴答的耗时、流式的暗淡思考文本。

夹具定义在 `src/dev/fixture.ts`，事件字段与 `src/dbchat/events/chat.py` 一一对应。

也可以用 URL 直接驱动，便于截图或自动化：

```
/dev?fixture=long&speed=8&play=1      # 播放（8 倍速）
/dev?fixture=failed&jump=1            # 直接跳到结尾
/dev                                  # 场景：long / question / failed / fast
```

## 目录结构

```
src/
  api/          # 后端路径与请求（endpoints.ts 是路径的唯一来源）
    endpoints.ts   所有 /api 路径
    client.ts      fetch 封装 + 中文错误文案
    sse.ts         自写的 SSE 分帧读取器（不用 EventSource：它会自动重连）
    chat.ts / admin.ts / databases.ts
  core/run.ts   # 事件 → 步骤流 的纯函数归约（不 import Vue，可被 node 直接测试）
  types/
    events.ts   # 事件契约的 TS 镜像（手写，由 scripts/check-types.mjs 校验漂移）
    api.ts      # HTTP 响应形状
  stores/       # chat（会话/流）/ databases（连接状态）
  components/   # 步骤流与各个弹层
  views/        # ChatView / AdminView / FixtureView
  dev/fixture.ts
scripts/check-types.mjs   # 契约漂移检查
test/eventStore.test.ts   # 归约层测试
```

## 事件契约与漂移检查

线上事件只有 `ChatEvent` 一种（`src/dbchat/events/chat.py`）。前端在
`src/types/events.ts` 里手工镜像它，并且：

- `src/core/run.ts` 的 `applyEvent` 对 `type` 做 **穷尽 switch**，`default`
  分支调用 `assertNever`。后端新增事件而前端没处理时，`ev` 无法收窄成
  `never`，**编译失败**。
- `npm run check:types` 用纯文本解析读 pydantic 模型，比对事件清单、字段名、
  枚举成员、中文标签表与 `RETRYABLE_ERRORS`，有漂移时退出码 1。
  不需要 Python 工具链，也不引入 codegen 依赖。

```bash
$ npm run check:types
✔ 事件契约无漂移（13 种事件，4 个模型）
```

## 界面上刻意不出现的东西

契约里没有的字段，前端不会去"猜"一个显示方式。具体地：

| 不显示 | 机制 |
| --- | --- |
| 原始 SQL | 只可能存在于 `activity.finished.sql_preview`，渲染在默认收起的「查看 SQL」折叠块里；后端默认不发这个字段 |
| 原始报错 | 只在 `error_detail` 折叠块里，`run.failed` 默认只显示友好 `message` |
| 阶段/动作的内部枚举值 | 只用 `label` 字段，缺失时回落到 `STAGE_LABELS` / `ACTIVITY_LABELS` 的中文；`Stage` / `ActivityKind` 的原始值不进 DOM |
| 轮次上限 | `stage.retried.max_attempts` 不渲染；重试提示只用 `reason`（+ 第几次尝试） |
| 工具名 / agent 代号 / 分类枚举 | 契约里不存在，`ChatEvent` 的字段是唯一数据来源 |

单元测试覆盖了这些边界（`test/eventStore.test.ts` 里的「不泄漏」用例）。

## 流式渲染与 XSS

回答与思考文本在流式过程中一律走 Vue 插值（`{{ }}`，自动转义），**只有**
回答结束之后才交给 `marked` 渲染，且渲染结果必须过 `DOMPurify`。
旧实现 `app/static/app.js:684` 的 `cd.innerHTML += text` 在流式过程中直接
注入未转义的模型输出，这里从结构上不会重现。
