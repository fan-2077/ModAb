"""Read-only presentation of real research records for the DSH workbench."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modab.autonomy.contracts import digest, read
from modab.autonomy.reports import preferred_report
from modab.autonomy.service import public_state
from modab.autonomy.store import ResearchStore

STATE_LABELS = {'unphosphorylated': '未磷酸化', 'pS202_pT205': 'pS202 / pT205',
                'pS202_pT205_pS208': 'pS202 / pT205 / pS208'}
TOOL_LABELS = {'prepare_at8_states': '准备配对研究输入', 'boltz2_predict': '预测复合物结构',
               'diagnose_pose':'诊断形状、姿态与磷酸接触','prepare_pose_calibration':'准备已知结构辅助校准',
               'measure_predictions': '比较结构与界面', 'grounded_report': '形成研究报告',
               'reuse_evidence': '整理已有证据', 'prepare_requirements': '检查方法与输入要求'}


def artifact_index(mission):
    return {a['id']: a for step in mission['steps'] for a in (step.get('receipt') or {}).get('artifacts', [])}


def checked_asset(mission, asset_id):
    asset = artifact_index(mission).get(asset_id)
    if asset is None:
        raise ValueError('产物不属于当前研究任务')
    path = Path(asset['path']).resolve(strict=True)
    if path.stat().st_size > 16 * 1024 * 1024 or digest(path) != asset['sha256']:
        raise ValueError('产物大小或内容与原任务记录不符')
    return asset, path


def summarize(mission):
    reports = mission['reports']
    report = preferred_report(reports)['document'] if reports else None
    facts = report.get('facts', {}).get('facts', []) if report else []
    rows = {}
    for fact in facts:
        subject = fact.get('subject', '')
        if fact.get('scope') == 'predicted_coordinate_measurement' or subject.count('/') >= 2:
            row = rows.setdefault(subject, {'subject': subject, 'metrics': {}, 'fact_ids': {}})
            row['metrics'][fact['metric']] = fact['value']
            row['fact_ids'][fact['metric']] = fact['id']
    for row in rows.values():
        parts = row['subject'].split('/')
        row['label'] = STATE_LABELS.get(parts[1], parts[1]) if len(parts) > 1 else row['subject']
    decisions = {d['decision_id']: d for d in mission['decisions']}
    steps = []
    structures = []
    for step in mission['steps']:
        spec = step.get('spec') or {}
        params = spec.get('parameters', {})
        inputs = list(spec.get('inputs', {}).values())
        source_name = Path(inputs[0]['path']).stem if inputs else ''
        state = next((name for name in sorted(STATE_LABELS, key=len, reverse=True)
                      if source_name.endswith('_' + name)), '')
        label = TOOL_LABELS.get(step['tool_id'], step['tool_id'])
        if state:
            label += ' · ' + STATE_LABELS[state]
        receipt = step.get('receipt') or {}
        decision = decisions.get(step.get('decision_id'), {}).get('document', {})
        assets = []
        for asset in receipt.get('artifacts', []):
            item = {k: asset[k] for k in ('id', 'kind', 'sha256')}
            item['name'] = Path(asset['path']).name
            assets.append(item)
            if asset['kind'] == 'predicted_structure':
                structures.append({**item, 'state': state, 'label': STATE_LABELS.get(state, source_name),
                                   'seed': params.get('seed'), 'step_id': step['step_id']})
        steps.append({'id': step['step_id'], 'tool': step['tool_id'], 'title': label,
                      'status': step['status'], 'reason': decision.get('reason', ''),
                      'parameters': params, 'error': step.get('error'), 'assets': assets,
                      'seconds': receipt.get('elapsed_seconds'), 'exit_code': receipt.get('process_exit_code'),
                      'started': receipt.get('worker_receipt', {}).get('started_utc'),
                      'finished': receipt.get('worker_receipt', {}).get('finished_utc')})
    interpretations = []
    if report:
        for group, claims in report.get('claims', {}).items():
            if isinstance(claims, list):
                interpretations.extend({**c, 'review_group': group} for c in claims
                                       if isinstance(c, dict) and c.get('text') and c.get('kind') != 'fact')
    objective = mission['commission']['objective']
    is_at8 = 'AT8' in objective
    title = 'AT8 · Tau 磷酸化状态比较' if is_at8 else objective.split('\n')[0][:60]
    review = report.get('research_review', {}) if report else {}
    result = {'id': mission['mission_id'], 'title': title, 'objective': objective,
            'status': mission['status'], 'reason': mission['reason'], 'created': mission['created_utc'],
            'updated': mission['updated_utc'], 'usage': mission['usage'],
            'commission_sha256': mission['commission_sha256'], 'budget': mission['commission']['budget'],
            'approved': bool(mission['approval']), 'steps': steps, 'structures': structures,
            'comparisons': list(rows.values()), 'interpretations': interpretations,
            'facts': facts, 'coverage': report.get('facts', {}).get('coverage', {}) if report else {},
            'candidate_ids': report.get('facts', {}).get('candidate_ids', []) if report else [],
            'conclusion': report.get('proposed_conclusion') if report else None,
            'research_review': review, 'report_available': bool(report),
            'decision_count': len(mission['decisions']),
            'rejected_decisions': [{'error': d.get('error'), 'status': d['status']}
                                   for d in mission['decisions'] if d['status'] != 'ACCEPTED']}
    from modab.autonomy.evidence_summary import evidence_summary
    result['evidence_summary'] = evidence_summary(result)
    return result


def state(config):
    raw = public_state(config)
    return {'schema': 'modab_workbench_v1', 'workspacePath': Path(__file__).resolve().parents[3].as_posix(), 'missions': [summarize(m) for m in raw['missions']],
            'profiles': raw['profiles'], 'catalog': raw['catalog']}


def structure(config, mission_id, asset_id):
    with ResearchStore(config['workspace']) as store:
        mission = store.snapshot(mission_id)
    asset, path = checked_asset(mission, asset_id)
    if asset['kind'] != 'predicted_structure':
        raise ValueError('此产物不是预测结构')
    from modab.autonomy.science_actions import atoms
    # Reuse the existing explicit single-model / no-altloc parser. No fitting or new metrics.
    parsed = atoms(path)
    chains = {}
    for (chain, residue, name), (xyz, comp, element) in sorted(parsed.items()):
        if name == 'CA':
            chains.setdefault(chain, []).append({'residue': residue, 'name': comp, 'xyz': xyz.tolist()})
    return {'asset_id': asset_id, 'sha256': asset['sha256'], 'chains': chains,
            'representation': 'C-alpha trace; original coordinates; no superposition', 'units': 'angstrom'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--operation', choices=['state', 'structure', 'asset'], default='state')
    parser.add_argument('--mission')
    parser.add_argument('--asset')
    args = parser.parse_args()
    config = read(args.config)
    if args.operation == 'state':
        result = state(config)
    elif args.operation == 'structure':
        result = structure(config, args.mission, args.asset)
    else:
        with ResearchStore(config['workspace']) as store:
            mission = store.snapshot(args.mission)
        asset, path = checked_asset(mission, args.asset)
        result = {'path': str(path), 'name': path.name, 'kind': asset['kind'], 'sha256': asset['sha256']}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
