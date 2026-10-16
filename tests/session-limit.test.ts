import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const NOW = Date.parse('2026-10-09T01:56:00.000Z')
const MINUTE = 60_000

const limits = (used: number, resetsAt = '2026-10-09T04:40:00.435Z') => [
  { kind: 'five_hour', percentUsed: used, resetsAt },
  { kind: 'seven_day', percentUsed: 6, resetsAt: '2026-10-13T08:00:00.000Z' },
]

const RAN = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

// A folder in memory standing in for the disk.
const disk = (on: On) => {
  const files = new Map<string, string>()
  on('fs.write', (_$, e) => (files.set(e.path.replaceAll('\\', '/'), e.text), { value: undefined }))
  on('fs.read', (_$, e) => {
    const text = files.get(e.path.replaceAll('\\', '/'))
    return text === undefined ? { deny: 'no such file' } : { value: text }
  })
  const read = (suffix: string) => {
    const key = [...files.keys()].find(k => k.endsWith(suffix))
    return key === undefined ? undefined : JSON.parse(files.get(key) ?? '')
  }
  return { files, read }
}

test('readings build up a history within one 5-hour window', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const { read } = disk(on)
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  const measure = (used: number, resetsAt?: string) =>
    $.session.measure({ context: { window: 1_000_000 }, rateLimits: limits(used, resetsAt), changed: ['rateLimits'] })

  await measure(43)
  await clock.advance(5 * MINUTE)
  await measure(44, '2026-10-09T04:39:59.779Z') // the same window, its reset read a little apart
  expect(read('/.claude/session-limit/usage.json').history).toEqual([
    [NOW, 43],
    [NOW + 5 * MINUTE, 44],
  ])
  expect(read('/.claude/session-limit/usage.json').limits[0].percentUsed).toBe(44)

  await clock.advance(MINUTE)
  await measure(2, '2026-10-09T09:40:00.000Z') // a new window starts over
  expect(read('/.claude/session-limit/usage.json').history).toEqual([[NOW + 6 * MINUTE, 2]])
})

test('the session reports working, then done with how long it took', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const { read } = disk(on)
  on('session.id', () => ({ value: 'abc' }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  const done = { answer: '', isAborted: false, turnId: 't1', reason: 'answer' } as const

  await $.turn.start({ text: 'hi', turnId: 't1' })
  expect(read('/.claude/session-limit/sessions/abc.json')).toMatchObject({ working: true, since: NOW })

  await clock.advance(25_000) // the heartbeat keeps a long turn fresh
  expect(read('/.claude/session-limit/sessions/abc.json')).toMatchObject({ working: true, updatedAt: NOW + 20_000 })

  await $.turn.complete({ ...done, durationMs: 9_000, agentId: 'sub' }) // a subagent's end is not ours
  expect(read('/.claude/session-limit/sessions/abc.json').working).toBe(true)

  await $.turn.complete({ ...done, durationMs: 25_000 })
  expect(read('/.claude/session-limit/sessions/abc.json')).toMatchObject({
    working: false,
    doneAt: NOW + 25_000,
    lastDurationMs: 25_000,
  })
})

test('the ring starts when the desktop app or a terminal attaches', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const runs: (readonly string[])[] = []
  on('process.run', (_$, e) => (runs.push(e.argv), { value: RAN }))
  on('session.attach', (_$, e) => ({ clientId: e.clientId }))

  await $.session.attach({ surface: 'mobile', clientId: 'mobile:default' })
  await clock.settle()
  expect(runs.length).toBe(0)

  await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
  await clock.settle()
  expect(runs.length).toBe(1)
  expect(runs[0]?.[0]).toBe('powershell.exe')
  expect(runs[0]?.at(-1)).toMatch(/Start-Process -FilePath \$p -ArgumentList '".*\\widget\\widget\.pyw"'/)
})

test('the refresh button gets one session to fetch a fresh reading', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const { files, read } = disk(on)
  const calls: string[] = []
  on('session.id', () => ({ value: 'abc' }))
  on('session.attach', (_$, e) => ({ clientId: e.clientId }))
  on('process.run', () => ({ value: RAN }))
  on('model.complete', (_$, e) => {
    calls.push(e.model)
    return { value: { isAnswered: true, text: 'ok', usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 } } }
  })
  on('session.usage', () => ({ value: { startedAt: NOW, context: { window: 1_000_000 }, rateLimits: limits(31) } }))

  await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
  await clock.advance(4_000)
  expect(calls).toEqual([]) // nobody asked

  files.set('C:/Users/ACER ODAY/.claude/session-limit/refresh.json', JSON.stringify({ askedAt: NOW + 4_000 }))
  await clock.advance(3_000)
  expect(calls).toEqual(['haiku'])
  expect(read('/.claude/session-limit/usage.json').limits[0].percentUsed).toBe(31)
  expect(read('/.claude/session-limit/refresh-done.json')).toEqual({ askedAt: NOW + 4_000, by: 'abc' })

  await clock.advance(6_000)
  expect(calls).toEqual(['haiku']) // answered once
})
