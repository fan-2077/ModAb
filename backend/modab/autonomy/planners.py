"""Bound planner implementations. Recorded plans are explicitly not live AI."""
from pathlib import Path
import subprocess

from dualctx.contracts import ContractError, canonical_hash
from .contracts import digest, parse, read


class RecordedPlanner:
    def __init__(self, config):
        self.config = config

    def prepare(self, context):
        if digest(self.config['path']) != self.config['sha256']:
            raise ContractError('Recorded planner changed')
        if context['commission']['planner']['kind'] == 'test_fixture' and context['commission']['evidence_kind'] != 'ENGINEERING_FIXTURE_ONLY':
            raise ContractError('Fixture cannot represent a live Agent')
        return {'origin': {'kind': context['commission']['planner']['kind'], 'sha256': self.config['sha256']}, 'costs': {}}

    def decide(self, context, request):
        rows = read(self.config['path'])
        index = len(context['decisions'])
        if index >= len(rows):
            raise ContractError('Recorded plan exhausted')
        # Resolve symbolic output kinds without changing decision semantics.
        import copy
        decision = copy.deepcopy(rows[index])
        ids = []
        for item in decision['input_ids']:
            if item.startswith('kind:'):
                matches = [k for k, v in context['available_inputs'].items() if v['kind'] == item[5:]]
                if len(matches) != 1:
                    raise ContractError('Recorded plan input is ambiguous or absent')
                ids += matches
            else:
                ids.append(item)
        decision['input_ids'] = ids
        return {'decision': decision, 'usage': {}, 'origin': request['origin']}


class DeepSeekPlanner:
    def __init__(self, config):
        self.config = config

    def prepare(self, context):
        cfg = self.config
        if not cfg.get('enabled'):
            raise ContractError('Planner runtime not enabled by resource commission')
        if cfg['endpoint'] not in context['commission']['data_policy']['allowed_endpoints']:
            raise ContractError('Planner endpoint outside commissioned data policy')
        # Payload is intentionally summaries/facts, not arbitrary binary inputs.
        import json
        size = len(json.dumps(context, ensure_ascii=False).encode('utf-8'))
        if size > cfg['max_context_bytes']:
            raise ContractError('Planner context exceeds configured bound')
        truncated=sum(row['stop_reason']=='length' and not row['usable_decision'] for row in context.get('model_delivery',[]))
        reasoning='off' if truncated>=2 else cfg.get('reasoning','off')
        return {'origin': {'kind': 'model', 'provider': 'deepseek', 'model': cfg['model'],
            'bridge_sha256': digest(cfg['bridge']), 'context_sha256': canonical_hash(context),
            'reasoning':reasoning,'max_output_tokens':cfg['max_output_tokens'],
            'adaptation':'Disable hidden thinking after repeated output-limit failures; keep explicit decision rationale and original output cap' if truncated>=2 else None},
            'costs': {'model_requests': 1, 'model_tokens': size + cfg['max_output_tokens'] + 4096}}

    def decide(self, context, request):
        import json
        cfg = self.config
        request_cfg={**cfg,'reasoning':request['origin'].get('reasoning',cfg.get('reasoning','off'))}
        process = subprocess.run([cfg['node'], cfg['bridge']], input=json.dumps({'config': request_cfg, 'context': context}),
            text=True, encoding='utf-8', capture_output=True, timeout=cfg['timeout_seconds'] + 10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        if process.returncode != 0:
            raise ContractError('Planner bridge failed; request outcome retained as unknown')
        from .reliability import decode_response
        return decode_response(parse(process.stdout))


def make_planner(config, commission):
    pid = commission['planner']['config_id']
    if pid not in config['planners']:
        raise ContractError('Unknown planner configuration')
    cfg = config['planners'][pid]
    if cfg['kind'] != commission['planner']['kind']:
        raise ContractError('Planner kind differs from commission')
    return DeepSeekPlanner(cfg) if cfg['kind'] == 'deepseek' else RecordedPlanner(cfg)
