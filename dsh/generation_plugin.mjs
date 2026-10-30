import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'modab-generation';
export const inject = ['tools'];
const entry = fileURLToPath(new URL('../backend/modab/generation_bridge.py', import.meta.url));
const pattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/;

export function apply(ctx, config) {
  for (const key of ['python', 'jobConfig']) {
    if (!isAbsolute(config?.[key] ?? '')) throw new Error('Absolute ' + key + ' required');
  }
  for (const [operation, name] of [['start', 'modab_start_plan'], ['query', 'modab_query_generation']]) {
    ctx.tools.register({
      name,
      description: operation === 'start'
        ? 'Start the exact already approved frozen generation plan once. Repeated calls look up the same job; never retry candidates.'
        : 'Query the same server generation job, synchronizing its execution receipt. Does not start or retry work.',
      parameters: { type: 'object', properties: { project_id: { type: 'string' }, plan_id: { type: 'string' } },
        required: ['project_id', 'plan_id'], additionalProperties: false },
      output: { schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      execute: (args, exec) => {
        if (!args || Object.keys(args).sort().join(',') !== 'plan_id,project_id' ||
            !pattern.test(args.project_id) || !pattern.test(args.plan_id)) throw new Error('Exact approved IDs required');
        exec.signal.throwIfAborted();
        return new Promise((resolve, reject) => {
          const child = spawn(config.python, ['-B', '-I', '-S', '-X', 'utf8', entry,
            '--config', config.jobConfig, '--operation', operation],
            { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], signal: exec.signal });
          let failure, bytes = 0;
          const chunks = [], errors = [];
          const stop = e => { failure ??= e; child.kill(); };
          const timer = setTimeout(() => stop(new Error('Generation bridge timeout; query the same job')), 28000);
          child.on('error', error => { failure ??= error; });
          child.stdin.on('error', stop);
          for (const [stream, output] of [[child.stdout, chunks], [child.stderr, errors]]) {
            stream.on('data', chunk => {
              bytes += chunk.length;
              if (bytes > 1048576) stop(new Error('Generation response exceeds limit'));
              else output.push(chunk);
            });
          }
          child.once('close', code => {
            clearTimeout(timer);
            if (failure) return reject(failure);
            if (code !== 0) return reject(new Error(Buffer.concat(chunks.length ? chunks : errors).toString('utf8')));
            try {
              const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
              if (value.schema !== 'modab_generation_response_v1' || value.project_id !== args.project_id || value.plan_id !== args.plan_id)
                throw new Error('Generation response binding mismatch');
              resolve(value);
            } catch (error) { reject(error); }
          });
          child.stdin.end(JSON.stringify({ schema: 'modab_plan_registry_request_v1', operation: 'query', ...args }));
        });
      },
    });
  }
}
