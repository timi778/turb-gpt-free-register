import base64
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


class _Response:
    def __init__(self, *, status_code=200, url="", payload=None, text=""):
        self.status_code = status_code
        self.url = url
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Session:
    device_id = "device-123"

    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get_auth_navigate_headers(self, **_kwargs):
        return {"user-agent": "test-browser"}

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        state = parse_qs(urlparse(url).query)["state"][0]
        return _Response(
            url=f"https://platform.openai.com/auth/callback?code=code-123&state={state}"
        )

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _Response(payload={
            "access_token": "platform-at",
            "refresh_token": "platform-rt",
            "id_token": "platform-id",
            "expires_in": 3600,
        })


class _PlaywrightPage:
    def __init__(self):
        self.url = "about:blank"
        self.closed = False

    def goto(self, url, **_kwargs):
        state = parse_qs(urlparse(url).query)["state"][0]
        self.url = f"https://platform.openai.com/auth/callback?code=pw-code&state={state}"

    def evaluate(self, _script, _args):
        return {
            "status": 200,
            "text": json.dumps({
                "access_token": "playwright-at",
                "refresh_token": "playwright-rt",
            }),
        }

    def close(self):
        self.closed = True


class _PlaywrightContext:
    def __init__(self):
        self.page = _PlaywrightPage()
        self.added_cookies = []

    def cookies(self):
        return [{"name": "oai-did", "value": "playwright-device"}]

    def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    def new_page(self):
        return self.page


class _SwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def new_window(self, _kind):
        self.driver.window_handles.append("oauth")
        self.driver.current_window_handle = "oauth"

    def window(self, handle):
        self.driver.current_window_handle = handle


class _SeleniumDriver:
    def __init__(self):
        self.current_window_handle = "main"
        self.window_handles = ["main"]
        self.switch_to = _SwitchTo(self)
        self.current_url = "https://chatgpt.com/"
        self.cdp_calls = []

    def get_cookies(self):
        return [{"name": "oai-did", "value": "selenium-device"}]

    def execute_cdp_cmd(self, method, params):
        self.cdp_calls.append((method, params))

    def get(self, url):
        state = parse_qs(urlparse(url).query)["state"][0]
        self.current_url = f"https://platform.openai.com/auth/callback?code=se-code&state={state}"

    def execute_async_script(self, _script, *_args):
        return {
            "status": 200,
            "text": json.dumps({
                "access_token": "selenium-at",
                "refresh_token": "selenium-rt",
            }),
        }

    def close(self):
        if self.current_window_handle in self.window_handles:
            self.window_handles.remove(self.current_window_handle)


class PlatformOAuthTests(unittest.TestCase):
    def test_authorization_uses_platform_client_offline_scope_and_pkce_s256(self):
        from core.platform_oauth import build_platform_authorization

        request = build_platform_authorization("user@example.com", "device-123")
        query = parse_qs(urlparse(request.url).query)

        self.assertEqual(query["client_id"], ["app_2SKx67EdpoN0G6j64rFvigXD"])
        self.assertEqual(query["redirect_uri"], ["https://platform.openai.com/auth/callback"])
        self.assertEqual(query["audience"], ["https://api.openai.com/v1"])
        self.assertIn("offline_access", query["scope"][0].split())
        self.assertEqual(query["code_challenge_method"], ["S256"])
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(request.code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(query["code_challenge"], [expected])

    def test_http_session_reuses_login_state_and_exchanges_authorization_code(self):
        from core.platform_oauth import get_platform_oauth_tokens

        session = _Session()
        tokens = get_platform_oauth_tokens(session, "user@example.com")

        self.assertEqual(tokens["access_token"], "platform-at")
        self.assertEqual(tokens["refresh_token"], "platform-rt")
        self.assertEqual(len(session.get_calls), 1)
        self.assertTrue(session.get_calls[0][1]["allow_redirects"])
        token_url, token_kwargs = session.post_calls[0]
        self.assertEqual(token_url, "https://auth.openai.com/api/accounts/oauth/token")
        body = parse_qs(token_kwargs["data"])
        self.assertEqual(body["grant_type"], ["authorization_code"])
        self.assertEqual(body["code"], ["code-123"])
        self.assertIn("code_verifier", body)

    def test_state_mismatch_is_rejected(self):
        from core.platform_oauth import extract_authorization_code

        with self.assertRaisesRegex(RuntimeError, "state"):
            extract_authorization_code(
                "https://platform.openai.com/auth/callback?code=x&state=wrong",
                expected_state="expected",
            )

    def test_playwright_path_uses_browser_page_and_restores_context(self):
        from core.platform_oauth import get_platform_oauth_tokens_playwright

        context = _PlaywrightContext()
        tokens = get_platform_oauth_tokens_playwright(context, "user@example.com")

        self.assertEqual(tokens["refresh_token"], "playwright-rt")
        self.assertTrue(context.page.closed)
        self.assertTrue(any(cookie["name"] == "oai-did" for cookie in context.added_cookies))

    def test_selenium_path_uses_temporary_tab_and_restores_original(self):
        from core.platform_oauth import get_platform_oauth_tokens_selenium

        driver = _SeleniumDriver()
        tokens = get_platform_oauth_tokens_selenium(driver, "user@example.com")

        self.assertEqual(tokens["refresh_token"], "selenium-rt")
        self.assertEqual(driver.current_window_handle, "main")
        self.assertEqual(driver.window_handles, ["main"])
        self.assertEqual(len(driver.cdp_calls), 2)

    @patch("core.codex_oauth.save_codex_credential")
    @patch("core.codex_oauth.build_codex_storage")
    @patch("core.codex_oauth._parse_id_token")
    def test_refresh_token_is_saved_as_full_codex_credential(self, parse_id, build_storage, save):
        from core.platform_oauth import finalize_platform_oauth

        parse_id.return_value = {"email": "", "account_id": "acct", "plan_type": "free"}
        build_storage.return_value = {"type": "codex", "refresh_token": "platform-rt"}
        save.return_value = "codex_accounts/codex-user@example.com-free.json"

        result = finalize_platform_oauth({
            "access_token": "platform-at",
            "refresh_token": "platform-rt",
            "id_token": "platform-id",
        }, "user@example.com")

        self.assertTrue(result["ok"])
        self.assertTrue(result["has_refresh_token"])
        self.assertEqual(result["file_path"], str(save.return_value))
        build_storage.assert_called_once()
        self.assertEqual(build_storage.call_args.args[1]["email"], "user@example.com")

    @patch("core.codex_oauth.save_codex_credential", side_effect=OSError("disk full"))
    @patch("core.codex_oauth.build_codex_storage", return_value={"type": "codex"})
    @patch("core.codex_oauth._parse_id_token", return_value={})
    def test_codex_file_failure_keeps_refresh_token_for_upload(self, parse_id, build_storage, save):
        from core.platform_oauth import finalize_platform_oauth

        result = finalize_platform_oauth({
            "access_token": "platform-at",
            "refresh_token": "platform-rt",
            "id_token": "platform-id",
        }, "user@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["refresh_token"], "platform-rt")
        self.assertIn("credential_error", result)


class RegistrationDriverPlatformOAuthHookTests(unittest.TestCase):
    @staticmethod
    def _assert_order(path: str, first: str, second: str):
        source = Path(path).read_text(encoding="utf-8")
        first_pos = source.index(first)
        second_pos = source.index(second, first_pos)
        if first_pos >= second_pos:
            raise AssertionError(f"{first!r} must appear before {second!r} in {path}")

    def test_protocol_gets_platform_tokens_before_legacy_codex(self):
        self._assert_order("main.py", "run_platform_oauth_http", "run_codex_oauth")

    def test_roxy_gets_platform_tokens_before_browser_state_is_cleared(self):
        self._assert_order(
            "core/roxy_registration.py",
            "run_platform_oauth_selenium",
            "clear_existing_state=True",
        )

    def test_cloak_gets_platform_tokens_before_browser_state_is_cleared(self):
        self._assert_order(
            "core/cloakbrowser_registration.py",
            "run_platform_oauth_playwright",
            "clear_existing_state=True",
        )

    def test_browser_use_gets_platform_tokens_before_context_close(self):
        self._assert_order(
            "core/browser_use_registration.py",
            "run_platform_oauth_playwright",
            "_close_browser_use_session(browser",
        )

    def test_skyvern_reuses_browser_use_registration_hook(self):
        source = Path("core/skyvern_registration.py").read_text(encoding="utf-8")
        self.assertIn("run_browser_use_registration", source)
        self.assertIn('cloud_provider="skyvern"', source)


if __name__ == "__main__":
    unittest.main()
