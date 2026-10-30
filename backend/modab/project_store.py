"""Small local project journal, separate from a scientific execution controller.

User confirmations enter through a trusted local caller; this is NOT an identity
provider. A durable handoff marker prevents this client from handing off twice.
An uncertain external submission must be looked up, never automatically retried.
No function here executes a task or evaluates biological evidence.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from dualctx.contracts import ContractError, canonical_hash, require_digest, require_id


FIXTURE = "ENGINEERING_FIXTURE_ONLY"
UNVERIFIED_EXTERNAL = "EXTERNAL_RECORD_UNVERIFIED"
SCHEMA_VERSION = "modab_project_store_v1"


def _json(value: Any) -> str:
    # Reuse the existing canonical identity convention; reject NaN/non-JSON data.
    canonical_hash(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be an explicit nonempty string")


class ProjectStore:
    """SQLite-backed local handoff records, not a worker scheduler or release gate."""

    def __init__(self, workspace: Path, *, readonly: bool = False):
        self.workspace = Path(workspace).resolve()
        self.readonly = readonly
        if readonly:
            self.db = sqlite3.connect((self.workspace / "modab.sqlite3").as_uri() + "?mode=ro",
                                      uri=True, timeout=10, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            try:
                version = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if version is None or version[0] != SCHEMA_VERSION:
                    raise ContractError("Unsupported project store schema")
            except BaseException:
                self.db.close()
                raise
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.workspace / "modab.sqlite3", timeout=10,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY, evidence_kind TEXT NOT NULL,
                confirmed_commission TEXT, stop_reason TEXT);
            CREATE TABLE IF NOT EXISTS commissions (
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                version INTEGER NOT NULL, sha256 TEXT NOT NULL, document TEXT NOT NULL,
                source_ref TEXT NOT NULL, PRIMARY KEY(project_id, version),
                UNIQUE(project_id, sha256));
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id), document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
                commission_sha256 TEXT NOT NULL, document TEXT NOT NULL, approval_id TEXT);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
                plan_id TEXT NOT NULL REFERENCES plans(plan_id), task_key TEXT NOT NULL,
                document TEXT NOT NULL, input_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                attempt_id TEXT, external_ref TEXT UNIQUE, receipt TEXT, receipt_sha256 TEXT,
                UNIQUE(plan_id, task_key));
            CREATE TABLE IF NOT EXISTS events (
                event_number INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                event_type TEXT NOT NULL, document TEXT NOT NULL, recorded_utc TEXT NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        if self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != SCHEMA_VERSION:
            self.db.close()
            raise ContractError("Unsupported project store schema; no implicit migration")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> ProjectStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN" if self.readonly else "BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _one(self, query: str, args: tuple) -> sqlite3.Row:
        row = self.db.execute(query, args).fetchone()
        if row is None:
            raise ContractError("Unknown project, version, plan, or task identity")
        return row

    def _event(self, project_id: str, event: str, payload: dict) -> None:
        self.db.execute("INSERT INTO events(project_id,event_type,document,recorded_utc) VALUES (?,?,?,?)",
                        (project_id, event, _json(payload), datetime.now(timezone.utc).isoformat()))

    def create_project(self, project_id: str, *, evidence_kind: str = FIXTURE) -> None:
        require_id(project_id, "project_id")
        if evidence_kind not in {FIXTURE, UNVERIFIED_EXTERNAL}:
            raise ContractError("Origin must be fixture or explicitly unverified external records")
        with self._transaction():
            existing = self.db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if existing:
                if existing["evidence_kind"] != evidence_kind:
                    raise ContractError("Project evidence origin cannot change")
                return
            self.db.execute("INSERT INTO projects VALUES (?,?,NULL,NULL)", (project_id, evidence_kind))
            self._event(project_id, "PROJECT_CREATED", {"evidence_kind": evidence_kind})

    def propose_commission(self, project_id: str, document: dict, *, source_ref: str) -> dict:
        """An immutable proposal never silently replaces the confirmed objective."""
        _text(source_ref, "source_ref")
        if not isinstance(document, dict) or not document:
            raise ContractError("Commission document must be nonempty JSON")
        encoded, digest = _json(document), canonical_hash(document)
        with self._transaction():
            self._one("SELECT * FROM projects WHERE project_id=?", (project_id,))
            old = self.db.execute("SELECT * FROM commissions WHERE project_id=? AND sha256=?",
                                  (project_id, digest)).fetchone()
            if old:
                return dict(old)
            version = self.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM commissions WHERE project_id=?",
                                      (project_id,)).fetchone()[0]
            self.db.execute("INSERT INTO commissions VALUES (?,?,?,?,?)",
                            (project_id, version, digest, encoded, source_ref))
            self._event(project_id, "COMMISSION_PROPOSED", {"version": version, "sha256": digest,
                                                           "source_ref": source_ref})
            return dict(self._one("SELECT * FROM commissions WHERE project_id=? AND version=?",
                                  (project_id, version)))

    def _decision(self, project_id: str, action: str, subject: str, actor: str,
                  source_ref: str, detail: str = "") -> tuple[str, bool]:
        _text(actor, "user actor")
        _text(source_ref, "user confirmation source_ref")
        doc = {"project_id": project_id, "action": action, "subject": subject,
               "actor": actor, "source_ref": source_ref, "detail": detail,
               "trust_boundary": "TRUSTED_LOCAL_CALLER_NOT_AUTHENTICATED_BY_THIS_LIBRARY"}
        decision_id = "decision_" + canonical_hash(doc)
        exists = self.db.execute("SELECT 1 FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if not exists:
            self.db.execute("INSERT INTO decisions VALUES (?,?,?)", (decision_id, project_id, _json(doc)))
            self._event(project_id, action, {"decision_id": decision_id, "subject": subject})
        return decision_id, exists is None

    def confirm_commission(self, project_id: str, sha256: str, *, actor: str, source_ref: str) -> str:
        with self._transaction():
            self._one("SELECT * FROM commissions WHERE project_id=? AND sha256=?", (project_id, sha256))
            decision, fresh = self._decision(project_id, "USER_CONFIRMED_COMMISSION", sha256, actor, source_ref)
            # Replaying an old confirmation must not revert a newer confirmed objective.
            if fresh:
                project = self._one("SELECT * FROM projects WHERE project_id=?", (project_id,))
                if project["confirmed_commission"] != sha256:
                    # A -> B -> A is a new decision, not permission to revive A's old plan.
                    for plan in self.db.execute(
                            "SELECT plan_id,approval_id FROM plans WHERE project_id=? AND approval_id IS NOT NULL",
                            (project_id,)).fetchall():
                        self._event(project_id, "PLAN_APPROVAL_INVALIDATED", {
                            "plan_id": plan["plan_id"], "previous_approval_id": plan["approval_id"],
                            "commission_confirmation_id": decision})
                    self.db.execute("UPDATE plans SET approval_id=NULL WHERE project_id=?", (project_id,))
                self.db.execute("UPDATE projects SET confirmed_commission=? WHERE project_id=?", (sha256, project_id))
            return decision

    def prepare_plan(self, project_id: str, commission_sha256: str, steps: list[dict], *,
                     max_submissions: int, model_call_id: str | None = None,
                     proposal_metadata: dict | None = None) -> str:
        if type(max_submissions) is not int or max_submissions < 0:
            raise ContractError("Submission limit must be a nonnegative integer")
        if not isinstance(steps, list) or not steps:
            raise ContractError("An explicit nonempty task list is required")
        normalized, seen = [], set()
        for step in steps:
            required = {"key", "tool", "tool_version", "object_id", "inputs"}
            if not isinstance(step, dict) or not required <= step.keys() or step.keys() - required - {"depends_on"}:
                raise ContractError("Unexpected or missing task fields")
            for key in ("key", "tool", "tool_version", "object_id"):
                require_id(step[key], key)
            if step["key"] in seen or not isinstance(step["inputs"], dict):
                raise ContractError("Task keys must be unique; inputs must be JSON objects")
            deps = step.get("depends_on", [])
            if not isinstance(deps, list) or any(not isinstance(x, str) for x in deps):
                raise ContractError("Dependencies must be task keys")
            if len(set(deps)) != len(deps) or not set(deps) <= seen:
                raise ContractError("Dependencies must refer to earlier tasks in this plan")
            normalized.append({**step, "depends_on": deps})
            seen.add(step["key"])
        doc = {"schema": "modab_plan_v1", "project_id": project_id,
               "commission_sha256": commission_sha256, "steps": normalized,
               "max_submissions": max_submissions, "budget_unit": "CLIENT_HANDOFFS_NOT_GPU_TIME"}
        if model_call_id is not None:
            doc["model_call_id"] = model_call_id
        if proposal_metadata is not None:
            expected = {"origin", "source_ref", "source_sha256", "assumptions", "unknowns", "questions", "rationale"}
            if model_call_id is not None or not isinstance(proposal_metadata, dict) or set(proposal_metadata) != expected:
                raise ContractError("Manual proposal metadata fields or provenance conflict")
            if proposal_metadata["origin"] != "MANUAL_IMPORT":
                raise ContractError("Manual proposal origin must be explicit")
            for field in ("source_ref", "source_sha256", "rationale"):
                _text(proposal_metadata[field], field)
            require_digest(proposal_metadata["source_sha256"], "proposal source_sha256")
            for field in ("assumptions", "unknowns", "questions"):
                if not isinstance(proposal_metadata[field], list):
                    raise ContractError("Proposal uncertainty fields must be lists")
                for value in proposal_metadata[field]: _text(value, field)
            doc["proposal_metadata"] = proposal_metadata
        encoded, plan_id = _json(doc), "plan_" + canonical_hash(doc)
        with self._transaction():
            self._one("SELECT * FROM commissions WHERE project_id=? AND sha256=?", (project_id, commission_sha256))
            if model_call_id is not None:
                call = self._model_call(project_id, model_call_id)
                if call["status"] != "VALIDATED" or call["request"]["kind"] != "plan":
                    raise ContractError("A validated planning proposal is required")
                self._require_model_context(call["request"]["context"])
                if commission_sha256 != call["request"]["context"]["commission_sha256"]:
                    raise ContractError("Model proposal commission mismatch")
            if not self.db.execute("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)).fetchone():
                self.db.execute("INSERT INTO plans VALUES (?,?,?,?,NULL)",
                                (plan_id, project_id, commission_sha256, encoded))
                self._event(project_id, "PLAN_PREPARED_NOT_APPROVED", {"plan_id": plan_id})
        return plan_id

    def approve_plan(self, plan_id: str, *, actor: str, source_ref: str) -> str:
        with self._transaction():
            plan = self._one("SELECT * FROM plans WHERE plan_id=?", (plan_id,))
            project = self._one("SELECT * FROM projects WHERE project_id=?", (plan["project_id"],))
            if project["confirmed_commission"] != plan["commission_sha256"]:
                raise ContractError("Plan must bind the currently user-confirmed commission")
            metadata = json.loads(plan["document"]).get("proposal_metadata", {})
            if metadata.get("unknowns") or metadata.get("questions"):
                raise ContractError("Unresolved proposal unknowns/questions prevent approval")
            if plan["approval_id"]:
                return plan["approval_id"]
            decision, fresh = self._decision(plan["project_id"], "USER_APPROVED_PLAN", plan_id, actor, source_ref)
            if not fresh:
                raise ContractError("Invalidated plan approval cannot be replayed; a fresh user confirmation is required")
            self.db.execute("UPDATE plans SET approval_id=? WHERE plan_id=?", (decision, plan_id))
            return decision

    def materialize_tasks(self, plan_id: str, *, expected_project_id: str | None = None) -> list[str]:
        """Register exact approved inputs. Registration never means execution."""
        with self._transaction():
            plan = self._one("SELECT * FROM plans WHERE plan_id=?", (plan_id,))
            if expected_project_id is not None:
                require_id(expected_project_id, "expected_project_id")
                if plan["project_id"] != expected_project_id:
                    raise ContractError("Plan belongs to a different project")
                project = self._one("SELECT * FROM projects WHERE project_id=?", (expected_project_id,))
                if project["stop_reason"] or project["confirmed_commission"] != plan["commission_sha256"]:
                    raise ContractError("Current confirmed commission and non-stopped project required")
            if not plan["approval_id"]:
                raise ContractError("Cannot materialize an unapproved plan")
            result = []
            for step in json.loads(plan["document"])["steps"]:
                task_id = "task_" + canonical_hash({"plan_id": plan_id, "key": step["key"]})
                identity = {k: step[k] for k in ("tool", "tool_version", "object_id", "inputs")}
                if not self.db.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
                    self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
                                    (task_id, plan["project_id"], plan_id, step["key"], _json(step),
                                     canonical_hash(identity), "NOT_RUN"))
                    self._event(plan["project_id"], "TASK_REGISTERED_NOT_RUN", {"task_id": task_id})
                result.append(task_id)
            return result

    def task(self, task_id: str) -> dict:
        row = dict(self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,)))
        row["document"] = json.loads(row["document"])
        row["receipt"] = json.loads(row["receipt"]) if row["receipt"] else None
        return row

    def _blocked_reason(self, task: sqlite3.Row) -> str | None:
        plan = self._one("SELECT * FROM plans WHERE plan_id=?", (task["plan_id"],))
        project = self._one("SELECT * FROM projects WHERE project_id=?", (task["project_id"],))
        if project["stop_reason"]:
            return "PROJECT_STOPPED"
        if project["confirmed_commission"] != plan["commission_sha256"]:
            return "COMMISSION_CHANGED_REQUIRES_NEW_APPROVED_PLAN"
        if not plan["approval_id"]:
            return "PLAN_REQUIRES_NEW_USER_APPROVAL"
        for key in json.loads(task["document"])["depends_on"]:
            parent = self._one("SELECT status FROM tasks WHERE plan_id=? AND task_key=?", (task["plan_id"], key))
            if parent["status"] != "SUCCEEDED":
                return "DEPENDENCY_NOT_SUCCEEDED"
        count = self.db.execute("SELECT COUNT(*) FROM tasks WHERE plan_id=? AND attempt_id IS NOT NULL",
                                (task["plan_id"],)).fetchone()[0]
        if count >= json.loads(plan["document"])["max_submissions"]:
            return "SUBMISSION_LIMIT_REACHED"
        return None

    def claim_handoff(self, task_id: str, *, handoff_metadata: dict | None = None) -> dict | None:
        """Return a package once; crash recovery must LOOK UP this idempotency key."""
        if handoff_metadata is not None:
            handoff_metadata = json.loads(_json(handoff_metadata))
        with self._transaction():
            task = self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if task["status"] != "NOT_RUN" or self._blocked_reason(task):
                return None
            attempt_id = "attempt_" + canonical_hash({"task_id": task_id, "attempt_number": 1})
            if handoff_metadata is not None:
                plan = self._one("SELECT * FROM plans WHERE plan_id=?", (task["plan_id"],))
                project = self._one("SELECT * FROM projects WHERE project_id=?", (task["project_id"],))
                request = handoff_metadata.get("request", {})
                bindings = {"request_id": attempt_id, "task_id": task_id, "project_id": task["project_id"],
                            "target_sha256": plan["commission_sha256"], "approval_id": plan["approval_id"],
                            "input_sha256": task["input_sha256"], "mode": FIXTURE}
                if (handoff_metadata.get("mode") != FIXTURE or project["evidence_kind"] != FIXTURE or
                    not isinstance(request, dict) or any(request.get(k) != v for k, v in bindings.items())):
                    raise ContractError("Fixture handoff no longer matches approved inputs or target")
            self.db.execute("UPDATE tasks SET status='SUBMISSION_PENDING', attempt_id=? WHERE task_id=?",
                            (attempt_id, task_id))
            self._event(task["project_id"], "HANDOFF_RESERVED_NOT_EXECUTION_PROOF",
                         {"task_id": task_id, "attempt_id": attempt_id,
                          **({"handoff_metadata": handoff_metadata} if handoff_metadata is not None else {})})
            return self.task(task_id)

    def record_fixture_note(self, project_id: str, key: str, payload: dict) -> None:
        """Idempotent diagnostic event; grants no approval or task state authority."""
        _text(key, "fixture note key")
        payload = json.loads(_json(payload))
        with self._transaction():
            project = self._one("SELECT * FROM projects WHERE project_id=?", (project_id,))
            if project["evidence_kind"] != FIXTURE:
                raise ContractError("Fixture notes require fixture project origin")
            for row in self.db.execute("SELECT document FROM events WHERE project_id=? AND event_type='FIXTURE_NOTE'", (project_id,)):
                old = json.loads(row[0])
                if old["key"] == key:
                    if old["payload"] != payload:
                        raise ContractError("Conflicting fixture note")
                    return
            self._event(project_id, "FIXTURE_NOTE", {"key": key, "payload": payload})

    def record_started(self, task_id: str, attempt_id: str, *, external_ref: str) -> None:
        _text(external_ref, "external_ref")
        with self._transaction():
            task = self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if task["attempt_id"] != attempt_id:
                raise ContractError("Start record attempt identity mismatch")
            if task["external_ref"] == external_ref and task["status"] in {"RUNNING", "SUCCEEDED", "FAILED"}:
                return
            if task["status"] != "SUBMISSION_PENDING":
                raise ContractError("Start record requires a pending handoff")
            if self.db.execute("SELECT 1 FROM tasks WHERE external_ref=?", (external_ref,)).fetchone():
                raise ContractError("External job is already bound to a different task")
            self.db.execute("UPDATE tasks SET status='RUNNING',external_ref=? WHERE task_id=?", (external_ref, task_id))
            self._event(task["project_id"], "EXECUTOR_REPORTED_STARTED",
                        {"task_id": task_id, "external_ref": external_ref})

    def record_receipt(self, task_id: str, receipt: dict) -> str:
        expected_fields = {"task_id", "attempt_id", "external_ref", "tool", "tool_version", "object_id",
                           "input_sha256", "status", "output", "error", "source_ref", "evidence_kind"}
        if not isinstance(receipt, dict) or set(receipt) != expected_fields:
            raise ContractError("Receipt fields do not match the handoff contract")
        _text(receipt["source_ref"], "receipt source_ref")
        if receipt["status"] not in {"SUCCEEDED", "FAILED"}:
            raise ContractError("Only explicit terminal outcomes are accepted as receipts")
        if receipt["output"] is not None and not isinstance(receipt["output"], dict):
            raise ContractError("Output must be a JSON object or explicit null")
        if receipt["status"] == "SUCCEEDED" and (receipt["output"] is None or receipt["error"] is not None):
            raise ContractError("Success requires an output and no error")
        if receipt["status"] == "FAILED":
            _text(receipt["error"], "failure error")
        digest = canonical_hash(receipt)
        with self._transaction():
            task = self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            step = json.loads(task["document"])
            project = self._one("SELECT * FROM projects WHERE project_id=?", (task["project_id"],))
            bindings = {"task_id": task_id, "attempt_id": task["attempt_id"],
                        "external_ref": task["external_ref"], "input_sha256": task["input_sha256"],
                        "evidence_kind": project["evidence_kind"],
                        **{k: step[k] for k in ("tool", "tool_version", "object_id")}}
            if any(receipt[k] != value for k, value in bindings.items()):
                raise ContractError("Receipt task/tool/input/object/origin identity mismatch")
            if task["receipt_sha256"]:
                if task["receipt_sha256"] != digest:
                    raise ContractError("Conflicting terminal receipt; existing result is immutable")
                return digest
            if task["status"] != "RUNNING":
                raise ContractError("Receipt requires a confirmed external start record")
            self.db.execute("UPDATE tasks SET status=?,receipt=?,receipt_sha256=? WHERE task_id=?",
                            (receipt["status"], _json(receipt), digest, task_id))
            self._event(task["project_id"], "RECEIPT_RECORDED",
                        {"task_id": task_id, "receipt_sha256": digest,
                         "output_sha256": canonical_hash(receipt["output"]) if receipt["output"] is not None else None})
            return digest

    def record_user_decision(self, project_id: str, *, subject: str, action: str,
                             actor: str, source_ref: str, detail: str) -> str:
        """Record interpretation/stop decisions; 'continue' cannot restart a stopped project."""
        if action not in {"CONTINUE", "STOP", "ACCEPT_HANDOFF"}:
            raise ContractError("Unsupported user decision")
        _text(detail, "decision detail")
        with self._transaction():
            project = self._one("SELECT * FROM projects WHERE project_id=?", (project_id,))
            valid = subject == project["confirmed_commission"] or self.db.execute(
                "SELECT 1 FROM plans WHERE project_id=? AND plan_id=?", (project_id, subject)).fetchone()
            valid = valid or self.db.execute("SELECT 1 FROM tasks WHERE project_id=? AND receipt_sha256=?",
                                             (project_id, subject)).fetchone()
            if not valid:
                raise ContractError("Decision must identify a recorded commission, plan, or receipt")
            decision, fresh = self._decision(project_id, "USER_" + action, subject, actor, source_ref, detail)
            if action == "STOP" and fresh:
                self.db.execute("UPDATE projects SET stop_reason=? WHERE project_id=?", (detail, project_id))
            return decision

    def _model_context(self, project_id: str, plan_id: str | None = None, *,
                       source_plan_call_id: str | None = None) -> dict:
        if plan_id is not None and source_plan_call_id is not None:
            raise ContractError("Select a stored plan OR a source planning call")
        project = self._one("SELECT * FROM projects WHERE project_id=?", (project_id,))
        if not project["confirmed_commission"] or project["stop_reason"]:
            raise ContractError("Model work requires a confirmed, non-stopped project")
        commission = self._one("SELECT * FROM commissions WHERE project_id=? AND sha256=?",
                               (project_id, project["confirmed_commission"]))
        confirmation = self._one("SELECT document FROM events WHERE project_id=? AND event_type='USER_CONFIRMED_COMMISSION' ORDER BY event_number DESC LIMIT 1",
                                 (project_id,))
        context = {"project_id": project_id, "commission_sha256": commission["sha256"],
                   "confirmation_id": json.loads(confirmation[0])["decision_id"],
                   "commission": json.loads(commission["document"]), "plan_id": plan_id,
                   "plan": None, "task_evidence": []}
        if source_plan_call_id is not None:
            source = self._model_call(project_id, source_plan_call_id)
            proposal = source.get("proposal")
            if source["status"] != "VALIDATED" or source["request"]["kind"] != "plan" or not isinstance(proposal, dict):
                raise ContractError("Linked review requires a recorded validated planning response")
            if (source["request"]["context"]["commission_sha256"] != commission["sha256"]
                    or proposal["commission_sha256"] != commission["sha256"]):
                raise ContractError("Source planning call belongs to another commission")
            if (canonical_hash(source["request"]) != source["request_sha256"]
                    or canonical_hash(source["response"]) != source["response_sha256"]):
                raise ContractError("Source planning call provenance mismatch")
            # Bind to the exact response document, including unknowns/questions.
            if json.loads(source["response"]["text"]) != proposal:
                raise ContractError("Stored planning proposal differs from its original response")
            digest = canonical_hash(proposal)
            context["plan_id"] = "proposal_" + digest
            context["plan"] = proposal
            context["review_target"] = {"kind": "MODEL_PLAN_PROPOSAL_NOT_EXECUTED",
                "source_plan_call_id": source_plan_call_id, "proposal_sha256": digest,
                "source_request_sha256": source["request_sha256"], "source_response_sha256": source["response_sha256"],
                "model_output_origin": source["model_output_origin"],
                "execution_receipts_attached": False}
        elif plan_id is not None:
            plan = self._one("SELECT * FROM plans WHERE project_id=? AND plan_id=?", (project_id, plan_id))
            if plan["commission_sha256"] != commission["sha256"]:
                raise ContractError("Cannot review a plan from another confirmed commission")
            context["plan"] = json.loads(plan["document"])
            for row in self.db.execute("SELECT task_id,status,receipt,receipt_sha256 FROM tasks WHERE plan_id=? ORDER BY task_id", (plan_id,)):
                context["task_evidence"].append({"task_id": row["task_id"], "status": row["status"],
                    "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
                    "receipt_sha256": row["receipt_sha256"]})
        return context

    def model_context(self, project_id: str, plan_id: str | None = None, *,
                      source_plan_call_id: str | None = None) -> dict:
        """Snapshot only the current commission and an optional plan's recorded evidence."""
        with self._transaction():
            return self._model_context(project_id, plan_id, source_plan_call_id=source_plan_call_id)

    def _require_model_context(self, context: dict) -> None:
        source = context.get("review_target", {}).get("source_plan_call_id")
        current = self._model_context(context["project_id"], None if source else context["plan_id"],
                                      source_plan_call_id=source)
        if canonical_hash(current) != canonical_hash(context):
            raise ContractError("Model context changed; obtain a new proposal")

    def _model_call(self, project_id: str, call_id: str) -> dict:
        call = None
        for row in self.db.execute("SELECT event_type,document FROM events WHERE project_id=? AND event_type IN ('MODEL_CALL_RESERVED','MODEL_CALL_FINISHED') ORDER BY event_number", (project_id,)):
            doc = json.loads(row["document"])
            if doc["call_id"] == call_id:
                call = doc if row["event_type"] == "MODEL_CALL_RESERVED" else {**call, **doc}
        if call is None:
            raise ContractError("Unknown model call")
        return call

    def model_call(self, project_id: str, call_id: str) -> dict:
        with self._transaction():
            return self._model_call(project_id, call_id)

    def begin_model_call(self, project_id: str, call_id: str, request: dict, *, evidence_kind: str) -> None:
        """Reserve once in the existing journal; no transmission or automatic retry."""
        require_id(call_id, "model call_id")
        request = json.loads(_json(request))
        with self._transaction():
            project = self._one("SELECT * FROM projects WHERE project_id=?", (project_id,))
            mixed_fixture_review = (evidence_kind == UNVERIFIED_EXTERNAL
                and project["evidence_kind"] == FIXTURE
                and request.get("evidence_policy") == "EXTERNAL_MODEL_MAY_REVIEW_LABELLED_FIXTURES")
            if evidence_kind not in {FIXTURE, UNVERIFIED_EXTERNAL} or (
                    evidence_kind != project["evidence_kind"] and not mixed_fixture_review):
                raise ContractError("Model transport and project evidence origins differ")
            if request["context"]["project_id"] != project_id:
                raise ContractError("Model request project mismatch")
            self._require_model_context(request["context"])
            for row in self.db.execute("SELECT document FROM events WHERE project_id=? AND event_type='MODEL_CALL_RESERVED'", (project_id,)):
                if json.loads(row[0])["call_id"] == call_id:
                    raise ContractError("Model call already reserved; inspect its record, do not resend")
            origins = sorted({t["receipt"]["evidence_kind"] for t in request["context"]["task_evidence"]
                              if t["receipt"] is not None})
            self._event(project_id, "MODEL_CALL_RESERVED", {"call_id": call_id, "request": request,
                "request_sha256": canonical_hash(request), "evidence_kind": evidence_kind,
                "model_output_origin": evidence_kind, "referenced_tool_evidence_origins": origins,
                "evidence_notice": "Model output origin never upgrades referenced evidence",
                "status": "RESERVED_OUTCOME_UNKNOWN"})

    def finish_model_call(self, project_id: str, call_id: str, *, status: str,
                          response: dict | None, proposal: dict | None, error_code: str | None) -> dict:
        if status not in {"VALIDATED", "FAILED", "INVALID_OUTPUT", "UNCERTAIN"}:
            raise ContractError("Unsupported model call outcome")
        if status == "VALIDATED":
            if not isinstance(response, dict) or not isinstance(proposal, dict) or error_code is not None:
                raise ContractError("Validated model outcome requires response/proposal and no error")
        elif proposal is not None or not isinstance(error_code, str) or not error_code.strip():
            raise ContractError("Failed model outcome requires an error code and no accepted proposal")
        with self._transaction():
            call = self._model_call(project_id, call_id)
            if call["status"] != "RESERVED_OUTCOME_UNKNOWN":
                raise ContractError("Model call result already recorded; refusing replacement")
            if status == "VALIDATED":
                try:
                    self._require_model_context(call["request"]["context"])
                except ContractError:
                    status, error_code = "STALE_CONTEXT", "CONTEXT_CHANGED"
            result = {"call_id": call_id, "status": status, "response": response,
                      "response_sha256": canonical_hash(response) if response is not None else None,
                      "proposal": proposal, "error_code": error_code}
            self._event(project_id, "MODEL_CALL_FINISHED", result)
            return result

    def recovery_items(self, project_id: str) -> list[dict]:
        rows = self.db.execute("SELECT task_id FROM tasks WHERE project_id=? AND status IN ('SUBMISSION_PENDING','RUNNING') ORDER BY task_id",
                               (project_id,)).fetchall()
        return [{**self.task(row[0]), "recovery_action": "LOOKUP_ONLY_DO_NOT_RESUBMIT"} for row in rows]

    def delivery_index(self, project_id: str) -> dict:
        """Consistent snapshot including failures, unrun work, and unresolved handoffs."""
        with self._transaction():
            project = dict(self._one("SELECT * FROM projects WHERE project_id=?", (project_id,)))
            tasks, counts = [], {}
            for raw in self.db.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY plan_id,task_key", (project_id,)).fetchall():
                item = self.task(raw["task_id"])
                item["blocked_reason"] = self._blocked_reason(raw) if raw["status"] == "NOT_RUN" else None
                receipt = item["receipt"]
                item["output_sha256"] = canonical_hash(receipt["output"]) if receipt and receipt["output"] is not None else None
                tasks.append(item)
                counts[raw["status"]] = counts.get(raw["status"], 0) + 1
            records = {}
            for table, order in (("commissions", "version"), ("decisions", "decision_id"), ("plans", "plan_id"), ("events", "event_number")):
                records[table] = []
                for row in self.db.execute(f"SELECT * FROM {table} WHERE project_id=? ORDER BY {order}", (project_id,)):
                    item = dict(row)
                    item["document"] = json.loads(item["document"])
                    records[table].append(item)
            model_calls = [self._model_call(project_id, row["document"]["call_id"])
                           for row in records["events"] if row["event_type"] == "MODEL_CALL_RESERVED"]
            index = {"schema": "modab_delivery_index_v1", "project": project, **records,
                     "task_count": len(tasks), "status_counts": counts, "tasks": tasks,
                     "unresolved_task_ids": [t["task_id"] for t in tasks if t["status"] in {"SUBMISSION_PENDING", "RUNNING"}],
                      "model_calls": model_calls,
                      "unresolved_model_call_ids": [c["call_id"] for c in model_calls if c["status"] in {"RESERVED_OUTCOME_UNKNOWN", "UNCERTAIN"}],
                      "backend_connections": "NOT_VERIFIED_BY_THIS_RECORD_STORE",
                     "claim_boundary": "SOFTWARE_RECORDS_ONLY__NO_BIOLOGICAL_EFFECTIVENESS_CLAIM"}
            return {**index, "index_sha256": canonical_hash(index)}

    def export_delivery(self, project_id: str, directory: Path) -> Path:
        index = self.delivery_index(project_id)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"delivery_index.{index['index_sha256']}.json"
        payload = (json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if path.exists():
            if path.read_bytes() != payload:
                raise ContractError("Existing delivery artifact differs; refusing overwrite")
        else:
            with path.open("xb") as handle:
                handle.write(payload)
        return path
