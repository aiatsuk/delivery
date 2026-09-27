// Run a shipped workflow script outside the host with scripted agents, for tests.
//
//   node workflow_harness.js <script.js> <scenario.json>
//
// The scenario holds `args` and `responses`: a map from an agent label to the results it returns
// in order (null means the agent failed or was skipped). Every agent call needs a scripted
// response. Prints {result, calls, logs, meta, unknownPhases}. Date.now, Math.random and a bare
// new Date() throw, as in the host runtime.
'use strict'
const fs = require('fs')

const [, , scriptPath, scenarioPath] = process.argv
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'))
const source = fs.readFileSync(scriptPath, 'utf8')
const match = source.match(/^export const meta = (\{[\s\S]*?\n\})\n/)
if (!match || source.indexOf('export const meta') !== 0) throw new Error('the script must start with `export const meta = {...}`')
const meta = new Function(`"use strict"; return (${match[1]})`)()
const body = source.slice(match[0].length)

const calls = []
const logs = []
const queues = {}
for (const [label, values] of Object.entries(scenario.responses || {})) queues[label] = [...values]

async function agent(prompt, opts = {}) {
  if (!opts.label) throw new Error('every agent needs a label')
  if (calls.some(c => c.label === opts.label)) throw new Error(`duplicate agent label ${opts.label}`)
  calls.push({ label: opts.label, phase: opts.phase, model: opts.model, effort: opts.effort, agentType: opts.agentType, schema: opts.schema || null, prompt })
  await new Promise(resolve => setImmediate(resolve))
  if (!queues[opts.label] || !queues[opts.label].length) throw new Error(`no scripted response for ${opts.label}`)
  return queues[opts.label].shift()
}
async function parallel(thunks) { return Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null))) }
function phase() {}
function log(message) { logs.push(String(message)) }
function GuardedDate(...values) {
  if (!values.length) throw new Error('new Date() is not available in workflows')
  return new Date(...values)
}
GuardedDate.now = () => { throw new Error('Date.now() is not available in workflows') }
const guardedMath = Object.create(Math)
guardedMath.random = () => { throw new Error('Math.random() is not available in workflows') }

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
new AsyncFunction('agent', 'parallel', 'phase', 'log', 'args', 'Date', 'Math', body)(agent, parallel, phase, log, scenario.args, GuardedDate, guardedMath)
  .then(result => {
    const titles = new Set((meta.phases || []).map(p => p.title))
    const unknownPhases = [...new Set(calls.map(c => c.phase).filter(p => p && !titles.has(p)))]
    process.stdout.write(JSON.stringify({ result, calls, logs, meta, unknownPhases }))
  })
  .catch(error => {
    process.stdout.write(JSON.stringify({ error: String((error && error.stack) || error), calls, logs }))
    process.exitCode = 1
  })
