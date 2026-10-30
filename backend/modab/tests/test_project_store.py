"""Non-biological fixed receipts only. No model, tool, network, or GPU calls."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dualctx.contracts import ContractError, canonical_hash
from modab.project_store import FIXTURE, ProjectStore


ROOT = Path(__file__).resolve().parents[2]


def step(key="format", *, depends_on=None):
    return {"key": key, "tool": "fixture_document_tool", "tool_version": "v1",
            "object_id": "fictional_document_001", "inputs": {"fixture_label": "sample", "format": "table"},
            "depends_on": depends_on or []}


def fixed_receipt(task, *, status="SUCCEEDED"):
    """A recorded fixture, not output from an actual executor."""
    doc = task["document"]
    return {"task_id": task["task_id"], "attempt_id": task["attempt_id"],
            "external_ref": task["external_ref"], "input_sha256": task["input_sha256"],
            **{k: doc[k] for k in ("tool", "tool_version", "object_id")},
            "status": status, "output": {"fixture_cells": 3} if status == "SUCCEEDED" else None,
            "error": None if status == "SUCCEEDED" else "FIXTURE_FORMAT_CHECK_FAILED",
            "source_ref": "fixture://fixed-receipt-v1", "evidence_kind": FIXTURE}


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "outputs/modab_test_tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = ProjectStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.create_project("fixture_project")
        self.commission = self.store.propose_commission("fixture_project", {
            "request": "Produce a table for a fictional document", "requirements": {"format": "table"}},
            source_ref="fixture://user-request-v1")
        self.store.confirm_commission("fixture_project", self.commission["sha256"],
                                      actor="fixture_user", source_ref="fixture://confirmation-v1")

    def plan(self, steps=None, budget=3, approve=True):
        plan = self.store.prepare_plan("fixture_project", self.commission["sha256"],
                                       steps or [step()], max_submissions=budget)
        if approve:
            self.store.approve_plan(plan, actor="fixture_user", source_ref="fixture://plan-approval")
        return plan

    def started(self, task_id):
        package = self.store.claim_handoff(task_id)
        self.assertIsNotNone(package)
        self.store.record_started(task_id, package["attempt_id"], external_ref="fixture://executor/" + task_id)
        return self.store.task(task_id)

    def test_commission_proposal_does_not_change_confirmed_goal_and_replay_cannot_revert(self):
        new = self.store.propose_commission("fixture_project", {"request": "Produce a fictional index"},
                                            source_ref="fixture://user-request-v2")
        self.assertEqual(new["version"], 2)
        self.assertEqual(self.store.delivery_index("fixture_project")["project"]["confirmed_commission"], self.commission["sha256"])
        with self.assertRaises(ContractError):
            self.store.confirm_commission("fixture_project", "0" * 64, actor="fixture_user", source_ref="fixture://bad")
        self.store.confirm_commission("fixture_project", new["sha256"], actor="fixture_user", source_ref="fixture://confirmation-v2")
        self.store.confirm_commission("fixture_project", self.commission["sha256"], actor="fixture_user", source_ref="fixture://confirmation-v1")
        self.assertEqual(self.store.delivery_index("fixture_project")["project"]["confirmed_commission"], new["sha256"])

    def test_approval_and_materialization_are_distinct_from_actual_run(self):
        plan = self.plan(approve=False)
        with self.assertRaises(ContractError): self.store.materialize_tasks(plan)
        self.store.approve_plan(plan, actor="fixture_user", source_ref="fixture://plan-approval")
        self.assertEqual(self.store.delivery_index("fixture_project")["task_count"], 0)
        ids = self.store.materialize_tasks(plan)
        self.assertEqual(self.store.materialize_tasks(plan), ids)
        index = self.store.delivery_index("fixture_project")
        self.assertEqual(index["status_counts"], {"NOT_RUN": 1})
        self.assertIsNone(index["tasks"][0]["attempt_id"])

    def test_confirmed_goal_change_blocks_old_plan_but_preserves_its_records(self):
        old_plan = self.plan()
        task_id = self.store.materialize_tasks(old_plan)[0]
        new = self.store.propose_commission("fixture_project", {"request": "New fictional objective"}, source_ref="fixture://v2")
        self.store.confirm_commission("fixture_project", new["sha256"], actor="fixture_user", source_ref="fixture://approve-v2")
        self.assertIsNone(self.store.claim_handoff(task_id))
        with self.assertRaises(ContractError): self.store.approve_plan(old_plan, actor="fixture_user", source_ref="fixture://stale")
        item = self.store.delivery_index("fixture_project")["tasks"][0]
        self.assertEqual(item["status"], "NOT_RUN")
        self.assertEqual(item["blocked_reason"], "COMMISSION_CHANGED_REQUIRES_NEW_APPROVED_PLAN")

    def test_approved_input_is_snapshotted_and_changed_input_needs_new_plan_approval(self):
        steps = [step()]
        plan = self.plan(steps)
        steps[0]["inputs"]["format"] = "altered"
        task_id = self.store.materialize_tasks(plan)[0]
        self.assertEqual(self.store.task(task_id)["document"]["inputs"]["format"], "table")
        changed = self.store.prepare_plan("fixture_project", self.commission["sha256"], steps, max_submissions=3)
        self.assertNotEqual(changed, plan)
        with self.assertRaises(ContractError): self.store.materialize_tasks(changed)

    def test_returning_to_old_commission_requires_fresh_plan_approval_even_after_restart(self):
        plan = self.plan()
        task_id = self.store.materialize_tasks(plan)[0]
        old_approval = self.store.approve_plan(plan, actor="fixture_user", source_ref="fixture://plan-approval")
        new = self.store.propose_commission("fixture_project", {"request": "Another fictional objective"},
                                            source_ref="fixture://changed-request")
        self.store.confirm_commission("fixture_project", new["sha256"], actor="fixture_user",
                                      source_ref="fixture://changed-confirmation")
        self.store.confirm_commission("fixture_project", self.commission["sha256"], actor="fixture_user",
                                      source_ref="fixture://explicit-return-confirmation")
        self.store.close()
        self.store = ProjectStore(self.path)
        self.assertIsNone(self.store.claim_handoff(task_id))
        index = self.store.delivery_index("fixture_project")
        self.assertEqual(index["tasks"][0]["blocked_reason"], "PLAN_REQUIRES_NEW_USER_APPROVAL")
        self.assertIn(old_approval, {d["decision_id"] for d in index["decisions"]})
        with self.assertRaises(ContractError): self.store.materialize_tasks(plan)
        with self.assertRaises(ContractError):
            self.store.approve_plan(plan, actor="fixture_user", source_ref="fixture://plan-approval")
        self.assertIsNone(self.store.claim_handoff(task_id))
        new_approval = self.store.approve_plan(plan, actor="fixture_user", source_ref="fixture://fresh-plan-approval")
        self.assertNotEqual(old_approval, new_approval)
        self.assertEqual(self.store.materialize_tasks(plan), [task_id])
        self.assertIsNotNone(self.store.claim_handoff(task_id))

    def test_no_duplicate_handoff_on_restart_and_uncertainty_is_explicit(self):
        task_id = self.store.materialize_tasks(self.plan())[0]
        package = self.store.claim_handoff(task_id)
        self.store.close()
        self.store = ProjectStore(self.path)
        self.assertIsNone(self.store.claim_handoff(task_id))
        items = self.store.recovery_items("fixture_project")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["attempt_id"], package["attempt_id"])
        self.assertEqual(items[0]["recovery_action"], "LOOKUP_ONLY_DO_NOT_RESUBMIT")
        self.store.record_started(task_id, package["attempt_id"], external_ref="fixture://reconciled-existing-job")
        self.store.close()
        self.store = ProjectStore(self.path)
        self.assertIsNone(self.store.claim_handoff(task_id))
        self.assertEqual(self.store.recovery_items("fixture_project")[0]["status"], "RUNNING")

    def test_two_clients_competing_for_one_task_get_one_handoff(self):
        task_id = self.store.materialize_tasks(self.plan())[0]
        def claim():
            with ProjectStore(self.path) as client: return client.claim_handoff(task_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            packages = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(x is not None for x in packages), 1)

    def test_submission_limit_counts_unconfirmed_handoffs_and_survives_restart(self):
        ids = self.store.materialize_tasks(self.plan([step("first"), step("second")], budget=1))
        self.assertIsNotNone(self.store.claim_handoff(ids[0]))
        with ProjectStore(self.path) as other: self.assertIsNone(other.claim_handoff(ids[1]))
        item = next(t for t in self.store.delivery_index("fixture_project")["tasks"] if t["task_id"] == ids[1])
        self.assertEqual(item["blocked_reason"], "SUBMISSION_LIMIT_REACHED")
        self.assertEqual(item["status"], "NOT_RUN")

    def test_receipt_rejects_every_identity_mismatch_without_changing_state(self):
        task_id = self.store.materialize_tasks(self.plan())[0]
        original = fixed_receipt(self.started(task_id))
        for field in ("task_id", "attempt_id", "external_ref", "tool", "tool_version", "object_id", "input_sha256", "evidence_kind"):
            with self.subTest(field=field):
                bad = {**original, field: "wrong"}
                with self.assertRaises(ContractError): self.store.record_receipt(task_id, bad)
                self.assertEqual(self.store.task(task_id)["status"], "RUNNING")
        self.assertIsNone(self.store.task(task_id)["receipt"])

    def test_terminal_receipt_is_idempotent_and_conflicts_do_not_replace_it(self):
        task_id = self.store.materialize_tasks(self.plan())[0]
        receipt = fixed_receipt(self.started(task_id))
        events_before = len(self.store.delivery_index("fixture_project")["events"])
        # A failed audit append must roll back the task update in the same transaction.
        with patch.object(self.store, "_event", side_effect=RuntimeError("fixture audit failure")):
            with self.assertRaises(RuntimeError): self.store.record_receipt(task_id, receipt)
        self.assertEqual(self.store.task(task_id)["status"], "RUNNING")
        self.assertIsNone(self.store.task(task_id)["receipt_sha256"])
        self.assertEqual(len(self.store.delivery_index("fixture_project")["events"]), events_before)
        digest = self.store.record_receipt(task_id, receipt)
        self.assertEqual(self.store.record_receipt(task_id, receipt), digest)
        self.assertEqual(len(self.store.delivery_index("fixture_project")["events"]), events_before + 1)
        bad = deepcopy(receipt)
        bad["output"]["fixture_cells"] = 99
        with self.assertRaises(ContractError): self.store.record_receipt(task_id, bad)
        self.assertEqual(self.store.task(task_id)["receipt_sha256"], digest)
        self.assertIsNone(self.store.claim_handoff(task_id))
        self.assertEqual(self.store.recovery_items("fixture_project"), [])

    def test_external_job_cannot_be_assigned_to_two_tasks(self):
        ids = self.store.materialize_tasks(self.plan([step("first"), step("second")]))
        first, second = [self.store.claim_handoff(t) for t in ids]
        self.store.record_started(ids[0], first["attempt_id"], external_ref="fixture://shared-job")
        with self.assertRaises(ContractError):
            self.store.record_started(ids[1], second["attempt_id"], external_ref="fixture://shared-job")
        self.assertEqual(self.store.task(ids[1])["status"], "SUBMISSION_PENDING")

    def test_failed_dependency_remains_not_run_and_delivery_includes_all_denominators(self):
        ids = self.store.materialize_tasks(self.plan([step("first"), step("second", depends_on=["first"])]))
        self.assertIsNone(self.store.claim_handoff(ids[1]))
        receipt = fixed_receipt(self.started(ids[0]), status="FAILED")
        self.store.record_receipt(ids[0], receipt)
        self.assertIsNone(self.store.claim_handoff(ids[1]))
        index = self.store.delivery_index("fixture_project")
        self.assertEqual(index["task_count"], 2)
        self.assertEqual(index["status_counts"], {"FAILED": 1, "NOT_RUN": 1})
        self.assertEqual(next(x for x in index["tasks"] if x["task_id"] == ids[1])["blocked_reason"], "DEPENDENCY_NOT_SUCCEEDED")
        exported = self.store.export_delivery("fixture_project", self.path / "delivery")
        self.assertEqual(json.loads(exported.read_text(encoding="utf-8")), index)
        self.assertEqual(self.store.export_delivery("fixture_project", self.path / "delivery"), exported)
        self.assertEqual(index["index_sha256"], canonical_hash({k: v for k, v in index.items() if k != "index_sha256"}))

    def test_user_stop_persists_and_accepts_late_receipt_without_new_submission(self):
        plan = self.plan([step("first"), step("second")])
        ids = self.store.materialize_tasks(plan)
        receipt = fixed_receipt(self.started(ids[0]))
        self.store.record_user_decision("fixture_project", subject=plan, action="STOP", actor="fixture_user",
                                         source_ref="fixture://stop", detail="Fixture stop requested")
        with ProjectStore(self.path) as other:
            self.assertIsNone(other.claim_handoff(ids[1]))
            other.record_receipt(ids[0], receipt)
            self.assertEqual(other.task(ids[0])["status"], "SUCCEEDED")
            other.record_user_decision("fixture_project", subject=plan, action="CONTINUE", actor="fixture_user",
                                       source_ref="fixture://continue-note", detail="Record only; no restart authority")
            self.assertIsNone(other.claim_handoff(ids[1]))

    def test_malformed_plan_or_receipt_does_not_become_success(self):
        for steps, budget in [([step(), step()], 2), ([step(depends_on=["missing"])], 2), ([step()], True)]:
            with self.assertRaises(ContractError): self.plan(steps, budget)
        task_id = self.store.materialize_tasks(self.plan())[0]
        receipt = fixed_receipt(self.started(task_id))
        for bad in ({**receipt, "output": None}, {**receipt, "status": "FAILED", "error": None},
                    {**receipt, "extra": "unrecognized"}):
            with self.assertRaises(ContractError): self.store.record_receipt(task_id, bad)
        with self.assertRaises(ValueError):
            self.store.record_receipt(task_id, {**receipt, "output": {"value": float("nan")}})
        self.assertEqual(self.store.task(task_id)["status"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
