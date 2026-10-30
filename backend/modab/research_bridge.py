"""Evidence-grounded research proposals and bounded local preparation for DSH.

The model supplies the route. This bridge validates its references and records
it; it does not choose a predictor or treat a proposal as scientific execution.
Only reuse_evidence and prepare_requirements have local implementations here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dualctx.contracts import ContractError, canonical_hash, require_id
from modab.project_store import ProjectStore, UNVERIFIED_EXTERNAL

MAX_BYTES = 65536
SCHEMA = "modab_research_response_v1"
MODES = {"full_evaluation", "existing_only"}


def read(path):
    return json.loads(Path(path).read_bytes())


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_text(value, field, limit=6000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(f"Invalid {field}")


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ContractError("Missing or unexpected fields: " + ",".join(keys))


def texts(value, field, *, minimum=0, maximum=20):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ContractError(f"Invalid {field} list")
    for item in value:
        require_text(item, field)


def save_immutable(path, value):
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ContractError("Existing artifact differs; refusing replacement")


def snapshot(config):
    """Read pinned local evidence; never trust an agent-supplied path or digest."""
    source = {}
    for key, item in config["evidence"].items():
        if file_hash(item["path"]) != item["sha256"]:
            raise ContractError("Evidence changed: " + key)
        source[key] = read(item["path"])
    measured = source["measurements"]
    if measured["status"] != "STRUCTURE_EVALUATION_COMPLETE" or measured["process_exit_code"] != 0:
        raise ContractError("Completed original-coordinate evaluation required")
    candidates = measured["candidates"]
    ids = [row["name"] for row in candidates]
    if len(ids) != 4 or len(set(ids)) != 4 or any(row["status"] != "MEASURED" for row in candidates):
        raise ContractError("All four recorded candidates must be retained")
    control = source["control_sources"]
    # Previous human/Codex recommendations and T361-T365 choices are deliberately
    # excluded. The model sees factual sources, not a preselected answer.
    value = {
        "evidence": {
            "measurements": {"source_sha256": config["evidence"]["measurements"]["sha256"],
                "candidate_count": len(ids), "candidates": candidates,
                "reference": measured["reference"], "scope": measured["interpretation"]},
            "geometry": {"source_sha256": config["evidence"]["geometry"]["sha256"],
                "facts": source["geometry"]["facts"],
                "limitations": source["geometry"]["limitations_zh"]},
            "control_sources": {"source_sha256": config["evidence"]["control_sources"]["sha256"],
                "existing_reference": control["current_evidence"]["existing_reference_scope"],
                "experimental_context": control["current_evidence"]["experimental_context"],
                "sources": control["sources"],
                "provenance": "Reviewed local source snapshot dated 2026-10-27; not a new literature or remote-environment check"},
        },
        "methods": config["methods"],
        "candidate_ids": ids,
        "executable_actions": ["reuse_evidence", "prepare_requirements"],
        "execution_boundary": "Local evidence packaging and requirement preparation only. No new scientific inference, installation, optimization or GPU job is available through these tools.",
    }
    if "reference_measurements" in source:
        reference = source["reference_measurements"]
        total = reference["contact_counts"]["4.0"]
        phosphate = reference["phosphate_contact_counts"]["4.0"]
        value["evidence"]["reference_measurements"] = {
            "source_sha256": config["evidence"]["reference_measurements"]["sha256"],
            "cutoff_A": 4.0, "definition": "Residue-pair counts, not atom-pair counts; total and phosphate-only counts are distinct metrics",
            "reference": {"name": reference["name"], "total_contacts": total, "phosphate_contacts": phosphate},
            "comparisons": [{"candidate_id": row["name"],
                "total_contacts": row["contact_pairs_le_4_A"],
                "total_minus_reference": row["contact_pairs_le_4_A"] - total,
                "phosphate_contacts": row["phosphate_contact_pairs_le_4_A"],
                "phosphate_minus_reference": row["phosphate_contact_pairs_le_4_A"] - phosphate}
                for row in candidates],
        }
        value["interpretation_rules"] = {
            "scope": "Software and computation only; no wet-lab work is commissioned",
            "geometry": "Small backbone RMSD describes proximity to reference; it does not establish all-atom self-consistency or physical validity",
            "contacts": "Compare each metric to its own reference. A lost distance-defined local contact does not establish weaker energetic interaction or loss of the entire recognition network",
            "short_distance": "The 1.724 A pair is oxygen-to-oxygen, not hydrogen-to-acceptor; report an anomalous near contact. It is not evidence of a strong hydrogen bond",
            "uncertainty": "No measured uncertainty/noise distribution or functional labels exists for these candidates; do not assert differences are within noise or assign a proven systematic cause",
            "calibration": "Check whether a chosen computational readout can distinguish reference states. Negative or non-discriminating results remain valid outcomes. Structural prediction cannot reproduce SPR KD magnitudes",
            "recommendation": "Paired structural prediction is a proposed structural diagnostic among possible methods, not the only way to assess specificity and not proof of specificity",
        }
    if config.get("context_version") == "explicit_metric_scope_v2":
        pair = value["evidence"]["geometry"]["facts"]["candidate_04_close_pair"]
        chains = [atom.split(":", 1)[0] for atom in pair["atoms"]]
        if chains != ["A", "C"] or candidates[3]["interchain_atom_pairs_lt_2_A"] != 1:
            raise ContractError("Known near-contact identity/count changed; review the evidence scope")
        value["evidence"]["geometry"]["short_contact_summary"] = {
            "metric": "Interchain heavy-atom pairs strictly below 2.0 A",
            "counts_by_candidate": {row["name"]: row["interchain_atom_pairs_lt_2_A"] for row in candidates},
            "candidate_04_pair": {**pair, "chains": chains, "is_interchain": True},
            "no_pair_detected_candidates": [row["name"] for row in candidates if row["interchain_atom_pairs_lt_2_A"] == 0],
            "all_candidates_have_zero_pairs": False,
            "scope": "This distance screen is not a full clashscore or all-atom validity check",
        }
        # The original prose contains a conditional 'not finding ... does not
        # imply ...'. Supply its precise scope, not a sentence that can be
        # mistaken for a negative observation covering all four candidates.
        value["evidence"]["geometry"]["limitations"] = measured["interpretation"]["scope"]
        value["interpretation_rules"]["short_contact_scope"] = "Use short_contact_summary counts exactly. Candidate-04 has one INTERCHAIN A-to-C pair below 2 A; only candidates 01-03 have zero detected pairs."
    return value


def context(config, args, origin):
    exact(args, {"objective", "mode"})
    require_text(args["objective"], "objective")
    if args["mode"] not in MODES:
        raise ContractError("Unknown task mode")
    body = {"project_id": config["project_id"], **args, **snapshot(config)}
    body["plan_contract"] = plan_contract(body, config.get("evidence_contract_version") == 1)
    context_id = "context_" + canonical_hash(body)
    body["context_id"] = context_id
    path = Path(config["workspace"]) / "contexts" / (context_id + ".json")
    save_immutable(path, body)
    with ProjectStore(Path(config["workspace"])) as store:
        store.create_project(config["project_id"], evidence_kind=UNVERIFIED_EXTERNAL)
        with store._transaction():
            store._event(config["project_id"], "RESEARCH_CONTEXT_READ", {
                "context_id": context_id, "origin": origin})
    return body


def load_context(config, context_id):
    require_id(context_id, "context_id")
    body = read(Path(config["workspace"]) / "contexts" / (context_id + ".json"))
    identity = {k: v for k, v in body.items() if k != "context_id"}
    if body["context_id"] != context_id or "context_" + canonical_hash(identity) != context_id:
        raise ContractError("Context identity mismatch")
    live = {k: body[k] for k in ("project_id", "objective", "mode")} | snapshot(config)
    live["plan_contract"] = plan_contract(live, config.get("evidence_contract_version") == 1)
    if canonical_hash(live) != canonical_hash(identity):
        raise ContractError("Context changed; read evidence again")
    return body


def plan_contract(ctx, explicit_evidence=False):
    full = ctx["mode"] == "full_evaluation"
    contract = {
        "selected_method_meaning": "The later scientific prediction route, NOT the first currently executable step" if full else "Existing evidence reader only",
        "allowed_selected_methods": [row["id"] for row in ctx["methods"] if row["role"] == "structure_prediction"] if full else ["existing_evidence"],
        "required_actions": ["reuse_evidence", "prepare_requirements"] if full else ["reuse_evidence"],
        "bindings": "reuse_evidence uses existing_evidence; prepare_requirements and predict_structures use selected_method",
        "execution_is_separate": "Selecting an unavailable method records a recommendation; only local available steps can execute",
    }
    if explicit_evidence:
        contract["allowed_evidence_ids"] = list(ctx["evidence"])
        contract["evidence_id_meaning"] = "Use only allowed_evidence_ids. Nested metric names and interpretation fields are not evidence IDs; cite their parent evidence."
    return contract


def validate_proposal(ctx, proposal):
    exact(proposal, {"selected_method", "rationale", "alternatives", "evidence_ids", "steps", "unknowns"})
    require_text(proposal["rationale"], "rationale")
    methods = {row["id"]: row for row in ctx["methods"]}
    selected = proposal["selected_method"]
    if selected not in methods:
        raise ContractError("Unknown method")
    if ctx["mode"] == "existing_only" and selected != "existing_evidence":
        raise ContractError("Existing-only request cannot select new computation")
    if ctx["mode"] == "full_evaluation" and methods[selected]["role"] != "structure_prediction":
        allowed = [row["id"] for row in ctx["methods"] if row["role"] == "structure_prediction"]
        raise ContractError("selected_method is the later prediction route, not the first executable step. "
                            + "Choose from " + str(allowed) + "; keep existing_evidence only on reuse_evidence step")
    texts(proposal["evidence_ids"], "evidence_ids", minimum=1)
    if not set(proposal["evidence_ids"]) <= ctx["evidence"].keys():
        raise ContractError("Unknown evidence reference")
    alternatives = proposal["alternatives"]
    if not isinstance(alternatives, list) or not 1 <= len(alternatives) <= 5:
        raise ContractError("At least one genuine method alternative required")
    alternative_ids = set()
    for item in alternatives:
        exact(item, {"method_id", "reason"})
        if item["method_id"] not in methods or item["method_id"] == selected or item["method_id"] in alternative_ids:
            raise ContractError("Invalid alternative method")
        alternative_ids.add(item["method_id"])
        require_text(item["reason"], "alternative reason")
    if ctx["mode"] == "full_evaluation" and not any(methods[mid]["role"] == "structure_prediction" for mid in alternative_ids):
        raise ContractError("Compare an alternative prediction method")
    texts(proposal["unknowns"], "unknowns", minimum=1)
    if not isinstance(proposal["steps"], list) or not 1 <= len(proposal["steps"]) <= 8:
        raise ContractError("One to eight explicit steps required")
    normalized = []
    seen = set()
    actions = {"reuse_evidence", "prepare_requirements", "predict_structures", "optimize_geometry"}
    for step in proposal["steps"]:
        exact(step, {"action", "method_id", "purpose"})
        action, method = step["action"], step["method_id"]
        if action not in actions or method not in methods or (action, method) in seen:
            raise ContractError("Unknown or duplicate step")
        seen.add((action, method))
        require_text(step["purpose"], "step purpose")
        if ctx["mode"] == "existing_only" and action != "reuse_evidence":
            raise ContractError("Existing-only request excludes preparation and new computation")
        if action == "reuse_evidence" and method != "existing_evidence":
            raise ContractError("Evidence reuse requires the evidence reader")
        if action in {"prepare_requirements", "predict_structures"} and method != selected:
            raise ContractError("Preparation/prediction must bind to the selected method")
        if action == "predict_structures" and methods[method]["role"] != "structure_prediction":
            raise ContractError("Predictor required")
        if action == "optimize_geometry" and methods[method]["role"] != "geometry_optimization":
            raise ContractError("Optimizer required")
        executable = action in ctx["executable_actions"]
        normalized.append({**step, "status": "AVAILABLE_LOCAL" if executable else "NOT_INTEGRATED",
                           "prerequisites": [] if executable else methods[method]["requirements"]})
    if ("reuse_evidence", "existing_evidence") not in seen:
        raise ContractError("Reuse all existing candidates before new computation")
    if ctx["mode"] == "full_evaluation" and ("prepare_requirements", selected) not in seen:
        raise ContractError("Prepare missing method requirements first")
    return normalized


def propose(config, args, origin):
    exact(args, {"context_id", "proposal"})
    ctx = load_context(config, args["context_id"])
    steps = validate_proposal(ctx, args["proposal"])
    proposal_doc = {"context_id": ctx["context_id"], "proposal": args["proposal"],
                    "validated_steps": steps, "candidate_ids": ctx["candidate_ids"]}
    proposal_hash = canonical_hash(proposal_doc)
    with ProjectStore(Path(config["workspace"])) as store:
        commission = store.propose_commission(config["project_id"],
            {"objective": ctx["objective"], "mode": ctx["mode"], "context_id": ctx["context_id"]},
            source_ref="research-context:" + ctx["context_id"])
        plan_steps = [{"key": f"step_{i + 1}", "tool": step["action"], "tool_version": "v1",
            "object_id": config["project_id"], "inputs": {"proposal_sha256": proposal_hash,
            "context_id": ctx["context_id"], "method_id": step["method_id"]}}
            for i, step in enumerate(steps)]
        plan_id = store.prepare_plan(config["project_id"], commission["sha256"], plan_steps,
                                     max_submissions=0)
        result = {"plan_id": plan_id, "status": "PROPOSED_WITH_LOCAL_ACTIONS_AVAILABLE",
            "mode": ctx["mode"], **proposal_doc,
            "scientific_execution_approved": False,
            "claim": "Schema/reference validation only; rationale quality requires review"}
        if config.get("decision_facts_version") == 1:
            result["method_decision_facts"] = method_decision_facts(ctx, args["proposal"])
        save_immutable(Path(config["workspace"]) / "plans" / (plan_id + ".json"), result)
        with store._transaction():
            store._event(config["project_id"], "RESEARCH_PLAN_PROPOSED", {
                "plan_id": plan_id, "proposal_sha256": proposal_hash, "origin": origin})
    return result


def method_decision_facts(ctx, proposal):
    methods = {row["id"]: row for row in ctx["methods"]}
    selected = methods[proposal["selected_method"]]
    predictors = [row for row in ctx["methods"] if row["role"] == "structure_prediction"]
    return {
        "selected_method": selected["id"], "selected_name": selected["name"],
        "selected_role": selected["role"], "selected_availability": selected["availability"],
        "documented_prediction_candidates": [row["id"] for row in predictors],
        "documented_prediction_candidate_count": len(predictors),
        "currently_executable_predictors": [row["id"] for row in predictors if row["availability"] == "LOCAL_ADAPTER_READY"],
        "selection_does_not_change_availability": True,
        "interpretation": "A preferred method may be NOT_INTEGRATED. More than one documented candidate means the selected method is not uniquely compatible. Optimizers are not substitutes for predictors.",
        "alternative_roles": [{"id": item["method_id"], "role": methods[item["method_id"]]["role"],
            "availability": methods[item["method_id"]]["availability"]} for item in proposal["alternatives"]],
    }


def load_plan(config, plan_id):
    require_id(plan_id, "plan_id")
    result = read(Path(config["workspace"]) / "plans" / (plan_id + ".json"))
    if result["plan_id"] != plan_id:
        raise ContractError("Plan identity mismatch")
    ctx = load_context(config, result["context_id"])
    steps = validate_proposal(ctx, result["proposal"])
    doc = {"context_id": ctx["context_id"], "proposal": result["proposal"],
           "validated_steps": steps, "candidate_ids": ctx["candidate_ids"]}
    with ProjectStore(Path(config["workspace"]), readonly=True) as store:
        row = store._one("SELECT document,project_id FROM plans WHERE plan_id=?", (plan_id,))
        stored = json.loads(row["document"])
        if row["project_id"] != config["project_id"] or "plan_" + canonical_hash(stored) != plan_id:
            raise ContractError("Stored plan binding mismatch")
        if any(step["inputs"]["proposal_sha256"] != canonical_hash(doc) for step in stored["steps"]):
            raise ContractError("Proposal was altered")
    if result["validated_steps"] != steps or result["candidate_ids"] != ctx["candidate_ids"] or result["mode"] != ctx["mode"]:
        raise ContractError("Plan context mismatch")
    if config.get("decision_facts_version") == 1 and result.get("method_decision_facts") != method_decision_facts(ctx, result["proposal"]):
        raise ContractError("Method facts differ from capability catalog")
    return result, ctx


def execute_available(config, args, origin):
    exact(args, {"plan_id"})
    plan, ctx = load_plan(config, args["plan_id"])
    target = Path(config["workspace"]) / "results" / (args["plan_id"] + ".json")
    methods = {row["id"]: row for row in ctx["methods"]}
    outputs = []
    for step in plan["validated_steps"]:
        if step["action"] == "reuse_evidence":
            outputs.append({"action": step["action"], "status": "COMPLETED_LOCAL",
                "source_hashes": {key: row["source_sha256"] for key, row in ctx["evidence"].items()},
                "candidate_ids": ctx["candidate_ids"], "candidate_count": len(ctx["candidate_ids"]),
                "facts": ctx["evidence"]["geometry"]["facts"],
                "limitations": ctx["evidence"]["geometry"]["limitations"]})
            if "reference_measurements" in ctx["evidence"]:
                outputs[-1]["contact_comparison"] = ctx["evidence"]["reference_measurements"]
                outputs[-1]["interpretation_rules"] = ctx["interpretation_rules"]
            if "short_contact_summary" in ctx["evidence"]["geometry"]:
                outputs[-1]["short_contact_summary"] = ctx["evidence"]["geometry"]["short_contact_summary"]
        elif step["action"] == "prepare_requirements":
            method = methods[step["method_id"]]
            outputs.append({"action": step["action"], "status": "COMPLETED_LOCAL",
                "selected_method": method["id"], "method_state": method["availability"],
                "required_before_execution": method["requirements"],
                "control_evidence": ctx["evidence"]["control_sources"],
                "agent_unknowns": plan["proposal"]["unknowns"],
                "prepared_scientific_inputs": False})
        else:
            outputs.append({"action": step["action"], "method_id": step["method_id"],
                "status": "NOT_RUN", "reason": "Method adapter not integrated",
                "prerequisites": step["prerequisites"]})
    result = {"plan_id": args["plan_id"], "context_id": ctx["context_id"],
        "status": "LOCAL_PREPARATION_COMPLETE" if ctx["mode"] == "full_evaluation" else "EXISTING_EVIDENCE_REUSED",
        "outputs": outputs, "new_scientific_jobs": 0,
        "all_four_candidates_retained": True,
        "remaining_scientific_goal": "Phosphorylation-state discrimination remains unevaluated",
        "scientific_goal_complete": False}
    if config.get("decision_facts_version") == 1:
        result["method_decision_facts"] = method_decision_facts(ctx, plan["proposal"])
    result["receipt_sha256"] = canonical_hash(result)
    # Local, deterministic, idempotent action. No handoff or approval is forged.
    with ProjectStore(Path(config["workspace"])) as store:
        with store._transaction():
            reused = target.exists()
            save_immutable(target, result)
            store._event(config["project_id"], "RESEARCH_LOCAL_ACTION_REUSED" if reused else "RESEARCH_LOCAL_ACTION_COMPLETED", {
                "plan_id": args["plan_id"], "receipt_sha256": result["receipt_sha256"],
                "origin": origin, "artifact": str(target)})
    return result | {"reused": reused, "artifact": str(target)}


def query(config, args, origin):
    exact(args, {"plan_id"})
    plan, _ = load_plan(config, args["plan_id"])
    path = Path(config["workspace"]) / "results" / (args["plan_id"] + ".json")
    result = read(path) if path.exists() else None
    if result:
        body = {k: v for k, v in result.items() if k != "receipt_sha256"}
        if canonical_hash(body) != result["receipt_sha256"] or result["plan_id"] != args["plan_id"]:
            raise ContractError("Result identity mismatch")
    return {"plan": plan, "result": result}


def parse_payload(payload):
    if len(payload) > MAX_BYTES:
        raise ContractError("Request too large")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(payload, object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ContractError("Nonfinite JSON")))


def handle(config_path, operation, payload):
    config = read(config_path)
    request = parse_payload(payload)
    exact(request, {"arguments", "origin"})
    origin = request["origin"]
    exact(origin, {"session_id", "tool_call_id"})
    for key, value in origin.items():
        require_text(value, key, 200)
    operations = {"context": context, "propose": propose, "execute": execute_available, "query": query}
    if operation not in operations:
        raise ContractError("Unknown research operation")
    result = operations[operation](config, request["arguments"], origin)
    return {"schema": SCHEMA, "operation": operation, **result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(handle(args.config, args.operation, sys.stdin.buffer.read(MAX_BYTES + 1)),
                         ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
