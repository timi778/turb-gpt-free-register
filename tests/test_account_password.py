# -*- coding: utf-8 -*-
import json
import string
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.env_loader import SECRET_ENV_KEYS
from core import db
from core.browser_use_registration import _generate_password, _has_new_password_form as browser_has_password_form
from core.roxy_registration import (
    _generate_roxy_password,
    _has_new_password_form as roxy_has_password_form,
    _setup_account_password,
)
from webui.config_editor import EDITABLE_FIELDS


PASSWORD_FORM_STATE = {
    "url": "https://auth.openai.com/reset-password/new-password",
    "inputs": [
        {"type": "hidden", "name": "username", "autocomplete": "username"},
        {"type": "password", "name": "new-password", "autocomplete": "new-password"},
        {"type": "password", "name": "confirm-password", "autocomplete": "new-password"},
    ],
    "errors": [],
}


class _FakeDriver:
    current_url = PASSWORD_FORM_STATE["url"]

    def execute_script(self, _script, *_args):
        return PASSWORD_FORM_STATE


class _FakePage:
    url = PASSWORD_FORM_STATE["url"]

    def evaluate(self, _script):
        return PASSWORD_FORM_STATE


class _FakeOtpInput:
    def __init__(self):
        self.value = ""

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def clear(self):
        self.value = ""

    def send_keys(self, value):
        self.value += str(value)


class _PasswordFlowDriver:
    def __init__(self):
        self.state = "chatgpt"
        self.current_url = "https://chatgpt.com/"
        self.otp_input = _FakeOtpInput()

    def get(self, url):
        self.current_url = url
        self.state = "settings" if "#settings/Security" in url else "new_password"

    def find_elements(self, _by, selector):
        if self.state == "otp" and selector == "input[autocomplete='one-time-code']":
            return [self.otp_input]
        return []

    def execute_script(self, script, *_args):
        if "otp_page_already_left" in script:
            self.state = "new_password"
            self.current_url = "https://auth.openai.com/reset-password/new-password"
            return {"ok": True, "reason": "otp_form_submitted"}
        if "missing_password_inputs" in script:
            self.state = "success"
            self.current_url = "https://chatgpt.com/#settings/Security"
            return {"ok": True}
        if "hasPasswordSetting" in script:
            inputs = PASSWORD_FORM_STATE["inputs"] if self.state == "new_password" else []
            return {
                "url": self.current_url,
                "inputs": inputs,
                "errors": [],
                "hasPasswordSetting": self.state == "settings",
            }
        if "password-setting" in script and "button.click()" in script:
            self.state = "otp"
            self.current_url = "https://auth.openai.com/email-verification"
            return True
        if "title: document.title" in script:
            inputs = [{
                "type": "text",
                "name": "code",
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "ariaInvalid": "",
            }] if self.state == "otp" else []
            return {"url": self.current_url, "inputs": inputs, "buttons": [], "errors": []}
        return None


class AccountPasswordTests(unittest.TestCase):
    def test_reset_password_form_is_detected_by_both_browser_drivers(self):
        self.assertTrue(roxy_has_password_form(_FakeDriver()))
        self.assertTrue(browser_has_password_form(_FakePage()))

    def test_roxy_password_flow_handles_secondary_otp_and_success(self):
        driver = _PasswordFlowDriver()
        with patch("core.roxy_registration.wait_for_otp", return_value="654321") as wait_otp, \
             patch("core.roxy_registration._registration_password", return_value="Flow-Pass-123!"), \
             patch("core.roxy_registration.human_delay"), \
             patch("core.roxy_registration.time.sleep"):
            password = _setup_account_password(driver, "flow@example.test")

        self.assertEqual(password, "Flow-Pass-123!")
        self.assertEqual(driver.otp_input.value, "654321")
        self.assertEqual(driver.state, "success")
        wait_otp.assert_called_once()

    def test_generated_passwords_meet_complexity_requirements(self):
        for password in (_generate_roxy_password(), _generate_password()):
            self.assertGreaterEqual(len(password), 14)
            self.assertTrue(any(ch in string.ascii_uppercase for ch in password))
            self.assertTrue(any(ch in string.ascii_lowercase for ch in password))
            self.assertTrue(any(ch in string.digits for ch in password))
            self.assertTrue(any(not ch.isalnum() for ch in password))

    def test_legacy_extra_json_password_is_exposed_to_console(self):
        row = {
            "id": 1,
            "email": "legacy@example.test",
            "extra_json": json.dumps({"registration_password": "Legacy-Pass-123!"}),
        }
        self.assertEqual(db._decorate_account(row)["registration_password"], "Legacy-Pass-123!")

    def test_password_is_saved_as_top_level_account_field(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts = root / "accounts.json"
            outlook = root / "outlook.json"
            accounts.write_text("[]", encoding="utf-8")
            outlook.write_text("[]", encoding="utf-8")
            patches = (
                patch.object(db, "_ACCOUNTS_JSON", accounts),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"),
                patch.object(db, "_OUTLOOK_JSON", outlook),
                patch.object(db, "_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
                patch.object(db, "_ACCOUNT_GROUPS_JSON", root / "groups.json"),
            )
            for current in patches:
                current.start()
            try:
                account_id = db.insert_account(
                    email="saved@example.test",
                    access_token="token",
                    registration_password="Saved-Pass-123!",
                    extra={"registration_password": "Saved-Pass-123!"},
                )
                self.assertEqual(db.get_account(account_id)["registration_password"], "Saved-Pass-123!")
                stored = json.loads(accounts.read_text(encoding="utf-8"))
                self.assertEqual(stored[0]["registration_password"], "Saved-Pass-123!")
            finally:
                for current in reversed(patches):
                    current.stop()

    def test_password_config_is_exposed_as_secret(self):
        field = next(item for item in EDITABLE_FIELDS if item["key"] == "REGISTER_PASSWORD")
        self.assertTrue(field["secret"])
        self.assertEqual(SECRET_ENV_KEYS["REGISTER_PASSWORD"], "ChatGPT 注册固定密码")
        switch = next(item for item in EDITABLE_FIELDS if item["key"] == "SET_PASSWORD_AFTER_REGISTRATION")
        self.assertEqual(switch["type"], "bool")


if __name__ == "__main__":
    unittest.main()
