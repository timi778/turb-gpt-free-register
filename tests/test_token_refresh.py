# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import db
from core import roxy_token_refresh
from core import token_refresh_service
from webui.app import create_app


class TokenRefreshTests(unittest.TestCase):
    def _db_paths(self, root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy_accounts.json",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_OUTLOOK_TXT": root / "outlook.txt",
            "_VIEWER_HTML": root / "viewer.html",
            "_ACCOUNT_GROUPS_JSON": root / "groups.json",
        }

    def test_legacy_account_expiry_falls_back_to_created_at(self):
        row = db._decorate_account({
            "id": 1,
            "email": "legacy@example.test",
            "access_token": "old-token",
            "created_at": "2026-01-01T00:00:00",
        })
        self.assertEqual(row["access_token_issued_at"], "2026-01-01T00:00:00")
        self.assertEqual(row["access_token_expires_at"], "2026-01-11T00:00:00")

    def test_complete_refresh_replaces_token_and_resets_issue_time(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            for path in paths.values():
                if path.suffix == ".json" and path.name != "groups.json":
                    path.write_text("[]", encoding="utf-8")
            with patch.multiple(db, **paths), patch.object(db, "_now", return_value="2026-08-01T12:00:00"):
                account_id = db.insert_account(
                    email="refresh@example.test",
                    access_token="old-token",
                    registration_password="Saved-Pass-123!",
                )
                self.assertTrue(db.claim_account_token_refresh(account_id))
                self.assertTrue(db.mark_account_token_refresh_running(account_id))
                self.assertTrue(db.complete_account_token_refresh(account_id, {
                    "ok": True,
                    "access_token": "new-token",
                    "session_info": {"expires": "2026-08-11T12:00:00"},
                }))
                stored = json.loads(paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))[0]
                self.assertEqual(stored["access_token"], "new-token")
                self.assertEqual(stored["access_token_updated_at"], "2026-08-01T12:00:00")
                self.assertEqual(stored["token_refresh_status"], "success")
                self.assertEqual(db.get_account(account_id)["access_token_expires_at"], "2026-08-11T12:00:00")

    def test_batch_worker_persists_success_and_does_not_log_password(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            for path in paths.values():
                if path.suffix == ".json" and path.name != "groups.json":
                    path.write_text("[]", encoding="utf-8")
            with patch.multiple(db, **paths):
                account_id = db.insert_account(
                    email="worker@example.test",
                    access_token="old-token",
                    registration_password="Worker-Pass-123!",
                )
                self.assertTrue(db.claim_account_token_refresh(account_id))
                with patch.object(token_refresh_service, "run_roxy_token_refresh", return_value={
                    "ok": True,
                    "access_token": "worker-new-token",
                    "session_info": {"expires": "2026-08-11T12:00:00"},
                }) as refresh:
                    result = token_refresh_service._run_one(
                        {"id": account_id, "email": "worker@example.test", "password": "Worker-Pass-123!"},
                        "batch-test",
                        1,
                        1,
                    )
                refresh.assert_called_once_with(email="worker@example.test", password="Worker-Pass-123!")
                self.assertTrue(result["ok"])
                self.assertEqual(db.get_account(account_id)["access_token"], "worker-new-token")
        self.assertNotIn("password", result)

    def test_initial_email_verification_switches_to_password_without_fetching_otp(self):
        driver = Mock()
        client = Mock()
        opened = SimpleNamespace(profile_id="profile-test")
        client.open_profile.return_value = opened
        events = []

        with patch.object(roxy_token_refresh, "RoxyBrowserClient", return_value=client), \
             patch.object(roxy_token_refresh, "_build_driver", return_value=driver), \
             patch.object(roxy_token_refresh, "_center_browser_window"), \
             patch.object(roxy_token_refresh, "_maybe_accept"), \
             patch.object(roxy_token_refresh, "_type_email_address"), \
             patch.object(roxy_token_refresh, "_submit_email_step"), \
             patch.object(roxy_token_refresh, "_wait_email_submit_next_state", return_value="otp"), \
             patch.object(roxy_token_refresh, "_switch_email_verification_to_password", side_effect=lambda _driver: events.append("switch") or "login_password"), \
             patch.object(roxy_token_refresh, "_fill_login_password", side_effect=lambda _driver, _password: events.append("password")) as fill_password, \
             patch.object(roxy_token_refresh, "_wait_after_password_submit", return_value="logged_in"), \
             patch.object(roxy_token_refresh, "_complete_email_otp", side_effect=lambda *_args: events.append("otp")) as complete_otp, \
             patch.object(roxy_token_refresh, "_fetch_chatgpt_session", return_value={"accessToken": "new-token"}), \
             patch.object(roxy_token_refresh, "human_delay"), \
             patch.object(roxy_token_refresh.time, "sleep"):
            result = roxy_token_refresh.run_roxy_token_refresh("login@example.test", "Saved-Pass-123!")

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["switch", "password"])
        fill_password.assert_called_once_with(driver, "Saved-Pass-123!")
        complete_otp.assert_not_called()

    def test_password_switch_uses_captured_href_when_spa_click_does_not_navigate(self):
        class Driver:
            current_url = "https://auth.openai.com/email-verification"

            def __init__(self):
                self.visited = []

            def execute_script(self, _script):
                return {
                    "ok": True,
                    "score": 170,
                    "attrs": "/log-in/password",
                    "text": "continuewithpassword",
                    "href": "https://auth.openai.com/log-in/password?state=preserved",
                }

            def get(self, url):
                self.visited.append(url)
                self.current_url = url

        driver = Driver()
        clock = [0.0]

        def tick():
            return clock[0]

        def sleep(seconds):
            clock[0] += max(float(seconds), 0.1)

        with patch.object(roxy_token_refresh, "_is_login_password_page", side_effect=lambda d: "/log-in/password" in d.current_url), \
             patch.object(roxy_token_refresh, "_has_access_token", return_value=False), \
             patch.object(roxy_token_refresh, "human_delay"), \
             patch.object(roxy_token_refresh.time, "time", side_effect=tick), \
             patch.object(roxy_token_refresh.time, "sleep", side_effect=sleep):
            state = roxy_token_refresh._switch_email_verification_to_password(driver, timeout=1)

        self.assertEqual(state, "login_password")
        self.assertEqual(driver.visited, ["https://auth.openai.com/log-in/password?state=preserved"])

    def test_password_submit_detects_openai_http_500_page(self):
        driver = Mock(current_url="https://auth.openai.com/log-in/password")
        driver.execute_script.return_value = {
            "url": "https://auth.openai.com/log-in/password",
            "title": "auth.openai.com",
            "text": "auth.openai.com is currently unable to handle this request. HTTP ERROR 500",
            "errors": [],
        }
        with patch.object(roxy_token_refresh, "_has_access_token", return_value=False), \
             patch.object(roxy_token_refresh, "_is_email_verification_page", return_value=False), \
             patch.object(roxy_token_refresh, "_is_login_password_page", return_value=False):
            state = roxy_token_refresh._wait_after_password_submit(driver, timeout=1)

        self.assertEqual(state, "transient_error")

    def test_password_submit_retries_once_after_openai_http_500(self):
        driver = Mock()
        client = Mock()
        opened = SimpleNamespace(profile_id="profile-test")
        client.open_profile.return_value = opened

        with patch.object(roxy_token_refresh, "RoxyBrowserClient", return_value=client), \
             patch.object(roxy_token_refresh, "_build_driver", return_value=driver), \
             patch.object(roxy_token_refresh, "_center_browser_window"), \
             patch.object(roxy_token_refresh, "_maybe_accept"), \
             patch.object(roxy_token_refresh, "_type_email_address"), \
             patch.object(roxy_token_refresh, "_submit_email_step"), \
             patch.object(roxy_token_refresh, "_wait_email_submit_next_state", return_value="login_password"), \
             patch.object(roxy_token_refresh, "_fill_login_password") as fill_password, \
             patch.object(roxy_token_refresh, "_wait_after_password_submit", side_effect=["transient_error", "logged_in"]), \
             patch.object(roxy_token_refresh, "_recover_from_transient_auth_error") as recover, \
             patch.object(roxy_token_refresh, "_fetch_chatgpt_session", return_value={"accessToken": "new-token"}), \
             patch.object(roxy_token_refresh, "human_delay"), \
             patch.object(roxy_token_refresh.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(roxy_token_refresh.time, "sleep"):
            result = roxy_token_refresh.run_roxy_token_refresh("login@example.test", "Saved-Pass-123!")

        self.assertTrue(result["ok"])
        self.assertEqual(fill_password.call_count, 2)
        recover.assert_called_once_with(driver)

    def test_email_otp_is_only_used_after_password_submit_requests_it(self):
        driver = Mock()
        client = Mock()
        opened = SimpleNamespace(profile_id="profile-test")
        client.open_profile.return_value = opened

        with patch.object(roxy_token_refresh, "RoxyBrowserClient", return_value=client), \
             patch.object(roxy_token_refresh, "_build_driver", return_value=driver), \
             patch.object(roxy_token_refresh, "_center_browser_window"), \
             patch.object(roxy_token_refresh, "_maybe_accept"), \
             patch.object(roxy_token_refresh, "_type_email_address"), \
             patch.object(roxy_token_refresh, "_submit_email_step"), \
             patch.object(roxy_token_refresh, "_wait_email_submit_next_state", return_value="login_password"), \
             patch.object(roxy_token_refresh, "_fill_login_password"), \
             patch.object(roxy_token_refresh, "_wait_after_password_submit", return_value="otp"), \
             patch.object(roxy_token_refresh, "_complete_email_otp") as complete_otp, \
             patch.object(roxy_token_refresh, "_fetch_chatgpt_session", return_value={"accessToken": "new-token"}), \
             patch.object(roxy_token_refresh, "human_delay"), \
             patch.object(roxy_token_refresh.time, "time", return_value=321.0), \
             patch.object(roxy_token_refresh.time, "sleep"):
            result = roxy_token_refresh.run_roxy_token_refresh("login@example.test", "Saved-Pass-123!")

        self.assertTrue(result["ok"])
        complete_otp.assert_called_once_with(driver, "login@example.test", 321.0)

    def test_webui_bulk_refresh_skips_accounts_without_password(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        with patch("webui.app.db.get_account", return_value={
            "id": 1,
            "email": "no-password@example.test",
            "registration_password": "",
        }), patch.object(token_refresh_service, "start_batch") as start_batch:
            response = client.post("/api/accounts/token-refresh-bulk", json={
                "account_ids": [1],
                "workers": 99,
            })
        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["skipped"][0]["reason"], "账号未保存密码")
        start_batch.assert_not_called()

    def test_webui_bulk_refresh_starts_mixed_selection_with_worker_limit(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        accounts = {
            1: {"id": 1, "email": "ready@example.test", "registration_password": "Pass-1"},
            2: {"id": 2, "email": "missing@example.test", "registration_password": ""},
        }
        with patch("webui.app.db.get_account", side_effect=lambda acc_id: accounts.get(acc_id)), \
             patch("webui.app.db.claim_account_token_refresh", return_value=True) as claim, \
             patch.object(token_refresh_service, "start_batch", return_value={
                 "batch_id": "batch-test", "workers": 16, "count": 1,
             }) as start_batch:
            response = client.post("/api/accounts/token-refresh-bulk", json={
                "account_ids": [1, 2],
                "workers": 99,
            })
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(payload["skipped_count"], 1)
        self.assertEqual(payload["workers"], 16)
        claim.assert_called_once_with(1, trigger="manual_bulk")
        start_batch.assert_called_once_with([
            {"id": 1, "email": "ready@example.test", "password": "Pass-1"},
        ], 16)


if __name__ == "__main__":
    unittest.main()
