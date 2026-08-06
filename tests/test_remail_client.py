# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import remail_client


def _response(payload, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    response.text = str(payload)
    response.headers = {}
    return response


class RemailClientTests(unittest.TestCase):
    def setUp(self):
        remail_client._CONTEXT_CACHE.clear()
        remail_client._SELECTION_CACHE.clear()
        project_patch = patch.object(remail_client._email_cfg, "REMAIL_PROJECT_ID", 0, create=True)
        product_patch = patch.object(remail_client._email_cfg, "REMAIL_PRODUCT_ID", 0, create=True)
        suffixes_patch = patch.object(remail_client._email_cfg, "REMAIL_EMAIL_SUFFIXES", [], create=True)
        mode_patch = patch.object(remail_client._email_cfg, "REMAIL_SERVICE_MODE", "code", create=True)
        project_patch.start()
        product_patch.start()
        suffixes_patch.start()
        mode_patch.start()
        self.addCleanup(project_patch.stop)
        self.addCleanup(product_patch.stop)
        self.addCleanup(suffixes_patch.stop)
        self.addCleanup(mode_patch.stop)

    def test_pick_account_requires_api_key(self):
        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "", create=True):
            with self.assertRaisesRegex(remail_client.RemailError, "Remail API Key"):
                remail_client.pick_account()

    def test_project_score_rejects_unrelated_microsoft_project(self):
        self.assertEqual(
            remail_client._project_score({"name": "Microsoft 账号验证码", "targetPlatform": "Microsoft"}),
            0,
        )

    def test_configured_email_suffixes_accept_multiple_formats_and_deduplicate(self):
        with patch.object(
            remail_client._email_cfg,
            "REMAIL_EMAIL_SUFFIXES",
            [" @OUTLOOK.COM, hotmail.com", "outlook.com\noutlook.fr"],
            create=True,
        ):
            self.assertEqual(
                remail_client._configured_email_suffixes(),
                ("outlook.com", "hotmail.com", "outlook.fr"),
            )

    def test_configured_email_suffixes_reject_invalid_value(self):
        with patch.object(
            remail_client._email_cfg,
            "REMAIL_EMAIL_SUFFIXES",
            ["outlook.com", "https://hotmail.com"],
            create=True,
        ):
            with self.assertRaisesRegex(remail_client.RemailError, "无效邮箱后缀"):
                remail_client._configured_email_suffixes()

    def test_service_mode_rejects_invalid_value(self):
        with patch.object(
            remail_client._email_cfg, "REMAIL_SERVICE_MODE", "archive", create=True
        ):
            with self.assertRaisesRegex(remail_client.RemailError, "code（短效）或 purchase（长效）"):
                remail_client._service_mode()

    def test_purchase_products_require_purchase_enabled_and_sort_by_purchase_price(self):
        products = [
            {
                "id": 2001,
                "type": "microsoft",
                "status": "enabled",
                "codeEnabled": True,
                "purchaseEnabled": False,
                "codePrice": "0.10",
                "purchasePrice": "0.10",
                "totalAvailable": 20,
            },
            {
                "id": 2002,
                "type": "microsoft",
                "status": "enabled",
                "purchaseEnabled": True,
                "purchasePrice": "2.00",
                "totalAvailable": 20,
            },
            {
                "id": 2003,
                "type": "microsoft",
                "status": "enabled",
                "purchaseEnabled": True,
                "purchasePrice": "1.20",
                "totalAvailable": 20,
            },
        ]

        eligible = remail_client._eligible_products(products, service_mode="purchase")

        self.assertEqual([item["id"] for item in eligible], [2003, 2002])

    @patch("core.remail_client.requests.request")
    def test_purchase_order_uses_purchase_service_mode(self, request):
        request.return_value = _response({
            "orderNo": "R202608070099",
            "status": "active",
            "deliveryEmail": "longterm@outlook.test",
            "serviceToken": "st-purchase-token",
        }, status_code=201)
        selection = remail_client.RemailSelection(
            project_id=1001,
            product_id=2002,
            project_name="OpenAI",
            product_type="microsoft",
            service_mode="purchase",
        )

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True):
            order = remail_client._create_order(selection)

        self.assertEqual(order["orderNo"], "R202608070099")
        self.assertEqual(
            request.call_args.kwargs["params"],
            {"serviceMode": "purchase", "supply": "private_first"},
        )

    @patch("core.remail_client.requests.request")
    def test_get_wallet_uses_override_key_and_returns_only_safe_fields(self, request):
        request.return_value = _response({
            "data": {
                "wallet": {
                    "userId": 7,
                    "consumerBalance": "168.50",
                    "supplierAvailable": "0.00",
                    "supplierFrozen": "0.00",
                    "historicalSpend": "391.20",
                    "orderCount": "486",
                    "updatedAt": "2026-08-07T00:30:00Z",
                    "apiKey": "must-not-leak",
                }
            }
        })

        wallet = remail_client.get_wallet(api_key="Bearer rk-form-value")

        self.assertEqual(wallet["consumerBalance"], "168.50")
        self.assertEqual(wallet["historicalSpend"], "391.20")
        self.assertEqual(wallet["orderCount"], 486)
        self.assertNotIn("apiKey", wallet)
        self.assertEqual(
            request.call_args.args,
            ("GET", "https://remail.aishop6.com/v1/open/wallet"),
        )
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer rk-form-value")

    @patch("core.remail_client.time.sleep")
    @patch("core.remail_client.requests.request")
    def test_project_request_recovers_from_ssl_error_by_switching_network_route(self, request, sleep):
        request.side_effect = [
            remail_client.requests.exceptions.SSLError("pickup?email=user@example.test&token=secret"),
            _response({"items": [], "total": 0, "offset": 0, "limit": 100}),
        ]

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True):
            self.assertEqual(remail_client._list_projects("OpenAI"), [])

        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["proxies"], remail_client._DIRECT_PROXIES)
        self.assertIsNone(request.call_args_list[1].kwargs["proxies"])
        sleep.assert_not_called()

    @patch("core.remail_client.time.sleep")
    @patch("core.remail_client.requests.request")
    def test_network_retries_do_not_expose_pickup_credentials(self, request, sleep):
        request.side_effect = [
            remail_client.requests.exceptions.SSLError(
                "https://remail.aishop6.com/v1/pickup?email=user@example.test&token=secret-token"
            )
        ] * remail_client.REQUEST_MAX_ATTEMPTS

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True):
            with self.assertRaises(remail_client.RemailError) as raised:
                remail_client._list_projects("OpenAI")

        self.assertEqual(request.call_count, remail_client.REQUEST_MAX_ATTEMPTS)
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 4.0])

    @patch("core.remail_client.time.sleep")
    @patch("core.remail_client.requests.request")
    def test_order_retry_after_522_reuses_idempotency_key(self, request, sleep):
        request.side_effect = [
            _response({"message": "Cloudflare origin timeout"}, status_code=522),
            _response({
                "orderNo": "R202608070001",
                "status": "active",
                "deliveryEmail": "fresh@outlook.test",
                "serviceToken": "st-test-token",
            }, status_code=201),
        ]
        selection = remail_client.RemailSelection(
            project_id=1001,
            product_id=2001,
            project_name="OpenAI",
            product_type="microsoft",
            email_suffixes=("outlook.com", "hotmail.com"),
        )

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True), patch(
            "core.remail_client.random.choice", return_value="outlook.com"
        ) as choice:
            order = remail_client._create_order(selection)

        self.assertEqual(order["orderNo"], "R202608070001")
        self.assertEqual(request.call_count, 2)
        first_key = request.call_args_list[0].kwargs["headers"]["Idempotency-Key"]
        second_key = request.call_args_list[1].kwargs["headers"]["Idempotency-Key"]
        self.assertEqual(first_key, second_key)
        self.assertEqual(
            request.call_args_list[0].kwargs["json"],
            {"projectId": 1001, "productId": 2001, "emailSuffix": "outlook.com"},
        )
        self.assertEqual(request.call_args_list[0].kwargs["json"], request.call_args_list[1].kwargs["json"])
        choice.assert_called_once_with(("outlook.com", "hotmail.com"))
        self.assertEqual(request.call_args_list[0].kwargs["proxies"], remail_client._DIRECT_PROXIES)
        self.assertIsNone(request.call_args_list[1].kwargs["proxies"])
        sleep.assert_not_called()

    @patch("core.remail_client.requests.request")
    def test_discovery_does_not_order_from_unrelated_microsoft_project(self, request):
        request.return_value = _response({
            "items": [{
                "id": 1001,
                "name": "Microsoft 账号验证码",
                "targetPlatform": "Microsoft",
                "status": "listed",
            }],
            "total": 1,
            "offset": 0,
            "limit": 100,
        })

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True):
            with self.assertRaisesRegex(remail_client.RemailError, "未找到 OpenAI/ChatGPT"):
                remail_client.pick_account()

        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))
        self.assertTrue(all(call.args[1].endswith("/v1/open/projects") for call in request.call_args_list))

    @patch("core.remail_client.requests.request")
    def test_explicit_project_and_product_override_skips_project_search(self, request):
        request.side_effect = [
            _response({
                "project": {"id": 1001, "name": "Custom project"},
                "products": [{
                    "id": 2001,
                    "type": "domain",
                    "status": "enabled",
                    "codeEnabled": True,
                    "totalAvailable": 3,
                }],
            }),
            _response({
                "orderNo": "R202608060002",
                "status": "active",
                "deliveryEmail": "custom@domain.test",
                "serviceToken": "st-custom-token",
            }),
        ]

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True), patch.object(
            remail_client._email_cfg, "REMAIL_PROJECT_ID", 1001, create=True
        ), patch.object(remail_client._email_cfg, "REMAIL_PRODUCT_ID", 2001, create=True):
            account = remail_client.pick_account()

        self.assertEqual(account.email, "custom@domain.test")
        self.assertEqual(request.call_args_list[0].args, ("GET", "https://remail.aishop6.com/v1/open/projects/1001"))

    @patch("core.remail_client.requests.request")
    def test_configured_suffixes_only_randomize_from_supported_stock(self, request):
        request.side_effect = [
            _response({
                "project": {"id": 1001, "name": "ChatGPT"},
                "products": [{
                    "id": 2001,
                    "type": "microsoft",
                    "status": "enabled",
                    "codeEnabled": True,
                    "totalAvailable": 15,
                    "suffixes": [
                        {"suffix": "outlook.com", "totalAvailable": 10, "publicAvailable": 10},
                        {"suffix": "hotmail.com", "totalAvailable": 0, "publicAvailable": 0},
                        {"suffix": "outlook.fr", "totalAvailable": 5, "publicAvailable": 5},
                    ],
                }],
            }),
            _response({
                "orderNo": "R202608070002",
                "status": "active",
                "deliveryEmail": "fresh@outlook.fr",
                "serviceToken": "st-suffix-token",
            }),
        ]

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True), patch.object(
            remail_client._email_cfg, "REMAIL_PROJECT_ID", 1001, create=True
        ), patch.object(remail_client._email_cfg, "REMAIL_PRODUCT_ID", 2001, create=True), patch.object(
            remail_client._email_cfg,
            "REMAIL_EMAIL_SUFFIXES",
            ["outlook.com", "hotmail.com", "outlook.fr"],
            create=True,
        ), patch("core.remail_client.random.choice", return_value="outlook.fr") as choice:
            account = remail_client.pick_account()

        self.assertEqual(account.email, "fresh@outlook.fr")
        choice.assert_called_once_with(("outlook.com", "outlook.fr"))
        self.assertEqual(
            request.call_args_list[1].kwargs["json"],
            {"projectId": 1001, "productId": 2001, "emailSuffix": "outlook.fr"},
        )

    @patch("core.remail_client.requests.request")
    def test_configured_product_rejects_unsupported_suffixes_before_order(self, request):
        request.return_value = _response({
            "project": {"id": 1001, "name": "ChatGPT"},
            "products": [{
                "id": 2001,
                "type": "microsoft",
                "status": "enabled",
                "codeEnabled": True,
                "totalAvailable": 10,
                "suffixes": [
                    {"suffix": "hotmail.com", "totalAvailable": 10, "publicAvailable": 10},
                ],
            }],
        })

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True), patch.object(
            remail_client._email_cfg, "REMAIL_PROJECT_ID", 1001, create=True
        ), patch.object(remail_client._email_cfg, "REMAIL_PRODUCT_ID", 2001, create=True), patch.object(
            remail_client._email_cfg, "REMAIL_EMAIL_SUFFIXES", ["outlook.com"], create=True
        ):
            with self.assertRaisesRegex(remail_client.RemailError, "emailSuffix"):
                remail_client.pick_account()

        request.assert_called_once()

    @patch("core.remail_client.requests.request")
    def test_pick_account_discovers_openai_product_and_creates_code_order(self, request):
        request.side_effect = [
            _response({
                "items": [{
                    "id": 1001,
                    "name": "OpenAI 验证码",
                    "targetPlatform": "OpenAI",
                    "status": "listed",
                }],
                "total": 1,
                "offset": 0,
                "limit": 100,
            }),
            _response({
                "project": {"id": 1001, "name": "OpenAI 验证码", "targetPlatform": "OpenAI"},
                "products": [{
                    "id": 2001,
                    "type": "microsoft",
                    "status": "enabled",
                    "codeEnabled": True,
                    "codePrice": "0.80",
                    "totalAvailable": 12,
                }],
            }),
            _response({
                "orderNo": "R202608060001",
                "status": "active",
                "deliveryEmail": "fresh@outlook.test",
                "serviceToken": "st-test-token",
            }, status_code=201),
        ]

        with patch.object(remail_client._email_cfg, "REMAIL_API_KEY", "rk-test", create=True):
            account = remail_client.pick_account()

        self.assertEqual(account.email, "fresh@outlook.test")
        self.assertEqual(account.service_token, "st-test-token")
        self.assertIs(remail_client.get_account_context(account.email), account)

        list_call, detail_call, order_call = request.call_args_list
        self.assertEqual(list_call.args, ("GET", "https://remail.aishop6.com/v1/open/projects"))
        self.assertEqual(list_call.kwargs["headers"]["Authorization"], "Bearer rk-test")
        self.assertEqual(list_call.kwargs["params"]["search"], "OpenAI")
        self.assertEqual(detail_call.args, ("GET", "https://remail.aishop6.com/v1/open/projects/1001"))
        self.assertEqual(order_call.args, ("POST", "https://remail.aishop6.com/v1/open/orders"))
        self.assertEqual(order_call.kwargs["params"], {"serviceMode": "code", "supply": "private_first"})
        self.assertEqual(order_call.kwargs["json"], {"projectId": 1001, "productId": 2001})
        self.assertTrue(order_call.kwargs["headers"]["Idempotency-Key"].startswith("turb-gpt-remail-"))

    @patch("core.remail_client.requests.request")
    def test_fetch_latest_otp_uses_pickup_service_token_without_api_key(self, request):
        account = remail_client.RemailAccount(
            email="fresh@outlook.test",
            service_token="st-test-token",
            order_no="R1",
            project_id=1001,
            product_id=2001,
        )
        remail_client._CONTEXT_CACHE[account.email] = account
        request.return_value = _response({
            "items": [{
                "id": 5012,
                "sender": "noreply@openai.com",
                "recipient": account.email,
                "receivedAt": "2026-08-06T15:30:05Z",
                "subject": "Your OpenAI verification code",
                "bodyPreview": "Your code is 654321.",
                "verificationCode": "654321",
            }],
            "fetch": {"lastStatus": "succeeded"},
        })

        code = remail_client.fetch_latest_otp(
            account.email,
            after_ts=1786030200,
            max_wait=1,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "654321")
        request.assert_called_once()
        call = request.call_args
        self.assertEqual(call.args, ("GET", "https://remail.aishop6.com/v1/pickup"))
        self.assertEqual(call.kwargs["params"], {"email": account.email, "token": "st-test-token"})
        self.assertNotIn("Authorization", call.kwargs["headers"])

    @patch("core.remail_client.requests.request")
    def test_fetch_latest_otp_reads_message_detail_when_preview_has_no_code(self, request):
        account = remail_client.RemailAccount(
            email="fresh@outlook.test",
            service_token="st-test-token",
            order_no="R1",
            project_id=1001,
            product_id=2001,
        )
        remail_client._CONTEXT_CACHE[account.email] = account
        request.side_effect = [
            _response({
                "items": [{
                    "id": 5012,
                    "sender": "account-security-noreply@accountprotection.microsoft.com",
                    "receivedAt": "2026-08-06T15:30:05Z",
                    "subject": "Microsoft account security code",
                    "bodyPreview": "Open this email to view your code.",
                }],
            }),
            _response({
                "id": 5012,
                "sender": "account-security-noreply@accountprotection.microsoft.com",
                "receivedAt": "2026-08-06T15:30:05Z",
                "subject": "Microsoft account security code",
                "body": "Security code: 112233",
            }),
        ]

        code = remail_client.fetch_latest_otp(
            account.email,
            max_wait=1,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "112233")
        self.assertEqual(
            request.call_args_list[1].args,
            ("GET", "https://remail.aishop6.com/v1/pickup/messages/5012"),
        )

    @patch("core.remail_client.requests.request")
    def test_fetch_latest_otp_returns_after_settle_without_waiting_for_fetch_cooldown(self, request):
        account = remail_client.RemailAccount(
            email="fresh@outlook.test",
            service_token="st-test-token",
            order_no="R1",
            project_id=1001,
            product_id=2001,
        )
        remail_client._CONTEXT_CACHE[account.email] = account
        request.return_value = _response({
            "items": [{
                "id": 5012,
                "sender": "noreply@openai.com",
                "receivedAt": "1970-01-01T00:16:40Z",
                "subject": "Your OpenAI verification code",
                "bodyPreview": "Your code is 654321.",
                "verificationCode": "654321",
            }],
            "fetch": {"nextFetchAllowedAt": "1970-01-01T00:18:20Z"},
        })

        class FakeClock:
            now = 0.0

            @classmethod
            def monotonic(cls):
                return cls.now

            @classmethod
            def sleep(cls, seconds):
                cls.now += seconds

        with patch("core.remail_client.time.monotonic", side_effect=FakeClock.monotonic), patch(
            "core.remail_client.time.time", return_value=1000.0
        ), patch("core.remail_client.time.sleep", side_effect=FakeClock.sleep):
            code = remail_client.fetch_latest_otp(
                account.email,
                max_wait=30,
                poll_interval=1,
                settle_seconds=5,
            )

        self.assertEqual(code, "654321")
        request.assert_called_once()
        self.assertEqual(FakeClock.now, 5.0)

    def test_fetch_latest_otp_requires_live_order_context(self):
        with self.assertRaisesRegex(remail_client.RemailError, "serviceToken"):
            remail_client.fetch_latest_otp("missing@example.test", max_wait=0)


if __name__ == "__main__":
    unittest.main()
