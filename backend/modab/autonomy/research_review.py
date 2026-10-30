"""Evidence coverage and structured, explicitly limited research review.

The controller describes available samples, never sets a scientific sample size.
Sampling choices and negative conclusions remain the commissioned Agent's job.
"""
from collections import defaultdict
from itertools import combinations
from dualctx.contracts import ContractError,canonical_hash
from .contracts import read,exact,text
from .reports import verify_facts
from .costs import followup_cost


def descriptive_comparisons(groups):
    """Observed ranges only: no significance, affinity, or information-value test."""
    rows=[]
    metrics=('Tau_CA_RMSD_after_Fv_fit_A','antibody_peptide_residue_pair_contacts',
        'phosphate_atom_residue_pair_contacts')
    for a,b in combinations(groups,2):
        if (a['subject']!=b['subject'] or a['state']==b['state'] or
            a.get('comparison_design') is None or a.get('comparison_design')!=b.get('comparison_design') or
            a['method_parameters']!=b['method_parameters']):continue
        for metric in metrics:
            x=a['observed_metric_ranges'].get(metric);y=b['observed_metric_ranges'].get(metric)
            if not x or not y:continue
            relation='overlapping_observed_ranges';gap=0
            if x['min']>y['max']:relation='first_observed_range_above_second';gap=x['min']-y['max']
            elif y['min']>x['max']:relation='second_observed_range_above_first';gap=y['min']-x['max']
            rows.append({'subject':a['subject'],'metric':metric,'first_state':a['state']['name'],
                'second_state':b['state']['name'],'first_range':x,'second_range':y,
                'observed_range_relationship':relation,'gap_between_observed_ranges':gap,
                'statistical_significance':'not_tested','scope':'These retained computational samples only; no population or binding conclusion'})
    return rows


def observation_summary(inputs,steps,budget,usage):
    prepared={};measured={};bundles={}
    for key,item in inputs.items():
        if item['kind']=='prepared_states':
            for r in read(item['path'])['records']:prepared[r['sha256']]=r
        elif item['kind']=='fact_bundle':
            bundle=read(item['path']);verify_facts(bundle);bundles[key]=bundle
            source=bundle.get('sources',{}).get('measurements')
            if source:
                for r in read(source['path']).get('records',[]):measured[r['structure_sha256']]=r
    outputs={};groups={}
    for step in steps:
        if step['tool_id']!='boltz2_predict' or not step.get('receipt') or step['receipt']['status']!='SUCCEEDED':continue
        spec=step['spec'];inp=next(iter(spec['inputs'].values()));r=prepared.get(inp['sha256'])
        if not r:raise ContractError('Successful prediction has no recorded prepared input identity')
        method={k:v for k,v in spec['parameters'].items() if k not in {'seed','timeout_seconds'}}
        key=r['subject']+'/'+r['state']['name']+'/'+canonical_hash([inp['sha256'],method])[:16]
        g=groups.setdefault(key,{'subject':r['subject'],'state':r['state'],'input_sha256':inp['sha256'],
            'comparison_design':{k:r[k] for k in ('antibody_form','tau_start','tau_end','msa_mode')}
                if all(k in r for k in ('antibody_form','tau_start','tau_end','msa_mode')) else None,
            'method_parameters':method,
            'input_ids':[k for k,v in inputs.items() if v['kind']=='boltz2_input' and v['sha256']==inp['sha256']],
            'seeds':set(),'structures':set(),'unmeasured_structures':set(),'metrics':defaultdict(list),'last_parameters':spec['parameters']})
        g['seeds'].add(spec['parameters']['seed'])
        for artifact in step['receipt']['artifacts']:
            if artifact['kind']!='predicted_structure':continue
            sha=artifact['sha256'];outputs[sha]=key
            if sha in g['structures']:continue
            g['structures'].add(sha)
            if sha not in measured:g['unmeasured_structures'].add(sha)
            else:
                for metric,value in measured[sha]['geometry'].items():g['metrics'][metric].append(value)
    result=[]
    for g in groups.values():
        result.append({**{k:v for k,v in g.items() if k not in {'seeds','structures','unmeasured_structures','metrics'}},
            'seeds':sorted(g['seeds']),'distinct_seed_count':len(g['seeds']),'structure_count':len(g['structures']),
            'unmeasured_count':len(g['unmeasured_structures']),
            'observed_metric_ranges':{k:{'min':min(v),'max':max(v),'n':len(v)} for k,v in g['metrics'].items()}})
    pending=[g for g in result if g['distinct_seed_count']<2]
    remaining={k:None if budget[k] is None else budget[k]-usage.get(k,0)
        for k in budget if k!='max_concurrent_jobs'}
    # Planning estimate only. The actual chosen action is reserved by the store.
    # Another seed is an example diagnostic, not a mandatory scientific design.
    minimum=followup_cost(pending,steps,remaining)
    matched_cost=followup_cost(result,steps,remaining)
    return {'groups':result,'successful_structure_sha256':sorted(outputs),'unmeasured_count':sum(g['unmeasured_count'] for g in result),
        'descriptive_state_comparisons':descriptive_comparisons(result),
        'matched_followup_round':{'group_count':len(result),'prediction_jobs':len(result),'combined_measurement_jobs':1,
            'report_jobs':1,**matched_cost,
            'is_required_design':False,'expected_information_value':'not_quantified',
            'scope':'Optional additional seed for every existing input/method group, followed by combined measurement and a new report; includes analysis and delivery costs.'},
        'sampling_uncertainty':'not_quantified' if pending else 'descriptive_repeats_available' if result else 'no_predictions',
        'remaining_budget':remaining,'seed_sensitivity_round':{'groups_needing_another_seed':[g['subject']+'/'+g['state']['name'] for g in pending],
            **minimum,
            'is_required_design':False},
        'limits':['Distinct seeds are computational samples, not biological replicates. Two seeds only allow an initial sensitivity check.',
            'Unphosphorylated matched states already in groups are existing controls, not missing controls.'],
        'fact_bundle_ids':list(bundles)}


def validate_review(review,bundle,progress,catalog,policy):
    exact(review,{'stop_basis','reason','evidence_fact_ids','next_test'})
    facts=verify_facts(bundle)
    if not isinstance(review['evidence_fact_ids'],list) or not review['evidence_fact_ids'] or not set(review['evidence_fact_ids'])<=facts.keys():
        raise ContractError('RESEARCH_REVIEW: Cite fact IDs from this report bundle')
    text(review['reason'],'review reason',3000)
    if review['stop_basis'] not in {'continue_research','bounded_inconclusive','resource_blocked','capability_blocked'}:
        raise ContractError('RESEARCH_REVIEW: Unknown stopping basis')
    test=review['next_test']
    if not isinstance(test,dict):raise ContractError('RESEARCH_REVIEW: next_test must be an object')
    design_fields={'hypothesis','alternative','held_constant','changed_factor'}
    structured=policy.get('require_discriminating_outcomes',False) or bool(design_fields & set(test))
    exact(test,{'tool_id','question','outcomes','disposition','reason'} | (design_fields if structured else set()))
    text(test['question'],'next test question',1500);text(test['reason'],'next test reason',1500)
    if test['tool_id'] not in catalog:raise ContractError('RESEARCH_REVIEW: Unknown follow-up tool')
    if structured:
        for field in design_fields:text(test[field],field,1500)
        if test['hypothesis'].strip()==test['alternative'].strip():raise ContractError('RESEARCH_REVIEW: Hypothesis and alternative must differ')
        if not isinstance(test['outcomes'],list) or len(test['outcomes'])!=2:
            raise ContractError('RESEARCH_REVIEW: Provide two structured outcome branches')
        for outcome in test['outcomes']:
            exact(outcome,{'observation','interpretation','next_action'})
            for field,value in outcome.items():text(value,field,1500)
        for field in ('observation','interpretation'):
            if test['outcomes'][0][field].strip()==test['outcomes'][1][field].strip():
                raise ContractError('RESEARCH_REVIEW: Outcome branches must distinguish observations and implications')
    elif not isinstance(test['outcomes'],list) or len(test['outcomes'])!=2 or any(not isinstance(x,str) or not x.strip() for x in test['outcomes']) or test['outcomes'][0]==test['outcomes'][1]:
        raise ContractError('RESEARCH_REVIEW: State two different informative outcomes of the next test')
    if test['disposition'] not in {'execute','defer','not_needed'}:raise ContractError('RESEARCH_REVIEW: Invalid next-test disposition')
    basis=review['stop_basis']
    if progress['unmeasured_count']:raise ContractError('RESEARCH_REVIEW: Some successful predictions remain unmeasured')
    source=bundle.get('sources',{}).get('measurements')
    covered={r['structure_sha256'] for r in read(source['path']).get('records',[])} if source else set()
    if not set(progress['successful_structure_sha256'])<=covered:
        raise ContractError('RESEARCH_REVIEW: Report omits already successful predictions; measure all retained samples together')
    if basis=='capability_blocked' and catalog[test['tool_id']]['availability'] not in {'NOT_DEPLOYED','UNAVAILABLE'}:
        raise ContractError('RESEARCH_REVIEW: Proposed follow-up capability is available; explain a specific runtime/input failure instead')
    if basis=='continue_research' and test['disposition']!='execute':
        raise ContractError('RESEARCH_REVIEW: Continuing research needs an executable next test')
    if basis!='continue_research' and test['disposition']=='execute':raise ContractError('RESEARCH_REVIEW: An informative executable follow-up remains pending')
    return {'status':'STRUCTURED_REVIEW_PASSED','review':review,'observations':progress,
        'scientific_semantics_automatically_proven':False,'review_scope':'coverage, sampling evidence, cited facts, resource and capability consistency'}
