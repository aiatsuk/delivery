// Run the shipped execution loop under node against the real delivery engine, for tests.
//
//   node workflow_harness.js <script.js> <scenario.json>
//
// The scenario holds `args` (the loop's args global), `journal` (the host journal directory to
// write) and `agents`: a map from an agent label to a list of scripted behaviours used in order.
// Relay agents (their prompt ends with the helper command) really run that command with bash and
// return the JSON it prints, as a relay agent would. An implementer behaviour is
// {write: {path: content}, report: {...overrides}} or null (the agent fails); it edits and stages
// the files in the worktree named by its prompt and returns a report with the dispatch ID and the
// staged tree. A reviewer behaviour is {verdict, evidence?, defects?} or null. Unlisted
// implementers write nothing; unlisted reviewers pass. Every call is written to the journal as the
// host writes it (started, then result or failed, plus a transcript), before the next agent starts.
// Prints {result, calls, logs, meta, unknownPhases} or {error, calls, logs}.
//
// Every journal line and agent here is a synthetic test artifact, not evidence that a host ran.
'use strict'
const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
const { execFileSync, spawnSync } = require('child_process')

const [, , scriptPath, scenarioPath] = process.argv
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'))
const source = fs.readFileSync(scriptPath, 'utf8')
const metaMatch = source.match(/^export const meta = (\{[\s\S]*?\n\})\n/)
if (!metaMatch || source.indexOf('export const meta') !== 0) throw new Error('the script must start with `export const meta = {...}`')
const meta = new Function(`"use strict"; return (${metaMatch[1]})`)()
const body = source.slice(metaMatch[0].length)

const RELAYS = new Set(['prepare', 'dispatch', 'collect', 'return', 'gate', 'review-open', 'review-close', 'finish'])
const PASS_EVIDENCE = "Synthetic fixture review of the exact current content; not a real reviewer's verdict.\n" +
  'Acceptance: the owned file holds the planned value and no other path changed.\n' +
  'Gates rerun: the planned fixture command passed on the current content fingerprint.\n'
const calls = []
const logs = []
const queues = {}
for (const [label, values] of Object.entries(scenario.agents || {})) queues[label] = [...values]
fs.mkdirSync(scenario.journal, { recursive: true })
const journalPath = path.join(scenario.journal, 'journal.jsonl')
const line = value => fs.appendFileSync(journalPath, JSON.stringify(value) + '\n')
line({ type: 'launched' })
let counter = 0

const git = (cwd, ...args) => execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8' }).trim()
const match = (prompt, re) => { const m = prompt.match(re); return m ? m[1] : null }

function relay(prompt) {
  const lines = prompt.split('\n').filter(l => l.trim())
  const run = spawnSync('bash', ['-c', lines[lines.length - 1]], { encoding: 'utf8', env: process.env })
  try {
    return JSON.parse(run.stdout)
  } catch (error) {
    throw new Error(`relay printed no JSON (exit ${run.status}): ${run.stdout}${run.stderr}`)
  }
}

function implement(prompt, behaviour) {
  const worktree = match(prompt, /work only inside the worktree '([^']+)'/)
  const dispatch = match(prompt, /dispatch_id must be exactly (\S+?)\./)
  for (const [file, content] of Object.entries((behaviour && behaviour.write) || {})) {
    fs.writeFileSync(path.join(worktree, file), content)
    git(worktree, 'add', '--', file)
  }
  return {
    dispatch_id: dispatch, summary: 'Scripted fixture implementer; edited only the listed files.',
    tests: [{ command: ['git', 'diff', '--cached', '--check'], exit_code: 0, outcome: 'Synthetic fixture observation.' }],
    limitations: ['Scripted fixture agent.'], tree: git(worktree, 'write-tree'), ...((behaviour && behaviour.report) || {}),
  }
}

function review(prompt, behaviour) {
  const token = match(prompt, /review_token must be exactly (\S+?)\./)
  const b = behaviour || { verdict: 'PASS' }
  return { review_token: token, verdict: b.verdict, evidence: b.evidence || PASS_EVIDENCE, defects: b.defects || [] }
}

async function agent(prompt, opts = {}) {
  const label = opts.label
  if (!label) throw new Error('every agent needs a label')
  if (calls.some(c => c.label === label)) throw new Error(`duplicate agent label ${label}`)
  calls.push({ label, phase: opts.phase, model: opts.model, effort: opts.effort, prompt })
  const kind = label.split(':')[0]
  const agentId = `a${crypto.createHash('sha256').update(label).digest('hex').slice(0, 15)}`
  const key = `v2:${crypto.createHash('sha256').update(`${label}#${++counter}`).digest('hex').slice(0, 16)}`
  line({ type: 'started', key, agentId, label, phase: opts.phase })
  fs.writeFileSync(path.join(scenario.journal, `agent-${agentId}.jsonl`), JSON.stringify({ type: 'fixture transcript', label }) + '\n')
  await new Promise(resolve => setImmediate(resolve))
  const scripted = queues[label] && queues[label].length ? queues[label].shift() : undefined
  let value
  if (scripted === null) {
    value = null
  } else if (RELAYS.has(kind)) {
    value = relay(prompt)
  } else if (kind === 'impl' || kind === 'rework') {
    value = implement(prompt, scripted)
  } else if (kind === 'review') {
    value = review(prompt, scripted)
  } else {
    throw new Error(`unexpected agent kind ${kind}`)
  }
  calls[calls.length - 1].result = value
  line(value === null ? { type: 'failed', key, agentId } : { type: 'result', key, agentId, result: value })
  return value
}

async function parallel(thunks) {
  // One at a time, so journal order is deterministic.
  const out = []
  for (const thunk of thunks) out.push(await Promise.resolve().then(thunk).catch(() => null))
  return out
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const run = new AsyncFunction('agent', 'parallel', 'phase', 'log', 'args', body)
run(agent, parallel, () => {}, m => logs.push(String(m)), scenario.args)
  .then(result => {
    const titles = new Set((meta.phases || []).map(p => p.title))
    const unknown = [...new Set(calls.map(c => c.phase).filter(p => p && !titles.has(p)))]
    process.stdout.write(JSON.stringify({ result, calls, logs, meta, unknownPhases: unknown }))
  })
  .catch(error => {
    process.stdout.write(JSON.stringify({ error: String((error && error.stack) || error), calls, logs }))
    process.exitCode = 1
  })
