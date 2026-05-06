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


def _is_already_exists(fault):
    """Return True when the SOAP fault indicates the ad unit already exists."""
    fault_str = str(fault)
    if "UniqueError" in fault_str:
        return True
    if "ALREADY_EXISTS" in fault_str:
        return True
    return False


def _is_rate_limited(fault):
    """Return True when the SOAP fault indicates quota / rate limiting."""
    fault_str = str(fault)
    if "QuotaError" in fault_str:
        return True
    if "RATE_LIMITED" in fault_str:
        return True
    if "RateExceededError" in fault_str:
        return True
    if "TOO_MANY_REQUESTS" in fault_str:
        return True
    return False


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
            "message": str(exc),
        }), 503


@app.route("/gam/ad-units/all", methods=["GET"])
def ad_units_all():
    """GET /gam/ad-units/all?env=dev — bulk pull all leaf ad units (hasChildren=false)."""
    return _get_all_leaf_ad_units()


@app.route("/gam/ad-units/batch", methods=["GET"])
def ad_units_batch():
    """GET /gam/ad-units/batch?ids=<csv> — fetch multiple ad units by GAM ID."""
    return _get_ad_units_batch()


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

            total = getattr(response, 'totalResultSetSize', None) or (response.get('totalResultSetSize') if isinstance(response, dict) else None)
            results = response.get("results", None) if isinstance(response, dict) else getattr(response, "results", None)
            logger.info("GAM page offset=%d: totalResultSetSize=%s results_type=%s results_len=%s",
                        offset, total, type(results).__name__, len(results) if results is not None else 'None')

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
            "message": f"getAdUnitsByStatement (all leaf) failed: {exc}",
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
            "message": f"getAdUnitsByStatement batch failed: {exc}",
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
            "message": f"getAdUnitsByStatement failed: {exc}",
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
            "message": f"InventoryService.createAdUnits failed: {fault}",
        }), 500

    except Exception as exc:
        logger.exception("Unexpected error creating ad unit")
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"InventoryService.createAdUnits failed: {exc}",
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
            "message": f"InventoryService.updateAdUnits failed: {fault}",
        }), 500

    except Exception as exc:
        logger.exception("Unexpected error updating ad unit %s", gam_id)
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"InventoryService.updateAdUnits failed: {exc}",
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
            "message": f"Archive failed: {fault}",
        }), 500

    except Exception as exc:
        logger.exception("Unexpected error archiving ad unit %s", gam_id)
        return jsonify({
            "error": "GAM_ERROR",
            "message": f"Archive failed: {exc}",
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
