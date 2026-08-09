# -*- coding: utf-8 -*-
import json
import logging
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core import db, remail_client, remail_relogin_service
from webui.app import create_app


class RemailLongLivedPersistenceTests(unittest.TestCase):
    def setUp(self):
        remail_client._CONTEXT_CACHE.clear()

    def tearDown(self):
        remail_client._CONTEXT_CACHE.clear()

    def test_long_lived_context_survives_restart_and_old_receive_until_does_not_block_pickup(self):
        with tempfile.TemporaryDirectory() as td:
            persisted = Path(td) / "remail_long_lived_accounts.json"
            account = remail_client.RemailAccount(
                email="month-old@outlook.test",
                service_token="st-month-old",
                order_no="R-LONG-1",
                project_id=1001,
                product_id=2001,
                service_mode="purchase",
                receive_until="2026-01-02T00:00:00Z",
                after_sale_until="2026-01-03T00:00:00Z",
            )
            with patch.object(remail_client, "_PERSISTED_CONTEXT_PATH", persisted):
                remail_client._persist_context(account)
                remail_client._CONTEXT_CACHE.clear()

                with patch("core.db.get_account_by_email", return_value=None):
                    restored = remail_client.get_account_context(account.email)

                self.assertIsNotNone(restored)
                self.assertTrue(restored.is_long_lived)
                self.assertEqual(restored.service_token, "st-month-old")
                self.assertEqual(restored.receive_until, "2026-01-02T00:00:00Z")

                with patch.object(remail_client, "_request", return_value={
                    "items": [{
                        "id": 5012,
                        "receivedAt": "2026-08-10T12:00:00Z",
                        "subject": "Your ChatGPT code",
                        "verificationCode": "654321",
                    }],
                    "fetch": {"lastStatus": "succeeded"},
                }) as request, patch.object(
                    remail_client, "refresh_account_context", return_value=restored
                ) as refresh:
                    code = remail_client.fetch_latest_otp(
                        account.email,
                        max_wait=1,
                        settle_seconds=0,
                    )

            self.assertEqual(code, "654321")
            request.assert_called_once_with(
                "GET",
                "/v1/pickup",
                params={"email": account.email, "token": "st-month-old"},
                authenticated=False,
            )
            refresh.assert_called_once_with(account.email)

    def test_short_lived_context_is_not_persisted_and_is_cleared_on_release(self):
        with tempfile.TemporaryDirectory() as td:
            persisted = Path(td) / "remail_long_lived_accounts.json"
            account = remail_client.RemailAccount(
                email="short@outlook.test",
                service_token="st-short",
                order_no="R-SHORT-1",
                project_id=1001,
                product_id=2001,
                service_mode="code",
            )
            with patch.object(remail_client, "_PERSISTED_CONTEXT_PATH", persisted):
                remail_client._CONTEXT_CACHE[account.email.lower()] = account
                remail_client._persist_context(account)
                self.assertFalse(persisted.exists())

                remail_client.release_account(account.email, status="used")

            self.assertNotIn(account.email.lower(), remail_client._CONTEXT_CACHE)
            self.assertFalse(persisted.exists())

    def test_missing_context_file_can_be_rebuilt_from_registered_account(self):
        with tempfile.TemporaryDirectory() as td:
            persisted = Path(td) / "remail_long_lived_accounts.json"
            stored = {
                "email": "restore@outlook.test",
                "service_token": "st-restore",
                "order_no": "R-RESTORE-1",
                "project_id": 1001,
                "product_id": 2001,
                "service_mode": "purchase",
                "receive_until": "2026-01-01T00:00:00Z",
            }
            row = {"extra_json": json.dumps({"remail": stored})}
            with patch.object(remail_client, "_PERSISTED_CONTEXT_PATH", persisted), patch(
                "core.db.get_account_by_email", return_value=row
            ):
                restored = remail_client.get_account_context("restore@outlook.test")

            self.assertIsNotNone(restored)
            self.assertEqual(restored.service_token, "st-restore")
            self.assertTrue(persisted.exists())
            self.assertIn("st-restore", persisted.read_text(encoding="utf-8"))

    def test_refresh_long_lived_context_persists_activation_and_warranty_times(self):
        email = "refresh-times@outlook.test"
        account = remail_client.RemailAccount(
            email=email,
            service_token="st-refresh-times",
            order_no="R-REFRESH-TIMES",
            project_id=1001,
            product_id=2001,
            service_mode="purchase",
            receive_started_at="2026-08-10T06:39:53+08:00",
            receive_until="2026-08-10T07:39:53+08:00",
        )
        order_payload = {
            "order": {
                "orderNo": "R-REFRESH-TIMES",
                "status": "active",
                "receiveStartedAt": "2026-08-10T06:39:53+08:00",
                "receiveUntil": "2026-08-10T07:39:53+08:00",
                "activatedAt": "2026-08-10T06:40:24+08:00",
                "afterSaleUntil": "2026-08-11T06:39:53+08:00",
                "lastMailReceivedAt": "2026-08-09T22:40:24Z",
            }
        }
        with tempfile.TemporaryDirectory() as td:
            persisted = Path(td) / "remail_long_lived_accounts.json"
            remail_client._CONTEXT_CACHE[email] = account
            with patch.object(remail_client, "_PERSISTED_CONTEXT_PATH", persisted), patch.object(
                remail_client, "_request", return_value=order_payload
            ) as request, patch.object(
                db, "update_account_remail_context", return_value=True
            ) as update_account:
                refreshed = remail_client.refresh_account_context(email)

            stored = json.loads(persisted.read_text(encoding="utf-8"))[email]

        self.assertEqual(refreshed.activated_at, "2026-08-10T06:40:24+08:00")
        self.assertEqual(refreshed.after_sale_until, "2026-08-11T06:39:53+08:00")
        self.assertEqual(stored["activated_at"], "2026-08-10T06:40:24+08:00")
        self.assertEqual(stored["after_sale_until"], "2026-08-11T06:39:53+08:00")
        request.assert_called_once_with("GET", "/v1/open/orders/R-REFRESH-TIMES")
        updated_context = update_account.call_args.args[1]
        self.assertEqual(updated_context["activated_at"], "2026-08-10T06:40:24+08:00")
        self.assertEqual(updated_context["after_sale_until"], "2026-08-11T06:39:53+08:00")

    def test_batch_pickup_status_check_maps_success_and_api_error(self):
        first = remail_client.RemailAccount(
            email="pickup-ok@outlook.test",
            service_token="st-pickup-ok",
            order_no="R-PICKUP-OK",
            project_id=1001,
            product_id=2001,
            service_mode="purchase",
        )
        second = remail_client.RemailAccount(
            email="pickup-bad@outlook.test",
            service_token="st-pickup-bad",
            order_no="R-PICKUP-BAD",
            project_id=1001,
            product_id=2001,
            service_mode="purchase",
        )
        remail_client._CONTEXT_CACHE[first.email] = first
        remail_client._CONTEXT_CACHE[second.email] = second
        response = [
            {"index": 0, "status": "succeeded", "data": {"items": []}},
            {
                "index": 1,
                "status": "failed",
                "error": {"code": "credential_invalid", "message": "Credential expired."},
            },
        ]
        with patch.object(remail_client, "_request", return_value=response) as request:
            statuses = remail_client.check_pickup_statuses([first.email, second.email])

        self.assertEqual(statuses[first.email]["status"], "available")
        self.assertEqual(statuses[second.email]["status"], "credential_invalid")
        request.assert_called_once_with(
            "POST",
            "/v1/pickup/batch",
            json={"items": [
                {"email": first.email, "token": first.service_token},
                {"email": second.email, "token": second.service_token},
            ]},
            authenticated=False,
            max_attempts=1,
        )

    def test_single_pickup_status_check_maps_rate_limit(self):
        account = remail_client.RemailAccount(
            email="pickup-rate@outlook.test",
            service_token="st-pickup-rate",
            order_no="R-PICKUP-RATE",
            project_id=1001,
            product_id=2001,
            service_mode="purchase",
        )
        remail_client._CONTEXT_CACHE[account.email] = account
        with patch.object(
            remail_client,
            "_request",
            side_effect=remail_client.RemailError("Remail 请求失败: HTTP 429; rate_limited"),
        ):
            statuses = remail_client.check_pickup_statuses([account.email])

        self.assertEqual(statuses[account.email]["status"], "rate_limited")

    @patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True})
    @patch("core.chatgpt2api_client.auto_upload_registered_account")
    @patch("core.account_export._append_batch_archive", return_value=Path("accounts/batch"))
    @patch("core.db.insert_account", return_value=77)
    @patch("core.remail_client.export_account_context")
    def test_successful_registration_stores_long_lived_context_and_driver(
        self,
        export_context,
        insert_account,
        _append_archive,
        _auto_upload,
        _enqueue,
    ):
        from core.account_export import save_account_data
        from config import roxybrowser as roxy_cfg

        export_context.return_value = {
            "email": "saved@outlook.test",
            "service_token": "st-saved",
            "order_no": "R-SAVED-1",
            "service_mode": "purchase",
        }
        with patch.object(roxy_cfg, "REGISTRATION_DRIVER", "skyvern"):
            row_id = save_account_data(
                "saved@outlook.test",
                "session-at",
                extra={"registration_password": "Saved-Pass-123!"},
                email_source="remail",
            )

        self.assertEqual(row_id, 77)
        saved_extra = insert_account.call_args.kwargs["extra"]
        self.assertEqual(saved_extra["registration_driver"], "skyvern")
        self.assertEqual(saved_extra["remail"]["service_token"], "st-saved")
        self.assertEqual(saved_extra["remail"]["service_mode"], "purchase")


class RemailReloginDatabaseTests(unittest.TestCase):
    @staticmethod
    def _db_paths(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy_accounts.json",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy_outlook.json",
            "_OUTLOOK_TXT": root / "outlook.txt",
            "_VIEWER_HTML": root / "viewer.html",
            "_ACCOUNT_GROUPS_JSON": root / "groups.json",
        }

    @staticmethod
    def _initialize(paths: dict) -> None:
        for key in ("_ACCOUNTS_JSON", "_OUTLOOK_JSON", "_ACCOUNT_GROUPS_JSON"):
            paths[key].write_text("[]", encoding="utf-8")

    @staticmethod
    def _long_lived_extra() -> dict:
        return {
            "unrelated": {"keep": True},
            "registration_driver": "browser_use",
            "remail": {
                "email": "db-long@outlook.test",
                "service_token": "st-db-long",
                "order_no": "R-DB-1",
                "service_mode": "purchase",
                "receive_until": "2026-01-01T00:00:00Z",
            },
            "platform_oauth": {
                "access_token": "old-platform-at",
                "refresh_token": "old-platform-rt",
                "id_token": "old-platform-id",
            },
        }

    def test_claim_run_and_complete_updates_tokens_without_losing_remail_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            self._initialize(paths)
            with patch.multiple(db, **paths):
                account_id = db.insert_account(
                    email="db-long@outlook.test",
                    access_token="old-session-at",
                    registration_password="Saved-Pass-123!",
                    extra=self._long_lived_extra(),
                )
                self.assertTrue(db.get_account(account_id)["remail_long_lived"])
                self.assertTrue(db.claim_account_remail_relogin(account_id, trigger="manual_bulk"))
                self.assertFalse(db.claim_account_remail_relogin(account_id))
                self.assertTrue(db.mark_account_remail_relogin_running(account_id))
                self.assertTrue(db.complete_account_remail_relogin(account_id, {
                    "status": "success",
                    "access_token": "new-session-at",
                    "driver": "browser_use",
                    "message": "补登成功，已获取 ChatGPT AT/Platform OAuth RT",
                    "session_info": {
                        "user": {"id": "user-1", "name": "Test User"},
                        "account": {"planType": "free"},
                        "expires": "2026-08-20T12:00:00Z",
                    },
                    "platform_oauth": {
                        "access_token": "new-platform-at",
                        "refresh_token": "new-platform-rt",
                        "id_token": "new-platform-id",
                        "has_refresh_token": True,
                        "file_path": "codex_accounts/db-long.json",
                    },
                    "credential": {"status": "success", "message": "Codex 凭证已更新"},
                    "upload": {"status": "success", "message": "上传成功"},
                }))

                decorated = db.get_account(account_id)

            stored = json.loads(paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))[0]
            extra = json.loads(stored["extra_json"])
            self.assertEqual(stored["access_token"], "new-session-at")
            self.assertEqual(stored["remail_relogin_status"], "success")
            self.assertEqual(extra["remail"]["service_token"], "st-db-long")
            self.assertEqual(extra["platform_oauth"]["refresh_token"], "new-platform-rt")
            self.assertTrue(extra["unrelated"]["keep"])
            self.assertEqual(decorated["remail_relogin_upload_status"], "success")
            self.assertTrue(decorated["platform_oauth_has_refresh_token"])

    def test_interrupted_relogin_is_recovered_and_can_be_claimed_again(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            self._initialize(paths)
            with patch.multiple(db, **paths):
                account_id = db.insert_account(
                    email="db-long@outlook.test",
                    access_token="old-session-at",
                    extra=self._long_lived_extra(),
                )
                self.assertTrue(db.claim_account_remail_relogin(account_id))
                self.assertTrue(db.mark_account_remail_relogin_running(account_id))
                self.assertEqual(db.recover_interrupted_remail_relogins(), 1)
                self.assertEqual(db.get_account(account_id)["remail_relogin_status"], "failed")
                self.assertTrue(db.claim_account_remail_relogin(account_id))

    def test_remail_context_refresh_updates_display_times_without_losing_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            self._initialize(paths)
            with patch.multiple(db, **paths):
                account_id = db.insert_account(
                    email="db-long@outlook.test",
                    access_token="session-at",
                    extra=self._long_lived_extra(),
                )
                self.assertTrue(db.update_account_remail_context(
                    "db-long@outlook.test",
                    {
                        "receive_started_at": "2026-08-10T06:39:53+08:00",
                        "receive_until": "2026-08-10T07:39:53+08:00",
                        "activated_at": "2026-08-10T06:40:24+08:00",
                        "after_sale_until": "2026-08-11T06:39:53+08:00",
                    },
                ))
                decorated = db.get_account(account_id)

            stored = json.loads(paths["_ACCOUNTS_JSON"].read_text(encoding="utf-8"))[0]
            remail = json.loads(stored["extra_json"])["remail"]

        self.assertEqual(decorated["remail_receive_until"], "2026-08-10T07:39:53+08:00")
        self.assertEqual(decorated["remail_activated_at"], "2026-08-10T06:40:24+08:00")
        self.assertEqual(decorated["remail_after_sale_until"], "2026-08-11T06:39:53+08:00")
        self.assertEqual(remail["service_token"], "st-db-long")

    def test_static_viewer_never_embeds_remail_service_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._db_paths(root)
            self._initialize(paths)
            with patch.multiple(db, **paths):
                db.insert_account(
                    email="db-long@outlook.test",
                    access_token="session-at",
                    extra=self._long_lived_extra(),
                )

            viewer = paths["_VIEWER_HTML"].read_text(encoding="utf-8")
            self.assertNotIn("st-db-long", viewer)
            self.assertNotIn("old-platform-rt", viewer)
            self.assertNotIn('"extra_json"', viewer)


class RemailReloginServiceTests(unittest.TestCase):
    def test_log_path_replaces_path_separators(self):
        with tempfile.TemporaryDirectory() as td, patch.object(
            remail_relogin_service, "_LOG_DIR", Path(td)
        ):
            path = remail_relogin_service.log_path("folder/name\\part:mail@test")

        self.assertEqual(path.parent, Path(td))
        self.assertEqual(path.name, "remail-relogin-folder_name_part_mail@test.log")

    def test_worker_reuses_registration_driver_refreshes_tokens_and_auto_uploads_full_oauth(self):
        email = "worker-long@outlook.test"
        oauth = {
            "access_token": "new-platform-at",
            "refresh_token": "new-platform-rt",
            "id_token": "new-platform-id",
            "has_refresh_token": True,
            "file_path": "codex_accounts/worker-long.json",
        }
        account = {
            "id": 7,
            "email": email,
            "registration_password": "Saved-Pass-123!",
            "proxy_used": "http://127.0.0.1:7890",
            "remail_long_lived": True,
            "extra_json": json.dumps({
                "registration_driver": "browser_use",
                "remail": {
                    "service_token": "st-worker-long",
                    "service_mode": "purchase",
                    "receive_until": "2026-01-01T00:00:00Z",
                },
            }),
        }
        context = remail_client.RemailAccount(
            email=email,
            service_token="st-worker-long",
            order_no="R-WORKER-1",
            project_id=1001,
            product_id=2001,
            service_mode="purchase",
            receive_until="2026-01-01T00:00:00Z",
        )
        relogin_result = {
            "status": "success",
            "message": "补登成功，已获取 ChatGPT AT/Platform OAuth RT",
            "access_token": "new-session-at",
            "session_info": {"expires": "2026-08-20T12:00:00Z"},
            "platform_oauth": oauth,
            "driver": "browser_use",
        }
        def fake_run_relogin(*_args, **_kwargs):
            logging.getLogger("tests.remail-relogin").warning(
                "password=%s serviceToken=%s access_token=%s refresh_token=%s OTP: %s",
                "Saved-Pass-123!",
                "st-worker-long",
                "new-session-at",
                "new-platform-rt",
                "654321",
            )
            return relogin_result

        with tempfile.TemporaryDirectory() as td, patch.object(
            remail_relogin_service, "_LOG_DIR", Path(td)
        ), patch.object(db, "mark_account_remail_relogin_running", return_value=True), patch.object(
            db, "get_account", return_value=account
        ), patch.object(
            remail_client, "get_account_context", return_value=context
        ), patch.object(
            remail_relogin_service, "run_relogin", side_effect=fake_run_relogin
        ) as run_relogin, patch.object(
            remail_relogin_service.chatgpt2api_client,
            "auto_upload_registered_account",
            return_value={"ok": True, "status": "success", "mode": "rt"},
        ) as upload, patch.object(
            db, "complete_account_remail_relogin", return_value=True
        ) as complete:
            result = remail_relogin_service._run_one(
                {"id": 7, "email": email}, "batch-test", 1, 1
            )
            log_text = remail_relogin_service.log_path(email).read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        run_relogin.assert_called_once_with(
            email,
            driver="browser_use",
            password="Saved-Pass-123!",
            proxy="http://127.0.0.1:7890",
        )
        upload.assert_called_once_with(
            "new-session-at",
            platform_oauth=oauth,
            email=email,
            password="Saved-Pass-123!",
        )
        completed = complete.call_args.args[1]
        self.assertEqual(completed["platform_oauth"]["refresh_token"], "new-platform-rt")
        self.assertEqual(completed["upload"]["mode"], "rt")
        self.assertNotIn("new-session-at", repr(result))
        self.assertNotIn("new-platform-rt", repr(result))
        self.assertIn("[Remail补登] 完成", log_text)
        self.assertIn("[已隐藏]", log_text)
        for secret in (
            "Saved-Pass-123!",
            "st-worker-long",
            "new-session-at",
            "new-platform-at",
            "new-platform-rt",
            "new-platform-id",
            "654321",
        ):
            self.assertNotIn(secret, log_text)

    def test_worker_failure_is_written_to_log_without_credentials(self):
        email = "worker-failed@outlook.test"
        password = "Failure-Pass-123!"
        service_token = "st-failure-secret"
        access_token = "failure-session-at"
        account = {
            "id": 9,
            "email": email,
            "registration_password": password,
            "remail_long_lived": True,
            "extra_json": json.dumps({
                "registration_driver": "protocol",
                "remail": {"service_token": service_token, "service_mode": "purchase"},
            }),
        }
        context = remail_client.RemailAccount(
            email=email,
            service_token=service_token,
            order_no="R-FAILED-1",
            project_id=1001,
            product_id=2001,
            service_mode="purchase",
        )
        with tempfile.TemporaryDirectory() as td, patch.object(
            remail_relogin_service, "_LOG_DIR", Path(td)
        ), patch.object(
            db, "mark_account_remail_relogin_running", return_value=True
        ), patch.object(
            db, "get_account", return_value=account
        ), patch.object(
            remail_client, "get_account_context", return_value=context
        ), patch.object(
            remail_relogin_service,
            "run_relogin",
            side_effect=RuntimeError(
                f"password={password} serviceToken={service_token} access_token={access_token}"
            ),
        ), patch.object(
            db, "complete_account_remail_relogin", return_value=True
        ) as complete:
            result = remail_relogin_service._run_one(
                {"id": 9, "email": email}, "batch-failed", 1, 1
            )
            log_text = remail_relogin_service.log_path(email).read_text(encoding="utf-8")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("[Remail补登] 失败", log_text)
        self.assertNotIn(password, log_text)
        self.assertNotIn(service_token, log_text)
        self.assertNotIn(access_token, log_text)
        self.assertNotIn(password, complete.call_args.args[1]["error"])

    def test_concurrent_workers_keep_account_logs_separate(self):
        accounts = {
            21: {
                "id": 21,
                "email": "parallel-one@outlook.test",
                "registration_password": "pass-one",
                "remail_long_lived": True,
                "extra_json": json.dumps({"registration_driver": "protocol", "remail": {"service_token": "st-one", "service_mode": "purchase"}}),
            },
            22: {
                "id": 22,
                "email": "parallel-two@outlook.test",
                "registration_password": "pass-two",
                "remail_long_lived": True,
                "extra_json": json.dumps({"registration_driver": "protocol", "remail": {"service_token": "st-two", "service_mode": "purchase"}}),
            },
        }
        barrier = threading.Barrier(2)

        def context_for(email):
            suffix = "one" if "one" in email else "two"
            return remail_client.RemailAccount(
                email=email,
                service_token=f"st-{suffix}",
                order_no=f"R-{suffix.upper()}-1",
                project_id=1001,
                product_id=2001,
                service_mode="purchase",
            )

        def run_for(email, **_kwargs):
            barrier.wait(timeout=3)
            logging.getLogger("tests.remail-parallel").warning("parallel-marker=%s", email)
            return {
                "status": "success",
                "access_token": f"session-{email}",
                "platform_oauth": {"refresh_token": f"refresh-{email}", "has_refresh_token": True},
                "driver": "protocol",
            }

        with tempfile.TemporaryDirectory() as td, patch.object(
            remail_relogin_service, "_LOG_DIR", Path(td)
        ), patch.object(
            db, "mark_account_remail_relogin_running", return_value=True
        ), patch.object(
            db, "get_account", side_effect=lambda acc_id: accounts[acc_id]
        ), patch.object(
            remail_client, "get_account_context", side_effect=context_for
        ), patch.object(
            remail_relogin_service, "run_relogin", side_effect=run_for
        ), patch.object(
            remail_relogin_service.chatgpt2api_client,
            "auto_upload_registered_account",
            return_value={"status": "skipped"},
        ), patch.object(
            db, "complete_account_remail_relogin", return_value=True
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        remail_relogin_service._run_one,
                        {"id": acc_id, "email": account["email"]},
                        "batch-parallel",
                        index,
                        2,
                    )
                    for index, (acc_id, account) in enumerate(accounts.items(), 1)
                ]
                results = [future.result(timeout=5) for future in futures]
            logs = {
                account["email"]: remail_relogin_service.log_path(account["email"]).read_text(encoding="utf-8")
                for account in accounts.values()
            }

        self.assertTrue(all(result["ok"] for result in results))
        self.assertIn("parallel-marker=parallel-one@outlook.test", logs["parallel-one@outlook.test"])
        self.assertNotIn("parallel-marker=parallel-two@outlook.test", logs["parallel-one@outlook.test"])
        self.assertIn("parallel-marker=parallel-two@outlook.test", logs["parallel-two@outlook.test"])
        self.assertNotIn("parallel-marker=parallel-one@outlook.test", logs["parallel-two@outlook.test"])


class RemailReloginWebUiTests(unittest.TestCase):
    @staticmethod
    def _client():
        recovery_names = (
            "recover_interrupted_plan_checks",
            "recover_interrupted_extract_links",
            "recover_interrupted_token_refreshes",
            "recover_interrupted_platform_oauth_refreshes",
            "recover_interrupted_remail_relogins",
        )
        patches = [patch.object(db, name, return_value=0) for name in recovery_names]
        for one in patches:
            one.start()
        try:
            client = create_app(auth_code="test-auth").test_client()
        finally:
            for one in reversed(patches):
                one.stop()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        return client

    def test_single_relogin_api_starts_long_lived_account(self):
        client = self._client()
        account = {"id": 7, "email": "api-long@outlook.test", "remail_long_lived": True}
        with patch("webui.app.db.get_account", return_value=account), patch(
            "webui.app.db.claim_account_remail_relogin", return_value=True
        ) as claim, patch.object(
            remail_relogin_service,
            "start_batch",
            return_value={"batch_id": "batch-one", "workers": 1, "count": 1},
        ) as start:
            response = client.post("/api/accounts/7/remail-relogin", json={"workers": 9})

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        claim.assert_called_once_with(7, trigger="manual")
        start.assert_called_once_with([{"id": 7, "email": "api-long@outlook.test"}], 9)

    def test_bulk_relogin_api_skips_short_lived_and_caps_workers(self):
        client = self._client()
        accounts = {
            7: {"id": 7, "email": "api-long@outlook.test", "remail_long_lived": True},
            8: {"id": 8, "email": "api-short@outlook.test", "remail_long_lived": False},
        }
        with patch("webui.app.db.get_account", side_effect=lambda acc_id: accounts.get(acc_id)), patch(
            "webui.app.db.claim_account_remail_relogin", return_value=True
        ) as claim, patch.object(
            remail_relogin_service,
            "start_batch",
            return_value={"batch_id": "batch-bulk", "workers": 16, "count": 1},
        ) as start:
            response = client.post("/api/accounts/remail-relogin-bulk", json={
                "account_ids": [7, 8, 7],
                "workers": 99,
            })

        payload = response.get_json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(payload["skipped_count"], 1)
        self.assertIn("不是已持久化", payload["skipped"][0]["reason"])
        claim.assert_called_once_with(7, trigger="manual_bulk")
        start.assert_called_once_with([{"id": 7, "email": "api-long@outlook.test"}], 16)

    def test_accounts_api_does_not_return_remail_service_token(self):
        client = self._client()
        row = {
            "id": 7,
            "email": "api-long@outlook.test",
            "remail_long_lived": True,
            "remail_service_token_present": True,
            "extra_json": json.dumps({"remail": {"service_token": "st-api-secret"}}),
        }
        with patch("webui.app.db.list_accounts", return_value=[row]):
            response = client.get("/api/accounts")

        serialized = json.dumps(response.get_json())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("extra_json", response.get_json()[0])
        self.assertNotIn("st-api-secret", serialized)

    def test_relogin_log_api_reads_log_and_returns_running_status(self):
        client = self._client()
        email = "api-log@outlook.test"
        account = {
            "id": 17,
            "email": email,
            "remail_long_lived": True,
            "remail_relogin_status": "running",
        }
        with tempfile.TemporaryDirectory() as td, patch.object(
            remail_relogin_service, "_LOG_DIR", Path(td)
        ), patch("webui.app.db.get_account_by_email", return_value=account):
            remail_relogin_service.log_path(email).write_text(
                "12:00:00 [INFO] 补登进行中\n",
                encoding="utf-8",
            )
            response = client.get(
                "/api/accounts/remail-relogin-log",
                query_string={"email": email},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["running"])
        self.assertEqual(payload["status"], "running")
        self.assertIn("补登进行中", payload["log"])

    def test_relogin_log_api_rejects_non_long_lived_account(self):
        client = self._client()
        with patch("webui.app.db.get_account_by_email", return_value={
            "id": 18,
            "email": "api-short@outlook.test",
            "remail_long_lived": False,
        }):
            response = client.get(
                "/api/accounts/remail-relogin-log",
                query_string={"email": "api-short@outlook.test"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("不是 Remail 长效邮箱账号", response.get_json()["error"])

    def test_refresh_metadata_api_returns_activation_times_without_service_token(self):
        client = self._client()
        email = "api-refresh@outlook.test"
        account = {
            "id": 19,
            "email": email,
            "remail_long_lived": True,
        }
        context = remail_client.RemailAccount(
            email=email,
            service_token="st-api-refresh-secret",
            order_no="R-API-REFRESH",
            project_id=1001,
            product_id=2001,
            service_mode="purchase",
            status="active",
            receive_until="2026-08-10T07:39:53+08:00",
            activated_at="2026-08-10T06:40:24+08:00",
            after_sale_until="2026-08-11T06:39:53+08:00",
        )
        with patch("webui.app.db.get_account", return_value=account), patch.object(
            remail_client, "refresh_account_context", return_value=context
        ) as refresh:
            response = client.post(
                "/api/accounts/remail-refresh-metadata",
                json={"account_ids": [19]},
            )

        payload = response.get_json()
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["updated_count"], 1)
        self.assertEqual(payload["items"][0]["remail_status"], "active")
        self.assertEqual(payload["items"][0]["remail_activated_at"], "2026-08-10T06:40:24+08:00")
        self.assertEqual(payload["items"][0]["remail_after_sale_until"], "2026-08-11T06:39:53+08:00")
        self.assertNotIn("st-api-refresh-secret", serialized)
        refresh.assert_called_once_with(email)

    def test_pickup_status_api_returns_simplified_status_without_service_token(self):
        client = self._client()
        accounts = {
            21: {
                "id": 21,
                "email": "pickup-ok@outlook.test",
                "remail_long_lived": True,
            },
            22: {
                "id": 22,
                "email": "pickup-bad@outlook.test",
                "remail_long_lived": True,
            },
        }
        statuses = {
            "pickup-ok@outlook.test": {
                "status": "available",
                "message": "取件凭证有效",
                "checked_at": "2026-08-10T08:00:00+08:00",
            },
            "pickup-bad@outlook.test": {
                "status": "credential_invalid",
                "message": "Credential expired.",
                "checked_at": "2026-08-10T08:00:00+08:00",
            },
        }
        with patch("webui.app.db.get_account", side_effect=lambda acc_id: accounts.get(acc_id)), patch.object(
            remail_client, "check_pickup_statuses", return_value=statuses
        ) as check:
            response = client.post(
                "/api/accounts/remail-check-pickup",
                json={"account_ids": [21, 22]},
            )

        payload = response.get_json()
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["checked_count"], 2)
        self.assertEqual(payload["items"][0]["remail_pickup_status"], "available")
        self.assertEqual(payload["items"][1]["remail_pickup_status"], "credential_invalid")
        self.assertNotIn("service_token", serialized)
        check.assert_called_once_with([
            "pickup-ok@outlook.test",
            "pickup-bad@outlook.test",
        ])

    def test_page_contains_long_lived_selection_and_relogin_controls(self):
        client = self._client()
        page = client.get("/").get_data(as_text=True)
        self.assertIn("btnReloginSelectedRemail", page)
        self.assertIn("btnSelectAllLongLivedRemail", page)
        self.assertIn("data-remail-relogin", page)
        self.assertIn("data-remail-relogin-log", page)
        self.assertIn('class="actions remail-actions"', page)
        self.assertIn("remail-pickup-available", page)
        self.assertIn("可收件", page)
        self.assertIn("凭证失效", page)
        self.assertIn("不可取件", page)
        self.assertIn("服务异常", page)
        self.assertIn("请求频繁", page)
        self.assertIn("内部错误", page)
        self.assertIn('class="actions account-actions"', page)
        self.assertIn("remailReloginLogPanel", page)
        self.assertIn("btnCloseRemailReloginLog", page)
        self.assertIn("首次激活截止：", page)
        self.assertIn("已激活：", page)
        self.assertIn("质保截止：", page)
        self.assertNotIn("以 Remail 当前可取件状态为准", page)
        self.assertNotIn("平台时间", page)
        self.assertIn("不会按固定 24 小时限制", page)


if __name__ == "__main__":
    unittest.main()
