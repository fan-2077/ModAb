"""Lossless factual projection for planning; full artifacts remain immutable."""
import copy
from .contracts import read


def compact_evidence(evidence):
    bundles={k:v for k,v in evidence.items() if isinstance(v,dict) and v.get('schema')=='modab_fact_bundle_v1'}
    coverage={};designs={}
    for key,bundle in bundles.items():
        source=bundle.get('sources',{}).get('measurements')
        measured=read(source['path']) if source else {}
        coverage[key]={r['structure_sha256'] for r in measured.get('records',[])}
        designs[key]={'parameters':{k:v for k,v in measured.get('parameters',{}).items()if k!='timeout_seconds'},
                      'reference_sha256':measured.get('reference',{}).get('sha256')}
    out=dict(evidence)
    for key,bundle in bundles.items():
        superseding=[other for other in bundles if coverage[key] and coverage[key]<coverage[other] and designs[key]==designs[other]
                     and bundle.get('candidate_ids')==bundles[other].get('candidate_ids')
                     and ('pose_diagnosis' not in bundle or 'pose_diagnosis' in bundles[other])]
        if superseding:
            target=max(superseding,key=lambda k:len(coverage[k]))
            out[key]={'schema':'modab_planner_prior_bundle_pointer_v1','superseded_by':target,
                      'original_bundle_sha256':bundle.get('bundle_sha256'),
                      'retained_structure_count':len(coverage[key]),
                      'note':'Full original bundle remains a selectable, hash-checked artifact. All its structures are covered by the newer bundle.'}
            continue
        value={k:copy.deepcopy(v) for k,v in bundle.items() if k not in {'facts','sources','bundle_sha256'}}
        value['schema']='modab_planner_fact_projection_v1'
        value['original_bundle_sha256']=bundle.get('bundle_sha256')
        value['facts']=[{k:r[k] for k in ('id','subject','metric','value','unit','scope')}for r in bundle['facts']]
        value['sources']={k:{'sha256':v['sha256']}for k,v in bundle['sources'].items()}
        if 'pose_diagnosis' in value:
            for row in value['pose_diagnosis']['records']:
                row.pop('geometry',None)  # Every scalar is already retained in facts.
        value['citation_instruction']='Cite exact IDs from facts. pose_N IDs cover geometry only; prediction_N IDs cover original measurements and confidence. N identifies the same measurement row within this bundle, not a fixed state across different bundles.'
        out[key]=value
    return out
