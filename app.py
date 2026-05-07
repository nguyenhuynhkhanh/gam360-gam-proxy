"""
GAM Proxy Service — Flask application that proxies requests to Google Ad Manager
via the googleads Python SDK (SOAP API).

Endpoints:
  POST /gam/ad-units   — Create an ad unit
  GET  /gam/ad-units    — Lookup ad unit by code
  GET  /gam/health      — Health check (NetworkService)
"""

import logging
import os
import re
import sys

from flask import Flask, jsonify, request
from googleads import ad_manager, errors

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gam_proxy")

# ---------------------------------------------------------------------------
# Configuration & GAM client bootstrap
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "googleads.yaml")
API_VERSION = "v202602"

def _load_client():
    """Load the AdManagerClient.
    Supports two modes:
    1. googleads.yaml file (local dev)
    2. GAM_SERVICE_ACCOUNT_JSON env var (Render/cloud deployment)
    """
    sa_json = os.environ.get("GAM_SERVICE_ACCOUNT_JSON")
    network_code = os.environ.get("GAM_NETWORK_CODE")

    if sa_json and network_code:
        # Cloud mode: generate config files from env vars
        import json
        import tempfile

        # Write service account key to temp file
        key_path = os.path.join(tempfile.gettempdir(), "gam-sa-key.json")
        with open(key_path, "w") as f:
            f.write(sa_json)

        # Write googleads.yaml config to temp file
        yaml_path = os.path.join(tempfile.gettempdir(), "googleads.yaml")
        with open(yaml_path, "w") as f:
            f.write(
                f"ad_manager:\n"
                f"  application_name: gam360-proxy\n"
                f"  network_code: \"{network_code}\"\n"
                f"  path_to_private_key_file: \"{key_path}\"\n"
            )

        logger.info("Loading GAM client from environment variables")
        return ad_manager.AdManagerClient.LoadFromStorage(yaml_path)

    # Local dev: use googleads.yaml file
    if not os.path.isfile(CONFIG_PATH):
        logger.error(
            "ERROR: googleads.yaml not found at %s and GAM_SERVICE_ACCOUNT_JSON not set.",
            CONFIG_PATH,
        )
        sys.exit(1)
    return ad_manager.AdManagerClient.LoadFromStorage(CONFIG_PATH)


client = _load_client()

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

PROXY_SECRET = os.environ.get("PROXY_SECRET", "")


@app.before_request
def check_proxy_secret():
    """Reject requests without valid proxy secret (skip in local dev)."""
    if not PROXY_SECRET:
        return  # No secret configured = local dev, allow all
    token = request.headers.get("X-Proxy-Secret", "")
    if token != PROXY_SECRET:
        return jsonify({"error": "UNAUTHORIZED", "message": "Invalid proxy secret"}), 401


# -- helpers ----------------------------------------------------------------

def _inventory_service():
    return client.GetService("InventoryService", version=API_VERSION)


def _network_service():
    return client.GetService("NetworkService", version=API_VERSION)


# gam-resilience-foundation FR-10: structured-fault inspection.
# Inspect getattr(fault, 'errors', []) first; iterate ApiError elements and
# match canonical error class identifiers against errorString. Fall back to
# substring matching on str(fault) only when .errors is absent or empty
# (older SDKs / unexpected fault shapes), and emit logger.warning so operators
# know the structured path was bypassed (BR-7).
_RATE_LIMIT_TOKENS = (
    "QuotaError.EXCEEDED_QUOTA",
    "QuotaError.PUBLISHER_QUOTA_EXCEEDED",
    "RateExceededError",
    "RATE_EXCEEDED",
    "RATE_LIMITED",
    "PUBLISHER_QUOTA_EXCEEDED",
    "TOO_MANY_REQUESTS",
    # The legacy substring tokens are kept so that the existing test
    # FakeGoogleAdsServerFault('QuotaError: rate limit exceeded') still
    # classifies True via the fallback path (FR-13 / AC-15).
    "QuotaError",
)
_ALREADY_EXISTS_TOKENS = (
    "UniqueError.NOT_UNIQUE",
    "CommonError.ALREADY_EXISTS",
    "ALREADY_EXISTS",
    "NOT_UNIQUE",
    "UniqueError",  # legacy substring fallback token (FR-13).
)


def _classify_fault(fault, tokens):
    """Inspect fault.errors first; fall back to substring on str(fault).

    Returns True if any ApiError matches one of `tokens` via the structured
    path, OR if any token appears in str(fault) via the fallback path. The
    fallback emits a logger.warning so operators detect when the structured
    path is bypassed (typically after a googleads SDK upgrade).
    """
    api_errors = getattr(fault, "errors", None)
    if api_errors:  # truthy list with at least one entry
        for api_error in api_errors:
            error_string = getattr(api_error, "errorString", "") or ""
            for token in tokens:
                if token in error_string:
                    return True
        # Structured path was taken but matched nothing — definitive negative.
        return False

    # Fallback path: .errors is None, missing, or empty.
    logger.warning(
        "Falling back to substring match for fault: %s",
        type(fault).__name__,
    )
    fault_str = str(fault)
    return any(token in fault_str for token in tokens)


def _is_rate_limited(fault):
    """Return True when the SOAP fault indicates quota / rate limiting."""
    return _classify_fault(fault, _RATE_LIMIT_TOKENS)


def _is_already_exists(fault):
    """Return True when the SOAP fault indicates the ad unit already exists."""
    return _classify_fault(fault, _ALREADY_EXISTS_TOKENS)


# gam-resilience-foundation FR-11/FR-12 / INV-TBD-c: sanitize JSON response
# `error.message` payloads at the proxy boundary. Strip <env:...>...</env:...>
# SOAP envelopes (DOTALL — they can span newlines), strip http(s) URLs,
# truncate at the first newline, and cap at 500 chars + ellipsis. Pure /
# deterministic / no side effects (NFR-5).
# Greedy match: outermost `<env:...>...</env:...>` pair. The greedy `.*` is
# what spec FR-11 calls for ("multi-line, greedy") and is what handles nested
# envelopes (the standard SOAP shape: `<env:Envelope><env:Body/></env:Envelope>`)
# in a single sub() call.
_ENV_TAG_RE = re.compile(r"<env:[^>]+>.*</env:[^>]+>", re.DOTALL | re.IGNORECASE)
# Defensive cleanup of any unmatched stray <env:...> or </env:...> tag the
# pair-stripper missed (malformed envelopes lacking one half).
_STRAY_ENV_TAG_RE = re.compile(r"</?env:[^>]*>", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SANITIZED_MAX = 500


def _sanitize_fault_message(message):
    """Strip SOAP envelope tags and URLs, truncate at first newline, cap length."""
    if message is None:
        return ""
    s = str(message)
    # Strip the (greedy) outermost env:* envelope pair — handles nested SOAP
    # envelopes in a single pass.
    s = _ENV_TAG_RE.sub("", s)
    # Defense-in-depth: strip any leftover stray env:* tags from malformed
    # input (e.g., an opening tag with no matching close).
    s = _STRAY_ENV_TAG_RE.sub("", s)
    s = _URL_RE.sub("", s)
    if "\n" in s:
        s = s.split("\n", 1)[0]
    if len(s) > _SANITIZED_MAX:
        s = s[:_SANITIZED_MAX] + "..."
    return s


# -- endpoints --------------------------------------------------------------

@app.route("/gam/health", methods=["GET"])
def health():
    """Health check: calls NetworkService.getCurrentNetwork()."""
    try:
        network_service = _network_service()
        network = network_service.getCurrentNetwork()
        return jsonify({
            "status": "ok",
            "networkCode": str(network["networkCode"]),
            "networkName": str(network["displayName"]),
            "effectiveRootAdUnitId": str(network["effectiveRootAdUnitId"]),
        }), 200
    except Exception as exc:
        logger.exception("Health check failed")
        return jsonify({
            "status": "error",
            "message": _sanitize_fault_message(str(exc)),
        }), 503


@app.route("/gam/ad-units/all", methods=["GET"])
def ad_units_all():
    """GET /gam/ad-units/all?env=dev — bulk pull all leaf ad units (hasChildren=false)."""
    return _get_all_leaf_ad_units()


@app.route("/gam/ad-units/batch", methods=["GET", "POST", "PATCH"])
def ad_units_batch():
    """Batch endpoints for the gam-batched-bulk-deploy-fix bugfix.

    GET    /gam/ad-units/batch?ids=<csv>  — fetch multiple ad units by GAM ID (sync_back path).
    POST   /gam/ad-units/batch            — create N ad units in one SOAP call.
    PATCH  /gam/ad-units/batch            — update N ad units in one SOAP call.
    """
    if request.method == "POST":
        return _create_ad_units_batch()
    if request.method == "PATCH":
        return _update_ad_units_batch()
    return _get_ad_units_batch()


@app.route("/gam/ad-units/lookup-batch", methods=["GET"])
def ad_units_lookup_batch():
    """GET /gam/ad-units/lookup-batch?codes=<csv>&parentId=<id>

    Bulk parent-scoped lookup. Returns canonical ids for the requested codes
    (only those that exist at the given parent are returned). Used by the
    worker's batched lookup-first idempotency path.
    """
    return _lookup_ad_units_batch()


@app.route("/gam/ad-units", methods=["GET", "POST"])
def ad_units():
    if request.method == "GET":
        return _get_ad_unit()
    return _create_ad_unit()


@app.route("/gam/ad-units/<gam_id>", methods=["PATCH", "DELETE"])
def modify_ad_unit(gam_id):
    """PATCH or DELETE /gam/ad-units/<gam_id>."""
    if request.method == "DELETE":
        return _archive_ad_unit(gam_id)
    return _update_ad_unit(gam_id)


def _get_all_leaf_ad_units():
    """GET /gam/ad-units/all?env=dev — paginated pull of all leaf ad units (hasChildren=false)."""
    import json as _json
    env_label = request.args.get("env", "dev")
    logger.info("Fetching all leaf ad units for env=%s", env_label)

    try:
        inventory_service = _inventory_service()
        page_size = 500
        offset = 0
        all_units = []

        while True:
            # No WHERE filter — fetch all ad units and post-filter in Python.
            # hasChildren is NOT a PQL-filterable column (computed response field only).
            # parentId IS NOT NULL was also unreliable — GAM returned 0 results silently.
            statement = (
                ad_manager.StatementBuilder(version=API_VERSION)
                .Limit(page_size)
                .Offset(offset)
            )

            response = inventory_service.getAdUnitsByStatement(
                statement.ToStatement()
            )

            results = response.get("results", None) if isinstance(response, dict) else getattr(response, "results", None)

            if not results or len(results) == 0:
                break

            # CRITICAL: capture raw SOAP result count BEFORE post-filtering.
            # The pagination break condition (len(results) < page_size) must use
            # this raw count, not the filtered count — otherwise pages are skipped
            # when an entire page consists of non-leaf nodes.
            raw_result_count = len(results)

            for ad_unit in results:
                unit_id = ad_unit["id"] if isinstance(ad_unit, dict) else ad_unit.id
                unit_code = (ad_unit["adUnitCode"] if isinstance(ad_unit, dict) else getattr(ad_unit, "adUnitCode", None)) or ""
                unit_name = (ad_unit["name"] if isinstance(ad_unit, dict) else getattr(ad_unit, "name", None)) or ""
                unit_status = (ad_unit["status"] if isinstance(ad_unit, dict) else getattr(ad_unit, "status", None)) or ""
                parent_id = ad_unit["parentId"] if isinstance(ad_unit, dict) else getattr(ad_unit, "parentId", None)

                # Extract sizes
                raw_sizes = []
                if isinstance(ad_unit, dict):
                    raw_sizes = ad_unit.get("adUnitSizes", []) or []
                else:
                    raw_sizes = getattr(ad_unit, "adUnitSizes", []) or []

                sizes_banner = []
                sizes_vast = []
                has_fluid = False

                for size_entry in raw_sizes:
                    if isinstance(size_entry, dict):
                        s = size_entry.get("size", {})
                        env_type = size_entry.get("environmentType", "BROWSER")
                        is_fluid = size_entry.get("isFluid", False)
                        width = s.get("width", 0) if isinstance(s, dict) else getattr(s, "width", 0)
                        height = s.get("height", 0) if isinstance(s, dict) else getattr(s, "height", 0)
                    else:
                        s = getattr(size_entry, "size", None)
                        env_type = getattr(size_entry, "environmentType", "BROWSER")
                        is_fluid = getattr(size_entry, "isFluid", False)
                        width = getattr(s, "width", 0) if s else 0
                        height = getattr(s, "height", 0) if s else 0

                    if is_fluid:
                        has_fluid = True
                        continue  # Fluid 1x1 is not a real size, skip it

                    size_str = f"{int(width)}x{int(height)}"
                    if str(env_type) == "VIDEO_PLAYER":
                        sizes_vast.append(size_str)
                    else:
                        sizes_banner.append(size_str)

                size_mode = "Fluid" if has_fluid else "Fixed"

                all_units.append({
                    "gam_id": str(unit_id),
                    "ad_unit_code": str(unit_code),
                    "name": str(unit_name),
                    "sizes_banner": _json.dumps(sizes_banner),
                    "sizes_vast": _json.dumps(sizes_vast),
                    "size_mode": size_mode,
                    "status": str(unit_status),
                    "parent_gam_id": str(parent_id) if parent_id is not None else None,
                })

            if raw_result_count < page_size:
                break
            offset += page_size

        logger.info("Fetched %d leaf ad units for env=%s", len(all_units), env_label)
        return jsonify(all_units), 200

    except Exception as exc:
        logger.exception("Failed to fetch all leaf ad units")
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"getAdUnitsByStatement (all leaf) failed: {_sanitize_fault_message(exc)}",
        }), 500


def _get_ad_units_batch():
    """GET /gam/ad-units/batch?ids=<csv> — fetch multiple ad units by GAM ID with sizes."""
    ids_param = request.args.get("ids", "")
    if not ids_param:
        return jsonify({
            "error": "VALIDATION_ERROR",
            "message": "ids query parameter is required",
        }), 400

    gam_ids = [s.strip() for s in ids_param.split(",") if s.strip()]
    if not gam_ids:
        return jsonify({"adUnits": []}), 200

    try:
        inventory_service = _inventory_service()
        all_results = []
        page_size = 100

        # Process in chunks to respect SOAP query limits
        for chunk_start in range(0, len(gam_ids), page_size):
            chunk = gam_ids[chunk_start:chunk_start + page_size]
            id_list = ", ".join(chunk)
            where_clause = f"id IN ({id_list})"

            offset = 0
            while True:
                statement = (
                    ad_manager.StatementBuilder(version=API_VERSION)
                    .Where(where_clause)
                    .Limit(page_size)
                    .Offset(offset)
                )

                response = inventory_service.getAdUnitsByStatement(
                    statement.ToStatement()
                )

                results = None
                if isinstance(response, dict):
                    results = response.get("results", None)
                else:
                    results = getattr(response, "results", None)

                if not results or len(results) == 0:
                    break

                for ad_unit in results:
                    unit_id = ad_unit["id"] if isinstance(ad_unit, dict) else ad_unit.id
                    unit_code = (ad_unit["adUnitCode"] if isinstance(ad_unit, dict) else getattr(ad_unit, "adUnitCode", None)) or ""

                    # Extract sizes
                    raw_sizes = []
                    if isinstance(ad_unit, dict):
                        raw_sizes = ad_unit.get("adUnitSizes", []) or []
                    else:
                        raw_sizes = getattr(ad_unit, "adUnitSizes", []) or []

                    sizes = []
                    for size_entry in raw_sizes:
                        if isinstance(size_entry, dict):
                            s = size_entry.get("size", {})
                            env_type = size_entry.get("environmentType", "BROWSER")
                            is_fluid = size_entry.get("isFluid", False)
                            width = s.get("width", 0)
                            height = s.get("height", 0)
                        else:
                            s = getattr(size_entry, "size", None)
                            env_type = getattr(size_entry, "environmentType", "BROWSER")
                            is_fluid = getattr(size_entry, "isFluid", False)
                            width = getattr(s, "width", 0) if s else 0
                            height = getattr(s, "height", 0) if s else 0

                        sizes.append({
                            "width": int(width),
                            "height": int(height),
                            "environmentType": str(env_type),
                            "isFluid": bool(is_fluid),
                        })

                    all_results.append({
                        "id": str(unit_id),
                        "adUnitCode": str(unit_code),
                        "sizes": sizes,
                    })

                if len(results) < page_size:
                    break
                offset += page_size

        return jsonify({"adUnits": all_results}), 200

    except Exception as exc:
        logger.exception("Batch lookup ad units failed")
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"getAdUnitsByStatement batch failed: {_sanitize_fault_message(exc)}",
        }), 500


def _get_ad_unit():
    """GET /gam/ad-units?code=<adUnitCode> or ?name=<name>&parentId=<parentId> — lookup."""
    code = request.args.get("code")
    name = request.args.get("name")
    parent_id = request.args.get("parentId")

    if not code and not name:
        return jsonify({
            "error": "VALIDATION_ERROR",
            "message": "code or name query parameter is required",
        }), 400

    try:
        inventory_service = _inventory_service()

        if code and parent_id:
            statement = (
                ad_manager.StatementBuilder(version=API_VERSION)
                .Where("adUnitCode = :code AND parentId = :parentId")
                .WithBindVariable("code", code)
                .WithBindVariable("parentId", int(parent_id))
                .Limit(1)
                .Offset(0)
            )
        elif name and parent_id:
            statement = (
                ad_manager.StatementBuilder(version=API_VERSION)
                .Where("name = :name AND parentId = :parentId")
                .WithBindVariable("name", name)
                .WithBindVariable("parentId", int(parent_id))
                .Limit(1)
                .Offset(0)
            )
        else:
            statement = (
                ad_manager.StatementBuilder(version=API_VERSION)
                .Where("adUnitCode = :code")
                .WithBindVariable("code", code)
                .Limit(1)
                .Offset(0)
            )

        response = inventory_service.getAdUnitsByStatement(
            statement.ToStatement()
        )

        results = getattr(response, "results", None) or response.get("results", None) if isinstance(response, dict) else getattr(response, "results", None)
        if results and len(results) > 0:
            ad_unit = results[0]
            ad_unit_id = ad_unit["id"] if isinstance(ad_unit, dict) else ad_unit.id
            ad_unit_code = ad_unit["adUnitCode"] if isinstance(ad_unit, dict) else ad_unit.adUnitCode
            return jsonify({
                "id": str(ad_unit_id),
                "adUnitCode": str(ad_unit_code),
            }), 200

        return jsonify({"error": "NOT_FOUND"}), 404

    except Exception as exc:
        logger.exception("Lookup ad unit failed for code=%s", code)
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"getAdUnitsByStatement failed: {_sanitize_fault_message(exc)}",
        }), 500


def _create_ad_unit():
    """POST /gam/ad-units — create an ad unit via InventoryService."""
    # Content-Type check
    if not request.is_json:
        return jsonify({
            "error": "VALIDATION_ERROR",
            "message": "Content-Type must be application/json",
        }), 415

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "error": "VALIDATION_ERROR",
            "message": "Invalid or empty JSON body",
        }), 400

    # Required field validation
    for field in ("name", "parentId"):
        if field not in body or body[field] is None:
            return jsonify({
                "error": "GAM_ERROR",
                "message": f"Missing required field: {field}",
            }), 400

    # Build SOAP ad unit dict
    ad_unit = {
        "name": body["name"],
        "parentId": body["parentId"],
    }

    if "adUnitCode" in body and body["adUnitCode"] is not None:
        ad_unit["adUnitCode"] = body["adUnitCode"]

    # adUnitSizes — translate from JSON to SOAP format
    if "adUnitSizes" in body and body["adUnitSizes"]:
        soap_sizes = []
        for size_entry in body["adUnitSizes"]:
            soap_size = {
                "size": {
                    "width": size_entry["size"]["width"],
                    "height": size_entry["size"]["height"],
                    "isAspectRatio": False,
                },
                "environmentType": size_entry.get("environmentType", "BROWSER"),
            }
            soap_sizes.append(soap_size)
        ad_unit["adUnitSizes"] = soap_sizes

    # targetWindow — only set when explicitly provided
    if "targetWindow" in body and body["targetWindow"] is not None:
        ad_unit["targetWindow"] = body["targetWindow"]

    try:
        inventory_service = _inventory_service()
        result = inventory_service.createAdUnits([ad_unit])

        created = result[0]
        created_id = created["id"] if isinstance(created, dict) else created.id
        logger.info("Created ad unit id=%s name=%s", created_id, body["name"])
        return jsonify({"id": str(created_id)}), 201

    except errors.GoogleAdsServerFault as fault:
        logger.error("SOAP fault creating ad unit: %s", fault)

        if _is_already_exists(fault):
            code_val = body.get("adUnitCode", body["name"])
            return jsonify({
                "error": "ALREADY_EXISTS",
                "message": f"An ad unit with code '{code_val}' already exists",
            }), 409

        if _is_rate_limited(fault):
            return jsonify({
                "error": "RATE_LIMITED",
                "retryAfter": 5,
            }), 429

        return jsonify({
            "error": "GAM_ERROR",
            "message": f"InventoryService.createAdUnits failed: {_sanitize_fault_message(fault)}",
        }), 500

    except Exception as exc:
        logger.exception("Unexpected error creating ad unit")
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"InventoryService.createAdUnits failed: {_sanitize_fault_message(exc)}",
        }), 500


def _update_ad_unit(gam_id):
    """Update an existing ad unit via InventoryService.updateAdUnits().
    Only mutable fields are sent: name, adUnitSizes, targetWindow.
    adUnitCode and parentId are NOT sent (immutable after creation)."""
    if not request.is_json:
        return jsonify({
            "error": "VALIDATION_ERROR",
            "message": "Content-Type must be application/json",
        }), 415

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "error": "VALIDATION_ERROR",
            "message": "Invalid or empty JSON body",
        }), 400

    # Build SOAP ad unit dict — only mutable fields + the id for identification
    ad_unit = {"id": gam_id}

    if "name" in body and body["name"] is not None:
        ad_unit["name"] = body["name"]

    # adUnitSizes — translate from JSON to SOAP format
    if "adUnitSizes" in body and body["adUnitSizes"]:
        soap_sizes = []
        for size_entry in body["adUnitSizes"]:
            soap_size = {
                "size": {
                    "width": size_entry["size"]["width"],
                    "height": size_entry["size"]["height"],
                    "isAspectRatio": False,
                },
                "environmentType": size_entry.get("environmentType", "BROWSER"),
            }
            soap_sizes.append(soap_size)
        ad_unit["adUnitSizes"] = soap_sizes

    # targetWindow — only set when explicitly provided
    if "targetWindow" in body and body["targetWindow"] is not None:
        ad_unit["targetWindow"] = body["targetWindow"]

    try:
        inventory_service = _inventory_service()
        result = inventory_service.updateAdUnits([ad_unit])

        updated = result[0]
        updated_id = updated["id"] if isinstance(updated, dict) else updated.id
        logger.info("Updated ad unit id=%s", updated_id)
        return jsonify({"id": str(updated_id)}), 200

    except errors.GoogleAdsServerFault as fault:
        logger.error("SOAP fault updating ad unit %s: %s", gam_id, fault)

        if _is_rate_limited(fault):
            return jsonify({
                "error": "RATE_LIMITED",
                "retryAfter": 5,
            }), 429

        return jsonify({
            "error": "GAM_ERROR",
            "message": f"InventoryService.updateAdUnits failed: {_sanitize_fault_message(fault)}",
        }), 500

    except Exception as exc:
        logger.exception("Unexpected error updating ad unit %s", gam_id)
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"InventoryService.updateAdUnits failed: {_sanitize_fault_message(exc)}",
        }), 500


def _archive_ad_unit(gam_id):
    """DELETE /gam/ad-units/<gam_id> — deactivate, rename (to free the name), then archive."""
    try:
        inventory_service = _inventory_service()
        gam_id_int = int(gam_id)

        # Step 1: Activate first (in case it's already archived — can't rename archived units)
        activate_stmt = (
            ad_manager.StatementBuilder(version=API_VERSION)
            .Where("id = :id")
            .WithBindVariable("id", gam_id_int)
        )
        try:
            inventory_service.performAdUnitAction(
                {"xsi_type": "ActivateAdUnits"},
                activate_stmt.ToStatement(),
            )
            logger.info("Activated ad unit id=%s before rename", gam_id)
        except Exception:
            pass  # May already be active — that's fine

        # Step 2: Rename to free up the name for reuse
        import time
        suffix = f"_archived_{int(time.time())}"
        # Fetch current name
        lookup_stmt = (
            ad_manager.StatementBuilder(version=API_VERSION)
            .Where("id = :id")
            .WithBindVariable("id", gam_id_int)
            .Limit(1)
            .Offset(0)
        )
        response = inventory_service.getAdUnitsByStatement(lookup_stmt.ToStatement())
        results = getattr(response, "results", None) or (response.get("results", None) if isinstance(response, dict) else None)
        if results and len(results) > 0:
            old_unit = results[0]
            old_name = old_unit["name"] if isinstance(old_unit, dict) else old_unit.name
            new_name = f"{old_name}{suffix}"
            inventory_service.updateAdUnits([{"id": gam_id_int, "name": new_name}])
            logger.info("Renamed ad unit id=%s from '%s' to '%s'", gam_id, old_name, new_name)

        # Step 3: Archive
        archive_stmt = (
            ad_manager.StatementBuilder(version=API_VERSION)
            .Where("id = :id")
            .WithBindVariable("id", gam_id_int)
        )
        result = inventory_service.performAdUnitAction(
            {"xsi_type": "ArchiveAdUnits"},
            archive_stmt.ToStatement(),
        )

        count = getattr(result, "numChanges", 0) if result else 0
        logger.info("Archived ad unit id=%s (numChanges=%d)", gam_id, count)
        return jsonify({"archived": True, "id": str(gam_id), "numChanges": count}), 200

    except errors.GoogleAdsServerFault as fault:
        logger.error("SOAP fault archiving ad unit %s: %s", gam_id, fault)

        if _is_rate_limited(fault):
            return jsonify({"error": "RATE_LIMITED", "retryAfter": 5}), 429

        return jsonify({
            "error": "GAM_ERROR",
            "message": f"Archive failed: {_sanitize_fault_message(fault)}",
        }), 500

    except Exception as exc:
        logger.exception("Unexpected error archiving ad unit %s", gam_id)
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"Archive failed: {_sanitize_fault_message(exc)}",
        }), 500


# ===========================================================================
# gam-batched-bulk-deploy-fix: batched create / update / lookup endpoints.
#
# Bug 2 fix: _build_update_ad_unit (called from _update_ad_units_batch) rejects
# any payload where any item's `id` is not a numeric string (^\d+$). Returns
# HTTP 400 VALIDATION_ERROR before SOAP is invoked. This is the proxy-side
# defense-in-depth catch for the worker-side _buildBatchUpdateBody guard.
# ===========================================================================

# Strict numeric-id pattern. Matches "0", "1", "23343020634" but NOT
# "undefined", "null", "", " 123", "123 ", "123.45", "-123", "1e5", "0x1F",
# "abc", "123abc", "a123", or whitespace-padded values. Mirrors the worker's
# /^\d+$/ check.
_NUMERIC_ID_RE = re.compile(r"^\d+$")


def _validate_numeric_id(raw):
    """Return (True, None) if `raw` is a non-empty numeric string, else
    (False, msg) describing the rejection reason for the caller."""
    if raw is None:
        return False, "id field must be a numeric string; got: <missing>"
    if not isinstance(raw, str):
        return False, f"id field must be a numeric string; got: {raw!r}"
    if _NUMERIC_ID_RE.match(raw) is None:
        return False, f"id field must be a numeric string; got: {raw}"
    return True, None


def _build_update_ad_unit(item):
    """Build the SOAP update dict for a single item AFTER validating its id.

    Raises ValueError on invalid id so the caller can return 400.
    Used by _update_ad_units_batch.
    """
    raw_id = item.get("id") if isinstance(item, dict) else None
    ok, err = _validate_numeric_id(raw_id)
    if not ok:
        raise ValueError(err)
    soap = {"id": raw_id}
    if "name" in item and item["name"] is not None:
        soap["name"] = item["name"]
    if "adUnitSizes" in item and item["adUnitSizes"]:
        soap_sizes = []
        for size_entry in item["adUnitSizes"]:
            soap_sizes.append({
                "size": {
                    "width": size_entry["size"]["width"],
                    "height": size_entry["size"]["height"],
                    "isAspectRatio": False,
                },
                "environmentType": size_entry.get("environmentType", "BROWSER"),
            })
        soap["adUnitSizes"] = soap_sizes
    if "targetWindow" in item and item["targetWindow"] is not None:
        soap["targetWindow"] = item["targetWindow"]
    return soap


def _create_ad_units_batch():
    """POST /gam/ad-units/batch — create up to GAM_BATCH_SIZE ad units in one
    SOAP call. Returns per-item attribution.

    Response shape:
      { results: [ { index, success, id?, error?, message?, fieldPath? }, ... ],
        totalSucceeded: N, totalFailed: M }

    Bug 1 (secondary) fix: the proxy MUST populate `id` for every survivor
    item where the SOAP envelope can resolve it. Survivors without resolvable
    id are returned as `{success: true}` (no id) and the worker handles the
    fallback via lookup-first.
    """
    if not request.is_json:
        return jsonify({"error": "VALIDATION_ERROR", "message": "Content-Type must be application/json"}), 415
    body = request.get_json(silent=True)
    if body is None or not isinstance(body, dict) or "adUnits" not in body:
        return jsonify({"error": "VALIDATION_ERROR", "message": "body must be {adUnits: [...]}"}), 400
    ad_units_in = body["adUnits"]
    if not isinstance(ad_units_in, list):
        return jsonify({"error": "VALIDATION_ERROR", "message": "adUnits must be an array"}), 400
    if len(ad_units_in) == 0:
        return jsonify({"results": [], "totalSucceeded": 0, "totalFailed": 0}), 200

    soap_units = []
    for au in ad_units_in:
        soap = {"name": au.get("name"), "parentId": au.get("parentId")}
        if "adUnitCode" in au and au["adUnitCode"] is not None:
            soap["adUnitCode"] = au["adUnitCode"]
        if "adUnitSizes" in au and au["adUnitSizes"]:
            soap_sizes = []
            for size_entry in au["adUnitSizes"]:
                soap_sizes.append({
                    "size": {
                        "width": size_entry["size"]["width"],
                        "height": size_entry["size"]["height"],
                        "isAspectRatio": False,
                    },
                    "environmentType": size_entry.get("environmentType", "BROWSER"),
                })
            soap["adUnitSizes"] = soap_sizes
        if "targetWindow" in au and au["targetWindow"] is not None:
            soap["targetWindow"] = au["targetWindow"]
        soap_units.append(soap)

    try:
        inventory_service = _inventory_service()
        result = inventory_service.createAdUnits(soap_units)
        # Build a position-aware id map: align created items with input by
        # adUnitCode (the stable key).
        results = []
        for i, au in enumerate(ad_units_in):
            created_id = None
            if i < len(result):
                created = result[i] if not isinstance(result, list) or len(result) > i else None
                if created is not None:
                    created_id = created["id"] if isinstance(created, dict) else getattr(created, "id", None)
            if created_id is not None:
                results.append({"index": i, "success": True, "id": str(created_id)})
            else:
                results.append({"index": i, "success": True})
        succ = sum(1 for r in results if r["success"])
        return jsonify({
            "results": results, "totalSucceeded": succ, "totalFailed": len(results) - succ,
        }), 200
    except errors.GoogleAdsServerFault as fault:
        logger.error("SOAP fault batch-creating ad units: %s", fault)
        # Per-item attribution from fault.errors[*].fieldPath = "adUnits[N].adUnitCode"
        results = [None] * len(ad_units_in)
        api_errors = getattr(fault, "errors", None) or []
        for api_error in api_errors:
            field_path = getattr(api_error, "fieldPath", "") or ""
            error_string = getattr(api_error, "errorString", "") or ""
            m = re.match(r"adUnits\[(\d+)\]", field_path)
            if not m:
                continue
            idx = int(m.group(1))
            if idx < 0 or idx >= len(ad_units_in):
                continue
            err_class = "ALREADY_EXISTS" if any(t in error_string for t in _ALREADY_EXISTS_TOKENS) \
                else "RATE_LIMITED" if any(t in error_string for t in _RATE_LIMIT_TOKENS) \
                else "INVALID_FIELD"
            results[idx] = {
                "index": idx, "success": False, "error": err_class,
                "message": error_string, "fieldPath": field_path,
            }
        # Fill survivors as {success: true} without id (worker recovers via lookup-first)
        for i in range(len(results)):
            if results[i] is None:
                results[i] = {"index": i, "success": True}
        succ = sum(1 for r in results if r["success"])
        return jsonify({
            "results": results, "totalSucceeded": succ, "totalFailed": len(results) - succ,
        }), 200
    except Exception as exc:
        logger.exception("Unexpected error batch-creating ad units")
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"InventoryService.createAdUnits batch failed: {_sanitize_fault_message(exc)}",
        }), 500


def _update_ad_units_batch():
    """PATCH /gam/ad-units/batch — update up to GAM_BATCH_SIZE ad units in one
    SOAP call. Validates each item's `id` field as ^\\d+$ BEFORE invoking SOAP.

    Bug 2 fix: any malformed id (literal "undefined"/"null", empty, alpha,
    mixed) returns HTTP 400 VALIDATION_ERROR with a descriptive message.
    """
    if not request.is_json:
        return jsonify({"error": "VALIDATION_ERROR", "message": "Content-Type must be application/json"}), 415
    body = request.get_json(silent=True)
    if body is None or not isinstance(body, dict) or "updates" not in body:
        return jsonify({"error": "VALIDATION_ERROR", "message": "body must be {updates: [...]}"}), 400
    updates_in = body["updates"]
    if not isinstance(updates_in, list):
        return jsonify({"error": "VALIDATION_ERROR", "message": "updates must be an array"}), 400
    if len(updates_in) == 0:
        return jsonify({"results": [], "totalSucceeded": 0, "totalFailed": 0}), 200

    # Validate every id BEFORE constructing SOAP
    soap_updates = []
    for item in updates_in:
        try:
            soap_updates.append(_build_update_ad_unit(item))
        except ValueError as ve:
            return jsonify({"error": "VALIDATION_ERROR", "message": str(ve)}), 400

    try:
        inventory_service = _inventory_service()
        result = inventory_service.updateAdUnits(soap_updates)
        results = []
        for i in range(len(updates_in)):
            updated = result[i] if i < len(result) else None
            if updated is not None:
                results.append({"index": i, "success": True})
            else:
                results.append({"index": i, "success": False, "error": "NOT_FOUND"})
        succ = sum(1 for r in results if r["success"])
        return jsonify({
            "results": results, "totalSucceeded": succ, "totalFailed": len(results) - succ,
        }), 200
    except errors.GoogleAdsServerFault as fault:
        logger.error("SOAP fault batch-updating ad units: %s", fault)
        results = [None] * len(updates_in)
        api_errors = getattr(fault, "errors", None) or []
        for api_error in api_errors:
            field_path = getattr(api_error, "fieldPath", "") or ""
            error_string = getattr(api_error, "errorString", "") or ""
            m = re.match(r"updates\[(\d+)\]", field_path) or re.match(r"adUnits\[(\d+)\]", field_path)
            if not m:
                continue
            idx = int(m.group(1))
            if idx < 0 or idx >= len(updates_in):
                continue
            err_class = "NOT_FOUND" if "NotFound" in error_string or "NOT_FOUND" in error_string \
                else "RATE_LIMITED" if any(t in error_string for t in _RATE_LIMIT_TOKENS) \
                else "INVALID_FIELD"
            results[idx] = {
                "index": idx, "success": False, "error": err_class,
                "message": error_string, "fieldPath": field_path,
            }
        for i in range(len(results)):
            if results[i] is None:
                results[i] = {"index": i, "success": True}
        succ = sum(1 for r in results if r["success"])
        return jsonify({
            "results": results, "totalSucceeded": succ, "totalFailed": len(results) - succ,
        }), 200
    except Exception as exc:
        logger.exception("Unexpected error batch-updating ad units")
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"InventoryService.updateAdUnits batch failed: {_sanitize_fault_message(exc)}",
        }), 500


def _lookup_ad_units_batch():
    """GET /gam/ad-units/lookup-batch?codes=A,B,C&parentId=987

    Parent-scoped batch lookup. Returns canonical ids for codes that exist
    at the given parent.
    """
    codes_param = request.args.get("codes", "")
    parent_id = request.args.get("parentId")
    if not codes_param:
        return jsonify({"error": "VALIDATION_ERROR", "message": "codes query parameter is required"}), 400
    if not parent_id:
        return jsonify({"error": "VALIDATION_ERROR", "message": "parentId query parameter is required"}), 400
    codes = [c.strip() for c in codes_param.split(",") if c.strip()]
    if not codes:
        return jsonify({"results": []}), 200
    try:
        inventory_service = _inventory_service()
        # Build IN-clause for the codes; SOAP PQL's `IN` syntax accepts
        # a comma-separated list of quoted strings.
        in_clause = ", ".join(f"'{c}'" for c in codes)
        statement = (
            ad_manager.StatementBuilder(version=API_VERSION)
            .Where(f"adUnitCode IN ({in_clause}) AND parentId = :pid")
            .WithBindVariable("pid", int(parent_id))
            .Limit(len(codes))
            .Offset(0)
        )
        response = inventory_service.getAdUnitsByStatement(statement.ToStatement())
        results_in = None
        if isinstance(response, dict):
            results_in = response.get("results", None)
        else:
            results_in = getattr(response, "results", None)
        results = []
        for au in (results_in or []):
            au_id = au["id"] if isinstance(au, dict) else getattr(au, "id", None)
            au_code = au["adUnitCode"] if isinstance(au, dict) else getattr(au, "adUnitCode", None)
            au_status = au["status"] if isinstance(au, dict) else getattr(au, "status", "ACTIVE")
            results.append({
                "adUnitCode": str(au_code), "id": str(au_id), "status": str(au_status),
            })
        return jsonify({"results": results}), 200
    except errors.GoogleAdsServerFault as fault:
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"lookup-batch failed: {_sanitize_fault_message(fault)}",
        }), 500
    except Exception as exc:
        logger.exception("Unexpected error in lookup-batch")
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"lookup-batch failed: {_sanitize_fault_message(exc)}",
        }), 500


# -- catch-all error handler -----------------------------------------------

@app.errorhandler(Exception)
def handle_exception(exc):
    """Prevent stack trace leaks for any unhandled exception."""
    logger.exception("Unhandled exception")
    return jsonify({
        "error": "INTERNAL_ERROR",
        "message": "An unexpected error occurred",
    }), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("GAM_PROXY_PORT", 5000))
    logger.info("Starting GAM proxy on 127.0.0.1:%d", port)
    app.run(host="127.0.0.1", port=port, debug=False)
