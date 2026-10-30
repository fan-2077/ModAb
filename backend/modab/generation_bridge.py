"""Narrow ID-only bridge to a previously deployed, frozen generation job."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dualctx.contracts import ContractError
from modab.project_store import ProjectStore, UNVERIFIED_EXTERNAL
from modab.registry_contracts import parse_request, MAX_REQUEST_BYTES


def remote(config, operation, manifest):
    request = {k: manifest[k] for k in ('project_id', 'plan_id', 'task_id', 'attempt_id', 'job_id')}
    command = [config['ssh'], '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
               '-o', 'StrictHostKeyChecking=yes', config['ssh_target'], 'python3', '-B',
               config['remote_controller'], operation]
    try:
        result = subprocess.run(command, input=json.dumps(request), text=True, encoding='utf-8',
            capture_output=True, timeout=22, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        return {'status': 'UNKNOWN', 'error': 'SSH timeout; query original job, never resubmit'}
    if result.returncode != 0:
        return {'status': 'UNKNOWN', 'error': 'Remote lookup/submission failed',
                'ssh_exit_code': result.returncode, 'diagnostic': result.stderr[-2000:]}
    value = json.loads(result.stdout)
    if value.get('job_id') != manifest['job_id'] or value.get('manifest_sha256') != config['manifest_sha256']:
        raise ContractError('Remote job/manifest identity mismatch')
    return value


def handle(config_path, operation, payload, transport=remote):
    if operation not in ('start', 'query'): raise ContractError('Unsupported generation operation')
    # Reuse the strict, duplicate-key rejecting ID parser without extending registry semantics.
    request = parse_request(payload)
    if request.operation != 'query': raise ContractError('Invalid generation envelope')
    config = json.loads(Path(config_path).read_bytes())
    manifest = config['manifest']
    if (request.project_id, request.plan_id) != (manifest['project_id'], manifest['plan_id']):
        raise ContractError('Only the explicitly approved generation plan is executable')
    with ProjectStore(Path(config['workspace'])) as store:
        plan = store._one('SELECT * FROM plans WHERE plan_id=?', (request.plan_id,))
        project = store._one('SELECT * FROM projects WHERE project_id=?', (request.project_id,))
        if project['evidence_kind'] != UNVERIFIED_EXTERNAL:
            raise ContractError('A fixture project cannot execute a scientific job')
        if plan['project_id'] != request.project_id or not plan['approval_id']:
            raise ContractError('Approved project/plan binding required')
        task_ids = store.materialize_tasks(request.plan_id, expected_project_id=request.project_id)
        if task_ids != [manifest['task_id']]: raise ContractError('Frozen task binding differs')
        task = store.task(manifest['task_id'])
        if task['input_sha256'] != manifest['input_sha256']:
            raise ContractError('Frozen input binding differs')
        claimed = store.claim_handoff(task['task_id']) if operation == 'start' else None
        if claimed and claimed['attempt_id'] != manifest['attempt_id']:
            raise ContractError('Frozen attempt binding differs')
        snapshot = transport(config, 'start' if claimed else 'query', manifest)
        status = snapshot.get('status')
        if status in ('RUNNING', 'SUCCEEDED', 'FAILED'):
            task = store.task(task['task_id'])
            if task['status'] == 'NOT_RUN':
                raise ContractError('Unexpected external execution without local handoff')
            store.record_started(task['task_id'], manifest['attempt_id'], external_ref=manifest['job_id'])
            if status in ('SUCCEEDED', 'FAILED'):
                receipt = {k: manifest[k] for k in ('task_id', 'attempt_id', 'input_sha256')}
                receipt.update(external_ref=manifest['job_id'],
                    **{k: task['document'][k] for k in ('tool', 'tool_version', 'object_id')},
                    status=status, output=snapshot,
                    error=None if status == 'SUCCEEDED' else json.dumps(snapshot.get('error', 'Remote job failed')),
                    source_ref=config['remote_controller'].rsplit('/', 1)[0] + '/job/run-receipt.json',
                    evidence_kind=UNVERIFIED_EXTERNAL)
                store.record_receipt(task['task_id'], receipt)
        task = store.task(task['task_id'])
    cache = Path(config['workspace']) / 'latest-remote-state.json'
    temporary = cache.with_name(cache.name + '.tmp')
    temporary.write_text(json.dumps({'queried_utc': datetime.now(timezone.utc).isoformat(),
                                    'remote': snapshot}, indent=2) + '\n', encoding='utf-8')
    temporary.replace(cache)
    candidates = []
    rows = snapshot.get('candidates', [])
    if status == 'NOT_RUN':
        rows = manifest.get('candidates', rows)
    for row in rows:
        result = row.get('result', {})
        candidates.append({k: row.get(k) for k in ('candidate_id', 'seed', 'status', 'actual_process_exit_code', 'elapsed_seconds', 'reason', 'same_sequence_as')}
                          | {'artifact_validation': result.get('status'),
                             'changed_design_residues': result.get('changed_design_residue_count'),
                             'phosphorus_atoms': result.get('phosphorus_atoms'),
                             'files': result.get('files')})
    return {'schema': 'modab_generation_response_v1', 'project_id': request.project_id,
            'plan_id': request.plan_id, 'job_id': manifest['job_id'],
            'task_id': task['task_id'], 'attempt_id': task['attempt_id'],
            'status': status, 'journal_status': task['status'],
            'receipt_sha256': task['receipt_sha256'], 'candidates': candidates,
            'accounted_seconds': snapshot.get('accounted_seconds', manifest.get('prior_accounted_seconds') if status == 'NOT_RUN' else None),
            'remaining_seconds': snapshot.get('remaining_seconds', snapshot.get('remaining_seconds_estimate',
                manifest.get('total_budget_seconds', 0) - manifest.get('prior_accounted_seconds', 0) if status == 'NOT_RUN' else None)),
            'error': snapshot.get('error'), 'automatic_retries': 0,
            'evidence_kind': 'LIVE_SERVER_EXECUTION_RECORD' if status in ('RUNNING', 'SUCCEEDED', 'FAILED') else 'LIVE_SERVER_STATUS_OBSERVATION',
            'scientific_scope': 'Generated candidates only; binding and specificity not established'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--operation', choices=['start', 'query'], required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(handle(args.config, args.operation, sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))))
        return 0
    except Exception as error:
        print(json.dumps({'error': type(error).__name__, 'message': str(error)}))
        return 2


if __name__ == '__main__': raise SystemExit(main())
