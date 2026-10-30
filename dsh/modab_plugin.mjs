import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'modab-plan-registry';
export const inject = ['tools'];
const entry = fileURLToPath(new URL('../backend/modab/registry_bridge.py', import.meta.url));
const requestSchema = 'modab_plan_registry_request_v1';
const responseSchema = 'modab_plan_registry_response_v1';
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/;
const text = { type: 'string' };
const nullableText = { oneOf: [text, { type: 'null' }] };
const object = (properties) => ({
  type: 'object', properties, required: Object.keys(properties), additionalProperties: false,
});
const parameters = object({ project_id: text, plan_id: text });
const outputSchema = object({
  schema: { type: 'string', const: responseSchema },
  project_id: text, plan_id: text,
  evidence_kind: { type: 'string', enum: ['ENGINEERING_FIXTURE_ONLY', 'EXTERNAL_RECORD_UNVERIFIED'] },
  entries: { type: 'array', items: object({
    legacy_task_id: text,
    status: { type: 'string', enum: ['NOT_RUN', 'SUBMISSION_PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED'] },
    input_sha256: text, attempt_id: nullableText, external_ref: nullableText,
    receipt_sha256: nullableText, error: nullableText,
  }) },
});

function validateArgs(args) {
  if (!args || typeof args !== 'object' || Array.isArray(args) ||
      Object.keys(args).sort().join(',') !== 'plan_id,project_id') {
    throw new Error('Expected exactly project_id and plan_id');
  }
  for (const key of ['project_id', 'plan_id']) {
    if (typeof args[key] !== 'string' || !idPattern.test(args[key])) {
      throw new Error('Invalid ' + key);
    }
  }
}

async function invoke(config, operation, args, signal) {
  validateArgs(args);
  signal.throwIfAborted();
  const payload = JSON.stringify({ schema: requestSchema, operation, ...args });
  return new Promise((resolve, reject) => {
    const child = spawn(config.python, [
      '-B', '-I', '-S', '-X', 'utf8', entry, '--workspace', config.workspace,
    ], { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], signal });
    let failure;
    const output = [];
    const errors = [];
    let outputBytes = 0;
    let errorBytes = 0;
    const stop = (error) => {
      failure ??= error;
      child.kill('SIGTERM');
    };
    const timer = setTimeout(() => stop(new Error('Registry bridge timeout: 30000 ms')), 30000);
    child.on('error', (error) => { failure ??= error; });
    child.stdin.on('error', stop);
    child.stdout.on('data', (chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > 1048576) stop(new Error('Registry stdout exceeds 1048576 bytes'));
      else output.push(chunk);
    });
    child.stderr.on('data', (chunk) => {
      errorBytes += chunk.length;
      if (errorBytes > 1048576) stop(new Error('Registry stderr exceeds 1048576 bytes'));
      else errors.push(chunk);
    });
    child.once('close', (code, killedBy) => {
      clearTimeout(timer);
      if (failure) return reject(failure);
      if (signal.aborted) return reject(signal.reason);
      const stdout = Buffer.concat(output).toString('utf8');
      const stderr = Buffer.concat(errors).toString('utf8');
      if (code !== 0) {
        return reject(new Error('Registry process exit=' + code + ', signal=' +
          killedBy + ': ' + (stdout || stderr)));
      }
      try {
        const value = JSON.parse(stdout);
        if (value.schema !== responseSchema || value.project_id !== args.project_id ||
            value.plan_id !== args.plan_id || !Array.isArray(value.entries)) {
          throw new Error('Registry response identity mismatch');
        }
        resolve(value);
      } catch (error) {
        reject(error);
      }
    });
    child.stdin.end(payload, 'utf8');
  });
}

export function apply(ctx, config) {
  for (const key of ['python', 'workspace']) {
    if (typeof config?.[key] !== 'string' || !isAbsolute(config[key])) {
      throw new Error('Plugin configuration requires an absolute ' + key + ' path');
    }
  }
  for (const operation of ['register', 'query']) {
    ctx.tools.register({
      name: 'modab_' + operation + '_plan',
      description: operation === 'register'
        ? 'Register records for an already approved plan. Does not approve a plan or start computation.'
        : 'Read stored records for a plan, including failures. Does not submit or restart work.',
      parameters,
      output: {
        schema: outputSchema,
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      execute: (args, exec) => invoke(config, operation, args, exec.signal),
    });
  }
}
