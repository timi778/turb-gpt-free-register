# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class RemailWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_remail_without_api_key(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "remail"
        ), patch.object(email_config, "REMAIL_API_KEY", "", create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Remail API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_remail_key_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "remail"
        ), patch.object(email_config, "REMAIL_API_KEY", "rk-test", create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("webui.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.db.count_accounts", return_value=0)
    def test_summary_does_not_count_remail_as_outlook_pool(self, count_accounts, outlook_pool_summary, domain_pool_summary):
        with patch.object(email_config, "EMAIL_SOURCE", "remail"):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["outlook_total"], 0)
        outlook_pool_summary.assert_not_called()

    @patch("core.remail_client.get_wallet")
    def test_wallet_endpoint_uses_form_key_without_returning_it(self, get_wallet):
        get_wallet.return_value = {
            "consumerBalance": "88.60",
            "historicalSpend": "120.00",
            "orderCount": 15,
            "updatedAt": "2026-08-07T00:30:00Z",
        }

        response = self.client.post(
            "/api/remail/wallet",
            json={"api_key": "rk-unsaved-form-value"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["wallet"]["consumerBalance"], "88.60")
        self.assertNotIn("api_key", payload)
        self.assertNotIn("rk-unsaved-form-value", response.get_data(as_text=True))
        get_wallet.assert_called_once_with(api_key="rk-unsaved-form-value")

    def test_page_contains_remail_mode_and_wallet_controls(self):
        response = self.client.get("/")
        config_response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("btnLoadRemailWallet", page)
        self.assertIn("remailWalletStatus", page)
        self.assertIn("remail-wallet-refresh", page)
        self.assertNotIn("<b>Remail 账户余额</b>", page)
        subhead_pos = page.index("html += configSubhead(activeSectionName, current.help);")
        wallet_pos = page.index("if (activeSectionName === 'Remail') html += renderRemailWalletTools();")
        fields_pos = page.index("for (const f of current.fields)")
        self.assertLess(subhead_pos, wallet_pos)
        self.assertLess(wallet_pos, fields_pos)
        fields = {item["key"]: item for item in config_response.get_json()}
        self.assertEqual(
            [item["value"] for item in fields["REMAIL_SERVICE_MODE"]["options"]],
            ["code", "purchase"],
        )


if __name__ == "__main__":
    unittest.main()
