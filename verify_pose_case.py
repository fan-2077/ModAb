"""Remeasure all 10 retained poses on CPU; no prediction or external service."""
from pathlib import Path
import json,hashlib,sys
import numpy as np
root=Path(__file__).resolve().parent;sys.path.insert(0,str(root/'backend'))
from modab.autonomy.science_actions import atoms
from modab.autonomy.pose_diagnostics import fit,pose_components,PHOSPHATE_NAMES
case=root/'case/pose-calibration';data=json.loads((case/'diagnosis.json').read_text(encoding='utf-8'))
ref=atoms(root/'case/reference.cif',reference_ca=True)
keys=[(c,n,'CA') for c,end in [('A',112),('B',113)] for n in range(1,end+1)]
fixed=np.array([ref[k][0] for k in keys]);tau_ref=np.array([ref[('C',n,'CA')][0] for n in range(9,17)])
checks=[]
for index,r in enumerate(data['records']):
 p=case/r['file'];assert hashlib.sha256(p.read_bytes()).hexdigest()==r['structure_sha256']
 a=atoms(p);rot,shift=fit(np.array([a[k][0] for k in keys]),fixed)
 tau=np.array([a[('C',n,'CA')][0] for n in range(8,16)])@rot+shift
 metrics=pose_components(tau,tau_ref);delta=max(abs(v-r['geometry'][k]) for k,v in metrics.items());assert delta<1e-8
 ab=sorted(k for k in a if k[0] in {'A','B'});pep=sorted(k for k in a if k[0]=='C')
 distances=np.linalg.norm(np.array([a[k][0] for k in ab])[:,None]-np.array([a[k][0] for k in pep])[None],axis=2)
 for t in (t for t in data['threshold_sensitivity'] if t['row']==index):
  pairs={(ab[i][:2],pep[j][:2]) for i,j in np.argwhere(distances<=t['cutoff_A'])}
  phos={(ab[i][:2],pep[j][:2]) for i,j in np.argwhere(distances<=t['cutoff_A']) if a[pep[j]][1] in {'SEP','TPO','PTR'} and pep[j][2] in PHOSPHATE_NAMES}
  assert (len(pairs),len(phos))==(t['residue_pair_contacts'],t['phosphate_residue_pair_contacts'])
 for site in r['phosphosites']:
  number=site['tau_position']-194;names={k[2] for k in pep if k[1]==number and k[2] in PHOSPHATE_NAMES}
  assert names==set(site['atoms']) and len(names)==4 and 'P' in names
 checks.append({'file':r['file'],'max_geometry_difference':delta,'thresholds_checked':4})
print(json.dumps({'status':'PASS','structures':len(checks),'checks':checks},indent=2))
