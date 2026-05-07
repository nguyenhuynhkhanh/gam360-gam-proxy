"""gam-batched-bulk-deploy-fix proxy-side regression suite.

Covers Bug 2 (P-06, H-14): _build_update_ad_unit / _update_ad_units_batch
must reject malformed `id` payloads with HTTP 400 VALIDATION_ERROR before
any SOAP call is invoked.

Imports test_app for its side-effect-loaded googleads mock so app.py is
importable in this isolated environment.
"""

import unittest
from unittest.mock import patch, MagicMock

import test_app as _testapp  # noqa: F401 — side-effect: googleads mock
import app as gam_app


class _BaseProxyTestCase(unittest.TestCase):
    def setUp(self):
        gam_app.app.testing = True
        self.client = gam_app.app.test_client()


class TestUpdateBatchValidationRejects(_BaseProxyTestCase):
    """P-06 / H-14 Invalid: every malformed id returns 400 VALIDATION_ERROR
    BEFORE any SOAP call is made."""

    def _post(self, raw_id):
        body = {"updates": [{"id": raw_id, "name": "x"}]}
        return self.client.patch("/gam/ad-units/batch", json=body)

    def test_literal_undefined_rejected(self):
        r = self._post("undefined")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "VALIDATION_ERROR")
        self.assertIn("undefined", r.get_json()["message"])

    def test_literal_null_rejected(self):
        r = self._post("null")
        self.assertEqual(r.status_code, 400)
        self.assertIn("null", r.get_json()["message"])

    def test_empty_string_rejected(self):
        r = self._post("")
        self.assertEqual(r.status_code, 400)

    def test_whitespace_only_rejected(self):
        r = self._post(" ")
        self.assertEqual(r.status_code, 400)

    def test_trailing_whitespace_rejected(self):
        r = self._post("123 ")
        self.assertEqual(r.status_code, 400)

    def test_leading_whitespace_rejected(self):
        r = self._post(" 123")
        self.assertEqual(r.status_code, 400)

    def test_float_rejected(self):
        r = self._post("123.45")
        self.assertEqual(r.status_code, 400)

    def test_negative_rejected(self):
        r = self._post("-123")
        self.assertEqual(r.status_code, 400)

    def test_positive_sign_rejected(self):
        r = self._post("+123")
        self.assertEqual(r.status_code, 400)

    def test_scientific_rejected(self):
        r = self._post("1e5")
        self.assertEqual(r.status_code, 400)

    def test_hex_rejected(self):
        r = self._post("0x1F")
        self.assertEqual(r.status_code, 400)

    def test_alpha_rejected(self):
        r = self._post("abc")
        self.assertEqual(r.status_code, 400)

    def test_mixed_alphanumeric_rejected(self):
        r = self._post("123abc")
        self.assertEqual(r.status_code, 400)

    def test_alpha_then_numeric_rejected(self):
        r = self._post("abc123")
        self.assertEqual(r.status_code, 400)

    def test_json_null_rejected(self):
        r = self._post(None)
        self.assertEqual(r.status_code, 400)

    def test_missing_id_key_rejected(self):
        body = {"updates": [{"name": "x"}]}  # no id key
        r = self.client.patch("/gam/ad-units/batch", json=body)
        self.assertEqual(r.status_code, 400)

    def test_trailing_alpha_rejected(self):
        r = self._post("23343020634a")
        self.assertEqual(r.status_code, 400)

    def test_leading_alpha_rejected(self):
        r = self._post("a23343020634")
        self.assertEqual(r.status_code, 400)


class TestUpdateBatchValidationAccepts(_BaseProxyTestCase):
    """P-06 / H-14 Valid: every well-formed numeric id passes the validator
    and reaches the SOAP layer (mocked)."""

    def _post_and_assert_passed(self, raw_id):
        body = {"updates": [{"id": raw_id, "name": "x"}]}
        # Mock the inventory service so the SOAP path resolves cleanly.
        mock_inventory = MagicMock()
        mock_inventory.updateAdUnits.return_value = [{"id": raw_id}]
        with patch.object(gam_app, "_inventory_service", return_value=mock_inventory):
            r = self.client.patch("/gam/ad-units/batch", json=body)
        # The validation passed if we got past the 400 — code may be 200
        # (success). We assert NOT 400.
        self.assertNotEqual(r.status_code, 400, msg=f"id={raw_id!r} unexpectedly rejected")
        return r

    def test_zero_accepted(self):
        self._post_and_assert_passed("0")

    def test_one_accepted(self):
        self._post_and_assert_passed("1")

    def test_short_accepted(self):
        self._post_and_assert_passed("123")

    def test_realistic_gam_id(self):
        self._post_and_assert_passed("23343020634")

    def test_very_long_numeric_accepted(self):
        self._post_and_assert_passed("99999999999999999999")


class TestUpdateBatchValidationBeforeSoap(_BaseProxyTestCase):
    """The validator must run BEFORE any SOAP call. Verify the SOAP mock is
    NOT invoked for malformed payloads."""

    def test_soap_never_invoked_for_invalid_id(self):
        mock_inventory = MagicMock()
        with patch.object(gam_app, "_inventory_service", return_value=mock_inventory):
            r = self.client.patch(
                "/gam/ad-units/batch", json={"updates": [{"id": "undefined", "name": "x"}]}
            )
        self.assertEqual(r.status_code, 400)
        # SOAP method must NEVER have been touched
        mock_inventory.updateAdUnits.assert_not_called()


class TestLookupBatchEndpoint(_BaseProxyTestCase):
    """The batched lookup endpoint exists and returns 400 on missing params."""

    def test_missing_codes_returns_400(self):
        r = self.client.get("/gam/ad-units/lookup-batch?parentId=987")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "VALIDATION_ERROR")

    def test_missing_parentId_returns_400(self):
        r = self.client.get("/gam/ad-units/lookup-batch?codes=A,B")
        self.assertEqual(r.status_code, 400)


class TestCreateBatchAcceptsList(_BaseProxyTestCase):
    """POST /gam/ad-units/batch returns 400 for missing/non-list adUnits."""

    def test_missing_adUnits_returns_400(self):
        r = self.client.post("/gam/ad-units/batch", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "VALIDATION_ERROR")

    def test_non_list_adUnits_returns_400(self):
        r = self.client.post("/gam/ad-units/batch", json={"adUnits": "not-a-list"})
        self.assertEqual(r.status_code, 400)

    def test_empty_adUnits_returns_200_empty_results(self):
        r = self.client.post("/gam/ad-units/batch", json={"adUnits": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["totalSucceeded"], 0)
        self.assertEqual(r.get_json()["totalFailed"], 0)


if __name__ == "__main__":
    unittest.main()
