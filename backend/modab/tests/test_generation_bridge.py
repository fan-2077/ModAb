import json
from pathlib import Path
import tempfile
import unittest

from dualctx.contracts import canonical_hash, ContractError
from modab.project_store import ProjectStore, UNVERIFIED_EXTERNAL
from modab.generation_bridge import handle


class GenerationBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with ProjectStore(self.root / 'store') as store:
            store.create_project('test_generation', evidence_kind=UNVERIFIED_EXTERNAL)
            commission = store.propose_commission('test_generation', {'fixture': True}, source_ref='test')
            store.confirm_commission('test_generation', commission['sha256'], actor='fixture', source_ref='test')
            plan = store.prepare_plan('test_generation', commission['sha256'],
                [{'key': 'batch', 'tool': 'test', 'tool_version': 'v1', 'object_id': 'test', 'inputs': {'fixture': True}}], max_submissions=1)
            store.approve_plan(plan, actor='fixture', source_ref='test')
            task = store.task(store.materialize_tasks(plan)[0])
        self.manifest = {'project_id': 'test_generation', 'plan_id': plan,
            'task_id': task['task_id'], 'attempt_id': 'attempt_' + canonical_hash({'task_id': task['task_id'], 'attempt_number': 1}),
            'input_sha256': task['input_sha256'], 'job_id': 'fixture_job'}
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'manifest': self.manifest, 'workspace': str(self.root / 'store'),
                                          'remote_controller': '/fixture/job_control.py'}))
        self.payload = json.dumps({'schema': 'modab_plan_registry_request_v1', 'operation': 'query',
                                  'project_id': 'test_generation', 'plan_id': plan}).encode()

    def tearDown(self): self.temp.cleanup()

    def test_repeat_start_submits_only_once(self):
        calls = []
        def transport(config, op, manifest):
            calls.append(op)
            return {'status': 'RUNNING', 'job_id': 'fixture_job', 'candidates': []}
        handle(self.config, 'start', self.payload, transport)
        handle(self.config, 'start', self.payload, transport)
        self.assertEqual(calls, ['start', 'query'])

    def test_unknown_submission_never_resubmits(self):
        calls = []
        def transport(config, op, manifest):
            calls.append(op); return {'status': 'UNKNOWN', 'error': 'connection lost'}
        first = handle(self.config, 'start', self.payload, transport)
        handle(self.config, 'start', self.payload, transport)
        self.assertEqual(calls, ['start', 'query'])
        self.assertEqual(first['journal_status'], 'SUBMISSION_PENDING')

    def test_query_never_starts(self):
        calls = []
        def transport(config, op, manifest):
            calls.append(op); return {'status': 'NOT_RUN'}
        result = handle(self.config, 'query', self.payload, transport)
        self.assertEqual(calls, ['query'])
        self.assertIsNone(result['attempt_id'])
        self.assertEqual(result['evidence_kind'], 'LIVE_SERVER_STATUS_OBSERVATION')

    def test_wrong_ids_rejected_before_transport(self):
        payload = json.loads(self.payload); payload['plan_id'] = 'wrong'
        with self.assertRaises(ContractError):
            handle(self.config, 'start', json.dumps(payload).encode(), lambda *x: self.fail('transport called'))

    def test_terminal_receipt_replay_stable(self):
        snapshot = {'status': 'SUCCEEDED', 'job_id': 'fixture_job', 'candidates': []}
        first = handle(self.config, 'start', self.payload, lambda *x: snapshot)
        second = handle(self.config, 'query', self.payload, lambda *x: snapshot)
        self.assertEqual(first['receipt_sha256'], second['receipt_sha256'])
        self.assertEqual(first['journal_status'], 'SUCCEEDED')

    def test_duplicate_json_field_rejected(self):
        payload = self.payload[:-1] + b',"project_id":"test_generation"}'
        with self.assertRaises(ContractError):
            handle(self.config, 'start', payload, lambda *x: self.fail('transport called'))


if __name__ == '__main__': unittest.main()
