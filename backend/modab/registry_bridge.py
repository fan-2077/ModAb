"""One request per process. Registration records work; it never starts it."""
import argparse
import json
from pathlib import Path
import sys
import traceback

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dualctx.contracts import ContractError
from modab.project_store import ProjectStore
from modab.registry_contracts import (
    MAX_REQUEST_BYTES, RESPONSE_SCHEMA, parse_request,
)


def handle(workspace: Path, payload: bytes) -> dict:
    request = parse_request(payload)
    workspace = Path(workspace).resolve()
    if not (workspace / "modab.sqlite3").is_file():
        raise ContractError("Existing project store required")
    with ProjectStore(workspace, readonly=request.operation == "query") as store:
        if request.operation == "register":
            store.materialize_tasks(request.plan_id,
                                    expected_project_id=request.project_id)
        with store._transaction():
            plan = store._one("SELECT * FROM plans WHERE plan_id=?", (request.plan_id,))
            if plan["project_id"] != request.project_id:
                raise ContractError("Plan belongs to a different project")
            project = store._one("SELECT * FROM projects WHERE project_id=?",
                                 (request.project_id,))
            entries = []
            rows = store.db.execute(
                "SELECT task_id FROM tasks WHERE plan_id=? ORDER BY task_key",
                (request.plan_id,)).fetchall()
            for row in rows:
                task = store.task(row["task_id"])
                entries.append({
                    "legacy_task_id": task["task_id"],
                    "status": task["status"],
                    "input_sha256": task["input_sha256"],
                    "attempt_id": task["attempt_id"],
                    "external_ref": task["external_ref"],
                    "receipt_sha256": task["receipt_sha256"],
                    "error": task["receipt"]["error"] if task["receipt"] else None,
                })
            return {
                "schema": RESPONSE_SCHEMA,
                "project_id": request.project_id,
                "plan_id": request.plan_id,
                "evidence_kind": project["evidence_kind"],
                "entries": entries,
            }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = handle(args.workspace, sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except ContractError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True))
        return 2
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
