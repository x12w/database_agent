import assert from 'node:assert/strict'
import test from 'node:test'
import { createChatStore } from '../src/stores/chat.ts'

test('old request cleanup preserves the new request and its cancellation handle', async () => {
  const originalFetch = globalThis.fetch
  const signals: AbortSignal[] = []
  // Synthetic promises; no HTTP requests are made.
  globalThis.fetch = (_url, options) => new Promise<Response>((_resolve, reject) => {
    const signal = options?.signal
    assert.ok(signal)
    signals.push(signal)
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
  })
  const store = createChatStore({ persist: false })
  try {
    const first = store.send('synthetic first question')
    store.stop()
    const second = store.send('synthetic second question')
    assert.equal(signals.length, 2)
    assert.equal(store.state.streaming, true)
    await first
    assert.equal(store.state.streaming, true)
    assert.equal(signals[1]?.aborted, false)
    store.stop()
    assert.equal(signals[1]?.aborted, true)
    await second
  } finally {
    store.stop()
    globalThis.fetch = originalFetch
  }
})
