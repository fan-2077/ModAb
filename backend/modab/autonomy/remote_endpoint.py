"""Standalone SSH endpoint; installed only under an explicit runtime grant.

No web listener. The caller sends one JSON request over SSH stdin. Its owned
job directory and durable worker are preserved across client disconnections.
"""
from datetime import datetime, timezone
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import worker


def status(directory, job_id):
    manifest_path = directory/'manifest.json'
    if not manifest_path.exists():
        return {'status':'NOT_FOUND','job_id':job_id}
    manifest=json.loads(manifest_path.read_bytes());bound=worker.identity(manifest)
    if manifest['job_id']!=job_id:raise ValueError('Job identity mismatch')
    if (directory/'receipt.json').exists():
        result=json.loads((directory/'receipt.json').read_bytes())
        if worker.identity({k:v for k,v in result.items() if k!='receipt_sha256'})!=result['receipt_sha256'] or result['manifest_sha256']!=bound:
            raise ValueError('Remote receipt identity mismatch')
        return result
    if (directory/'state.json').exists():
        result=json.loads((directory/'state.json').read_bytes())
        if result['manifest_sha256']!=bound:raise ValueError('Remote state identity mismatch')
        if result['status'] in {'STARTING','RUNNING'} and (datetime.now(timezone.utc)-datetime.fromisoformat(result['heartbeat_utc'])).total_seconds()>15:
            result.update(status='UNKNOWN',reason='Worker heartbeat stale')
        return result
    claimed=(directory/'execution-once.json').exists()
    if claimed and (directory/'state.json').exists():return status(directory,job_id)
    return {'status':'UNKNOWN' if claimed else 'PREPARED','job_id':job_id,'manifest_sha256':bound}


def put(path,data):
    if path.exists():
        if path.read_bytes()!=data:raise ValueError('Immutable remote artifact differs')
    else:
        with path.open('xb') as out:
            out.write(data);out.flush();__import__('os').fsync(out.fileno())


def execute(root,request):
    jid=request['job_id']
    if not re.fullmatch(r'job_[a-zA-Z0-9_-]{1,120}',jid):raise ValueError('Invalid job identity')
    directory=root/'jobs'/jid
    operation=request['operation']
    if operation=='submit':
        manifest=request['manifest']
        if manifest['job_id']!=jid or manifest['cwd']!=str(directory):raise ValueError('Invalid remote manifest binding')
        directory.mkdir(parents=True,exist_ok=True)
        for name,encoded in request['files'].items():
            if Path(name).name!=name or name in {'manifest.json','receipt.json','execution-once.json'}:raise ValueError('Invalid upload name')
            data=base64.b64decode(encoded,validate=True)
            if len(data)>4*1024*1024:raise ValueError('Upload exceeds limit')
            put(directory/name,data)
        put(directory/'manifest.json',json.dumps(manifest,sort_keys=True,indent=2).encode())
        current=status(directory,jid)
        if current['status'] in {'PREPARED','NOT_FOUND'}:
            with (directory/'supervisor.log').open('ab') as log:
                child=subprocess.Popen([sys.executable,'-B',str(Path(worker.__file__).resolve()),str(directory)],
                    stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
            return {'status':'SUBMITTED','job_id':jid,'supervisor_pid':child.pid}
        return current
    if operation=='cancel':
        directory.mkdir(parents=True,exist_ok=True)
        (directory/'CANCEL').touch(exist_ok=True)
        return status(directory,jid)
    if operation=='status':return status(directory,jid)
    if operation=='collect':
        current=status(directory,jid)
        if current['status'] not in {'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT'}:raise ValueError('Remote job is not terminal')
        files={};total=0
        paths=[directory/name for name in ('receipt.json','stdout.log','stderr.log','preflight.json','prediction-summary.json')]
        paths+=list((directory/'output').glob('**/predictions/**/*.cif'))
        paths+=list((directory/'output').glob('**/predictions/**/confidence*.json'))
        for path in paths:
            if not path.is_file() or path.is_symlink():continue
            size=path.stat().st_size;total+=size
            if total>request['max_bytes'] or len(files)>=200:raise ValueError('Remote collection exceeds grant')
            files[str(path.relative_to(directory))]=base64.b64encode(path.read_bytes()).decode()
        return {'status':current,'files':files}
    raise ValueError('Unknown remote operation')


if __name__=='__main__':
    try:
        root=Path(sys.argv[1]).resolve()
        request=json.loads(sys.stdin.buffer.read(8*1024*1024))
        print(json.dumps(execute(root,request)))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__+': '+str(exc)}))
        raise SystemExit(2)
