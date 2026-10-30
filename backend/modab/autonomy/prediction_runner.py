"""Bound Boltz runtime preflight and output inspection; no automatic install."""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys


def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def run(request):
    cfg=request['runtime']
    import fcntl,shutil
    root=Path(cfg['remote_root'])
    for name in ('TMPDIR','XDG_CACHE_HOME','TORCH_HOME','HF_HOME','TRITON_CACHE_DIR'):
        if name in os.environ:
            path=Path(os.environ[name]).resolve()
            if not path.is_relative_to(root):raise RuntimeError('Runtime cache escaped commissioned directory')
            path.mkdir(parents=True,exist_ok=True)
    (root/'locks').mkdir(exist_ok=True)
    lock=(root/'locks'/('gpu_'+str(cfg['device_index'])+'.lock')).open('a+')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    gpu=subprocess.check_output(['nvidia-smi','--id='+str(cfg['device_index']),'--query-gpu=uuid,memory.free','--format=csv,noheader,nounits'],text=True).strip().split(',')
    active=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader,nounits'],text=True)
    if any(line.split(',')[0].strip()==gpu[0].strip() for line in active.splitlines()):raise RuntimeError('Commissioned device is occupied; no new prediction started')
    if int(gpu[1].strip())<30000:raise RuntimeError('Insufficient free GPU memory')
    if shutil.disk_usage(root).free<20*1024**3:raise RuntimeError('Commissioned free disk floor reached')
    root_bytes=int(subprocess.check_output(['du','-s','-B1',str(root)],text=True).split()[0])
    if root_bytes>40*1024**3:raise RuntimeError('Commissioned root disk limit reached')
    import torch
    versions={name:importlib.metadata.version(name) for name in cfg['expected_versions']}
    if versions!=cfg['expected_versions']:raise RuntimeError('Scientific package versions differ from runtime binding')
    if not torch.cuda.is_available() or torch.cuda.device_count()!=1:raise RuntimeError('Exactly one commissioned CUDA device must be usable')
    for path,expected in cfg['assets'].items():
        if sha(path)!=expected:raise RuntimeError('Scientific asset identity changed')
    preflight={'versions':versions,'cuda_version':torch.version.cuda,'visible_devices':torch.cuda.device_count(),
        'device_name':torch.cuda.get_device_name(0),'asset_digests':cfg['assets'],
        'device_uuid':gpu[0].strip(),'cpu_affinity':sorted(os.sched_getaffinity(0)),
        'root_bytes_before_prediction':root_bytes,'memory_policy':'worker monitors owned process group RSS at one-second intervals; 32GiB stop threshold'}
    Path('preflight.json').write_text(json.dumps(preflight,indent=2),encoding='utf-8')
    result=subprocess.run(request['command'],stdin=subprocess.DEVNULL,check=False)
    if result.returncode:return result.returncode
    predictions=list(Path('output').glob('**/predictions/**/*.cif'))
    confidence=list(Path('output').glob('**/predictions/**/confidence*.json'))
    if not predictions or not confidence:raise RuntimeError('Process exited zero but expected structural outputs are missing')
    records=[]
    for path in confidence:
        value=json.loads(path.read_bytes())
        records.append({'path':str(path),'sha256':sha(path),'confidence':value})
    Path('prediction-summary.json').write_text(json.dumps({'schema':'modab_boltz_prediction_v1',
        'status':'STRUCTURE_PREDICTED_NOT_BINDING_VALIDATED','input_sha256':sha('input.yaml'),
        'structures':[{'path':str(p),'sha256':sha(p)} for p in predictions],
        'confidence_records':records,'new_structure_count':len(predictions)},indent=2),encoding='utf-8')
    return 0


if __name__=='__main__':
    raise SystemExit(run(json.loads(Path(sys.argv[1]).read_bytes())))
