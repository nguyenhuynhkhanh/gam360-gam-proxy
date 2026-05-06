"""gam-resilience-foundation HOLDOUT validation suite (Python side).

Mirrors:
  H-06 sanitize strips multi-line envelope (order: regex first, truncate second)
  H-10 structured proxy classifier skips empty errorString
"""

import unittest
from unittest.mock import MagicMock

# The test_app module owns the googleads mock setup; reuse it.
import test_app as _testapp  # noqa: F401  (side effect: googleads mock + app import)
import app as gam_app


class TestSanitizationMultilineEnvelope(unittest.TestCase):
    """H-06 / EC-12: regex strip happens BEFORE newline truncation."""

    def test_envelope_spanning_newlines_stripped(self):
        msg = (
            '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">\n'
            "  <env:Body>\n"
            "    <detail>QuotaError occurred</detail>\n"
            "  </env:Body>\n"
            "</env:Envelope>\n"
            "extra: see https://internal.example.com for details"
        )
        out = gam_app._sanitize_fault_message(msg)
        self.assertNotIn("<env:", out)
        self.assertNotIn("</env:", out)
        self.assertNotIn("https://", out)

    def test_envelope_then_newline_then_more_content_yields_empty(self):
        # After envelope strip, message is '\nQuotaError on next line'.
        # First newline is at position 0, so truncation yields empty string.
        msg = "<env:Envelope>x</env:Envelope>\nQuotaError on next line"
        out = gam_app._sanitize_fault_message(msg)
        self.assertNotIn("<env:", out)
        self.assertEqual(out, "")


class TestStructuredClassifierEmptyErrorString(unittest.TestCase):
    """H-10 / EC-13: structured classifier handles malformed ApiError elements."""

    def _fault_with_errors(self, errors):
        fault = _testapp.FakeGoogleAdsServerFault("opaque")
        fault.errors = errors
        return fault

    def _api_err(self, error_string):
        m = MagicMock()
        m.errorString = error_string
        m.fieldPath = ""
        return m

    def test_skips_element_with_no_errorString(self):
        fault = self._fault_with_errors([
            self._api_err(""),
            self._api_err("RateExceededError.RATE_EXCEEDED"),
        ])
        self.assertTrue(gam_app._is_rate_limited(fault))

    def test_only_empty_elements_returns_false_no_warning(self):
        fault = self._fault_with_errors([self._api_err("")])
        # Structured path was taken (errors truthy) → returns False without
        # triggering substring fallback. We assert no WARNING is emitted by
        # capturing logs and confirming "Falling back" is not present.
        with self.assertLogs(gam_app.logger, level="DEBUG") as captured:
            result = gam_app._is_rate_limited(fault)
            # Force a sentinel info log so assertLogs captures something.
            gam_app.logger.info("__sentinel__")
        self.assertFalse(result)
        joined = "\n".join(captured.output)
        self.assertNotIn("Falling back to substring match", joined)

    def test_element_with_no_errorString_attribute_at_all(self):
        class BareError:
            pass
        api_err = BareError()  # no errorString attribute
        fault = self._fault_with_errors([api_err])
        self.assertFalse(gam_app._is_rate_limited(fault))


if __name__ == "__main__":
    unittest.main()
