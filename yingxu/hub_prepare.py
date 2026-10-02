"""Pure request preparation for Hub's candidate aihub-execution/1.

No HTTP, environment lookup, DB mutation, submission, polling, receipt or review.
Tested with the actual temporary Hub descriptor; not registered in the product.
Existing readonly selections alone cannot authorize this preparation.
"""
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from .hub_contract import ContractError, canonical, identity, parse, sha, validate_result

PROTOCOL = "aihub-execution/1"
SECRET_FIELD = re.compile(r"(?i)(?:^|[_-])(?:authorization|credential|password|secret|token|api[_-]?key|access[_-]?key)(?:$|[_-])")


def safe_parameters(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
            if SECRET_FIELD.search(normalized_key):
                raise ContractError("private_input_field")
            safe_parameters(child)
    elif isinstance(value, list):
        for child in value:
            safe_parameters(child)


@dataclass(frozen=True)
class FrozenRequest:
    payload: bytes
    service_authority: str
    ledger_epoch: str
    expected_client: str
    original_input_digest: str
    local_request_id: str

    def body_copy(self):
        return parse(self.payload)

    def public_binding(self):
        body = self.body_copy()
        return {"protocol": PROTOCOL, "request_id": body["request_id"], "origin": body["origin"],
                "local_request_id": self.local_request_id,
                "service_authority_id": self.service_authority, "ledger_epoch": self.ledger_epoch,
                "expected_client_id": self.expected_client, "capability_id": body["capability_id"],
                "expected_declaration_sha256": body["expected_declaration_sha256"],
                "sent_input_sha256": body["input_sha256"], "run_input_digest": self.original_input_digest,
                "preparation_only": True, "submitted": False}


def prepare(attempt, selection, descriptor, input_json, *, source_authority,
            input_revision, hub_project, title):
    """Pure preparation from explicitly provided frozen data; not execution proof."""
    for field in ("project_id", "task_id", "run_id", "id", "request_id", "connection_id"):
        identity(attempt[field])
    try:
        wire_request_id = str(uuid.UUID(attempt["request_id"]))
    except (ValueError, AttributeError):
        raise ContractError("request_uuid") from None
    sha(attempt["input_digest"])
    identity(source_authority)
    identity(input_revision)
    if not isinstance(hub_project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", hub_project):
        raise ContractError("hub_project_slug")
    if not isinstance(title, str) or not 0 < len(title) <= 160 or any(ord(c) < 32 for c in title):
        raise ContractError("title")
    for field in ("project_id", "task_id", "run_id", "connection_id", "connection_revision", "input_digest"):
        if selection[field] != attempt[field]:
            raise ContractError("selection_attempt_scope_mismatch")
    snapshot = selection["snapshot"]
    if (descriptor.get("protocol") != PROTOCOL or descriptor.get("schema_version") != 1
            or descriptor.get("accepted_transaction") != "one_collaboration_database_commit"
            or descriptor.get("provider_execution") != "scoped_worker_adapter"
            or descriptor.get("input_digest") != "sha256_exact_input_json_utf8"
            or descriptor.get("input_bytes") != 16000 or descriptor.get("native_cancel") is not False
            or descriptor.get("routing") != "declared_client"):
        raise ContractError("execution_not_enabled")
    for field in ("execution_authority_id", "ledger_epoch"):
        try:
            if str(uuid.UUID(descriptor[field])) != descriptor[field]:
                raise ValueError("noncanonical")
        except (ValueError, TypeError, AttributeError):
            raise ContractError("execution_identity_uninitialized") from None
    if (descriptor["identity"]["service_instance_id"] != snapshot["identity"]["service_instance_id"]
            or descriptor["workspace"]["binding_revision"] != snapshot["workspace"]["binding_revision"]
            or descriptor["workspace_root"] != snapshot["workspace_root"]
            or descriptor["connection_revision"] != snapshot["connection_revision"]):
        raise ContractError("execution_selection_binding_changed")
    root = snapshot["workspace_root"]
    if not isinstance(root, str) or len(root) > 1024 or not (PureWindowsPath(root).is_absolute() or PurePosixPath(root).is_absolute()) or any(ord(c) < 32 for c in root):
        raise ContractError("private_workspace_binding")
    declaration = snapshot["declaration"]
    raw_declaration = declaration["text"].encode("utf-8")
    if len(raw_declaration) > 32768 or hashlib.sha256(raw_declaration).hexdigest() != declaration["sha256"]:
        raise ContractError("declaration_digest_mismatch")
    sha(declaration["sha256"])
    if selection["declaration_sha256"] != declaration["sha256"] or selection["capability_id"] != snapshot["selected"]["id"]:
        raise ContractError("selection_declaration_mismatch")
    if not re.fullmatch(r"[0-9a-f]{32}", selection["capability_id"]):
        raise ContractError("capability_identity")
    client = snapshot["selected"]["client_id"]
    # Existing Hub allows safe Chinese client names. This candidate keeps that scope.
    if not isinstance(client, str) or not 0 < len(client) <= 120 or client != client.strip() or re.search(r'[\\/:*?"<>|\x00-\x1f]', client):
        raise ContractError("publisher_identity")
    if not isinstance(input_json, str):
        raise ContractError("actual_input_required")
    raw_input = input_json.encode("utf-8")
    if len(raw_input) > 16000:
        raise ContractError("actual_input_budget")
    parameters = parse(raw_input)  # Limited draft fixture profile; no silent conversions.
    if type(parameters) is not dict:
        raise ContractError("input_object_required")
    safe_parameters(parameters)
    body = {"protocol": PROTOCOL, "request_id": wire_request_id,
            "origin": {"authority_id": source_authority, "project_id": attempt["project_id"],
                       "task_id": attempt["task_id"], "run_id": attempt["run_id"],
                       "call_id": attempt["id"], "input_revision": input_revision},
            "_workspace_root": root, "workspace_binding_revision": snapshot["workspace"]["binding_revision"],
            "execution_authority_id": descriptor["execution_authority_id"], "ledger_epoch": descriptor["ledger_epoch"],
            "connection_revision": snapshot["connection_revision"], "capability_id": selection["capability_id"],
            "expected_declaration_sha256": declaration["sha256"], "input_json": input_json,
            "input_sha256": hashlib.sha256(raw_input).hexdigest(), "hub_project": hub_project, "title": title}
    return FrozenRequest(canonical(body), descriptor["execution_authority_id"], descriptor["ledger_epoch"], client, attempt["input_digest"], attempt["request_id"])


def map_result(frozen, execution_id, result):
    """Stable remote identity alongside description, not a local receipt/result_id.

    This function maps the SYNTHETIC reference-model result descriptor only.
    Actual Hub r1 responses use execution_client_candidate.validate_receipt.
    A caller must separately retain immutable observed identities and validate the
    execution/request/fingerprint response before using this mapping.
    """
    identity(execution_id)
    validate_result(result)
    expected = frozen.service_authority if result["locator"]["scheme"] == "hub_result" else frozen.body_copy()["origin"]["authority_id"]
    if result["locator"]["authority_id"] != expected:
        raise ContractError("result_authority_mismatch")
    return {"remote_identity": {"authority_id": frozen.service_authority, "execution_id": execution_id,
                                "result_id": result["id"], "result_version": result["version"]},
            "descriptor": json.loads(canonical(result)), "receipt_state": "not_received", "review_state": "not_reviewed"}
