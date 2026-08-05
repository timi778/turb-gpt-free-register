import unittest
from pathlib import Path


class PlatformOAuthWebUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")

    def test_tables_show_historical_and_current_rt_columns(self):
        self.assertIn("<th>首次 RT</th>", self.template)
        self.assertIn("<th>当前 RT</th>", self.template)
        for label in ("等待中", "已获取", "未返回", "OAuth 失败", "已跳过", "未执行", "未知"):
            self.assertIn(label, self.template)
        for label in ("有 RT", "无 RT", "刷新中", "刷新失败"):
            self.assertIn(label, self.template)

    def test_bulk_refresh_control_uses_safe_oauth_endpoints(self):
        self.assertIn('id="btnRefreshSelectedOAuth"', self.template)
        self.assertIn("/api/accounts/oauth-refresh-bulk", self.template)
        self.assertIn("/api/accounts/oauth-refresh-status", self.template)
        self.assertIn("refreshSelectedOAuth", self.template)

    def test_bulk_button_participates_in_selection_state(self):
        self.assertIn("const refreshOAuthBtn = $('#btnRefreshSelectedOAuth');", self.template)
        self.assertIn("refreshOAuthBtn.disabled = ACCOUNT_SELECTED.size === 0", self.template)


if __name__ == "__main__":
    unittest.main()
