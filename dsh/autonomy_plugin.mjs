import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
export const name = 'modab-autonomy';
export const inject = ['tools'];
const entry = fileURLToPath(new URL('../backend/modab/autonomy/bridge.py', import.meta.url));
const object = properties => ({ type: 'object', properties, required: Object.keys(properties), additionalProperties: false });
const text = { type: 'string' };
export const definitions = [
  ['catalog', 'modab_research_task_catalog', 'Read verified task profiles, tool readiness and the local task-control link. No task is started.', object({})],
  ['draft', 'modab_draft_research_task', 'Save a research objective against one available data/resource profile. This does not approve or start work. Tell the user the task ID and control link; do not claim approval.', object({ profile_id: text, objective: text, request_id: text })],
  ['query', 'modab_query_research_task', 'Recover persistent task progress, actual tool receipts, resource accounting and validated reports. Closing a chat does not cancel a commissioned task.', object({ mission_id: text })],
];
export async function invoke(config, operation, args, exec) {
  exec.signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify({ arguments: args, origin: { session_id: String(exec.agent?.id ?? 'direct_tool_test'), tool_call_id: String(exec.callId ?? 'direct_tool_test') } });
    if (Buffer.byteLength(payload) > 65536) return reject(new Error('Task input exceeds limit'));
    const child = spawn(config.python, ['-B', '-I', '-S', '-X', 'utf8', entry, '--config', config.autonomyConfig, '--operation', operation],
      { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], signal: exec.signal });
    const chunks = []; let failure, size = 0;
    const timer = setTimeout(() => { failure = new Error('Task bridge timeout'); child.kill(); }, 30000);
    child.on('error', e => { failure ??= e; });
    child.stdin.on('error', e => { failure ??= e; });
    child.stdout.on('data', c => { size += c.length; if(size > 4 * 1024 * 1024) { failure = new Error('Task output exceeds limit'); child.kill(); } else chunks.push(c); });
    child.stderr.resume();
    child.once('close', code => {
      clearTimeout(timer);
      if (failure) return reject(failure);
      try { const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if(code !== 0 || value.schema !== 'modab_autonomy_response_v1' || value.operation !== operation) throw new Error(value.error ?? 'Task response mismatch');
        resolve(value);
      } catch(e) { reject(e); }
    });
    child.stdin.end(payload);
  });
}
export function apply(ctx, config) {
  for(const key of ['python','autonomyConfig']) if(!isAbsolute(config?.[key]??'')) throw new Error('Absolute '+key+' required');
  for(const [operation,name,description,parameters] of definitions) ctx.tools.register({name,description,parameters,
    output: {schema:{type:'object',additionalProperties:true},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
    execute:(args,exec)=>invoke(config,operation,args,exec)});
}
