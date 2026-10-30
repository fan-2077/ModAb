"""Authority and fact-grounding regression cases for the durable task service."""
import copy
from pathlib import Path
import tempfile
import unittest

from dualctx.contracts import ContractError
from modab.autonomy.contracts import SCHEMA, digest
from modab.autonomy.store import ResearchStore
from modab.autonomy.reports import at8_facts, deterministic_claims, validate_claims, verify_facts
from modab.autonomy.adapters import validate_boltz_input

ROOT = Path(__file__).resolve().parents[3]
SCI = ROOT / 'science/at8_prepare_v1/evaluation_v1/results'


def inputs():
    return {key: {'path': str(SCI / file), 'sha256': digest(SCI / file), 'kind': kind}
        for key, file, kind in (
            ('measurements', 'run-receipt.json', 'at8_measurements'),
            ('reference', 'prepared_AT8_reference.json', 'at8_reference_measurements'),
            ('geometry', 'interpretation.json', 'at8_geometry'))}


def commission():
    return {'schema': SCHEMA, 'request_id': 'fixture_request', 'project_id': 'autonomy_fixture',
        'objective': 'Engineering checks of existing evidence; no prediction', 'inputs': inputs(),
        'allowed_tools': ['reuse_evidence', 'grounded_report'], 'allowed_resources': ['local_cpu'],
        'budget': {'model_requests': 0, 'model_tokens': 0, 'compute_seconds': 60, 'job_attempts': 3, 'max_concurrent_jobs': 1},
        'planner': {'kind': 'test_fixture', 'config_id': 'fixture_plan'},
        'stop_conditions': ['Finish fixture evidence flow'],
        'data_policy': {'public_inputs_only': True, 'allowed_endpoints': []},
        'evidence_kind': 'ENGINEERING_FIXTURE_ONLY'}


class AutonomyFactsTests(unittest.TestCase):
    def setUp(self):
        self.bundle = at8_facts(inputs())

    def test_counts_differences_and_fractions_are_distinct(self):
        facts = verify_facts(self.bundle)
        self.assertEqual([facts[f'c{i}_phosphate_delta']['value'] for i in range(1, 5)], [-1, 2, 1, 2])
        self.assertEqual(facts['c1_fraction']['value'], 8 / 21)
        self.assertEqual(facts['c1_phosphate']['unit'], 'count')
        self.assertEqual(facts['c1_fraction']['unit'], 'fraction')
        self.assertEqual(facts['c4_near']['value'], 1)
        self.assertEqual(facts['candidate_count']['value'], 4)

    def test_changed_metric_unit_or_value_is_rejected(self):
        claim = next(x for x in deterministic_claims(self.bundle) if x['id'] == 'claim_c1_phosphate')
        for key, bad in [('unit', 'fraction'), ('value', 11), ('metric', 'binding_affinity')]:
            changed = copy.deepcopy(claim)
            changed[key] = bad
            self.assertEqual(validate_claims(self.bundle, [changed])['status'], 'REJECTED')

    def test_free_prose_cannot_become_verified_fact(self):
        claim = deterministic_claims(self.bundle)[0]
        claim['text'] = 'This proves strong binding.'
        self.assertEqual(validate_claims(self.bundle, [claim])['status'], 'REJECTED')
        hypothesis = {'id': 'h1', 'kind': 'hypothesis', 'fact_ids': ['c1_phosphate'],
            'subject': None, 'metric': None, 'value': None, 'unit': None, 'text': 'A local contact may warrant investigation.'}
        result = validate_claims(self.bundle, [hypothesis])
        self.assertEqual(result['accepted'], [])
        self.assertEqual(result['unverified'][0]['status'], 'UNVERIFIED_INTERPRETATION')

    def test_tampered_bundle_and_lost_denominator_rejected(self):
        changed = copy.deepcopy(self.bundle)
        changed['facts'][0]['value'] = []
        with self.assertRaisesRegex(ContractError, 'identity'):
            verify_facts(changed)
        with self.assertRaisesRegex(ContractError, 'denominator'):
            validate_claims(self.bundle, [], required_candidates=['candidate-01'])

    def test_denominator_cannot_be_rewritten_even_with_new_bundle_digest(self):
        from dualctx.contracts import canonical_hash
        changed = copy.deepcopy(self.bundle)
        changed['candidate_ids'].pop()
        changed['bundle_sha256'] = canonical_hash({k:v for k,v in changed.items() if k!='bundle_sha256'})
        with self.assertRaisesRegex(ContractError,'denominator'):
            verify_facts(changed)

    def test_unexecuted_prediction_cannot_be_declared_a_verified_fact(self):
        claim = {'id':'fake_execution','kind':'fact','fact_ids':['prediction_completed'],
            'subject':'reference','metric':'prediction_status','value':'SUCCEEDED','unit':'status','text':''}
        self.assertEqual(validate_claims(self.bundle,[claim])['status'],'REJECTED')


class AutonomyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = ResearchStore(self.path, migrate=True)
        self.addCleanup(lambda: self.store.close())

    def commissioned(self):
        mission = self.store.create(commission(), {'kind': 'unit_test'})
        return self.store.approve(mission['mission_id'], mission['commission_sha256'], 'fixture_user', 'fixture://task-grant')

    def test_explicit_migration_preserves_project_store(self):
        self.assertTrue((self.path / 'before-autonomy-v1.sqlite3').exists())
        self.assertEqual(self.store.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], 'modab_project_store_v1')
        with ResearchStore(self.path) as recovered:
            self.assertEqual(recovered.missions(), [])

    def test_approval_binds_exact_document_and_is_idempotent(self):
        mission = self.store.create(commission(), {'kind': 'unit_test'})
        with self.assertRaisesRegex(ContractError, 'changed'):
            self.store.approve(mission['mission_id'], '0' * 64, 'fixture_user', 'fixture://grant')
        ready = self.commissioned()
        self.assertEqual(ready['status'], 'QUEUED')
        self.assertEqual(len(self.store.missions()), 1)

    def test_unapproved_and_over_budget_calls_are_blocked(self):
        m = self.store.create(commission(), {'kind': 'unit_test'})
        with self.assertRaisesRegex(ContractError, 'permitted'):
            self.store.begin_decision(m['mission_id'], {}, {'kind': 'test_fixture'}, {})
        m = self.commissioned()
        with self.assertRaisesRegex(ContractError, 'BUDGET_EXHAUSTED:model_requests'):
            self.store.begin_decision(m['mission_id'], {}, {'kind': 'model'}, {'model_requests': 1})

    def test_step_claim_and_accounting_survive_reopen(self):
        m = self.commissioned()
        did = self.store.begin_decision(m['mission_id'], {}, {'kind': 'test_fixture'}, {})
        self.store.finish_decision(did, 'ACCEPTED', {'fixture': True})
        spec = {'tool_id': 'reuse_evidence', 'resource_id': 'local_cpu', 'cost_reservation': {'compute_seconds': 30, 'job_attempts': 1}}
        step = self.store.create_step(m['mission_id'], did, spec)
        self.assertEqual(self.store.create_step(m['mission_id'], did, spec)['job_id'], step['job_id'])
        self.store.settle(m['mission_id'], step['step_id'], {'compute_seconds': 2, 'job_attempts': 1})
        with ResearchStore(self.path) as recovered:
            self.assertEqual(recovered.usage(m['mission_id'])['compute_seconds'], 2)
            self.assertEqual(len(recovered.steps(m['mission_id'])), 1)

    def test_unlimited_requests_still_account_and_enforce_tokens(self):
        c = commission()
        c['budget'].update(model_requests=None, model_tokens=12)
        m = self.store.create(c, {'kind': 'unit_test'})
        self.store.approve(m['mission_id'], m['commission_sha256'], 'fixture_user', 'fixture://unlimited')
        first = self.store.begin_decision(m['mission_id'], {}, {'kind': 'test_fixture'}, {'model_requests': 100, 'model_tokens': 10})
        self.store.settle(m['mission_id'], first, {'model_requests': 100, 'model_tokens': 9})
        self.store.begin_decision(m['mission_id'], {}, {'kind': 'test_fixture'}, {'model_requests': 1, 'model_tokens': 3})
        self.assertEqual(self.store.usage(m['mission_id']), {'model_requests': 101, 'model_tokens': 12})
        with self.assertRaisesRegex(ContractError, 'BUDGET_EXHAUSTED:model_tokens'):
            self.store.begin_decision(m['mission_id'], {}, {'kind': 'test_fixture'}, {'model_requests': 1, 'model_tokens': 1})
        self.assertEqual(self.store.usage(m['mission_id'])['model_requests'], 101)
        from modab.autonomy.research_review import observation_summary
        summary = observation_summary({}, [], c['budget'], self.store.usage(m['mission_id']))
        self.assertIsNone(summary['remaining_budget']['model_requests'])
        self.assertEqual(summary['remaining_budget']['model_tokens'], 0)

    def test_only_request_limit_can_be_unlimited(self):
        from modab.autonomy.contracts import validate_mission
        for field in ('model_tokens', 'compute_seconds', 'job_attempts', 'max_concurrent_jobs'):
            c = commission(); c['budget'][field] = None
            with self.assertRaisesRegex(ContractError, 'Invalid ' + field):
                validate_mission(c)

    def test_pause_and_cancel_block_new_submissions(self):
        m = self.commissioned()
        self.store.control(m['mission_id'], 'pause', {'kind': 'fixture_user'})
        with self.assertRaises(ContractError):
            self.store.begin_decision(m['mission_id'], {}, {'kind': 'test_fixture'}, {})
        self.store.control(m['mission_id'], 'resume', {'kind': 'fixture_user'})
        self.store.control(m['mission_id'], 'cancel', {'kind': 'fixture_user'})
        with self.assertRaises(ContractError):
            self.store.control(m['mission_id'], 'resume', {'kind': 'fixture_user'})


class AutonomyInputTests(unittest.TestCase):
    def test_modified_residue_must_match_parent_and_numbering(self):
        value = {'version': 1, 'sequences': [{'protein': {'id': 'A', 'sequence': 'ASTY', 'msa': 'empty',
            'modifications': [{'position': 2, 'ccd': 'SEP'}]}}]}
        validate_boltz_input(value, 'single_sequence')
        value['sequences'][0]['protein']['modifications'][0]['position'] = 3
        with self.assertRaisesRegex(ContractError, 'parent residue'):
            validate_boltz_input(value, 'single_sequence')


if __name__ == '__main__':
    unittest.main()
