"""Derive explicit construct and sampling facts from a retained mission.

This is a presentation aid, not a scientific interpretation validator.
"""
def evidence_summary(mission):
    steps = mission.get('steps', [])
    preparations = [s['parameters'] for s in steps if s.get('tool') == 'prepare_at8_states']
    predictions = [s for s in steps if s.get('tool') == 'boltz2_predict' and s.get('status') == 'SUCCEEDED']
    constructs = []
    for p in preparations:
        item = {'antibody_form': p['antibody_form'], 'tau_start': p['tau_start'],
                'tau_end': p['tau_end'], 'tau_residue_count': p['tau_end'] - p['tau_start'] + 1,
                'subjects': p['subjects'], 'states': p['states']}
        if item not in constructs:
            constructs.append(item)
    rows = mission.get('comparisons', [])
    groups = {}
    for r in rows:
        key = '/'.join(r['subject'].split('/')[:2])
        groups.setdefault(key, []).append(r)
    sampling = [{'subject_state': key, 'measured_samples': len(values)} for key, values in groups.items()]
    rankings = {}
    for metric in ('Tau_CA_RMSD_after_Fv_fit_A','antibody_peptide_residue_pair_contacts','phosphate_atom_residue_pair_contacts','iptm','complex_plddt'):
        values=[{'subject':r['subject'],'value':r['metrics'][metric]} for r in rows if isinstance(r.get('metrics',{}).get(metric),(int,float))]
        rankings[metric] = sorted(values,key=lambda r:r['value'],reverse=True)
    return {'constructs': constructs, 'sampling': sampling, 'metric_values_descending': rankings,
            'prediction_jobs_in_this_mission': len(predictions),
            'measured_samples_in_this_mission': len(rows),
            'within_state_variation_estimated': False if groups and all(len(v) == 1 for v in groups.values()) else None,
            'interpretation': 'One measured sample per state cannot estimate within-state sampling variation.' if groups and all(len(v) == 1 for v in groups.values()) else 'Use matched parameters and distinct input/seed identities before pooling samples.',
            'rules': ['每项指标必须分别排序。例如 iptm 与 complex_plddt 不是同一个量，不能合称置信度后共享一个高低排序。排序仅比较数值，不定义性能优劣。',
                      '未磷酸化组保留为总接触和位置比较的配对对照；其磷酸接触零来自输入没有磷酸原子。',
                      '分别陈述位置偏差、总接触与磷酸接触的数值差异，不把一项非零读数概括成唯一跨状态差异。',
                      '位置偏差同时包含肽段形状和相对位置；接触数和置信度不能直接换算成结合力。',
                      '本任务与历史任务使用相同输入和随机种子时，不能直接累加为独立重复。']}
