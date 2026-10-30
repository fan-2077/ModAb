"""Registered local actions, invoked by the owned worker in isolated Python."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modab.autonomy.contracts import atomic_json, digest, read
from modab.autonomy.reports import at8_facts, assemble, deterministic_claims, render_html


def execute(request, output):
    for key, item in request['inputs'].items():
        if digest(item['path']) != item['sha256']:
            raise ValueError('Pinned input changed: ' + key)
    action = request['action']
    if action == 'reuse_evidence':
        result = at8_facts(request['inputs'])
    elif action in {'prepare_at8_states','measure_predictions'}:
        from modab.autonomy.science_actions import prepare,measure
        result=(prepare if action=='prepare_at8_states' else measure)(request['inputs'],request['parameters'],output)
    elif action in {'diagnose_pose','prepare_pose_calibration'}:
        from modab.autonomy.pose_diagnostics import diagnose,prepare_calibration
        result=(diagnose if action=='diagnose_pose' else prepare_calibration)(request['inputs'],request['parameters'],output)
    elif action == 'prepare_requirements':
        source = next(x for x in request['inputs'].values() if x['kind'] == 'research_context_config')
        config = read(source['path'])
        selected = next(x for x in config['methods'] if x['id'] == request['parameters']['method_id'])
        result = {'schema': 'modab_method_requirements_v1', 'selected_method': selected['id'],
            'method_version': selected['version'], 'declared_availability': selected['availability'],
            'requirements': selected['requirements'], 'sources': selected['sources'],
            'prepared_executable_scientific_input': False, 'new_predictions': 0}
    elif action == 'grounded_report':
        source = next(x for x in request['inputs'].values() if x['kind'] == 'fact_bundle')
        bundle = read(source['path'])
        claims = request['parameters'].get('claims', deterministic_claims(bundle))
        claims = claims + request['parameters'].get('interpretations', [])
        result = assemble(bundle, claims, request['mission_id'], request['prior_steps'],
            request['parameters'].get('conclusion', 'preparation_only'))
        if request.get('research_review_result'):result['research_review']=request['research_review_result']
        output.with_suffix('.html').write_text(render_html(result), encoding='utf-8')
    else:
        raise ValueError('Unsupported registered local action')
    atomic_json(output, result, immutable=True)
    return {'status': 'LOCAL_ACTION_COMPLETED', 'action': action, 'artifact_sha256': digest(output),
        'scientific_prediction_executed': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = execute(read(args.request), args.output)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
