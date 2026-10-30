"""Non-biological registry tests; Node calls real Python/SQLite, not DSH."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dualctx.contracts import ContractError
from modab.project_store import ProjectStore
from modab.registry_bridge import handle
from modab.registry_contracts import REQUEST_SCHEMA, parse_request
from modab.tests.test_project_store import step, fixed_receipt

ROOT = Path(__file__).resolve().parents[2]
NODE = Path(r"C:\Program Files\nodejs\node.exe")


class RegistryBridgeTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "outputs/modab_test_tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        with ProjectStore(self.path) as store:
            store.create_project("fixture_project")
            commission = store.propose_commission(
                "fixture_project", {"request": "Register a fictional document task"},
                source_ref="fixture://registry-request")
            self.commission = commission["sha256"]
            store.confirm_commission("fixture_project", self.commission,
                                     actor="fixture_user", source_ref="fixture://registry-confirm")
            self.plan = store.prepare_plan("fixture_project", self.commission,
                                           [step()], max_submissions=1)
            store.approve_plan(self.plan, actor="fixture_user",
                               source_ref="fixture://registry-approval")

    def payload(self, operation="register", **changes):
        value = {"schema": REQUEST_SCHEMA, "operation": operation,
                 "project_id": "fixture_project", "plan_id": self.plan}
        return json.dumps({**value, **changes}).encode()

    def call(self, operation="register", **changes):
        return handle(self.path, self.payload(operation, **changes))

    def test_registration_replay_and_reopen_keep_same_unrun_record(self):
        first = self.call()
        self.assertEqual(first, self.call())
        self.assertEqual(first, self.call("query"))
        entry = first["entries"][0]
        self.assertEqual(entry["status"], "NOT_RUN")
        self.assertIsNone(entry["attempt_id"])
        with ProjectStore(self.path, readonly=True) as store:
            self.assertEqual(store.db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='TASK_REGISTERED_NOT_RUN'"
            ).fetchone()[0], 1)

    def test_unapproved_plan_cannot_register(self):
        with ProjectStore(self.path) as store:
            plan = store.prepare_plan("fixture_project", self.commission,
                                      [step("second")], max_submissions=1)
        with self.assertRaises(ContractError):
            self.call(plan_id=plan)
        self.assertEqual(self.call("query", plan_id=plan)["entries"], [])

    def test_changed_commission_cannot_register_old_plan(self):
        with ProjectStore(self.path) as store:
            new = store.propose_commission("fixture_project", {"request": "Different goal"},
                                           source_ref="fixture://registry-new-goal")
            store.confirm_commission("fixture_project", new["sha256"],
                                     actor="fixture_user", source_ref="fixture://registry-new-confirm")
        with self.assertRaises(ContractError):
            self.call()
        self.assertEqual(self.call("query")["entries"], [])

    def test_stop_blocks_registration_but_preserves_reading(self):
        first = self.call()
        with ProjectStore(self.path) as store:
            store.record_user_decision("fixture_project", subject=self.plan, action="STOP",
                                       actor="fixture_user", source_ref="fixture://registry-stop",
                                       detail="Stop the fictional workflow")
        with self.assertRaises(ContractError):
            self.call()
        self.assertEqual(first, self.call("query"))

    def test_wrong_project_cannot_register_or_read_plan(self):
        for operation in ("register", "query"):
            with self.subTest(operation=operation):
                with self.assertRaises(ContractError):
                    self.call(operation, project_id="other_project")
        self.assertEqual(self.call("query")["entries"], [])

    def test_query_does_not_create_or_change_records(self):
        before = sha256((self.path / "modab.sqlite3").read_bytes()).hexdigest()
        self.assertEqual(self.call("query")["entries"], [])
        after = sha256((self.path / "modab.sqlite3").read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_missing_store_is_not_created(self):
        missing = self.path / "missing"
        for operation in ("register", "query"):
            with self.assertRaises(ContractError):
                handle(missing, self.payload(operation))
        self.assertFalse(missing.exists())

    def test_invalid_fields_duplicates_encoding_and_limits_are_rejected(self):
        invalid = [
            self.payload(approved=True), self.payload(project_id=7),
            self.payload(operation="submit"), self.payload(schema="unknown"),
            b'{"schema":"x","schema":"y"}', b"\xff", b"x" * 4097,
            self.payload().replace(b'"register"', b'NaN'),
        ]
        for payload in invalid:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(ContractError):
                    parse_request(payload)

    def test_failed_record_keeps_error_and_receipt_identity(self):
        task_id = self.call()["entries"][0]["legacy_task_id"]
        with ProjectStore(self.path) as store:
            task = store.claim_handoff(task_id)
            store.record_started(task_id, task["attempt_id"],
                                 external_ref="fixture://registry-failed")
            receipt = fixed_receipt(store.task(task_id), status="FAILED")
            digest = store.record_receipt(task_id, receipt)
        entry = self.call("query")["entries"][0]
        self.assertEqual(entry["status"], "FAILED")
        self.assertEqual(entry["error"], "FIXTURE_FORMAT_CHECK_FAILED")
        self.assertEqual(entry["receipt_sha256"], digest)
        self.assertEqual(entry["external_ref"], "fixture://registry-failed")

    def test_concurrent_registration_creates_one_record(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.call(), range(2)))
        self.assertEqual(replies[0], replies[1])
        with ProjectStore(self.path, readonly=True) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_native_plugin_process_bridge_roundtrip(self):
        result = subprocess.run(
            [str(NODE), str(ROOT.parent / "dsh/tests/registry_roundtrip.mjs"),
             sys.executable, str(self.path), "fixture_project", self.plan],
            text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        print("\n" + result.stdout, end="", flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        with ProjectStore(self.path, readonly=True) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
