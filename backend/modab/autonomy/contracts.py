"""Versioned authority, artifact and tool contracts; standard library only."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from dualctx.contracts import ContractError, canonical_hash

SCHEMA = 'modab_research_mission_v1'
ID = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,127}$')
BUDGET_FIELDS = {'model_requests', 'model_tokens', 'compute_seconds', 'job_attempts', 'max_concurrent_jobs'}
TERMINAL = {'COMPLETED', 'COMPLETED_WITH_LIMITATIONS', 'FAILED', 'CANCELLED', 'BUDGET_EXHAUSTED'}


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ContractError('Invalid identifier')
    return value


def text(value, name, limit=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError('Invalid ' + name)
    return value


def integer(value, name, minimum=0, maximum=2**31 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError('Invalid ' + name)
    return value


def exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        if not isinstance(value,dict):
            raise ContractError('Expected JSON object with fields: ' + ','.join(sorted(fields)))
        raise ContractError('Unexpected or missing fields: missing=' + ','.join(sorted(set(fields)-set(value))) + '; extra=' + ','.join(sorted(set(value)-set(fields))))


def parse(data, limit=1048576):
    if len(data) > limit:
        raise ContractError('JSON input exceeds limit')
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ContractError('Duplicate JSON key: ' + key)
            out[key] = value
        return out
    return json.loads(data, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ContractError('Nonfinite JSON')))


def read(path):
    return parse(Path(path).read_bytes(), limit=32 * 1024 * 1024)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value, *, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if immutable:
        try:
            with path.open('xb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            if path.read_bytes() != data:
                raise ContractError('Refusing to replace immutable artifact')
        return
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        from .worker import replace_file
        replace_file(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_mission(value):
    exact(value, {'schema', 'request_id', 'project_id', 'objective', 'inputs', 'allowed_tools',
        'allowed_resources', 'budget', 'planner', 'stop_conditions', 'data_policy', 'evidence_kind'})
    if value['schema'] != SCHEMA:
        raise ContractError('Unsupported mission schema')
    for key in ('request_id', 'project_id'):
        identifier(value[key])
    text(value['objective'], 'objective')
    for key in ('allowed_tools', 'allowed_resources', 'stop_conditions'):
        if not isinstance(value[key], list) or not value[key] or len(value[key]) > 64:
            raise ContractError('Nonempty bounded ' + key + ' required')
        if len(set(value[key])) != len(value[key]):
            raise ContractError('Duplicate ' + key)
        for item in value[key]:
            (text(item, key) if key == 'stop_conditions' else identifier(item))
    if not isinstance(value['inputs'], dict) or not value['inputs'] or len(value['inputs']) > 64:
        raise ContractError('Pinned input identities required')
    for key, item in value['inputs'].items():
        identifier(key)
        exact(item, {'path', 'sha256', 'kind'})
        if not isinstance(item['path'], str) or not Path(item['path']).is_absolute():
            raise ContractError('Absolute local input path required')
        if not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ContractError('Invalid input digest')
        text(item['kind'], 'input kind', 80)
    exact(value['budget'], BUDGET_FIELDS)
    for key, amount in value['budget'].items():
        # Explicitly commissioned unlimited request count; other budgets stay finite.
        if key == 'model_requests' and amount is None:
            continue
        integer(amount, key, minimum=1 if key == 'max_concurrent_jobs' else 0)
    if value['budget']['max_concurrent_jobs'] > 32:
        raise ContractError('Unsupported concurrency')
    exact(value['planner'], {'kind', 'config_id'})
    if value['planner']['kind'] not in {'recorded_plan', 'deepseek', 'test_fixture'}:
        raise ContractError('Unknown planner')
    identifier(value['planner']['config_id'])
    if value['evidence_kind'] not in {'COMPUTATIONAL_RESEARCH', 'ENGINEERING_FIXTURE_ONLY'}:
        raise ContractError('Invalid evidence kind')
    if value['planner']['kind'] == 'test_fixture' and value['evidence_kind'] != 'ENGINEERING_FIXTURE_ONLY':
        raise ContractError('Fixture planner cannot impersonate research')
    exact(value['data_policy'], {'public_inputs_only', 'allowed_endpoints'})
    if type(value['data_policy']['public_inputs_only']) is not bool:
        raise ContractError('Invalid data policy')
    if not isinstance(value['data_policy']['allowed_endpoints'], list):
        raise ContractError('Explicit endpoint allowlist required')
    for endpoint in value['data_policy']['allowed_endpoints']:
        if not isinstance(endpoint, str) or not endpoint.startswith('https://'):
            raise ContractError('HTTPS endpoint required')
    return value


def verify_inputs(mission):
    for key, item in mission['inputs'].items():
        p = Path(item['path'])
        if not p.is_file() or digest(p) != item['sha256']:
            raise ContractError('Input changed or absent: ' + key)


def validate_decision(value, mission, catalog, available_inputs=None):
    exact(value, {'action', 'reason', 'evidence_ids', 'tool_id', 'input_ids', 'parameters', 'conclusion'})
    text(value['reason'], 'decision reason')
    if value['action'] not in {'run_tool', 'finish', 'needs_input'}:
        raise ContractError('Unsupported decision action')
    for key in ('evidence_ids', 'input_ids'):
        if not isinstance(value[key], list) or len(value[key]) > 128 or not all(isinstance(x, str) for x in value[key]):
            raise ContractError('Invalid ' + key)
    if not set(value['input_ids']) <= (available_inputs or mission['inputs']).keys():
        raise ContractError('Uncommissioned input identity')
    if not isinstance(value['parameters'], dict):
        raise ContractError('Parameters must be an object')
    if value['action'] == 'run_tool':
        if value['tool_id'] not in mission['allowed_tools'] or value['tool_id'] not in catalog:
            raise ContractError('Uncommissioned or unknown tool')
        if value['conclusion'] is not None:
            raise ContractError('Tool action is not a conclusion')
    else:
        if value['tool_id'] is not None or value['input_ids'] or value['parameters']:
            raise ContractError('Non-tool decision has tool fields')
        if value['action'] == 'finish' and value['conclusion'] not in {'supported', 'not_supported', 'inconclusive', 'preparation_only'}:
            raise ContractError('Explicit conclusion category required')
        if value['action'] == 'needs_input' and value['conclusion'] is not None:
            raise ContractError('Input request is not a conclusion')
    return value
