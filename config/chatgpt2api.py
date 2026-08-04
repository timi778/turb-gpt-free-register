"""平台 OAuth 与 chatgpt2api 自动上传配置。"""
from config.env_loader import apply_env_overrides, env_str

# 注册登录态建立后，复用同一 Cookie 获取 Platform OAuth AT/RT。
ENABLE_PLATFORM_OAUTH: bool = True

# 配置完整时，每个账号本地保存后立即同步上传一次。
CHATGPT2API_AUTO_UPLOAD: bool = True
CHATGPT2API_BASE_URL: str = env_str("CHATGPT2API_BASE_URL", "")
CHATGPT2API_ADMIN_KEY: str = env_str("CHATGPT2API_ADMIN_KEY", "")
CHATGPT2API_TIMEOUT: int = 30

apply_env_overrides(globals(), {
    "ENABLE_PLATFORM_OAUTH": "bool",
    "CHATGPT2API_AUTO_UPLOAD": "bool",
    "CHATGPT2API_BASE_URL": "str",
    "CHATGPT2API_ADMIN_KEY": "str",
    "CHATGPT2API_TIMEOUT": "int",
})
