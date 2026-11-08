import { expect, mock, test } from 'claude-code/testing'
import { curve, sha256, verify } from './hooks/p256.js'

const SOCKET = '/home/me/.local/state/walkie-talkie/mod.sock'
const PHONE_KEY_FILE = '/home/me/.local/state/walkie-talkie/phone.pub'
const CONFIG_FILE = '/home/me/.config/walkie-talkie/connect.json'
const WORK = '/home/me/.claude'
const PERSONAL = '/home/me/.claude-personal'
const CONFIG = { logins: { [WORK]: { deny: [] }, [PERSONAL]: { deny: [] } }, deny: [] }

// The phone's key, as a stand-in for its secure hardware: it signs what a fingerprint approves.
const hexOf = (n) => n.toString(16).padStart(64, '0')
const bytesOfHex = (hex) => Uint8Array.from(hex.match(/../g), (b) => parseInt(b, 16))
const base64Of = (bytes) => btoa(String.fromCharCode(...bytes))
function affine(point) {
  const zInverse = curve.inverse(point[2], curve.P)
  return [curve.mod(point[0] * zInverse * zInverse), curve.mod(point[1] * zInverse * zInverse * zInverse)]
}
function phoneKey(secret) {
  const [x, y] = affine(curve.multiply(secret, curve.G))
  return {
    public: base64Of(bytesOfHex(`${curve.KEY_PREFIX}04${hexOf(x)}${hexOf(y)}`)),
    async sign(message) {
      const digest = sha256(new TextEncoder().encode(message))
      const e = BigInt(`0x${Array.from(digest, (b) => b.toString(16).padStart(2, '0')).join('')}`)
      const k = curve.mod(e * 7n + secret, curve.N) || 1n
      const r = curve.mod(affine(curve.multiply(k, curve.G))[0], curve.N)
      const s = curve.mod(curve.inverse(k, curve.N) * (e + r * secret), curve.N)
      const integer = (n) => {
        let bytes = bytesOfHex(hexOf(n))
        while (bytes.length > 1 && bytes[0] === 0 && !(bytes[1] & 0x80)) bytes = bytes.subarray(1)
        return bytes[0] & 0x80 ? [0x02, bytes.length + 1, 0, ...bytes] : [0x02, bytes.length, ...bytes]
      }
      const body = [...integer(r), ...integer(s)]
      return base64Of(new Uint8Array([0x30, body.length, ...body]))
    },
  }
}
const PHONE = phoneKey(0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdefn)
const OTHER_PHONE = phoneKey(0xfeedfacecafebeef0123456789abcdef0123456789abcdef0123456789abcdefn)

// What the gateway passes on from the phone: a fingerprint's signature over this session's own challenge.
async function grantFor(poll, key = PHONE, change = {}) {
  const grant = { session: poll.instance, modNonce: poll.nonce, nonce: 'gateway-nonce', run: 'run-1', ...change }
  grant.sig = await key.sign(['walkie-start', grant.nonce, grant.run, grant.session, grant.modNonce].join('|'))
  return grant
}

// The world beneath the mod: a gateway that hands out `orders`, one per poll, then holds polls
// open, and a session that records what the mod asks of it.
// A gateway that signs nothing itself: a chosen:true it hands out carries the phone's grant only when
// `grants` says so, as the real one does after a fingerprint, unless the order brings its own.
function world(
  on,
  {
    env = {},
    orders = [],
    submitted = () => ({ text: 'ok' }),
    abortRejects = false,
    run = () => ({ value: { exitCode: 0, stdout: '4242\n', stderr: '' } }),
    config = CONFIG,
    surfaces = ['terminal'],
    phoneKeyFile = () => PHONE.public,
    grants = true,
    sessionId = () => 'session-1',
    cwd = () => '/work/project',
    read = () => undefined,
  } = {},
) {
  mock.env(on, { HOME: '/home/me', CLAUDE_CONFIG_DIR: PERSONAL, ...env })
  const clock = mock.clock(on)
  const seen = { polls: [], events: [], submits: [], aborts: [], statuses: [], toasts: [], rows: [], order: [], reads: [] }
  const queue = [...orders]
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: sessionId() }))
  on('session.cwd', () => ({ value: cwd() }))
  on('session.surfaces', () => ({ value: surfaces }))
  on('process.run', (_, e) => run(e))
  on('fs.stat', (_, e) => ({ value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: false, realPath: e.path.replace(/\/+$/, '') } }))
  on('fs.read', (_, e) => {
    seen.reads.push(e.path)
    const own = read(e.path)
    if (own) return own
    if (e.path === CONFIG_FILE && config !== null) return { value: typeof config === 'string' ? config : JSON.stringify(config) }
    if (e.path === PHONE_KEY_FILE && phoneKeyFile() !== null) return { value: `${phoneKeyFile()}\n` }
    return { deny: 'no such file' }
  })
  on('ui.status', (_, e) => {
    seen.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', (_, e) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  on('http.fetch', async (_, e) => {
    expect(e.init.socketPath).toBe(SOCKET)
    const body = JSON.parse(e.init.body)
    if (e.url.endsWith('/event')) {
      seen.events.push(...body.events)
      return { value: { status: 200, ok: true, headers: {}, text: '{}' } }
    }
    seen.polls.push(body)
    while (!queue.length) await clock.sleep(1000)
    let order = queue.shift()
    if (typeof order === 'function') order = await order(body)
    if (order.gatewayOff) return { value: { status: 500, ok: false, headers: {}, text: '' } }
    if (grants && order.chosen === true && body.chosen !== true && order.grant === undefined) {
      order = { ...order, grant: await grantFor(body) }
    }
    // The real gateway names the run a fingerprint approved in each answer to the chosen session.
    if (grants && order.chosen === true && !('run' in order)) order = { ...order, run: 'run-1' }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(order) } }
  })
  on('prompt.submit', (_, e) => {
    seen.submits.push(e)
    seen.order.push('prompt')
    return submitted(e)
  })
  on('session.append', (_, e, next) => {
    seen.rows.push(e.message.content[0].text)
    seen.order.push('rules')
    return next(e)
  })
  on('turn.abort', (_, e) => {
    seen.aborts.push(e.turnId)
    return abortRejects ? { deny: 'not the running turn' } : { value: undefined }
  })
  return { clock, seen, queue }
}

async function begin($, clock) {
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await clock.advance(1000)
}

test('an orchestrated worker never connects', async ($, on) => {
  const { clock, seen } = world(on, { env: { FM_TASK_ID: 'worker-1' } })
  await begin($, clock)
  expect(seen.polls.length).toBe(0)
})

test('a session polls with its live session ID', async ($, on) => {
  const { clock, seen } = world(on)
  await begin($, clock)
  expect(seen.polls[0]).toEqual({ instance: '4242', session: 'session-1', cwd: '/work/project', chosen: false, nonce: expect.any(String), promptedAt: 0 })
})

test('a poll says what the mod last heard about being chosen, so the gateway can correct it at once', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true }, { chosen: false }] })
  await begin($, clock)
  await clock.advance(1000)
  expect(seen.polls.slice(0, 3).map((poll) => poll.chosen)).toEqual([false, true, false])
})

test('a spoken prompt goes in as the owner, and its refusal reaches the gateway', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 1, submit: 'hello' }],
    submitted: () => ({ drop: 'the session was stopped and nobody has sent input since' }),
  })
  await begin($, clock)
  await clock.settle()
  expect(seen.events).toEqual([
    { kind: 'order', id: 1, accepted: true, busy: false },
    { kind: 'order', id: 1, drop: 'the session was stopped and nobody has sent input since' },
  ])
})

function asPrompts(seen) {
  return seen.submits.map((e) => e.text)
}

async function finish($, turnId) {
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId, reason: 'answer' })
}

test('a prompt spoken to a busy session is accepted at once, and waits for the turn to end', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true }, { chosen: true, id: 1, submit: 'hello' }] })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: '<task-notification>a wake</task-notification>', turnId: 'wake' })
  await clock.advance(1000)
  expect(seen.events).toEqual([{ kind: 'order', id: 1, accepted: true, busy: true }])
  expect(asPrompts(seen)).toEqual([])
  expect(seen.rows).toEqual([]) // the running turn doesn't get the voice rules
  await finish($, 'wake')
  await clock.settle()
  expect(asPrompts(seen)).toEqual(['hello'])
  expect(seen.order).toEqual(['rules', 'prompt'])
})

test('prompts spoken to a busy session go in once each, in order, each with a turn of its own', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true }, { chosen: true, id: 1, submit: 'one' }, { chosen: true, id: 2, submit: 'two' }, { chosen: true, id: 3, submit: 'three' }],
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'busy', turnId: 'busy' })
  await clock.advance(2000)
  expect(asPrompts(seen)).toEqual([])
  await finish($, 'busy')
  await clock.settle()
  expect(asPrompts(seen)).toEqual(['one'])
  await $.turn.start({ text: 'one', turnId: 't1' })
  await clock.advance(1000)
  expect(asPrompts(seen)).toEqual(['one'])
  await finish($, 't1')
  await clock.settle()
  expect(asPrompts(seen)).toEqual(['one', 'two'])
  await $.turn.start({ text: 'two', turnId: 't2' })
  await finish($, 't2')
  await clock.settle()
  expect(asPrompts(seen)).toEqual(['one', 'two', 'three'])
  await $.turn.start({ text: 'three', turnId: 't3' })
  await finish($, 't3')
  await clock.advance(1000)
  expect(asPrompts(seen)).toEqual(['one', 'two', 'three'])
  expect(seen.events.filter((e) => e.kind === 'turn-start').map((e) => e.turnId)).toEqual(['t1', 't2', 't3'])
})

async function typedPromptFailsThenSpokenOneGoesIn($, on, submitted) {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true }, { chosen: true, id: 1, submit: 'hello' }],
    submitted: (e) => (e.text === 'typed' ? submitted() : { text: 'ok' }),
  })
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await clock.advance(200)
  await clock.settle()
  await $.prompt.submit({ text: 'typed', origin: { kind: 'bridge' }, wait: false }).catch(() => {})
  await clock.advance(1000)
  await clock.settle()
  expect(asPrompts(seen)).toEqual(['typed', 'hello'])
}

test('a typed prompt the engine refuses does not hold up the spoken ones', async ($, on) => {
  await typedPromptFailsThenSpokenOneGoesIn($, on, () => ({ drop: 'the session was stopped' }))
})

test('a typed prompt that fails does not hold up the spoken ones', async ($, on) => {
  await typedPromptFailsThenSpokenOneGoesIn($, on, () => {
    throw new Error('no entry')
  })
})

test('a spoken turn names its order, so its timings stay its own', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 'a-1', submit: 'hello' }] })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await begin($, clock)
  await $.turn.start({ text: 'hello', turnId: 't1' })
  await clock.settle()
  expect(seen.events).toContainEqual({ kind: 'turn-start', turnId: 't1', order: 'a-1' })
})

test('an order the gateway sends twice runs once', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 'a-1', submit: 'hello' }, { chosen: true, id: 'a-1', submit: 'hello' }],
  })
  await begin($, clock)
  await clock.advance(1000)
  expect(asPrompts(seen)).toEqual(['hello'])
})

test('a tap drops the prompts still waiting, and says so', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true }, { chosen: true, id: 1, submit: 'one' }, { chosen: true, id: 2, abort: null }],
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: '<task-notification>a wake</task-notification>', turnId: 'wake' })
  await clock.advance(1000)
  expect(seen.aborts).toEqual([]) // never the turn the session started by itself
  await finish($, 'wake')
  await clock.advance(1000)
  expect(asPrompts(seen)).toEqual([])
  expect(seen.events).toContainEqual({ kind: 'order', id: 1, dropped: true })
  expect(seen.events).toContainEqual({ kind: 'order', id: 2, ok: true, dropped: true })
})

test('Stop drops the prompts still waiting, so nothing runs after the owner stopped', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true }, { chosen: true, id: 1, submit: 'one' }, { chosen: false }],
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'busy', turnId: 'busy' })
  await clock.advance(1000)
  await finish($, 'busy')
  await clock.advance(1000)
  await clock.settle()
  expect(asPrompts(seen)).toEqual([])
  expect(seen.events).toContainEqual({ kind: 'order', id: 1, dropped: true })
})

test('losing the gateway ends the grant and drops the waiting prompts, so it takes a new fingerprint', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true }, { chosen: true, id: 1, submit: 'one' }, { gatewayOff: true }, { chosen: true, grant: null, id: 2, submit: 'two' }],
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'busy', turnId: 'busy' })
  await clock.advance(1000)
  await finish($, 'busy')
  await clock.settle()
  expect(seen.statuses).toEqual(['Walkie-talkie on', undefined])
  await new Promise((resolve) => setTimeout(resolve, 2100)) // the retry goes by the real clock
  await clock.advance(1000)
  await clock.settle()
  expect(asPrompts(seen)).toEqual([])
  expect(seen.events).toContainEqual({ kind: 'order', id: 1, dropped: true })
  expect(seen.events).toContainEqual({ kind: 'grant-ended', reason: 'none' }) // the gateway that answers ends its run
  expect(seen.statuses).toEqual(['Walkie-talkie on', undefined])
})

test('a refused prompt takes the ones behind it with it, and says how many', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true }, { chosen: true, id: 1, submit: 'one' }, { chosen: true, id: 2, submit: 'two' }, { chosen: true, id: 3, submit: 'three' }],
    submitted: () => ({ drop: 'the session was stopped and nobody has sent input since' }),
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'busy', turnId: 'busy' })
  await clock.advance(2000)
  await finish($, 'busy')
  await clock.settle()
  expect(asPrompts(seen)).toEqual(['one'])
  const results = seen.events.filter((e) => e.kind === 'order' && !e.accepted)
  expect(results).toEqual([
    { kind: 'order', id: 1, drop: 'the session was stopped and nobody has sent input since', later: 2 },
    { kind: 'order', id: 2, dropped: true },
    { kind: 'order', id: 3, dropped: true },
  ])
})

const EARLY = 'spoken before your last reply finished'

test('a prompt spoken while the session was busy cannot confirm what that turn asks', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true }, { chosen: true, id: 1, submit: 'Yes, go ahead.' }] })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'Do the long work now.', turnId: 'busy' })
  await clock.advance(1000)
  await finish($, 'busy')
  await clock.settle()
  expect(seen.rows.length).toBe(1)
  expect(seen.rows[0]).toContain(EARLY)
  expect(seen.order).toEqual(['rules', 'prompt'])
})

test('a prompt the gateway marks as ahead of a reply cannot confirm it either', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 1, submit: 'Yes, go ahead.', unheard: true }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.events[0]).toEqual({ kind: 'order', id: 1, accepted: true, busy: false })
  expect(seen.rows[0]).toContain(EARLY)
})

test('a prompt spoken to an idle session after the reply can confirm it', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 1, submit: 'Yes, go ahead.' }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.rows.length).toBe(1)
  expect(seen.rows[0]).not.toContain(EARLY)
})

test('the voice rules ask for a specific word, not a bare yes', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 1, submit: 'hello' }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.rows[0]).toContain('one specific word')
  expect(seen.rows[0]).toContain("Don't ask for a bare yes")
})

test('a rejected abort does not stop the rest of the order', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 2, abort: 'not-the-running-one', submit: 'next' }],
    abortRejects: true,
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: '', turnId: 'not-the-running-one' })
  await clock.advance(1000)
  expect(seen.events.find((e) => e.kind === 'order' && e.error)?.error).toContain('not the running turn')
  expect(seen.events).toContainEqual({ kind: 'order', id: 2, accepted: true, busy: true })
})

test('a tap stops only the turn it names, not a turn the session started by itself', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 3, abort: 'spoken' }] })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: '<task-notification>done</task-notification>', turnId: 'machine' })
  await clock.advance(1000)
  expect(seen.aborts).toEqual([])
  expect(seen.events).toEqual([{ kind: 'order', id: 3, ok: true }])
})

test('a tap that names no turn leaves a turn the session started by itself alone', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 3, abort: null }] })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: '<task-notification>done</task-notification>', turnId: 'machine' })
  await clock.advance(1000)
  expect(seen.aborts).toEqual([])
})

test('a tap before a spoken prompt starts stops its turn as it starts', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 4, submit: 'hello' }, { chosen: true, id: 5, abort: null }],
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await begin($, clock)
  await $.turn.start({ text: 'hello', turnId: 'tapped' })
  expect(seen.aborts).toEqual(['tapped'])
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: true, turnId: 'tapped', reason: 'answer' })
  await clock.settle()
  expect(seen.events.filter((e) => e.kind === 'turn-start')).toEqual([])
})

function behindADialog() {
  return new Promise(() => {})
}

test('a prompt behind a dialog when the phone lets go stops as it starts', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 4, submit: 'hello' }, { chosen: false }, { chosen: false }],
    submitted: behindADialog,
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await begin($, clock)
  await clock.settle()
  expect(asPrompts(seen)).toEqual(['hello'])
  await $.turn.start({ text: 'hello', turnId: 'late' })
  expect(seen.aborts).toEqual(['late'])
  expect(seen.events.filter((e) => e.kind === 'turn-start')).toEqual([])
})

test('the phone letting go keeps an earlier tap on a prompt behind a dialog', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 4, submit: 'hello' }, { chosen: true, id: 5, abort: null }, { chosen: false }],
    submitted: behindADialog,
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await begin($, clock)
  await clock.settle()
  await $.turn.start({ text: 'hello', turnId: 'late' })
  expect(seen.aborts).toEqual(['late'])
})

test('losing the gateway stops a prompt behind a dialog as it starts, as the phone letting go does', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 4, submit: 'hello' }, { gatewayOff: true }],
    submitted: behindADialog,
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await begin($, clock)
  await clock.settle()
  await $.turn.start({ text: 'hello', turnId: 'late' })
  expect(seen.aborts).toEqual(['late'])
})

test('the phone letting go leaves a running spoken turn alone, and stops nothing typed after it', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 4, submit: 'hello' }, { chosen: true }, { chosen: false }, { chosen: false }],
    submitted: behindADialog,
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await clock.advance(200)
  await clock.settle()
  await $.turn.start({ text: 'hello', turnId: 'running' })
  await clock.advance(1000)
  await clock.settle()
  expect(seen.aborts).toEqual([])
  await finish($, 'running')
  await $.turn.start({ text: 'typed', turnId: 'typed' })
  expect(seen.aborts).toEqual([])
  expect(seen.events.filter((e) => e.kind === 'turn-start').map((e) => e.turnId)).toEqual(['running'])
})

test('a tap stops a spoken turn the gateway has not heard of yet', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 4, submit: 'hello' }, { chosen: true, id: 5, abort: null }],
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await clock.advance(200)
  await clock.settle()
  await $.turn.start({ text: 'hello', turnId: 'spoken' })
  expect(seen.aborts).toEqual([])
  await clock.advance(200)
  await clock.settle()
  expect(seen.aborts).toEqual(['spoken'])
})

// Bash answers when `finish` is called, as the engine does for a command a turn abort moves to the
// background. TaskStop is refused while that Bash call is open, as the engine refuses a tool call
// from inside the stopped turn.
function shell(on, seen) {
  seen.stops = []
  let finish = null
  let open = false
  on('tool.call', (_, e) => {
    if (e.tool === 'TaskStop') {
      if (open) return { deny: 'the turn was stopped' }
      seen.stops.push(e.task_id)
      return { result: { message: 'stopped' } }
    }
    if (e.command === 'background') return { result: { backgroundTaskId: 'task-background' } }
    open = true
    return new Promise((resolve) => {
      finish = (task) => {
        open = false
        resolve({ result: { backgroundTaskId: task } })
      }
    })
  })
  return { finish: (task) => finish(task) }
}

test('a tap stops a command the turn abort moved to the background, so the work ends there', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true }, { chosen: true, id: 5, abort: 'spoken' }] })
  const { finish } = shell(on, seen)
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: '', turnId: 'spoken' })
  const running = $.tool.call({ tool: 'Bash', command: './pause.sh' })
  await clock.advance(1000)
  expect(seen.aborts).toEqual(['spoken'])
  finish('task-running')
  await running
  await clock.advance(200)
  await clock.settle()
  expect(seen.stops).toEqual(['task-running'])
})

test('a tap stops a command the spoken turn had already left in the background', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true, id: 4, submit: 'hello' }, { chosen: true, id: 5, abort: 'spoken' }],
  })
  shell(on, seen)
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await clock.advance(200)
  await clock.settle()
  await $.turn.start({ text: 'hello', turnId: 'spoken' })
  await $.tool.call({ tool: 'Bash', command: 'background' })
  expect(seen.stops).toEqual([])
  await clock.advance(400)
  await clock.settle()
  expect(seen.aborts).toEqual(['spoken'])
  expect(seen.stops).toEqual(['task-background'])
})

test('a command keeps running without a tap, and in a turn the session started by itself', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true }, { chosen: true, id: 3, abort: null }] })
  shell(on, seen)
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: '<task-notification>done</task-notification>', turnId: 'machine' })
  await $.tool.call({ tool: 'Bash', command: 'background' })
  await clock.advance(1000)
  await clock.settle()
  expect(seen.aborts).toEqual([])
  expect(seen.stops).toEqual([])
})

test('when the gateway lets a session go, it drops the voice rules and stops streaming', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 6, submit: 'hello' }, { chosen: false }] })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await begin($, clock)
  await clock.settle()
  expect(seen.statuses).toEqual(['Walkie-talkie on', undefined])
  await $.prompt.submit({ text: 'typed later', origin: { kind: 'composer' }, wait: false })
  await $.turn.start({ text: 'typed later', turnId: 'later' })
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 'later', reason: 'answer' })
  await clock.settle()
  expect(seen.events.filter((e) => e.kind !== 'order')).toEqual([])
  expect(seen.rows.length).toBe(1) // the spoken prompt's, and none for the prompt typed after release
})

test('only a turn that answers the owner is sent to the gateway', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true }] })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  await begin($, clock)
  const composer = { kind: 'composer' }
  const task = { kind: 'task-notification' }

  await $.prompt.submit({ text: 'typed by a person', origin: composer, wait: false })
  await $.turn.start({ text: 'typed by a person', turnId: 'owner' })
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 'owner', reason: 'answer' })
  await $.prompt.submit({ text: '<task-notification>done</task-notification>', origin: task, wait: false })
  await $.turn.start({ text: '<task-notification>done</task-notification>', turnId: 'machine' })
  await $.turn.complete({ answer: 'noted', durationMs: 1, isAborted: false, turnId: 'machine', reason: 'answer' })
  await clock.settle()

  expect(seen.events).toEqual([
    { kind: 'turn-start', turnId: 'owner' },
    { kind: 'turn-end', turnId: 'owner', reason: 'answer' },
  ])
})

function rulesFor(on) {
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('prompt.compose', () => ({ sections: [{ id: 'base', text: 'base', scope: 'shared' }] }))
}

async function ownerTurn($, turnId, text = 'hello') {
  await $.turn.start({ text, turnId })
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId, reason: 'answer' })
}

const composeInput = {
  model: 'm',
  promptModel: 'm',
  surfaces: ['terminal'],
  tools: [],
  outputStyle: { name: 'default', isKeepingCodingInstructions: true },
  traits: [],
}

test('the voice rules reach the first spoken turn of a session that took turns before it was chosen', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: false }, { chosen: false }, { chosen: true, id: 7, submit: 'hello' }] })
  rulesFor(on)
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await clock.advance(200)
  await clock.settle()
  await ownerTurn($, 'before-choosing', 'typed earlier')
  expect(seen.rows).toEqual([])
  await clock.advance(1000)
  await clock.settle()
  await ownerTurn($, 'spoken')
  expect(seen.rows.length).toBe(1)
  expect(seen.rows[0]).toContain('out walking with earbuds')
})

test('the voice rules go in before each prompt that answers the owner, so its first request has them', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 8, submit: 'hello' }] })
  rulesFor(on)
  await begin($, clock)
  await clock.settle()
  expect(seen.order).toEqual(['rules', 'prompt'])
  await ownerTurn($, 'spoken')
  await $.prompt.submit({ text: 'typed', origin: { kind: 'composer' }, wait: false })
  expect(seen.order).toEqual(['rules', 'prompt', 'rules', 'prompt'])
  await ownerTurn($, 'typed', 'typed')
  await $.prompt.submit({ text: '<task-notification>done</task-notification>', origin: { kind: 'task-notification' }, wait: false })
  await ownerTurn($, 'machine', '<task-notification>done</task-notification>')
  expect(seen.rows.length).toBe(2)
})

test('a prompt typed over a running turn brings the voice rules into that turn once', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true }] })
  rulesFor(on)
  await begin($, clock)
  await $.turn.start({ text: '<task-notification>done</task-notification>', turnId: 'running' })
  expect(seen.rows.length).toBe(0)
  const typed = { text: 'typed over', origin: { kind: 'composer' }, turnId: 'running', wait: false }
  await $.prompt.submit(typed)
  await $.prompt.submit(typed)
  expect(seen.rows.length).toBe(1)
})

test('a session chosen before its first turn stops getting the voice rules at release', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 9, submit: 'hello' }, { chosen: false }] })
  rulesFor(on)
  await $.session.start({ cwd: '/work/project', surface: 'terminal', isInteractive: true })
  await clock.advance(200)
  await clock.settle()
  await ownerTurn($, 'spoken')
  expect(seen.rows.length).toBe(1)
  await clock.advance(200)
  await clock.settle()
  expect(seen.statuses).toEqual(['Walkie-talkie on', undefined])
  await $.prompt.submit({ text: 'typed later', origin: { kind: 'composer' }, wait: false })
  await ownerTurn($, 'later', 'typed later')
  expect(seen.rows.length).toBe(1)
})

test('choosing a session leaves its system prompt as sent', async ($, on) => {
  const { clock } = world(on, { orders: [{ chosen: true }] })
  rulesFor(on)
  await begin($, clock)
  const composed = await $.prompt.compose(composeInput)
  expect(composed.sections.map((s) => s.id)).toEqual(['base'])
})

// --- the front-desk role ----------------------------------------------------------------------

const DESK = {
  model: 'small-model',
  status: ['/tool', 'status'],
  handoff: ['/tool', 'note'],
  replies: ['/tool', 'replies', '--after', '{cursor}'],
}
const STATE = '/home/me/.local/state/walkie-talkie'
const CURSOR_FILE = `${STATE}/front-desk-cursor`

// A session the launcher started: it has a status and an inbox, and what it files is recorded.
function desk(on, { orders = [], model = () => ({ isAnswered: true, text: 'FILE' }), handoff = () => {}, handoffExit = 0, status = () => ({ exitCode: 0, stdout: 'Two workers are running.' }), replies = () => [], cursor = '000000000001' } = {}) {
  const seen = { notes: [], prompts: [], replyArgs: [], cursors: [] }
  const run = async (e) => {
    if (e.argv[0] !== '/tool') return { value: { exitCode: 0, stdout: '4242\n', stderr: '' } }
    if (e.argv[1] === 'status') return { value: { stderr: '', ...status() } }
    if (e.argv[1] === 'note') {
      seen.notes.push(e.argv[2])
      await handoff()
      return { value: { exitCode: handoffExit, stdout: '', stderr: '' } }
    }
    seen.replyArgs.push(e.argv.slice(1))
    return { value: { exitCode: 0, stdout: JSON.stringify({ replies: await replies() }), stderr: '' } }
  }
  const read = (path) => (path !== CURSOR_FILE ? undefined : cursor !== null ? { value: cursor } : { deny: 'no such file' })
  const made = world(on, { env: { WALKIE_FRONT_DESK: JSON.stringify(DESK) }, orders, run, read })
  Object.assign(seen, made.seen)

  on('fs.write', (_, e) => {
    seen.cursors.push(e.text)
    return { value: undefined }
  })
  on('model.complete', async (_, e) => {
    seen.prompts.push(e)
    return { value: await model(e) }
  })
  return { clock: made.clock, seen, queue: made.queue }
}

const spokenEvents = (seen) => seen.events.filter((e) => e.kind !== 'order')

test('the front desk tells the gateway its role', async ($, on) => {
  const { clock, seen } = desk(on)
  await begin($, clock)
  expect(seen.polls[0].role).toBe('front-desk')
})

test('a session that is no front desk names no role', async ($, on) => {
  const { clock, seen } = world(on)
  await begin($, clock)
  expect('role' in seen.polls[0]).toBe(false)
})

test('a question is answered from the cached status, with no turn and no hand-off', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [{ chosen: true, id: 1, submit: 'how many workers' }],
    model: () => ({ isAnswered: true, text: 'Two workers are running.' }),
  })
  await begin($, clock)
  await clock.settle()
  expect(seen.submits).toEqual([])
  expect(seen.notes).toEqual([])
  expect(seen.prompts[0].model).toBe('small-model')
  expect(seen.prompts[0].prompt).toContain('Two workers are running.')
  expect(seen.prompts[0].prompt).toContain('They just said: how many workers')
  expect(seen.events[0]).toEqual({ kind: 'order', id: 1, ok: true })
  expect(spokenEvents(seen)).toEqual([
    { kind: 'turn-start', turnId: 'desk-1', order: 1 },
    { kind: 'text', turnId: 'desk-1', text: 'Two workers are running.' },
    { kind: 'turn-end', turnId: 'desk-1', reason: 'answer' },
  ])
})

test('anything else is filed in the words as recognised, and says so in one line', async ($, on) => {
  const { clock, seen } = desk(on, { orders: [{ chosen: true, id: 2, submit: 'merge the release branch' }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.submits).toEqual([])
  expect(seen.notes.length).toBe(1)
  expect(seen.notes[0]).toContain('merge the release branch')
  expect(seen.notes[0]).toContain('Say yes to confirm')
  expect(spokenEvents(seen).find((e) => e.kind === 'text').text).toBe('Filed.')
})

test('a model that fails or stays silent still gets the prompt filed', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [{ chosen: true, id: 3, submit: 'what is going on' }],
    model: () => ({ isAnswered: false, reason: 'api-error' }),
  })
  await begin($, clock)
  await clock.settle()
  expect(seen.notes.length).toBe(1)
  expect(spokenEvents(seen).find((e) => e.kind === 'text').text).toBe('Filed.')
})

test('a hand-off that fails says it was not filed', async ($, on) => {
  const { clock, seen } = desk(on, { orders: [{ chosen: true, id: 4, submit: 'merge it' }], handoffExit: 1 })
  await begin($, clock)
  await clock.settle()
  expect(spokenEvents(seen).some((e) => e.kind === 'not-filed')).toBe(true)
  expect(spokenEvents(seen).some((e) => e.kind === 'text')).toBe(false)
})

test('the model sees the talk so far, but no more than a fixed number of lines', async ($, on) => {
  const orders = Array.from({ length: 10 }, (_, i) => ({ chosen: true, id: i + 1, submit: `what about ${i}?` }))
  const { clock, seen } = desk(on, { orders, model: () => ({ isAnswered: true, text: 'An answer.' }) })
  await begin($, clock)
  await clock.advance(3000)
  await clock.settle()
  const last = seen.prompts.at(-1).prompt
  expect(last).toContain('what about 8?')
  expect(last).toContain('what about 3?')
  expect(last).not.toContain('what about 2?')
})

test('prompts are answered one at a time, in order', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [{ chosen: true, id: 1, submit: 'is it first?' }, { chosen: true, id: 2, submit: 'is it second?' }],
    model: async (e) => {
      await clock.sleep(1000)
      return { isAnswered: true, text: e.prompt.endsWith('is it second?') ? 'Answer to second.' : 'Answer to first.' }
    },
  })
  await begin($, clock)
  await clock.advance(5000)
  await clock.settle()
  expect(spokenEvents(seen).filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(['Answer to first.', 'Answer to second.'])
})

test('a tap before the model answers files nothing', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [{ chosen: true, id: 1, submit: 'is it merged?' }, { chosen: true, id: 2, abort: null }],
    model: async () => {
      await clock.sleep(1000)
      return { isAnswered: true, text: 'FILE' }
    },
  })
  await begin($, clock)
  await clock.advance(5000)
  await clock.settle()
  expect(seen.notes).toEqual([])
  expect(seen.events).toContainEqual({ kind: 'order', id: 2, ok: true, dropped: 1 })
})

test('a tap counts only the prompts it stops, not one with the hand-off command', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [
      { chosen: true, id: 1, submit: 'merge it' },
      { chosen: true, id: 2, submit: 'post it' },
      { chosen: true, id: 3, abort: null },
      { chosen: true, id: 4, abort: null },
    ],
    handoff: () => clock.sleep(10000),
  })
  await begin($, clock)
  await clock.advance(15000)
  await clock.settle()
  expect(seen.notes.length).toBe(1)
  expect(seen.notes[0]).toContain('merge it')
  expect(seen.events).toContainEqual({ kind: 'order', id: 3, ok: true, dropped: 1 })
  expect(seen.events).toContainEqual({ kind: 'order', id: 4, ok: true, dropped: 0 })
  expect(spokenEvents(seen)).toContainEqual({ kind: 'text', turnId: 'desk-1', text: 'Filed.' })
})

for (const text of ['FILE', 'File.', 'file', 'Filed.', 'FILED', 'filing', "That isn't in the status. FILE", '', '   ']) {
  test(`a model reply of "${text}" gets the prompt filed`, async ($, on) => {
    const { clock, seen } = desk(on, {
      orders: [{ chosen: true, id: 1, submit: 'is it merged?' }],
      model: () => ({ isAnswered: true, text }),
    })
    await begin($, clock)
    await clock.settle()
    expect(seen.prompts.length).toBe(1)
    expect(seen.notes.length).toBe(1)
    expect(spokenEvents(seen).find((e) => e.kind === 'text').text).toBe('Filed.')
  })
}

for (const text of [
  'Probably a mishearing of build. The build is green. Is that what you meant?',
  'Is the build green?',
  Array(41).fill('word').join(' '),
  "The status says the worker is on the branch named release 2, but it doesn't say what it's doing right now. I'll pass that question to the primary.",
  'I passed that on.',
  'I will pass that on.',
  'I am going to check it.',
  'I’m sending that along.',
  'We’ll look at that soon.',
  'Let me check that.',
  'Passing that along.',
  'Checking with the primary now.',
  'Noted, the primary has it.',
  'Okay, merging now.',
  'The build is green, merging now.',
  'All done.',
  'Will do.',
]) {
  test(`a model answer that breaks the spoken rules is filed, not spoken: ${text.slice(0, 30)}`, async ($, on) => {
    const { clock, seen } = desk(on, {
      orders: [{ chosen: true, id: 1, submit: 'is the bill green' }],
      model: () => ({ isAnswered: true, text }),
    })
    await begin($, clock)
    await clock.settle()
    expect(seen.notes.length).toBe(1)
    expect(seen.notes[0]).toContain('is the bill green')
    expect(spokenEvents(seen).filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(['Filed.'])
  })
}

for (const text of [
  'The worker changed one file.',
  'Yes, an issue was filed this morning.',
  'File changes are pushed and tests pass.',
  'The primary is running the tests.',
  'Yes, the tests passed.',
  'The worker will open the pull request next.',
  'The worker’ll finish the tests soon.',
  'The worker is going to merge after the tests.',
  'The worker sent its report to the primary.',
  'The worker is merging the release branch.',
  'The worker is on the release branch, waiting on review.',
  'According to the status, two workers are running.',
  'Based on the status, the tests passed.',
  'Nothing is running.',
  'Staging is green.',
  'Failed on the last run.',
  Array(40).fill('word').join(' '),
]) {
  test(`an answer that states the status is spoken: ${text}`, async ($, on) => {
    const { clock, seen } = desk(on, {
      orders: [{ chosen: true, id: 1, submit: 'what changed' }],
      model: () => ({ isAnswered: true, text }),
    })
    await begin($, clock)
    await clock.settle()
    expect(seen.notes).toEqual([])
    expect(spokenEvents(seen).find((e) => e.kind === 'text').text).toBe(text)
  })
}

test('the model is not shown the word it could copy instead of filing', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [{ chosen: true, id: 1, submit: 'merge it' }, { chosen: true, id: 2, submit: 'is it merged?' }],
  })
  await begin($, clock)
  await clock.advance(1000)
  await clock.settle()
  expect(seen.prompts[0].prompt).toContain('They said: merge it')
  expect(seen.prompts[0].prompt).not.toContain('Filed.')
})

test('a new reply is relayed once, and the cursor moves past it', async ($, on) => {
  let waiting = [{ id: 'note-1', body: 'Say yes to merge.', cursor: '000000000002' }]
  const { clock, seen, queue } = desk(on, {
    orders: [{ chosen: true }],
    replies: () => {
      const out = waiting
      waiting = []
      return out
    },
  })
  await begin($, clock)
  await clock.advance(11000)
  await clock.settle()
  expect(seen.replyArgs[0]).toEqual(['replies', '--after', '000000000001'])
  expect(seen.replyArgs.at(-1)).toEqual(['replies', '--after', '000000000002'])
  expect(seen.events.filter((e) => e.kind === 'relay')).toEqual([{ kind: 'relay', note: 'note-1', text: 'Say yes to merge.' }])
  expect(seen.cursors).toEqual(['000000000002'])

  queue.push({ chosen: true, id: 10, submit: 'what did it say' })
  await clock.advance(1000)
  await clock.settle()
  expect(seen.prompts[0].prompt).toContain('The primary replied: Say yes to merge.')
})

test('a reply read as the session stops being chosen stays unread', async ($, on) => {
  const made = desk(on, {
    orders: [{ chosen: true }],
    replies: async () => {
      made.queue.push({ chosen: false })
      await made.clock.sleep(2000)
      return [{ id: 'note-1', body: 'Later.', cursor: '000000000002' }]
    },
  })
  const { clock, seen } = made
  await begin($, clock)
  await clock.advance(8000)
  await clock.settle()
  expect(seen.replyArgs.length).toBe(1)
  expect(seen.events.filter((e) => e.kind === 'relay')).toEqual([])
  expect(seen.cursors).toEqual([])
})

test('replies from before the first start are never spoken', async ($, on) => {
  let waiting = [{ id: 'old', body: 'Old news.', cursor: '000000000009' }]
  const { clock, seen } = desk(on, {
    cursor: null,
    orders: [{ chosen: true }],
    replies: () => {
      const out = waiting
      waiting = []
      return out
    },
  })
  await begin($, clock)
  await clock.advance(11000)
  await clock.settle()
  expect(seen.events.filter((e) => e.kind === 'relay')).toEqual([])
  expect(seen.cursors).toEqual(['000000000009'])
})

test('a first start with no replies is saved, so a restart knows it has caught up', async ($, on) => {
  const { clock, seen } = desk(on, { cursor: null, orders: [{ chosen: true }] })
  await begin($, clock)
  await clock.advance(6000)
  await clock.settle()
  expect(seen.cursors).toEqual([''])
})

test('after a first start with no replies, a reply is spoken even across a restart', async ($, on) => {
  const { clock, seen } = desk(on, {
    cursor: '',
    orders: [{ chosen: true }],
    replies: () => [{ id: 'note-1', body: 'Later.', cursor: '000000000002' }],
  })
  await begin($, clock)
  await clock.advance(6000)
  await clock.settle()
  expect(seen.replyArgs[0]).toEqual(['replies', '--after', ''])
  expect(seen.events).toContainEqual({ kind: 'relay', note: 'note-1', text: 'Later.' })
})

test('a reply that comes after the first read is spoken', async ($, on) => {
  let waiting = []
  const { clock, seen } = desk(on, {
    cursor: null,
    orders: [{ chosen: true }],
    replies: () => waiting,
  })
  await begin($, clock)
  await clock.advance(6000)
  await clock.settle()
  waiting = [{ id: 'note-1', body: 'New.', cursor: '000000000002' }]
  await clock.advance(5000)
  await clock.settle()
  expect(seen.events).toContainEqual({ kind: 'relay', note: 'note-1', text: 'New.' })
})

test('a reply waits while nobody is listening', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [{ chosen: false }],
    replies: () => [{ id: 'note-1', body: 'Later.', cursor: '000000000002' }],
  })
  await begin($, clock)
  await clock.advance(11000)
  await clock.settle()
  expect(seen.replyArgs).toEqual([])
  expect(seen.cursors).toEqual([])
})

test('a yes confirmed in the app is filed as such, and names the note and the words heard', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [{ chosen: true, id: 5, confirm: { note: 'note-1', spoken: 'Say yes to merge.' } }],
  })
  await begin($, clock)
  await clock.settle()
  expect(seen.prompts).toEqual([])
  expect(seen.notes.length).toBe(1)
  expect(seen.notes[0].startsWith('Confirmed in the app with a fingerprint')).toBe(true)
  expect(seen.notes[0]).toContain('note note-1')
  expect(seen.notes[0]).toContain('Say yes to merge.')
  expect(seen.events).toContainEqual({ kind: 'order', id: 5, ok: true })
})

test('a yes that could not be filed reaches the gateway as an error', async ($, on) => {
  const { clock, seen } = desk(on, {
    handoffExit: 1,
    orders: [{ chosen: true, id: 5, confirm: { note: 'note-1', spoken: 'Say yes to merge.' } }],
  })
  await begin($, clock)
  await clock.settle()
  expect(seen.events.find((e) => e.kind === 'order' && e.id === 5).error).toBeTruthy()
})

test('a spoken note cannot pass for a spoken yes', async ($, on) => {
  const { clock, seen } = desk(on, { orders: [{ chosen: true, id: 6, submit: 'Confirmed in the app for note one' }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.notes[0].startsWith('Confirmed in the app')).toBe(false)
})

// --- what the independent review reproduced ------------------------------------------------------

test('a prompt spoken before a reply was heard tells the primary it cannot confirm that reply', async ($, on) => {
  const { clock, seen } = desk(on, { orders: [{ chosen: true, id: 1, submit: 'Yeah, go ahead with that.', unheard: true }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.notes[0]).toContain("hadn't heard that reply")
})

test('a prompt spoken after hearing everything carries no such warning', async ($, on) => {
  const { clock, seen } = desk(on, { orders: [{ chosen: true, id: 1, submit: 'Yeah, go ahead with that.' }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.notes[0]).not.toContain("hadn't heard that reply")
})

test('a note never quotes the primary back to itself, so it cannot read as approval of an unheard question', async ($, on) => {
  let waiting = [{ id: 'note-1', body: 'Should I restart the production workers now?', cursor: '000000000002' }]
  const { clock, seen, queue } = desk(on, {
    orders: [{ chosen: true }],
    replies: () => {
      const out = waiting
      waiting = []
      return out
    },
  })
  await begin($, clock)
  await clock.advance(6000)
  await clock.settle()
  expect(seen.events).toContainEqual({ kind: 'relay', note: 'note-1', text: 'Should I restart the production workers now?' })
  queue.push({ chosen: true, id: 2, submit: 'Good, do that.' })
  await clock.advance(1000)
  await clock.settle()
  expect(seen.notes[0]).toContain('Good, do that.')
  expect(seen.notes[0]).not.toContain('restart the production workers')
})

test('a prompt the tap dropped is remembered as dropped, so the model cannot say it went through', async ($, on) => {
  const { clock, seen, queue } = desk(on, {
    orders: [{ chosen: true, id: 1, submit: 'is the launch update out?' }, { chosen: true, id: 2, abort: null }],
    model: async () => {
      await clock.sleep(1000)
      return { isAnswered: true, text: 'FILE' }
    },
  })
  await begin($, clock)
  await clock.advance(3000)
  await clock.settle()
  expect(seen.notes).toEqual([])
  queue.push({ chosen: true, id: 3, submit: 'did my last message go through?' })
  await clock.advance(3000)
  await clock.settle()
  const prompt = seen.prompts.at(-1).prompt
  expect(prompt).toContain('is the launch update out?')
  expect(prompt).toContain("Those words were dropped and didn't go to the primary.")
  expect(prompt).not.toContain('Those words went to the primary.')
})

test('a prompt behind the tap is remembered as dropped too', async ($, on) => {
  const { clock, seen, queue } = desk(on, {
    orders: [{ chosen: true, id: 1, submit: 'is it merged?' }, { chosen: true, id: 2, submit: 'post the update' }, { chosen: true, id: 3, abort: null }],
    model: async () => {
      await clock.sleep(1000)
      return { isAnswered: true, text: 'FILE' }
    },
  })
  await begin($, clock)
  await clock.advance(3000)
  await clock.settle()
  expect(seen.notes).toEqual([])
  queue.push({ chosen: true, id: 4, submit: 'what happened?' })
  await clock.advance(3000)
  await clock.settle()
  const lines = seen.prompts.at(-1).prompt.split('\n')
  expect(lines[lines.indexOf('They said: post the update') + 1]).toBe("Those words were dropped and didn't go to the primary.")
})

test('a failed hand-off is remembered as unknown, not as filed or as lost', async ($, on) => {
  const { clock, seen, queue } = desk(on, { orders: [{ chosen: true, id: 1, submit: 'merge it' }], handoffExit: 1 })
  await begin($, clock)
  await clock.settle()
  queue.push({ chosen: true, id: 2, submit: 'did that go through?' })
  await clock.advance(1000)
  await clock.settle()
  expect(seen.prompts.at(-1).prompt).toContain("can't tell whether those words were filed")
})

test('a request is filed with no model call, so no answer can claim it was done', async ($, on) => {
  const claims = ['Filed it.', 'Filing that now.', 'Sent to the primary.', 'That went to the primary.', 'Done.', 'Noted.', 'Okay.', 'Merging now.', 'The merge is underway.', 'The primary will do that.', 'Passed along.']
  let next = 0
  const orders = claims.map((_, i) => ({ chosen: true, id: i + 1, submit: 'merge pull request three' }))
  const { clock, seen } = desk(on, { orders, model: () => ({ isAnswered: true, text: claims[next++] }) })
  await begin($, clock)
  await clock.advance(5000)
  await clock.settle()
  expect(seen.prompts).toEqual([])
  expect(seen.notes.length).toBe(claims.length)
  expect(spokenEvents(seen).filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(claims.map(() => 'Filed.'))
})

for (const request of [
  'Can you post the update?',
  'Could you merge the release branch?',
  'Would you post the update?',
  'Will you merge it?',
  'Please post the update?',
  'Have the worker merge the release branch.',
  'Any chance you could merge it.',
]) {
  test(`a request shaped as a question is filed with no model call: ${request}`, async ($, on) => {
    const { clock, seen } = desk(on, { orders: [{ chosen: true, id: 1, submit: request }], model: () => ({ isAnswered: true, text: 'The update is posted.' }) })
    await begin($, clock)
    await clock.settle()
    expect(seen.prompts).toEqual([])
    expect(seen.notes[0]).toContain(request)
    expect(spokenEvents(seen).filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(['Filed.'])
  })
}

test('an answer to a question that claims to act is filed', async ($, on) => {
  const claims = ['Done.', 'Merging now.', 'Posting it now.', "It's done.", 'That is done.']
  let next = 0
  const orders = claims.map((_, i) => ({ chosen: true, id: i + 1, submit: 'is the update posted?' }))
  const { clock, seen } = desk(on, { orders, model: () => ({ isAnswered: true, text: claims[next++] }) })
  await begin($, clock)
  await clock.advance(5000)
  await clock.settle()
  expect(seen.prompts.length).toBe(claims.length)
  expect(seen.notes.length).toBe(claims.length)
  expect(spokenEvents(seen).filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(claims.map(() => 'Filed.'))
})

test('the rules send a question about filing to the primary', async ($, on) => {
  const { clock, seen } = desk(on, { orders: [{ chosen: true, id: 1, submit: 'did that post go to the primary?' }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.prompts[0].system).toContain('whether something was filed, sent, or received gets FILE')
  expect(seen.notes.length).toBe(1)
})

// It steps through a minute and more of the mod's 200 ms poll timer, which takes seconds on a busy Mac.
test('a question is filed, not answered, once the status is too old to trust', { timeoutMs: 30000 }, async ($, on) => {
  let reads = 0
  const { clock, seen, queue } = desk(on, {
    orders: [{ chosen: true }],
    status: () => (reads++ === 0 ? { exitCode: 0, stdout: 'Tests pass.' } : { exitCode: 2, stdout: '' }),
    model: () => ({ isAnswered: true, text: 'Yes, the tests are passing.' }),
  })
  await begin($, clock)
  await clock.advance(91000)
  await clock.settle()
  queue.push({ chosen: true, id: 1, submit: 'are the tests passing?' })
  await clock.advance(1000)
  await clock.settle()
  expect(seen.prompts).toEqual([])
  expect(seen.notes.length).toBe(1)
  expect(spokenEvents(seen).find((e) => e.kind === 'text').text).toBe('Filed.')
})

// It steps through a minute and more of the mod's 200 ms poll timer, which takes seconds on a busy Mac.
test('a question is still answered while a failing status is young', { timeoutMs: 30000 }, async ($, on) => {
  let reads = 0
  const { clock, seen, queue } = desk(on, {
    orders: [{ chosen: true }],
    status: () => (reads++ === 0 ? { exitCode: 0, stdout: 'Tests pass.' } : { exitCode: 2, stdout: '' }),
    model: () => ({ isAnswered: true, text: 'Yes, the tests are passing.' }),
  })
  await begin($, clock)
  await clock.advance(40000)
  await clock.settle()
  queue.push({ chosen: true, id: 1, submit: 'are the tests passing?' })
  await clock.advance(1000)
  await clock.settle()
  expect(seen.notes).toEqual([])
  expect(spokenEvents(seen).find((e) => e.kind === 'text').text).toBe('Yes, the tests are passing.')
})

test('the model is told when the status was cut', async ($, on) => {
  const { clock, seen } = desk(on, {
    orders: [{ chosen: true, id: 1, submit: 'are the tests passing?' }],
    status: () => ({ exitCode: 0, stdout: `${'All fine. '.repeat(700)}FAILURE: tests are red.` }),
  })
  await begin($, clock)
  await clock.settle()
  expect(seen.prompts[0].prompt).not.toContain('FAILURE')
  expect(seen.prompts[0].prompt).toContain("The status was cut off here, so it isn't complete.")
})

test('an uncut status carries no such warning', async ($, on) => {
  const { clock, seen } = desk(on, { orders: [{ chosen: true, id: 1, submit: 'are the tests passing?' }] })
  await begin($, clock)
  await clock.settle()
  expect(seen.prompts[0].prompt).not.toContain('cut off')
})

test('an answer holding what the filter leaves out is filed, not read as something on screen', async ($, on) => {
  const answers = [
    '`main`',
    'The build passed at 3f2a1b9c.',
    'It failed in src/gateway/phone.py.',
    'See /var/log/gateway.log for that.',
    'It is at https://example.com/run.',
    'The worker runs version 2.4.1.',
    'Set MODE=fast to do that.',
    'The value of $HOME is set.',
    'Use <prod-db> for that.',
    '2026-11-04 14:52 was the last build.',
    '+3 tests failed on the last run.',
    '@@ the last hunk changed.',
    '{"build": "green"} is the state.',
    '![build](badge) is green.',
    '    the tests are indented.',
    'Traceback (most recent call last) was the output.',
    '-----BEGIN KEY----- is in the output.',
  ]
  let next = 0
  const orders = answers.map((_, i) => ({ chosen: true, id: i + 1, submit: 'what is the state?' }))
  const { clock, seen } = desk(on, { orders, model: () => ({ isAnswered: true, text: answers[next++] }) })
  await begin($, clock)
  await clock.advance(5000)
  await clock.settle()
  expect(seen.notes.length).toBe(answers.length)
  expect(spokenEvents(seen).filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(answers.map(() => 'Filed.'))
})

test('an ordinary answer with numbers and names is still spoken', async ($, on) => {
  const answers = ['PR 214 for the 2.4 release is waiting for your review.', 'Two workers are running, and half of the setup guide is done.', 'The login fix and the docs work are both client/server changes.', 'Yes, an issue was filed this morning.']
  let next = 0
  const orders = answers.map((_, i) => ({ chosen: true, id: i + 1, submit: 'what is the state?' }))
  const { clock, seen } = desk(on, { orders, model: () => ({ isAnswered: true, text: answers[next++] }) })
  await begin($, clock)
  await clock.advance(5000)
  await clock.settle()
  expect(seen.notes).toEqual([])
  expect(spokenEvents(seen).filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(answers)
})

// --- who may connect, and only with a fingerprint ---------------------------------

const PY_KEY = 'MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEJkSQeP6tgEz5km223KdBAclhgVIN9kV8fx7WLU/7NBMjIS7rnrL6ueNaaSG18DDZ/VoLohyPowjNLoCqEPbJTg=='
const PY_MESSAGE = 'walkie-start|gateway-nonce|run-1|4242|mod-nonce'
const PY_SIG = 'MEUCIFG6dntm0MXrv5CttjCBiGmZ4H2BULdS29zqqiEw+RVmAiEAoVrPWQHUsNakYaWhX9b8GOCGcjVVawjTHIcUjr/RCiQ='

test('the check accepts a signature as Android and the gateway make it, and nothing else', async () => {
  expect(verify(PY_KEY, PY_MESSAGE, PY_SIG)).toBe(true)
  expect(verify(PY_KEY, `${PY_MESSAGE}x`, PY_SIG)).toBe(false)
  expect(verify(PHONE.public, PY_MESSAGE, PY_SIG)).toBe(false)
  const flipped = atob(PY_SIG).split('')
  flipped[10] = String.fromCharCode(flipped[10].charCodeAt(0) ^ 1)
  expect(verify(PY_KEY, PY_MESSAGE, btoa(flipped.join('')))).toBe(false)
  for (const bad of ['', 'not base64!', 'MAYCAQACAQA=', btoa('\x30\x06\x02\x01\x00\x02\x01\x01'), PY_SIG.slice(0, -8)]) {
    expect(verify(PY_KEY, PY_MESSAGE, bad)).toBe(false)
  }
  for (const bad of ['', PY_KEY.slice(0, -8), PHONE.public.replace('A', 'B'), null]) expect(verify(bad, PY_MESSAGE, PY_SIG)).toBe(false)
  expect(verify(PHONE.public, 'hello', await PHONE.sign('hello'))).toBe(true)
  expect(verify(OTHER_PHONE.public, 'hello', await PHONE.sign('hello'))).toBe(false)
})

test('its SHA-256 matches the standard', async () => {
  const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  const of = (text) => hex(sha256(new TextEncoder().encode(text)))
  expect(of('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
  expect(of('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
  const long = 'a'.repeat(1000)
  expect(of(long)).toBe(hex(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(long)))))
})

async function quiet($, on, options) {
  const { clock, seen } = world(on, options)
  await begin($, clock)
  return seen
}

test('a login the owner did not name never connects', async ($, on) => {
  const seen = await quiet($, on, { env: { CLAUDE_CONFIG_DIR: '/home/me/.claude-other' } })
  expect(seen.polls.length).toBe(0)
})

test('the default login connects only when named', async ($, on) => {
  const named = await quiet($, on, { env: { CLAUDE_CONFIG_DIR: '' } })
  expect(named.polls.length).toBe(1)
})

test('the default login stays out when only the other one is named', async ($, on) => {
  const seen = await quiet($, on, { env: { CLAUDE_CONFIG_DIR: '' }, config: { logins: { [PERSONAL]: { deny: [] } }, deny: [] } })
  expect(seen.polls.length).toBe(0)
})

for (const [what, config] of [
  ['no settings file', null],
  ['a broken settings file', 'not json'],
  ['settings with no deny list', { logins: { [PERSONAL]: {} } }],
  ['a login with no deny list', { logins: { [PERSONAL]: {} }, deny: [] }],
  ['logins as a plain list', { logins: [PERSONAL], deny: [] }],
]) {
  test(`with ${what}, nothing connects`, async ($, on) => {
    const seen = await quiet($, on, { config })
    expect(seen.polls.length).toBe(0)
  })
}

test('a headless run never connects', async ($, on) => {
  const seen = await quiet($, on, { surfaces: [] })
  expect(seen.polls.length).toBe(0)
})

const denied = (deny, perLogin = []) => ({ logins: { [WORK]: { deny: [] }, [PERSONAL]: { deny: perLogin } }, deny })

test('a denied folder is never listed, and its name never reaches the gateway', async ($, on) => {
  const seen = await quiet($, on, { config: denied(['secret-project']), cwd: () => '/work/Secret-Project/src' })
  expect(seen.polls.length).toBe(0)
  expect(seen.events).toEqual([])
})

for (const name of ['made-up-employer', 'tool']) {
  test(`a repository whose remote names ${name} is never listed, under any folder name`, async ($, on) => {
    const remotes = 'origin\thttps://github.com/made-up-employer/tool.git (fetch)\nupstream\tgit@github.com:made-up-employer/tool (push)\n'
    const run = (e) => ({ value: { exitCode: 0, stdout: e.argv[0] === 'git' ? remotes : '4242\n', stderr: '' } })
    const seen = await quiet($, on, { config: denied([], [name]), run })
    expect(seen.polls.length).toBe(0)
  })
}

test('a deny on one login leaves the other login alone', async ($, on) => {
  const seen = await quiet($, on, { env: { CLAUDE_CONFIG_DIR: WORK }, config: denied([], ['project']) })
  expect(seen.polls.length).toBe(1)
})

test('when git cannot be asked, a session with a deny list stays out', async ($, on) => {
  const run = (e) => {
    if (e.argv[0] === 'git') throw new Error('no git')
    return { value: { exitCode: 0, stdout: '4242\n', stderr: '' } }
  }
  const seen = await quiet($, on, { config: denied(['secret-project']), run })
  expect(seen.polls.length).toBe(0)
})

for (const [what, stderr, polls] of [
  ['a folder that is no repository connects', 'fatal: not a git repository (or any of the parent directories): .git\n', 1],
  ['a repository git refuses to read stays out', 'fatal: detected dubious ownership in repository at /work/project\n', 0],
  ['a repository whose git config is broken stays out', 'fatal: bad config line 1 in file .git/config\n', 0],
]) {
  test(`with a deny list, ${what}`, async ($, on) => {
    const run = (e) => ({ value: e.argv[0] === 'git' ? { exitCode: 128, stdout: '', stderr } : { exitCode: 0, stdout: '4242\n', stderr: '' } })
    const seen = await quiet($, on, { config: denied(['secret-project']), run })
    expect(seen.polls.length).toBe(polls)
  })
}

test('git answers in English, so a folder that is no repository is known in any language', async ($, on) => {
  const gits = []
  const run = (e) => {
    if (e.argv[0] === 'git') gits.push(e.init.env)
    return { value: { exitCode: 0, stdout: '4242\n', stderr: '' } }
  }
  await quiet($, on, { config: denied(['secret-project']), run })
  expect(gits).toEqual([{ LC_ALL: 'C', LANG: 'C' }])
})

test('a session that comes back from a denied folder is listed again', async ($, on) => {
  let folder = '/work/project'
  const { clock, seen } = world(on, { config: denied(['secret-project']), cwd: () => folder, orders: Array(100).fill({ chosen: false }) })
  await begin($, clock)
  folder = '/work/secret-project'
  await clock.advance(1000)
  const whileDenied = seen.polls.length
  await clock.advance(1000)
  expect(seen.polls.length).toBe(whileDenied)
  folder = '/work/project'
  await clock.advance(1000)
  expect(seen.polls.length).toBeGreaterThan(whileDenied)
  expect(seen.polls.at(-1).cwd).toBe('/work/project')
})

test('a session that moves into a denied folder lets go, and says goodbye under its old folder', async ($, on) => {
  let folder = '/work/project'
  const { clock, seen } = world(on, { config: denied(['secret-project']), cwd: () => folder, orders: Array(20).fill({ chosen: true }) })
  await begin($, clock)
  await clock.settle()
  expect(seen.statuses).toContain('Walkie-talkie on')
  folder = '/work/secret-project'
  await clock.advance(2000)
  await clock.settle()
  expect(seen.statuses.at(-1)).toBe(undefined)
  expect(seen.events.map((e) => e.kind)).toContain('bye')
  expect(JSON.stringify([...seen.polls, ...seen.events])).not.toContain('secret-project')
})

test('a gateway that says chosen, with no fingerprint behind it, gets nothing in', async ($, on) => {
  const { clock, seen } = world(on, { grants: false, orders: [{ chosen: true, id: 1, submit: 'run something' }, { chosen: true, id: 2, type: 'run it' }] })
  await begin($, clock)
  await clock.advance(1000)
  await clock.settle()
  expect(asPrompts(seen)).toEqual([])
  expect(seen.statuses).not.toContain('Walkie-talkie on')
  expect(seen.events).toContainEqual({ kind: 'grant-ended', reason: 'none' })
  expect(seen.events.filter((e) => e.kind === 'grant-ended').length).toBe(1)
})

for (const [what, make] of [
  ['signed by another key', (poll) => grantFor(poll, OTHER_PHONE)],
  ['for another session', (poll) => grantFor(poll, PHONE, { session: '999' })],
  ['over another challenge', (poll) => grantFor(poll, PHONE, { modNonce: 'old-challenge' })],
  ['changed after signing', async (poll) => ({ ...(await grantFor(poll)), run: 'another-run' })],
]) {
  test(`a grant ${what} gets nothing in`, async ($, on) => {
    const orders = [
      async (poll) => {
        const grant = await make(poll)
        return { chosen: true, grant, run: grant.run, id: 1, submit: 'hi' }
      },
    ]
    const { clock, seen } = world(on, { grants: false, orders })
    await begin($, clock)
    await clock.settle()
    expect(asPrompts(seen)).toEqual([])
    expect(seen.statuses).not.toContain('Walkie-talkie on')
  })
}

test('with no phone key on file, no grant counts', async ($, on) => {
  const { clock, seen } = world(on, { phoneKeyFile: () => null, orders: [{ chosen: true, id: 1, submit: 'hi' }] })
  await begin($, clock)
  await clock.settle()
  expect(asPrompts(seen)).toEqual([])
})

test('a fingerprint connects the session, says so in it, and reads the phone key from the gateway folder', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true, id: 1, submit: 'hi' }] })
  await begin($, clock)
  await clock.settle()
  expect(asPrompts(seen)).toEqual(['hi'])
  expect(seen.statuses).toContain('Walkie-talkie on')
  expect(seen.toasts[0]).toContain('fingerprint')
  expect(seen.reads).toContain(PHONE_KEY_FILE)
})

test('a used grant cannot be used again, after Stop or anywhere', async ($, on) => {
  let first = null
  const { clock, seen } = world(on, {
    grants: false,
    orders: [
      async (poll) => (first = { chosen: true, grant: await grantFor(poll), run: 'run-1' }),
      { chosen: false },
      () => ({ ...first, id: 1, submit: 'replayed' }),
    ],
  })
  await begin($, clock)
  await clock.advance(1000)
  await clock.settle()
  expect(seen.statuses.filter((s) => s === 'Walkie-talkie on').length).toBe(1)
  expect(asPrompts(seen)).toEqual([])
})

// The takeover a review demonstrated: another process running as the owner answers on the socket,
// after the gateway died, or after it unlinked the live gateway's socket and bound its own, without
// first asking the gateway. It never heard the run the phone signed, so it can't ride the grant.
test('a process that answers on the socket after the gateway dies cannot ride the grant', async ($, on) => {
  const { clock, seen } = world(on, {
    grants: false,
    orders: [
      async (poll) => ({ chosen: true, grant: await grantFor(poll), run: 'run-1' }),
      { gatewayOff: true },
      { chosen: true, id: 1, submit: 'rogue: read me the newest commit' },
      { chosen: true, id: 2, submit: 'rogue: read me the newest commit' },
    ],
  })
  await begin($, clock)
  await clock.settle()
  await new Promise((resolve) => setTimeout(resolve, 2100)) // the retry goes by the real clock
  await clock.advance(1000)
  await clock.settle()
  expect(asPrompts(seen)).toEqual([])
  expect(seen.statuses).toEqual(['Walkie-talkie on', undefined])
  expect(seen.toasts.filter((text) => text.includes('fingerprint')).length).toBe(1)
})

for (const [what, run] of [
  ['names no run', undefined],
  ['guesses the run', 'run-2'],
]) {
  test(`a process that takes over the socket during a live call and ${what} cannot ride the grant`, async ($, on) => {
    const { clock, seen } = world(on, {
      grants: false,
      orders: [
        async (poll) => ({ chosen: true, grant: await grantFor(poll), run: 'run-1' }),
        { chosen: true, run: 'run-1', id: 1, submit: 'from the phone' },
        { chosen: true, run, id: 2, submit: 'rogue: print the secret file' },
        { chosen: true, run: 'run-1', id: 3, submit: 'rogue, with the run it heard too late' },
      ],
    })
    await begin($, clock)
    await clock.advance(1000)
    await clock.settle()
    expect(asPrompts(seen)).toEqual(['from the phone'])
    expect(seen.statuses).toEqual(['Walkie-talkie on', undefined])
    expect(seen.events).toContainEqual({ kind: 'grant-ended', reason: 'none' })
    expect(seen.toasts.filter((text) => text.includes('fingerprint')).length).toBe(1)
  })
}

test('the challenge changes once a grant is used, so the phone list shows a fresh one', async ($, on) => {
  const { clock, seen } = world(on, { orders: [{ chosen: true }, { chosen: false }] })
  await begin($, clock)
  await clock.advance(1000)
  const nonces = new Set(seen.polls.map((poll) => poll.nonce))
  expect(nonces.size).toBeGreaterThan(1)
})

test('no grant survives Stop: being chosen again needs a new fingerprint', async ($, on) => {
  const { clock, seen } = world(on, {
    orders: [{ chosen: true }, { chosen: false }, { chosen: true, grant: null, id: 1, submit: 'after stop' }],
  })
  await begin($, clock)
  await clock.advance(1000)
  await clock.settle()
  expect(asPrompts(seen)).toEqual([])
  expect(seen.statuses).toEqual(['Walkie-talkie on', undefined])
})

const LONG_SILENCE_MS = 35 * 60 * 1000

for (const [what, running, ended] of [
  ['a reply still on its way keeps the grant past the idle limit', true, false],
  ['silence with no reply on its way ends the grant', false, true],
]) {
  test(what, { timeoutMs: 120000 }, async ($, on) => {
    const polls = LONG_SILENCE_MS / 200 + 100
    const orders = [{ chosen: true, id: 1, submit: 'run the full test suite' }, ...Array(polls).fill({ chosen: true })]
    const { clock, seen } = world(on, { orders })
    on('turn.start', (_, e) => ({ turnId: e.turnId }))
    await begin($, clock)
    await clock.settle()
    if (running) await $.turn.start({ text: 'run the full test suite', turnId: 'long' })
    for (let minute = 0; minute < LONG_SILENCE_MS / 60000; minute++) await clock.advance(60000)
    await clock.settle()
    expect(seen.statuses).toEqual(ended ? ['Walkie-talkie on', undefined] : ['Walkie-talkie on'])
    expect(seen.events.some((e) => e.kind === 'grant-ended')).toBe(ended)
  })
}

test('a /clear ends the grant: the phone chose the conversation before it', async ($, on) => {
  let id = 'session-1'
  let cleared = false
  const afterClear = async () => {
    while (!cleared) await clock.sleep(100)
    return { chosen: true, id: 1, submit: 'after clear' }
  }
  const { clock, seen } = world(on, { sessionId: () => id, orders: [{ chosen: true }, afterClear] })
  on('session.end', () => ({ sessionId: 'session-1' }))
  await begin($, clock)
  await clock.settle()
  expect(seen.statuses).toEqual(['Walkie-talkie on'])
  id = 'session-2'
  await $.session.end({ reason: 'clear' })
  cleared = true
  await clock.advance(1000)
  await clock.settle()
  expect(asPrompts(seen)).toEqual([])
  expect(seen.events).toContainEqual({ kind: 'grant-ended', reason: 'clear' })
})
