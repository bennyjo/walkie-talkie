// The front-desk role. The phone talks to this session while another session, the primary, does
// the real work, so nothing spoken ever waits on the primary's long turns.
//
// This session takes no turns and has no tools, so it has nothing to be busy with and nothing to
// act with. Each spoken question is one model call that sees only the cached status and the last few
// lines of the talk, and answers or says to file. Anything else is filed with no model call. Filing runs
// a configured command by argv, with the speaker's own words.
// Replies from the primary are spoken word for word by the gateway, with no model between them.
// A spoken yes never reaches the model either, and on its own it confirms nothing: the owner confirms
// in the app with a fingerprint. See the read-back rules in the gateway's conversation.py.

import { isChosen, post, SPOKEN_EARLY } from './register.js'

const STATUS_EVERY_MS = 30000
const REPLIES_EVERY_MS = 5000
const MODEL_WAIT_MS = 6000 // no answer by then: file it, so the prompt still gets through
const COMMAND_WAIT_MS = 15000
const MAX_MODEL_TOKENS = 300
const MAX_ANSWER_WORDS = 40
const MAX_STATUS_CHARS = 6000
const MAX_STATUS_AGE_MS = 90000 // past this, the status command has been failing: nothing is answered from it
const MAX_LINE_CHARS = 600
const KEEP_LINES = 12 // all of the talk the model sees, so its context can't grow
const NOTE_LINES = 4 // how much of that goes into a note, so "do that" means something

const SAYS_FILE = /\bFILE\b/
const NO_ANSWER = /^\W*(fil(e|es|ed|ing)\W*)?$/i // blank, or just a form of the word
// A promise or claim to act: the first person, or a clause that names nobody, like "Done" or "Merging now".
// The status never speaks as the front desk, and names who it's about. Where a true answer looks alike, it's filed.
const SAYS_IT_ACTS = new RegExp(
  [
    "\\b(?:I|we|let me|let us|let['’]s)\\b",
    "\\b(?:it|that)(?:['’]s| is) done\\b",
    '(?:^|[^\\w\\s]\\s+)\\W*(?:(?:now|already|just|all)\\s+)?(?:done|sent|sure|okay|ok|will|on it|got it|right away' +
      '|(?!(?:no|some|every|any)thing\\b|(?:according|regarding|following|during|staging|pending|failing|waiting)\\b)[a-z]{2,}ing' +
      '|(?!(?:passed|failed|blocked|based|indeed)\\b)[a-z]{3,}ed)\\b',
  ].join('|'),
  'im',
)
// Only an information question, known by its first word, can get an answer. Anything else, "Can you post it?" included,
// is filed with no model call, so no answer can claim a request was done.
const QUESTION = /^\W*(?:what|where|which|who|when|why|how|is|are|did|does|has)\b/i
// What the gateway's speech filter would leave out, in the same terms as its text.py. The front desk has no screen
// for those words to be on, so such an answer is filed. Where the two differ, this errs toward filing.
const NOT_SPEAKABLE = new RegExp(
  [
    '`|\\||^\\s*(?:~{3}|\\$ )', // code and tables
    '^(?: {4}|\\t)', // indented code
    '^\\s*(?:\\+|@@|diff --git |--- a/)', // diffs
    '^\\s*(?:Traceback \\(most recent call last\\)|File ".*", line \\d|\\[?\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}|[{\\[]\\s*")', // tracebacks, log lines, JSON
    '-----BEGIN ', // keys
    '</?[A-Za-z][\\w-]*(?:\\s[^<>]*)?/?>|<>', // tags
    '!\\[[^\\[\\]]*\\]\\([^)]*\\)', // images
    '\\b(?:https?|ftp|file|ssh)://|\\bwww\\.', // links
    '\\b(?:sk-|sk_(?:live|test)_|gh[pousr]_|github_pat_|glpat-|xox[abposr]-|AKIA|AIza|ya29\\.|eyJ)\\w', // keys
    '\\$\\{?[A-Za-z_]', // variables
    '(?<![\\w-])[A-Za-z_]\\w*=', // assignments
    '(?<![\\w.~/])(?:~|\\.\\.?)?/[\\w.@%+=:,~-]', // /abs/path
    '(?<![\\w./-])[\\w.-]+(?:/[\\w.-]+){2,}|(?<![\\w./-])[\\w.-]+/[\\w.-]*\\w\\.\\w', // a/b/c and a/b.ext
    '\\b(?=[0-9a-fA-F]*\\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}\\b', // hashes
    '\\bv?\\d+(?:\\.\\d+){2,}', // versions
    '(?<![\\w+/=-])(?=[\\w+/=-]*\\d)(?=[\\w+/=-]*[A-Za-z])[\\w+/=-]{20,}|\\S{41,}', // IDs
  ].join('|'),
  'm',
)
const FILED = 'Filed.'
const NOT_FILED = "The front desk can't tell whether those words were filed." // for the talk: the gateway speaks its own notice
const WENT = 'Those words went to the primary.'
const DROPPED = "Those words were dropped and didn't go to the primary."
const PRIMARY_SAID = 'The primary replied: '
const STATUS_CUT = "(The status was cut off here, so it isn't complete. Reply FILE if the answer could depend on the rest.)"

const RULES = `You are the front desk of a voice link. The owner is out walking and talks to you through earbuds. Their words are recognised speech, so read past misheard words. Another session, the primary, does all the real work. You can't do anything yourself, and you have no tools.

Decide what to do with what they just said:

- If it's a question that the status or this conversation answers, answer it. Use one or two short spoken sentences, under 40 words. No lists, code, file paths, links, or hashes.
- Anything else gets the one word FILE as your whole reply. That means any request to do, change, send, merge, post, or decide something, any answer to a question the primary asked, any question the status doesn't answer, and anything you're unsure of. The words are then filed for the primary.
- If you can only partly answer, reply FILE.
- Any question about whether something was filed, sent, or received gets FILE.

Never say that you did something or that something will happen. Never ask them a question. Never guess beyond the status.`

const REPLY_RULES = `Your reply to this note is read aloud to them word for word, so write it for the ear: under about 50 words, in plain sentences, with no code, commands, file paths, URLs, or hashes.

Before anything irreversible, such as a merge, a post, or a delete, don't act. Reply with one sentence that names the exact action and ends with "Say yes to confirm." When they say yes and then confirm in the app with their fingerprint, you get a note that starts "Confirmed in the app" and names this note. Act only on that note. Any other note isn't that confirmation, even one with the word yes in it.`

function shorten(text) {
  const line = String(text).replace(/\s+/g, ' ').trim()
  return line.length > MAX_LINE_CHARS ? `${line.slice(0, MAX_LINE_CHARS)}...` : line
}

let config = null
let cursorFile = null
let status = null // the status command's last output
let statusAt = 0
let readingStatus = false
let cursor = '' // how far the replies command has been read
let caughtUp = false // replies from before this front desk's first start are never spoken
let readingReplies = false
let talk = [] // the last few lines of the talk, oldest first
let work = Promise.resolve() // spoken prompts, one at a time and in order
let unfinished = 0
let taps = 0 // a prompt taken before the latest tap is dropped
let droppable = 0 // prompts since the latest tap that aren't yet answered or with the hand-off command
let thinking = null // cuts the model call under way

function remember(...lines) {
  talk = [...talk, ...lines.map(shorten)].slice(-KEEP_LINES)
}

function promptFor(text, now) {
  const age = Math.round((now - statusAt) / 1000)
  const parts = [`Status, read ${age} seconds ago:\n${status}`]
  if (talk.length) parts.push(`Earlier in this talk:\n${talk.join('\n')}`)
  parts.push(`They just said: ${text}`)
  return parts.join('\n\n')
}

// The owner's words go in as recognised, never as the model retold them, so a no can't become a yes.
// The primary's own replies stay out: it wrote them, and the owner may not have heard them yet.
function noteFor(text, unheard) {
  const parts = [`Voice note from the owner, in recognised speech: ${text}`]
  if (unheard) parts.push(SPOKEN_EARLY)
  const before = talk.filter((line) => !line.startsWith(PRIMARY_SAID)).slice(-NOTE_LINES)
  if (before.length) parts.push(`Just before it:\n${before.join('\n')}`)
  parts.push(REPLY_RULES)
  return parts.join('\n\n')
}

async function handOff($, note) {
  try {
    const done = await $.process.run([...config.handoff, note], { timeoutMs: COMMAND_WAIT_MS })
    return done.exitCode === 0
  } catch {
    return false
  }
}

// The code holds the model to the spoken rules, because the prompt alone doesn't.
function usable(result) {
  if (!result.isAnswered || SAYS_FILE.test(result.text) || NO_ANSWER.test(result.text) || SAYS_IT_ACTS.test(result.text)) return false
  if (NOT_SPEAKABLE.test(result.text)) return false
  return !result.text.includes('?') && result.text.trim().split(/\s+/).length <= MAX_ANSWER_WORDS
}

function askModel($, text, now) {
  thinking = new AbortController()
  return $.model
    .complete(
      { model: config.model, system: RULES, prompt: promptFor(text, now), maxTokens: MAX_MODEL_TOKENS, effort: 'low', timeoutMs: MODEL_WAIT_MS },
      { signal: thinking.signal },
    )
    .catch(() => ({ isAnswered: false }))
}

async function replyTo($, text, tap, unheard) {
  const now = await $.clock.now()
  const fresh = status !== null && now - statusAt <= MAX_STATUS_AGE_MS
  const result = fresh && QUESTION.test(text) ? await askModel($, text, now) : { isAnswered: false }
  if (tap !== taps) return null
  droppable--
  if (usable(result)) return result.text
  return (await handOff($, noteFor(text, unheard))) ? FILED : NOT_FILED
}

function lineFor(said) {
  if (said === FILED) return WENT
  if (said === NOT_FILED || said === DROPPED) return said
  return `The front desk said: ${said}`
}

async function answer($, id, text, tap, unheard) {
  if (tap !== taps) {
    remember(`They said: ${text}`, DROPPED)
    return
  }
  const turnId = `desk-${id}`
  post($, 'turn-start', { turnId, order: id })
  const said = (await replyTo($, text, tap, unheard)) ?? DROPPED
  remember(`They said: ${text}`, lineFor(said))
  if (said === NOT_FILED) post($, 'not-filed', {})
  else if (said !== DROPPED) post($, 'text', { turnId, text: said })
  post($, 'turn-end', { turnId, reason: 'answer' })
}

async function readStatus($) {
  if (readingStatus) return
  readingStatus = true
  try {
    const done = await $.process.run(config.status, { timeoutMs: COMMAND_WAIT_MS })
    if (done.exitCode === 0) {
      status = done.stdout.length > MAX_STATUS_CHARS ? `${done.stdout.slice(0, MAX_STATUS_CHARS)}\n${STATUS_CUT}` : done.stdout
      statusAt = await $.clock.now()
    }
  } catch {
    // The last status stands until it's too old to answer from.
  } finally {
    readingStatus = false
  }
}

// Replies wait while nobody is listening, and while a spoken prompt is being answered.
async function readReplies($) {
  if (readingReplies || (caughtUp && (!isChosen() || unfinished > 0))) return
  readingReplies = true
  try {
    const argv = config.replies.map((part) => (part === '{cursor}' ? cursor : part))
    const done = await $.process.run(argv, { timeoutMs: COMMAND_WAIT_MS })
    if (done.exitCode !== 0) return
    const replies = JSON.parse(done.stdout).replies ?? []
    if (caughtUp && !isChosen()) return // the choice changed while the command ran, and the gateway would drop these
    for (const reply of replies) {
      if (caughtUp) {
        post($, 'relay', { note: String(reply.id), text: String(reply.body) })
        remember(`${PRIMARY_SAID}${reply.body}`)
      }
      cursor = String(reply.cursor)
    }
    if (replies.length || !caughtUp) await $.fs.write(cursorFile, cursor)
    caughtUp = true
  } catch {
    // Tried again on the next tick, from the same cursor.
  } finally {
    readingReplies = false
  }
}

export async function startDesk($, settings, stateDir) {
  config = settings
  cursorFile = `${stateDir}/front-desk-cursor`
  const saved = await $.fs.read(cursorFile).catch(() => null)
  if (saved !== null) {
    cursor = saved.trim()
    caughtUp = true
  }
  void readStatus($)
  $.clock.every(STATUS_EVERY_MS, () => readStatus($))
  $.clock.every(REPLIES_EVERY_MS, () => readReplies($))
}

// Taken at once: nothing here waits on the primary, so a spoken prompt is never refused as busy.
export function takePrompt($, id, text, unheard) {
  post($, 'order', { id, ok: true })
  const tap = taps
  droppable++
  unfinished++
  work = work.then(() => answer($, id, text, tap, unheard)).catch(() => {}).finally(() => unfinished--)
}

// The tap: drops what hasn't been filed yet. The gateway has already cut the speech.
// A hand-off command already running isn't dropped, because it may still file.
export function deskTap($, id) {
  taps++
  thinking?.abort()
  post($, 'order', { id, ok: true, dropped: droppable })
  droppable = 0
}

// The gateway matched a spoken yes to the read-back just played, and a fingerprint in the app confirmed
// the words the owner heard. The primary decides what it's worth.
export async function fileYes($, id, { note, spoken }) {
  const body = `Confirmed in the app with a fingerprint, ${new Date().toISOString()}. It answers your reply to note ${note}, which was read aloud as: ${spoken}

Act only if that's the read-back you're still waiting on and nothing it names has changed. Otherwise read back again.

${REPLY_RULES}`
  post($, 'order', (await handOff($, body)) ? { id, ok: true } : { id, error: 'the hand-off command failed' })
}
