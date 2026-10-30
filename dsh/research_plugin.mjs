import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'modab-research';
export const inject = ['tools'];
const entry = fileURLToPath(new URL('../backend/modab/research_bridge.py', import.meta.url));
const text = { type: 'string' };
const array = items => ({ type: 'array', items });
const object = properties => ({ type: 'object', properties,
  required: Object.keys(properties), additionalProperties: false });
const definitions = [
  ['context', 'modab_research_context',
    'Read the project evidence and actual method capability catalog for a research objective. Choose existing_only only when the user restricts work to existing results; otherwise full_evaluation. Returns a bound context ID. No prediction is run.',
    object({ objective: text, mode: { type: 'string', enum: ['full_evaluation', 'existing_only'] } })],
  ['propose', 'modab_propose_research_plan',
    'Save YOUR evidence-grounded method choice, real alternatives and steps. Use IDs from the context. Program checks references and assigns availability; it does not choose the method. This is a proposal, not approval of new scientific jobs.',
    object({ context_id: text, proposal: object({
      selected_method: { type: 'string', description: 'For full_evaluation: the later prediction method from context.plan_contract.allowed_selected_methods, NOT existing_evidence. For existing_only: existing_evidence. The first executable step is specified separately in steps.' }, rationale: text,
      alternatives: array(object({ method_id: text, reason: text })),
      evidence_ids: array({ type: 'string', description: 'Use only context.plan_contract.allowed_evidence_ids (or top-level context.evidence keys in older contexts). Nested field names such as short_contact_summary and limitations are not evidence IDs.' }),
      steps: array(object({ action: { type: 'string', enum: ['reuse_evidence', 'prepare_requirements', 'predict_structures', 'optimize_geometry'] }, method_id: text, purpose: text })),
      unknowns: array(text),
    }) })],
  ['execute', 'modab_execute_available',
    'Perform available LOCAL steps in a saved proposal: verify and reuse all original results; prepare selected method requirements if included. Returns an actual local artifact and receipt. Unintegrated predictors/optimizers remain NOT_RUN. Idempotent; never starts a server/GPU job.',
    object({ plan_id: text })],
  ['query', 'modab_query_research_plan',
    'Recover a saved research proposal and its exact local action receipt after a restart. Does not execute any work.',
    object({ plan_id: text })],
];

export async function invoke(config, operation, args, exec) {
  exec.signal.throwIfAborted();
  const origin = { session_id: String(exec.agent?.id ?? 'direct_tool_test'),
    tool_call_id: String(exec.callId ?? exec.rootCallId ?? 'direct_tool_test') };
  const payload = JSON.stringify({ arguments: args, origin });
  if (Buffer.byteLength(payload) > 65536) throw new Error('Research request exceeds limit');
  return new Promise((resolve, reject) => {
    const child = spawn(config.python, ['-B', '-I', '-S', '-X', 'utf8', entry,
      '--config', config.researchConfig, '--operation', operation],
      { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], signal: exec.signal });
    let failure, bytes = 0;
    const chunks = [], errors = [];
    const stop = error => { failure ??= error; child.kill(); };
    const timer = setTimeout(() => stop(new Error('Research bridge timeout')), 30000);
    child.on('error', error => { failure ??= error; });
    child.stdin.on('error', stop);
    for (const [stream, output] of [[child.stdout, chunks], [child.stderr, errors]]) {
      stream.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 1048576) stop(new Error('Research response exceeds limit'));
        else output.push(chunk);
      });
    }
    child.once('close', code => {
      clearTimeout(timer);
      if (failure) return reject(failure);
      if (exec.signal.aborted) return reject(exec.signal.reason);
      const stdout = Buffer.concat(chunks).toString('utf8');
      if (code !== 0) return reject(new Error(stdout || Buffer.concat(errors).toString('utf8')));
      try {
        const value = JSON.parse(stdout);
        if (value.schema !== 'modab_research_response_v1' || value.operation !== operation) {
          throw new Error('Research response identity mismatch');
        }
        resolve(value);
      } catch (error) { reject(error); }
    });
    child.stdin.end(payload, 'utf8');
  });
}

export function apply(ctx, config) {
  for (const key of ['python', 'researchConfig']) {
    if (!isAbsolute(config?.[key] ?? '')) throw new Error('Absolute ' + key + ' required');
  }
  for (const [operation, name, description, parameters] of definitions) {
    ctx.tools.register({ name, description, parameters,
      output: { schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      execute: (args, exec) => invoke(config, operation, args, exec),
    });
  }
}
