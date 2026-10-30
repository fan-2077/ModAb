window.__ModuleLoader__.load({id:'modab-research-workbench',factory:require=>{
  const R=require('react'),h=R.createElement,{useState,useEffect,useRef,useSyncExternalStore}=R;
  let snapshot={data:null,error:null},listeners=new Set(),timer,request;
  const publish=v=>{snapshot=v;for(const f of listeners)f()};
  async function refresh(){
    if(request)return request;
    request=fetch('/modab/state').then(async r=>{const d=await r.json();if(!r.ok)throw Error(d.error);publish({data:d,error:null})})
      .catch(e=>publish({...snapshot,error:e.message})).finally(()=>{request=null});return request;
  }
  function subscribe(f){listeners.add(f);if(listeners.size===1){refresh();timer=setInterval(()=>{if(!document.hidden)refresh()},10000)}return()=>{listeners.delete(f);if(!listeners.size)clearInterval(timer)}}
  const useResearch=()=>useSyncExternalStore(subscribe,()=>snapshot);
  async function post(path,value){if(!snapshot.data)await refresh();const r=await fetch('/modab/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-ModAb-Workbench':snapshot.data.csrf},body:JSON.stringify(value)});const d=await r.json();if(!r.ok)throw Error(d.error||'操作失败');await refresh();return d}
  const labels={COMPLETED:'研究已结束',COMPLETED_WITH_LIMITATIONS:'已完成 · 有待检验问题',RUNNING:'正在研究',DRAFT:'等待确认',PAUSED:'已暂停',NEEDS_ATTENTION:'需要处理',CANCELLED:'已取消',SUCCEEDED:'已完成',FAILED:'失败',QUEUED:'排队中',PENDING:'待执行',APPROVED:'已委托'};
  const fmt=(v,n=2)=>typeof v==='number'?(Number.isInteger(v)?String(v):v.toFixed(n)):'—';
  const badge=s=>h('span',{className:'mw-badge '+(s?.includes('FAIL')?'mw-error':'')},labels[s]??s);
  const button=(text,onClick,props={})=>h('button',{type:'button',className:'mw-button',onClick,...props},text);
  function ErrorBox({error}){return error?h('div',{role:'alert',className:'mw-alert'},error):null}
  const metrics=[['Tau_CA_RMSD_after_Fv_fit_A','Tau 位置偏差','Å'],['antibody_peptide_residue_pair_contacts','总残基对接触','对'],['phosphate_atom_residue_pair_contacts','磷酸参与的残基对接触','对']];
  function Compare({mission}){
    const [metric,setMetric]=useState(metrics[0][0]);const def=metrics.find(x=>x[0]===metric);const rows=mission.comparisons;
    if(!rows.length)return h('p',{className:'mw-muted'},'尚未产生结构测量结果。');
    const max=Math.max(1,...rows.map(r=>r.metrics[metric]??0));
    return h('section',{className:'mw-section'},h('div',{className:'mw-section-title'},h('h3',null,'状态比较'),h('span',null,rows.length+' 个计算样本')),
      h('label',{className:'mw-select-label'},'比较指标',h('select',{value:metric,onChange:e=>setMetric(e.target.value)},metrics.map(m=>h('option',{key:m[0],value:m[0]},m[1])))),
      h('div',{className:'mw-chart',role:'img','aria-label':def[1]+'比较图'},rows.map((r,i)=>h('div',{className:'mw-chart-row',key:r.subject},h('span',null,r.label),h('div',{className:'mw-bar-track'},h('div',{className:'mw-bar',style:{width:(100*(r.metrics[metric]??0)/max)+'%',background:['#276a61','#5484b0','#ae7647'][i%3]}})),h('strong',null,fmt(r.metrics[metric])+' '+def[2])))),
      h('p',{className:'mw-caption'},metric===metrics[0][0]?'将抗体 Fv 的 Cα 原子对齐实验参考后，比较共享 Tau 残基的 Cα 坐标。该量同时受肽段形状和相对位置影响。':'按原任务 '+fmt(rows[0].metrics.contact_cutoff_A)+' Å 阈值计数。残基对数为绝对计数；未磷酸化输入本身没有磷酸原子。'),
      h('div',{className:'mw-table-scroll'},h('table',null,h('thead',null,h('tr',null,h('th',null,'状态'),...metrics.map(m=>h('th',{key:m[0]},m[1]+' / '+m[2])))),h('tbody',null,rows.map(r=>h('tr',{key:r.subject},h('td',null,r.label),...metrics.map(m=>h('td',{key:m[0],title:r.fact_ids[m[0]]},fmt(r.metrics[m[0]])))))))));
  }
  function Structure({mission}){
    const [id,setId]=useState(mission.structures[0]?.id??''),[data,setData]=useState(null),[err,setErr]=useState(null),[rotation,setRotation]=useState([-.25,.4]),[zoom,setZoom]=useState(1);const canvas=useRef(),drag=useRef();
    useEffect(()=>{let alive=true;setData(null);setErr(null);if(id)fetch('/modab/structure?mission='+mission.id+'&asset='+id).then(async r=>{const d=await r.json();if(!r.ok)throw Error(d.error);if(alive)setData(d)}).catch(e=>{if(alive)setErr(e.message)});return()=>{alive=false}},[id,mission.id]);
    useEffect(()=>{if(!data||!canvas.current)return;const c=canvas.current,g=c.getContext('2d'),w=c.width,ht=c.height;g.clearRect(0,0,w,ht);const all=Object.values(data.chains).flat();const center=[0,1,2].map(i=>all.reduce((s,a)=>s+a.xyz[i],0)/all.length);const radius=Math.max(...all.map(a=>Math.hypot(...a.xyz.map((v,i)=>v-center[i]))),1);const sc=Math.min(w,ht)*.40/radius*zoom;const [rx,ry]=rotation;
      const project=a=>{let [x,y,z]=a.xyz.map((v,i)=>v-center[i]);[x,z]=[x*Math.cos(ry)+z*Math.sin(ry),-x*Math.sin(ry)+z*Math.cos(ry)];[y,z]=[y*Math.cos(rx)-z*Math.sin(rx),y*Math.sin(rx)+z*Math.cos(rx)];return [w/2+x*sc,ht/2-y*sc,z]};
      const colors={A:'#4f87ba',B:'#76b3aa',C:'#db9654'},segments=[];
      for(const [chain,atoms] of Object.entries(data.chains)){for(let i=1;i<atoms.length;i++){if(atoms[i].residue!==atoms[i-1].residue+1)continue;const a=project(atoms[i-1]),b=project(atoms[i]);segments.push({a,b,chain,z:(a[2]+b[2])/2})}}
      segments.sort((a,b)=>a.z-b.z);for(const s of segments){g.strokeStyle=colors[s.chain]??'#888';g.lineWidth=s.chain==='C'?5:3;g.lineCap='round';g.beginPath();g.moveTo(...s.a.slice(0,2));g.lineTo(...s.b.slice(0,2));g.stroke()}
      for(const a of data.chains.C??[]){if(['SEP','TPO'].includes(a.name)){const p=project(a);g.fillStyle='#b65c25';g.beginPath();g.arc(p[0],p[1],5,0,Math.PI*2);g.fill()}}
    },[data,rotation,zoom]);
    if(!mission.structures.length)return h('p',{className:'mw-muted'},'预测结构将在任务完成相应步骤后出现。');
    return h('section',{className:'mw-section'},h('div',{className:'mw-section-title'},h('h3',null,'预测结构'),h('span',null,'可旋转 · Cα 主链')),
      h('label',{className:'mw-select-label'},'结构状态',h('select',{value:id,onChange:e=>setId(e.target.value)},mission.structures.map(s=>h('option',{key:s.id,value:s.id},s.label+' · seed '+(s.seed??'未记录'))))),
      h(ErrorBox,{error:err}),h('div',{className:'mw-structure'},!data&&!err?h('span',{className:'mw-loading'},'读取原始坐标…'):null,
        h('canvas',{ref:canvas,width:760,height:440,role:'img','aria-label':'预测复合物主链；拖动旋转',tabIndex:0,
          onPointerDown:e=>{drag.current=[e.clientX,e.clientY];e.currentTarget.setPointerCapture(e.pointerId)},
          onPointerMove:e=>{if(!drag.current)return;const [x,y]=drag.current;setRotation(r=>[r[0]+(e.clientY-y)*.009,r[1]+(e.clientX-x)*.009]);drag.current=[e.clientX,e.clientY]},onPointerUp:()=>{drag.current=null},onPointerCancel:()=>{drag.current=null},
          onKeyDown:e=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();setRotation(r=>[r[0]+(e.key==='ArrowUp'?-.1:e.key==='ArrowDown'?.1:0),r[1]+(e.key==='ArrowLeft'?-.1:e.key==='ArrowRight'?.1:0)])}}})),
      h('div',{className:'mw-view-controls'},h('span',null,h('i',{style:{background:'#4f87ba'}}),'链 A',h('i',{style:{background:'#76b3aa'}}),'链 B',h('i',{style:{background:'#db9654'}}),'Tau · 链 C'),button('重置视角',()=>{setRotation([-.25,.4]);setZoom(1)})),
      h('label',{className:'mw-zoom'},'缩放',h('input',{type:'range',min:.6,max:2,step:.05,value:zoom,onChange:e=>setZoom(+e.target.value)})),
      h('p',{className:'mw-caption'},'显示各预测文件原坐标中的 Cα 连线，未与参考结构叠合。深橙标记修饰残基的 Cα；侧链与磷酸原子未显示。'),
      h('a',{className:'mw-link',href:'/modab/asset?mission='+mission.id+'&asset='+id,download:true},'下载当前结构 mmCIF ↗'));
  }
  function Plan({mission}){return h('section',{className:'mw-section'},h('h3',null,'研究过程'),h('ol',{className:'mw-timeline'},mission.steps.map((s,i)=>h('li',{key:s.id},h('span',{className:'mw-step-number'},i+1),h('div',null,h('div',{className:'mw-step-heading'},h('strong',null,s.title),badge(s.status)),h('p',null,s.reason||'等待研究控制器给出本步骤的执行依据。'),s.error?h(ErrorBox,{error:typeof s.error==='string'?s.error:JSON.stringify(s.error)}):null,h('details',null,h('summary',null,'参数与来源'+(s.seconds!=null?' · '+fmt(s.seconds)+' 秒':'')),h('pre',null,JSON.stringify(s.parameters,null,2)),...s.assets.map(a=>h('a',{key:a.id,className:'mw-source',href:'/modab/asset?mission='+mission.id+'&asset='+a.id},a.name))))))));}
  function Findings({mission}){return h(R.Fragment,null,h('section',{className:'mw-section'},h('h3',null,'主要发现'),mission.interpretations.length?mission.interpretations.map(c=>h('article',{key:c.id,className:'mw-finding'},h('small',null,c.kind==='limitation'?'适用条件':'研究解释 · 待进一步检验'),h('p',null,c.text),h('details',null,h('summary',null,'查看所引测量'),h('ul',null,(c.fact_ids??[]).map(id=>{const f=mission.facts.find(x=>x.id===id);return h('li',{key:id},f?f.subject+' · '+f.metric+': '+fmt(f.value)+' '+(f.unit??''):id)}))))):h('p',{className:'mw-muted'},'Agent 会根据实际计算结果生成研究解释。')),h('section',{className:'mw-section'},h('h3',null,'本任务比较条件'),...(mission.evidence_summary?.constructs??[]).map((c,i)=>h('p',{key:i},c.antibody_form+' · Tau '+c.tau_start+'–'+c.tau_end+'，共 '+c.tau_residue_count+' 个残基；'+(mission.evidence_summary?.measured_samples_in_this_mission??0)+' 个已测样本。')),h('p',{className:'mw-caption'},mission.evidence_summary?.within_state_variation_estimated===false?'每个状态只有一个已测样本，同状态采样波动尚未估计。未磷酸化组保留为配对对照。':'跨任务合并时需核对输入、方法和随机种子，重复运行不自动增加独立样本。')),h(Compare,{mission}));}
  function Mission({mission,onDiscuss,compact=false}){
    const [tab,setTab]=useState('findings'),[error,setError]=useState(null),[busy,setBusy]=useState(false);const finished=['COMPLETED','COMPLETED_WITH_LIMITATIONS','CANCELLED'].includes(mission.status);
    async function act(action){setBusy(true);setError(null);try{await post(action==='approve'?'approve':'control',action==='approve'?{mission_id:mission.id,commission_sha256:mission.commission_sha256}:{mission_id:mission.id,action})}catch(e){setError(e.message)}finally{setBusy(false)}}
    return h('div',{className:'mw-mission'+(compact?' mw-compact':'')},h('header',{className:'mw-mission-header'},h('div',null,h('div',{className:'mw-eyebrow'},'RESEARCH / AT8'),h('h2',null,mission.title),badge(mission.status)),onDiscuss?button('继续研究对话 ↗',()=>onDiscuss(mission),{className:'mw-button mw-primary'}):null),
      h(ErrorBox,{error}),h('nav',{className:'mw-tabs','aria-label':'研究内容'},[['findings','发现与比较'],['structure','三维结构'],['plan','计划与执行'],['scope','任务与资源']].map(([id,label])=>button(label,()=>setTab(id),{key:id,'aria-pressed':tab===id,className:tab===id?'mw-tab active':'mw-tab'}))),
      tab==='findings'?h(Findings,{mission}):tab==='structure'?h(Structure,{mission}):tab==='plan'?h(Plan,{mission}):h('section',{className:'mw-section'},h('h3',null,'研究目标'),h('p',null,mission.objective),h('h3',null,'本任务资源记录'),h('p',{className:'mw-caption'},'下表为创建本任务时的委托与实际累计消耗。新任务使用当前可用范围。'),h('pre',null,JSON.stringify({budget:mission.budget,usage:mission.usage},null,2)),h('details',null,h('summary',null,'任务身份与结束原因'),h('p',null,mission.id),h('p',null,mission.reason))),
      h('footer',{className:'mw-mission-footer'},mission.report_available?h('a',{className:'mw-link',href:'/modab/report/'+mission.id,target:'_blank',rel:'noopener'},'完整研究报告 ↗'):h('span',null,'报告将在研究过程中生成'),
        mission.status==='DRAFT'?button('确认此任务并开始',()=>act('approve'),{disabled:busy,className:'mw-button mw-primary'}):!finished?h('div',{className:'mw-actions'},button(mission.status==='PAUSED'?'恢复':'暂停',()=>act(mission.status==='PAUSED'?'resume':'pause'),{disabled:busy}),button('取消任务',()=>act('cancel'),{disabled:busy})):null));
  }
  function apply(ctx){
    try {
    const workspace=async()=>{if(!snapshot.data)await refresh();if(!snapshot.data?.workspacePath)throw Error('未取得工作区路径，请刷新工作台');return ctx.workspaces.create({path:snapshot.data.workspacePath})};
    async function startDiscussion(prompt){
      const ws=await workspace();const sid=await ctx.sessions.create({workspaceId:ws.workspaceId,agentPreset:'standard'});
      const ref=ctx.sessions.retain(sid,{source:'workspaceOperation'});
      try{const binding=await ref.ready;ctx.conversation.input.requestDraftInitialization(binding,{prompt,whenDraftExists:'append'});ctx.uiWorkspace.openSession(sid)}finally{ref.release()}
    }
    async function openMission(m,prompt){
      await refresh();const ws=await workspace();const linked=Object.keys(snapshot.data.links).filter(s=>snapshot.data.links[s]===m.id);
      let sid=linked.find(s=>ws.sessionIds.includes(s))??linked.find(s=>ctx.sessions.list.getSnapshot().byId[s]?.blank===false);
      if(!sid){
        sid=await ctx.sessions.create({workspaceId:ws.workspaceId,agentPreset:'standard'});
        await post('bind',{session_id:sid,mission_id:m.id});
      }
      const ref=ctx.sessions.retain(sid,{source:'workspaceOperation'});
      try{const binding=await ref.ready;
        if(ctx.sessions.list.getSnapshot().byId[sid]?.blank)await binding.session.rename(m.title);
        if(prompt)ctx.conversation.input.requestDraftInitialization(binding,{prompt,whenDraftExists:'append'});
        ctx.uiWorkspace.openSession(sid);
      }finally{ref.release()}
    }
    function Dashboard(){
      const {data,error}=useResearch(),[selected,setSelected]=useState(null),[query,setQuery]=useState(''),[busy,setBusy]=useState(false),[localError,setLocalError]=useState(null);const mission=data?.missions.find(m=>m.id===selected)??data?.missions[0];
      async function discuss(m,prompt){setBusy(true);setLocalError(null);try{await openMission(m,prompt)}catch(e){setLocalError(e.message)}finally{setBusy(false)}}
      return h('main',{className:'mw-dashboard'},h('div',{className:'mw-topline'},h('span',null,'ModAb / 研究工作台'),h('span',{className:'mw-live'},'● 连接真实任务记录')),
        h('div',{className:'mw-intro'},h('div',{className:'mw-eyebrow'},'MOLECULAR RESEARCH, TOGETHER'),h('h1',null,'从研究问题，到可追问的证据。'),h('p',null,'与 Agent 讨论方法、跟进计算，并在同一项研究中查看结构与比较结果。')),
        h('form',{className:'mw-composer',onSubmit:async e=>{e.preventDefault();if(query.trim()&&!busy){setBusy(true);try{await startDiscussion(query.trim());setQuery('')}catch(e){setLocalError(e.message)}finally{setBusy(false)}}}},h('label',{htmlFor:'mw-objective'},'开始一项研究讨论'),h('textarea',{id:'mw-objective',value:query,onChange:e=>setQuery(e.target.value),placeholder:'描述你想研究的问题，例如：AT8 的预测结果能否区分 Tau 的磷酸化状态？',rows:2}),h('div',{className:'mw-composer-bottom'},h('small',null,'讨论形成方案后，在任务中确认计算范围。'),h('button',{className:'mw-button mw-primary',type:'submit',disabled:!query.trim()||busy},'进入对话 →'))),
        h(ErrorBox,{error:error||localError}),!data?h('p',null,'正在读取研究记录…'):h('div',{className:'mw-workspace'},h('aside',{className:'mw-projects'},h('h2',null,'研究任务'),...data.missions.map((m,i)=>button(h(R.Fragment,null,h('small',null,'研究 '+String(data.missions.length-i).padStart(2,'0')),h('strong',null,m.title),badge(m.status),h('span',null,m.steps.length+' 个步骤 · '+m.structures.length+' 个结构')),()=>setSelected(m.id),{key:m.id,className:'mw-project '+(m.id===mission?.id?'active':''),'aria-pressed':m.id===mission?.id}))),mission?h(Mission,{key:mission.id,mission,onDiscuss:m=>discuss(m),compact:false}):h('p',null,'目前没有研究任务，从上方对话开始。')),
        busy?h('div',{className:'mw-toast',role:'status'},'正在打开关联的研究对话…'):null);
    }
    function Panel({useTabInfo}){const info=useTabInfo(),{data,error}=useResearch();const id=info.tab.navigation.params?.missionId??data?.links[info.sessionId];const m=data?.missions.find(x=>x.id===id);return h('div',{className:'mw-detail-panel'},h(ErrorBox,{error}),m?h(Mission,{key:m.id,mission:m,compact:true}):h('p',{className:'mw-section'},'正在载入关联研究…'))}
    function Opener({sessionId,useSidebarMounted}){const {data}=useResearch(),mounted=useSidebarMounted(s=>s!==undefined),opened=useRef(null);const id=data?.links[sessionId];
      const open=()=>ctx.sidebarRight.openTab('modab-research',{params:{missionId:id}});
      useEffect(()=>{if(id&&mounted&&opened.current!==id){open();opened.current=id}},[id,mounted]);
      return id?button('研究详情',open,{className:'mw-button mw-header-action'}):null;
    }
    function AutoPanel({useSidebarMounted}){const sid=useSidebarMounted(s=>s),{data}=useResearch(),opened=useRef(null);const id=data?.links[sid];useEffect(()=>{if(id&&sid&&opened.current!==sid+id){ctx.sidebarRight.openTab('modab-research',{params:{missionId:id}});opened.current=sid+id}},[sid,id]);return null}
    const reg=(name,options,component)=>ctx.slots.inject(name,()=>ctx.slots.register({name,...options},component));
    reg('main',{key:'modab-workbench'},Dashboard);
    reg('sidebar.panellist',{id:'modab-workbench',order:-100,label:()=> '研究工作台'},({size})=>h('svg',{width:size,height:size,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor',strokeWidth:1.7},h('path',{d:'M5 4h14v16H5zM8 8h8M8 12h3M8 16h8M15 11v3m-1.5-1.5h3'})));
    reg('sidebar.brand.name',{priority:-100},()=>h('strong',{className:'mw-brand'},'ModAb'));
    reg('conversation.hero.brand.mark',{priority:-100},()=>h('div',{className:'mw-chat-brand'},h('strong',null,'ModAb'),h('span',null,'让研究问题与证据持续对话')));
    ctx.effect(()=>ctx.sidebarRightTabs.register({id:'modab-research',kind:'modab-research',title:()=> '研究详情'}));
    reg('sidebar.right.pane.tab',{key:'modab-research'},Panel);
    reg('conversation.session.header.utilities',{id:'modab-research',order:-100,inject:()=>({hooks:{sidebarMounted:ctx.sidebarRight.mounted}})},Opener);
    reg('shell.overlay',{id:'modab-research-auto-panel',inject:()=>({hooks:{sidebarMounted:ctx.sidebarRight.mounted}})},AutoPanel);
    } catch(error) { console.error('ModAb workbench activation failed',error); throw error; }
  }
  return {apply,inject:['slots','sessions','workspaces','uiWorkspace','conversation','sidebarRight','sidebarRightTabs']};
}});
