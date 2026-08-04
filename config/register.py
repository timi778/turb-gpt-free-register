# -*- coding: utf-8 -*-
"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（仅 USE_EMAIL_SERVICE=True 时邮箱会从 Outlook 池领取）。
"""
from config.env_loader import apply_env_overrides

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取）
REGISTER_EMAIL = ""

# 固定注册密码，仅在 REGISTER_PASSWORD_MODE=fixed 时使用。
REGISTER_PASSWORD = ""

# 注册密码生成方式：random=每个账号随机生成；fixed=使用 REGISTER_PASSWORD。
# 空值仅用于兼容旧配置：已有 REGISTER_PASSWORD 时按 fixed，否则按 random。
REGISTER_PASSWORD_MODE = ""

# OTP-only 注册拿到登录态后，主动进入安全设置补设账号密码。
SET_PASSWORD_AFTER_REGISTRATION = True

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTER_EMAIL': 'str',
    'REGISTER_PASSWORD': 'str',
    'REGISTER_PASSWORD_MODE': 'str',
    'SET_PASSWORD_AFTER_REGISTRATION': 'bool',
    'REGISTER_NAME': 'str',
})

REGISTER_PASSWORD_MODE = str(REGISTER_PASSWORD_MODE or "").strip().lower()
if not REGISTER_PASSWORD_MODE:
    REGISTER_PASSWORD_MODE = "fixed" if str(REGISTER_PASSWORD or "").strip() else "random"
