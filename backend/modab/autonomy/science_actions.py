"""Pinned AT8 sequence preparation and explicit prediction measurements."""
from pathlib import Path
import math
from .contracts import atomic_json,digest,exact,integer,read
from .reports import fact,verify_facts
from dualctx.contracts import ContractError,canonical_hash

def validate_preparation(params,dataset):
    exact(params,{'timeout_seconds','subjects','tau_start','tau_end','states','msa_mode','antibody_form'})
    integer(params['timeout_seconds'],'timeout',1,3600)
    if params['antibody_form']!='Fv':raise ContractError('Only source-verified Fv constructs are currently registered')
    if not isinstance(params['subjects'],list) or not params['subjects'] or len(set(params['subjects']))!=len(params['subjects']) or not set(params['subjects'])<=dataset['antibodies'].keys():raise ContractError('Unknown or duplicate antibody identity')
    if not (dataset['tau']['start']<=params['tau_start']<=params['tau_end']<=dataset['tau']['end']):raise ContractError('Tau span outside source-verified interval')
    if params['msa_mode'] not in {'server','single_sequence'}:raise ContractError('Invalid MSA mode')
    if not isinstance(params['states'],list) or not 1<=len(params['states'])<=4:raise ContractError('Bounded explicit states required')
    seen=set();definitions=set()
    for state in params['states']:
        exact(state,{'name','phosphorylated_positions'})
        from .contracts import identifier
        identifier(state['name'])
        if state['name'] in seen:raise ContractError('Duplicate state name')
        seen.add(state['name'])
        positions=state['phosphorylated_positions']
        if not isinstance(positions,list) or len(set(positions))!=len(positions):raise ContractError('Unique positions required')
        definition=tuple(sorted(positions))
        if definition in definitions:raise ContractError('Different state names must denote distinct modification sets')
        definitions.add(definition)
        for pos in positions:
            integer(pos,'phosphorylation position',params['tau_start'],params['tau_end'])
            if dataset['tau']['sequence'][pos-dataset['tau']['start']] not in 'STY':raise ContractError('Phosphate parent residue mismatch')

def prepare(inputs,params,output):
    source=next(x for x in inputs.values() if x['kind']=='at8_sequence_dataset')
    dataset=read(source['path']);validate_preparation(params,dataset)
    for item in dataset['source_files'].values():
        if digest(item['path'])!=item['sha256']:raise ContractError('Sequence source changed')
    records=[];start,end=params['tau_start'],params['tau_end']
    tau=dataset['tau']['sequence'][start-dataset['tau']['start']:end-dataset['tau']['start']+1]
    for subject in params['subjects']:
        for state in params['states']:
            proteins=[{'protein':{'id':chain,'sequence':seq}} for chain,seq in dataset['antibodies'][subject].items()]
            proteins.append({'protein':{'id':'C','sequence':tau,'modifications':[{'position':p-start+1,'ccd':{'S':'SEP','T':'TPO','Y':'PTR'}[tau[p-start]]} for p in state['phosphorylated_positions']]}})
            if params['msa_mode']=='single_sequence':
                for entity in proteins:entity['protein']['msa']='empty'
            model_input={'version':1,'sequences':proteins}
            from .adapters import validate_boltz_input
            validate_boltz_input(model_input,params['msa_mode'])
            path=output.parent/(subject+'_'+state['name']+'.json');atomic_json(path,model_input,immutable=True)
            records.append({'subject':subject,'state':state,'tau_start':start,'tau_end':end,'path':str(path.resolve()),'sha256':digest(path),'kind':'boltz2_input','antibody_form':'Fv','msa_mode':params['msa_mode']})
    return {'schema':'modab_at8_prepared_states_v1','records':records,'source_dataset_sha256':source['sha256'],
            'candidate_denominator':dataset['candidate_ids'],'template_conditioning':False,'terminal_chemistry':'untagged default peptide; no biotin/PEG4 or N-acetyl',
            'limits':['Published binding assay used Fab and tagged peptide; these Fv predictions are a qualitative reference, not assay reproduction.']}

def atoms(path,*,reference_ca=False,provenance=None):
    import gemmi,numpy as np
    block=gemmi.cif.read_file(str(path)).sole_block()
    rows=block.find('_atom_site.',['label_asym_id','label_seq_id','label_comp_id','label_atom_id','type_symbol','Cartn_x','Cartn_y','Cartn_z','pdbx_PDB_model_num','label_alt_id'])
    out={}
    for row in rows:
        row=list(row)
        chain,num,res,name,element,*_=list(row)
        if chain not in {'A','B','C'} or not num.isdigit() or element in {'H','D'}:continue
        # Only these reference atoms enter the already declared comparison.
        # A unique labelled coordinate is not a choice between conformers.
        if reference_ca and not (name=='CA' and ((chain in {'A','B'} and 1<=int(num)<=({'A':112,'B':113}[chain])) or (chain=='C' and 9<=int(num)<=16))):continue
        if row[8]!='1' or (not reference_ca and row[9] not in {'.','?'}):raise ContractError('Multiple models or alternate conformers require explicit handling')
        key=(chain,int(num),name)
        if key in out:raise ContractError('Ambiguous atom correspondence')
        if reference_ca and provenance is not None and row[9] not in {'.','?'}:
            provenance.append({'chain':chain,'label_seq_id':int(num),'atom':name,'altloc':row[9],'policy':'only coordinate present for selected atom; no conformer discarded'})
        xyz=np.array([float(v) for v in row[5:8]],dtype=float)
        if not np.isfinite(xyz).all():raise ContractError('Nonfinite coordinate')
        out[key]=(xyz,res,element)
    if not out:raise ContractError('No supported atoms')
    return out

def geometry(prediction,reference,record,cutoff,near):
    import numpy as np
    pred=atoms(prediction);ref=atoms(reference,reference_ca=True)
    antibody=[k for k in pred if k[0] in {'A','B'}];peptide=[k for k in pred if k[0]=='C']
    if not antibody or not peptide:raise ContractError('Missing antibody or peptide chains')
    dist=np.linalg.norm(np.array([pred[k][0] for k in antibody])[:,None,:]-np.array([pred[k][0] for k in peptide])[None,:,:],axis=2)
    # Count residue pairs once even when several atom pairs satisfy the threshold.
    pairs={(antibody[i][:2],peptide[j][:2]) for i,j in np.argwhere(dist<=cutoff)}
    phosphate_names={'P','O1P','O2P','O3P','OP1','OP2','OP3'}
    phosphate={(antibody[i][:2],peptide[j][:2]) for i,j in np.argwhere(dist<=cutoff) if pred[peptide[j]][1] in {'SEP','TPO','PTR'} and peptide[j][2] in phosphate_names}
    anchors=[k for k in sorted(ref) if k[0] in {'A','B'} and k[2]=='CA' and 1<=k[1]<=({'A':112,'B':113}[k[0]])]
    if len(anchors)!=225 or any(k not in pred for k in anchors):raise ContractError('Expected exact 225 Fv C-alpha correspondences')
    moving=np.array([pred[k][0] for k in anchors]);fixed=np.array([ref[k][0] for k in anchors]);mc=moving.mean(0);fc=fixed.mean(0)
    u,s,vt=np.linalg.svd((moving-mc).T@(fixed-fc));correction=np.eye(3);correction[-1,-1]=1 if np.linalg.det(u@vt)>0 else -1
    rotation=u@correction@vt;translation=fc-mc@rotation
    common=[]
    # Prepared experimental peptide label 9..16 corresponds to Tau 202..209.
    for key in ref:
        if key[0]=='C' and key[2]=='CA' and 9<=key[1]<=16:
            tau=193+key[1];pk=('C',tau-record['tau_start']+1,'CA')
            if record['tau_start']<=tau<=record['tau_end'] and pk in pred:common.append((key,pk))
    if not common:raise ContractError('No shared experimental peptide C-alpha correspondence')
    moved=np.array([pred[p][0]@rotation+translation for r,p in common]);fixed_tau=np.array([ref[r][0] for r,p in common])
    return {'antibody_peptide_residue_pair_contacts':len(pairs),'phosphate_atom_residue_pair_contacts':len(phosphate),
        'antibody_peptide_heavy_atom_pairs_below_near_cutoff':int((dist<near).sum()),'minimum_antibody_peptide_distance_A':float(dist.min()),
        'Fv_CA_fit_RMSD_A':float(np.sqrt(np.mean(np.sum((moving@rotation+translation-fixed)**2,axis=1)))),
        'Tau_CA_RMSD_after_Fv_fit_A':float(np.sqrt(np.mean(np.sum((moved-fixed_tau)**2,axis=1)))),
        'Fv_fit_CA_count':len(anchors),'shared_Tau_CA_count':len(common),'contact_cutoff_A':cutoff,'near_cutoff_A':near}

def verify_prediction_identity(path,model_input):
    import gemmi
    observed=atoms(path)
    expected_keys=set()
    for entity in model_input['sequences']:
        protein=entity['protein'];chain=protein['id'];mods={m['position']:m['ccd'] for m in protein.get('modifications',[])}
        for number,parent in enumerate(protein['sequence'],1):
            expected_keys.add((chain,number))
            ca=(chain,number,'CA')
            if ca not in observed:raise ContractError('Predicted residue has no unique C-alpha')
            name=observed[ca][1]
            if number in mods:
                if name!=mods[number] or (chain,number,'P') not in observed:raise ContractError('Predicted phosphorylation identity differs from input')
            elif gemmi.find_tabulated_residue(name).one_letter_code!=parent:raise ContractError('Predicted sequence differs from input')
    if {k[:2] for k in observed}!=expected_keys:raise ContractError('Predicted residue inventory differs from input')

def measure(inputs,params,output):
    exact(params,{'timeout_seconds','contact_cutoff_A','near_cutoff_A'})
    for key in ('contact_cutoff_A','near_cutoff_A'):
        if type(params[key]) not in {int,float} or not math.isfinite(params[key]) or not 0<params[key]<=8:raise ContractError('Invalid distance cutoff')
    if params['near_cutoff_A']>=params['contact_cutoff_A']:raise ContractError('Near cutoff must be smaller')
    reference=next(x for x in inputs.values() if x['kind']=='at8_reference_structure')
    dataset=read(next(x['path'] for x in inputs.values() if x['kind']=='at8_sequence_dataset'))
    prepared=[read(x['path']) for x in inputs.values() if x['kind']=='prepared_states']
    records={r['sha256']:r for p in prepared for r in p['records']}
    measured=[];sources={}
    for key,item in inputs.items():
        if item['kind']!='structure_prediction':continue
        summary=read(item['path']);record=records[summary['input_sha256']];parent=Path(item['path']).parent
        if digest(record['path'])!=record['sha256']:raise ContractError('Prepared sequence input changed')
        model_input=read(record['path'])
        for structure in summary['structures']:
            path=parent/structure['path']
            if digest(path)!=structure['sha256']:raise ContractError('Prediction coordinates changed')
            verify_prediction_identity(path,model_input)
            sample=path.stem.rsplit('_model_',1)[-1]
            confidences=[c for c in summary['confidence_records'] if Path(c['path']).stem.endswith('_model_'+sample)]
            if len(confidences)!=1:raise ContractError('Ambiguous confidence correspondence')
            conf=confidences[0]['confidence'];geo=geometry(path,reference['path'],record,params['contact_cutoff_A'],params['near_cutoff_A'])
            measured.append({'subject':record['subject'],'state':record['state'],'sample':sample,'input_sha256':record['sha256'],
                'structure_sha256':structure['sha256'],'geometry':geo,'confidence':conf,'prediction_summary':item,
                'conditioning':record.get('conditioning',{'mode':'unconditioned'})})
        sources[key]={'path':item['path'],'sha256':item['sha256']}
    if not measured:raise ContractError('At least one actual prediction is required')
    import importlib.metadata,sys
    reference_labels=[];atoms(reference['path'],reference_ca=True,provenance=reference_labels)
    measurement=output.parent/'measurements.json';atomic_json(measurement,{'records':measured,'parameters':params,'reference':reference,
        'reference_atom_policy':{'selected':'225 Fv CA plus 8 shared Tau CA, model 1, label numbering','ambiguity':'reject multiple coordinates for any selected atom','unique_labelled_atoms':reference_labels},
        'software':{'python':sys.version,'packages':{name:importlib.metadata.version(name) for name in ('numpy','gemmi')}},
        'scope':'structure_diagnostics_not_affinity; conditioning is explicit per record'},immutable=True)
    sources['measurements']={'path':str(measurement.resolve()),'sha256':digest(measurement)}
    dataset_source=next(x for x in inputs.values() if x['kind']=='at8_sequence_dataset')
    sources['sequence_dataset']={k:dataset_source[k] for k in ('path','sha256')}
    facts=[fact('registered_candidate_ids','all_candidates','registered_candidate_ids',dataset['candidate_ids'],'identifiers','sequence_dataset','/candidate_ids',scope='retained_candidate_denominator')]
    for i,row in enumerate(measured):
        subject=row['subject']+'/'+row['state']['name']+'/model_'+row['sample']
        for metric,val in row['geometry'].items():
            facts.append(fact('prediction_'+str(i)+'_'+metric,subject,metric,val,'angstrom' if metric.endswith('_A') else 'count','measurements',f'/records/{i}/geometry/{metric}',scope='predicted_coordinate_measurement'))
        for metric in ('confidence_score','iptm','ptm','complex_plddt','complex_iplddt','ligand_iptm','protein_iptm'):
            if metric in row['confidence'] and type(row['confidence'][metric]) in {int,float}:
                facts.append(fact('prediction_'+str(i)+'_'+metric,subject,metric,row['confidence'][metric],'model_score','measurements',f'/records/{i}/confidence/{metric}',scope='model_confidence_not_affinity'))
        for left,right in [('0','2'),('1','2'),('2','0'),('2','1')]:
            value=row['confidence'].get('pair_chains_iptm',{}).get(left,{}).get(right)
            if type(value) in {int,float}:
                metric='pair_chains_iptm_'+left+'_'+right
                facts.append(fact('prediction_'+str(i)+'_'+metric,subject,metric,value,'model_score','measurements',f'/records/{i}/confidence/pair_chains_iptm/{left}/{right}',scope='model_interface_confidence_not_affinity'))
    bundle={'schema':'modab_fact_bundle_v1','facts':facts,'sources':sources,'candidate_ids':dataset['candidate_ids'],
       'coverage':{'predicted_subjects':sorted({r['subject'] for r in measured}),'predicted_records':len(measured),'all_candidates_measured':set(dataset['candidate_ids'])<={r['subject'] for r in measured}},
       'limits':['Model confidence and coordinate similarity do not measure binding affinity or specificity.',
                 'Experimental reference is a crystal structure; possible training overlap means recovery is not an independent accuracy benchmark.',
                 'All four candidate identities remain in the denominator even when only reference calibration was run.',
                 'Peptide RMSD is computed after fitting all 225 Fv C-alpha atoms; no separate peptide fit hides placement error.',
                 'Interface metrics use antibody chains A/B against peptide C only; phosphate atom names are P,O1P,O2P,O3P,OP1,OP2,OP3; residue pairs counted once.',
                 'Unmodified peptide has no phosphate atoms, so a zero phosphate-contact count cannot by itself demonstrate specificity.']}
    verify_facts(bundle);bundle['bundle_sha256']=canonical_hash(bundle)
    return bundle
