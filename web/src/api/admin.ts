import { API } from './endpoints.ts'
import { apiGet, apiSend, withQuery } from './client.ts'
import { streamSse, type SseMessage } from './sse.ts'
import { isExploreEventType, type ExploreEvent } from '../types/events.ts'
import type { AdminQuestionsResponse } from '../types/api.ts'

export function fetchAdminQuestions(
  status: 'pending' | 'answered' | 'all',
  profileId: string | null,
): Promise<AdminQuestionsResponse> {
  return apiGet<AdminQuestionsResponse>(
    withQuery(API.adminQuestions(), { status, profile_id: profileId }),
  )
}

export function answerAdminQuestion(questionId: string, answer: string, profileId: string | null) {
  return apiSend<{ success?: boolean; message?: string }>('POST', API.adminAnswer(questionId), {
    answer,
    profile_id: profileId,
  })
}

export function ignoreAdminQuestion(questionId: string, profileId: string | null) {
  return apiSend<{ success?: boolean; message?: string }>('POST', API.adminIgnore(questionId), {
    profile_id: profileId,
  })
}

export interface ExploreHandlers {
  /** 只会收到契约（`src/dbchat/events/explore.py`）里的事件。 */
  onEvent: (event: ExploreEvent) => void
  onHeartbeat?: () => void
  onClose?: () => void
  /** 传输层失败（网络断开、服务端提前关闭）。业务失败走 `explore.failed` 事件。 */
  onError?: (error: unknown) => void
}

/**
 * 主动探索数据库（SSE）。
 *
 * 事件名是契约里的点号命名（`explore.progress` 等），不再是旧实现的
 * `progress` / `admin_question` / `done` / `explore_error`：
 * `error` 是浏览器的保留 SSE 事件名，旧代码为了绕开它不得不改名 ——
 * 点号命名从根上消除了那个 hack。
 *
 * 契约外的帧（心跳、未来的新事件）被安静丢弃，不进入视图。
 */
export async function exploreDatabase(
  dbId: string | null,
  sessionId: string | null,
  handlers: ExploreHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const url = withQuery(API.adminExploreDatabase(), { db_id: dbId, session_id: sessionId })
  await streamSse(
    url,
    {
      onMessage: (message: SseMessage) => {
        if (!isExploreEventType(message.event)) return
        let payload: unknown
        try {
          payload = JSON.parse(message.data)
        } catch {
          return
        }
        const event = payload as ExploreEvent
        if (event && event.type === message.event) handlers.onEvent(event)
      },
      onHeartbeat: handlers.onHeartbeat,
      onClose: handlers.onClose,
      onError: handlers.onError,
    },
    signal,
  )
}
