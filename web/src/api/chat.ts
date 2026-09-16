import { API } from './endpoints.ts'
import { withQuery } from './client.ts'
import { streamSse, type SseMessage } from './sse.ts'
import { isChatEventType, type ChatEvent } from '../types/events.ts'

export interface ChatStreamParams {
  message: string
  sessionId: string
  dbId: string | null
  /** 逗号分隔的表名；空数组或 null 表示不筛选。 */
  tableFilter: string[] | null
}

export function chatStreamUrl(params: ChatStreamParams): string {
  const filter = params.tableFilter && params.tableFilter.length > 0 ? params.tableFilter.join(',') : null
  return withQuery(API.chatStream(), {
    message: params.message,
    session_id: params.sessionId,
    db_id: params.dbId,
    table_filter: filter,
  })
}

/**
 * 消费聊天 SSE。回调只会收到契约里的事件；契约外的事件名（例如心跳或
 * 后端未来的新事件）被安静丢弃，不进入视图。
 */
export async function streamChat(
  params: ChatStreamParams,
  handlers: {
    onEvent: (event: ChatEvent) => void
    onHeartbeat?: () => void
    onClose?: () => void
    onError?: (error: unknown) => void
  },
  signal?: AbortSignal,
): Promise<void> {
  await streamSse(
    chatStreamUrl(params),
    {
      onMessage: (message: SseMessage) => {
        if (!isChatEventType(message.event)) return
        let payload: unknown
        try {
          payload = JSON.parse(message.data)
        } catch {
          return
        }
        const event = payload as ChatEvent
        if (event && event.type === message.event) handlers.onEvent(event)
      },
      onHeartbeat: handlers.onHeartbeat,
      onClose: handlers.onClose,
      onError: handlers.onError,
    },
    signal,
  )
}
