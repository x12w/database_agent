import { onUnmounted, ref, type Ref } from 'vue'

/**
 * 一个共享的时钟，给"运行中"的步骤提供滴答的耗时。
 *
 * 整个步骤流只跑一个定时器（不是每行一个），并且只在真的有事发生时
 * 才更新时间，空闲页面不做无谓的重渲染。
 */
export function useTicker(active: () => boolean, intervalMs = 100): Ref<number> {
  const now = ref(Date.now())
  const timer = setInterval(() => {
    if (active()) now.value = Date.now()
  }, intervalMs)
  onUnmounted(() => clearInterval(timer))
  return now
}
