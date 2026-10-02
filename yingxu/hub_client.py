"""Opt-in r2 compatible HTTP adapter; explicit operations, no scheduler or DB.

Reuses calls.3's bounded loopback transport. Needs that source on sys.path; not
registered in the product. Owner grants and worker operations are not exposed.
"""
import hashlib
import re
import unicodedata
import uuid

from .hub_prepare import FrozenRequest, PROTOCOL
from .hub_contract import ContractError, canonical, exact, parse, sha
from .ai_providers import HTTPProvider, ProviderError, safe_value

RECEIPT_FIELDS = ("protocol execution_authority_id ledger_epoch execution_id request_id origin input_sha256 "
                  "workspace_binding_revision capability_id declaration_sha256 executor queue_task_id dispatch_state "
                  "provider_state cancel_requested provider_request_id results results_manifest_sha256 result_identity_scope "
                  "outcome evidence_source native_execution_verified_by_hub native_cancel_by_hub materialization_error")
PROVIDER_STATES = {"not_started", "submitting", "running", "uncertain", "succeeded", "failed", "cancelled"}
DISPATCH_STATES = {"materialization_pending", "queued_ready", "claimed", "completed", "cancelled_before_claim"}
TERMINAL = {"succeeded", "failed", "cancelled"}
MIME_TOKEN = r"[A-Za-z0-9!#$%&'*+.^_`|~-]+"
MIME_QUOTED = r'"(?:[\x20-\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*"'
MIME_TYPE = re.compile(MIME_TOKEN + "/" + MIME_TOKEN + r"(?:; *" + MIME_TOKEN + "=(?:" + MIME_TOKEN + "|" + MIME_QUOTED + "))*")


def wire_uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError("format")
    except (ValueError, TypeError, AttributeError):
        raise ContractError("wire_uuid") from None
    return value


def validate_receipt(raw, frozen, execution_id=None, previous=None, known_secret=""):
    data = parse(raw)
    exact(data, RECEIPT_FIELDS)
    body = frozen.body_copy()
    expected = {"protocol": PROTOCOL, "execution_authority_id": frozen.service_authority,
                "ledger_epoch": frozen.ledger_epoch, "request_id": body["request_id"],
                "origin": body["origin"], "input_sha256": body["input_sha256"],
                "workspace_binding_revision": body["workspace_binding_revision"], "capability_id": body["capability_id"],
                "declaration_sha256": body["expected_declaration_sha256"]}
    if any(data.get(k) != value for k, value in expected.items()):
        raise ContractError("receipt_original_binding_mismatch")
    wire_uuid(data["execution_id"])
    wire_uuid(data["queue_task_id"])
    if execution_id is not None and data["execution_id"] != execution_id:
        raise ContractError("receipt_execution_changed")
    exact(data["executor"], "client_id tool")
    if data["executor"]["client_id"] != frozen.expected_client:
        raise ContractError("receipt_executor_changed")
    if not isinstance(data["executor"]["tool"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", data["executor"]["tool"]):
        raise ContractError("receipt_tool_identity")
    if (data["provider_state"] not in PROVIDER_STATES or data["dispatch_state"] not in DISPATCH_STATES
            or type(data["cancel_requested"]) is not bool
            or data["native_execution_verified_by_hub"] is not False or data["native_cancel_by_hub"] is not False
            or data["evidence_source"] not in ("worker_report", "queue_ledger")
            or data["result_identity_scope"] != "execution_authority_id/execution_id/result_id"):
        raise ContractError("receipt_state_contract")
    if data["provider_request_id"] is not None and (not isinstance(data["provider_request_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", data["provider_request_id"])):
        raise ContractError("provider_request_identity")
    state, dispatch = data["provider_state"], data["dispatch_state"]
    before_claim_cancel = dispatch == "cancelled_before_claim"
    if ((before_claim_cancel and (state != "cancelled" or not data["cancel_requested"] or data["provider_request_id"] is not None))
            or (dispatch in ("materialization_pending", "queued_ready") and (state != "not_started" or data["cancel_requested"]))
            or (dispatch == "completed" and state not in TERMINAL)
            or (state == "not_started" and data["provider_request_id"] is not None)
            or (state != "not_started" and not before_claim_cancel and data["provider_request_id"] is None)
            or data["evidence_source"] != ("worker_report" if data["provider_request_id"] else "queue_ledger")):
        raise ContractError("receipt_state_combination")
    # Do not preserve/display arbitrary materialization error text in the public view.
    if data["materialization_error"] is not None and not isinstance(data["materialization_error"], str):
        raise ContractError("materialization_error_type")
    if data["materialization_error"] is not None:
        data["materialization_error"] = "reported_materialization_problem"
    safe_value(data, (known_secret,))
    outcome = data["outcome"]
    if not isinstance(outcome, dict):
        raise ContractError("provider_outcome")
    if before_claim_cancel:
        if outcome:
            raise ContractError("provider_outcome")
    elif state in ("failed", "cancelled") and not outcome:
        raise ContractError("provider_outcome")
    if outcome:
        if data["provider_state"] == "failed":
            exact(outcome, "error_code")
            if not isinstance(outcome["error_code"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", outcome["error_code"]):
                raise ContractError("provider_outcome")
        elif data["provider_state"] == "cancelled":
            exact(outcome, "cancel_evidence")
            exact(outcome["cancel_evidence"], "kind reference")
            reference = outcome["cancel_evidence"]["reference"]
            if (outcome["cancel_evidence"]["kind"] not in ("native_terminal", "never_submitted")
                    or not isinstance(reference, str) or not reference.strip() or len(reference) > 500):
                raise ContractError("provider_outcome")
            # Public provenance retains an equality digest, never an arbitrary
            # worker reference which could contain a private path or URL.
            outcome["cancel_evidence"]["reference"] = "sha256:" + hashlib.sha256(reference.encode("utf-8")).hexdigest()
        else:
            raise ContractError("provider_outcome")
    results = data["results"]
    if type(results) is not list or len(results) > 32:
        raise ContractError("receipt_results_limit")
    seen, mapped = set(), []
    for result in results:
        required = "result_id kind media_type bytes locator"
        exact(result, required + (" sha256" if "sha256" in result else ""))
        remote_id = result["result_id"]
        if (not isinstance(remote_id, str) or not 0 < len(remote_id) <= 200 or remote_id != remote_id.strip()
                or remote_id in (".", "..") or "/" in remote_id or "\\" in remote_id
                or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', remote_id)
                or any(unicodedata.category(char) in {"Cc", "Cs"} for char in remote_id) or remote_id in seen):
            raise ContractError("stable_result_identity")
        seen.add(remote_id)
        if result["kind"] not in ("image", "video", "audio", "document", "other") or type(result["bytes"]) is not int or not 0 <= result["bytes"] <= 2**53-1:
            raise ContractError("result_descriptor")
        if not isinstance(result["media_type"], str) or len(result["media_type"]) > 200 or not MIME_TYPE.fullmatch(result["media_type"]):
            raise ContractError("result_media_type")
        locator = result["locator"]
        if not isinstance(locator, str) or len(locator) > 500 or not re.fullmatch(r"[A-Za-z0-9_/-]+(?:\.[A-Za-z0-9_/-]+)*", locator) or locator.startswith("/") or any(p in ("", ".", "..") for p in locator.split("/")):
            raise ContractError("opaque_locator_only")
        if "sha256" in result:
            sha(result["sha256"])
        mapped.append({"remote_identity": {"authority_id": frozen.service_authority, "execution_id": data["execution_id"], "result_id": remote_id},
                       "description": {"name": remote_id, "kind": result["kind"], "mime_type": result["media_type"],
                                       "size_bytes": result["bytes"], **({"sha256": result["sha256"]} if "sha256" in result else {})},
                       "locator": locator, "source_verification": "declared_sha256_unverified" if "sha256" in result else "manual_source_unverified",
                       "receipt_eligible_size": result["bytes"] <= 512 * 1024**2,
                       "received": False, "reviewed": False})
    if data["provider_state"] == "succeeded":
        if not results or data["results_manifest_sha256"] != hashlib.sha256(canonical(results)).hexdigest():
            raise ContractError("result_manifest_mismatch")
    elif results or data["results_manifest_sha256"] is not None:
        raise ContractError("nonterminal_results_not_supported")
    if previous:
        if previous["execution_id"] != data["execution_id"] or previous["queue_task_id"] != data["queue_task_id"]:
            raise ContractError("receipt_execution_changed")
        if previous["provider_request_id"] and previous["provider_request_id"] != data["provider_request_id"]:
            raise ContractError("provider_request_identity_changed")
        if previous["provider_state"] in ("succeeded", "failed", "cancelled") and previous["provider_state"] != data["provider_state"]:
            raise ContractError("terminal_provider_state_changed")
        if previous["provider_state"] == "succeeded" and previous["results_manifest_sha256"] != data["results_manifest_sha256"]:
            raise ContractError("terminal_result_identity_changed")
        allowed_provider = {"not_started": PROVIDER_STATES, "submitting": PROVIDER_STATES - {"not_started"},
                            "running": {"running", "uncertain"} | TERMINAL,
                            "uncertain": {"running", "uncertain"} | TERMINAL}
        if data["provider_state"] not in allowed_provider.get(previous["provider_state"], {previous["provider_state"]}):
            raise ContractError("provider_state_regressed")
        allowed_dispatch = {"materialization_pending": DISPATCH_STATES, "queued_ready": DISPATCH_STATES - {"materialization_pending"},
                            "claimed": {"claimed", "completed"}, "completed": {"completed"},
                            "cancelled_before_claim": {"cancelled_before_claim"}}
        if data["dispatch_state"] not in allowed_dispatch[previous["dispatch_state"]]:
            raise ContractError("dispatch_state_regressed")
        if previous["cancel_requested"] and not data["cancel_requested"]:
            raise ContractError("cancel_request_regressed")
        if (previous["provider_state"] in ("running", "uncertain") and state == "cancelled"
                and outcome.get("cancel_evidence", {}).get("kind") == "never_submitted"):
            raise ContractError("cancel_evidence_conflicts_with_observed_run")
        if previous["provider_state"] in TERMINAL and previous["outcome"] != data["outcome"]:
            raise ContractError("terminal_outcome_changed")
    return {"receipt": data, "mapped_results": mapped, "auto_received": False, "auto_reviewed": False,
            "cancel_effect": "submission_prevented" if before_claim_cancel else "worker_reported_cancelled" if state == "cancelled" else "requested" if data["cancel_requested"] else "not_requested",
            "native_verified": False}


class AIHubExecutionClient(HTTPProvider):
    max_response = 128 * 1024

    def _request(self, method, suffix, body=None):
        allowed = method == "POST" and suffix in ("/api/execution/accept", "/api/execution/status", "/api/execution/cancel") and isinstance(body, dict)
        if not allowed:
            raise ProviderError("operation_not_allowed", "执行候选仅接受显式提交、原请求核对或取消。")
        return super()._request(method, suffix, body)

    def accept(self, frozen, previous=None):
        if not isinstance(frozen, FrozenRequest):
            raise ContractError("frozen_request_required")
        body = frozen.body_copy()
        if previous is not None:
            body["prior_execution_id"] = wire_uuid(previous["execution_id"])
        raw = self._request("POST", "/api/execution/accept", body)
        return validate_receipt(raw, frozen, previous=previous, known_secret=self.secret)

    def status(self, frozen, execution_id=None, previous=None):
        body = {"_workspace_root": frozen.body_copy()["_workspace_root"]}
        body.update({"execution_id": wire_uuid(execution_id)} if execution_id else {"request_id": frozen.body_copy()["request_id"]})
        raw = self._request("POST", "/api/execution/status", body)
        return validate_receipt(raw, frozen, execution_id, previous, self.secret)

    def cancel(self, frozen, execution_id, previous=None):
        raw = self._request("POST", "/api/execution/cancel", {"_workspace_root": frozen.body_copy()["_workspace_root"], "execution_id": wire_uuid(execution_id)})
        return validate_receipt(raw, frozen, execution_id, previous, self.secret)
