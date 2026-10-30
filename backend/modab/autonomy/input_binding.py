"""Resolve only unique, hash-linked companion inputs; never select a method/sample."""
from dualctx.contracts import ContractError
from .contracts import read,digest


def bind_measurement_inputs(selected,available):
    selected=dict(selected);bindings=[]
    def add_unique(kind,candidates,reason):
        if len(candidates)!=1:
            raise ContractError('INPUT_BINDING: '+kind+' requires an unambiguous selection; candidates='+','.join(candidates))
        key=candidates[0]
        selected[key]=available[key];bindings.append({'input_id':key,'kind':kind,'reason':reason})
    for kind in ('at8_reference_structure','at8_sequence_dataset'):
        if not any(v['kind']==kind for v in selected.values()):
            add_unique(kind,[k for k,v in available.items() if v['kind']==kind],
                'Only available commissioned companion of this kind')
    summaries={}
    for key,item in available.items():
        if item['kind']=='structure_prediction':
            if digest(item['path'])!=item['sha256']:raise ContractError('Prediction summary identity changed: '+key)
            summaries[key]=read(item['path'])
    for item in list(selected.values()):
        if item['kind']=='predicted_structure':
            candidates=[k for k,v in summaries.items() if any(a['sha256']==item['sha256'] for a in v['structures'])]
            if not any(k in selected for k in candidates):
                add_unique('structure_prediction',candidates,'Summary contains the exact selected coordinate SHA-256')
    for key,item in list(selected.items()):
        if item['kind']!='structure_prediction':continue
        expected=summaries[key]['input_sha256'];candidates=[];already_bound=False
        for k,v in available.items():
            if v['kind']!='prepared_states':continue
            if digest(v['path'])!=v['sha256']:raise ContractError('Prepared manifest identity changed: '+k)
            if any(r['sha256']==expected for r in read(v['path'])['records']):
                candidates.append(k)
                if k in selected:already_bound=True
        if not already_bound:add_unique('prepared_states',candidates,'Manifest contains the selected prediction input SHA-256')
    return selected,bindings
