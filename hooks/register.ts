import type { EngineInterface, Register, SessionRateLimit, Timer } from 'claude-code'

// The floating ring (widget/widget.pyw) reads what this module writes in
// ~/.claude/session-limit (the same place whichever copy of the plugin runs:
// its folder, or the copy Claude installs under plugins/cache):
//   usage.json          the rate-limit windows and the 5-hour window's history
//   sessions/<id>.json  this session's state: working or not, context fill
// It draws, sounds and forecasts on its own; this module only feeds it and starts it.

type Point = [number, number] // when (ms), percent used

const MINUTE = 60_000

const data = ($: EngineInterface) => {
  const root = $.plugin.root
  const at = root.toLowerCase().search(/[\\/]\.claude[\\/]/)
  return at < 0 ? `${root}/widget` : `${root.slice(0, at + 8)}/session-limit`
}

const isWindows = ($: EngineInterface) => /^[A-Za-z]:[\\/]/.test($.plugin.root)

// Two readings of one 5-hour window name its reset a few hundred ms apart.
const sameWindow = (a?: string, b?: string) =>
  !!a && !!b && Math.abs(Date.parse(a) - Date.parse(b)) < 15 * MINUTE

const save = async ($: EngineInterface, limits: SessionRateLimit[]) => {
  const now = await $.clock.now()
  const file = `${data($)}/usage.json`
  const five = limits.find(l => l.kind === 'five_hour')

  let history: Point[] = []
  try {
    const prev = JSON.parse(await $.fs.read(file)) as { window?: string; history?: Point[] }
    if (sameWindow(prev.window, five?.resetsAt)) history = prev.history ?? []
  } catch {
    // No file yet, or unreadable: the history starts over.
  }
  if (five) {
    const last = history.at(-1)
    if (!last || last[1] !== five.percentUsed || now - last[0] >= 10 * MINUTE) {
      history.push([now, five.percentUsed])
    }
  }

  await $.fs.write(
    file,
    JSON.stringify({ updatedAt: now, limits, window: five?.resetsAt, history: history.slice(-400) }),
  )
}

// The ring's own language (its menu sets it), so a warning reads like the ring.
const language = async ($: EngineInterface) => {
  try {
    return (JSON.parse(await $.fs.read(`${data($)}/state.json`)) as { lang?: string }).lang === 'en' ? 'en' : 'ar'
  } catch {
    return 'ar'
  }
}

// What the ring lacks, said once a session: without it pythonw fails with no window to say so.
let warned = false

const warn = async ($: EngineInterface, missing: string) => {
  if (warned) return
  warned = true
  const en = (await language($)) === 'en'
  const text =
    missing === 'python'
      ? en
        ? 'Session limit: the ring needs Python 3. Install it from python.org with "Add python.exe to PATH" ticked, then start a new session.'
        : 'حدّ الجلسة: تحتاج الدائرة إلى Python 3. ثبّته من python.org مع تفعيل خيار Add python.exe to PATH، ثم افتح جلسة جديدة.'
      : en
        ? `Session limit: the ring needs ${missing}. Run: pip install ${missing}, then start a new session.`
        : `حدّ الجلسة: تحتاج الدائرة إلى ${missing}. شغّل: pip install ${missing} ثم افتح جلسة جديدة.`
  $.ui.toast(text, { timeoutMs: 20_000 })
}

// Starts the ring detached, so it outlives the session; it keeps itself to one copy.
// First it checks Python and the packages the ring draws with, and names what is missing.
const launch = async ($: EngineInterface) => {
  if (!isWindows($)) return
  const quote = (path: string) => path.replace(/'/g, "''")
  const script = quote(`${$.plugin.root}\\widget\\widget.pyw`)
  const check = quote(`${$.plugin.root}\\widget\\check.py`)
  const command = [
    '$p = (Get-Command pythonw.exe, pyw.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source',
    "if (-not $p) { 'no-python'; exit }",
    // pythonw has no output to answer with; its console twin beside it does.
    "$c = $p -replace 'pythonw\\.exe$', 'python.exe' -replace 'pyw\\.exe$', 'py.exe'",
    `if (Test-Path $c) { $m = & $c '${check}'; if ($m) { "missing:$m"; exit } }`,
    `Start-Process -FilePath $p -ArgumentList '"${script}"'`,
  ].join('; ')
  const { stdout } = await $.process.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command])
  const said = stdout.trim()
  if (said === 'no-python') await warn($, 'python')
  else if (said.startsWith('missing:')) await warn($, said.slice('missing:'.length).trim())
}

// This session's state for the ring: it spins while `working`, chimes when
// `doneAt` moves after a long reply, and shows `context` on its card.
const status = { working: false, since: 0, doneAt: 0, lastDurationMs: 0, context: null as number | null }
let sessionId: string | undefined
let heartbeat: Timer | undefined

const report = async ($: EngineInterface) => {
  sessionId ??= await $.session.id()
  const updatedAt = await $.clock.now()
  await $.fs.write(`${data($)}/sessions/${sessionId}.json`, JSON.stringify({ ...status, updatedAt }))
}

const quietly = (work: Promise<unknown>) => work.catch(() => undefined)

// The ring's refresh button drops refresh.json. One open session answers it
// with the smallest model call there is, since the limits come back with every
// response, then writes the fresh reading, claude.ai use included.
let poller: Timer | undefined

const answerRefresh = async ($: EngineInterface) => {
  const dir = data($)
  const read = async (name: string) => {
    try {
      return (JSON.parse(await $.fs.read(`${dir}/${name}`)) as { askedAt?: number }).askedAt ?? 0
    } catch {
      return 0
    }
  }
  const asked = await read('refresh.json')
  const now = await $.clock.now()
  if (!asked || now - asked > 60_000 || (await read('refresh-done.json')) >= asked) return

  sessionId ??= await $.session.id()
  await $.fs.write(`${dir}/refresh-done.json`, JSON.stringify({ askedAt: asked, by: sessionId }))
  await $.model.complete({ model: 'haiku', prompt: 'ok', maxTokens: 1, timeoutMs: 15_000 })
  const { rateLimits } = await $.session.usage()
  if (rateLimits.length) await save($, rateLimits)
}

const listen = ($: EngineInterface) => {
  poller ??= $.clock.every(3_000, () => void quietly(answerRefresh($)))
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    listen($)
    if (e.isInteractive) void quietly(launch($))

    try {
      const { rateLimits } = await $.session.usage()
      if (rateLimits.length) await save($, rateLimits)
    } catch {
      // No reading yet: session.measure writes one after the first response.
    }

    return result
  })

  // The desktop app's sessions start headless and gain their window here.
  on('session.attach', ($, e, next) => {
    listen($)
    if (e.surface === 'desktop' || e.surface === 'terminal') void quietly(launch($))

    return next(e)
  })

  // After a /reload-plugins no session.start comes, so any event starts the listening.
  on('session.measure', async ($, e, next) => {
    listen($)
    if (e.changed.includes('rateLimits') && e.rateLimits.length) {
      await quietly(save($, e.rateLimits))
    }
    if (e.changed.includes('context') && e.context.percent !== undefined) {
      status.context = e.context.percent
      await quietly(report($))
    }

    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    listen($)
    status.working = true
    status.since = await $.clock.now()
    // A heartbeat, so a session that dies mid-turn stops the ring spinning.
    heartbeat ??= $.clock.every(20_000, () => void quietly(report($)))
    await quietly(report($))

    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      heartbeat?.cancel()
      heartbeat = undefined
      status.working = false
      status.doneAt = await $.clock.now()
      status.lastDurationMs = e.durationMs
      await quietly(report($))
    }

    return next(e)
  })

  on('session.end', async ($, e, next) => {
    heartbeat?.cancel()
    heartbeat = undefined
    if (status.working) {
      status.working = false
      await quietly(report($))
    }

    return next(e)
  })
}
