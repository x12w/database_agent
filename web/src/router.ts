import { ref, type Ref } from 'vue'

/**
 * 极简路由：`/`（查询）、`/admin`（管理面板）、`/dev`（开发夹具）。
 *
 * 只有三条路径，用不着引 vue-router；`history.pushState` 够用，
 * 后端把这三条路径都回落到 index.html 即可。
 */
export type RoutePath = '/' | '/admin' | '/dev'

function normalize(path: string): RoutePath {
  if (path.startsWith('/admin')) return '/admin'
  if (path.startsWith('/dev')) return '/dev'
  return '/'
}

export const route: Ref<RoutePath> = ref(
  normalize(typeof window === 'undefined' ? '/' : window.location.pathname),
)

export function navigate(to: RoutePath): void {
  if (typeof window !== 'undefined' && window.location.pathname !== to) {
    window.history.pushState({}, '', to)
  }
  route.value = to
}

if (typeof window !== 'undefined') {
  window.addEventListener('popstate', () => {
    route.value = normalize(window.location.pathname)
  })
}
