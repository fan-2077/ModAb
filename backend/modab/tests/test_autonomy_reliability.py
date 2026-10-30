"""Regressions for observed delivery, retry, context and stopping failures."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime,timezone,timedelta

from dualctx.contracts import ContractError
from modab.autonomy.contracts import atomic_json,digest,validate_decision
from modab.autonomy.reliability import decode_response,execution_identity,prevent_unchanged_retry,compact_decisions
from modab.autonomy.research_review import observation_summary,validate_review,descriptive_comparisons
from modab.autonomy.reports import at8_facts,assemble,deterministic_claims,render_html,preferred_report
from modab.autonomy.store import ResearchStore
from modab.autonomy.controller import Controller
from modab.autonomy.planners import RecordedPlanner
from modab.autonomy.adapters import Registry
from modab.autonomy.jobs import LocalJobs
from modab.autonomy.worker import identity,replace_file
from modab.tests.test_autonomy import commission,inputs
from modab.tests.test_autonomy_controller import plan


class ReliabilityTests(unittest.TestCase):
    def test_windows_metadata_replacement_retries_transient_sharing_only(self):
        locked=PermissionError('sharing fixture');locked.winerror=32
        with patch('modab.autonomy.worker.os.replace',side_effect=[locked,None]) as replace,patch('modab.autonomy.worker.time.sleep'):
            replace_file('source','destination');self.assertEqual(replace.call_count,2)
        with patch('modab.autonomy.worker.os.replace',side_effect=locked) as replace,patch('modab.autonomy.worker.time.sleep'):
            with self.assertRaises(PermissionError):replace_file('source','destination')
            self.assertEqual(replace.call_count,21)
        with patch('modab.autonomy.worker.os.replace',side_effect=PermissionError('unrelated')) as replace:
            with self.assertRaises(PermissionError):replace_file('source','destination')
            self.assertEqual(replace.call_count,1)

    def test_starting_worker_is_waited_on_but_stale_owner_remains_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs=LocalJobs(tmp);directory=jobs.directory('job_starting');directory.mkdir()
            manifest={'job_id':'job_starting'};atomic_json(directory/'manifest.json',manifest)
            atomic_json(directory/'execution-once.json',{'worker_pid':123})
            state={'job_id':'job_starting','manifest_sha256':identity(manifest),'status':'STARTING',
                'heartbeat_utc':datetime.now(timezone.utc).isoformat()}
            atomic_json(directory/'state.json',state)
            self.assertEqual(jobs.status('job_starting')['status'],'STARTING')
            state['heartbeat_utc']=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
            atomic_json(directory/'state.json',state)
            self.assertEqual(jobs.status('job_starting')['status'],'UNKNOWN')

    def test_measurement_binds_only_unique_hash_linked_companions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);available={}
            def add(key,kind,value):
                path=root/(key+'.json');atomic_json(path,value)
                available[key]={'path':str(path),'sha256':digest(path),'kind':kind}
            add('reference','at8_reference_structure',{})
            add('dataset','at8_sequence_dataset',{})
            add('coordinates','predicted_structure',{'fixture':'coordinate identity only'})
            add('summary','structure_prediction',{'input_sha256':'inputhash','structures':[{'sha256':available['coordinates']['sha256']}]})
            add('prepared','prepared_states',{'records':[{'sha256':'inputhash'}]})
            decision={'tool_id':'measure_predictions','input_ids':['coordinates'],'parameters':{'timeout_seconds':30,'contact_cutoff_A':4.5,'near_cutoff_A':3.5}}
            spec=Registry({}).prepare({'mission_id':'fixture'},decision,available,[])
            self.assertEqual(set(spec['inputs']),set(available));self.assertEqual(len(spec['input_bindings']),4)
            add('other_reference','at8_reference_structure',{'other':True})
            with self.assertRaisesRegex(ContractError,'unambiguous'):Registry({}).prepare({'mission_id':'fixture'},decision,available,[])

    def test_input_and_parameter_errors_name_exact_repairs(self):
        reg=Registry({});decision={'tool_id':'reuse_evidence','input_ids':['measurements'],'parameters':{'timeout_seconds':15}}
        with self.assertRaisesRegex(ContractError,'at8_reference_measurements.*reference'):
            reg.prepare({'mission_id':'fixture'},decision,inputs(),[])
        decision['input_ids']=list(inputs());decision['parameters']['conclusion']='inconclusive'
        with self.assertRaisesRegex(ContractError,'extra=conclusion; allowed=timeout_seconds'):
            reg.prepare({'mission_id':'fixture'},decision,inputs(),[])

    def test_schema_implied_repair_keeps_raw_response_and_rejects_duplicate_or_meaningful_missing_fields(self):
        doc=plan()[0];del doc['conclusion']
        raw={'content':[{'type':'text','text':'```json\n'+json.dumps(doc)+'\n```'}]}
        response=decode_response({'raw_response':raw})
        self.assertEqual(response['raw_response'],raw)
        self.assertIsNone(response['decision']['conclusion'])
        self.assertEqual(len(response['normalizations']),2)
        validate_decision(response['decision'],commission(),{'reuse_evidence':{}},inputs())
        del doc['tool_id']
        response=decode_response({'raw_response':{'content':[{'type':'text','text':json.dumps(doc)}]}})
        with self.assertRaises(ContractError):validate_decision(response['decision'],commission(),{'reuse_evidence':{}},inputs())
        response=decode_response({'raw_response':{'content':[{'type':'text','text':'{"action":"finish","action":"run_tool"}'}]}})
        self.assertIsNone(response['decision']);self.assertTrue(response['parse_error'])

    def test_failed_action_cannot_be_repeated_by_extending_timeout_or_changing_reason(self):
        spec={'tool_id':'measure_predictions','inputs':{'x':{'kind':'fixture','sha256':'a'*64}},
            'parameters':{'timeout_seconds':20,'contact_cutoff_A':4},'timeout_seconds':20,'runtime_identity':'runtime1'}
        spec['execution_identity']=execution_identity(spec)
        step={'step_id':'failed','spec':copy.deepcopy(spec),'receipt':{'status':'FAILED','diagnostic_excerpt':{'stderr':'ModuleNotFoundError: gemmi'}}}
        spec['parameters']['timeout_seconds']=40;spec['timeout_seconds']=40
        with self.assertRaisesRegex(ContractError,'dependency_missing'):prevent_unchanged_retry(spec,[step])
        step['receipt']={'status':'TIMED_OUT'}
        prevent_unchanged_retry(spec,[step])
        step['receipt']['status']='FAILED';spec['runtime_identity']='repaired_runtime'
        prevent_unchanged_retry(spec,[step])

    def test_rejected_text_cannot_reenter_as_research_evidence(self):
        result=compact_decisions([{'sequence':1,'decision_id':'d','status':'REJECTED','error':'missing field',
            'document':{'action':'finish','reason':'fabricated affinity 999','parameters':{'nested':'untrusted'}}}])
        self.assertNotIn('fabricated',json.dumps(result));self.assertNotIn('nested',json.dumps(result))
        self.assertEqual(result[0]['error'],'missing field')

    def test_revision_reuses_same_task_budget_and_does_not_reapply_old_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'plan.json';atomic_json(path,plan()+[plan()[1],plan()[2]])
            with ResearchStore(root/'store',migrate=True) as store:
                m=store.create(commission(),{'kind':'fixture'});mid=m['mission_id']
                store.approve(mid,m['commission_sha256'],'fixture','fixture://reliability')
                planner=RecordedPlanner({'path':str(path),'sha256':digest(path)})
                controller=Controller(store,{'workspace':str(root/'store')},planner)
                end=time.monotonic()+20
                while time.monotonic()<end:
                    snap=controller.tick(mid)
                    if snap['status']=='COMPLETED_WITH_LIMITATIONS':break
                    time.sleep(.05)
                self.assertEqual(snap['status'],'COMPLETED_WITH_LIMITATIONS',snap['reason'])
                previous=copy.deepcopy(snap)
                store.request_revision(mid,{'kind':'explicit_user_repair'},'Review original stopping rationale',snap['usage'])
                self.assertEqual(store.usage(mid),previous['usage'])
                snap=controller.tick(mid)
                self.assertEqual(len(snap['decisions']),4)
                self.assertNotEqual(snap['status'],'COMPLETED_WITH_LIMITATIONS')
                self.assertEqual(snap['reports'],previous['reports'])
                self.assertEqual(snap['commission_sha256'],previous['commission_sha256'])
                with self.assertRaisesRegex(ContractError,'Only a completed'):store.request_revision(mid,'again','duplicate',snap['usage'])
                # The request passed to a worker must never recursively embed old specs.
                controller.tick(mid)
                request=store.steps(mid)[-1]['spec']['request']
                self.assertTrue(request['prior_steps'])
                self.assertTrue(all('spec' not in s for s in request['prior_steps']))


class ResearchReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.bundle=at8_facts(inputs())
        self.catalog={'boltz2_predict':{'availability':'QUALIFIED'},'grounded_report':{'availability':'LOCAL_ADAPTER_READY'}}
        self.progress={'successful_structure_sha256':[],'unmeasured_count':0,'sampling_uncertainty':'not_quantified','seed_sensitivity_round':{}}
        self.review={'stop_basis':'bounded_inconclusive','reason':'Descriptive result only; repeats would test sampling uncertainty, not affinity.',
            'evidence_fact_ids':['candidate_count'],'next_test':{'tool_id':'boltz2_predict','question':'Is placement sensitive to sampling?',
                'outcomes':['Placement changes substantially','Placement remains similar'],'disposition':'defer','reason':'This fixture delivers the existing evidence without a scientific sample-size rule.'}}

    def test_negative_report_can_finish_without_forced_sample_size(self):
        checked=validate_review(self.review,self.bundle,self.progress,self.catalog,{})
        self.assertEqual(checked['status'],'STRUCTURED_REVIEW_PASSED')
        self.assertFalse(checked['scientific_semantics_automatically_proven'])

    def test_state_ranges_remain_distinct_and_observation_is_not_significance(self):
        groups=[]
        for name,placement,contacts in [('unmodified',(8.4,9.38),(0,0)),('double',(6.15,6.55),(1,11)),('triple',(5.24,6.14),(0,9))]:
            groups.append({'subject':'reference','state':{'name':name},'method_parameters':{'sampling_steps':200},
                'comparison_design':{'antibody_form':'Fv','tau_start':195,'tau_end':214,'msa_mode':'server'},
                'observed_metric_ranges':{k:{'min':v[0],'max':v[1],'n':2} for k,v in (
                    ('Tau_CA_RMSD_after_Fv_fit_A',placement),('phosphate_atom_residue_pair_contacts',contacts))}})
        rows=descriptive_comparisons(groups)
        contacts=next(r for r in rows if r['first_state']=='double' and r['second_state']=='triple' and r['metric'].startswith('phosphate'))
        self.assertEqual((contacts['first_range']['min'],contacts['first_range']['max']),(1,11))
        self.assertEqual((contacts['second_range']['min'],contacts['second_range']['max']),(0,9))
        placement=next(r for r in rows if r['first_state']=='unmodified' and r['second_state']=='double' and r['metric'].startswith('Tau'))
        self.assertEqual(placement['observed_range_relationship'],'first_observed_range_above_second')
        self.assertEqual(placement['statistical_significance'],'not_tested')
        groups[1]['comparison_design']['tau_start']=193
        self.assertFalse(any(r['first_state']=='double' or r['second_state']=='double' for r in descriptive_comparisons(groups)))

    def test_third_seed_estimate_includes_measurement_and_report(self):
        path=self.root/'prepared.json'
        records=[{'subject':'reference','state':{'name':state},'sha256':state} for state in ('unmodified','double','triple')]
        atomic_json(path,{'records':records})
        steps=[]
        for state in ('unmodified','double','triple'):
            for seed in (7,42):
                steps.append({'tool_id':'boltz2_predict','spec':{'inputs':{'x':{'sha256':state}},
                    'parameters':{'seed':seed,'sampling_steps':200,'timeout_seconds':1800}},
                    'receipt':{'status':'SUCCEEDED','artifacts':[{'kind':'predicted_structure','sha256':state+str(seed)}]}})
        budget={'model_requests':None,'model_tokens':1000000,'compute_seconds':7200,'job_attempts':12,'max_concurrent_jobs':1}
        summary=observation_summary({'p':{'kind':'prepared_states','path':str(path)}},steps,budget,{'job_attempts':9})
        matched=summary['matched_followup_round']
        self.assertEqual(matched['estimated_action_budget']['job_attempts'],5)
        self.assertFalse(matched['fits_non_token_budget'])
        self.assertEqual(matched['expected_information_value'],'not_quantified')
        self.assertFalse(matched['is_required_design'])
        summary=observation_summary({'p':{'kind':'prepared_states','path':str(path)}},steps,budget,{'job_attempts':7})
        self.assertTrue(summary['matched_followup_round']['fits_non_token_budget'])

    def test_inconsistent_followup_missing_evidence_and_unmeasured_outputs_are_rejected(self):
        bad=copy.deepcopy(self.review);bad['evidence_fact_ids']=['invented']
        with self.assertRaises(ContractError):validate_review(bad,self.bundle,self.progress,self.catalog,{})
        bad=copy.deepcopy(self.review);bad['next_test']['disposition']='execute'
        with self.assertRaisesRegex(ContractError,'pending'):validate_review(bad,self.bundle,self.progress,self.catalog,{})
        bad=copy.deepcopy(self.review);bad['stop_basis']='continue_research'
        with self.assertRaisesRegex(ContractError,'executable'):validate_review(bad,self.bundle,self.progress,self.catalog,{})
        bad=copy.deepcopy(self.review);bad['stop_basis']='capability_blocked'
        with self.assertRaisesRegex(ContractError,'available'):validate_review(bad,self.bundle,self.progress,self.catalog,{})
        self.progress['unmeasured_count']=1
        with self.assertRaisesRegex(ContractError,'unmeasured'):validate_review(self.review,self.bundle,self.progress,self.catalog,{})

    def test_report_cannot_drop_a_successful_sample(self):
        self.progress['successful_structure_sha256']=['omitted']
        with self.assertRaisesRegex(ContractError,'omits'):validate_review(self.review,self.bundle,self.progress,self.catalog,{})

    def test_budget_fallback_preserves_reviewed_report_with_same_facts(self):
        report=assemble(self.bundle,deterministic_claims(self.bundle),'fixture',[],'inconclusive')
        reviewed=copy.deepcopy(report);reviewed['research_review']=validate_review(self.review,self.bundle,self.progress,self.catalog,{})
        entries=[{'document':reviewed},{'document':report}]
        self.assertIs(preferred_report(entries),entries[0])
        entries[1]['document']['facts']['limits'].append('New evidence boundary')
        self.assertIs(preferred_report(entries),entries[1])

    def test_reviewed_report_finalizes_without_another_model_request(self):
        with ResearchStore(self.root/'store',migrate=True) as store:
            m=commission();m['evidence_kind']='COMPUTATIONAL_RESEARCH';m['planner']['kind']='recorded_plan'
            mission=store.create(m,{'kind':'fixture'});mid=mission['mission_id']
            store.approve(mid,mission['commission_sha256'],'fixture','fixture://finalization')
            report=assemble(self.bundle,deterministic_claims(self.bundle),mid,[],'inconclusive')
            report['research_review']=validate_review(self.review,self.bundle,self.progress,self.catalog,{})
            path=self.root/'report.json';atomic_json(path,report)
            did=store.begin_decision(mid,{}, {'kind':'fixture'},{});store.finish_decision(did,'ACCEPTED',plan()[1])
            spec={'tool_id':'grounded_report','resource_id':'local_cpu','cost_reservation':{'job_attempts':1,'compute_seconds':1}}
            step=store.create_step(mid,did,spec)
            store.update_step(step['step_id'],'SUCCEEDED',{'status':'SUCCEEDED','receipt_sha256':'fixture','artifacts':[{'kind':'grounded_report','path':str(path),'sha256':digest(path)}]})
            store.report(mid,report)
            controller=Controller(store,{'workspace':str(self.root/'store'),'research_policy':{'require_stop_review':True}},None)
            context={'commission':m,'research_progress':self.progress,'catalog':self.catalog,'revision_requests':[]}
            before=store.usage(mid)
            self.assertTrue(controller._finalize_reviewed_report(mid,context))
            self.assertEqual(store.mission(mid)['status'],'COMPLETED_WITH_LIMITATIONS')
            self.assertEqual(store.usage(mid),before);self.assertEqual(len(store.decisions(mid)),1)
            store.request_revision(mid,'user','Request a further review',before)
            context['revision_requests']=store.revisions(mid)
            self.assertFalse(controller._finalize_reviewed_report(mid,context))

    def test_finish_requires_current_review_and_consistent_conclusion(self):
        with ResearchStore(self.root/'store',migrate=True) as store:
            m=commission();m['evidence_kind']='COMPUTATIONAL_RESEARCH';m['planner']['kind']='recorded_plan'
            mission=store.create(m,{'kind':'fixture'});mid=mission['mission_id']
            store.approve(mid,mission['commission_sha256'],'fixture','fixture://finish-gate')
            report=assemble(self.bundle,deterministic_claims(self.bundle),mid,[],'inconclusive')
            store.report(mid,report)
            controller=Controller(store,{'workspace':str(self.root/'store'),'research_policy':{'require_stop_review':True}},None)
            def finish(conclusion):
                doc={**plan()[2],'conclusion':conclusion}
                did=store.begin_decision(mid,{}, {'kind':'fixture'},{});store.finish_decision(did,'ACCEPTED',doc)
                return controller._apply(store.mission(mid),store.decisions(mid)[-1],{'research_progress':self.progress})
            snap=finish('inconclusive');self.assertEqual(snap['decisions'][-1]['status'],'REJECTED')
            report['research_review']=validate_review(self.review,self.bundle,self.progress,self.catalog,{})
            store.report(mid,report)
            snap=finish('supported');self.assertEqual(snap['decisions'][-1]['status'],'REJECTED')
            self.assertIn('differs',snap['reason'])
            snap=finish('inconclusive');self.assertEqual(snap['status'],'COMPLETED_WITH_LIMITATIONS')
            report['research_review']['review']['reason']='<script>untrusted</script>'
            html=render_html(report);self.assertIn('&lt;script&gt;',html);self.assertNotIn('<script>',html)

    def test_sampling_summary_separates_input_and_method_changes(self):
        prepared=self.root/'prepared.json';atomic_json(prepared,{'records':[{'subject':'reference','state':{'name':'state'},'sha256':'inputhash'}]})
        input_map={'prepared':{'kind':'prepared_states','path':str(prepared)},'prediction_input':{'kind':'boltz2_input','sha256':'inputhash'}}
        steps=[]
        for seed,method in [(1,10),(2,10),(3,20)]:
            steps.append({'tool_id':'boltz2_predict','spec':{'inputs':{'input':{'sha256':'inputhash'}},
                'parameters':{'seed':seed,'sampling_steps':method,'timeout_seconds':60}},
                'receipt':{'status':'SUCCEEDED','artifacts':[{'kind':'predicted_structure','sha256':'s'+str(seed)}]}})
        summary=observation_summary(input_map,steps,commission()['budget'],{})
        self.assertEqual(sorted(g['distinct_seed_count'] for g in summary['groups']),[1,2])
        self.assertEqual(summary['unmeasured_count'],3)
        self.assertFalse(summary['seed_sensitivity_round']['is_required_design'])
        steps[0]['spec']['inputs']['input']['sha256']='unmatched'
        with self.assertRaisesRegex(ContractError,'identity'):observation_summary(input_map,steps,commission()['budget'],{})


if __name__=='__main__':unittest.main()
