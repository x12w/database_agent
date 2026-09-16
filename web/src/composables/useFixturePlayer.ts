import { computed, onBeforeUnmount, ref, type ComputedRef, type Ref } from 'vue'

/** 回放脚本的一步：距序列开始的毫秒偏移 + 该步事件的类型（类型只用于顶部提示行）。 */
export interface PlayerStep {
  at: number
  type: string
}

export interface FixturePlayer {
  /** 回放倍速（1×–16×）。 */
  speed: Ref<number>
  playing: Ref<boolean>
  finished: Ref<boolean>
  cursor: Ref<number>
  /** 脚本里的步骤总数。 */
  total: ComputedRef<number>
  /** 模拟总时长（毫秒）。 */
  totalMs: ComputedRef<number>
  /** 下一个要应用的事件类型。 */
  currentType: ComputedRef<string>
  play: () => void
  pause: () => void
  step: () => void
  jumpToEnd: () => void
  reset: () => void
}

/**
 * 夹具回放器：按 `at` 的间隔逐步应用事件，支持倍速 / 暂停 / 单步 / 跳到结尾。
 *
 * 聊天与探索两条通道的开发页共用它 —— 两边脚本形状一样（`{ at, event }`），
 * 差别只在 `apply` 里调用哪一个归约函数（`applyEvent` / `applyExploreEvent`）。
 * 组件卸载时定时器自动清掉，不会留下跑着的 setTimeout。
 */
export function useFixturePlayer(options: {
  /** 当前场景的步骤。场景（或通道）切换后要重新取，所以传函数而不是数组。 */
  steps: () => PlayerStep[]
  /** 应用第 index 步的事件（游标由本 composable 维护）。 */
  apply: (index: number) => void
  /** 清空场景状态；游标与播放标志由本 composable 负责。 */
  reset: () => void
}): FixturePlayer {
  const speed = ref(2)
  const playing = ref(false)
  const finished = ref(false)
  const cursor = ref(0)

  let timer: ReturnType<typeof setTimeout> | null = null

  const total = computed(() => options.steps().length)
  const totalMs = computed(() => options.steps().reduce((max, item) => Math.max(max, item.at), 0))
  const currentType = computed(() => options.steps()[cursor.value]?.type ?? '—')

  function clear(): void {
    if (timer) clearTimeout(timer)
    timer = null
  }

  function step(): void {
    const item = options.steps()[cursor.value]
    if (!item) {
      playing.value = false
      finished.value = true
      return
    }
    options.apply(cursor.value)
    cursor.value += 1
  }

  function schedule(): void {
    clear()
    if (!playing.value) return
    const steps = options.steps()
    const item = steps[cursor.value]
    if (!item) {
      playing.value = false
      finished.value = true
      return
    }
    const previous = cursor.value > 0 ? steps[cursor.value - 1]!.at : 0
    const delay = Math.max(0, item.at - previous) / speed.value
    timer = setTimeout(() => {
      step()
      schedule()
    }, delay)
  }

  function play(): void {
    if (finished.value) reset()
    playing.value = true
    schedule()
  }

  function pause(): void {
    playing.value = false
    clear()
  }

  function jumpToEnd(): void {
    pause()
    while (options.steps()[cursor.value] !== undefined) step()
    finished.value = true
  }

  function reset(): void {
    pause()
    finished.value = false
    cursor.value = 0
    options.reset()
  }

  onBeforeUnmount(clear)

  return { speed, playing, finished, cursor, total, totalMs, currentType, play, pause, step, jumpToEnd, reset }
}
