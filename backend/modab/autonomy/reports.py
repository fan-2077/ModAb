"""Numerical facts are rendered by code; unrestricted prose never becomes fact."""
from __future__ import annotations

from fractions import Fraction
import html
from pathlib import Path
import re

from dualctx.contracts import ContractError, canonical_hash
from .contracts import digest, exact, identifier, read, text


def pointer(document, path):
    value = document
    if path == '':
        return value
    if not path.startswith('/'):
        raise ContractError('JSON pointer must start with /')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def fact(fid, subject, metric, value, unit, source, json_pointer, *, scope='observed_coordinate_measurement'):
    return {'id': identifier(fid), 'subject': subject, 'metric': metric, 'value': value,
        'unit': unit, 'scope': scope, 'source_id': source, 'json_pointer': json_pointer,
        'derivation': None}


def derive(fid, subject, metric, value, unit, operation, operands, scope):
    return {'id': identifier(fid), 'subject': subject, 'metric': metric, 'value': value,
        'unit': unit, 'scope': scope, 'source_id': None, 'json_pointer': None,
        'derivation': {'operation': operation, 'operands': operands}}


def verify_facts(bundle):
    if 'bundle_sha256' in bundle and canonical_hash({k: v for k, v in bundle.items() if k != 'bundle_sha256'}) != bundle['bundle_sha256']:
        raise ContractError('Fact bundle identity mismatch')
    sources = bundle['sources']
    loaded = {}
    for sid, item in sources.items():
        if digest(item['path']) != item['sha256']:
            raise ContractError('Report source changed: ' + sid)
        loaded[sid] = read(item['path'])
    accepted = {}
    for row in bundle['facts']:
        if row['id'] in accepted:
            raise ContractError('Duplicate fact identity')
        if row['source_id'] is not None:
            if row['source_id'] not in loaded:
                raise ContractError('Unknown fact source')
            expected = pointer(loaded[row['source_id']], row['json_pointer'])
        else:
            expression = row['derivation']
            operands = [accepted[x]['value'] for x in expression['operands']]
            if expression['operation'] == 'subtract' and len(operands) == 2:
                if len({accepted[x]['unit'] for x in expression['operands']} | {row['unit']}) != 1:
                    raise ContractError('Subtraction units differ')
                expected = operands[0] - operands[1]
            elif expression['operation'] == 'divide' and len(operands) == 2 and operands[1] != 0:
                if row['unit'] != 'fraction' or len({accepted[x]['unit'] for x in expression['operands']}) != 1:
                    raise ContractError('Ratio units differ')
                expected = operands[0] / operands[1]
            elif expression['operation'] == 'length' and len(operands) == 1:
                if row['unit'] != 'count':
                    raise ContractError('Length must have count units')
                expected = len(operands[0])
            else:
                raise ContractError('Unsupported or invalid fact derivation')
        if type(expected) is not type(row['value']) or expected != row['value']:
            raise ContractError('Fact differs from its source or arithmetic: ' + row['id'])
        accepted[row['id']] = row
    if 'candidate_records' in accepted:
        expected_ids = [x['name'] for x in accepted['candidate_records']['value']]
        if bundle['candidate_ids'] != expected_ids:
            raise ContractError('Candidate denominator differs from original source')
        for i, subject in enumerate(expected_ids, 1):
            for suffix in ('total', 'phosphate', 'near'):
                if accepted.get(f'c{i}_{suffix}', {}).get('subject') != subject:
                    raise ContractError('Candidate facts omitted or assigned to another subject')
    return accepted


def at8_facts(inputs):
    """Map existing measurement definitions, retaining the full candidate list."""
    kinds = {item['kind']: (key, item) for key, item in inputs.items()}
    needed = ('at8_measurements', 'at8_reference_measurements', 'at8_geometry')
    if not set(needed) <= kinds.keys():
        raise ContractError('AT8 fact adapter requires candidate, reference and geometry sources')
    sources = {key: {'path': item['path'], 'sha256': item['sha256']} for key, item in kinds.values() if item['kind'] in needed}
    mid, msrc = kinds['at8_measurements']
    rid, rsrc = kinds['at8_reference_measurements']
    gid, gsrc = kinds['at8_geometry']
    measured, reference, geometry = read(msrc['path']), read(rsrc['path']), read(gsrc['path'])
    rows = measured['candidates']
    facts = [fact('candidate_records', 'all_candidates', 'candidate_records', rows, 'records', mid, '/candidates'),
        derive('candidate_count', 'all_candidates', 'candidate_count', len(rows), 'count', 'length', ['candidate_records'], 'retained_candidate_denominator'),
        fact('reference_total', 'prepared_AT8_reference', 'total_residue_pair_contacts_le_4_A', reference['contact_counts']['4.0'], 'count', rid, '/contact_counts/4.0'),
        fact('reference_phosphate', 'prepared_AT8_reference', 'phosphate_residue_pair_contacts_le_4_A', reference['phosphate_contact_counts']['4.0'], 'count', rid, '/phosphate_contact_counts/4.0')]
    facts.append(derive('reference_fraction', 'prepared_AT8_reference', 'phosphate_fraction_of_total_contacts_le_4_A',
        reference['phosphate_contact_counts']['4.0'] / reference['contact_counts']['4.0'], 'fraction', 'divide', ['reference_phosphate', 'reference_total'], 'derived_distance_contact_fraction'))
    for i, row in enumerate(rows):
        subject = row['name']
        prefix = 'c' + str(i + 1)
        for suffix, key, metric in (
            ('total', 'contact_pairs_le_4_A', 'total_residue_pair_contacts_le_4_A'),
            ('phosphate', 'phosphate_contact_pairs_le_4_A', 'phosphate_residue_pair_contacts_le_4_A'),
            ('near', 'interchain_atom_pairs_lt_2_A', 'interchain_heavy_atom_pairs_lt_2_A')):
            facts.append(fact(prefix + '_' + suffix, subject, metric, row[key], 'count', mid, f'/candidates/{i}/{key}'))
        for kind in ('total', 'phosphate'):
            key = 'contact_pairs_le_4_A' if kind == 'total' else 'phosphate_contact_pairs_le_4_A'
            ref = reference['contact_counts' if kind == 'total' else 'phosphate_contact_counts']['4.0']
            facts.append(derive(prefix + '_' + kind + '_delta', subject, kind + '_absolute_count_minus_reference',
                row[key] - ref, 'count', 'subtract', [prefix + '_' + kind, 'reference_' + kind], 'difference_of_absolute_distance_contact_counts'))
        facts.append(derive(prefix + '_fraction', subject, 'phosphate_fraction_of_total_contacts_le_4_A',
            row['phosphate_contact_pairs_le_4_A'] / row['contact_pairs_le_4_A'], 'fraction', 'divide',
            [prefix + '_phosphate', prefix + '_total'], 'derived_distance_contact_fraction'))
    pair = geometry['facts']['candidate_04_close_pair']
    facts += [fact('c4_near_distance', 'candidate-04', 'interchain_O_to_O_near_contact_distance', pair['distance_A'], 'angstrom', gid, '/facts/candidate_04_close_pair/distance_A'),
        fact('c4_near_atoms', 'candidate-04', 'near_contact_atom_identifiers', pair['atoms'], 'atom_identifiers', gid, '/facts/candidate_04_close_pair/atoms')]
    bundle = {'schema': 'modab_fact_bundle_v1', 'sources': sources, 'facts': facts,
        'candidate_ids': [row['name'] for row in rows],
        'limits': ['Counts use the declared distance definitions; they are not affinity or selectivity measurements.',
                   'Absolute counts, absolute differences and fractions are distinct facts.',
                   'No paired unphosphorylated prediction or functional validation is included in these sources.']}
    verify_facts(bundle)
    bundle['bundle_sha256'] = canonical_hash(bundle)
    return bundle


def validate_claims(bundle, claims, *, required_candidates=None, executed_steps=None):
    facts = verify_facts(bundle)
    if required_candidates is not None and bundle['candidate_ids'] != required_candidates:
        raise ContractError('Candidate denominator differs from commissioned evidence')
    if not isinstance(claims, list) or len(claims) > 2048:
        raise ContractError('Invalid claims list')
    accepted, pending, errors = [], [], []
    seen = set()
    for claim in claims:
        try:
            exact(claim, {'id', 'kind', 'fact_ids', 'subject', 'metric', 'value', 'unit', 'text'})
            identifier(claim['id'])
            if claim['id'] in seen:
                raise ContractError('Duplicate claim identity')
            seen.add(claim['id'])
            if not isinstance(claim['fact_ids'], list) or not set(claim['fact_ids']) <= facts.keys():
                raise ContractError('Unknown claim evidence')
            if claim['kind'] == 'fact':
                if len(claim['fact_ids']) != 1 or claim['text'] != '':
                    raise ContractError('Verified facts require one source fact and deterministic text')
                original = facts[claim['fact_ids'][0]]
                for key in ('subject', 'metric', 'value', 'unit'):
                    if type(claim[key]) is not type(original[key]) or claim[key] != original[key]:
                        raise ContractError('Claim ' + key + ' differs from the cited fact')
                accepted.append(claim)
            elif claim['kind'] in {'interpretation', 'hypothesis', 'limitation'}:
                text(claim['text'], 'claim text')
                if any(claim[key] is not None for key in ('subject', 'metric', 'value', 'unit')):
                    raise ContractError('Free interpretation cannot impersonate a typed fact')
                if claim['kind'] != 'limitation' and not claim['fact_ids']:
                    raise ContractError('Interpretation requires cited facts')
                pending.append({**claim, 'status': 'UNVERIFIED_INTERPRETATION'})
            else:
                raise ContractError('Unknown claim type')
        except (ContractError, TypeError) as exc:
            errors.append({'claim_id': claim.get('id') if isinstance(claim, dict) else None, 'error': str(exc)})
    return {'status': 'REJECTED' if errors else 'FACTS_VALIDATED_WITH_UNVERIFIED_INTERPRETATIONS' if pending else 'FACTS_VALIDATED',
        'accepted': accepted, 'unverified': pending, 'errors': errors,
        'automatic_semantic_review': False}


def deterministic_claims(bundle):
    return [{'id': 'claim_' + row['id'], 'kind': 'fact', 'fact_ids': [row['id']],
        **{key: row[key] for key in ('subject', 'metric', 'value', 'unit')}, 'text': ''}
        for row in bundle['facts'] if row['unit'] != 'records']


def assemble(bundle, claims, mission_id, steps, conclusion='preparation_only'):
    checked = validate_claims(bundle, claims, required_candidates=bundle['candidate_ids'])
    if checked['errors']:
        raise ContractError('Report rejected: ' + '; '.join(row['error'] for row in checked['errors']))
    return {'schema': 'modab_grounded_report_v1', 'mission_id': mission_id,
        'validation_status': checked['status'], 'facts': bundle, 'claims': checked,
        'execution': [{'step_id': step['step_id'], 'tool_id': step['tool_id'], 'status': step['status'],
            'receipt_sha256': step['receipt'].get('receipt_sha256') if step['receipt'] else None} for step in steps],
        'proposed_conclusion': conclusion, 'scientific_conclusion_automatically_proven': False,
        'all_candidates_retained': True}


def preferred_report(reports):
    """Keep a reviewed report visible when a later fallback repeats its facts."""
    if not reports:return None
    latest=reports[-1]
    if latest['document'].get('research_review'):return latest
    identity=canonical_hash(latest['document']['facts'])
    for entry in reversed(reports[:-1]):
        if entry['document'].get('research_review') and canonical_hash(entry['document']['facts'])==identity:
            return entry
    return latest


def render_html(report):
    esc = lambda value: html.escape(str(value))
    labels = {'candidate_count':'保留的候选数','total_residue_pair_contacts_le_4_A':'总残基对接触数（≤ 4 Å）',
        'phosphate_residue_pair_contacts_le_4_A':'磷酸基团相关残基对接触数（≤ 4 Å）',
        'phosphate_fraction_of_total_contacts_le_4_A':'磷酸接触占总接触的比例（≤ 4 Å）',
        'interchain_heavy_atom_pairs_lt_2_A':'跨链重原子对数（< 2 Å）',
        'total_absolute_count_minus_reference':'总接触绝对数减去参考值',
        'phosphate_absolute_count_minus_reference':'磷酸接触绝对数减去参考值',
        'interchain_O_to_O_near_contact_distance':'跨链氧原子近接触距离',
        'near_contact_atom_identifiers':'近接触原子标识'}
    facts={f['id']:f for f in report['facts']['facts']}
    rows = []
    for c in report['claims']['accepted']:
        f=facts[c['fact_ids'][0]]
        reference = f['source_id'] + ':' + f['json_pointer'] if f['source_id'] else str(f['derivation'])
        values=[c['subject'],labels.get(c['metric'],c['metric']),c['value'],c['unit'],f['id']+' ← '+reference]
        rows.append('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in values) + '</tr>')
    notes = ''.join('<li>' + esc(c['text']) + ' <small>待检验解释</small></li>' for c in report['claims']['unverified'])
    limits = ''.join('<li>' + esc(x) + '</li>' for x in report['facts']['limits'])
    execution = ''.join('<li>' + esc(s['tool_id']) + '：' + esc(s['status']) + '</li>' for s in report['execution'])
    sources=''.join('<li><strong>'+esc(k)+'</strong> '+esc(v['path'])+'<br>SHA-256: '+esc(v['sha256'])+'</li>' for k,v in report['facts']['sources'].items())
    review_section=''
    if report.get('research_review'):
        review=report['research_review']['review'];next_test=review['next_test']
        basis={'continue_research':'继续检验','bounded_inconclusive':'在已声明范围内交付未定结论','resource_blocked':'资源约束','capability_blocked':'能力缺口'}
        disposition={'execute':'执行','defer':'保留为后续工作','not_needed':'本轮不执行'}
        design=''.join('<p>'+label+'：'+esc(next_test[key])+'</p>' for key,label in
            [('hypothesis','待检验解释'),('alternative','竞争解释'),('held_constant','保持不变'),('changed_factor','实际改变')]
            if key in next_test)
        outcomes=''.join('<li>'+('观察：'+esc(x['observation'])+'；判断：'+esc(x['interpretation'])+'；下一步：'+esc(x['next_action'])
            if isinstance(x,dict) else esc(x))+'</li>' for x in next_test['outcomes'])
        review_section='<h2>继续或结束的依据</h2><p>'+esc(basis.get(review['stop_basis'],review['stop_basis']))+'：'+esc(review['reason'])+'</p><p>后续检验：'+esc(next_test['question'])+'</p>'+design+'<ul>'+outcomes+'</ul><p>'+esc(disposition.get(next_test['disposition'],next_test['disposition']))+'：'+esc(next_test['reason'])+'</p><p>已核对证据覆盖、引用与执行一致性；科研判断仍需检验。</p>'
    if review_section:notes+='<li>'+review_section+'</li>'
    return '<!doctype html><meta charset="utf-8"><title>ModAb 证据报告</title><style>body{font:16px system-ui;max-width:1300px;margin:40px auto;padding:0 24px;color:#18302b;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #d5e0db;text-align:left;padding:10px}td{overflow-wrap:anywhere}td:last-child{font-size:12px}small{color:#876200}</style><h1>研究证据报告</h1><p>事实核验状态：' + esc(report['validation_status']) + '</p><p>任务：' + esc(report['mission_id']) + '</p><p>本报告核验数值和来源；科学结论未被自动证明。</p><h2>已记录的执行</h2><ul>' + execution + '</ul><table><tr><th>对象</th><th>测量口径</th><th>数值</th><th>单位</th><th>来源或计算依据</th></tr>' + ''.join(rows) + '</table><h2>解释与待检验项</h2><ul>' + notes + '</ul><h2>适用范围</h2><ul>' + limits + '</ul><h2>来源文件</h2><ul>'+sources+'</ul>'
