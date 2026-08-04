# -*- coding: utf-8 -*-
import json
import string
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import register as register_config
from config.env_loader import SECRET_ENV_KEYS
from core import db
from core import roxy_registration
from core.browser_use_registration import (
    _generate_password,
    _has_new_password_form as browser_has_password_form,
    _registration_password as browser_registration_password,
)
from core.roxy_registration import (
    _generate_roxy_password,
    _has_new_password_form as roxy_has_password_form,
    _is_login_password_url,
    _registration_password as roxy_registration_password,
    _setup_account_password,
)
from webui import config_editor
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
    def test_login_password_url_variants_are_recognized(self):
        self.assertTrue(_is_login_password_url("https://auth.openai.com/log-in/password"))
        self.assertTrue(_is_login_password_url("https://auth.openai.com/login/password"))
        self.assertTrue(_is_login_password_url("https://auth.openai.com/u/login/password"))
        self.assertFalse(_is_login_password_url("https://auth.openai.com/reset-password/new-password"))

    def test_login_password_dom_is_recognized_when_spa_url_does_not_change(self):
        driver = Mock(current_url="https://auth.openai.com/email-verification")
        with patch("core.roxy_registration._password_page_state", return_value={
            "url": "https://auth.openai.com/email-verification",
            "inputs": [{
                "type": "password",
                "name": "password",
                "id": "password",
                "autocomplete": "current-password",
                "visible": True,
            }],
        }):
            self.assertTrue(roxy_registration._is_login_password_page(driver))

    def test_reset_password_dom_is_not_misclassified_as_login(self):
        driver = Mock(current_url="https://auth.openai.com/reset-password/new-password")
        with patch("core.roxy_registration._password_page_state", return_value={
            "url": "https://auth.openai.com/reset-password/new-password",
            "inputs": [{
                "type": "password",
                "name": "new-password",
                "id": "new-password",
                "autocomplete": "new-password",
                "visible": True,
            }],
        }):
            self.assertFalse(roxy_registration._is_login_password_page(driver))

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

    def test_password_setup_preserves_and_returns_to_chatgpt_tab(self):
        driver = Mock()
        driver.current_window_handle = "chatgpt"
        driver.window_handles = ["chatgpt", "password"]

        with patch("core.roxy_registration._registration_password", return_value="Flow-Pass-123!"), \
             patch("core.roxy_registration._open_password_reset_flow", return_value=123.0), \
             patch("core.roxy_registration._complete_password_reauth_if_needed"), \
             patch("core.roxy_registration._wait_for_new_password_form"), \
             patch("core.roxy_registration._submit_new_password") as submit_password, \
             patch("core.roxy_registration.human_delay"):
            password = _setup_account_password(driver, "flow@example.test")

        self.assertEqual(password, "Flow-Pass-123!")
        driver.switch_to.new_window.assert_called_once_with("tab")
        submit_password.assert_called_once_with(
            driver,
            "Flow-Pass-123!",
            return_window_handle="chatgpt",
        )
        driver.switch_to.window.assert_called_once_with("chatgpt")

    def test_post_password_session_falls_back_to_password_login(self):
        driver = Mock()
        expected = {"accessToken": "fresh-token"}
        with patch("core.roxy_registration._fetch_chatgpt_session", side_effect=RuntimeError("session 暂无 accessToken")), \
             patch("core.roxy_token_refresh.login_roxy_driver_with_password", return_value=expected) as login:
            session = roxy_registration._fetch_session_after_password_flow(
                driver,
                "flow@example.test",
                "Flow-Pass-123!",
                timeout=120,
            )

        self.assertEqual(session, expected)
        login.assert_called_once_with(
            driver,
            "flow@example.test",
            "Flow-Pass-123!",
            session_timeout=120,
        )

    def test_direct_otp_registration_reauthenticates_before_password_setup(self):
        driver = Mock()
        expected = {"accessToken": "otp-login-token"}
        with patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=RuntimeError("session 暂无 accessToken")), \
             patch.object(roxy_registration, "_login_roxy_driver_with_email_otp", return_value=expected) as login:
            session = roxy_registration._ensure_registration_session(driver, "otp@example.test")

        self.assertEqual(session, expected)
        login.assert_called_once_with(driver, "otp@example.test")

    def test_roxy_registration_reads_session_after_password_setup(self):
        driver = Mock()
        client = Mock()
        opened = SimpleNamespace(profile_id="profile-test", raw={})
        client.open_profile.return_value = opened
        events = []

        with patch.object(roxy_registration, "RoxyBrowserClient", return_value=client), \
             patch.object(roxy_registration, "_build_driver", return_value=driver), \
             patch.object(roxy_registration, "_center_browser_window"), \
             patch.object(roxy_registration, "_maybe_accept"), \
             patch.object(roxy_registration, "_check_manual_stop"), \
             patch.object(roxy_registration, "human_delay"), \
             patch.object(roxy_registration, "_submit_email_and_wait_next", return_value="otp"), \
             patch.object(roxy_registration, "wait_for_otp", return_value="123456"), \
             patch.object(roxy_registration, "_clear_otp_inputs"), \
             patch.object(roxy_registration, "_type_otp"), \
             patch.object(roxy_registration, "_click_continue"), \
             patch.object(roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"), \
             patch.object(roxy_registration, "_complete_profile_page", return_value=True), \
             patch.object(roxy_registration, "_password_setup_enabled", return_value=True), \
             patch.object(roxy_registration, "_ensure_registration_session"), \
             patch.object(roxy_registration, "_setup_account_password", side_effect=lambda *_args: events.append("password") or "Saved-Pass-123!"), \
             patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=lambda *_args, **_kwargs: events.append("session") or {"accessToken": "post-password-token"}), \
             patch.object(roxy_registration, "save_account_data", return_value=1) as save_account, \
             patch.object(roxy_registration, "resolve_email_source", return_value="mailnest"), \
             patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", False), \
             patch.object(roxy_registration._cfg, "ROXY_KEEP_BROWSER_OPEN", False):
            result = roxy_registration.run_roxy_registration(
                "new@example.test", "Test User", "1990-01-01",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["access_token"], "post-password-token")
        self.assertEqual(events, ["password", "session"])
        self.assertEqual(save_account.call_args.kwargs["access_token"], "post-password-token")

    def test_generated_passwords_meet_complexity_requirements(self):
        for password in (_generate_roxy_password(), _generate_password()):
            self.assertGreaterEqual(len(password), 14)
            self.assertTrue(any(ch in string.ascii_uppercase for ch in password))
            self.assertTrue(any(ch in string.ascii_lowercase for ch in password))
            self.assertTrue(any(ch in string.digits for ch in password))
            self.assertTrue(any(not ch.isalnum() for ch in password))

    def test_random_password_mode_ignores_saved_fixed_password(self):
        with patch.object(register_config, "REGISTER_PASSWORD_MODE", "random"), \
             patch.object(register_config, "REGISTER_PASSWORD", "Saved-Fixed-123!"):
            generated = (roxy_registration_password(), browser_registration_password())

        self.assertTrue(all(password != "Saved-Fixed-123!" for password in generated))
        self.assertNotEqual(generated[0], generated[1])

    def test_fixed_password_mode_uses_custom_password(self):
        with patch.object(register_config, "REGISTER_PASSWORD_MODE", "fixed"), \
             patch.object(register_config, "REGISTER_PASSWORD", "Saved-Fixed-123!"):
            self.assertEqual(roxy_registration_password(), "Saved-Fixed-123!")
            self.assertEqual(browser_registration_password(), "Saved-Fixed-123!")

    def test_fixed_password_mode_requires_custom_password(self):
        with patch.object(register_config, "REGISTER_PASSWORD_MODE", "fixed"), \
             patch.object(register_config, "REGISTER_PASSWORD", ""):
            with self.assertRaisesRegex(RuntimeError, "固定密码模式"):
                roxy_registration_password()
            with self.assertRaisesRegex(RuntimeError, "固定密码模式"):
                browser_registration_password()

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
        self.assertEqual(field["group"], "账号密码")
        self.assertEqual(SECRET_ENV_KEYS["REGISTER_PASSWORD"], "ChatGPT 注册固定密码")
        switch = next(item for item in EDITABLE_FIELDS if item["key"] == "SET_PASSWORD_AFTER_REGISTRATION")
        self.assertEqual(switch["type"], "bool")
        self.assertEqual(switch["group"], "账号密码")
        self.assertEqual(switch["control"], "checkbox")
        mode = next(item for item in EDITABLE_FIELDS if item["key"] == "REGISTER_PASSWORD_MODE")
        self.assertEqual(mode["group"], "账号密码")
        self.assertEqual([option["value"] for option in mode["options"]], ["random", "fixed"])

    def test_webui_rejects_empty_fixed_password(self):
        with self.assertRaisesRegex(ValueError, "请填写自定义固定密码"):
            config_editor.update_config({
                "SET_PASSWORD_AFTER_REGISTRATION": True,
                "REGISTER_PASSWORD_MODE": "fixed",
                "REGISTER_PASSWORD": "",
            })


if __name__ == "__main__":
    unittest.main()
