"""Per-job transport routing is persisted before any SSH submission."""
import base64
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys

from dualctx.contracts import ContractError
from .contracts import atomic_json, parse, read
from .jobs import LocalJobs


class JobRouter(LocalJobs):
    def bind_remote(self, job_id, runtime):
        route={k:runtime[k] for k in ('ssh','ssh_target','remote_root','control_python','endpoint','max_collect_bytes')}
        atomic_json(self.directory(job_id)/'route.json',route,immutable=True)

    def _route(self,job_id):
        path=self.directory(job_id)/'route.json'
        return read(path) if path.exists() else None

    def request(self,route,operation,job_id,**payload):
        import json
        command=shlex.join([route['control_python'],route['endpoint'],route['remote_root']])
        process=subprocess.run([route['ssh'],'-T','-o','BatchMode=yes','-o','ConnectTimeout=10',route['ssh_target'],command],
            input=json.dumps({'operation':operation,'job_id':job_id,**payload}),text=True,encoding='utf-8',
            capture_output=True,timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=False)
        if process.returncode!=0:raise ContractError('Remote operation response unavailable; reconcile original job')
        result=parse(process.stdout,limit=32*1024*1024)
        if 'error' in result:raise ContractError(result['error'])
        return result

    def submit(self,manifest):
        route=self._route(manifest['job_id'])
        if not route:return super().submit(manifest)
        directory=self.directory(manifest['job_id'])
        atomic_json(directory/'manifest.json',manifest,immutable=True)
        files={p.name:base64.b64encode(p.read_bytes()).decode() for p in directory.iterdir()
            if p.name in {'input.yaml','prediction_runner.py','request.json','pose_template.cif'}}
        try:return self.request(route,'submit',manifest['job_id'],manifest=manifest,files=files)
        except (ContractError,OSError,subprocess.TimeoutExpired):
            return {'status':'UNKNOWN','job_id':manifest['job_id'],'reason':'SSH submission response unavailable; original job must be queried'}

    def status(self,job_id):
        route=self._route(job_id)
        if not route:return super().status(job_id)
        # Once collected, the local signed-by-hash worker receipt is sufficient.
        if (self.directory(job_id)/'receipt.json').exists():return super().status(job_id)
        try:
            state=self.request(route,'status',job_id)
            if state['status'] in {'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT'}:
                result=self.request(route,'collect',job_id,max_bytes=route['max_collect_bytes'])
                directory=self.directory(job_id)
                for name,encoded in result['files'].items():
                    rel=PurePosixPath(name)
                    if rel.is_absolute() or '..' in rel.parts:raise ContractError('Invalid remote collection path')
                    path=directory.joinpath(*rel.parts);path.parent.mkdir(parents=True,exist_ok=True)
                    data=base64.b64decode(encoded,validate=True)
                    if path.exists() and path.read_bytes()!=data:raise ContractError('Collected file changed')
                    if not path.exists():
                        with path.open('xb') as stream:stream.write(data)
                return super().status(job_id)
            return state
        except (ContractError,OSError,subprocess.TimeoutExpired):
            return {'status':'UNKNOWN','job_id':job_id,'reason':'SSH state or result collection unavailable; no new submission permitted'}

    def cancel(self,job_id):
        route=self._route(job_id)
        if not route:return super().cancel(job_id)
        return self.request(route,'cancel',job_id)
