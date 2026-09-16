#!/usr/bin/env node
/** 在浏览器里直连 SSE，打印收到的事件名序列 —— 隔离前后端。 */
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const BASE = process.argv[2] || "http://127.0.0.1:8000";
const CHROME = "/run/current-system/sw/bin/google-chrome";
const PORT = 9223;
let msgId = 0;
const pending = new Map();
function send(ws, method, params = {}) {
  const id = ++msgId;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => {
    pending.set(id, { res, rej });
    setTimeout(() => pending.has(id) && (pending.delete(id), rej(new Error(method))), 120000);
  });
}
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-sandbox",
  `--remote-debugging-port=${PORT}`, "about:blank"], { stdio: "ignore" });
try {
  let t = null;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" }); if (r.ok) { t = await r.json(); break; } } catch {}
  }
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); }
  });
  await new Promise((r, j) => { ws.addEventListener("open", r, { once: true }); ws.addEventListener("error", j, { once: true }); });
  await send(ws, "Runtime.enable");
  await send(ws, "Page.enable");
  // 必须先导航到应用自身的源 —— 停在 about:blank 时 fetch 是跨域请求，
  // 会被静默拦掉，探针会误报"后端没发事件"。
  await send(ws, "Page.navigate", { url: BASE });
  await sleep(3000);

  const expr = `(async () => {
    const url = ${JSON.stringify(BASE)} + '/api/chat/stream?message=' +
      encodeURIComponent('一共有多少名员工？') + '&db_id=1da18658&session_id=rawprobe';
    const res = await fetch(url);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    const names = [];
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\\n\\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of frame.split('\\n')) {
          if (line.startsWith('event: ')) names.push(line.slice(7));
        }
      }
    }
    return { names, closed: true };
  })()`;
  const r = await send(ws, "Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  const got = r.result.value?.names || [];
  const counts = {};
  for (const n of got) counts[n] = (counts[n] || 0) + 1;
  console.log("浏览器直连收到的事件:", JSON.stringify(counts, null, 0));
  console.log("是否收到 run.finished:", got.includes("run.finished") ? "是 ✓" : "否 ✗");
  ws.close();
} finally { chrome.kill("SIGKILL"); }
