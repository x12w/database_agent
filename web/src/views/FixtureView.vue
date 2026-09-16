<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'

import ExploreStage from '../components/ExploreStage.vue'
import RunView from '../components/RunView.vue'
import { useFixturePlayer, type PlayerStep } from '../composables/useFixturePlayer.ts'
import { useTicker } from '../composables/useTicker.ts'
import { applyExploreEvent, createExploreState, type ExploreState } from '../core/explore.ts'
import { applyEvent, createRun, type RunState } from '../core/run.ts'
import { EXPLORE_FIXTURES, getExploreFixture } from '../dev/exploreFixture.ts'
import { FIXTURES, getFixture } from '../dev/fixture.ts'
import { navigate } from '../router.ts'

/**
 * 开发夹具视图（`/dev`）。
 *
 * 两条通道各一套夹具，喂进**同一套**归约与**同一批**组件：
 *   · 聊天：`core/run.ts` 的 `applyEvent` + `RunView` / `StepStream` / `StepRow`；
 *   · 探索：`core/explore.ts` 的 `applyExploreEvent` + `ExploreStage`。
 * 因此这里看到的就是后端接上以后的真实渲染，不需要任何后端进程。
 */
type Channel = 'chat' | 'explore'

const channel = ref<Channel>('chat')
const scenarioId = ref(FIXTURES[0]!.id)
const exploreScenarioId = ref(EXPLORE_FIXTURES[0]!.id)

const run = reactive<RunState>(createRun())
const exploreState = reactive<ExploreState>(createExploreState())

const fixture = computed(() => getFixture(scenarioId.value))
const exploreFixture = computed(() => getExploreFixture(exploreScenarioId.value))

/** 回放器只关心"第几步、隔多久"，事件类型由这里的两条通道各自派发。 */
const chatSteps = computed<PlayerStep[]>(() =>
  fixture.value.steps.map((item) => ({ at: item.at, type: item.event.type })),
)
const exploreSteps = computed<PlayerStep[]>(() =>
  exploreFixture.value.steps.map((item) => ({ at: item.at, type: item.event.type })),
)

function applyStep(index: number): void {
  if (channel.value === 'chat') {
    const item = fixture.value.steps[index]
    if (item) applyEvent(run, item.event)
    return
  }
  const item = exploreFixture.value.steps[index]
  if (item) applyExploreEvent(exploreState, item.event)
}

const { speed, playing, finished, cursor, total, totalMs, currentType, play, pause, step, jumpToEnd, reset } =
  useFixturePlayer({
    steps: () => (channel.value === 'chat' ? chatSteps.value : exploreSteps.value),
    apply: applyStep,
    reset: () => {
      Object.assign(run, createRun())
      Object.assign(exploreState, createExploreState())
    },
  })

const tick = useTicker(() => playing.value)

watch([scenarioId, exploreScenarioId, channel], reset)

/**
 * 支持用 URL 直接驱动，便于截图 / 自动化：
 *   /dev?fixture=long&speed=8&play=1        聊天通道（默认）
 *   /dev?channel=explore&fixture=scan&play=1 探索通道
 *   /dev?fixture=failed&jump=1              直接跳到结尾
 */
onMounted(async () => {
  const params = new URLSearchParams(window.location.search)
  const wantedChannel = params.get('channel')
  if (wantedChannel === 'explore' || wantedChannel === 'chat') channel.value = wantedChannel
  const wanted = params.get('fixture')
  if (wanted) {
    if (channel.value === 'explore' && EXPLORE_FIXTURES.some((f) => f.id === wanted)) {
      exploreScenarioId.value = wanted
    } else if (channel.value === 'chat' && FIXTURES.some((f) => f.id === wanted)) {
      scenarioId.value = wanted
    }
  }
  const speedParam = Number(params.get('speed'))
  if (Number.isFinite(speedParam) && speedParam >= 1) speed.value = Math.min(16, speedParam)
  await nextTick()
  if (params.get('jump') === '1') jumpToEnd()
  else if (params.get('play') === '1') play()
})

const description = computed(() =>
  channel.value === 'chat' ? fixture.value.description : exploreFixture.value.description,
)
</script>

<template>
  <div class="shell">
    <div class="dev-bar">
      <span class="tag">开发夹具</span>

      <div class="channels">
        <button
          type="button"
          class="chan"
          :class="{ active: channel === 'chat' }"
          @click="channel = 'chat'"
        >
          查询通道
        </button>
        <button
          type="button"
          class="chan"
          :class="{ active: channel === 'explore' }"
          @click="channel = 'explore'"
        >
          探索通道
        </button>
      </div>

      <select v-if="channel === 'chat'" v-model="scenarioId">
        <option v-for="item in FIXTURES" :key="item.id" :value="item.id">{{ item.title }}</option>
      </select>
      <select v-else v-model="exploreScenarioId">
        <option v-for="item in EXPLORE_FIXTURES" :key="item.id" :value="item.id">
          {{ item.title }}
        </option>
      </select>
      <span class="desc muted">{{ description }}</span>

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
      {{ cursor }} / {{ total }} 事件 · 下一个：{{ currentType }} · 模拟总时长
      {{ (totalMs / 1000).toFixed(1) }}s
      <span v-if="channel === 'explore'" class="hint">· 夹具里卡片是只读的，不会真的提交</span>
    </div>

    <main class="stage">
      <RunView
        v-if="channel === 'chat'"
        :text="fixture.message"
        :run="run"
        :now="tick"
        :can-retry="true"
        @retry="reset"
      />
      <ExploreStage v-else :state="exploreState" :profile-id="null" :interactive="false" />
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

.channels {
  display: flex;
  gap: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 2px;
}

.chan {
  padding: 3px 10px;
  border-radius: 4px;
  font-size: var(--text-xs);
  color: var(--text-muted);
  transition: var(--transition);
}

.chan:hover {
  color: var(--text-secondary);
}

.chan.active {
  color: var(--text-primary);
  background: var(--bg-elevated);
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

.hint {
  color: var(--accent-warm-dim);
}

.stage {
  flex: 1;
  overflow: auto;
  padding: 8px 28px 24px;
  max-width: 900px;
  width: 100%;
}
</style>
