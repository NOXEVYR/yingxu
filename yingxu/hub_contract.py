"""Draft contract reference. Not imported by either product; no network or I/O."""
import hashlib
import json
import re

PROTOCOL = "yingxu-aihub-execution/1-draft.1"
MAX_BYTES = 128 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")


class ContractError(ValueError):
    pass


def _integer(token):
    value = int(token)
    if token == "-0" or abs(value) > MAX_SAFE_INTEGER:
        raise ContractError("integer_not_safe")
    return value


def _unsupported_number(_):
    raise ContractError("only_safe_integers")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_key")
        result[key] = value
    return result


def _check(value, depth=0, budget=None):
    if budget is None:
        budget = [4096]
    budget[0] -= 1
    if depth > 20 or budget[0] < 0:
        raise ContractError("structure_limit")
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ContractError("isolated_surrogate")
    elif value is None or isinstance(value, bool):
        pass
    elif type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise ContractError("integer_not_safe")
    elif isinstance(value, list):
        for child in value:
            _check(child, depth + 1, budget)
    elif isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not re.fullmatch(r"[\x20-\x7e]+", key):
                raise ContractError("ascii_object_keys_required")
            _check(child, depth + 1, budget)
    else:
        raise ContractError("only_json_safe_integers")


def parse(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ContractError("body_limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_int=_integer, parse_float=_unsupported_number,
                           parse_constant=_unsupported_number)
        _check(value)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError("invalid_json") from exc


def canonical(value):
    _check(value)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ContractError("body_limit")
    return raw


def digest(value, domain="canonical"):
    if domain not in ("canonical", "input", "intent", "schema"):
        raise ContractError("invalid_digest_domain")
    prefix = ("yingxu-exec/" + domain + "/draft.1\0").encode("ascii")
    return hashlib.sha256(prefix + canonical(value)).hexdigest()


def exact(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ContractError("unexpected_fields")


def identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ContractError("invalid_identity")


def sha(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ContractError("invalid_sha256")


def validate_request(request):
    canonical(request)
    exact(request, "protocol client_id idempotency_key intent")
    if request["protocol"] != PROTOCOL:
        raise ContractError("unsupported_protocol")
    identity(request["client_id"])
    identity(request["idempotency_key"])
    intent = request["intent"]
    exact(intent, "origin service capability grant route inputs input_digest")
    exact(intent["origin"], "authority_id project_id task_id run_id input_version")
    for value in intent["origin"].values():
        identity(value)
    exact(intent["service"], "authority_id workspace_id connection_revision ledger_epoch")
    for value in intent["service"].values():
        identity(value)
    exact(intent["grant"], "id revision")
    for value in intent["grant"].values():
        identity(value)
    exact(intent["capability"], "id expected_declaration_sha256 schema_sha256")
    identity(intent["capability"]["id"])
    sha(intent["capability"]["expected_declaration_sha256"])
    sha(intent["capability"]["schema_sha256"])
    route = intent["route"]
    if type(route) is not dict:
        raise ContractError("invalid_route")
    if route.get("policy") == "exact_client":
        exact(route, "policy client_id")
        identity(route["client_id"])
    elif route.get("policy") == "capability_pool":
        exact(route, "policy pool_id")
        identity(route["pool_id"])
    else:
        raise ContractError("invalid_route")
    inputs = intent["inputs"]
    exact(inputs, "parameters attachments")
    if type(inputs["parameters"]) is not dict or type(inputs["attachments"]) is not list:
        raise ContractError("invalid_inputs")
    if len(inputs["attachments"]) > 32:
        raise ContractError("attachment_limit")
    seen = set()
    for attachment in inputs["attachments"]:
        exact(attachment, "id version sha256 size_bytes mime_type locator")
        identity(attachment["id"])
        identity(attachment["version"])
        sha(attachment["sha256"])
        if attachment["id"] in seen:
            raise ContractError("duplicate_attachment")
        seen.add(attachment["id"])
        if type(attachment["size_bytes"]) is not int or not 0 <= attachment["size_bytes"] <= 512 * 1024**2:
            raise ContractError("attachment_size")
        if not isinstance(attachment["mime_type"], str) or not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", attachment["mime_type"]):
            raise ContractError("mime_type")
        exact(attachment["locator"], "scheme authority_id resource_id version")
        if attachment["locator"]["scheme"] != "origin_resource":
            raise ContractError("unsupported_locator")
        for field in ("authority_id", "resource_id", "version"):
            identity(attachment["locator"][field])
        if attachment["locator"]["authority_id"] != intent["origin"]["authority_id"]:
            raise ContractError("attachment_authority")
    sha(intent["input_digest"])
    if digest(inputs, "input") != intent["input_digest"]:
        raise ContractError("input_digest_mismatch")
    return digest({"protocol": request["protocol"], "client_id": request["client_id"], "intent": intent}, "intent")


def validate_result(result):
    exact(result, "id version name kind mime_type size_bytes sha256 locator")
    identity(result["id"])
    identity(result["version"])
    if not isinstance(result["name"], str) or not 0 < len(result["name"]) <= 200:
        raise ContractError("result_name")
    if result["kind"] not in ("image", "video", "audio", "document", "other"):
        raise ContractError("result_kind")
    if not isinstance(result["mime_type"], str) or not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", result["mime_type"]):
        raise ContractError("mime_type")
    size = result["size_bytes"]
    if size is not None and (type(size) is not int or not 0 <= size <= MAX_SAFE_INTEGER):
        raise ContractError("result_size")
    if result["sha256"] is not None:
        sha(result["sha256"])
    locator = result["locator"]
    exact(locator, "scheme authority_id resource_id version")
    if locator["scheme"] not in ("origin_resource", "hub_result"):
        raise ContractError("unsupported_locator")
    for field in ("authority_id", "resource_id", "version"):
        identity(locator[field])
    canonical(result)
    return result
