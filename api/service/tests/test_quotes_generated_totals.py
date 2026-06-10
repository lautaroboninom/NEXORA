from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from service.views import quotes_views


class QuoteGeneratedTotalsTests(SimpleTestCase):
    def test_postgres_skips_generated_total_columns(self):
        rows = [
            {"column_name": "subtotal", "is_generated": "NEVER"},
            {"column_name": "iva_21", "is_generated": "ALWAYS"},
            {"column_name": "total", "is_generated": "ALWAYS"},
        ]

        with patch.object(quotes_views, "connection", SimpleNamespace(vendor="postgresql")):
            with patch.object(quotes_views, "q", return_value=rows):
                self.assertEqual(quotes_views._get_quote_total_update_columns(), ["subtotal"])

    def test_postgres_keeps_plain_columns(self):
        rows = [
            {"column_name": "subtotal", "is_generated": "NEVER"},
            {"column_name": "iva_21", "is_generated": "NEVER"},
            {"column_name": "total", "is_generated": "NEVER"},
        ]

        with patch.object(quotes_views, "connection", SimpleNamespace(vendor="postgresql")):
            with patch.object(quotes_views, "q", return_value=rows):
                self.assertEqual(
                    quotes_views._get_quote_total_update_columns(),
                    ["subtotal", "iva_21", "total"],
                )

    def test_postgres_metadata_failure_uses_legacy_columns(self):
        with patch.object(quotes_views, "connection", SimpleNamespace(vendor="postgresql")):
            with patch.object(quotes_views, "q", side_effect=RuntimeError("boom")):
                self.assertEqual(
                    quotes_views._get_quote_total_update_columns(),
                    ["subtotal", "iva_21", "total"],
                )

    def test_non_postgres_uses_legacy_columns(self):
        with patch.object(quotes_views, "connection", SimpleNamespace(vendor="sqlite")):
            self.assertEqual(
                quotes_views._get_quote_total_update_columns(),
                ["subtotal", "iva_21", "total"],
            )
