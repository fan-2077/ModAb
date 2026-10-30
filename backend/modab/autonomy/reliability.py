"""Deterministic decision repair, failure identity and bounded model context."""
import copy
import json
import sys
from importlib.metadata import version,PackageNotFoundError
from pathlib import Path
from dualctx.contracts import ContractError, canonical_hash
from .contracts import digest, parse


def decode_response(response):
    """Repair only schema-implied empty fields; never invent an action or input."""
    response=copy.deepcopy(response)
    raw=response.get('raw_response')
    if raw is None:return response
    text='\n'.join(p.get('text','') for p in raw.get('content',[]) if p.get('type')=='text').strip()
    changes=[]
    if text.startswith('```json\n') and text.endswith('\n```'):
        text=text[8:-4];changes.append('removed_single_json_markdown_fence')
    try:
        try:
            value=parse(text.encode('utf-8'))
        except json.JSONDecodeError as exc:
            # A normally finished response sometimes omits only its final outer
            # brace. Append exactly that punctuation; never repair strings,
            # values, nested truncated content, duplicate keys, or length stops.
            if (raw.get('stopReason')!='stop' or exc.pos!=len(text) or
                    exc.msg!="Expecting ',' delimiter" or not text.startswith('{') or not text.endswith('}')):
                raise
            value=parse((text+'}').encode('utf-8'))
            required={'action','reason','evidence_ids','tool_id','input_ids','parameters'}
            if not isinstance(value,dict) or not required<=set(value)<=required|{'conclusion'}:
                raise ContractError('Outer-brace repair requires complete decision fields')
            changes.append('closed_single_missing_outer_object_brace')
        if not isinstance(value,dict):raise ContractError('Decision must be one JSON object')
        defaults={}
        if value.get('action')=='run_tool':defaults={'conclusion':None}
        elif value.get('action') in {'finish','needs_input'}:
            defaults={'tool_id':None,'input_ids':[],'parameters':{}}
            if value['action']=='needs_input':defaults['conclusion']=None
        for key,empty in defaults.items():
            if key not in value:value[key]=empty;changes.append('added_schema_implied_empty_'+key)
        response.update(decision=value,parse_error=None,normalizations=changes)
    except (ValueError,TypeError,ContractError) as exc:
        response.update(decision=None,parse_error=str(exc),normalizations=changes)
    return response


def execution_identity(spec):
    # Timeout extension is a real repair for a timeout, but not a missing library.
    return canonical_hash({'tool_id':spec['tool_id'],'inputs':sorted((v['kind'],v['sha256']) for v in spec['inputs'].values()),
        'parameters':{k:v for k,v in spec['parameters'].items() if k!='timeout_seconds'},
        'runtime':spec.get('runtime_identity'),'calibration_template_sha256':spec.get('calibration_template',{}).get('sha256')})


def runtime_identity(tool_id,config):
    root=Path(__file__).parent
    names=['action_runner.py','science_actions.py','pose_diagnostics.py','reports.py','contracts.py','jobs.py','input_binding.py'] if tool_id!='boltz2_predict' else ['prediction_runner.py']
    packages={}
    if tool_id in {'measure_predictions','diagnose_pose'}:
        for name in ('numpy','gemmi'):
            try:packages[name]=version(name)
            except PackageNotFoundError:packages[name]=None
    return canonical_hash({'sources':{name:digest(root/name) for name in names},
        'local_python':{'executable':sys.executable,'version':sys.version,'packages':packages},
        'runtime':config.get('tools',{}).get(tool_id,{})})


def failure_kind(receipt):
    detail='\n'.join(receipt.get('diagnostic_excerpt',{}).values())
    if 'ModuleNotFoundError' in detail or 'ImportError' in detail:return 'dependency_missing'
    if 'Ambiguous atom' in detail or 'alternate conformers' in detail:return 'input_ambiguity'
    if receipt.get('status')=='TIMED_OUT':return 'timeout'
    if 'limit' in str(receipt.get('worker_receipt',{}).get('reason','')).lower():return 'resource_limit'
    return 'known_tool_failure'


def prevent_unchanged_retry(spec,steps):
    identity=execution_identity(spec)
    for step in reversed(steps):
        receipt=step.get('receipt')
        prior=step.get('spec',{})
        # Old receipts remain evidence; absence of an old runtime fingerprint is
        # not proof that the current repaired executable is unchanged.
        if not receipt or not prior.get('execution_identity'):continue
        if prior['execution_identity']!=identity:continue
        if receipt['status']=='SUCCEEDED':return
        if receipt['status'] not in {'FAILED','TIMED_OUT'}:continue
        if failure_kind(receipt)=='timeout' and spec['timeout_seconds']>prior['timeout_seconds']:return
        raise ContractError('UNCHANGED_FAILED_ACTION: '+step['step_id']+'; '+failure_kind(receipt)+
            '; inputs, method and runtime are unchanged. Correct the cause, choose another informative action, or report the specific capability gap. Do not resubmit.')


def compact_decisions(decisions):
    rows=[]
    for d in decisions:
        doc=d.get('document') or {}
        rows.append({'sequence':d['sequence'],'decision_id':d['decision_id'],'status':d['status'],'error':d.get('error'),
            'action':doc.get('action'),'tool_id':doc.get('tool_id'),
            'reason':doc.get('reason','')[:1200] if d['status']=='ACCEPTED' else None})
    return rows


def compact_receipt(receipt):
    if not receipt:return None
    value={k:receipt.get(k) for k in ('status','tool_id','step_id','process_exit_code','receipt_sha256','elapsed_seconds','result_summary')}
    value['artifacts']=[{k:a[k] for k in ('id','kind','sha256')} for a in receipt.get('artifacts',[])]
    if receipt['status']!='SUCCEEDED':
        value['failure_kind']=failure_kind(receipt)
        value['diagnostic_excerpt']={k:'\n'.join(v.splitlines()[-3:]) for k,v in receipt.get('diagnostic_excerpt',{}).items()}
    return value
