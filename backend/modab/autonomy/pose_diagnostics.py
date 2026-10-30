"""Auditable AT8 pose diagnosis and structure-assisted calibration inputs.

The original prediction is immutable. A template is a new, explicitly
conditioned experiment, never a replacement for an unconditioned result.
"""
from pathlib import Path
import copy
import math

from dualctx.contracts import ContractError, canonical_hash
from .contracts import atomic_json, digest, exact, read
from .reports import fact, verify_facts

PHOSPHATE_NAMES = {'P', 'O1P', 'O2P', 'O3P', 'OP1', 'OP2', 'OP3'}


def fit(moving, fixed):
    import numpy as np
    mc, fc = moving.mean(0), fixed.mean(0)
    u, _, vt = np.linalg.svd((moving-mc).T @ (fixed-fc))
    correction = np.eye(3)
    correction[-1, -1] = 1 if np.linalg.det(u @ vt) > 0 else -1
    rotation = u @ correction @ vt
    return rotation, fc-mc @ rotation


def rmsd(left, right):
    import numpy as np
    return float(np.sqrt(np.mean(np.sum((left-right)**2, axis=1))))


def pose_components(moving, fixed):
    """Inputs already in the antibody frame; retain total placement error."""
    import numpy as np
    rotation, translation = fit(moving, fixed)
    total = rmsd(moving, fixed)
    centroid = float(np.linalg.norm(moving.mean(0)-fixed.mean(0)))
    centered = rmsd(moving-moving.mean(0), fixed-fixed.mean(0))
    shape = rmsd(moving @ rotation+translation, fixed)
    if not math.isclose(total**2, centroid**2+centered**2, abs_tol=1e-8):
        raise ContractError('Pose decomposition identity failed')
    return {'Tau_CA_RMSD_after_Fv_fit_A': total,
            'Tau_shape_RMSD_after_independent_fit_A': shape,
            'Tau_centroid_shift_after_Fv_fit_A': centroid,
            'Tau_centered_RMSD_without_rotation_A': centered,
            'Tau_best_fit_rotation_angle_deg': float(np.degrees(np.arccos(np.clip((np.trace(rotation)-1)/2, -1, 1)))),
            'orientation_excess_mean_square_A2': max(0.0, centered**2-shape**2)}


def prediction_settings(summary, directory):
    if 'parameters' in summary:
        return {k:v for k,v in summary['parameters'].items() if k!='timeout_seconds'}, None
    path = directory/'request.json'
    if not path.exists():
        return {}, None
    command = read(path).get('command', [])
    result = {}
    for key in ('seed', 'sampling_steps', 'recycling_steps', 'diffusion_samples'):
        flag = '--'+key
        if flag in command:
            result[key] = int(command[command.index(flag)+1])
    result.update(use_potentials='--use_potentials' in command,
                  msa_mode='server' if '--use_msa_server' in command else 'single_sequence')
    return result, {'path': str(path), 'sha256': digest(path)}


def diagnose(inputs, params, output):
    import numpy as np
    from .science_actions import atoms, measure
    exact(params, {'timeout_seconds', 'contact_cutoff_A', 'near_cutoff_A', 'sensitivity_cutoffs_A'})
    cutoffs = params['sensitivity_cutoffs_A']
    if (not isinstance(cutoffs, list) or not 2 <= len(cutoffs) <= 6 or
        any(type(x) not in {int, float} or not math.isfinite(x) or not 0 < x <= 8 for x in cutoffs) or
        len(set(cutoffs)) != len(cutoffs)):
        raise ContractError('Two to six distinct positive sensitivity cutoffs <= 8 A required')
    base = measure(inputs, {k: v for k, v in params.items() if k != 'sensitivity_cutoffs_A'}, output)
    measurement = read(base['sources']['measurements']['path'])
    reference = atoms(measurement['reference']['path'], reference_ca=True)
    anchors = [(c, n, 'CA') for c, end in [('A', 112), ('B', 113)] for n in range(1, end+1)]
    ref_ab = np.array([reference[k][0] for k in anchors])
    ref_tau = np.array([reference[('C', n, 'CA')][0] for n in range(9, 17)])
    prepared = {r['sha256']: r for x in inputs.values() if x['kind'] == 'prepared_states' for r in read(x['path'])['records']}
    rows, thresholds, settings_sources = [], [], {}
    for row in measurement['records']:
        source = row['prediction_summary']; summary = read(source['path']); directory = Path(source['path']).parent
        structural = [x for x in summary['structures'] if x['sha256'] == row['structure_sha256']]
        if len(structural) != 1:
            raise ContractError('Ambiguous prediction-to-diagnosis correspondence')
        path = directory/structural[0]['path']; pred = atoms(path)
        record = prepared[row['input_sha256']]
        if not record['tau_start'] <= 202 <= 209 <= record['tau_end']:
            raise ContractError('Pose calibration requires the full shared Tau202-209 segment')
        moving = np.array([pred[k][0] for k in anchors]); rot, shift = fit(moving, ref_ab)
        tau = np.array([pred[('C', pos-record['tau_start']+1, 'CA')][0] for pos in range(202, 210)]) @ rot+shift
        metrics = pose_components(tau, ref_tau)
        if not math.isclose(metrics['Tau_CA_RMSD_after_Fv_fit_A'], row['geometry']['Tau_CA_RMSD_after_Fv_fit_A'], abs_tol=1e-8):
            raise ContractError('Independent pose and original RMSD disagree')
        ab = sorted(k for k in pred if k[0] in {'A', 'B'}); peptide = sorted(k for k in pred if k[0] == 'C')
        distances = np.linalg.norm(np.array([pred[k][0] for k in ab])[:, None]-np.array([pred[k][0] for k in peptide])[None], axis=2)
        sites = []
        for pos in record['state']['phosphorylated_positions']:
            number = pos-record['tau_start']+1
            keys = [k for k in peptide if k[1] == number and k[2] in PHOSPHATE_NAMES]
            names = {k[2] for k in keys}
            oxygens = {k[2] for k in keys if pred[k][2] == 'O'}
            complete = 'P' in names and len(oxygens) == 3 and len(keys) == 4
            nearest = None
            if keys:
                nearest = min(math.dist(pred[a][0], pred[p][0]) for a in ab for p in keys)
            sites.append({'tau_position': pos, 'atoms': sorted(names), 'complete_P_plus_3_O': complete,
                          'nearest_antibody_distance_A': nearest})
        settings, settings_source = prediction_settings(summary, directory)
        if settings_source:
            settings_sources['settings_'+str(len(rows))] = settings_source
        metrics['complete_phosphate_group_count'] = sum(s['complete_P_plus_3_O'] for s in sites)
        metrics['expected_phosphate_group_count'] = len(sites)
        item = {'subject': row['subject'], 'state': row['state'], 'seed': settings.get('seed'),
                'structure_sha256': row['structure_sha256'], 'input_sha256': row['input_sha256'],
                'conditioning': record.get('conditioning', {'mode': 'unconditioned'}),
                'parameters': settings, 'geometry': metrics, 'phosphosites': sites}
        rows.append(item)
        for cutoff in sorted(cutoffs):
            pairs = {(ab[i][:2], peptide[j][:2]) for i, j in np.argwhere(distances <= cutoff)}
            phos = {(ab[i][:2], peptide[j][:2]) for i, j in np.argwhere(distances <= cutoff)
                    if pred[peptide[j]][1] in {'SEP', 'TPO', 'PTR'} and peptide[j][2] in PHOSPHATE_NAMES}
            thresholds.append({'row': len(rows)-1, 'cutoff_A': cutoff, 'residue_pair_contacts': len(pairs),
                               'phosphate_residue_pair_contacts': len(phos)})
    comparisons = []
    for after in rows:
        if after['conditioning']['mode'] == 'unconditioned':
            continue
        before = [r for r in rows if r['conditioning']['mode'] == 'unconditioned' and
                  r['subject'] == after['subject'] and r['state'] == after['state'] and
                  r['seed'] is not None and r['seed'] == after['seed'] and r['parameters'] == after['parameters']]
        # Different immutable copies of the same coordinate are not extra replicates.
        before = list({r['structure_sha256']: r for r in before}.values())
        if len(before) == 1:
            comparisons.append({'seed': after['seed'], 'before_sha256': before[0]['structure_sha256'],
                                'after_sha256': after['structure_sha256'],
                                'delta_after_minus_before': {k: after['geometry'][k]-before[0]['geometry'][k] for k in (
                                    'Tau_CA_RMSD_after_Fv_fit_A', 'Tau_shape_RMSD_after_independent_fit_A',
                                    'Tau_centroid_shift_after_Fv_fit_A', 'Tau_best_fit_rotation_angle_deg')},
                                'scope': 'Matched declared parameters; MSA content identity must be checked separately.'})
    diag = {'schema': 'modab_pose_diagnosis_v1', 'records': rows, 'threshold_sensitivity': thresholds,
            'matched_calibration_comparisons': comparisons,
            'checks': {'shared_tau_CA_count': 8, 'Fv_fit_CA_count': 225,
                       'reference_self_RMSD_A': rmsd(ref_tau, ref_tau),
                       'all_phosphate_groups_complete': all(s['complete_P_plus_3_O'] for r in rows for s in r['phosphosites'])},
            'method': 'Kabsch proper rotation; original coordinates; Tau202-209 after 225 Fv CA fit and independent 8 CA fit; label numbering; reject ambiguous atoms',
            'interpretation_policy': 'Inspect chemistry first, then shape versus position and rotation, then threshold stability. Test a proposed correction on new artifacts; retain all originals. Template recovery is conditioned calibration, not blind validation or affinity.'}
    path = output.parent/'pose-diagnosis.json'; atomic_json(path, diag, immutable=True)
    base.pop('bundle_sha256', None)
    base['sources'].update(settings_sources)
    base['sources']['pose_diagnosis'] = {'path': str(path.resolve()), 'sha256': digest(path)}
    for i, row in enumerate(rows):
        subject = row['subject']+'/'+row['state']['name']+'/seed_'+str(row['seed'])+'/'+row['conditioning']['mode']
        for metric, value in row['geometry'].items():
            unit = 'angstrom' if metric.endswith('_A') else 'degree' if metric.endswith('_deg') else 'angstrom_squared' if metric.endswith('_A2') else 'count'
            base['facts'].append(fact('pose_'+str(i)+'_'+metric, subject, metric, value, unit, 'pose_diagnosis',
                                     f'/records/{i}/geometry/{metric}', scope='predicted_coordinate_diagnosis'))
    for i, pair in enumerate(comparisons):
        for metric, value in pair['delta_after_minus_before'].items():
            base['facts'].append(fact('repair_'+str(i)+'_'+metric, 'seed_'+str(pair['seed']), 'after_minus_before_'+metric,
                                     value, 'degree' if metric.endswith('_deg') else 'angstrom', 'pose_diagnosis',
                                     f'/matched_calibration_comparisons/{i}/delta_after_minus_before/{metric}', scope='conditioned_calibration_comparison'))
    base['pose_diagnosis'] = diag
    base['limits'].append('Separate peptide fit measures shape only; original antibody-frame error remains reported. Template-assisted outputs are labelled and compared separately.')
    verify_facts(base); base['bundle_sha256'] = canonical_hash(base)
    return base


def prepare_calibration(inputs, params, output):
    allowed={'timeout_seconds','mode'}|({'template_tolerance_A'}if params.get('mode')=='restrained_template'else set())
    exact(params, allowed)
    if params['mode'] not in {'soft_template','restrained_template'}:
        raise ContractError('Only explicit soft_template or restrained_template calibration is registered')
    tolerance=params.get('template_tolerance_A')
    if params['mode']=='restrained_template' and (type(tolerance)not in {int,float} or not math.isfinite(tolerance) or not 0.5<=tolerance<=5):
        raise ContractError('Explicit backbone template tolerance in [0.5,5] A required')
    source = [x for x in inputs.values() if x['kind'] == 'boltz2_input']
    reference = [x for x in inputs.values() if x['kind'] == 'at8_reference_structure']
    diagnoses = [read(x['path']) for x in inputs.values() if x['kind'] == 'fact_bundle' and 'pose_diagnosis' in read(x['path'])]
    if len(source) != 1 or len(reference) != 1 or not diagnoses:
        raise ContractError('Calibration needs one original input, one reference, and prior pose diagnosis')
    source, reference = source[0], reference[0]
    for bundle in diagnoses:
        verify_facts(bundle)
    matched = [r for b in diagnoses for r in b['pose_diagnosis']['records'] if r['input_sha256'] == source['sha256']]
    if not matched:
        raise ContractError('Selected input has not been diagnosed')
    if any(not s['complete_P_plus_3_O'] for r in matched for s in r['phosphosites']):
        raise ContractError('Repair missing phosphate chemistry before pose calibration')
    records = [r for x in inputs.values() if x['kind'] == 'prepared_states' for r in read(x['path'])['records'] if r['sha256'] == source['sha256']]
    if not records or any(r != records[0] for r in records):
        raise ContractError('Original prepared input has no unique identity record')
    model_input = copy.deepcopy(read(source['path']))
    if 'templates' in model_input:
        raise ContractError('Do not recursively condition an already templated input')
    template_path = output.parent/'pose_template.cif'
    data = Path(reference['path']).read_bytes()
    if template_path.exists():
        if template_path.read_bytes() != data:
            raise ContractError('Immutable template differs')
    else:
        with template_path.open('xb') as stream:
            stream.write(data)
    force=params['mode']=='restrained_template'
    model_input['templates'] = [{'cif': 'pose_template.cif', 'chain_id': ['A', 'B', 'C'],
                                 'template_id': ['A', 'B', 'C'], 'force': force,
                                 **({'threshold':tolerance}if force else {})}]
    target = output.parent/'conditioned_input.json'; atomic_json(target, model_input, immutable=True)
    conditioning = {'mode': params['mode'], 'force': force, 'template_tolerance_A':tolerance, 'reference_sha256': reference['sha256'],
                    'baseline_input_sha256': source['sha256'], 'diagnosis_bundle_sha256': diagnoses[0]['bundle_sha256'],
                    'scope': 'known-structure-assisted calibration; not blind prediction or independent biological validation'}
    record = {**records[0], 'path': str(target.resolve()), 'sha256': digest(target), 'conditioning': conditioning}
    return {'schema': 'modab_at8_prepared_states_v1', 'records': [record], 'template_conditioning': True,
            'conditioning': conditioning, 'candidate_denominator': diagnoses[0]['candidate_ids'],
            'template': {'path': str(template_path.resolve()), 'sha256': digest(template_path)},
            'limits': ['Original input and predictions are retained. Conditioning is the changed factor. Recovery of the known structure is not evidence of affinity or state selectivity.']}
