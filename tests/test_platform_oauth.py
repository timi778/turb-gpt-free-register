import base64
import hashlib
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


class _PlaywrightRequest:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        state = parse_qs(urlparse(url).query)["state"][0]
        return _Response(
            url=f"https://platform.openai.com/auth/callback?code=pw-http-code&state={state}"
        )

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _Response(payload={
            "access_token": "playwright-http-at",
            "refresh_token": "playwright-http-rt",
        })


class _PlaywrightContext:
    def __init__(self):
        self.added_cookies = []
        self.request = _PlaywrightRequest()

    def cookies(self):
        return [{"name": "oai-did", "value": "playwright-device"}]

    def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    def new_page(self):
        raise AssertionError("OAuth must not load the callback page")


class _CookieJar:
    def __init__(self):
        self.values = []

    def set(self, name, value, **kwargs):
        self.values.append((name, value, kwargs))


class _SeleniumHttpSession(_Session):
    instances = []

    def __init__(self, proxy=None):
        super().__init__()
        self.proxy = proxy
        self.session = type("CookieSession", (), {"cookies": _CookieJar()})()
        self.device_id = "http-device"
        self.__class__.instances.append(self)


class _SeleniumDriver:
    def __init__(self):
        self.cdp_calls = []

    def get_cookies(self):
        return [{"name": "oai-did", "value": "selenium-device"}]

    def execute_cdp_cmd(self, method, params):
        self.cdp_calls.append((method, params))
        if method == "Storage.getCookies":
            return {"cookies": [
                {
                    "name": "oai-did",
                    "value": "selenium-device",
                    "domain": ".auth.openai.com",
                    "path": "/",
                },
                {
                    "name": "session-token",
                    "value": "session-value",
                    "domain": ".auth.openai.com",
                    "path": "/",
                },
            ]}
        raise RuntimeError(f"unexpected CDP command: {method}")


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

    @patch("core.platform_oauth.time.sleep")
    def test_network_failure_retries_three_rounds_then_succeeds(self, sleep):
        from core.platform_oauth import _run

        attempts = []

        def fetcher():
            attempts.append(len(attempts) + 1)
            if len(attempts) <= 3:
                raise RuntimeError(
                    "Failed to perform, curl: (35) TLS connect error: invalid library"
                )
            return {"access_token": "platform-at"}

        result = _run(fetcher, "user@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(attempts, [1, 2, 3, 4])
        self.assertEqual(
            [item.args[0] for item in sleep.call_args_list],
            [3.0, 3.0, 3.0],
        )

    @patch("core.platform_oauth.time.sleep")
    def test_network_failure_stops_after_three_retry_rounds(self, sleep):
        from core.platform_oauth import _run

        attempts = []

        def fetcher():
            attempts.append(len(attempts) + 1)
            raise RuntimeError("curl: (35) TLS connect error")

        result = _run(fetcher, "user@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(attempts, [1, 2, 3, 4])
        self.assertEqual(sleep.call_count, 3)

    @patch("core.platform_oauth.time.sleep")
    def test_non_network_failure_is_not_retried(self, sleep):
        from core.platform_oauth import _run

        attempts = []

        def fetcher():
            attempts.append(len(attempts) + 1)
            raise RuntimeError("Platform OAuth state 校验失败")

        result = _run(fetcher, "user@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(attempts, [1])
        sleep.assert_not_called()

    def test_playwright_path_uses_context_request_without_loading_callback_page(self):
        from core.platform_oauth import get_platform_oauth_tokens_playwright

        context = _PlaywrightContext()
        tokens = get_platform_oauth_tokens_playwright(context, "user@example.com")

        self.assertEqual(tokens["refresh_token"], "playwright-http-rt")
        self.assertEqual(len(context.request.get_calls), 1)
        self.assertEqual(len(context.request.post_calls), 1)
        self.assertTrue(any(cookie["name"] == "oai-did" for cookie in context.added_cookies))

    @patch("core.platform_oauth._complete_platform_authorization_in_selenium", return_value="browser-code")
    @patch("core.session.BrowserSession", _SeleniumHttpSession)
    def test_selenium_path_authorizes_in_browser_once_then_uses_http_token_exchange(self, complete):
        from core.platform_oauth import get_platform_oauth_tokens_selenium

        _SeleniumHttpSession.instances.clear()
        driver = _SeleniumDriver()
        tokens = get_platform_oauth_tokens_selenium(
            driver, "user@example.com", proxy="http://proxy.example:8080"
        )

        self.assertEqual(tokens["refresh_token"], "platform-rt")
        complete.assert_called_once()
        self.assertEqual(len(_SeleniumHttpSession.instances), 1)
        http = _SeleniumHttpSession.instances[0]
        self.assertEqual(http.proxy, "http://proxy.example:8080")
        # 授权 GET 只能由 Selenium 浏览器执行一次，HTTP 会话只负责 token 兑换。
        self.assertEqual(len(http.get_calls), 0)
        self.assertEqual(len(http.post_calls), 1)
        cookie_names = [item[0] for item in http.session.cookies.values]
        self.assertIn("session-token", cookie_names)
        self.assertIn("oai-did", cookie_names)
        self.assertEqual(driver.cdp_calls[0][0], "Storage.getCookies")
        body = parse_qs(http.post_calls[0][1]["data"])
        self.assertEqual(body["code"], ["browser-code"])

    def test_browser_otp_fallback_uses_shared_email_provider(self):
        from core.platform_oauth import (
            _complete_platform_authorization_in_selenium,
            build_platform_authorization,
        )

        authorization = build_platform_authorization("user@example.com", "device-123")

        class Driver:
            current_url = ""

            def __init__(self):
                self.get_calls = 0

            def get(self, _url):
                self.get_calls += 1
                self.current_url = "https://auth.openai.com/email-verification"

        driver = Driver()

        class BrowserHelpers:
            @staticmethod
            def _safe_get(driver, url, **kwargs):
                self.assertEqual(kwargs["attempts"], 1)
                driver.get(url)

            @staticmethod
            def _is_email_verification_page(_driver):
                return True

            @staticmethod
            def _clear_otp_inputs(_driver):
                return None

            @staticmethod
            def _type_otp(_driver, _code):
                return None

            @staticmethod
            def _click_continue(_driver):
                return None

            @staticmethod
            def _wait_after_email_otp_submit(_driver, timeout):
                assert timeout == 12
                return accepted()

        def accepted(*_args, **_kwargs):
            driver.current_url = (
                "https://platform.openai.com/auth/callback"
                f"?code=otp-code&state={authorization.state}"
            )
            return "accepted"

        with patch("core.email_provider.wait_for_otp", return_value="123456") as wait_otp:
            code = _complete_platform_authorization_in_selenium(
                driver,
                authorization,
                "user@example.com",
                browser_helpers=BrowserHelpers,
            )

        self.assertEqual(code, "otp-code")
        self.assertEqual(driver.get_calls, 1)
        self.assertEqual(wait_otp.call_args.args[0], "user@example.com")
        self.assertIn("after_ts", wait_otp.call_args.kwargs)

    def test_browser_otp_fallback_switches_login_password_page_to_passwordless(self):
        from core.platform_oauth import (
            _complete_platform_authorization_in_selenium,
            build_platform_authorization,
        )

        authorization = build_platform_authorization("user@example.com", "device-123")

        class Driver:
            current_url = ""

            def get(self, _url):
                self.current_url = "https://auth.openai.com/log-in/password"

        driver = Driver()
        state = {"otp": False, "passwordless_clicks": 0}

        def accepted(*_args, **_kwargs):
            driver.current_url = (
                "https://platform.openai.com/auth/callback"
                f"?code=passwordless-code&state={authorization.state}"
            )
            return "accepted"

        class BrowserHelpers:
            @staticmethod
            def _is_email_verification_page(_driver):
                return state["otp"]

            @staticmethod
            def _is_email_login_page_still_present(_driver):
                return False

            @staticmethod
            def _is_login_password_page(_driver):
                return not state["otp"]

            @staticmethod
            def _click_passwordless_signup_if_present(_driver):
                state["otp"] = True
                state["passwordless_clicks"] += 1
                return {"ok": True}

            @staticmethod
            def _clear_otp_inputs(_driver):
                return None

            @staticmethod
            def _type_otp(_driver, _code):
                return None

            @staticmethod
            def _click_continue(_driver):
                return None

            @staticmethod
            def _wait_after_email_otp_submit(_driver, timeout):
                assert timeout == 12
                return accepted()

        code = _complete_platform_authorization_in_selenium(
            driver,
            authorization,
            "user@example.com",
            otp_provider=lambda _email, **_kwargs: "123456",
            browser_helpers=BrowserHelpers,
        )

        self.assertEqual(code, "passwordless-code")
        self.assertEqual(state["passwordless_clicks"], 1)

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

    def test_roxy_waits_before_platform_oauth(self):
        source = Path("core/roxy_registration.py").read_text(encoding="utf-8")
        delay = "random.uniform(5.0, 10.0)"
        oauth = "run_platform_oauth_selenium"
        self.assertIn(delay, source)
        self.assertLess(source.index(delay), source.index(oauth))

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
