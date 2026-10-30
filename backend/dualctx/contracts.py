"""Identity and evidence contracts; scores retain their evaluator's semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re
from typing import Any


class ContractError(ValueError):
    pass


def canonical_hash(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def sequence_hash(sequence: str) -> str:
    if not sequence or not re.fullmatch("[ACDEFGHIKLMNPQRSTVWY]+", sequence):
        raise ContractError("An explicit uppercase, unmodified 20-amino-acid sequence is required")
    return sha256(sequence.encode("ascii")).hexdigest()


def require_digest(value: str, field: str) -> None:
    if not re.fullmatch("[a-f0-9]{64}", value):
        raise ContractError(f"{field} must be a lowercase SHA256 digest")


def require_id(value: str, field: str) -> None:
    if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ContractError(f"Invalid {field}")


@dataclass(frozen=True)
class TargetContext:
    context_id: str
    target_sequence: str
    structure_sha256: str
    residue_map_sha256: str
    chain_id: str = "A"
    role: str = "on_target"

    def __post_init__(self) -> None:
        require_id(self.context_id, "context_id")
        sequence_hash(self.target_sequence)
        require_digest(self.structure_sha256, "structure_sha256")
        require_digest(self.residue_map_sha256, "residue_map_sha256")
        if self.role != "on_target":
            raise ContractError("This prototype supports positive on-target contexts only")
        if not re.fullmatch("[A-Za-z0-9]", self.chain_id):
            raise ContractError("The reviewed PDB adapters require one-character chain IDs")

    @property
    def fingerprint(self) -> str:
        return canonical_hash(asdict(self))


@dataclass(frozen=True)
class Candidate:
    sequence: str
    generation_event_id: str
    generation_method: str
    generation_seed: int
    input_fingerprint: str
    parent_candidate_ids: tuple[str, ...] = ()
    evidence_kind: str = "test_only"

    def __post_init__(self) -> None:
        sequence_hash(self.sequence)
        require_id(self.generation_event_id, "generation_event_id")
        require_id(self.generation_method, "generation_method")
        require_digest(self.input_fingerprint, "input_fingerprint")
        if type(self.generation_seed) is not int or not 0 <= self.generation_seed < 2**32:
            raise ContractError("generation_seed must be an explicit uint32")
        if self.evidence_kind not in {"test_only", "new_run", "historical_import"}:
            raise ContractError("Candidate origin must be explicit")

    @property
    def sequence_sha256(self) -> str:
        return sequence_hash(self.sequence)

    @property
    def candidate_id(self) -> str:
        # Repeated generation of the same sequence is one candidate, with separate events.
        return "binder_" + self.sequence_sha256


@dataclass(frozen=True)
class MetricRule:
    name: str
    direction: str
    threshold: float
    scale: float
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        require_id(self.name, "metric name")
        values = (self.threshold, self.scale, self.minimum, self.maximum)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in values):
            raise ContractError("Metric ranges and thresholds must be finite numbers")
        if self.direction not in {"higher", "lower"} or self.scale <= 0:
            raise ContractError("Metric direction/scale invalid")
        if not self.minimum <= self.threshold <= self.maximum or self.minimum == self.maximum:
            raise ContractError("Threshold outside metric range")

    def margin(self, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ContractError(f"Non-finite/non-numeric {self.name}")
        if not self.minimum <= value <= self.maximum:
            raise ContractError(f"{self.name} outside declared metric range")
        sign = 1 if self.direction == "higher" else -1
        return sign * (value - self.threshold) / self.scale


@dataclass(frozen=True)
class EvaluationProtocol:
    evaluator: str
    revision: str
    settings_fingerprint: str
    technical_seeds: tuple[int, ...]
    metrics: tuple[MetricRule, ...]
    evidence_kind: str = "test_only"
    context_semantics: str = "sequence_cofold"
    aggregate_technical_seeds: str = "median"

    def __post_init__(self) -> None:
        require_id(self.evaluator, "evaluator")
        if not self.revision:
            raise ContractError("Evaluator revision is required")
        require_digest(self.settings_fingerprint, "settings_fingerprint")
        if not self.technical_seeds or len(set(self.technical_seeds)) != len(self.technical_seeds):
            raise ContractError("Unique technical seeds must be frozen")
        if any(type(s) is not int or not 0 <= s < 2**32 for s in self.technical_seeds):
            raise ContractError("technical_seeds must be uint32 values")
        if not self.metrics or len({m.name for m in self.metrics}) != len(self.metrics):
            raise ContractError("Unique metric definitions required")
        if self.evidence_kind not in {"test_only", "new_run", "historical_import"}:
            raise ContractError("Evaluation origin must be explicit")
        if self.context_semantics not in {"sequence_cofold", "structure_conditioned"}:
            raise ContractError("Evaluation context semantics must be explicit")
        if self.aggregate_technical_seeds != "median":
            raise ContractError("Only predeclared median technical-seed aggregation is implemented")

    @property
    def fingerprint(self) -> str:
        return canonical_hash(asdict(self))


@dataclass(frozen=True)
class Evaluation:
    evaluation_id: str
    candidate_id: str
    sequence_sha256: str
    context_id: str
    context_fingerprint: str
    protocol_fingerprint: str
    technical_seed: int
    status: str
    metrics: dict[str, float]
    artifact_sha256: str | None
    evidence_kind: str

    def __post_init__(self) -> None:
        require_id(self.evaluation_id, "evaluation_id")
        require_digest(self.sequence_sha256, "sequence_sha256")
        require_digest(self.context_fingerprint, "context_fingerprint")
        require_digest(self.protocol_fingerprint, "protocol_fingerprint")
        if type(self.technical_seed) is not int or not 0 <= self.technical_seed < 2**32:
            raise ContractError("technical_seed must be uint32")
        if self.status not in {"succeeded", "timeout", "execution_failed", "invalid_output"}:
            raise ContractError("Evaluation status invalid")
        if self.status == "succeeded":
            if self.artifact_sha256 is None:
                raise ContractError("Successful evaluation requires an artifact digest")
            require_digest(self.artifact_sha256, "artifact_sha256")
        elif self.metrics:
            raise ContractError("Execution failures cannot carry scientific metrics")
        if self.evidence_kind not in {"test_only", "new_run", "historical_import"}:
            raise ContractError("Evaluation evidence kind invalid")


@dataclass(frozen=True)
class RepairMask:
    candidate_id: str
    sequence_sha256: str
    context_id: str
    context_fingerprint: str
    parent_complex_sha256: str
    mutable_sequence_positions: tuple[int, ...]
    evidence_refs: tuple[str, ...]
    binder_chain: str = "B"

    def validate(self, candidate: Candidate, context: TargetContext) -> None:
        if (self.candidate_id, self.sequence_sha256) != (candidate.candidate_id, candidate.sequence_sha256):
            raise ContractError("Repair mask does not identify the exact binder sequence")
        if (self.context_id, self.context_fingerprint) != (context.context_id, context.fingerprint):
            raise ContractError("Repair mask belongs to a different target context")
        require_digest(self.parent_complex_sha256, "parent_complex_sha256")
        positions = self.mutable_sequence_positions
        if not positions or len(set(positions)) != len(positions):
            raise ContractError("Repair positions must be nonempty and unique")
        if any(type(p) is not int or not 1 <= p <= len(candidate.sequence) for p in positions):
            raise ContractError("Repair positions are one-based binder SEQUENCE positions")
        if len(positions) >= len(candidate.sequence):
            raise ContractError("Local repair must freeze at least one binder position")
        if not self.evidence_refs:
            raise ContractError("A local mask requires residue-level supporting evidence")
        if not re.fullmatch("[A-Za-z0-9]", self.binder_chain) or self.binder_chain == context.chain_id:
            raise ContractError("Binder and target chain identities must be distinct")

    def validate_child(self, parent: Candidate, child_sequence: str) -> None:
        sequence_hash(child_sequence)
        if self.sequence_sha256 != parent.sequence_sha256 or self.candidate_id != parent.candidate_id:
            raise ContractError("Mask/parent sequence mismatch")
        if len(parent.sequence) != len(child_sequence):
            raise ContractError("Local sequence repair cannot change length")
        mutable = set(self.mutable_sequence_positions)
        changed = {i for i, (a, b) in enumerate(zip(parent.sequence, child_sequence), 1) if a != b}
        if not changed <= mutable:
            raise ContractError("A repaired child mutated a frozen binder position")


@dataclass(frozen=True)
class TaskIntent:
    operation: str
    context_id: str
    context_fingerprint: str
    method: str
    seed: int
    candidate_id: str | None
    consumes_parent: bool
    evidence_refs: tuple[str, ...]
    reason: str
    evidence_kind: str
    re_evaluate_contexts: tuple[str, ...]
    mutable_sequence_positions: tuple[int, ...] = ()
    parent_complex_sha256: str | None = None

    @property
    def task_id(self) -> str:
        # Dispatchers can use this as an idempotency key; this module does not launch jobs.
        return "task_" + canonical_hash(asdict(self))
