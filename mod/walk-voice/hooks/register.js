// Connects one live session to the walkie-talkie gateway on this Mac, over a
// Unix socket in a directory only this user can enter. It stays inert in an
// orchestrated worker, in a login that ~/.config/walkie-talkie/connect.json
// doesn't name, in a headless run, and in a folder that file denies, whose name
// never leaves the session. It does nothing visible while no gateway is running.
//
// A socket's word alone can't choose this session. A fingerprint on the phone signs a
// challenge this mod made, and the mod checks that signature against the phone's
// public key before it takes any order but a tap. That grant lives only in this
// module's memory, and holds only while each answer names the run the phone signed.
// So something that answers on the socket without having asked the gateway, such as a
// listener after the gateway dies, can't ride the grant. A process running as the owner
// that asks the gateway in this session's name can: see "What this can't stop" in
// docs/security.md. The grant ends at Stop, at /clear,
// at any poll that fails or doesn't name that run, after IDLE_MS with nothing said,
// after GRANT_MS at most, and with a reload. Then the next Start takes a new
// fingerprint.
//
// Inbound: the gateway answers a long poll with an order: submit, abort, type, or confirm.
// Outbound: only for the session the phone chose, and only for turns that answer
// the owner: text pieces, turn ends, and order results.
//
// A spoken prompt is accepted at once, and waits here until the session is idle.
// The engine's own queue would keep it too, but it runs the voice rules into the
// running turn, can fold several prompts into one turn, and says nothing until the
// prompt leaves the queue, so the speaker can't tell queued from lost.
//
// This mod approves nothing, and it never touches a permission dialog.
//
// A session the front-desk launcher starts takes the role in front-desk.js instead of turns.

import { deskTap, fileYes, startDesk, takePrompt } from './front-desk.js'
import { verify } from './p256.js'

const NAME = 'walk-voice'
const POLL_EVERY_MS = 200
const RETRY_AFTER_MS = 2000
const ENTER_AFTER_MS = 100 // lets the terminal draw the filled box before the Enter
const MAX_SEEN_ORDERS = 100
const GRANT_MS = 4 * 60 * 60 * 1000 // the longest a fingerprint lasts, as the phone's own limit on a run
const IDLE_MS = 31 * 60 * 1000 // the gateway ends a run after 30 minutes with nothing said; this backs it up
const CONNECTED = 'Walkie-talkie connected after a fingerprint on the phone. Spoken words now reach this session.'
const ENDED = 'Walkie-talkie ended.'

// Turns the session starts by itself: a background task's notice, or an orchestrator's doorbell.
const MACHINE_PROMPT = /^\s*(<task-notification>|: Firstmate operational input waiting:)/

const VOICE_RULES = `Apply these rules to your reply to the prompt next to this note, and to no later reply. The person is out walking with earbuds and talks to you through a voice app. They can't see the screen. Your text is read aloud to them as you write it.

Write for the ear:

- Start with the verdict or answer in one sentence: done, not done, blocked, or the answer itself.
- Then give at most three short points: what changed, whether you verified it, and what needs their decision.
- Keep the whole reply under about 100 words, in plain conversational sentences.
- No code, commands, file paths, URLs, diffs, logs, tables, credentials, hashes, or version strings. They're dropped, so describe them in words instead.
- Their prompts are recognised speech, so read past misheard words, and ask when the meaning is unclear.
- If you're blocked, say so and ask the one question that unblocks you.
- Before anything destructive, say in one sentence what you need confirmed, naming it in words, and ask them to say one specific word back. Don't ask for a bare yes.`

export const SPOKEN_EARLY = `This message was spoken before your last reply finished, so they hadn't heard that reply. It can't confirm anything in that reply.`

let socket = null // the gateway's socket, null while this session stays inert
let stateDir = null // the gateway's folder, which holds its socket and the phone's public key
let deny = [] // folder names whose sessions never connect
let loginName = '' // a short name for this session's login, which the phone shows beside its folder
let instance = null // this process, stable across /clear and reloads
let chosen = false // the phone chose this session, and this mod checked the fingerprint that did
let heardChosen = false // what the gateway last said, which is no grant on its own
let nonce = null // this session's challenge, which the phone signs to choose it
let grant = null // { session, run, until }, the checked fingerprint's grant: only in memory, so a reload forgets it
let activeAt = 0 // when anything was last said or heard while chosen
let promptedAt = 0 // when this session last took a prompt, so the phone can list it first
let shownCwd = null // the last folder that may be named outside this session
let checkedCwd = null // the folder last checked, so each folder the session enters is checked once
let denied = true // until the folder is checked
let polling = false
let retryAt = 0
let runningTurn = null // the main loop's running turn
let spokenTurn = null // the running turn whose text is read aloud
let spokenTasks = [] // commands the running spoken turn left in the background
let tappedTurn = null // the turn a tap stopped, whose commands get stopped as they come back
let tasksToStop = [] // commands a tap left to stop, on the next tick
let ownerPrompts = 0 // prompts from the owner that haven't started their turn yet
let stopAtStart = 0 // how many of those a tap stopped before they started
let waiting = [] // spoken prompts held until the session is idle, oldest first, each with whether it came early
let inFlight = false // a spoken prompt has gone to the engine and hasn't been answered yet
let startingOrder = null // the order of the spoken prompt whose turn comes next, so its timings stay its own
const seenOrders = new Set() // so an order the gateway sends twice runs once
let outbox = []
let sending = false
let isDesk = false // this session has the front-desk role, because the launcher started it for that

function request($, path, body) {
  return $.http.fetch(`http://walkie${path}`, {
    method: 'POST',
    socketPath: socket,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
}

// One batch in flight at a time, so events reach the gateway in order. Never
// awaited by a hook: a gateway that's off changes nothing in the session.
export function post($, kind, fields) {
  if (!socket || denied) return
  outbox.push({ kind, ...fields })
  if (!sending) void flush($)
}

async function flush($) {
  sending = true
  while (outbox.length) {
    const events = outbox
    outbox = []
    try {
      await request($, '/event', { instance, session: await $.session.id(), cwd: shownCwd, events })
    } catch {
      break // the gateway is off: these events have nobody to reach
    }
  }
  outbox = []
  sending = false
}

export function isChosen() {
  return chosen
}

function setChosen($, value) {
  if (value === chosen) return
  chosen = value
  $.ui.status(chosen ? 'Walkie-talkie on' : undefined)
  $.ui.toast(chosen ? CONNECTED : ENDED)
}

function newNonce() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('')
}

// The grant ends, and with it every order but a tap. The challenge changes, so its signature can't be used again.
function endGrant($) {
  grant = null
  nonce = newNonce()
  setChosen($, false)
  letGo()
  dropWaiting($)
}

async function granted($) {
  if (!grant) return false
  const now = await $.clock.now()
  if (spokenTurn !== null || inFlight) activeAt = now // a reply on its way counts: the owner is waiting for it
  if (now > grant.until || now - activeAt > IDLE_MS || grant.session !== (await $.session.id())) endGrant($)
  return grant !== null
}

// The phone signed this session's challenge after a fingerprint. Only a signature from the paired
// phone's key counts, so an older gateway can't choose this session. A process running as the
// owner still can, for example by writing phone.pub: see "What this can't stop" in docs/security.md.
async function takeGrant($, offer) {
  const ok =
    offer !== null &&
    typeof offer === 'object' &&
    offer.session === instance &&
    offer.modNonce === nonce &&
    [offer.nonce, offer.run, offer.sig].every((part) => typeof part === 'string' && part && !part.includes('|')) &&
    verify(
      (await $.fs.read(`${stateDir}/phone.pub`).catch(() => '')).trim(),
      ['walkie-start', offer.nonce, offer.run, instance, nonce].join('|'),
      offer.sig,
    )
  nonce = newNonce() // used, whatever the answer
  if (!ok) return false
  const now = await $.clock.now()
  grant = { session: await $.session.id(), run: offer.run, until: now + GRANT_MS }
  activeAt = now
  return true
}

function active($) {
  void $.clock.now().then((now) => {
    activeAt = now
  })
}

// A folder on the deny list never connects, and its name never leaves the session. A name matches
// any folder in the path, and the owner or repository of any git remote, so a copy under another
// folder name is still known, and one name can cover an owner's every repository. When git can't
// be asked, the answer is no.
async function deniedFolder($, cwd) {
  if (!deny.length) return false
  const parts = cwd.toLowerCase().split('/')
  if (deny.some((name) => parts.includes(name))) return true
  try {
    const result = await $.process.run(['git', '-C', cwd, 'remote', '-v'], { timeoutMs: 5000, env: { LC_ALL: 'C', LANG: 'C' } })
    if (result.exitCode !== 0) return !/not a git repository/i.test(result.stderr ?? '')
    const names = result.stdout
      .toLowerCase()
      .split(/\s+/)
      .flatMap((url) => url.replace(/\.git\/*$/, '').split(/[/:]/))
    return names.some((name) => deny.includes(name))
  } catch {
    return true
  }
}

// Checked whenever the folder changes. A session that moves into a denied folder lets go, and says
// goodbye under the last folder it was allowed to name.
async function folderAllowed($) {
  const cwd = await $.session.cwd()
  if (cwd === checkedCwd) return !denied
  checkedCwd = cwd
  if (await deniedFolder($, cwd)) {
    if (!denied) {
      endGrant($)
      post($, 'bye', {})
    }
    denied = true
  } else {
    denied = false
    shownCwd = cwd
  }
  return !denied
}

function forgetOwnerTurns() {
  spokenTurn = null
  ownerPrompts = 0
  stopAtStart = 0
}

// The phone let this session go. What a tap already stopped, and a spoken prompt still with the
// engine, stop as they start: nobody would hear them. A turn already running carries on.
function letGo() {
  const pending = inFlight && startingOrder !== null ? 1 : 0
  spokenTurn = null
  stopAtStart = ownerPrompts = Math.max(stopAtStart, pending)
}

function promptRefused() {
  ownerPrompts = Math.max(0, ownerPrompts - 1)
  stopAtStart = Math.min(stopAtStart, ownerPrompts)
}

// The rules ride beside each prompt that answers the owner, not in the system prompt: the engine
// keeps a thread's recorded system prompt for the whole thread, so a change there never reaches a
// session that has taken a turn, and never leaves one that was chosen before its first. A new row
// keeps the prompt cache, survives compaction and /clear, and stops by itself at release. It goes
// in before the prompt: a turn builds its first request before turn.start, so a row added there
// would miss that request.
async function addVoiceRules($, early = false) {
  const text = early ? `${VOICE_RULES}\n\n${SPOKEN_EARLY}` : VOICE_RULES
  await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } }).catch(() => {})
}

function dropWaiting($) {
  const count = waiting.length
  for (const prompt of waiting) post($, 'order', { id: prompt.id, dropped: true })
  waiting = []
  return count
}

async function pollOnce($) {
  if (polling || !socket || Date.now() < retryAt) return
  polling = true
  try {
    if (!(await folderAllowed($))) return
    // The session's ID is read on every poll: session.start doesn't fire after /clear.
    const response = await request($, '/next', {
      instance,
      session: await $.session.id(),
      cwd: shownCwd,
      chosen: heardChosen,
      nonce,
      promptedAt,
      login: loginName,
      role: isDesk ? 'front-desk' : undefined,
    })
    if (!response.ok) throw new Error(`status ${response.status}`)
    const order = JSON.parse(response.text)
    const fresh = order.grant !== undefined && (await takeGrant($, order.grant))
    const wasHeard = heardChosen
    const wasChosen = chosen
    heardChosen = order.chosen === true
    // Stop, the run ended, or an answer that doesn't name the run: no grant survives it.
    if (grant && !(heardChosen && order.run === grant.run)) endGrant($)
    // Only a signature checked in this same answer makes the session chosen.
    const ok = heardChosen && (chosen || fresh) && (await granted($))
    // The gateway thinks this session is chosen, with no grant here: a reload, a lost connection,
    // an idle end, or an older gateway.
    if (heardChosen && !ok && (!wasHeard || wasChosen)) post($, 'grant-ended', { reason: 'none' })
    setChosen($, ok)
    if (!chosen) {
      letGo()
      dropWaiting($)
    }
    if (order.id !== undefined) {
      if (seenOrders.has(order.id)) return
      seenOrders.add(order.id)
      if (seenOrders.size > MAX_SEEN_ORDERS) seenOrders.delete(seenOrders.values().next().value)
    }
    if (chosen && (order.submit || order.abort !== undefined || order.type || order.confirm)) active($)
    if (isDesk) {
      if (order.abort !== undefined) deskTap($, order.id)
      if (order.submit && chosen) takePrompt($, order.id, order.submit, order.unheard === true)
      if (order.confirm && chosen) void fileYes($, order.id, order.confirm)
      return
    }
    if (order.abort !== undefined) await abortTurn($, order.id, order.abort)
    if (order.submit && chosen) submit($, order.id, order.submit, order.unheard === true)
    if (order.type && chosen) void typeIntoTerminal($, order.id, order.type)
  } catch {
    // The gateway is off, or the socket changed hands. The grant ends, and a gateway that answers
    // next hears that this session holds none. Try again soon.
    if (grant) endGrant($)
    heardChosen = false
    retryAt = Date.now() + RETRY_AFTER_MS
  } finally {
    polling = false
  }
}

// A command that outlives its turn would finish unheard, and its notice would start a turn that
// carries on with the work. This runs on the timer, because the engine refuses a tool call made
// from inside the stopped turn. A command that has already ended has nothing left to stop.
function stopTasks($) {
  for (const task of tasksToStop) void $.tool.call({ tool: 'TaskStop', task_id: task }).catch(() => {})
  tasksToStop = []
}

// The tap. It stops the turn the gateway named, or the spoken turn the gateway hasn't heard of
// yet, and never a turn the session started by itself. Aborting a turn moves its running command
// to the background, so that comes back to the Bash hook to be stopped. A prompt with the engine
// stops as it starts, and prompts still waiting here go no further.
// In its own try: a rejected abort must not skip anything else in the order.
async function abortTurn($, id, turnId) {
  stopAtStart = ownerPrompts
  const dropped = dropWaiting($)
  try {
    const target = turnId ?? spokenTurn
    if (target && target === runningTurn) {
      tappedTurn = target
      tasksToStop.push(...spokenTasks)
      await $.turn.abort({ turnId: target })
    }
    post($, 'order', dropped > 0 ? { id, ok: true, dropped: true } : { id, ok: true })
  } catch (error) {
    post($, 'order', { id, error: String(error?.message ?? error) })
  }
}

// The gateway hears at once that this session has the prompt, and whether it has to wait.
// A prompt that had to wait, or that the gateway says beat a reply to the ears, can't answer that reply.
function submit($, id, text, unheard) {
  const busy = runningTurn !== null || inFlight || waiting.length > 0
  post($, 'order', { id, accepted: true, busy })
  waiting.push({ id, text, early: busy || unheard })
  submitNext($)
}

// One at a time, and only while idle and chosen, so each prompt gets a turn of its own, in the order
// spoken, with the voice rules, and a reply someone can hear.
// Not awaited: with a permission prompt open a submit waits, and an abort must still get through.
// A refused prompt resolves { drop }, and the gateway hears that too. The prompts behind it go with
// it, because the session would refuse them too, and only the first can be typed in on request.
function submitNext($) {
  if (!chosen || runningTurn !== null || inFlight || ownerPrompts > 0 || !waiting.length) return
  const { id, text, early } = waiting.shift()
  inFlight = true
  startingOrder = id
  void $.clock.now().then((now) => {
    promptedAt = now
  })
  // Here, because the engine doesn't raise a mod's own prompt.submit hook for its own prompt.
  ownerPrompts++
  addVoiceRules($, early).then(() => $.prompt.submit({ text, asUser: true })).then(
    (result) => {
      inFlight = false
      if (result.drop) {
        promptRefused()
        const later = waiting.length
        post($, 'order', later > 0 ? { id, drop: result.drop, later } : { id, drop: result.drop })
        dropWaiting($)
      } else {
        post($, 'order', { id, ok: true })
      }
      submitNext($)
    },
    (error) => {
      inFlight = false
      promptRefused()
      post($, 'order', { id, error: String(error?.message ?? error) })
      submitNext($)
    },
  )
}

// The one job a mod can't do: after someone stops a session at the keyboard, only
// real input lifts Claude Code's refusal of new work. So the prompt goes into the
// session's own prompt box, and only the Enter goes through the terminal. The engine
// refuses the fill while a dialog holds the keys, and the Enter follows only if
// the box still takes text, so this can't answer a dialog.
async function typeIntoTerminal($, id, text) {
  try {
    const terminal = await ownTerminal($)
    if (!terminal) return post($, 'order', { id, refused: 'no-pane' })
    if ((await $.prompt.read()).text.trim()) return post($, 'order', { id, refused: 'draft' })
    const refused = await fillRefused($, text)
    if (refused) return post($, 'order', { id, refused })
    await $.clock.sleep(ENTER_AFTER_MS)
    const late = await fillRefused($, text)
    if (late) return post($, 'order', { id, refused: late })
    await terminal.pressEnter()
    post($, 'order', { id, ok: true })
  } catch (error) {
    post($, 'order', { id, error: String(error?.message ?? error) })
  }
}

async function fillRefused($, text) {
  const filled = await $.prompt.fill({ text, mode: 'replace' })
  if (filled.isFilled) return null
  return filled.refusal === 'no_composer' ? 'no-pane' : 'dialog'
}

async function run($, argv) {
  const result = await $.process.run(argv, { timeoutMs: 5000 })
  if (result.exitCode !== 0) throw new Error(`${argv[0]} exit ${result.exitCode}`)
  return result.stdout
}

// This session's terminal, only while this Claude is what's in front in it.
async function ownTerminal($) {
  const herdr = (await $.env.get('HERDR_ENV')) === '1' ? await $.env.get('HERDR_PANE_ID') : null
  if (herdr) {
    const bin = (await $.env.get('HERDR_BIN_PATH')) || 'herdr'
    const info = JSON.parse(await run($, [bin, 'pane', 'process-info', '--pane', herdr]))
    const front = info.result.process_info.foreground_processes
    if (!front.some((process) => String(process.pid) === instance)) return null
    return {
      pressEnter: () => run($, [bin, 'pane', 'send-keys', herdr, 'enter']),
    }
  }
  const tmux = await $.env.get('TMUX')
  const pane = await $.env.get('TMUX_PANE')
  if (!tmux || !pane) return null
  const socketPath = tmux.split(',')[0]
  const tty = (await run($, ['tmux', '-S', socketPath, 'display-message', '-p', '-t', pane, '#{pane_tty}'])).trim()
  const own = (await run($, ['ps', '-o', 'tty=,pgid=,tpgid=', '-p', instance])).trim().split(/\s+/)
  if (tty !== `/dev/${own[0]}` || own[1] !== own[2]) return null
  return {
    pressEnter: () => run($, ['tmux', '-S', socketPath, 'send-keys', '-t', pane, 'Enter']),
  }
}

async function realPath($, path) {
  return (await $.fs.stat(path, { resolve: true }).catch(() => null))?.realPath ?? null
}

// Which logins may connect and which folders never may, from the one file the install writes. A
// missing or unreadable file, or a login it doesn't name, keeps this session out.
async function allowedHere($) {
  const home = await $.env.get('HOME')
  let config
  try {
    config = JSON.parse(await $.fs.read(`${home}/.config/walkie-talkie/connect.json`))
  } catch {
    return false
  }
  const logins = config?.logins
  if (!logins || typeof logins !== 'object' || !Array.isArray(config.deny)) return false
  const login = await realPath($, (await $.env.get('CLAUDE_CONFIG_DIR')) || `${home}/.claude`)
  const here = login && Object.hasOwn(logins, login) ? logins[login] : null
  if (!here || !Array.isArray(here.deny)) return false
  deny = [...config.deny, ...here.deny].filter((name) => typeof name === 'string' && name).map((name) => name.toLowerCase())
  loginName = await nameOfLogin($, home, login)
  return true
}

// The login's folder, short: "default" for ~/.claude, and "personal" for ~/.claude-personal. Like a
// folder, a login whose path holds a denied name goes unnamed.
async function nameOfLogin($, home, login) {
  if (login.toLowerCase().split('/').some((part) => deny.includes(part))) return ''
  if (login === (await realPath($, `${home}/.claude`))) return 'default'
  return login.split('/').pop().replace(/^\./, '').replace(/^claude-/, '')
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (await $.env.get('FM_TASK_ID')) return started // an orchestrated worker never connects
    if (!(await allowedHere($))) return started
    const role = await $.env.get('WALKIE_FRONT_DESK')
    if (!role && !(await $.session.surfaces()).length) return started // a headless run: nobody is there
    stateDir = (await $.env.get('WALKIE_TALKIE_STATE')) || `${await $.env.get('HOME')}/.local/state/walkie-talkie`
    socket = `${stateDir}/mod.sock`
    nonce = newNonce()
    instance = (await run($, ['/bin/sh', '-c', 'echo $PPID'])).trim()
    if (role) {
      isDesk = true
      await startDesk($, JSON.parse(role), stateDir)
    }
    $.clock.every(POLL_EVERY_MS, () => {
      stopTasks($)
      submitNext($)
      return pollOnce($)
    })
    return started
  })

  on('session.end', async ($, e, next) => {
    // A /clear ends one transcript, not this process: the running turn is gone, the connection stays.
    runningTurn = null
    forgetOwnerTurns()
    if (e.reason === 'clear' && grant) {
      endGrant($) // a new conversation: the phone chose the old one
      post($, 'grant-ended', { reason: 'clear' })
    }
    if (e.reason !== 'clear') post($, 'bye', {})
    return next(e)
  })

  // Prompts typed at the keyboard or through Remote Control. A spoken one starts in submit().
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'composer' || e.origin.kind === 'bridge') promptedAt = await $.clock.now()
    if (!chosen || e.command || MACHINE_PROMPT.test(e.text)) return next(e)
    if (e.origin.kind !== 'composer' && e.origin.kind !== 'bridge') return next(e)
    active($)
    if (e.turnId && e.turnId === runningTurn) {
      // Typed over a running turn: it folds in, so that turn's rest answers the owner.
      if (spokenTurn !== e.turnId) {
        post($, 'turn-start', { turnId: e.turnId })
        spokenTurn = e.turnId
        await addVoiceRules($)
      }
    } else {
      ownerPrompts++
      await addVoiceRules($)
      try {
        const result = await next(e)
        if (result.drop) promptRefused()
        return result
      } catch (error) {
        promptRefused()
        throw error
      }
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    let stop = false
    if (e.agentId === undefined) {
      runningTurn = e.turnId
      spokenTasks = []
      // A continuation has no text, and a machine's prompt says what it is: neither answers the owner.
      if (ownerPrompts > 0 && e.text && !MACHINE_PROMPT.test(e.text)) {
        ownerPrompts--
        if (stopAtStart > 0) {
          stopAtStart--
          stop = true
        } else {
          spokenTurn = e.turnId
          post($, 'turn-start', startingOrder !== null ? { turnId: e.turnId, order: startingOrder } : { turnId: e.turnId })
        }
        startingOrder = null
      }
    }
    const started = await next(e)
    if (stop) await $.turn.abort({ turnId: e.turnId }).catch(() => {})
    return started
  })

  on('turn.step', async function* ($, e, next) {
    let block = null
    for await (const chunk of next(e)) {
      if (e.agentId === undefined && e.turnId === spokenTurn && chunk.kind === 'text') {
        // A new block starts a new line, so sentences of two blocks don't run together.
        const text = block !== null && block !== chunk.index ? `\n${chunk.text}` : chunk.text
        block = chunk.index
        post($, 'text', { turnId: e.turnId, text })
        active($)
      }
      yield chunk
    }
    if (block !== null && e.turnId === spokenTurn) post($, 'text', { turnId: e.turnId, text: '\n' })
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const turn = runningTurn
    const ran = await next(e)
    const task = ran.result?.backgroundTaskId
    if (task && turn && e.agentId === undefined) {
      if (turn === tappedTurn) tasksToStop.push(task)
      else if (turn === spokenTurn) spokenTasks.push(task)
    }
    return ran
  })

  on('classic.PermissionRequest', async ($, e, next) => {
    if (spokenTurn) post($, 'permission', {})
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      if (runningTurn === e.turnId) runningTurn = null
      if (spokenTurn === e.turnId) {
        spokenTurn = null
        post($, 'turn-end', { turnId: e.turnId, reason: e.reason })
      }
      // Whatever went to the engine has had its turn by now, so a count left over can't hold up the rest.
      if (!inFlight) {
        ownerPrompts = 0
        stopAtStart = 0
      }
    }
    const completed = await next(e)
    submitNext($)
    return completed
  })
}
