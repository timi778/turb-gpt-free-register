# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

import config
from config import email
from config.env_loader import SECRET_ENV_KEYS
from webui.config_editor import EDITABLE_FIELDS


class RemailConfigTests(unittest.TestCase):
    def test_email_config_declares_remail_api_key_with_empty_default(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn('REMAIL_API_KEY = env_str("REMAIL_API_KEY", "")', source)

    def test_secret_registry_includes_remail_api_key(self):
        self.assertEqual(SECRET_ENV_KEYS["REMAIL_API_KEY"], "Remail API Key")

    def test_webui_exposes_remail_key_as_secret_env_field(self):
        field = next(item for item in EDITABLE_FIELDS if item["key"] == "REMAIL_API_KEY")
        self.assertEqual(field["group"], "邮箱 / OTP")
        self.assertTrue(field["secret"])
        self.assertEqual(field["storage"], "env")

    def test_optional_project_override_and_suffixes_are_declared(self):
        fields = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(fields["REMAIL_SERVICE_MODE"]["type"], "str")
        self.assertEqual(
            fields["REMAIL_SERVICE_MODE"]["options"],
            [
                {"value": "code", "label": "短效接码（一次邮件）"},
                {"value": "purchase", "label": "长效购买（可重复收件）"},
            ],
        )
        self.assertEqual(fields["REMAIL_PROJECT_ID"]["type"], "int")
        self.assertNotIn("REMAIL_PRODUCT_ID", fields)
        self.assertEqual(fields["REMAIL_EMAIL_SUFFIXES"]["type"], "list_str_multiline")

    def test_top_level_config_exports_remail_fields(self):
        self.assertEqual(config.REMAIL_API_KEY, email.REMAIL_API_KEY)
        self.assertEqual(config.REMAIL_SERVICE_MODE, email.REMAIL_SERVICE_MODE)
        self.assertEqual(config.REMAIL_PROJECT_ID, email.REMAIL_PROJECT_ID)
        self.assertEqual(config.REMAIL_EMAIL_SUFFIXES, email.REMAIL_EMAIL_SUFFIXES)

    def test_webui_places_remail_fields_in_their_own_section(self):
        template = (
            Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("key.startsWith('REMAIL_')", template)
        self.assertIn("'MailNest', 'Remail', 'CloudMail'", template)


if __name__ == "__main__":
    unittest.main()
