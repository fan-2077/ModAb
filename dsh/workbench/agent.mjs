import { python, readLinks, bindSession } from './index.mjs';
import { invoke } from '../autonomy_plugin.mjs';
import { join } from 'node:path';
export const name='modab-workbench-agent';
export const inject=['tools'];
export function apply(ctx,config){
  const schema=properties=>({type:'object',properties,required:Object.keys(properties),additionalProperties:false});
  const register=(name,description,parameters,execute)=>ctx.tools.register({name,description,parameters,
    output:{schema:{type:'object',additionalProperties:true},render:(_,value)=>[{type:'text',text:JSON.stringify(value)}]},execute});
  register('modab_research_context','Read the real research task bound to this conversation, its measured results, explanations, execution history and next-test review. Call before discussing task results. Does not start computation.',schema({}),async(_,exec)=>{
    const state=await python(config),links=await readLinks(join(config.stateDir,'session-links.json'));
    const id=links[String(exec.agent?.id)];
    const mission=state.missions.find(m=>m.id===id);
    return {bound_mission_id:id??null,mission:mission??null,tasks:state.missions.map(m=>({id:m.id,title:m.title,status:m.status})),
      profiles:state.profiles,catalog:state.catalog,
      interpretation_rules:['比较时保持输入、方法和计数口径一致。','数值来自保留的真实预测；解释与后续假说和测量事实分开。','未磷酸化输入没有磷酸原子，其磷酸接触为零属于输入构造；该零值不能作为选择性证据，但未磷酸化组仍是有效配对对照，必须保留在全部状态、总接触及位置比较中，不能排除整个对照。','结构置信度、接触数和RMSD均不是结合力；不能预设更接近某个参考结构就对应更强结合或选择性，也不能把置信度评分视为接触数变化的原因。不同指标测量对象不同不等于它们在统计上相互独立。','历史停止原因和历史预算属于原任务，不是当前剩余额度。','未测量采样离散度时，不得声称组间差异已落在采样波动范围内。','重复检验要区分差异方向持续稳定和方向反转或被组内波动覆盖；解释这两类结果分别如何增强或削弱计算差异可重复性的依据。少量重复不自动证明可靠性。','先核对现有状态间已改变的因素，不能把已经存在的配对比较描述成缺失的新对照。条件匹配和随机种子变化本身不构成生物学因果证明。','历史后续假说需要独立审查；引用事实编号时使用完整编号。','若要求新计算，可保存明确的任务草案，由用户在工作台确认后启动；没有批准工具。']};
  });
  register('modab_draft_research_task','Create a persistent research task DRAFT from a supported profile and explicit user objective. Bind this conversation to the draft only when it has no bound task. User must review and approve in workbench before execution.',schema({profile_id:{type:'string'},objective:{type:'string'},request_id:{type:'string'}}),async(args,exec)=>{
    const result=await invoke(config,'draft',args,exec);
    const id=result.mission_id??result.result?.mission_id??result.mission?.mission_id;
    const sid=String(exec.agent?.id),file=join(config.stateDir,'session-links.json');
    if(id&&!(await readLinks(file))[sid])await bindSession(file,sid,id);
    return result;
  });
}
