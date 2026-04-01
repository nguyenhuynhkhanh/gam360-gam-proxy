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
    """Load the AdManagerClient from googleads.yaml.  Exits the process if the
    config file is missing so the proxy fails fast at startup."""
    if not os.path.isfile(CONFIG_PATH):
        logger.error(
            "ERROR: googleads.yaml not found at %s. "
            "Copy googleads.yaml.example to googleads.yaml and fill in your "
            "service account credentials before starting the proxy.",
            CONFIG_PATH,
        )
        sys.exit(1)
    return ad_manager.AdManagerClient.LoadFromStorage(CONFIG_PATH)


client = _load_client()

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


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

        if name and parent_id:
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
