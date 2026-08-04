"""chatgpt2api 管理接口客户端：连接检测与逐账号上传。"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests

from config import chatgpt2api as _cfg

logger = logging.getLogger(__name__)


def _result(*, status: str, ok: bool = False, mode: str | None = None, **extra) -> dict:
    return {"status": status, "ok": ok, "mode": mode, **extra}


def _normalize_base_url(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("chatgpt2api 服务地址必须是有效的 http(s) URL")
    return base


def is_configured(base_url: str | None = None, admin_key: str | None = None) -> bool:
    base = _cfg.CHATGPT2API_BASE_URL if base_url is None else base_url
    key = _cfg.CHATGPT2API_ADMIN_KEY if admin_key is None else admin_key
    try:
        return bool(_normalize_base_url(base) and str(key or "").strip())
    except ValueError:
        return False


def _response_json(response) -> dict | list | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, (dict, list)) else None


def _request(
    method: str,
    path: str,
    *,
    base_url: str | None = None,
    admin_key: str | None = None,
    json_body: dict | None = None,
):
    base = _normalize_base_url(
        _cfg.CHATGPT2API_BASE_URL if base_url is None else base_url
    )
    key = str(
        _cfg.CHATGPT2API_ADMIN_KEY if admin_key is None else admin_key
    ).strip()
    if not base or not key:
        raise ValueError("请填写 chatgpt2api 服务地址和管理鉴权 Key")
    timeout = max(1, int(getattr(_cfg, "CHATGPT2API_TIMEOUT", 30) or 30))
    return requests.request(
        method.upper(),
        f"{base}{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            **({"Content-Type": "application/json"} if json_body is not None else {}),
        },
        json=json_body,
        timeout=timeout,
    )


def _error_preview(response) -> str:
    text = str(getattr(response, "text", "") or "").strip()
    return text[:200] or "无响应体"


def test_connection(base_url: str | None = None, admin_key: str | None = None) -> dict:
    """GET /api/accounts，验证地址和 Bearer 管理鉴权。"""
    try:
        response = _request(
            "GET", "/api/accounts", base_url=base_url, admin_key=admin_key
        )
    except (requests.RequestException, ValueError) as exc:
        return _result(status="failed", error=f"连接失败: {type(exc).__name__}: {exc}")

    http_status = int(getattr(response, "status_code", 0) or 0)
    if http_status in (401, 403):
        return _result(
            status="failed", http_status=http_status, error=f"管理鉴权失败 HTTP {http_status}"
        )
    if not 200 <= http_status < 300:
        return _result(
            status="failed",
            http_status=http_status,
            error=f"HTTP {http_status}: {_error_preview(response)}",
        )

    payload = _response_json(response)
    items = payload if isinstance(payload, list) else None
    if isinstance(payload, dict):
        for key in ("items", "accounts", "data"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    return _result(
        status="success",
        ok=True,
        http_status=http_status,
        account_count=len(items) if isinstance(items, list) else None,
    )


def upload_account(
    session_access_token: str,
    *,
    platform_oauth: dict | None = None,
    email: str = "",
    password: str = "",
    base_url: str | None = None,
    admin_key: str | None = None,
) -> dict:
    """有 RT 上传完整 Codex 结构；确实没有 RT 时才回退为 AT。"""
    oauth = platform_oauth if isinstance(platform_oauth, dict) else {}
    session_at = str(session_access_token or "").strip()
    platform_at = str(oauth.get("access_token") or "").strip()
    refresh_token = str(oauth.get("refresh_token") or "").strip()
    id_token = str(oauth.get("id_token") or "").strip()

    if refresh_token:
        mode = "rt"
        body = {
            "accounts": [{
                "type": "codex",
                "email": str(email or "").strip(),
                "password": str(password or ""),
                "access_token": platform_at or session_at,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "source_type": "codex",
            }]
        }
    else:
        mode = "at"
        token = session_at or platform_at
        if not token:
            return _result(status="failed", mode=mode, error="缺少 access token")
        body = {"tokens": [token]}

    try:
        response = _request(
            "POST",
            "/api/accounts",
            base_url=base_url,
            admin_key=admin_key,
            json_body=body,
        )
    except (requests.RequestException, ValueError) as exc:
        return _result(
            status="failed", mode=mode, error=f"{type(exc).__name__}: {exc}"
        )

    http_status = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= http_status < 300:
        # RT 路径失败时不做 AT 二次上传，防止同一账号产生两种不一致记录。
        return _result(
            status="failed",
            mode=mode,
            http_status=http_status,
            error=f"HTTP {http_status}: {_error_preview(response)}",
        )

    payload = _response_json(response)
    payload = payload if isinstance(payload, dict) else {}
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors and not payload.get("added") and not payload.get("skipped"):
        return _result(
            status="failed",
            mode=mode,
            http_status=http_status,
            error=str(errors)[:200],
        )
    return _result(
        status="success",
        ok=True,
        mode=mode,
        http_status=http_status,
        added=int(payload.get("added") or 0),
        skipped=int(payload.get("skipped") or 0),
        refreshed=int(payload.get("refreshed") or 0),
    )


def auto_upload_registered_account(
    session_access_token: str,
    *,
    platform_oauth: dict | None = None,
    email: str = "",
    password: str = "",
) -> dict:
    """按热加载配置上传一个已本地保存的账号。"""
    if not bool(getattr(_cfg, "CHATGPT2API_AUTO_UPLOAD", True)):
        return _result(status="skipped", message="CHATGPT2API_AUTO_UPLOAD=False")
    if not is_configured():
        return _result(status="skipped", message="chatgpt2api 地址或管理 Key 未配置")

    result = upload_account(
        session_access_token,
        platform_oauth=platform_oauth,
        email=email,
        password=password,
    )
    if result.get("ok"):
        logger.info(
            "[chatgpt2api] 单账号上传成功: email=%s mode=%s added=%s skipped=%s",
            email,
            result.get("mode"),
            result.get("added", 0),
            result.get("skipped", 0),
        )
    else:
        logger.warning(
            "[chatgpt2api] 单账号上传失败（本地账号已保留）: email=%s mode=%s error=%s",
            email,
            result.get("mode") or "-",
            str(result.get("error") or result.get("message") or "未知错误")[:220],
        )
    return result
