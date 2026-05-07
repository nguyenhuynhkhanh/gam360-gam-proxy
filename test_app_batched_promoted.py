"""
gam-batched-bulk-deploy: HOLDOUT scenarios for the proxy.

Coverage table (H-* IDs map to scenarios in
dark-factory/scenarios/holdout/gam-batched-bulk-deploy/):

    H-01  proxy POST batch — 50-item single SOAP call
    H-02  proxy PATCH batch — per-item attribution for updates
    H-03  proxy whole-batch GAM rate-limit fault returns 429
    H-04  proxy lookup-batch with 50 codes, half found
    H-05  all new endpoints reject requests without X-Proxy-Secret
"""

import importlib
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Defer to test_app's mocking machinery (it sets up sys.modules['googleads']
# and registers the FakeGoogleAdsServerFault class).
from test_app import (  # noqa: E402
    BaseTestCase,
    FakeGoogleAdsServerFault,
    gam_app,
)


# ---------------------------------------------------------------------------
# H-01: 50-item POST /gam/ad-units/batch single SOAP call
# ---------------------------------------------------------------------------
class TestBatchCreateFiftyItemsSingleSoapCall(BaseTestCase):

    def test_proxy_passes_50_items_in_single_createAdUnits_call(self):
        items = [
            {"name": f"u{i}", "adUnitCode": f"u{i}", "parentId": "987",
             "adUnitSizes": [{"size": {"width": 300, "height": 250}, "environmentType": "BROWSER"}],
             "targetWindow": "BLANK"}
            for i in range(50)
        ]
        soap_results = [{"id": str(1000 + i)} for i in range(50)]

        mock_inventory = MagicMock()
        mock_inventory.createAdUnits.return_value = soap_results

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.post(
                "/gam/ad-units/batch",
                data=json.dumps({"adUnits": items}),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["totalSucceeded"], 50)
        self.assertEqual(data["totalFailed"], 0)
        self.assertEqual(len(data["results"]), 50)
        # SOAP called EXACTLY ONCE with all 50 items.
        self.assertEqual(mock_inventory.createAdUnits.call_count, 1)
        call_arg = mock_inventory.createAdUnits.call_args[0][0]
        self.assertEqual(len(call_arg), 50)


# ---------------------------------------------------------------------------
# H-02: PATCH batch — per-item attribution for updates
# ---------------------------------------------------------------------------
class TestBatchUpdatePerItemAttribution(BaseTestCase):

    def test_attributes_failure_to_specific_index_in_updates(self):
        api_err = MagicMock()
        api_err.fieldPath = "updates[2].adUnitSizes[0]"
        api_err.errorString = "INVALID_FIELD"
        api_err.message = "Bad size"
        api_err.trigger = "Bad size"
        fault = FakeGoogleAdsServerFault("opaque")
        fault.errors = [api_err]
        mock_inventory = MagicMock()
        mock_inventory.updateAdUnits.side_effect = fault

        body = {"updates": [
            {"id": "111", "name": "a"},
            {"id": "222", "name": "b"},
            {"id": "333", "name": "c"},
            {"id": "444", "name": "d"},
        ]}

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.patch(
                "/gam/ad-units/batch",
                data=json.dumps(body),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["results"]), 4)
        # Index 2 fails; others echo input ids.
        self.assertTrue(data["results"][0]["success"])
        self.assertEqual(data["results"][0]["id"], "111")
        self.assertTrue(data["results"][1]["success"])
        self.assertEqual(data["results"][1]["id"], "222")
        self.assertFalse(data["results"][2]["success"])
        self.assertEqual(data["results"][2]["fieldPath"], "updates[2].adUnitSizes[0]")
        self.assertTrue(data["results"][3]["success"])
        self.assertEqual(data["results"][3]["id"], "444")


# ---------------------------------------------------------------------------
# H-03: rate-limit fault returns 429 (NOT 500)
# ---------------------------------------------------------------------------
class TestBatchCreateRateLimitedH03(BaseTestCase):

    def test_rate_limit_fault_returns_429(self):
        # Use a structured rate-limit fault.
        api_err = MagicMock()
        api_err.errorString = "RateExceededError.RATE_EXCEEDED"
        api_err.fieldPath = ""
        fault = FakeGoogleAdsServerFault("opaque")
        fault.errors = [api_err]
        mock_inventory = MagicMock()
        mock_inventory.createAdUnits.side_effect = fault

        body = {"adUnits": [
            {"name": f"u{i}", "adUnitCode": f"u{i}", "parentId": "987", "adUnitSizes": []}
            for i in range(10)
        ]}

        with patch.object(gam_app, "client") as mock_client:
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.post(
                "/gam/ad-units/batch",
                data=json.dumps(body),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 429)
        data = resp.get_json()
        self.assertEqual(data["error"], "RATE_LIMITED")
        self.assertEqual(data["retryAfter"], 5)


# ---------------------------------------------------------------------------
# H-04: lookup-batch with 50 codes, half (25) found
# ---------------------------------------------------------------------------
class TestLookupBatchFiftyHalfFound(BaseTestCase):

    def test_returns_half_found_codes_only(self):
        codes = [f"c{i}" for i in range(50)]
        # GAM returns 25 of them.
        soap_results = [
            {"id": str(2000 + i), "adUnitCode": f"c{i}", "status": "ACTIVE"}
            for i in range(25)
        ]
        mock_inventory = MagicMock()
        mock_inventory.getAdUnitsByStatement.return_value = {"results": soap_results}
        mock_sb = MagicMock()
        mock_sb.Where.return_value = mock_sb
        mock_sb.WithBindVariable.return_value = mock_sb
        mock_sb.ToStatement.return_value = "fake_statement"

        with patch.object(gam_app, "client") as mock_client, \
             patch.object(gam_app.ad_manager, "StatementBuilder", return_value=mock_sb):
            mock_client.GetService.return_value = mock_inventory
            resp = self.client.get(
                f"/gam/ad-units/lookup-batch?codes={','.join(codes)}&parentId=987"
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["results"]), 25)
        # Single SOAP call.
        self.assertEqual(mock_inventory.getAdUnitsByStatement.call_count, 1)
        # IN clause built with 50 :c0..:c49 binds.
        clause = mock_sb.Where.call_args_list[0][0][0]
        self.assertIn(":c0", clause)
        self.assertIn(":c49", clause)


# ---------------------------------------------------------------------------
# H-05: All new endpoints reject without X-Proxy-Secret (auth gate)
# ---------------------------------------------------------------------------
class TestNewEndpointsAuthGate(BaseTestCase):

    def setUp(self):
        super().setUp()
        # Force PROXY_SECRET to a non-empty value so the before_request hook
        # actually rejects missing headers. Restore in tearDown.
        self._old_secret = gam_app.PROXY_SECRET
        gam_app.PROXY_SECRET = "test-secret-value"

    def tearDown(self):
        gam_app.PROXY_SECRET = self._old_secret

    def test_post_batch_without_secret_rejected(self):
        resp = self.client.post(
            "/gam/ad-units/batch",
            data=json.dumps({"adUnits": [{"name": "x", "adUnitCode": "x", "parentId": "1"}]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_patch_batch_without_secret_rejected(self):
        resp = self.client.patch(
            "/gam/ad-units/batch",
            data=json.dumps({"updates": [{"id": "1", "name": "x"}]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_lookup_batch_without_secret_rejected(self):
        resp = self.client.get("/gam/ad-units/lookup-batch?codes=foo&parentId=1")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
