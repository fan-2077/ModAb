import { readFile, writeFile, mkdir, rename } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'modab-research-workbench';
export const inject = ['webServer', 'connection'];
const actualBridge = fileURLToPath(new URL('../../backend/modab/autonomy/workbench.py', import.meta.url));

export function python(config, operation='state', args=[]) {
  return new Promise((resolve,reject) => {
    const child=spawn(config.python, ['-B','-X','utf8',config.bridge ?? actualBridge,'--config',config.autonomyConfig,'--operation',operation,...args],
      {windowsHide:true,stdio:['ignore','pipe','pipe']});
    let out='',err='';
    const timer=setTimeout(()=>child.kill(),30000);
    child.stdout.on('data',c=>{out+=c;if(out.length>6*1024*1024)child.kill()});
    child.stderr.on('data',c=>{err+=c});
    child.on('error',reject);
    child.on('close',code=>{clearTimeout(timer);if(code!==0)return reject(new Error(err.slice(-1200)||'研究记录读取失败'));
      try{resolve(JSON.parse(out))}catch(e){reject(e)}});
  });
}

export async function readLinks(file) {
  try{return JSON.parse(await readFile(file,'utf8'))}catch(e){if(e.code==='ENOENT')return {};throw e}
}
let linkQueue=Promise.resolve();
export function bindSession(file,sessionId,missionId){
  const work=linkQueue.catch(()=>{}).then(async()=>{
    const links=await readLinks(file);
    if(links[sessionId] && links[sessionId]!==missionId)throw new Error('此对话已关联其他研究任务，请新建对话');
    links[sessionId]=missionId;await mkdir(dirname(file),{recursive:true});
    const temp=file+'.tmp';await writeFile(temp,JSON.stringify(links,null,2));await rename(temp,file);return links;
  });linkQueue=work;return work;
}

export function apply(ctx,config){
  const csrf=randomBytes(24).toString('hex');
  const serviceConfig=JSON.parse(readFileSync(config.autonomyConfig,'utf8'));
  const upstream='http://127.0.0.1:'+serviceConfig.port;
  const linksFile=join(config.stateDir,'session-links.json');
  let cache,cacheAt=0,inflight;
  const getState=async()=>{
    if(cache&&Date.now()-cacheAt<1200)return cache;
    if(!inflight)inflight=python(config).then(v=>{cache=v;cacheAt=Date.now();return v}).finally(()=>{inflight=null});
    return inflight;
  };
  const send=(res,status,value,type='application/json; charset=utf-8')=>{
    const data=Buffer.isBuffer(value)?value:Buffer.from(typeof value==='string'?value:JSON.stringify(value));
    res.writeHead(status,{'Content-Type':type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Length':data.length});res.end(data);
  };
  ctx.effect(()=>ctx.webServer.register({kind:'prefix',path:'/modab',handler:async(req,res)=>{
    const rejected=ctx.connection.requestRejection(req);if(rejected!==undefined)return send(res,rejected,{error:'请从已连接的 ModAb 页面访问'});
    const url=new URL(req.url,ctx.webServer.getOrigin());const path=url.pathname;
    try{
      if(req.method==='GET'&&path==='/modab/workbench.css')return send(res,200,await readFile(new URL('./workbench.css',import.meta.url)),'text/css; charset=utf-8');
      if(req.method==='GET'&&path==='/modab/state')return send(res,200,{...await getState(),workspacePath:config.workspacePath??fileURLToPath(new URL('../../',import.meta.url)).replace(/\\/g,'/').replace(/\/$/,''),links:await readLinks(linksFile),csrf});
      if(req.method==='GET'&&['/modab/structure','/modab/asset'].includes(path)){
        const mid=url.searchParams.get('mission'),aid=url.searchParams.get('asset');
        if(!/^research_[a-f0-9]+$/.test(mid)||!/^artifact_[a-f0-9]+$/.test(aid))throw new Error('无效的任务或产物标识');
        const data=await python(config,path.endsWith('structure')?'structure':'asset',['--mission',mid,'--asset',aid]);
        if(path.endsWith('structure'))return send(res,200,data);
        res.setHeader('Content-Disposition','attachment; filename="'+data.name.replace(/[^a-zA-Z0-9._-]/g,'_')+'"');
        return send(res,200,await readFile(data.path),'application/octet-stream');
      }
      if(req.method==='GET'&&path.startsWith('/modab/report/')){
        const mid=path.split('/').at(-1);if(!/^research_[a-f0-9]+$/.test(mid))throw new Error('无效任务');
        const r=await fetch(upstream+'/report/'+mid,{signal:AbortSignal.timeout(10000)});
        return send(res,r.status,await r.text(),'text/html; charset=utf-8');
      }
      if(req.method==='POST'){
        if(req.headers.origin!==ctx.webServer.getOrigin()||req.headers['x-modab-workbench']!==csrf)return send(res,403,{error:'仅接受当前页面中的用户操作'});
        let body='';for await(const part of req){body+=part;if(Buffer.byteLength(body)>65536)throw new Error('请求过大')}
        const value=JSON.parse(body);
        if(path==='/modab/bind'){
          if(!/^session-[a-zA-Z0-9-]+$/.test(value.session_id))throw new Error('无效对话');
          const state=await getState();if(!state.missions.some(m=>m.id===value.mission_id))throw new Error('任务不存在');
          return send(res,200,{links:await bindSession(linksFile,value.session_id,value.mission_id)});
        }
        const allowed={'/modab/draft':'/api/draft','/modab/approve':'/api/approve','/modab/control':'/api/control'};
        if(allowed[path]){
          const token=await readFile(join(serviceConfig.workspace,'service.token'),'utf8');
          const r=await fetch(upstream+allowed[path],{method:'POST',headers:{'Content-Type':'application/json','Origin':upstream,'X-ModAb-Control':token},body:JSON.stringify(value),signal:AbortSignal.timeout(15000)});
          cacheAt=0;return send(res,r.status,await r.json());
        }
      }
      return send(res,404,{error:'未找到该入口'});
    }catch(e){return send(res,400,{error:e.message})}
  }}));
  ctx.effect(()=>ctx.webServer.tapIndex(html=>html.replace('<title>','<title>ModAb · ').replace('</head>','<link rel="stylesheet" href="modab/workbench.css"></head>')));
}
