import { API } from './endpoints.ts'
import { apiGet, apiSend, withQuery } from './client.ts'
import { streamSse, type SseMessage } from './sse.ts'
import type {
  AdminQuestionsResponse,
  ExploreDoneEvent,
  ExploreProgressEvent,
  ExploreQuestionEvent,
} from '../types/api.ts'

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
  onProgress?: (event: ExploreProgressEvent) => void
  onQuestion?: (event: ExploreQuestionEvent) => void
  onDone?: (event: ExploreDoneEvent) => void
  onError?: (message: string) => void
  onClose?: () => void
}

/** 主动探索数据库（SSE）。事件名沿用旧实现：progress / admin_question / done / explore_error。 */
export async function exploreDatabase(
  dbId: string | null,
  sessionId: string | null,
  handlers: ExploreHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const url = withQuery(API.adminExploreDatabase(), { db_id: dbId, session_id: sessionId })
  let errored = false
  await streamSse(
    url,
    {
      onMessage: (message: SseMessage) => {
        let payload: unknown
        try {
          payload = JSON.parse(message.data)
        } catch {
          return
        }
        switch (message.event) {
          case 'progress':
            handlers.onProgress?.(payload as ExploreProgressEvent)
            break
          case 'admin_question':
            handlers.onQuestion?.(payload as ExploreQuestionEvent)
            break
          case 'done':
            handlers.onDone?.(payload as ExploreDoneEvent)
            break
          case 'explore_error': {
            errored = true
            const detail = payload as { message?: string }
            handlers.onError?.(detail.message ?? '探索出错')
            break
          }
          default:
            break
        }
      },
      onClose: handlers.onClose,
      onError: (error) => {
        // 传输层中断与业务错误分开报，避免"探索出错"盖掉真正的报错。
        if (!errored) handlers.onError?.(error instanceof Error ? error.message : '探索连接中断')
        handlers.onClose?.()
      },
    },
    signal,
  )
}
