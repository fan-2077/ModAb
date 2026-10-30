import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export const name = 'modab-agent-policy';
export const inject = ['tools', 'appExit'];

export function apply(ctx, config) {
  const rules = JSON.parse(readFileSync(config.runFile, 'utf8'));
  if (rules.schema !== 'modab_web_dialogue_run_v1') throw new Error('Unknown run rules');
  for (const key of ['maxOutputTokens', 'maxRequestsPerTurn', 'turnTimeoutMs']) {
    if (!Number.isSafeInteger(rules[key]) || rules[key] <= 0 || rules[key] > 2147483647) {
      throw new Error('Invalid Agent limit: ' + key);
    }
  }
  const dir = dirname(config.runFile);
  const admissions = join(dir, 'admissions.jsonl');
  const events = join(dir, 'session-events.jsonl');
  const agents = new Set();
  const timers = new Map();
  const types = new Set([
    'system/message', 'user/message', 'assistant/message', 'assistant/attempt',
    'request/header', 'request/context', 'step/start', 'step/end', 'turn/start',
    'turn/end', 'tool/call', 'tool/result', 'llm/retry', 'llm/retry-started',
  ]);
  const append = (file, value) =>
    appendFileSync(file, JSON.stringify(value) + '\n', 'utf8');
  const read = file => existsSync(file)
    ? readFileSync(file, 'utf8').split('\n').filter(Boolean).map(JSON.parse) : [];
  const clear = id => {
    const active = timers.get(id);
    if (!active) return;
    clearTimeout(active.timer);
    active.signal.removeEventListener('abort', active.abort);
    timers.delete(id);
  };

  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.get('tools').restrict({ allow: rules.tools });
    const names = ctx.tools.schemas(agent).map(item => item.name).sort();
    if (JSON.stringify(names) !== JSON.stringify(rules.tools)) {
      throw new Error('ModAb visible tool set differs from approved list: ' +
        JSON.stringify({ expected: rules.tools, actual: names }));
    }
    agents.add(agent.id);
    append(events, { kind: 'agent-created', sessionId: agent.id, tools: names });
  });
  ctx.on('session/event', (session, event) => {
    if (agents.has(session.id) && types.has(event.type)) {
      append(events, { kind: 'session-event', sessionId: session.id, event });
    }
  });
  ctx.on('agent/status', ({ agent, status }) => {
    if (status === 'idle') clear(agent.id);
  });
  ctx.on('agent/disposed', ({ agent }) => {
    clear(agent.id);
    agents.delete(agent.id);
  });
  ctx.on('agent/request', async ({ agent, turn, step, signal }, next) => {
    signal.throwIfAborted();
    const call = await next();
    signal.throwIfAborted();
    const used = read(admissions).filter(row =>
      row.sessionId === agent.id && row.turn === turn).length;
    if (used >= rules.maxRequestsPerTurn) {
      throw new Error('ModAb per-turn model-request budget exhausted');
    }
    if (!timers.has(agent.id)) {
      const abort = () => clear(agent.id);
      const timer = setTimeout(() => {
        agent.cancel({ kind: 'hook', reason: 'ModAb turn time limit reached' });
      }, rules.turnTimeoutMs);
      timers.set(agent.id, { timer, signal, abort });
      signal.addEventListener('abort', abort, { once: true });
    }
    append(admissions, {
      time: new Date().toISOString(), sessionId: agent.id, turn, step,
      provider: call.provider, model: call.model,
      reasoningEffort: call.reasoningEffort ?? null,
      maxTokens: rules.maxOutputTokens,
    });
    return { ...call, maxTokens: rules.maxOutputTokens };
  });
  ctx.effect(() => {
    const stopTimer = setInterval(() => {
      if (existsSync(join(dir, 'STOP'))) ctx.appExit(0);
    }, 1000);
    return () => {
      clearInterval(stopTimer);
      for (const id of [...timers.keys()]) clear(id);
    };
  });
}
