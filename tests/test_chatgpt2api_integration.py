import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config.env_loader import SECRET_ENV_KEYS
from webui.app import create_app
from webui.config_editor import EDITABLE_FIELDS


def _http_response(status=200, payload=None, text=""):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload if payload is not None else {}
    response.text = text
    return response


class Chatgpt2ApiClientTests(unittest.TestCase):
    @patch("core.chatgpt2api_client.requests.request")
    def test_connection_uses_bearer_auth_and_accounts_endpoint(self, request):
        from core.chatgpt2api_client import test_connection

        request.return_value = _http_response(payload={"items": [{"id": 1}]})
        result = test_connection("https://pool.example.com/", "admin-key")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_count"], 1)
        args, kwargs = request.call_args
        self.assertEqual(args[:2], ("GET", "https://pool.example.com/api/accounts"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer admin-key")

    @patch("core.chatgpt2api_client.requests.request")
    def test_refresh_token_prefers_full_codex_account_payload(self, request):
        from core.chatgpt2api_client import upload_account

        request.return_value = _http_response(payload={"added": 1, "skipped": 0})
        result = upload_account(
            "session-at",
            platform_oauth={
                "access_token": "platform-at",
                "refresh_token": "platform-rt",
                "id_token": "platform-id",
            },
            email="user@example.com",
            password="password-123",
            base_url="https://pool.example.com",
            admin_key="admin-key",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "rt")
        account = request.call_args.kwargs["json"]["accounts"][0]
        self.assertEqual(account, {
            "type": "codex",
            "email": "user@example.com",
            "password": "password-123",
            "access_token": "platform-at",
            "refresh_token": "platform-rt",
            "id_token": "platform-id",
            "source_type": "codex",
        })

    @patch("core.chatgpt2api_client.requests.request")
    def test_missing_refresh_token_falls_back_to_session_access_token(self, request):
        from core.chatgpt2api_client import upload_account

        request.return_value = _http_response(payload={"added": 1})
        result = upload_account(
            "session-at",
            platform_oauth={"access_token": "platform-at", "refresh_token": ""},
            base_url="https://pool.example.com",
            admin_key="admin-key",
        )

        self.assertEqual(result["mode"], "at")
        self.assertEqual(request.call_args.kwargs["json"], {"tokens": ["session-at"]})

    @patch("core.chatgpt2api_client.requests.request")
    def test_rt_upload_failure_does_not_retry_as_at(self, request):
        from core.chatgpt2api_client import upload_account

        request.return_value = _http_response(status=500, text="failed")
        result = upload_account(
            "session-at",
            platform_oauth={"access_token": "platform-at", "refresh_token": "platform-rt"},
            base_url="https://pool.example.com",
            admin_key="admin-key",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "rt")
        request.assert_called_once()

    @patch("core.chatgpt2api_client._ABNORMAL_ACCOUNT_PAGE_SIZE", 2)
    @patch("core.chatgpt2api_client.requests.request")
    def test_list_abnormal_accounts_paginates_and_projects_safe_fields(self, request):
        from core.chatgpt2api_client import list_abnormal_accounts

        request.side_effect = [
            _http_response(payload={"total": 3, "items": [
                {
                    "id": 11,
                    "email": "One@Example.com",
                    "status_category": "abnormal",
                    "status_label": "异常",
                    "status_reason_code": "auth_invalid",
                    "status_reason": "登录态失效",
                    "refresh_token": "must-not-leak",
                },
                {
                    "id": 12,
                    "email": "two@example.com",
                    "status_category": "abnormal",
                    "status_reason": "请求被拒绝",
                },
            ]}),
            _http_response(payload={"total": 3, "items": [{
                "id": 13,
                "email": "three@example.com",
                "status_category": "abnormal",
                "status_reason": "账号不可用",
            }]}),
        ]

        result = list_abnormal_accounts("https://pool.example.com/", "admin-key")

        self.assertTrue(result["ok"])
        self.assertEqual(result["abnormal_count"], 3)
        self.assertEqual(result["email_count"], 3)
        self.assertNotIn("refresh_token", result["accounts"][0])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["params"], {
            "status": "abnormal",
            "page": 1,
            "page_size": 2,
        })
        self.assertEqual(request.call_args_list[1].kwargs["params"]["page"], 2)
        self.assertEqual(
            request.call_args_list[0].kwargs["headers"]["Authorization"],
            "Bearer admin-key",
        )

    @patch("core.chatgpt2api_client.requests.request")
    def test_list_abnormal_accounts_rejects_non_abnormal_projection(self, request):
        from core.chatgpt2api_client import list_abnormal_accounts

        request.return_value = _http_response(payload={"total": 1, "items": [{
            "id": 21,
            "email": "normal@example.com",
            "status_category": "normal",
        }]})

        result = list_abnormal_accounts("https://pool.example.com", "admin-key")

        self.assertFalse(result["ok"])
        self.assertIn("非异常账号", result["error"])

    @patch("core.chatgpt2api_client.requests.request")
    def test_list_abnormal_accounts_reports_auth_and_malformed_response(self, request):
        from core.chatgpt2api_client import list_abnormal_accounts

        request.return_value = _http_response(status=401, text="unauthorized")
        auth_result = list_abnormal_accounts("https://pool.example.com", "bad-key")
        self.assertFalse(auth_result["ok"])
        self.assertEqual(auth_result["http_status"], 401)

        request.return_value = _http_response(payload={"data": []})
        malformed_result = list_abnormal_accounts("https://pool.example.com", "admin-key")
        self.assertFalse(malformed_result["ok"])
        self.assertIn("items", malformed_result["error"])


class Chatgpt2ApiConfigAndWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_admin_key_is_secret_env_configuration(self):
        field = next(item for item in EDITABLE_FIELDS if item["key"] == "CHATGPT2API_ADMIN_KEY")
        self.assertEqual(field["group"], "chatgpt2api")
        self.assertTrue(field["secret"])
        self.assertEqual(field["storage"], "env")
        self.assertIn("CHATGPT2API_ADMIN_KEY", SECRET_ENV_KEYS)

    @patch("core.chatgpt2api_client.test_connection")
    def test_connection_endpoint_uses_unsaved_form_values(self, test_connection):
        test_connection.return_value = {"ok": True, "status": 200, "account_count": 2}

        response = self.client.post("/api/chatgpt2api/test", json={
            "base_url": "https://pool.example.com",
            "admin_key": "admin-key",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        test_connection.assert_called_once_with("https://pool.example.com", "admin-key")

    def test_index_contains_connection_test_control_and_status(self):
        source = Path("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn("btnTestChatgpt2Api", source)
        self.assertIn("chatgpt2ApiStatus", source)
        self.assertIn("连接正常", source)


class AccountSaveUploadIsolationTests(unittest.TestCase):
    @patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": False})
    @patch("core.chatgpt2api_client.auto_upload_registered_account")
    @patch("core.account_export._append_batch_archive")
    @patch("core.db.insert_account", return_value=77)
    def test_upload_failure_does_not_change_local_save_result(
        self, insert_account, append_archive, auto_upload, enqueue
    ):
        from core.account_export import save_account_data

        append_archive.return_value = Path("accounts/batch")
        auto_upload.side_effect = RuntimeError("upload unavailable")

        row_id = save_account_data(
            "user@example.com",
            "session-at",
            extra={"registration_password": "pw", "platform_oauth": {"refresh_token": "rt"}},
        )

        self.assertEqual(row_id, 77)
        insert_account.assert_called_once()
        auto_upload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
