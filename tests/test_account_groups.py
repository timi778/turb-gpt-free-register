# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountGroupTests(unittest.TestCase):
    def test_group_lifecycle_and_account_assignment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = {
                "_ACCOUNTS_JSON": root / "accounts.json",
                "_LEGACY_ACCOUNTS_JSON": root / "legacy_accounts.json",
                "_ACCOUNTS_TXT": root / "accounts.txt",
                "_TOKENS_TXT": root / "tokens.txt",
                "_OUTLOOK_JSON": root / "outlook.json",
                "_OUTLOOK_TXT": root / "outlook.txt",
                "_JOBS_JSON": root / "jobs.json",
                "_LEGACY_JOBS_JSON": root / "legacy_jobs.json",
                "_ACCOUNT_GROUPS_JSON": root / "groups.json",
                "_VIEWER_HTML": root / "viewer.html",
                "_LOG_DIR": root / "logs",
            }
            with patch.multiple(db, **paths):
                first = db.create_account_group("同一分钟")
                duplicate = db.create_account_group("同一分钟")
                self.assertNotEqual(first["name"], duplicate["name"])

                account_id = db.insert_account(email="one@example.com", access_token="token")
                job = db.create_job("outlook", group_id=first["id"])
                self.assertTrue(db.assign_account_group(account_id, first["id"]))
                self.assertEqual(db.get_account(account_id)["group_name"], first["name"])
                self.assertEqual(
                    next(item for item in db.list_account_groups() if item["id"] == first["id"])["account_count"],
                    1,
                )

                renamed = db.rename_account_group(first["id"], "我的批次")
                self.assertEqual(renamed["name"], "我的批次")
                self.assertEqual(db.get_account(account_id)["group_name"], "我的批次")

                deleted = db.delete_account_group(first["id"])
                self.assertEqual(deleted["detached_accounts"], 1)
                self.assertEqual(deleted["detached_jobs"], 1)
                self.assertIsNone(db.get_account(account_id)["group_id"])
                self.assertIsNone(db.get_job(job["id"])["group_id"])


if __name__ == "__main__":
    unittest.main()
