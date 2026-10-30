"""Constraint, provenance, recovery and native-process checks for research plans."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dualctx.contracts import ContractError
from modab import research_bridge as bridge
from modab.project_store import ProjectStore

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / 'runtime/research-v1/config.json'
ORIGIN = {'session_id': 'engineering_test', 'tool_call_id': 'test_call'}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = bridge.read(CONFIG)
        self.config['workspace'] = str(Path(self.tmp.name) / 'store')
        for key, source in self.config['evidence'].items():
            target = Path(self.tmp.name) / (key + '.json')
            target.write_bytes(Path(source['path']).read_bytes())
            source['path'] = str(target)

    def proposal(self, mode='full_evaluation', method='boltz2'):
        ctx = bridge.context(self.config, {'objective': 'Evaluate all candidates', 'mode': mode}, ORIGIN)
        selected = method if mode == 'full_evaluation' else 'existing_evidence'
        steps = [{'action': 'reuse_evidence', 'method_id': 'existing_evidence', 'purpose': 'Retain all candidates'}]
        if mode == 'full_evaluation':
            steps += [{'action': 'prepare_requirements', 'method_id': selected, 'purpose': 'Identify runtime gaps'},
                      {'action': 'predict_structures', 'method_id': selected, 'purpose': 'Later paired-state evaluation'}]
        return ctx, {'selected_method': selected, 'rationale': 'Engineering fixture rationale',
            'alternatives': [{'method_id': 'alphafold3' if selected == 'boltz2' else 'boltz2', 'reason': 'Other predictor with distinct setup requirements'}],
            'evidence_ids': ['measurements', 'geometry', 'control_sources'], 'steps': steps,
            'unknowns': ['No independent paired-state result']}

    def plan(self, mode='full_evaluation', method='boltz2'):
        ctx, proposal = self.proposal(mode, method)
        return bridge.propose(self.config, {'context_id': ctx['context_id'], 'proposal': proposal}, ORIGIN)

    def test_full_run_preserves_four_and_does_not_infer(self):
        plan = self.plan()
        result = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        self.assertEqual([x['status'] for x in result['outputs']], ['COMPLETED_LOCAL', 'COMPLETED_LOCAL', 'NOT_RUN'])
        self.assertEqual(result['outputs'][0]['candidate_count'], 4)
        self.assertEqual(result['new_scientific_jobs'], 0)
        self.assertFalse(result['scientific_goal_complete'])
        with ProjectStore(Path(self.config['workspace']), readonly=True) as store:
            self.assertIsNone(store.db.execute('SELECT approval_id FROM plans').fetchone()[0])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)

    def test_alternative_predictor_is_not_hardcoded(self):
        plan = self.plan(method='alphafold3')
        result = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        self.assertEqual(result['outputs'][1]['selected_method'], 'alphafold3')
        self.assertIn('权重', result['outputs'][1]['required_before_execution'][0])

    def test_existing_only_blocks_new_work(self):
        ctx, proposal = self.proposal('existing_only')
        for change in ('selected_method', 'steps'):
            edited = copy.deepcopy(proposal)
            if change == 'selected_method':
                edited['selected_method'] = 'boltz2'
            else:
                edited['steps'].append({'action': 'prepare_requirements', 'method_id': 'boltz2', 'purpose': 'Disallowed'})
            with self.assertRaises(ContractError):
                bridge.propose(self.config, {'context_id': ctx['context_id'], 'proposal': edited}, ORIGIN)
        plan = bridge.propose(self.config, {'context_id': ctx['context_id'], 'proposal': proposal}, ORIGIN)
        result = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        self.assertEqual(result['status'], 'EXISTING_EVIDENCE_REUSED')
        self.assertEqual(len(result['outputs']), 1)

    def test_false_evidence_or_method_is_rejected(self):
        ctx, proposal = self.proposal()
        for field, value in [('evidence_ids', ['made_up']), ('selected_method', 'magic_predictor')]:
            edited = copy.deepcopy(proposal)
            edited[field] = value
            with self.assertRaises(ContractError):
                bridge.propose(self.config, {'context_id': ctx['context_id'], 'proposal': edited}, ORIGIN)

    def test_nested_metric_name_is_not_an_evidence_reference(self):
        self.config['evidence_contract_version'] = 1
        ctx, proposal = self.proposal()
        self.assertEqual(ctx['plan_contract']['allowed_evidence_ids'], list(ctx['evidence']))
        proposal['evidence_ids'].append('short_contact_summary')
        with self.assertRaisesRegex(ContractError, 'Unknown evidence reference'):
            bridge.propose(self.config, {'context_id': ctx['context_id'], 'proposal': proposal}, ORIGIN)
        proposal['evidence_ids'].remove('short_contact_summary')
        plan = bridge.propose(self.config, {'context_id': ctx['context_id'], 'proposal': proposal}, ORIGIN)
        self.assertEqual(bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)['status'], 'LOCAL_PREPARATION_COMPLETE')

    def test_reference_metric_comparisons_do_not_merge_total_and_phosphate(self):
        reference = ROOT / 'science/at8_prepare_v1/evaluation_v1/results/prepared_AT8_reference.json'
        self.config['evidence']['reference_measurements'] = {'path': str(reference), 'sha256': bridge.file_hash(reference)}
        plan = self.plan('existing_only')
        result = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        comparison = result['outputs'][0]['contact_comparison']
        self.assertEqual(comparison['reference']['total_contacts'], 26)
        self.assertEqual(comparison['reference']['phosphate_contacts'], 9)
        self.assertEqual([x['total_minus_reference'] for x in comparison['comparisons']], [-5, -3, -4, -3])
        self.assertEqual([x['phosphate_minus_reference'] for x in comparison['comparisons']], [-1, 2, 1, 2])

    def test_interchain_scope_retains_positive_candidate_in_denominator(self):
        reference = ROOT / 'science/at8_prepare_v1/evaluation_v1/results/prepared_AT8_reference.json'
        self.config['evidence']['reference_measurements'] = {'path': str(reference), 'sha256': bridge.file_hash(reference)}
        self.config['context_version'] = 'explicit_metric_scope_v2'
        plan = self.plan('existing_only')
        result = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        summary = result['outputs'][0]['short_contact_summary']
        self.assertEqual(list(summary['counts_by_candidate'].values()), [0, 0, 0, 1])
        self.assertEqual(summary['candidate_04_pair']['chains'], ['A', 'C'])
        self.assertTrue(summary['candidate_04_pair']['is_interchain'])
        self.assertFalse(summary['all_candidates_have_zero_pairs'])
        self.assertNotIn('candidate-04', summary['no_pair_detected_candidates'])

    def test_selected_method_is_not_promoted_to_executable_or_unique(self):
        self.config['decision_facts_version'] = 1
        plan = self.plan()
        result = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        facts = result['method_decision_facts']
        self.assertEqual(facts['selected_availability'], 'NOT_INTEGRATED')
        self.assertEqual(facts['documented_prediction_candidate_count'], 2)
        self.assertEqual(facts['currently_executable_predictors'], [])
        self.assertEqual(facts['alternative_roles'][0]['id'], 'alphafold3')

    def test_evidence_change_invalidates_context(self):
        ctx, proposal = self.proposal()
        Path(self.config['evidence']['geometry']['path']).write_text('{}')
        with self.assertRaisesRegex(ContractError, 'Evidence changed'):
            bridge.propose(self.config, {'context_id': ctx['context_id'], 'proposal': proposal}, ORIGIN)

    def test_plan_tampering_is_rejected(self):
        plan = self.plan()
        target = Path(self.config['workspace']) / 'plans' / (plan['plan_id'] + '.json')
        altered = bridge.read(target)
        altered['proposal']['rationale'] = 'Tampered explanation'
        target.write_text(json.dumps(altered))
        with self.assertRaisesRegex(ContractError, 'altered'):
            bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)

    def test_receipt_recovery_and_idempotency(self):
        plan = self.plan()
        first = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        second = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        self.assertFalse(first['reused'])
        self.assertTrue(second['reused'])
        self.assertEqual(first['receipt_sha256'], second['receipt_sha256'])
        recovered = bridge.query(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        self.assertEqual(recovered['result']['receipt_sha256'], first['receipt_sha256'])

    def test_tampered_receipt_is_rejected(self):
        plan = self.plan()
        result = bridge.execute_available(self.config, {'plan_id': plan['plan_id']}, ORIGIN)
        target = Path(result['artifact'])
        changed = bridge.read(target)
        changed['scientific_goal_complete'] = True
        target.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ContractError, 'identity mismatch'):
            bridge.query(self.config, {'plan_id': plan['plan_id']}, ORIGIN)

    def test_duplicate_keys_and_path_injection(self):
        with self.assertRaises(ContractError):
            bridge.parse_payload(b'{"mode":1,"mode":2}')
        with self.assertRaises(ContractError):
            bridge.load_context(self.config, '../../elsewhere')

    def test_real_isolated_python_process(self):
        config = Path(self.tmp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        payload = {'arguments': {'objective': 'Reuse existing results', 'mode': 'existing_only'}, 'origin': ORIGIN}
        done = subprocess.run([sys.executable, '-B', '-I', '-S', '-X', 'utf8', bridge.__file__,
            '--config', str(config), '--operation', 'context'], input=json.dumps(payload),
            capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout)['mode'], 'existing_only')


if __name__ == '__main__':
    unittest.main()
