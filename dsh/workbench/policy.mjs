import { appendFileSync,readFileSync,existsSync } from 'node:fs';
import { join } from 'node:path';
export const name='modab-workbench-policy';
export const inject=['tools'];
export function apply(ctx,config){
  const events=join(config.stateDir,'dialogue-events.jsonl'),admissions=join(config.stateDir,'dialogue-admissions.jsonl');
  const log=(file,data)=>appendFileSync(file,JSON.stringify({time:new Date().toISOString(),...data})+'\n');
  const allowed=['modab_research_context','modab_draft_research_task'];
  const ids=new Set();
  ctx.on('agent/created',({agent})=>{ids.add(agent.id);agent.ctx.get('tools').restrict({allow:allowed});log(events,{kind:'agent-created',sessionId:agent.id,tools:ctx.tools.schemas(agent).map(t=>t.name)})});
  ctx.on('agent/request',async({agent,turn,step,signal},next)=>{
    signal.throwIfAborted();const call=await next();signal.throwIfAborted();
    const previous=existsSync(admissions)?readFileSync(admissions,'utf8').split('\n').filter(Boolean).map(JSON.parse):[];
    // Conservatively reserve output plus the complete serialized context in characters.
    const reservation=JSON.stringify(call.context??call.messages??call).length+4096;
    if(previous.reduce((s,r)=>s+r.reservedTokens,0)+reservation>config.tokenBudget)throw Error('研究讨论的委托令牌额度已用尽');
    log(admissions,{sessionId:agent.id,turn,step,provider:call.provider,model:call.model,maxTokens:4096,reservedTokens:reservation});
    return {...call,maxTokens:4096};
  });
  ctx.on('session/event',(session,event)=>{if(ids.has(session.id)&&['assistant/message','tool/call','tool/result','turn/end','step/end'].includes(event.type))log(events,{kind:'session-event',sessionId:session.id,event})});
}
