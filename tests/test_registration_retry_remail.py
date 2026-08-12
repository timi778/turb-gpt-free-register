# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, registration_service, remail_client
from webui.app import create_app


class _RecordingExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))


class RemailRegistrationRetryTests(unittest.TestCase):
    def setUp(self):
        remail_client._CONTEXT_CACHE.clear()

    def tearDown(self):
        remail_client._CONTEXT_CACHE.clear()

    @staticmethod
    def _short_context(email="retry-short@outlook.test"):
        return {
            "source": "remail",
            "context": {
                "email": email,
                "service_token": "st-short-task-secret",
                "order_no": "R-SHORT-RETRY-1",
                "project_id": 1001,
                "product_id": 2001,
                "service_mode": "code",
            },
        }

    def test_retry_job_copies_same_email_and_order_context_without_exposing_secret(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_path = root / "jobs.json"
            jobs_path.write_text("[]", encoding="utf-8")
            executor = _RecordingExecutor()
            context = self._short_context()
            with patch.object(db, "_JOBS_JSON", jobs_path), \
                 patch.object(db, "_LEGACY_JOBS_JSON", root / "legacy-jobs.json"), \
                 patch.object(db, "_LOG_DIR", root / "logs"), \
                 patch.object(db, "get_account_by_email", return_value=None), \
                 patch.object(registration_service, "get_executor", return_value=executor):
                source = db.create_job("remail")
                db.update_job(
                    source["id"],
                    status="failed",
                    email=context["context"]["email"],
                    email_context=context,
                )
                result = registration_service.retry_job(source["id"], workers=1)

                child = db.get_job(result["job"]["id"])

            self.assertTrue(result["ok"])
            self.assertEqual(child["email"], context["context"]["email"])
            self.assertEqual(child["email_context"], context)
            self.assertNotIn("email_context", result["job"])
            self.assertNotIn("st-short-task-secret", json.dumps(result, ensure_ascii=False))
            self.assertEqual(len(executor.calls), 1)

    def test_short_lived_retry_restores_order_and_never_acquires_another_email(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_path = root / "jobs.json"
            jobs_path.write_text("[]", encoding="utf-8")
            context = self._short_context()
            with patch.object(db, "_JOBS_JSON", jobs_path), \
                 patch.object(db, "_LEGACY_JOBS_JSON", root / "legacy-jobs.json"), \
                 patch.object(db, "_LOG_DIR", root / "logs"), \
                 patch("core.email_provider.acquire_email", side_effect=AssertionError("不应重新获取邮箱")) as acquire, \
                 patch("main.run_registration", return_value={
                     "success": True,
                     "email": context["context"]["email"],
                     "account_id": 1,
                     "platform_oauth": {"status": "skipped"},
                 }) as run_registration:
                source = db.create_job("remail")
                db.update_job(
                    source["id"],
                    status="failed",
                    email=context["context"]["email"],
                    email_context=context,
                )
                child, _ = db.create_retry_job(
                    source["id"],
                    job_type="registration",
                    email_source="remail",
                    email=context["context"]["email"],
                    email_context=context,
                )

                registration_service._run_one_job(child["id"], child["log_file"])
                stored = db.get_job(child["id"])

            acquire.assert_not_called()
            run_registration.assert_called_once()
            self.assertEqual(run_registration.call_args.kwargs["email"], context["context"]["email"])
            self.assertEqual(stored["status"], "success")
            restored = remail_client.get_account_context(context["context"]["email"])
            self.assertEqual(restored.order_no, "R-SHORT-RETRY-1")
            self.assertEqual(restored.service_token, "st-short-task-secret")

    def test_jobs_and_job_log_apis_never_return_saved_order_context(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        job = {
            "id": 12,
            "status": "failed",
            "email": "hidden@outlook.test",
            "email_context": self._short_context("hidden@outlook.test"),
        }
        with patch("webui.app.db.list_jobs", return_value=[dict(job)]), \
             patch("webui.app.db.get_job", return_value=dict(job)), \
             patch("webui.app.svc.get_retry_info", return_value={}), \
             patch("webui.app.svc.read_job_log", return_value="log"):
            listing = client.get("/api/jobs")
            log = client.get("/api/jobs/12/log")

        self.assertNotIn("email_context", listing.get_json()[0])
        self.assertNotIn("email_context", log.get_json()["job"])
        self.assertNotIn("st-short-task-secret", listing.get_data(as_text=True))
        self.assertNotIn("st-short-task-secret", log.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
