<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import RunView from '../components/RunView.vue'
import { useTicker } from '../composables/useTicker.ts'
import { FIXTURES, getFixture, type Fixture } from '../dev/fixture.ts'
import { applyEvent, createRun, type RunState } from '../core/run.ts'
import { navigate } from '../router.ts'

/**
 * 开发夹具视图（`/dev`）。
 *
 * 把合成的事件序列喂进**同一套**归约（`applyEvent`）与**同一批**组件
 * （RunView / StepStream / StepRow），因此这里看到的就是后端接上以后的
 * 真实渲染，不需要任何后端进程。
 */
const scenarioId = ref(FIXTURES[0]!.id)
const speed = ref(2)
const playing = ref(false)
const finished = ref(false)
const cursor = ref(0)

const run = reactive<RunState>(createRun())
const now = useTicker(() => playing.value)

let timer: ReturnType<typeof setTimeout> | null = null

const fixture = computed<Fixture>(() => getFixture(scenarioId.value))
const currentEvent = computed(() => fixture.value.steps[cursor.value]?.event.type ?? '—')

function clear(): void {
  if (timer) clearTimeout(timer)
  timer = null
}

function reset(): void {
  clear()
  playing.value = false
  finished.value = false
  cursor.value = 0
  Object.assign(run, createRun())
}

function step(): void {
  const item = fixture.value.steps[cursor.value]
  if (!item) {
    playing.value = false
    finished.value = true
    return
  }
  applyEvent(run, item.event)
  cursor.value += 1
}

function schedule(): void {
  clear()
  if (!playing.value) return
  const item = fixture.value.steps[cursor.value]
  if (!item) {
    playing.value = false
    finished.value = true
    return
  }
  const previous = cursor.value > 0 ? fixture.value.steps[cursor.value - 1]!.at : 0
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
  while (cursor.value < fixture.value.steps.length) step()
  finished.value = true
}

watch(scenarioId, reset)

onBeforeUnmount(clear)

/**
 * 支持用 URL 直接驱动，便于截图 / 自动化：
 *   /dev?fixture=long&speed=8&play=1   播放
 *   /dev?fixture=failed&jump=1         直接跳到结尾
 */
onMounted(async () => {
  const params = new URLSearchParams(window.location.search)
  const wanted = params.get('fixture')
  if (wanted && FIXTURES.some((f) => f.id === wanted)) scenarioId.value = wanted
  const speedParam = Number(params.get('speed'))
  if (Number.isFinite(speedParam) && speedParam >= 1) speed.value = Math.min(16, speedParam)
  await nextTick()
  if (params.get('jump') === '1') jumpToEnd()
  else if (params.get('play') === '1') play()
})
</script>

<template>
  <div class="shell">
    <div class="dev-bar">
      <span class="tag">开发夹具</span>
      <select v-model="scenarioId">
        <option v-for="item in FIXTURES" :key="item.id" :value="item.id">
          {{ item.title }}
        </option>
      </select>
      <span class="desc muted">{{ fixture.description }}</span>

      <span class="spacer" />

      <label class="speed">
        倍速
        <input v-model.number="speed" type="range" min="1" max="16" step="1" />
        <span class="mono">{{ speed }}×</span>
      </label>

      <button v-if="!playing" type="button" class="btn btn-primary" @click="play">
        {{ finished ? '重放' : '播放' }}
      </button>
      <button v-else type="button" class="btn" @click="pause">暂停</button>
      <button type="button" class="btn" :disabled="playing || finished" @click="step">单步</button>
      <button type="button" class="btn" @click="jumpToEnd">跳到结尾</button>
      <button type="button" class="btn" @click="reset">重置</button>
      <button type="button" class="btn" @click="navigate('/')">返回应用</button>
    </div>

    <div class="meta mono">
      {{ cursor }} / {{ fixture.steps.length }} 事件 · 下一个：{{ currentEvent }} · 模拟总时长
      {{ (fixture.totalMs / 1000).toFixed(1) }}s
    </div>

    <main class="stage">
      <RunView
        :text="fixture.message"
        :run="run"
        :now="now"
        :can-retry="true"
        @retry="reset"
      />
    </main>
  </div>
</template>

<style scoped>
.shell {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.dev-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-surface);
}

.tag {
  font-size: var(--text-xs);
  color: var(--accent-warm);
  border: 1px solid rgba(229, 181, 103, 0.4);
  border-radius: 999px;
  padding: 2px 8px;
}

.desc {
  font-size: var(--text-xs);
}

.spacer {
  flex: 1;
}

.speed {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.speed input {
  width: 100px;
  accent-color: var(--accent-cool);
}

.meta {
  padding: 6px 18px;
  font-size: var(--text-xs);
  color: var(--text-dim);
  border-bottom: 1px solid var(--border);
}

.stage {
  flex: 1;
  overflow: auto;
  padding: 8px 28px 24px;
  max-width: 900px;
  width: 100%;
}
</style>
