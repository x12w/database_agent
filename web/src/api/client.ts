/**
 * 极薄的 fetch 封装。所有请求都经过它，便于统一处理错误与中文文案。
 */

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function readError(resp: Response): Promise<string> {
  let detail = `${resp.status} ${resp.statusText}`
  try {
    const text = await resp.text()
    if (text) {
      try {
        const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown }
        if (typeof parsed.detail === 'string') detail = parsed.detail
        else if (typeof parsed.message === 'string') detail = parsed.message
        else detail = text.slice(0, 300)
      } catch {
        detail = text.slice(0, 300)
      }
    }
  } catch {
    /* 读不出正文就用状态码 */
  }
  return detail
}

export async function apiGet<T>(url: string, signal?: AbortSignal): Promise<T> {
  const resp = await fetch(url, { method: 'GET', signal, headers: { Accept: 'application/json' } })
  if (!resp.ok) throw new ApiError(resp.status, await readError(resp))
  return (await resp.json()) as T
}

export async function apiSend<T>(
  method: 'POST' | 'PUT' | 'DELETE',
  url: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const resp = await fetch(url, {
    method,
    signal,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!resp.ok) throw new ApiError(resp.status, await readError(resp))
  if (resp.status === 204) return undefined as T
  const text = await resp.text()
  if (!text) return undefined as T
  return JSON.parse(text) as T
}

/** 把查询参数拼成 query string，跳过空值。 */
export function withQuery(path: string, params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue
    search.set(key, String(value))
  }
  const qs = search.toString()
  return qs ? `${path}?${qs}` : path
}
