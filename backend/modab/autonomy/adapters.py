"""Capability registry and typed tool adapters. No model-supplied shell code."""
from __future__ import annotations

import math
from pathlib import Path
import sys

from dualctx.contracts import ContractError, canonical_hash
from .contracts import atomic_json, digest, exact, integer, read, text
from .jobs import inherited_environment
from .reports import verify_facts

BUILTINS = {
    'reuse_evidence': {'name': '校验并整理已有证据', 'inputs': {'at8_measurements', 'at8_reference_measurements', 'at8_geometry'}, 'output_kind': 'fact_bundle'},
    'prepare_requirements': {'name': '整理所选方法的执行要求', 'inputs': {'research_context_config'}, 'output_kind': 'method_requirements'},
    'grounded_report': {'name': '生成带来源核验的事实报告', 'inputs': {'fact_bundle'}, 'output_kind': 'grounded_report'},
    'prepare_at8_states': {'name': '按所选分子形式、Tau 范围与状态准备预测输入', 'inputs': {'at8_sequence_dataset'}, 'output_kind': 'prepared_states'},
    'measure_predictions': {'name': '测量实际预测的界面、位置误差和模型置信度', 'inputs': {'structure_prediction','prepared_states','at8_reference_structure','at8_sequence_dataset'}, 'output_kind': 'fact_bundle'},
    'diagnose_pose': {'name': '诊断磷酸完整性、肽段形状、结合姿态与阈值敏感性', 'inputs': {'structure_prediction','prepared_states','at8_reference_structure','at8_sequence_dataset'}, 'output_kind': 'fact_bundle'},
    'prepare_pose_calibration': {'name': '依据已有诊断准备已知结构软模板校准', 'inputs': {'boltz2_input','prepared_states','at8_reference_structure','fact_bundle'}, 'output_kind': 'prepared_states'},
}


class Registry:
    def __init__(self, config):
        self.config = config

    def catalog(self):
        result = {}
        for tid, value in BUILTINS.items():
            result[tid] = {'id': tid, 'name': value['name'], 'version': '1', 'availability': 'LOCAL_ADAPTER_READY',
                'required_input_kinds': sorted(value['inputs']), 'output_kind': value['output_kind'],
                'scientific_prediction': False, 'resource_id': 'local_cpu',
                'parameters': {'timeout_seconds': 'positive integer bounded by commission',
                    **({'method_id': 'ID from the documented method catalog'} if tid == 'prepare_requirements' else {}),
                    **({'claims': 'optional typed factual claims; omitted produces deterministic facts', 'interpretations':'optional typed non-factual claims added to all deterministic facts','conclusion': 'preparation_only, inconclusive, not_supported or supported',
                        'research_review':'when research_policy.require_stop_review: {stop_basis:continue_research|bounded_inconclusive|resource_blocked|capability_blocked, reason:string, evidence_fact_ids:[IDs in selected fact_bundle], next_test:{tool_id:available tool,question:string,outcomes:[two different informative outcomes],disposition:execute|defer|not_needed,reason:string}}'} if tid == 'grounded_report' else {})}}
        result['prepare_at8_states']['parameters']={'timeout_seconds':'positive integer',
            'subjects':'list of antibody IDs from at8_sequence_dataset; original_AT8 reference first',
            'antibody_form':'Fv (the source-verified registered construct)',
            'tau_start':'biological start position within source dataset','tau_end':'biological end position within source dataset',
            'states':'list of {name: unique identifier, phosphorylated_positions: list of biological positions}; matched parent sequences across states',
            'msa_mode':'server or single_sequence'}
        result['measure_predictions']['parameters']={'timeout_seconds':'positive integer',
            'contact_cutoff_A':'positive number <= 8; counts distinct antibody-peptide residue pairs',
            'near_cutoff_A':'positive number less than contact_cutoff_A; counts antibody-peptide heavy atom pairs'}
        result['measure_predictions']['limits']=['Confidence scores are not affinity.',
            'Superposition uses all 225 Fv CA atoms; peptide RMSD uses shared Tau202-209 CA atoms after that fit.',
            'Include every prepared_states manifest corresponding to selected predictions. Same thresholds across compared states.']
        result['measure_predictions']['input_contract']={
            'structure_prediction':'Select the prediction-summary artifacts for ALL samples to compare. Raw predicted_structure coordinates resolve only through a unique hash-linked summary.',
            'prepared_states':'Manifests matching the selected prediction input SHA-256; uniquely linked companions are resolved automatically and recorded.',
            'at8_reference_structure':'Commissioned experimental reference; automatically bound only when exactly one is available.',
            'at8_sequence_dataset':'Commissioned identity and candidate denominator; automatically bound only when exactly one is available.'}
        result['diagnose_pose']['parameters']={**result['measure_predictions']['parameters'],
            'sensitivity_cutoffs_A':'2 to 6 distinct cutoffs in (0,8]; use the same list across all retained samples'}
        result['diagnose_pose']['input_contract']=result['measure_predictions']['input_contract']
        result['diagnose_pose']['limits']=['Keep original antibody-frame RMSD, report independently fitted peptide shape and centroid/rotation separately.',
            'Include ALL baseline and corrected samples to get before/after comparisons; never select a favorable seed.',
            'A change in pose is not affinity. Examine complete phosphate groups and threshold sensitivity before selecting a repair.']
        result['prepare_pose_calibration']['parameters']={'timeout_seconds':'positive integer','mode':'soft_template or restrained_template',
            'template_tolerance_A':'required ONLY for restrained_template: [0.5,5] A; backbone restraint tolerance, not an independent accuracy cutoff'}
        result['prepare_pose_calibration']['limits']=['Select exactly ONE diagnosed original boltz2_input and its prepared_states manifest plus the diagnosis fact_bundle and reference.',
            'Produces a NEW boltz2_input with a three-chain template. Soft mode only provides features; restrained mode activates a backbone potential with explicit tolerance. Predict with matched baseline seeds/settings and diagnose all samples together.',
            'Restrained recovery demonstrates a conditional modelling capability; distance improvement is partly imposed by the prior and cannot validate an independent binding pose.',
            'This tests known-structure-assisted pose recovery; never claim blind prediction accuracy or state selectivity from template recovery.']
        for tool_id,item in result.items():
            validation=self.config.get('tools',{}).get(tool_id,{}).get('runtime_validation')
            if validation:item['runtime_validation']=validation
        if self.config.get('research_policy',{}).get('require_discriminating_outcomes'):
            result['grounded_report']['parameters']['research_review']=('Required: {stop_basis:continue_research|bounded_inconclusive|resource_blocked|capability_blocked, '
                'reason:string,evidence_fact_ids:[current fact IDs],next_test:{tool_id:available tool,question:string,'
                'hypothesis:string,alternative:string,held_constant:string,changed_factor:string,'
                'outcomes:[{observation:string,interpretation:string,next_action:string},'
                '{observation:string,interpretation:string,next_action:string}],disposition:execute|defer|not_needed,reason:string}}. '
                'The two branches must support different interpretations, including a stable-direction result when testing repeatability.')
        boltz = self.config.get('tools', {}).get('boltz2_predict')
        result['boltz2_predict'] = {'id': 'boltz2_predict', 'name': 'Boltz-2 独立结构预测', 'version': '2.2.1',
            'availability': 'CONFIGURED_REQUIRES_PREFLIGHT' if boltz and boltz.get('enabled') else 'NOT_DEPLOYED',
            'required_input_kinds': ['boltz2_input'], 'output_kind': 'structure_prediction', 'scientific_prediction': True,
            'resource_id': boltz.get('resource_id') if boltz else None,
            'parameters': {'timeout_seconds': 'positive integer', 'seed': 'nonnegative integer',
                'sampling_steps': 'positive integer', 'recycling_steps': 'nonnegative integer',
                'diffusion_samples': 'positive integer', 'use_potentials': 'boolean',
                'msa_mode': 'single_sequence (limited accuracy) or server'},
            'limits': ['Structure prediction does not establish affinity or phosphorylation specificity.',
                       'No protein-small-molecule affinity head is permitted for an antibody-peptide task.']}
        return result

    def prepare(self, mission, decision, inputs, prior_steps, *, remaining_compute_seconds=None):
        tid = decision['tool_id']
        if tid not in self.catalog():
            raise ContractError('Unknown tool')
        selected = {key: inputs[key] for key in decision['input_ids']}
        bindings=[]
        if tid in {'measure_predictions','diagnose_pose'}:
            from .input_binding import bind_measurement_inputs
            selected,bindings=bind_measurement_inputs(selected,inputs)
        for key, row in selected.items():
            if digest(row['path']) != row['sha256']:
                raise ContractError('Tool input identity changed: ' + key)
        params = decision['parameters']
        if tid in BUILTINS:
            missing=BUILTINS[tid]['inputs']-{v['kind'] for v in selected.values()}
            if missing:
                options={kind:[key for key,item in inputs.items() if item['kind']==kind] for kind in sorted(missing)}
                raise ContractError('MISSING_TOOL_INPUTS: missing='+','.join(sorted(missing))+'; available input IDs by kind='+str(options))
        spec=self._local(mission, tid, params, selected, prior_steps) if tid in BUILTINS else self._boltz(mission, params, selected)
        if tid=='boltz2_predict' and 'templates' in read(next(iter(selected.values()))['path']):
            source=next(iter(selected.values()))
            manifests=[read(v['path']) for v in inputs.values() if v['kind']=='prepared_states' and digest(v['path'])==v['sha256']]
            matches=[m for m in manifests if m.get('conditioning',{}).get('mode')in {'soft_template','restrained_template'} and any(r['sha256']==source['sha256'] for r in m['records'])]
            if not matches:raise ContractError('Template input must be linked to a recorded calibration preparation')
            template=matches[0]['template']
            if any(m['template']!=template for m in matches) or digest(template['path'])!=template['sha256']:
                raise ContractError('Calibration template identity changed or is ambiguous')
            if Path(template['path']).resolve()!=Path(source['path']).parent.joinpath('pose_template.cif').resolve():
                raise ContractError('Calibration template path differs from prepared input')
            spec['calibration_template']=template
        if remaining_compute_seconds is not None and spec['timeout_seconds']>remaining_compute_seconds:
            if remaining_compute_seconds<1:raise ContractError('BUDGET_EXHAUSTED:compute_seconds')
            spec['timeout_adjustment']={'requested_seconds':spec['timeout_seconds'],
                'enforced_seconds':remaining_compute_seconds,'reason':'remaining_actual_compute_budget'}
            spec['timeout_seconds']=remaining_compute_seconds
            spec['parameters']={**spec['parameters'],'timeout_seconds':remaining_compute_seconds}
            spec['cost_reservation']['compute_seconds']=remaining_compute_seconds
            if 'request' in spec:spec['request']['parameters']=spec['parameters']
        spec['input_bindings']=bindings
        from .reliability import runtime_identity,execution_identity,prevent_unchanged_retry
        spec['runtime_identity']=runtime_identity(tid,self.config)
        spec['execution_identity']=execution_identity(spec)
        prevent_unchanged_retry(spec,prior_steps)
        return spec

    def _local(self, mission, tid, params, inputs, prior_steps):
        optional = {'method_id'} if tid == 'prepare_requirements' else {'claims', 'interpretations','conclusion','research_review'} if tid == 'grounded_report' else {'subjects','antibody_form','tau_start','tau_end','states','msa_mode'} if tid=='prepare_at8_states' else {'contact_cutoff_A','near_cutoff_A','sensitivity_cutoffs_A'} if tid=='diagnose_pose' else {'contact_cutoff_A','near_cutoff_A'} if tid=='measure_predictions' else {'mode','template_tolerance_A'} if tid=='prepare_pose_calibration' else set()
        if not isinstance(params, dict) or set(params) - ({'timeout_seconds'} | optional):
            extra=sorted(set(params)-({'timeout_seconds'}|optional)) if isinstance(params,dict) else ['parameters must be an object']
            raise ContractError('UNKNOWN_TOOL_PARAMETERS: extra='+','.join(extra)+'; allowed='+','.join(sorted({'timeout_seconds'}|optional)))
        timeout = integer(params.get('timeout_seconds'), 'timeout_seconds', 1, 3600)
        kinds = {x['kind'] for x in inputs.values()}
        if not BUILTINS[tid]['inputs'] <= kinds:
            raise ContractError('Missing required input kinds')
        if tid == 'prepare_requirements':
            text(params.get('method_id'), 'method_id', 80)
            cfg = read(next(x['path'] for x in inputs.values() if x['kind'] == 'research_context_config'))
            if params['method_id'] not in {x['id'] for x in cfg['methods']}:
                raise ContractError('Unknown method reference')
        if tid == 'grounded_report':
            if params.get('conclusion', 'preparation_only') not in {'supported', 'not_supported', 'inconclusive', 'preparation_only'}:
                raise ContractError('Invalid report conclusion category')
        if tid=='prepare_at8_states':
            from .science_actions import validate_preparation
            validate_preparation(params,read(next(x['path'] for x in inputs.values() if x['kind']=='at8_sequence_dataset')))
        return {'tool_id': tid, 'tool_version': '1', 'resource_id': 'local_cpu', 'transport': 'local',
            'inputs': inputs, 'parameters': params, 'timeout_seconds': timeout,
            'output_kind': BUILTINS[tid]['output_kind'], 'scientific_prediction': False,
            'request': {'action': tid, 'mission_id': mission['mission_id'], 'inputs': inputs,
                'parameters': params, 'prior_steps': [{'step_id':s['step_id'],'tool_id':s['tool_id'],'status':s['status'],
                    'receipt':{'receipt_sha256':s['receipt']['receipt_sha256']} if s.get('receipt') else None} for s in prior_steps]},
            'cost_reservation': {'compute_seconds': timeout, 'job_attempts': 1}}

    def _boltz(self, mission, params, inputs):
        cfg = self.config.get('tools', {}).get('boltz2_predict', {})
        if not cfg.get('enabled'):
            raise ContractError('CAPABILITY_NOT_READY:boltz2_predict')
        if cfg.get('transport') != 'ssh' or not cfg.get('runtime_qualified'):
            raise ContractError('CAPABILITY_NOT_READY:qualified_ssh_runtime_required')
        exact(params, {'timeout_seconds', 'seed', 'sampling_steps', 'recycling_steps', 'diffusion_samples', 'use_potentials', 'msa_mode'})
        for name in ('timeout_seconds', 'sampling_steps', 'diffusion_samples'):
            integer(params[name], name, 1)
        for name in ('seed', 'recycling_steps'):
            integer(params[name], name, 0)
        if type(params['use_potentials']) is not bool or params['msa_mode'] not in {'server', 'single_sequence'}:
            raise ContractError('Invalid Boltz prediction settings')
        if params['msa_mode'] == 'server' and cfg['msa_endpoint'] not in mission['commission']['data_policy']['allowed_endpoints']:
            raise ContractError('MSA transmission outside mission data policy')
        if len(inputs) != 1 or next(iter(inputs.values()))['kind'] != 'boltz2_input':
            raise ContractError('Exactly one pinned Boltz input is required')
        model_input = read(next(iter(inputs.values()))['path'])
        validate_boltz_input(model_input, params['msa_mode'])
        if 'templates' in model_input:
            template=Path(next(iter(inputs.values()))['path']).parent/'pose_template.cif'
            if not template.is_file() or template.is_symlink():raise ContractError('Pinned calibration template missing')
        return {'tool_id': 'boltz2_predict', 'tool_version': '2.2.1', 'resource_id': cfg['resource_id'],
            'transport': cfg['transport'], 'inputs': inputs, 'parameters': params,
            'timeout_seconds': params['timeout_seconds'], 'output_kind': 'structure_prediction',
            'scientific_prediction': True, 'runtime': cfg,
            'cost_reservation': {'compute_seconds': params['timeout_seconds'], 'job_attempts': 1}}

    def manifest(self, step, jobs):
        spec = step['spec']
        directory = jobs.directory(step['job_id'])
        directory.mkdir(parents=True, exist_ok=True)
        pins = {row['path']: row['sha256'] for row in spec['inputs'].values()}
        if spec['tool_id'] in BUILTINS:
            request_path = directory / 'request.json'
            atomic_json(request_path, spec['request'], immutable=True)
            runner = Path(__file__).with_name('action_runner.py')
            pins.update({str(request_path): digest(request_path), str(runner): digest(runner)})
            for dependency in (Path(__file__).with_name('reports.py'), Path(__file__).with_name('contracts.py'),Path(__file__).with_name('science_actions.py'),Path(__file__).with_name('pose_diagnostics.py'),Path(__file__).with_name('adapters.py'),
                Path(__file__).with_name('worker.py'), Path(__file__).resolve().parents[2]/'dualctx/contracts.py'):
                pins[str(dependency)] = digest(dependency)
            argv = [sys.executable, '-B', '-I', '-S', '-X', 'utf8', str(runner), str(request_path), str(directory / 'result.json')]
            if spec['tool_id'] in {'measure_predictions','diagnose_pose'}:
                argv=[sys.executable,'-B','-X','utf8',str(runner),str(request_path),str(directory/'result.json')]
            return {'schema': 'modab_owned_job_v1', 'job_id': step['job_id'], 'mission_id': step['mission_id'],
                'step_id': step['step_id'], 'argv': argv, 'cwd': str(directory),
                'env': {**inherited_environment(),'OMP_NUM_THREADS':'8','MKL_NUM_THREADS':'8','OPENBLAS_NUM_THREADS':'8'}, 'inputs': pins, 'timeout_seconds': spec['timeout_seconds']}
        cfg = spec['runtime']
        jobs.bind_remote(step['job_id'], cfg)
        from pathlib import PurePosixPath
        import json
        remote = str(PurePosixPath(cfg['remote_root']) / 'jobs' / step['job_id'])
        source = next(iter(spec['inputs'].values()))
        # JSON is a YAML subset, accepted by the version-pinned Boltz parser.
        model_input = read(source['path'])
        input_path = directory / 'input.yaml'
        atomic_json(input_path, model_input, immutable=True)
        staged=[]
        if 'templates' in model_input:
            template_source=Path(spec['calibration_template']['path'])
            if digest(template_source)!=spec['calibration_template']['sha256']:raise ContractError('Calibration template changed before staging')
            template_target=directory/'pose_template.cif'
            data=template_source.read_bytes()
            if template_target.exists() and template_target.read_bytes()!=data:raise ContractError('Pinned template changed')
            if not template_target.exists():
                with template_target.open('xb') as stream:stream.write(data)
            staged.append(template_target)
        runner = Path(__file__).with_name('prediction_runner.py')
        runner_path = directory / runner.name
        if runner_path.exists() and runner_path.read_bytes() != runner.read_bytes():
            raise ContractError('Pinned prediction runner changed')
        if not runner_path.exists():
            with runner_path.open('xb') as stream:stream.write(runner.read_bytes())
        p=spec['parameters']
        command=[cfg['python'],'-m','boltz.main','predict',remote+'/input.yaml','--out_dir',remote+'/output',
            '--cache',cfg['cache'],'--checkpoint',cfg['checkpoint'],'--model','boltz2','--devices','1','--accelerator','gpu',
            '--seed',str(p['seed']),'--sampling_steps',str(p['sampling_steps']),'--recycling_steps',str(p['recycling_steps']),
            '--diffusion_samples',str(p['diffusion_samples']),'--num_workers','0','--no_kernels']
        if p['use_potentials']:command.append('--use_potentials')
        if p['msa_mode']=='server':command += ['--use_msa_server','--msa_server_url',cfg['msa_endpoint']]
        request_path=directory/'request.json'
        atomic_json(request_path,{'runtime':{k:cfg[k] for k in ('expected_versions','assets','device_index','remote_root')},'command':command,
            'parameters':p,'conditioning':{'mode':'restrained_template'if model_input['templates'][0]['force']else 'soft_template','reference_sha256':digest(staged[0])} if staged else {'mode':'unconditioned'}},immutable=True)
        remote_pins={remote+'/'+path.name:digest(path) for path in (input_path,runner_path,request_path,*staged)}
        remote_pins.update(cfg['assets'])
        return {'schema':'modab_owned_job_v1','job_id':step['job_id'],'mission_id':step['mission_id'],
            'step_id':step['step_id'],'argv':[cfg['python'],'-B',remote+'/prediction_runner.py',remote+'/request.json'],
            'cwd':remote,'env':{'PATH':cfg['bin_path'],'HOME':cfg['home'],'CUDA_VISIBLE_DEVICES':str(cfg['device_index']),
                'OMP_NUM_THREADS':str(cfg['cpu_threads']),'MKL_NUM_THREADS':str(cfg['cpu_threads']),
                'OPENBLAS_NUM_THREADS':str(cfg['cpu_threads']),'PYTHONUNBUFFERED':'1','WANDB_MODE':'disabled',
                'XDG_CACHE_HOME':cfg['remote_root']+'/cache/platform','TORCH_HOME':cfg['remote_root']+'/cache/torch',
                'HF_HOME':cfg['remote_root']+'/cache/huggingface','TRITON_CACHE_DIR':cfg['remote_root']+'/cache/triton',
                'TMPDIR':remote+'/tmp'},'inputs':remote_pins,'timeout_seconds':spec['timeout_seconds'],
                'resource_limits':{'cpu_threads':cfg['cpu_threads'],'memory_bytes':cfg['memory_bytes'],
                    'storage_root':cfg['remote_root'],'min_free_bytes':20*1024**3,'max_root_bytes':40*1024**3}}

    def collect(self, step, directory, worker_receipt):
        spec = step['spec']
        receipt = {'job_id': step['job_id'], 'step_id': step['step_id'], 'tool_id': step['tool_id'],
            'tool_version': spec['tool_version'], 'process_exit_code': worker_receipt['process_exit_code'],
            'elapsed_seconds': worker_receipt['elapsed_seconds'], 'worker_receipt': worker_receipt,
            'status': worker_receipt['status'], 'scientific_prediction': spec['scientific_prediction'], 'artifacts': []}
        if worker_receipt['status']!='SUCCEEDED':
            receipt['diagnostic_excerpt']={name:(Path(directory)/name).read_text(encoding='utf-8',errors='replace')[-5000:]
                for name in ('stdout.log','stderr.log') if (Path(directory)/name).exists()}
        if worker_receipt['status'] == 'SUCCEEDED' and step['tool_id'] in BUILTINS:
            path = Path(directory) / 'result.json'
            result = read(path)
            if spec['output_kind'] == 'fact_bundle':
                verify_facts(result)
            elif spec['output_kind'] == 'grounded_report':
                verify_facts(result['facts'])
                if result['validation_status'] not in {'FACTS_VALIDATED', 'FACTS_VALIDATED_WITH_UNVERIFIED_INTERPRETATIONS'}:
                    raise ContractError('Report facts were not validated')
            receipt['artifacts'].append({'id': 'artifact_' + digest(path), 'path': str(path.resolve()),
                'sha256': digest(path), 'kind': spec['output_kind']})
            if spec['output_kind']=='prepared_states':
                for item in result['records']:
                    if digest(item['path'])!=item['sha256']:raise ContractError('Prepared input changed')
                    receipt['artifacts'].append({'id':'artifact_'+item['sha256'],**{k:item[k] for k in ('path','sha256','kind')}})
            if path.with_suffix('.html').exists():
                html_path = path.with_suffix('.html')
                receipt['artifacts'].append({'id': 'artifact_' + digest(html_path), 'path': str(html_path.resolve()),
                    'sha256': digest(html_path), 'kind': 'report_html'})
            receipt['result_summary'] = {'schema': result.get('schema'), 'candidate_ids': result.get('candidate_ids'),
                'validation_status': result.get('validation_status'), 'selected_method': result.get('selected_method')}
        elif worker_receipt['status']=='SUCCEEDED' and step['tool_id']=='boltz2_predict':
            summary_path=Path(directory)/'prediction-summary.json'
            summary=read(summary_path)
            if summary['schema']!='modab_boltz_prediction_v1' or summary['new_structure_count']!=spec['parameters']['diffusion_samples']:
                raise ContractError('Prediction output count differs from requested sample count')
            for item in summary['structures']+summary['confidence_records']:
                from pathlib import PurePosixPath
                relative=PurePosixPath(item['path'])
                if relative.is_absolute() or '..' in relative.parts:raise ContractError('Invalid prediction artifact path')
                path=Path(directory).joinpath(*relative.parts)
                if digest(path)!=item['sha256']:raise ContractError('Prediction artifact identity mismatch')
                receipt['artifacts'].append({'id':'artifact_'+digest(path),'path':str(path.resolve()),
                    'sha256':digest(path),'kind':'predicted_structure' if path.suffix=='.cif' else 'prediction_confidence'})
            receipt['artifacts'].append({'id':'artifact_'+digest(summary_path),'path':str(summary_path.resolve()),
                'sha256':digest(summary_path),'kind':'structure_prediction'})
            receipt['result_summary']={'new_structure_count':summary['new_structure_count'],
                'scientific_status':'STRUCTURE_PREDICTED_NOT_BINDING_VALIDATED'}
        receipt['receipt_sha256'] = canonical_hash(receipt)
        return receipt


def validate_boltz_input(value, msa_mode):
    if not isinstance(value, dict) or value.get('version') != 1 or set(value) - {'version', 'sequences','templates'}:
        raise ContractError('This registered adapter requires explicit sequences and optional registered soft template; unsupported input field')
    if 'templates' in value:
        templates=value['templates']
        if not isinstance(templates,list)or len(templates)!=1:raise ContractError('One registered local template required')
        template=templates[0];force=template.get('force')
        expected={'cif':'pose_template.cif','chain_id':['A','B','C'],'template_id':['A','B','C'],'force':force}
        if type(force)is not bool:raise ContractError('Explicit boolean template force required')
        if force:
            tolerance=template.get('threshold')
            if type(tolerance)not in {int,float}or not math.isfinite(tolerance)or not 0.5<=tolerance<=5:raise ContractError('Explicit bounded template restraint tolerance required')
            expected['threshold']=tolerance
        if template!=expected:raise ContractError('Only the registered local template calibration is permitted')
    if not isinstance(value['sequences'], list) or not value['sequences']:
        raise ContractError('Protein sequences required')
    seen = set()
    for entity in value['sequences']:
        if set(entity) != {'protein'}:
            raise ContractError('This adapter supports protein/peptide complexes only')
        chain = entity['protein']
        if set(chain) - {'id', 'sequence', 'modifications', 'msa'} or not {'id', 'sequence'} <= chain.keys():
            raise ContractError('Unsupported protein input')
        if not isinstance(chain['id'], str) or chain['id'] in seen:
            raise ContractError('Unique chain ID required')
        seen.add(chain['id'])
        if not isinstance(chain['sequence'], str) or not chain['sequence'] or set(chain['sequence']) - set('ACDEFGHIKLMNPQRSTVWY'):
            raise ContractError('Explicit canonical parent sequence required')
        positions = set()
        for mod in chain.get('modifications', []):
            exact(mod, {'position', 'ccd'})
            pos = integer(mod['position'], 'modification position', 1, len(chain['sequence']))
            if pos in positions or mod['ccd'] not in {'SEP', 'TPO', 'PTR'}:
                raise ContractError('Modification unsupported by current validation scope')
            positions.add(pos)
            if chain['sequence'][pos - 1] != {'SEP': 'S', 'TPO': 'T', 'PTR': 'Y'}[mod['ccd']]:
                raise ContractError('Modification does not match parent residue')
        if msa_mode == 'single_sequence' and chain.get('msa') != 'empty':
            raise ContractError('Single-sequence mode must explicitly declare empty MSA on every chain')
        if msa_mode == 'server' and 'msa' in chain and chain['msa'] != 'empty':
            raise ContractError('Unregistered MSA path')
    return value
