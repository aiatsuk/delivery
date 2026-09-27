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

const REPORT = {
  type: 'object',
  properties: {
    dispatch_id: { type: 'string' },
    summary: { type: 'string' },
    tests: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          command: { type: 'array', items: { type: 'string' } },
          exit_code: { type: 'integer' },
          outcome: { type: 'string' },
        },
        required: ['command', 'exit_code', 'outcome'],
      },
    },
    limitations: { type: 'array', items: { type: 'string' } },
  },
  required: ['dispatch_id', 'summary', 'tests', 'limitations'],
}

const argv = command => command.map(part => JSON.stringify(part)).join(' ')
const list = xs => xs.map(x => `- ${x}`).join('\n')

function brief(t) {
  const extra = t.instructions ? `\n\nFindings to address in this dispatch (quoted from the gate or review):\n${t.instructions}` : ''
  return `You are the implementation worker for task ${t.task} of Delivery Harness run ${A.run_root}.\n` +
    `Read the task contract ${t.brief} in full first. Worktree: ${t.worktree} (branch ${t.branch}). ` +
    `Owned paths:\n${list(t.paths)}\nAcceptance: ${t.acceptance}\n` +
    `Planned gates (argument arrays; run them from the worktree):\n${list(t.gates.map(argv))}${extra}\n\n` +
    'You are not alone in this repository. Work only in your assigned worktree and owned paths. ' +
    "Do not revert others' changes. Preserve any unexpected work and report it. Stage exact intended files. " +
    'Do not commit, push, open a PR, merge, deploy, or mutate product/session records. Report actual tests and limitations. ' +
    'Put no AI or tool names into code, comments or messages.\n\n' +
    `Return the report as the structured output. dispatch_id must be exactly ${t.dispatch_id}. ` +
    'summary: what actually changed and why. tests: every command you ran as an argument array, its exit code and the observed outcome ' +
    '(an empty list means no tests ran). limitations: specific behaviour you did not check.'
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
