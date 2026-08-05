"""Platform OAuth refresh-token grant and account synchronization service."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from core import chatgpt2api_client, db
from core.platform_oauth import PLATFORM_CLIENT_ID, PLATFORM_TOKEN_URL

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 30
_MAX_WORKERS = 3


def _safe_error(response, refresh_token: str) -> str:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = {}
    if isinstance(payload, dict):
        code = str(payload.get("error") or "").strip()
        description = str(payload.get("error_description") or payload.get("message") or "").strip()
        message = ": ".join(part for part in (code, description) if part)
    else:
        message = ""
    if not message:
        message = f"HTTP {int(getattr(response, 'status_code', 0) or 0)}"
    if refresh_token:
        message = message.replace(refresh_token, "[redacted]")
    return message[:300]


def exchange_refresh_token(refresh_token: str, *, session=None) -> dict:
    """执行一次 refresh-token grant；不会自动重试或回传提交的 RT。"""
    token = str(refresh_token or "").strip()
    if not token:
        raise RuntimeError("缺少 Platform OAuth refresh_token")
    client = session or requests.Session()
    try:
        response = client.post(
            PLATFORM_TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": PLATFORM_CLIENT_ID,
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"{type(exc).__name__}: OAuth Token 请求失败") from exc

    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        raise RuntimeError(_safe_error(response, token))
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError("OAuth Token 响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("OAuth Token 响应格式无效")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("OAuth Token 响应缺少 access_token")
    result = {"access_token": access_token}
    for key in ("refresh_token", "id_token", "token_type"):
        value = str(payload.get(key) or "").strip()
        if value:
            result[key] = value
    if payload.get("expires_in") is not None:
        try:
            result["expires_in"] = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            pass
    return result


def _merge_oauth(current: dict, tokens: dict) -> dict:
    merged = dict(current or {})
    for key in ("access_token", "refresh_token", "id_token", "token_type"):
        value = str(tokens.get(key) or "").strip()
        if value:
            merged[key] = value
    if tokens.get("expires_in") is not None:
        try:
            merged["expires_in"] = int(tokens.get("expires_in") or 0)
        except (TypeError, ValueError):
            pass
    return merged


def _save_codex_account(platform_oauth: dict, email: str) -> str:
    from core.codex_oauth import (
        _parse_id_token,
        build_codex_storage,
        save_codex_credential,
    )

    claims = _parse_id_token(str(platform_oauth.get("id_token") or ""))
    claims["email"] = str(claims.get("email") or email).strip()
    storage = build_codex_storage(platform_oauth, claims)
    path = save_codex_credential(
        storage,
        claims["email"],
        str(claims.get("plan_type") or ""),
    )
    return str(path)


def _run_one(item: dict, batch_id: str, index: int, total: int) -> dict:
    acc_id = int(item["id"])
    email = str(item.get("email") or "").strip()
    if not db.mark_account_platform_oauth_refresh_running(acc_id):
        return {
            "ok": False,
            "status": "skipped",
            "id": acc_id,
            "email": email,
            "error": "账号已删除或 OAuth 刷新状态已被重置",
        }
    logger.info("[Platform OAuth 刷新] batch=%s %s/%s email=%s", batch_id, index, total, email)
    try:
        account = db.get_account_platform_oauth_context(acc_id)
        if not account:
            raise RuntimeError("账号不存在")
        email = str(account.get("email") or email).strip()
        current_oauth = account.get("platform_oauth") if isinstance(account.get("platform_oauth"), dict) else {}
        refresh_token = str(current_oauth.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError("账号没有 Platform OAuth refresh_token")
        tokens = exchange_refresh_token(refresh_token)
        merged_oauth = _merge_oauth(current_oauth, tokens)
        if not db.complete_account_platform_oauth_refresh(acc_id, {
            "ok": True,
            "tokens": tokens,
            "message": "OAuth Token 刷新成功",
        }):
            raise RuntimeError("账号凭证保存失败")

        try:
            _save_codex_account(merged_oauth, email)
            credential_result = {"status": "success", "message": "Codex 凭证已更新"}
        except Exception as exc:  # noqa: BLE001 - 文件同步失败不能终止 OAuth 持久化
            credential_result = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {str(exc)[:220]}",
            }
            logger.warning(
                "[Platform OAuth 刷新] Codex 凭证写入失败: email=%s error=%s",
                email,
                credential_result["error"],
            )

        upload_result = chatgpt2api_client.auto_upload_registered_account(
            str(account.get("access_token") or ""),
            platform_oauth=merged_oauth,
            email=email,
            password=str(account.get("registration_password") or ""),
        )
        if not isinstance(upload_result, dict):
            upload_result = {"status": "failed", "error": "chatgpt2api 返回了无效结果"}
        db.update_account_platform_oauth_sync_result(
            acc_id,
            credential_result=credential_result,
            upload_result=upload_result,
        )
        partial = (
            credential_result.get("status") != "success"
            or upload_result.get("status") == "failed"
        )
        logger.info(
            "[Platform OAuth 刷新] 完成: batch=%s email=%s credential=%s upload=%s",
            batch_id,
            email,
            credential_result.get("status"),
            upload_result.get("status"),
        )
        return {
            "ok": True,
            "status": "partial" if partial else "success",
            "id": acc_id,
            "email": email,
            "credential_status": credential_result.get("status"),
            "upload_status": upload_result.get("status"),
            "upload_mode": upload_result.get("mode"),
        }
    except Exception as exc:  # noqa: BLE001 - 单账号失败必须转为可持久化的批量结果
        error = f"{type(exc).__name__}: {str(exc)[:260]}"
        db.complete_account_platform_oauth_refresh(acc_id, {"ok": False, "error": error})
        logger.warning("[Platform OAuth 刷新] 失败: batch=%s email=%s error=%s", batch_id, email, error)
        return {
            "ok": False,
            "status": "failed",
            "id": acc_id,
            "email": email,
            "error": error,
        }


def _run_batch(items: list[dict], workers: int, batch_id: str) -> None:
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"platform-oauth-refresh-{batch_id}",
    ) as executor:
        futures = [
            executor.submit(_run_one, item, batch_id, index, len(items))
            for index, item in enumerate(items, 1)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                logger.exception("[Platform OAuth 刷新] 批量子任务异常: batch=%s", batch_id)


def start_batch(items: list[dict], workers: int = _MAX_WORKERS) -> dict:
    """异步启动已 claim 的账号；调用方和服务端都会把并发限制为 3。"""
    if not items:
        raise ValueError("OAuth 刷新任务为空")
    workers = max(1, min(_MAX_WORKERS, int(workers)))
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    try:
        threading.Thread(
            target=_run_batch,
            args=(list(items), workers, batch_id),
            name=f"platform-oauth-refresh-dispatch-{batch_id}",
            daemon=True,
        ).start()
    except Exception as exc:
        for item in items:
            db.complete_account_platform_oauth_refresh(
                int(item["id"]),
                {"ok": False, "error": f"OAuth 批量任务启动失败: {type(exc).__name__}: {exc}"},
            )
        raise
    return {"batch_id": batch_id, "workers": workers, "count": len(items)}
