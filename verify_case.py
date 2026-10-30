"""Recompute retained coordinates; no model calls, GPU work or network."""
import json,sys,hashlib
from pathlib import Path
root=Path(__file__).resolve().parent
sys.path.insert(0,str(root/'backend'))
from modab.autonomy.science_actions import geometry
data=json.loads((root/'case/results.json').read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(root/'case/reference.cif')==data['reference_sha256']
checks=[]
for s in data['structures']:
    p=root/'case'/s['file'];assert sha(p)==s['sha256']
    measured=geometry(p,root/'case/reference.cif',{'tau_start':195,'tau_end':214},4.5,4.0)
    expected=next(x['metrics'] for x in data['comparisons'] if x['subject'].split('/')[1]==s['state'])
    delta=max(abs(measured[k]-expected[k]) for k in measured)
    assert delta<1e-8,(s['state'],delta)
    checks.append({'state':s['state'],'max_absolute_difference':delta})
print(json.dumps({'status':'PASS','scope':'retained coordinate remeasurement, not fresh prediction','checks':checks},indent=2))
