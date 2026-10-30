"""Regressions for timeout-as-cost and indistinguishable research branches."""
import copy
import json
import unittest

from dualctx.contracts import ContractError
from modab.autonomy.costs import estimate_runtime
from modab.autonomy.adapters import Registry
from modab.autonomy.research_review import validate_review
from modab.autonomy.reports import at8_facts,assemble,deterministic_claims,render_html
from modab.tests.test_autonomy import inputs
from modab.autonomy.reliability import decode_response


class CostAndReasoningTests(unittest.TestCase):
    def test_only_complete_normal_stop_decision_can_recover_one_outer_brace(self):
        doc={'action':'run_tool','reason':'Generate report','evidence_ids':[],
             'tool_id':'grounded_report','input_ids':[],'parameters':{'timeout_seconds':600}}
        raw={'stopReason':'stop','content':[{'type':'text','text':json.dumps(doc)[:-1]}]}
        result=decode_response({'raw_response':raw})
        self.assertEqual(result['decision'],{**doc,'conclusion':None})
        self.assertEqual(result['raw_response'],raw)
        self.assertIn('closed_single_missing_outer_object_brace',result['normalizations'])
        for stop,body in [('length',json.dumps(doc)[:-1]),('stop',json.dumps(doc)[:-2]),
                          ('stop','{"action":"run_tool","reason":"incomplete'),
                          ('stop','{"action":"run_tool","action":"finish","parameters":{}')]:
            bad=decode_response({'raw_response':{'stopReason':stop,'content':[{'type':'text','text':body}]}})
            self.assertIsNone(bad['decision'])

    def test_empirical_estimate_does_not_sum_timeouts_or_hide_failures(self):
        params={'seed':42,'sampling_steps':200,'timeout_seconds':2400}
        spec={'inputs':{'p':{'sha256':'same'}},'parameters':params,'runtime_identity':'v1'}
        steps=[{'tool_id':'boltz2_predict','step_id':'ok','spec':spec,
                'receipt':{'status':'SUCCEEDED','elapsed_seconds':80}},
               {'tool_id':'boltz2_predict','step_id':'failed','spec':spec,
                'receipt':{'status':'FAILED','elapsed_seconds':100}}]
        estimate=estimate_runtime('boltz2_predict',{**params,'seed':7},steps,input_sha256='same',reference_spec=spec)
        self.assertEqual(estimate['estimated_seconds'],160)
        self.assertEqual(estimate['timeout_seconds'],2400)
        self.assertTrue(estimate['failed_observations_retained'])
        for change in ({'sampling_steps':400},{'sampling_steps':200}):
            other=copy.deepcopy(spec);other['runtime_identity']='v2'
            fallback=estimate_runtime('boltz2_predict',{**params,**change},steps,input_sha256='same',reference_spec=other)
            self.assertEqual(fallback['estimated_seconds'],2400)
            self.assertFalse(fallback['observed_jobs'])
        self.assertFalse(estimate_runtime('boltz2_predict',params,steps,input_sha256='different')['observed_jobs'])
        self.assertFalse(estimate_runtime('boltz2_predict',{**params,'sampling_steps':400},steps,input_sha256='same')['observed_jobs'])

    def test_remaining_time_caps_execution_without_rewriting_model_decision(self):
        decision={'tool_id':'reuse_evidence','input_ids':list(inputs()),'parameters':{'timeout_seconds':600}}
        original=copy.deepcopy(decision)
        spec=Registry({}).prepare({'mission_id':'fixture'},decision,inputs(),[],remaining_compute_seconds=120)
        self.assertEqual(spec['timeout_seconds'],120)
        self.assertEqual(spec['cost_reservation']['compute_seconds'],120)
        self.assertEqual(spec['request']['parameters']['timeout_seconds'],120)
        self.assertEqual(decision,original)
        with self.assertRaisesRegex(ContractError,'BUDGET_EXHAUSTED'):
            Registry({}).prepare({'mission_id':'fixture'},decision,inputs(),[],remaining_compute_seconds=0)

    def test_structured_branches_require_distinct_implications_and_render_safely(self):
        bundle=at8_facts(inputs())
        progress={'successful_structure_sha256':[],'unmeasured_count':0}
        catalog={'boltz2_predict':{'availability':'QUALIFIED'}}
        review={'stop_basis':'bounded_inconclusive','reason':'Bounded structural observation.',
                'evidence_fact_ids':['candidate_count'],'next_test':{
                    'tool_id':'boltz2_predict','question':'Does direction persist across seeds?',
                    'hypothesis':'State-associated difference','alternative':'Sampling fluctuation',
                    'held_constant':'Sequences, model and thresholds','changed_factor':'Random seed',
                    'outcomes':[{'observation':'Stable direction','interpretation':'Supports reproducibility',
                                 'next_action':'Test another independent explanation'},
                                {'observation':'Mixed or reversed direction','interpretation':'Weakens reproducibility',
                                 'next_action':'Investigate sampling sensitivity <script>'}],
                    'disposition':'defer','reason':'Engineering report fixture.'}}
        policy={'require_discriminating_outcomes':True}
        checked=validate_review(review,bundle,progress,catalog,policy)
        report=assemble(bundle,deterministic_claims(bundle),'fixture',[],'inconclusive')
        report['research_review']=checked
        html=render_html(report)
        self.assertIn('保持不变',html);self.assertIn('Stable direction',html)
        self.assertIn('&lt;script&gt;',html);self.assertNotIn('<script>',html)
        bad=copy.deepcopy(review)
        bad['next_test']['outcomes'][1]['interpretation']=bad['next_test']['outcomes'][0]['interpretation']
        with self.assertRaisesRegex(ContractError,'distinguish'):validate_review(bad,bundle,progress,catalog,policy)
        bad=copy.deepcopy(review);bad['next_test']['outcomes']=['unstable A','unstable B']
        with self.assertRaises(ContractError):validate_review(bad,bundle,progress,catalog,policy)


if __name__=='__main__':unittest.main()
