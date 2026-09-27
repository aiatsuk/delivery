export const meta = {
  name: 'delivery-implement',
  description: 'Dispatch registered Delivery Harness tasks to real implementation agents in their own worktrees',
  whenToUse: 'After task-prepare and task-register --via-workflow for every dependency-ready task; import each result with task-import',
  phases: [
    { title: 'Implement', detail: 'one agent per registered dispatch, in its prepared worktree' },
  ],
}

// Thin by design: the delivery engine stays the authority. This script only guarantees a real,
// separately identified agent per dispatch and a schema-checked report that carries the
// dispatch ID. The harness journal records each agent's identity and result; `task-import`
// binds that journal entry to the dispatch, so no identity is typed in by the coordinator.

const A = args

// Mirrors the engine's report validation, so a report the engine would refuse fails here and
// is retried by the host instead of leaving the dispatch without an importable result.
const TEXT = { type: 'string', minLength: 1 }
const REPORT = {
  type: 'object',
  properties: {
    dispatch_id: TEXT,
    summary: TEXT,
    tests: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          command: { type: 'array', items: TEXT, minItems: 1 },
          exit_code: { type: 'integer' },
          outcome: TEXT,
        },
        required: ['command', 'exit_code', 'outcome'],
      },
    },
    limitations: { type: 'array', items: TEXT },
    tree: TEXT,
  },
  required: ['dispatch_id', 'summary', 'tests', 'limitations', 'tree'],
}

const argv = command => command.map(part => JSON.stringify(part)).join(' ')
const list = xs => xs.map(x => `- ${x}`).join('\n')
// Gates come from the task contract as {command, risk, ...}; bare argument arrays are safe gates.
const gateOf = g => (Array.isArray(g) ? { command: g, risk: 'safe' } : g)
const safeGates = gates => gates.map(gateOf).filter(g => g.risk === 'safe').map(g => argv(g.command))
const otherGates = gates => gates.map(gateOf).filter(g => g.risk !== 'safe').length

function brief(t) {
  const extra = t.instructions ? `\n\nFindings to address in this dispatch (quoted from the gate or review):\n${t.instructions}` : ''
  return `You are the implementation worker for task ${t.task} of Delivery Harness run ${A.run_root}.\n` +
    `Read the task contract ${t.brief} in full first. Worktree: ${t.worktree} (branch ${t.branch}). ` +
    `Owned paths:\n${list(t.paths)}\nAcceptance: ${t.acceptance}\n` +
    `Planned safe gates (argument arrays; run them from the worktree):\n${list(safeGates(t.gates))}\n` +
    (otherGates(t.gates) ? `Do not run the ${otherGates(t.gates)} other planned gate(s): they have external or destructive effects and run only under the coordinator's authority.\n` : '') +
    `${extra}\n` +
    'You are not alone in this repository. Work only in your assigned worktree and owned paths. ' +
    "Do not revert others' changes. Preserve any unexpected work and report it. Stage exact intended files. " +
    'Do not commit, push, open a PR, merge, deploy, or mutate product/session records. Report actual tests and limitations. ' +
    'Put no AI or tool names into code, comments or messages.\n\n' +
    `Return the report as the structured output. dispatch_id must be exactly ${t.dispatch_id}. ` +
    'summary: what actually changed and why. tests: every command you ran as an argument array, its exit code and the observed outcome ' +
    '(an empty list means no tests ran). limitations: specific behaviour you did not check. ' +
    `tree: the output of \`git -C ${JSON.stringify(t.worktree)} write-tree\` after your final staging.`
}

const results = await parallel(A.tasks.map(t => () => agent(brief(t), {
  label: `implement:${t.task}`,
  phase: 'Implement',
  schema: REPORT,
  ...(t.model ? { model: t.model } : {}),
  ...(t.effort ? { effort: t.effort } : {}),
})))

const tasks = A.tasks.map((t, i) => {
  const report = results[i]
  if (!report) return { task: t.task, dispatch_id: t.dispatch_id, returned: false, reason: 'the agent returned no result' }
  if (report.dispatch_id !== t.dispatch_id) {
    return { task: t.task, dispatch_id: t.dispatch_id, returned: false, reason: `the report named dispatch ${report.dispatch_id}` }
  }
  return { task: t.task, dispatch_id: t.dispatch_id, returned: true, summary: report.summary, limitations: report.limitations.length }
})
log(`${tasks.filter(t => t.returned).length} of ${tasks.length} dispatches returned; import each with task-import`)
return { schema: 'delivery-implement-result/v1', run_root: A.run_root, tasks }
