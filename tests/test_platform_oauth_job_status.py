import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, registration_service
from webui.app import create_app


class PlatformOAuthJobStatusTests(unittest.TestCase):
    def test_snapshot_maps_result_without_copying_tokens(self):
        snapshot = registration_service.platform_oauth_job_snapshot({
            "status": "success",
            "ok": True,
            "has_refresh_token": True,
            "access_token": "secret-platform-at",
            "refresh_token": "secret-platform-rt",
            "id_token": "secret-platform-id",
            "message": "已获取 Platform AT/RT secret-platform-rt",
        }, completed_at="2026-08-05T12:00:00")

        self.assertEqual(snapshot, {
            "platform_oauth_status": "success",
            "platform_oauth_has_refresh_token": True,
            "platform_oauth_message": "已获取 Platform AT/RT [redacted]",
            "platform_oauth_completed_at": "2026-08-05T12:00:00",
        })
        self.assertNotIn("secret-platform", repr(snapshot))

    def test_snapshot_maps_missing_failed_skipped_and_not_reached(self):
        cases = [
            ({"status": "partial", "ok": True, "has_refresh_token": False}, "missing"),
            ({"status": "success", "ok": True, "has_refresh_token": False}, "missing"),
            ({"status": "failed", "ok": False}, "failed"),
            ({"status": "skipped", "ok": False}, "skipped"),
            (None, "not_reached"),
        ]
        for raw, expected in cases:
            with self.subTest(expected=expected):
                snapshot = registration_service.platform_oauth_job_snapshot(
                    raw, completed_at="2026-08-05T12:00:00"
                )
                self.assertEqual(snapshot["platform_oauth_status"], expected)
                self.assertFalse(snapshot["platform_oauth_has_refresh_token"])

    def test_db_job_snapshot_uses_an_explicit_allowlist(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = Path(td) / "jobs.json"
            jobs_path.write_text("[]", encoding="utf-8")
            with patch.object(db, "_JOBS_JSON", jobs_path), \
                 patch.object(db, "_LEGACY_JOBS_JSON", Path(td) / "legacy-jobs.json"):
                job = db.create_job("outlook")
                db.update_job(job["id"], platform_oauth_snapshot={
                    "platform_oauth_status": "success",
                    "platform_oauth_has_refresh_token": True,
                    "platform_oauth_message": "ok",
                    "platform_oauth_completed_at": "2026-08-05T12:00:00",
                    "refresh_token": "must-not-be-saved",
                })

            stored = json.loads(jobs_path.read_text(encoding="utf-8"))[0]
            self.assertEqual(stored["platform_oauth_status"], "success")
            self.assertNotIn("refresh_token", stored)

    def test_jobs_api_marks_legacy_terminal_unknown_and_active_waiting(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        rows = [
            {"id": 1, "status": "success", "email": "old@example.test"},
            {"id": 2, "status": "running", "email": "new@example.test"},
        ]
        with patch("webui.app.db.list_jobs", return_value=rows), \
             patch("webui.app.svc.get_retry_info", return_value={}):
            response = client.get("/api/jobs")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        by_id = {item["id"]: item for item in payload}
        self.assertEqual(by_id[1]["platform_oauth_status"], "unknown")
        self.assertEqual(by_id[2]["platform_oauth_status"], "waiting")


if __name__ == "__main__":
    unittest.main()
