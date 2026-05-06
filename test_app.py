"""
Unit tests for the GAM proxy Flask application.

All googleads SDK calls are mocked — no real SOAP requests are made.
"""

import importlib
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# We need to mock the googleads module before importing app, because app.py
# calls _load_client() at import time.

# Create a fake googleads module structure
mock_googleads = MagicMock()
mock_ad_manager_module = MagicMock()
mock_errors_module = MagicMock()

# Create a real exception class for GoogleAdsServerFault
class FakeGoogleAdsServerFault(Exception):
    pass

mock_errors_module.GoogleAdsServerFault = FakeGoogleAdsServerFault
mock_googleads.errors = mock_errors_module
mock_googleads.ad_manager = mock_ad_manager_module

# Mock StatementBuilder
mock_statement_builder_class = MagicMock()
mock_ad_manager_module.StatementBuilder = mock_statement_builder_class

sys.modules["googleads"] = mock_googleads
sys.modules["googleads.ad_manager"] = mock_ad_manager_module
sys.modules["googleads.errors"] = mock_errors_module


def _create_fake_yaml():
    """Write a minimal googleads.yaml so the startup check passes."""
    yaml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "googleads.yaml")
    if not os.path.exists(yaml_path):
        with open(yaml_path, "w") as f:
            f.write("ad_manager:\n  application_name: test\n  network_code: '0'\n")
    return yaml_path


_yaml_path = _create_fake_yaml()

# Now import the app — the mocked googleads will be used
import app as gam_app  # noqa: E402


class BaseTestCase(unittest.TestCase):
    """Base class that sets up the Flask test client."""

    def setUp(self):
        gam_app.app.testing = True
        self.client = gam_app.app.test_client()


# =========================================================================
# s01: Health check success
# =========================================================================
class TestHealthCheckSuccess(BaseTestCase):

    def test_returns_200_with_network_info(self):
        mock_network_service = MagicMock()
        mock_network_service.getCurrentNetwork.return_value = {
            "networkCode": "12345678",
            "effectiveRootAdUnitId": "98765432",
        }

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_network_service
            resp = self.client.get("/gam/health")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["networkCode"], "12345678")
        self.assertEqual(data["effectiveRootAdUnitId"], "98765432")


# =========================================================================
# s02: Health check failure
# =========================================================================
class TestHealthCheckFailure(BaseTestCase):

    def test_returns_503_on_exception(self):
        mock_network_service = MagicMock()
        mock_network_service.getCurrentNetwork.side_effect = Exception(
            "Authentication failed: invalid key file"
        )

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_network_service
            resp = self.client.get("/gam/health")

        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "error")
        self.assertIn("Authentication failed", data["message"])


# =========================================================================
# s03: Create ad unit success
# =========================================================================
class TestCreateAdUnitSuccess(BaseTestCase):

    def test_returns_201_with_id(self):
        mock_inventory = MagicMock()
        mock_inventory.createAdUnits.return_value = [{"id": "12345678"}]

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.post(
                "/gam/ad-units",
                data=json.dumps({
                    "name": "stlight",
                    "adUnitCode": "vtvprime_dev/mob/home/stlight",
                    "parentId": "99001",
                    "adUnitSizes": [
                        {"size": {"width": 300, "height": 250}, "environmentType": "BROWSER"},
                        {"size": {"width": 640, "height": 480}, "environmentType": "VIDEO_PLAYER"},
                    ],
                    "targetWindow": "BLANK",
                }),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(data["id"], "12345678")

        # Verify SOAP payload structure
        call_args = mock_inventory.createAdUnits.call_args[0][0]
        ad_unit_payload = call_args[0]
        self.assertEqual(ad_unit_payload["name"], "stlight")
        self.assertEqual(ad_unit_payload["parentId"], "99001")
        self.assertEqual(ad_unit_payload["targetWindow"], "BLANK")
        self.assertEqual(len(ad_unit_payload["adUnitSizes"]), 2)
        self.assertEqual(ad_unit_payload["adUnitSizes"][0]["environmentType"], "BROWSER")
        self.assertEqual(ad_unit_payload["adUnitSizes"][1]["environmentType"], "VIDEO_PLAYER")


# =========================================================================
# s04: Create hierarchy node (empty sizes, no targetWindow)
# =========================================================================
class TestCreateHierarchyNode(BaseTestCase):

    def test_returns_201_with_empty_sizes(self):
        mock_inventory = MagicMock()
        mock_inventory.createAdUnits.return_value = [{"id": "10001"}]

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.post(
                "/gam/ad-units",
                data=json.dumps({
                    "name": "vtvprime_dev",
                    "adUnitCode": "vtvprime_dev",
                    "parentId": "98765432",
                    "adUnitSizes": [],
                }),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(data["id"], "10001")

        # Verify no targetWindow or adUnitSizes in payload
        call_args = mock_inventory.createAdUnits.call_args[0][0]
        ad_unit_payload = call_args[0]
        self.assertNotIn("targetWindow", ad_unit_payload)
        self.assertNotIn("adUnitSizes", ad_unit_payload)


# =========================================================================
# s05: Already exists -> 409
# =========================================================================
class TestCreateAdUnitAlreadyExists(BaseTestCase):

    def test_returns_409_on_already_exists(self):
        mock_inventory = MagicMock()
        fault = FakeGoogleAdsServerFault("UniqueError: ad unit code already in use")
        mock_inventory.createAdUnits.side_effect = fault

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.post(
                "/gam/ad-units",
                data=json.dumps({
                    "name": "mob",
                    "adUnitCode": "vtvprime_dev/mob",
                    "parentId": "10001",
                    "adUnitSizes": [],
                }),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["error"], "ALREADY_EXISTS")
        self.assertIn("vtvprime_dev/mob", data["message"])


# =========================================================================
# s06: Lookup by code -> 200
# =========================================================================
class TestLookupAdUnitByCode(BaseTestCase):

    def test_returns_200_with_id_and_code(self):
        mock_inventory = MagicMock()
        mock_inventory.getAdUnitsByStatement.return_value = {
            "results": [{"id": "20001", "adUnitCode": "vtvprime_dev/mob/home"}],
        }

        # Mock StatementBuilder chain
        mock_sb_instance = MagicMock()
        mock_sb_instance.Where.return_value = mock_sb_instance
        mock_sb_instance.WithBindVariable.return_value = mock_sb_instance
        mock_sb_instance.Limit.return_value = mock_sb_instance
        mock_sb_instance.Offset.return_value = mock_sb_instance
        mock_sb_instance.ToStatement.return_value = "fake_statement"

        with patch.object(gam_app, "client") as mock_client, \
             patch.object(gam_app.ad_manager, "StatementBuilder", return_value=mock_sb_instance):
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.get("/gam/ad-units?code=vtvprime_dev/mob/home")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["id"], "20001")
        self.assertEqual(data["adUnitCode"], "vtvprime_dev/mob/home")


# =========================================================================
# s07: Lookup not found -> 404
# =========================================================================
class TestLookupAdUnitNotFound(BaseTestCase):

    def test_returns_404_when_not_found(self):
        mock_inventory = MagicMock()
        mock_inventory.getAdUnitsByStatement.return_value = {"results": None}

        mock_sb_instance = MagicMock()
        mock_sb_instance.Where.return_value = mock_sb_instance
        mock_sb_instance.WithBindVariable.return_value = mock_sb_instance
        mock_sb_instance.Limit.return_value = mock_sb_instance
        mock_sb_instance.Offset.return_value = mock_sb_instance
        mock_sb_instance.ToStatement.return_value = "fake_statement"

        with patch.object(gam_app, "client") as mock_client, \
             patch.object(gam_app.ad_manager, "StatementBuilder", return_value=mock_sb_instance):
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.get("/gam/ad-units?code=vtvprime_dev/nonexistent/path")

        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertEqual(data["error"], "NOT_FOUND")


# =========================================================================
# s08: Rate limited -> 429
# =========================================================================
class TestRateLimited(BaseTestCase):

    def test_returns_429_on_rate_limit(self):
        mock_inventory = MagicMock()
        fault = FakeGoogleAdsServerFault("QuotaError: rate limit exceeded")
        mock_inventory.createAdUnits.side_effect = fault

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.post(
                "/gam/ad-units",
                data=json.dumps({
                    "name": "stlight",
                    "adUnitCode": "vtvprime_dev/mob/home/stlight",
                    "parentId": "99001",
                    "adUnitSizes": [],
                    "targetWindow": "BLANK",
                }),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 429)
        data = resp.get_json()
        self.assertEqual(data["error"], "RATE_LIMITED")
        self.assertEqual(data["retryAfter"], 5)


# =========================================================================
# s09: GAM error -> 500
# =========================================================================
class TestGamError(BaseTestCase):

    def test_returns_500_on_unexpected_soap_fault(self):
        mock_inventory = MagicMock()
        fault = FakeGoogleAdsServerFault("ServerError: internal failure")
        mock_inventory.createAdUnits.side_effect = fault

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.post(
                "/gam/ad-units",
                data=json.dumps({
                    "name": "stlight",
                    "adUnitCode": "vtvprime_dev/mob/home/stlight",
                    "parentId": "INVALID_PARENT_ID",
                    "adUnitSizes": [],
                }),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 500)
        data = resp.get_json()
        self.assertEqual(data["error"], "GAM_ERROR")
        self.assertIn("InventoryService.createAdUnits failed", data["message"])


# =========================================================================
# s14: Missing required fields -> 400
# =========================================================================
class TestValidation(BaseTestCase):

    def test_missing_name_returns_400(self):
        resp = self.client.post(
            "/gam/ad-units",
            data=json.dumps({"adUnitCode": "vtvprime_dev/mob/home/stlight"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertEqual(data["error"], "GAM_ERROR")
        self.assertIn("Missing required field: name", data["message"])

    def test_missing_parentId_returns_400(self):
        resp = self.client.post(
            "/gam/ad-units",
            data=json.dumps({"name": "test"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("Missing required field: parentId", data["message"])

    def test_wrong_content_type_returns_415(self):
        resp = self.client.post(
            "/gam/ad-units",
            data="name=test",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(resp.status_code, 415)
        data = resp.get_json()
        self.assertEqual(data["error"], "VALIDATION_ERROR")

    def test_missing_code_query_param_returns_400(self):
        resp = self.client.get("/gam/ad-units")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertEqual(data["error"], "VALIDATION_ERROR")
        self.assertEqual(data["message"], "code query parameter is required")


# =========================================================================
# s18: Startup fails without googleads.yaml
# =========================================================================
class TestStartupWithoutConfig(unittest.TestCase):

    def test_exit_when_yaml_missing(self):
        """_load_client() should sys.exit(1) when googleads.yaml is missing."""
        with patch.object(gam_app.os.path, "isfile", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                gam_app._load_client()
            self.assertEqual(ctx.exception.code, 1)


# =========================================================================
# RED-PHASE TESTS: gam-proxy-leaf-filter bug
# These tests prove the bug before the fix is applied.
# After the fix, they serve as regression guards.
# =========================================================================

class TestGetAllLeafAdUnits(BaseTestCase):
    """Tests for GET /gam/ad-units/all (bug: hasChildren PQL filter)."""

    def _make_ad_unit(self, unit_id, code, has_children, parent_id="10001"):
        """Return a dict-style SOAP result ad unit."""
        return {
            "id": unit_id,
            "adUnitCode": code,
            "name": code.split("/")[-1],
            "status": "ACTIVE",
            "parentId": parent_id,
            "hasChildren": has_children,
            "adUnitSizes": [
                {
                    "size": {"width": 300, "height": 250},
                    "environmentType": "BROWSER",
                    "isFluid": False,
                }
            ],
        }

    def _setup_sb_mock(self, where_clauses_captured=None):
        """Return a mock StatementBuilder instance that captures Where() calls."""
        mock_sb = MagicMock()
        mock_sb.Where.side_effect = lambda clause: (
            where_clauses_captured.append(clause) or mock_sb
            if where_clauses_captured is not None
            else mock_sb
        )
        mock_sb.Limit.return_value = mock_sb
        mock_sb.Offset.return_value = mock_sb
        mock_sb.ToStatement.return_value = "fake_statement"
        return mock_sb

    # ------------------------------------------------------------------
    # s10: Happy path — mix of leaf and non-leaf, only leaves returned
    # ------------------------------------------------------------------
    def test_s10_leaf_only_returned(self):
        """AC-2/AC-3: Only leaf nodes (hasChildren=False) are in the response."""
        leaf_unit = self._make_ad_unit("201", "vtvprime/mob/home/stlight", False)
        hierarchy_unit = self._make_ad_unit("202", "vtvprime/mob/home", True)

        mock_inventory = MagicMock()
        # First call returns mixed results, second call returns empty (end of pages)
        mock_inventory.getAdUnitsByStatement.side_effect = [
            {"results": [leaf_unit, hierarchy_unit]},
            {"results": None},
        ]

        where_clauses = []
        mock_sb = self._setup_sb_mock(where_clauses)

        with patch.object(gam_app, "client") as mock_client, \
             patch.object(gam_app.ad_manager, "StatementBuilder", return_value=mock_sb):
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.get("/gam/ad-units/all")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        # Only the leaf unit should be returned
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["gam_id"], "201")
        # Hierarchy unit must NOT be present
        gam_ids = [u["gam_id"] for u in data]
        self.assertNotIn("202", gam_ids)

    # ------------------------------------------------------------------
    # s11: No hasChildren in PQL WHERE clause
    # ------------------------------------------------------------------
    def test_s11_no_haschildren_pql_filter(self):
        """AC-5: getAdUnitsByStatement must NOT pass hasChildren in WHERE."""
        mock_inventory = MagicMock()
        mock_inventory.getAdUnitsByStatement.return_value = {"results": None}

        where_clauses = []
        mock_sb = self._setup_sb_mock(where_clauses)

        with patch.object(gam_app, "client") as mock_client, \
             patch.object(gam_app.ad_manager, "StatementBuilder", return_value=mock_sb):
            mock_client.GetService.return_value = mock_inventory
            self.client.get("/gam/ad-units/all")

        # Where() must have been called (the pre-filter parentId IS NOT NULL)
        self.assertTrue(len(where_clauses) > 0, "Expected at least one WHERE clause call")
        for clause in where_clauses:
            self.assertNotIn(
                "hasChildren",
                clause,
                f"hasChildren must not appear in PQL WHERE clause: '{clause}'"
            )

    # ------------------------------------------------------------------
    # s12: Pagination — two pages fetched
    # ------------------------------------------------------------------
    def test_s12_pagination_fetches_all_pages(self):
        """AC-4: Pagination must use raw SOAP result count for break/offset logic."""
        # 500 leaf units on first page; empty second page
        first_page = [
            self._make_ad_unit(str(i), f"vtvprime/mob/leaf{i}", False)
            for i in range(500)
        ]

        mock_inventory = MagicMock()
        mock_inventory.getAdUnitsByStatement.side_effect = [
            {"results": first_page},
            {"results": None},
        ]

        mock_sb = self._setup_sb_mock()

        with patch.object(gam_app, "client") as mock_client, \
             patch.object(gam_app.ad_manager, "StatementBuilder", return_value=mock_sb):
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.get("/gam/ad-units/all")

        self.assertEqual(resp.status_code, 200)
        # getAdUnitsByStatement must have been called twice (page 1 + empty page 2)
        self.assertEqual(mock_inventory.getAdUnitsByStatement.call_count, 2)
        data = resp.get_json()
        self.assertEqual(len(data), 500)

    # ------------------------------------------------------------------
    # s13: SOAP fault returns GAM_ERROR (not INTERNAL_ERROR)
    # ------------------------------------------------------------------
    def test_s13_soap_fault_returns_gam_error(self):
        """AC fix: SOAP fault must produce GAM_ERROR 500, not INTERNAL_ERROR."""
        mock_inventory = MagicMock()
        mock_inventory.getAdUnitsByStatement.side_effect = Exception(
            "GoogleAdsServerFault: hasChildren is not a valid PQL filter"
        )

        mock_sb = self._setup_sb_mock()

        with patch.object(gam_app, "client") as mock_client, \
             patch.object(gam_app.ad_manager, "StatementBuilder", return_value=mock_sb):
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.get("/gam/ad-units/all")

        self.assertEqual(resp.status_code, 500)
        data = resp.get_json()
        # Must be GAM_ERROR, not INTERNAL_ERROR (which is the Flask global handler)
        self.assertEqual(data["error"], "GAM_ERROR")

    # ------------------------------------------------------------------
    # s13b: Empty network returns 200 with empty array
    # ------------------------------------------------------------------
    def test_s13b_empty_network_returns_200(self):
        """AC-6: Zero ad units should return HTTP 200 with []."""
        mock_inventory = MagicMock()
        mock_inventory.getAdUnitsByStatement.return_value = {"results": None}

        mock_sb = self._setup_sb_mock()

        with patch.object(gam_app, "client") as mock_client, \
             patch.object(gam_app.ad_manager, "StatementBuilder", return_value=mock_sb):
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.get("/gam/ad-units/all")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data, [])

    # ------------------------------------------------------------------
    # Meta-test: Regression guard — scan production code for hasChildren PQL
    # ------------------------------------------------------------------
    def test_meta_no_haschidren_pql_in_production_code(self):
        """Regression guard: no .Where() call in app.py passes hasChildren."""
        import re
        app_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
        with open(app_path, "r") as f:
            source = f.read()

        # Find all .Where("...") calls
        where_calls = re.findall(r'\.Where\("([^"]+)"\)', source)
        for clause in where_calls:
            self.assertNotIn(
                "hasChildren",
                clause,
                f"Found invalid hasChildren PQL filter in app.py: .Where(\"{clause}\")"
            )


if __name__ == "__main__":
    unittest.main()
