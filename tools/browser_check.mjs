#!/usr/bin/env node
/**
 * 端到端界面验证 —— 用 CDP 驱动真实浏览器走完一次提问。
 *
 * 存在的理由：前端的类型检查、构建、夹具渲染冒烟都只能证明"组件能渲染"，
 * 证明不了"接上真实后端、发一个真问题、步骤流会正确长出来"。
 * 这是用户三个诉求里"界面"那一项唯一能算数的验证。
 *
 * 零依赖：Node 22+ 自带 WebSocket，直接讲 CDP。
 *
 * 用法：
 *   node tools/browser_check.mjs [baseUrl] [question]
 */

import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const BASE = process.argv[2] || "http://127.0.0.1:8000";
const QUESTION = process.argv[3] || "一共有多少名员工？";
const CHROME = process.env.CHROME || "/run/current-system/sw/bin/google-chrome";
const PORT = 9222;

let msgId = 0;
const pending = new Map();

function send(ws, method, params = {}) {
  const id = ++msgId;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`CDP 超时: ${method}`));
      }
    }, 30000);
  });
}

async function evaluate(ws, expression) {
  const r = await send(ws, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (r.exceptionDetails) throw new Error(`页面异常: ${r.exceptionDetails.text}`);
  return r.result?.value;
}

async function main() {
  const chrome = spawn(
    CHROME,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-sandbox",
      "--hide-scrollbars",
      `--remote-debugging-port=${PORT}`,
      "--window-size=1440,900",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  try {
    // 等 CDP 端口就绪
    let target = null;
    for (let i = 0; i < 40; i++) {
      await sleep(250);
      try {
        const res = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, {
          method: "PUT",
        });
        if (res.ok) {
          target = await res.json();
          break;
        }
      } catch {
        /* 还没起来 */
      }
    }
    if (!target) throw new Error("Chrome 调试端口未就绪");

    const ws = new WebSocket(target.webSocketDebuggerUrl);
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const { resolve, reject } = pending.get(m.id);
        pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      }
    });
    await new Promise((r, j) => {
      ws.addEventListener("open", r, { once: true });
      ws.addEventListener("error", j, { once: true });
    });

    await send(ws, "Page.enable");
    await send(ws, "Runtime.enable");

    const errors = [];
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
        errors.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
      }
      if (m.method === "Runtime.exceptionThrown") {
        errors.push(m.params.exceptionDetails.text);
      }
    });

    console.log(`打开 ${BASE} ...`);
    await send(ws, "Page.navigate", { url: BASE });
    await sleep(3500);

    const title = await evaluate(ws, "document.title");
    console.log(`页面标题: ${title}`);

    // 选库（默认已是第一个），填入问题并提交
    console.log(`提问: ${QUESTION}`);
    await evaluate(
      ws,
      `(() => {
        const ta = document.querySelector('textarea, input[type=text]');
        if (!ta) return 'no-input';
        const proto = ta.tagName === 'TEXTAREA'
          ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(ta, ${JSON.stringify(QUESTION)});
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        return 'ok';
      })()`,
    );
    await sleep(400);
    await evaluate(
      ws,
      `(() => {
        const btn = [...document.querySelectorAll('button')]
          .find(b => b.textContent.includes('发送'));
        if (!btn) return 'no-button';
        btn.click();
        return 'clicked';
      })()`,
    );

    // 轮询步骤流，记录它如何生长
    const snapshots = [];
    let lastText = "";
    let lastSteps = 0;
    let midShotTaken = false;
    for (let i = 0; i < 150; i++) {
      await sleep(2000);
      const text = await evaluate(ws, "document.body.innerText");
      if (text !== lastText) {
        lastText = text;
        const running = await evaluate(
          ws,
          `document.querySelectorAll('[class*=spin],[class*=running],[class*=progress]').length`,
        );
        snapshots.push({ t: i * 2, len: text.length, running });
      }
      // 运行中截一张 —— 这才是"步骤流是否在长"的证据
      if (!midShotTaken && i >= 3) {
        const mid = await send(ws, "Page.captureScreenshot", { format: "png" });
        writeFileSync("/tmp/browser_check_running.png", Buffer.from(mid.data, "base64"));
        midShotTaken = true;
      }
      // 完成判据必须是"收尾完成"而非"出现过答案" ——
      // `.answer` 在流式一开始就存在，用它判断会提前退出，
      // 截到一张还在跑、步骤流尚未展开的图（我第一版就踩了这个坑）。
      // 真正的收尾标志：打字光标消失 + 步骤流收拢 + 统计行出现。
      const done = await evaluate(
        ws,
        `(() => {
          const caret = document.querySelector('.caret');
          const stats = document.querySelector('.stats');
          const steps = document.querySelectorAll('[class*=step],[class*=Step]').length;
          return { settled: !caret && !!stats, steps };
        })()`,
      );
      lastSteps = done.steps;
      if (done.settled) break;
    }

    const finalText = await evaluate(ws, "document.body.innerText");
    const messagesHtml = await evaluate(
      ws,
      `(document.querySelector('.messages') || document.body).innerHTML`,
    );
    writeFileSync("/tmp/browser_check_dom.txt", finalText, "utf8");
    writeFileSync("/tmp/browser_check_messages.html", messagesHtml, "utf8");

    // 截图（整页）
    const shot = await send(ws, "Page.captureScreenshot", { format: "png" });
    writeFileSync("/tmp/browser_check.png", Buffer.from(shot.data, "base64"));

    console.log(`\n步骤流演进（共 ${snapshots.length} 次变化）:`);
    for (const s of snapshots) console.log(`  ${String(s.t).padStart(4)}s  ${s.len} 字符`);

    console.log("\n=== 最终页面文本 ===");
    console.log(finalText.slice(0, 2200));
    console.log("\n=== 控制台错误 ===");
    console.log(errors.length ? errors.slice(0, 8).join("\n") : "无 ✓");
    console.log("\n截图: /tmp/browser_check.png");
    console.log("DOM : /tmp/browser_check_dom.txt");
    ws.close();
  } finally {
    chrome.kill("SIGKILL");
  }
}

main().catch((e) => {
  console.error("失败:", e.message);
  process.exit(1);
});
