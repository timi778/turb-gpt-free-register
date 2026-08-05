import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from core import codex_oauth, db, platform_oauth_refresh_service
from webui.app import create_app


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class PlatformOAuthRefreshTests(unittest.TestCase):
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

    def _insert_oauth_account(self, root: Path) -> tuple[dict, int]:
        paths = self._db_paths(root)
        for path in paths.values():
            if path.suffix == ".json" and path.name != "groups.json":
                path.write_text("[]", encoding="utf-8")
        account_id = db.insert_account(
            email="oauth@example.test",
            access_token="session-at",
            registration_password="Saved-Pass-123!",
            extra={
                "unrelated": {"keep": True},
                "platform_oauth": {
                    "access_token": "old-platform-at",
                    "refresh_token": "old-platform-rt",
                    "id_token": "old-platform-id",
                },
            },
        )
        return paths, account_id

    def test_account_decoration_exposes_status_not_platform_tokens(self):
        decorated = db._decorate_account({
            "id": 7,
            "email": "safe@example.test",
            "extra_json": json.dumps({
                "platform_oauth": {
                    "access_token": "secret-platform-at",
                    "refresh_token": "secret-platform-rt",
                    "id_token": "secret-platform-id",
                    "refresh_status": "success",
                    "refresh_message": "OAuth Token 刷新成功",
                    "refreshed_at": "2026-08-05T12:00:00",
                }
            }),
        })
        self.assertTrue(decorated["platform_oauth_has_refresh_token"])
        self.assertEqual(decorated["platform_oauth_refresh_status"], "success")
        safe = {key: value for key, value in decorated.items() if key.startswith("platform_oauth_")}
        self.assertNotIn("secret-platform", repr(safe))

    def test_status_snapshot_never_returns_platform_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            for path in paths.values():
                if path.suffix == ".json" and path.name != "groups.json":
                    path.write_text("[]", encoding="utf-8")
            with patch.multiple(db, **paths):
                self._insert_oauth_account(root)
                snapshot = db.list_account_platform_oauth_statuses()
        serialized = json.dumps(snapshot)
        self.assertNotIn("old-platform-at", serialized)
        self.assertNotIn("old-platform-rt", serialized)
        self.assertNotIn("old-platform-id", serialized)

    def test_complete_refresh_preserves_omitted_tokens_and_unrelated_extra(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            for path in paths.values():
                if path.suffix == ".json" and path.name != "groups.json":
                    path.write_text("[]", encoding="utf-8")
            with patch.multiple(db, **paths), patch.object(db, "_now", return_value="2026-08-05T12:00:00"):
                _, account_id = self._insert_oauth_account(root)
                self.assertTrue(db.claim_account_platform_oauth_refresh(account_id, trigger="manual_bulk"))
                self.assertTrue(db.mark_account_platform_oauth_refresh_running(account_id))
                self.assertTrue(db.complete_account_platform_oauth_refresh(account_id, {
                    "ok": True,
                    "tokens": {"access_token": "new-platform-at", "expires_in": 3600},
                    "message": "OAuth Token 刷新成功",
                }))

            stored = json.loads(paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))[0]
            extra = json.loads(stored["extra_json"])
            oauth = extra["platform_oauth"]
            self.assertEqual(oauth["access_token"], "new-platform-at")
            self.assertEqual(oauth["refresh_token"], "old-platform-rt")
            self.assertEqual(oauth["id_token"], "old-platform-id")
            self.assertEqual(oauth["refresh_status"], "success")
            self.assertTrue(extra["unrelated"]["keep"])

    def test_failed_refresh_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            for path in paths.values():
                if path.suffix == ".json" and path.name != "groups.json":
                    path.write_text("[]", encoding="utf-8")
            with patch.multiple(db, **paths):
                _, account_id = self._insert_oauth_account(root)
                self.assertTrue(db.claim_account_platform_oauth_refresh(account_id))
                self.assertTrue(db.mark_account_platform_oauth_refresh_running(account_id))
                self.assertTrue(db.complete_account_platform_oauth_refresh(account_id, {
                    "ok": False,
                    "error": "invalid_grant",
                }))
            stored = json.loads(paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))[0]
            oauth = json.loads(stored["extra_json"])["platform_oauth"]
            self.assertEqual(oauth["refresh_token"], "old-platform-rt")
            self.assertEqual(oauth["refresh_status"], "failed")

    def test_exchange_posts_refresh_grant_once(self):
        session = Mock()
        session.post.return_value = _Response(200, {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "id_token": "new-id",
            "expires_in": 3600,
        })
        result = platform_oauth_refresh_service.exchange_refresh_token(
            "old-rt", session=session
        )
        self.assertEqual(result["refresh_token"], "new-rt")
        self.assertEqual(session.post.call_count, 1)
        kwargs = session.post.call_args.kwargs
        self.assertEqual(kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(kwargs["data"]["refresh_token"], "old-rt")

    def test_exchange_invalid_grant_is_safe_and_not_retried(self):
        session = Mock()
        session.post.return_value = _Response(400, {
            "error": "invalid_grant",
            "error_description": "Refresh token expired",
        })
        with self.assertRaisesRegex(RuntimeError, "invalid_grant") as raised:
            platform_oauth_refresh_service.exchange_refresh_token(
                "secret-old-rt", session=session
            )
        self.assertEqual(session.post.call_count, 1)
        self.assertNotIn("secret-old-rt", str(raised.exception))

    def test_exchange_timeout_is_not_retried(self):
        session = Mock()
        session.post.side_effect = requests.Timeout("timed out")
        with self.assertRaisesRegex(RuntimeError, "Timeout"):
            platform_oauth_refresh_service.exchange_refresh_token(
                "secret-old-rt", session=session
            )
        self.assertEqual(session.post.call_count, 1)

    def test_worker_persists_rotated_rt_and_uploads_full_codex_account(self):
        account = {
            "id": 7,
            "email": "oauth@example.test",
            "access_token": "session-at",
            "registration_password": "Saved-Pass-123!",
            "platform_oauth": {
                "access_token": "old-at",
                "refresh_token": "old-rt",
                "id_token": "old-id",
            },
        }
        tokens = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "id_token": "new-id",
            "expires_in": 3600,
        }
        with patch.object(db, "mark_account_platform_oauth_refresh_running", return_value=True), \
             patch.object(db, "get_account_platform_oauth_context", return_value=account), \
             patch.object(db, "complete_account_platform_oauth_refresh", return_value=True) as complete, \
             patch.object(db, "update_account_platform_oauth_sync_result", return_value=True), \
             patch.object(platform_oauth_refresh_service, "exchange_refresh_token", return_value=tokens), \
             patch.object(platform_oauth_refresh_service, "_save_codex_account", return_value="codex.json"), \
             patch.object(platform_oauth_refresh_service.chatgpt2api_client, "auto_upload_registered_account", return_value={
                 "ok": True, "status": "success", "mode": "rt"
             }) as upload:
            result = platform_oauth_refresh_service._run_one(
                {"id": 7, "email": "oauth@example.test"}, "batch-test", 1, 1
            )

        self.assertTrue(result["ok"])
        complete.assert_called_once()
        persisted = complete.call_args.args[1]["tokens"]
        self.assertEqual(persisted["refresh_token"], "new-rt")
        upload.assert_called_once()
        self.assertEqual(upload.call_args.kwargs["platform_oauth"]["refresh_token"], "new-rt")
        self.assertNotIn("new-rt", repr(result))

    def test_codex_credential_write_replaces_atomically_without_temp_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(codex_oauth, "_PROJECT_ROOT", root), \
                 patch.object(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts"):
                path = codex_oauth.save_codex_credential(
                    {"type": "codex", "refresh_token": "first"},
                    "oauth@example.test",
                    "",
                )
                path = codex_oauth.save_codex_credential(
                    {"type": "codex", "refresh_token": "second"},
                    "oauth@example.test",
                    "",
                )
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["refresh_token"], "second")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_oauth_refresh_bulk_api_is_safe_and_caps_workers(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        account = {
            "id": 7,
            "email": "oauth@example.test",
            "platform_oauth_has_refresh_token": True,
        }
        with patch("webui.app.db.get_account", return_value=account), \
             patch("webui.app.db.claim_account_platform_oauth_refresh", return_value=True), \
             patch.object(platform_oauth_refresh_service, "start_batch", return_value={
                 "batch_id": "batch-test", "workers": 3, "count": 1,
             }) as start:
            response = client.post("/api/accounts/oauth-refresh-bulk", json={
                "account_ids": [7],
                "workers": 99,
            })
        self.assertEqual(response.status_code, 202)
        body_text = json.dumps(response.get_json())
        self.assertNotIn("old-rt", body_text)
        self.assertNotIn("old-at", body_text)
        start.assert_called_once_with([
            {"id": 7, "email": "oauth@example.test"}
        ], 3)

    def test_accounts_api_removes_platform_oauth_extra_json(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        row = {
            "id": 7,
            "email": "oauth@example.test",
            "extra_json": json.dumps({
                "platform_oauth": {
                    "access_token": "secret-platform-at",
                    "refresh_token": "secret-platform-rt",
                    "id_token": "secret-platform-id",
                }
            }),
            "platform_oauth_has_refresh_token": True,
        }
        with patch("webui.app.db.list_accounts", return_value=[row]):
            response = client.get("/api/accounts")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()[0]
        self.assertNotIn("extra_json", payload)
        self.assertTrue(payload["platform_oauth_has_refresh_token"])
        serialized = json.dumps(payload)
        self.assertNotIn("secret-platform", serialized)


if __name__ == "__main__":
    unittest.main()
