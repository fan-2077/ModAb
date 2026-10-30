"""Empirical planning costs, separate from enforced worker timeouts."""
import math


def estimate_runtime(tool_id, parameters, steps, *, input_sha256=None,
                     reference_spec=None, workload_scale=1):
    method = {k: v for k, v in parameters.items() if k not in {'seed', 'timeout_seconds'}}
    matched = []
    for step in steps:
        receipt = step.get('receipt') or {}
        spec = step.get('spec', {})
        if step['tool_id'] != tool_id or receipt.get('status') not in {'SUCCEEDED', 'FAILED', 'TIMED_OUT'}:
            continue
        if reference_spec and any(spec.get(k) != reference_spec.get(k)
                                  for k in ('resource_id', 'tool_version', 'runtime_identity')):
            continue
        if input_sha256 and input_sha256 not in {v['sha256'] for v in spec.get('inputs', {}).values()}:
            continue
        if tool_id == 'boltz2_predict' and method != {
                k: v for k, v in spec.get('parameters', {}).items() if k not in {'seed', 'timeout_seconds'}}:
            continue
        elapsed = receipt.get('elapsed_seconds')
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and math.isfinite(elapsed) and elapsed > 0:
            matched.append({'step_id': step.get('step_id'), 'status': receipt['status'], 'seconds': elapsed})
    timeout = parameters.get('timeout_seconds', 600)
    if matched:
        estimate = math.ceil(max(r['seconds'] for r in matched) * max(1, workload_scale) * 1.5 + 10)
        source = 'matched_observed_max_times_1_5_plus_10_seconds'
    else:
        # Explicitly weak cold-start priors, never advertised as observations.
        estimate = {'measure_predictions': 30, 'grounded_report': 10}.get(tool_id, timeout)
        source = 'unmeasured_local_prior' if tool_id in {'measure_predictions', 'grounded_report'} else 'no_comparable_observation_timeout_fallback'
    return {'estimated_seconds': estimate, 'timeout_seconds': timeout,
            'source': source, 'observed_jobs': matched, 'workload_scale': max(1, workload_scale),
            'uncertainty': 'planning allowance, not a calibrated statistical bound',
            'failed_observations_retained': any(r['status'] != 'SUCCEEDED' for r in matched)}


def followup_cost(groups, steps, remaining):
    components = []
    for group in groups:
        reference = next((s['spec'] for s in reversed(steps)
                          if s['tool_id'] == 'boltz2_predict' and
                          group['input_sha256'] in {v['sha256'] for v in s['spec']['inputs'].values()} and
                          {k:v for k,v in s['spec']['parameters'].items() if k not in {'seed','timeout_seconds'}} == group['method_parameters']), None)
        estimate = estimate_runtime('boltz2_predict', group['last_parameters'], steps,
                                    input_sha256=group['input_sha256'], reference_spec=reference)
        components.append({'tool_id': 'boltz2_predict', 'subject': group['subject'],
                           'state': group['state']['name'], **estimate})
    current = sum(g['structure_count'] for g in groups)
    added = sum(g['last_parameters'].get('diffusion_samples', 1) for g in groups)
    for tool_id in ('measure_predictions', 'grounded_report'):
        latest = next((s['spec'] for s in reversed(steps) if s['tool_id'] == tool_id and
                       (s.get('receipt') or {}).get('status') == 'SUCCEEDED'), None)
        estimate = estimate_runtime(tool_id, (latest or {}).get('parameters', {'timeout_seconds': 600}),
                                    steps, reference_spec=latest,
                                    workload_scale=(current+added)/max(1,current) if tool_id=='measure_predictions' else 1)
        components.append({'tool_id': tool_id, **estimate})
    budget = {'model_requests': len(groups)+3, 'job_attempts': len(groups)+2,
              'compute_seconds': sum(c['estimated_seconds'] for c in components)}
    limiting = [k for k,v in budget.items() if remaining.get(k) is not None and remaining[k]<v]
    return {'estimated_action_budget': budget, 'fits_non_token_budget': bool(groups) and not limiting,
            'limiting_non_token_budgets': limiting, 'cost_components': components,
            'timeout_allowance_seconds': sum(c['timeout_seconds'] for c in components),
            'token_cost': 'Separate per-request reservation; not inferred from compute time',
            'reservation_policy': 'Each job reserves its timeout capped to remaining actual compute budget; actual elapsed time is settled.'}
