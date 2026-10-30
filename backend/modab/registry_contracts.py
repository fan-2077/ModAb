"""Wire contracts for plan-bound legacy records; no model-gateway dependency."""
from dataclasses import dataclass
import json

from dualctx.contracts import ContractError, require_id

REQUEST_SCHEMA = "modab_plan_registry_request_v1"
RESPONSE_SCHEMA = "modab_plan_registry_response_v1"
MAX_REQUEST_BYTES = 4096


@dataclass(frozen=True)
class RegistryRequest:
    operation: str
    project_id: str
    plan_id: str


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ContractError("Non-JSON numeric constant: " + value)


def parse_request(payload: bytes) -> RegistryRequest:
    if len(payload) > MAX_REQUEST_BYTES:
        raise ContractError("Registry request exceeds 4096 bytes")
    try:
        value = json.loads(payload.decode("utf-8"),
                           object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("Invalid UTF-8 JSON request") from exc
    if not isinstance(value, dict) or set(value) != {
            "schema", "operation", "project_id", "plan_id"}:
        raise ContractError("Unexpected or missing registry request fields")
    if value["schema"] != REQUEST_SCHEMA:
        raise ContractError("Unsupported registry request schema")
    if value["operation"] not in ("register", "query"):
        raise ContractError("Unsupported registry operation")
    for key in ("project_id", "plan_id"):
        if not isinstance(value[key], str):
            raise ContractError(key + " must be a string")
        require_id(value[key], key)
    return RegistryRequest(value["operation"], value["project_id"], value["plan_id"])
