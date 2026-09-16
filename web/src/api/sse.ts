/**
 * SSE 读取器，基于 fetch + ReadableStream。
 *
 * 为什么不用 `EventSource`：它会在流正常结束/断开后**自动重连**，旧实现的
 * 重连循环正是由此而来；而且它无法设置请求头、无法区分"结束"与"断开"。
 * 这里自己解析分帧，重连完全由调用方决定（本应用：不重连）。
 */

export interface SseMessage {
  event: string
  data: string
}

export interface SseHandlers {
  onMessage: (message: SseMessage) => void
  /** 收到注释帧（心跳 `: ping`）。长耗时阶段用它判断连接是否还活着。 */
  onHeartbeat?: () => void
  /** 流正常结束（服务端关闭）时调用。 */
  onClose?: () => void
  /** 网络/解析失败时调用；主动 abort 不算错误。 */
  onError?: (error: unknown) => void
}

/** 解析一个 SSE 帧（注释帧返回 null）。 */
export function parseSseBlock(block: string): SseMessage | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const rawLine of block.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (line === '' || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
  }
  if (dataLines.length === 0) return null
  return { event, data: dataLines.join('\n') }
}

/** 把任意流切成一帧一帧。注释帧（心跳）走 `onHeartbeat`。 */
export async function* readSseFrames(
  body: ReadableStream<Uint8Array>,
  onHeartbeat?: () => void,
): AsyncGenerator<SseMessage, void, void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let index = buffer.indexOf('\n\n')
      while (index !== -1) {
        const block = buffer.slice(0, index)
        buffer = buffer.slice(index + 2)
        const message = parseSseBlock(block)
        if (message) yield message
        else if (isCommentBlock(block)) onHeartbeat?.()
        index = buffer.indexOf('\n\n')
      }
    }
    buffer += decoder.decode()
    const tail = parseSseBlock(buffer)
    if (tail) yield tail
    else if (isCommentBlock(buffer)) onHeartbeat?.()
  } finally {
    reader.releaseLock()
  }
}

/** 整帧都是注释/空行 —— 即心跳。 */
function isCommentBlock(block: string): boolean {
  let sawComment = false
  for (const rawLine of block.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (line === '') continue
    if (!line.startsWith(':')) return false
    sawComment = true
  }
  return sawComment
}

/**
 * 打开一个 SSE 流并消费到结束。
 *
 * `signal` 中止时不会触发 `onError`（用户主动停止不是错误）。
 */
export async function streamSse(
  url: string,
  handlers: SseHandlers,
  signal?: AbortSignal,
): Promise<void> {
  try {
    const resp = await fetch(url, {
      method: 'GET',
      signal,
      headers: { Accept: 'text/event-stream' },
      cache: 'no-store',
    })
    if (!resp.ok || !resp.body) {
      throw new Error(`SSE 连接失败：${resp.status} ${resp.statusText}`)
    }
    for await (const message of readSseFrames(resp.body, handlers.onHeartbeat)) {
      handlers.onMessage(message)
    }
    handlers.onClose?.()
  } catch (error) {
    if (signal?.aborted) {
      handlers.onClose?.()
      return
    }
    handlers.onError?.(error)
  }
}
