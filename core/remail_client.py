# -*- coding: utf-8 -*-
"""Remail（remail.aishop6.com）邮箱领取与收件客户端。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp

logger = logging.getLogger(__name__)

BASE_URL = "https://remail.aishop6.com"
REQUEST_TIMEOUT = 20
REQUEST_MAX_ATTEMPTS = 4
REQUEST_RETRY_BASE_DELAY = 2.0
REQUEST_RETRY_MAX_DELAY = 15.0
DELIVERY_MAX_WAIT = 30
DELIVERY_POLL_INTERVAL = 1
SELECTION_CACHE_TTL = 300

_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
_SERVICE_MODES = {
    "code": ("codeEnabled", "codePrice", "短效接码"),
    "purchase": ("purchaseEnabled", "purchasePrice", "长效购买"),
}

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 长效订单的 serviceToken 是后续收取登录验证码所必需的凭证。
# 该文件只存本地运行数据，且由 .gitignore 排除，不通过 WebUI API 返回。
_PERSISTED_CONTEXT_PATH = _PROJECT_ROOT / "remail_long_lived_accounts.json"


class RemailError(RuntimeError):
    """Remail 请求、下单或取码失败。"""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# 与其他邮箱客户端的异常命名保持兼容。
RemailClientError = RemailError


@dataclass(frozen=True)
class RemailSelection:
    project_id: int
    product_id: int
    project_name: str
    product_type: str
    service_mode: str = "code"
    email_suffixes: tuple[str, ...] = ()


@dataclass
class RemailAccount:
    email: str
    service_token: str
    order_no: str
    project_id: int
    product_id: int
    service_mode: str = "code"
    order_id: str = ""
    status: str = ""
    receive_started_at: str = ""
    receive_until: str = ""
    activated_at: str = ""
    after_sale_until: str = ""
    last_mail_received_at: str = ""
    email_suffix: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_long_lived(self) -> bool:
        return str(self.service_mode or "").strip().lower() == "purchase"


_CONTEXT_CACHE: dict[str, RemailAccount] = {}
_SELECTION_CACHE: dict[str, tuple[float, RemailSelection]] = {}
_SELECTION_LOCK = threading.Lock()
_CONTEXT_LOCK = threading.RLock()


def _context_to_dict(account: RemailAccount, *, include_secret: bool = True) -> dict:
    """把上下文转换为内部持久化格式；默认包含 serviceToken（仅本地使用）。"""
    data = {
        "email": account.email,
        "order_no": account.order_no,
        "project_id": int(account.project_id or 0),
        "product_id": int(account.product_id or 0),
        "service_mode": _service_mode(account.service_mode),
        "order_id": account.order_id,
        "status": account.status,
        "receive_started_at": account.receive_started_at,
        "receive_until": account.receive_until,
        "activated_at": account.activated_at,
        "after_sale_until": account.after_sale_until,
        "last_mail_received_at": account.last_mail_received_at,
        "email_suffix": account.email_suffix,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }
    if include_secret:
        data["service_token"] = account.service_token
    return data


def _context_from_dict(raw: dict | None) -> RemailAccount | None:
    if not isinstance(raw, dict):
        return None
    email = str(raw.get("email") or "").strip()
    token = str(raw.get("service_token") or raw.get("serviceToken") or "").strip()
    order_no = str(raw.get("order_no") or raw.get("orderNo") or "").strip()
    if not email or "@" not in email or not token:
        return None
    try:
        project_id = int(raw.get("project_id") or raw.get("projectId") or 0)
        product_id = int(raw.get("product_id") or raw.get("productId") or 0)
    except (TypeError, ValueError):
        project_id = product_id = 0
    try:
        mode = _service_mode(str(raw.get("service_mode") or raw.get("serviceMode") or "purchase"))
    except RemailError:
        mode = "purchase"
    return RemailAccount(
        email=email,
        service_token=token,
        order_no=order_no,
        project_id=project_id,
        product_id=product_id,
        service_mode=mode,
        order_id=str(raw.get("order_id") or raw.get("orderId") or ""),
        status=str(raw.get("status") or ""),
        receive_started_at=str(raw.get("receive_started_at") or raw.get("receiveStartedAt") or ""),
        receive_until=str(raw.get("receive_until") or raw.get("receiveUntil") or ""),
        activated_at=str(raw.get("activated_at") or raw.get("activatedAt") or ""),
        after_sale_until=str(raw.get("after_sale_until") or raw.get("afterSaleUntil") or ""),
        last_mail_received_at=str(raw.get("last_mail_received_at") or raw.get("lastMailReceivedAt") or ""),
        email_suffix=str(raw.get("email_suffix") or raw.get("emailSuffix") or ""),
        created_at=str(raw.get("created_at") or raw.get("createdAt") or ""),
        updated_at=str(raw.get("updated_at") or raw.get("updatedAt") or ""),
    )


def _load_persisted_contexts() -> dict[str, dict]:
    try:
        if not _PERSISTED_CONTEXT_PATH.exists():
            return {}
        raw = json.loads(_PERSISTED_CONTEXT_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("[Remail] 长效邮箱上下文文件读取失败，将按空上下文继续")
        return {}
    if isinstance(raw, list):
        raw = {
            str(item.get("email") or "").strip().lower(): item
            for item in raw
            if isinstance(item, dict) and str(item.get("email") or "").strip()
        }
    if not isinstance(raw, dict):
        return {}
    return {str(key).strip().lower(): value for key, value in raw.items() if isinstance(value, dict)}


def _save_persisted_contexts(contexts: dict[str, dict]) -> None:
    _PERSISTED_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PERSISTED_CONTEXT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(contexts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, _PERSISTED_CONTEXT_PATH)
    try:
        os.chmod(_PERSISTED_CONTEXT_PATH, 0o600)
    except OSError:
        pass


def _persist_context(account: RemailAccount) -> bool:
    if not account.is_long_lived:
        return False
    try:
        with _CONTEXT_LOCK:
            contexts = _load_persisted_contexts()
            contexts[_cache_key(account.email)] = _context_to_dict(account)
            _save_persisted_contexts(contexts)
        return True
    except Exception as exc:
        # 已购买邮箱仍应继续当前注册；注册成功时还会把同一凭证写入账号 extra_json。
        logger.warning(
            "[Remail] 长效邮箱上下文文件写入失败，将依赖账号记录恢复: email=%s error=%s",
            account.email,
            f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return False


def _context_from_registered_account(email: str) -> RemailAccount | None:
    """迁移兼容：若独立上下文文件缺失，尝试从已保存账号 extra_json 恢复。"""
    try:
        from core import db

        row = db.get_account_by_email(email)
        raw_extra = (row or {}).get("extra_json") if isinstance(row, dict) else None
        if isinstance(raw_extra, str):
            raw_extra = json.loads(raw_extra) if raw_extra else {}
        if isinstance(raw_extra, dict):
            remail = raw_extra.get("remail")
            account = _context_from_dict(remail if isinstance(remail, dict) else None)
            if account and account.is_long_lived:
                _persist_context(account)
                return account
    except Exception:
        # 恢复失败不应阻断普通邮箱来源解析。
        logger.debug("[Remail] 从账号记录恢复长效上下文失败", exc_info=True)
    return None


def export_account_context(email: str, *, include_secret: bool = True) -> dict | None:
    """返回账号对应的长效上下文，供账号落库/补登服务使用。"""
    account = get_account_context(email)
    if account is None or not account.is_long_lived:
        return None
    return _context_to_dict(account, include_secret=include_secret)


def is_long_lived_email(email: str) -> bool:
    account = get_account_context(email)
    return bool(account and account.is_long_lived)


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _api_key(api_key_override: str | None = None) -> str:
    raw = getattr(_email_cfg, "REMAIL_API_KEY", "") if api_key_override is None else api_key_override
    api_key = str(raw or "").strip()
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    if not api_key:
        raise RemailError(
            "Remail API Key 未配置，请填写 Remail API Key（WebUI「配置 → 邮箱 / OTP」）。"
        )
    return api_key


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _service_mode(value: str | None = None) -> str:
    raw = getattr(_email_cfg, "REMAIL_SERVICE_MODE", "code") if value is None else value
    mode = str(raw or "code").strip().lower()
    if mode not in _SERVICE_MODES:
        raise RemailError("Remail REMAIL_SERVICE_MODE 仅支持 code（短效）或 purchase（长效）")
    return mode


def _service_mode_label(mode: str) -> str:
    return _SERVICE_MODES[_service_mode(mode)][2]


def _error_message(payload, response) -> str:
    if isinstance(payload, dict):
        code = str(payload.get("code") or "").strip()
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if value:
                return f"{code}: {value}" if code else str(value)
        if code:
            return code
        fields = payload.get("fields")
        if fields:
            return str(fields)
    return str(getattr(response, "text", "") or "")[:240]


def _retry_after(response) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _retry_delay(failed_attempt: int, retry_after: float | None = None) -> float:
    # 第一次失败立即切换网络路径；之后按 2s、4s 退避，避免瞬时抖动直接中止任务。
    base = 0.0 if failed_attempt == 1 else REQUEST_RETRY_BASE_DELAY * (2 ** (failed_attempt - 2))
    delay = max(base, float(retry_after or 0))
    return min(REQUEST_RETRY_MAX_DELAY, delay)


def _route_for_attempt(attempt: int) -> tuple[str, dict | None]:
    # Remail API 与注册出口无绑定要求。macOS/系统代理对该站点可能间歇返回 522/SSL EOF，
    # 因此优先直连；失败时交替尝试系统网络设置，兼顾必须经代理访问的环境。
    if attempt % 2 == 1:
        return "直连", dict(_DIRECT_PROXIES)
    return "系统网络设置", None


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    authenticated: bool = True,
    api_key_override: str | None = None,
    extra_headers: dict[str, str] | None = None,
    max_attempts: int | None = None,
):
    headers = {"Accept": "application/json"}
    if authenticated:
        headers["Authorization"] = f"Bearer {_api_key(api_key_override)}"
    if extra_headers:
        headers.update(extra_headers)

    attempt_limit = max(1, int(max_attempts or REQUEST_MAX_ATTEMPTS))
    last_error: Exception | None = None
    for attempt in range(1, attempt_limit + 1):
        route_name, proxies = _route_for_attempt(attempt)
        try:
            response = requests.request(
                method,
                BASE_URL + path,
                params=params,
                json=json,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                proxies=proxies,
            )
        except requests.RequestException as exc:
            last_error = exc
            error_name = type(exc).__name__
            if attempt >= attempt_limit:
                raise RemailError(
                    f"Remail 请求失败 ({path}, {route_name}): 网络异常 {error_name}"
                ) from None
            delay = _retry_delay(attempt)
            next_route, _ = _route_for_attempt(attempt + 1)
            logger.warning(
                "[Remail] 请求经%s失败，%.1fs 后改用%s重试 (%s/%s): %s %s; %s: %s",
                route_name,
                delay,
                next_route,
                attempt + 1,
                attempt_limit,
                method,
                path,
                error_name,
                "网络连接失败",
            )
            if delay > 0:
                time.sleep(delay)
            continue

        try:
            payload = response.json()
        except ValueError:
            payload = None

        status_code = int(response.status_code)
        retryable = status_code in _RETRYABLE_HTTP_STATUSES or (
            200 <= status_code < 300 and payload is None
        )
        if retryable and attempt < attempt_limit:
            retry_after = _retry_after(response)
            delay = _retry_delay(attempt, retry_after)
            next_route, _ = _route_for_attempt(attempt + 1)
            message = _error_message(payload, response)
            logger.warning(
                "[Remail] 请求经%s返回临时异常，%.1fs 后改用%s重试 (%s/%s): %s %s; HTTP %s; %s",
                route_name,
                delay,
                next_route,
                attempt + 1,
                attempt_limit,
                method,
                path,
                status_code,
                message or "响应不是 JSON",
            )
            if delay > 0:
                time.sleep(delay)
            continue

        if status_code == 401 and authenticated:
            raise RemailError("Remail API Key 非法、已禁用或已过期")
        if status_code >= 400:
            message = _error_message(payload, response)
            raise RemailError(
                f"Remail 请求失败 ({path}): HTTP {status_code}; {message or str(payload)[:240]}",
                retry_after=_retry_after(response),
            )
        if payload is None:
            raise RemailError(f"Remail 响应不是 JSON ({path}): HTTP {status_code}")
        if attempt > 1:
            logger.info("[Remail] 请求重试成功: %s %s via=%s attempt=%s", method, path, route_name, attempt)
        return payload

    error_name = type(last_error).__name__ if last_error is not None else "未知网络错误"
    raise RemailError(f"Remail 请求失败 ({path}): {error_name}")


def _container_candidates(payload) -> list[dict]:
    out: list[dict] = []
    if isinstance(payload, dict):
        out.append(payload)
        data = payload.get("data")
        if isinstance(data, dict):
            out.append(data)
    return out


def _list_items(payload) -> list[dict]:
    for container in _container_candidates(payload):
        items = container.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _wallet_from_payload(payload) -> dict:
    allowed_fields = (
        "userId",
        "consumerBalance",
        "supplierAvailable",
        "supplierFrozen",
        "historicalSpend",
        "orderCount",
        "updatedAt",
    )
    candidates: list[dict] = []
    for container in _container_candidates(payload):
        candidates.append(container)
        wallet = container.get("wallet")
        if isinstance(wallet, dict):
            candidates.append(wallet)

    for candidate in candidates:
        if not any(field in candidate for field in allowed_fields):
            continue
        wallet = {
            field: candidate.get(field)
            for field in allowed_fields
            if field in candidate and candidate.get(field) is not None
        }
        if "orderCount" in wallet:
            try:
                wallet["orderCount"] = int(wallet["orderCount"])
            except (TypeError, ValueError):
                pass
        return wallet
    raise RemailError("Remail 钱包响应缺少余额对象")


def get_wallet(api_key: str | None = None) -> dict:
    """只读查询 Remail 钱包；api_key 可使用 WebUI 尚未保存的表单值。"""
    payload = _request(
        "GET",
        "/v1/open/wallet",
        api_key_override=api_key,
    )
    return _wallet_from_payload(payload)


def _project_score(project: dict) -> int:
    name = str(project.get("name") or "").strip().lower()
    target = str(project.get("targetPlatform") or "").strip().lower()
    description = str(project.get("description") or "").strip().lower()
    normalized_name = re.sub(r"\s+", "", name)
    normalized_target = re.sub(r"\s+", "", target)
    hints = ("openai", "chatgpt")

    score = 0
    if normalized_target in hints:
        score += 80
    elif any(hint in normalized_target for hint in hints):
        score += 60
    if normalized_name in hints:
        score += 50
    elif any(hint in normalized_name for hint in hints):
        score += 40
    elif "gpt" in normalized_name:
        score += 20
    if any(hint in description for hint in hints):
        score += 10
    return score


def _normalize_email_suffix(value) -> str:
    suffix = str(value or "").strip().lower().lstrip("@").rstrip(".")
    if not suffix or len(suffix) > 253 or ".." in suffix:
        return ""
    labels = suffix.split(".")
    if any(
        len(label) > 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
        for label in labels
    ):
        return ""
    return suffix


def _configured_email_suffixes() -> tuple[str, ...]:
    raw = getattr(_email_cfg, "REMAIL_EMAIL_SUFFIXES", []) or []
    if isinstance(raw, str):
        values = [raw]
    else:
        try:
            values = list(raw)
        except TypeError:
            values = [raw]

    out: list[str] = []
    invalid: list[str] = []
    for value in values:
        for token in re.split(r"[\s,;|]+", str(value or "")):
            token = token.strip()
            if not token:
                continue
            suffix = _normalize_email_suffix(token)
            if not suffix:
                invalid.append(token)
                continue
            if suffix not in out:
                out.append(suffix)
    if invalid:
        raise RemailError(
            "Remail REMAIL_EMAIL_SUFFIXES 包含无效邮箱后缀: " + ", ".join(invalid[:6])
        )
    return tuple(out)


def _product_inventory(product: dict) -> int | None:
    for key in ("totalAvailable", "publicAvailable"):
        if key not in product or product.get(key) is None:
            continue
        try:
            return int(product.get(key))
        except (TypeError, ValueError):
            continue
    return None


def _suffix_inventory(item: dict) -> int | None:
    for key in ("totalAvailable", "publicAvailable"):
        if key not in item or item.get(key) is None:
            continue
        try:
            return int(item.get(key))
        except (TypeError, ValueError):
            continue
    return None


def _available_product_suffixes(product: dict) -> tuple[str, ...]:
    suffixes = product.get("suffixes")
    if not isinstance(suffixes, list):
        return ()
    out: list[str] = []
    for item in suffixes:
        if not isinstance(item, dict):
            continue
        suffix = _normalize_email_suffix(item.get("suffix"))
        if not suffix:
            continue
        inventory = _suffix_inventory(item)
        if inventory is not None and inventory <= 0:
            continue
        if suffix not in out:
            out.append(suffix)
    return tuple(out)


def _matching_email_suffixes(
    product: dict,
    configured_suffixes: tuple[str, ...],
) -> tuple[str, ...]:
    if not configured_suffixes:
        return ()
    available = set(_available_product_suffixes(product))
    return tuple(suffix for suffix in configured_suffixes if suffix in available)


def _product_price(product: dict, service_mode: str | None = None) -> float:
    mode = _service_mode(service_mode)
    price_key = _SERVICE_MODES[mode][1]
    try:
        return float(product.get(price_key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _product_sort_key(product: dict, service_mode: str | None = None) -> tuple[int, float, int, int]:
    mode = _service_mode(service_mode)
    product_type = str(product.get("type") or "").strip().lower()
    type_priority = {"microsoft": 0, "domain": 1, "random": 2}.get(product_type, 9)
    inventory = _product_inventory(product)
    product_id = int(product.get("id") or 0)
    return type_priority, _product_price(product, mode), -(inventory or 0), product_id


def _eligible_products(
    products,
    configured_suffixes: tuple[str, ...] = (),
    service_mode: str | None = None,
) -> list[dict]:
    mode = _service_mode(service_mode)
    enabled_key = _SERVICE_MODES[mode][0]
    out: list[dict] = []
    if not isinstance(products, list):
        return out
    for product in products:
        if not isinstance(product, dict):
            continue
        if str(product.get("status") or "enabled").strip().lower() != "enabled":
            continue
        if product.get(enabled_key) is not True:
            continue
        try:
            if int(product.get("id") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        inventory = _product_inventory(product)
        if inventory is not None and inventory <= 0:
            continue
        if configured_suffixes and not _matching_email_suffixes(product, configured_suffixes):
            continue
        out.append(product)
    return sorted(out, key=lambda product: _product_sort_key(product, mode))


def _list_projects(search: str | None) -> list[dict]:
    params = {"scope": "visible", "status": "listed", "offset": 0, "limit": 100}
    if search:
        params["search"] = search
    payload = _request("GET", "/v1/open/projects", params=params)
    return _list_items(payload)


def _project_detail(project_id: int) -> tuple[dict, list[dict]]:
    payload = _request("GET", f"/v1/open/projects/{project_id}")
    for container in _container_candidates(payload):
        project = container.get("project")
        products = container.get("products")
        if isinstance(project, dict) and isinstance(products, list):
            return project, [item for item in products if isinstance(item, dict)]
    raise RemailError(f"Remail 项目详情响应缺少 project/products: project_id={project_id}")


def _configured_ids() -> tuple[int, int]:
    def _as_int(name: str) -> int:
        raw = getattr(_email_cfg, name, 0)
        try:
            value = int(raw or 0)
        except (TypeError, ValueError) as exc:
            raise RemailError(f"Remail {name} 必须是非负整数") from exc
        if value < 0:
            raise RemailError(f"Remail {name} 必须是非负整数")
        return value

    return _as_int("REMAIL_PROJECT_ID"), _as_int("REMAIL_PRODUCT_ID")


def _selection_from_product(
    *,
    project_id: int,
    project_name: str,
    product: dict,
    configured_suffixes: tuple[str, ...],
    service_mode: str,
) -> RemailSelection:
    matched_suffixes = _matching_email_suffixes(product, configured_suffixes)
    if configured_suffixes and not matched_suffixes:
        raise RemailError(
            "Remail 商品不支持配置的 emailSuffix，或这些后缀当前无库存: "
            + ", ".join(configured_suffixes)
        )
    ignored_suffixes = [suffix for suffix in configured_suffixes if suffix not in matched_suffixes]
    if ignored_suffixes:
        logger.warning(
            "[Remail] 以下配置后缀在商品 %s 中不可用或无库存，已从随机池排除: %s",
            product.get("id"),
            ", ".join(ignored_suffixes),
        )
    return RemailSelection(
        project_id=project_id,
        product_id=int(product.get("id") or 0),
        project_name=project_name,
        product_type=str(product.get("type") or ""),
        service_mode=_service_mode(service_mode),
        email_suffixes=matched_suffixes,
    )


def _configured_selection(
    configured_suffixes: tuple[str, ...] | None = None,
    service_mode: str | None = None,
) -> RemailSelection | None:
    mode = _service_mode(service_mode)
    configured_suffixes = (
        _configured_email_suffixes() if configured_suffixes is None else configured_suffixes
    )
    project_id, product_id = _configured_ids()
    if not project_id and not product_id:
        return None
    if not project_id:
        raise RemailError("已填写 REMAIL_PRODUCT_ID，但 REMAIL_PROJECT_ID 为空")

    try:
        project, products = _project_detail(project_id)
    except RemailError as exc:
        raise RemailError(f"Remail 配置的项目 ID 无法读取: project_id={project_id}; {exc}") from exc

    if product_id:
        selected = None
        for item in products:
            try:
                item_id = int(item.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if item_id == product_id:
                selected = item
                break
        if selected is None:
            raise RemailError(
                f"Remail 配置的商品不属于该项目: project_id={project_id}, product_id={product_id}"
            )
        if not _eligible_products([selected], service_mode=mode):
            raise RemailError(
                f"Remail 配置的商品当前不可用于{_service_mode_label(mode)}或无库存: "
                f"project_id={project_id}, product_id={product_id}"
            )
        return _selection_from_product(
            project_id=project_id,
            project_name=str(project.get("name") or project_id),
            product=selected,
            configured_suffixes=configured_suffixes,
            service_mode=mode,
        )

    eligible = _eligible_products(products, configured_suffixes, mode)
    if not eligible:
        suffix_note = (
            f"，且需支持 emailSuffix={','.join(configured_suffixes)}"
            if configured_suffixes
            else ""
        )
        raise RemailError(
            f"Remail 配置的项目暂无可用{_service_mode_label(mode)}商品: project_id={project_id}{suffix_note}"
        )
    selected = eligible[0]
    return _selection_from_product(
        project_id=project_id,
        project_name=str(project.get("name") or project_id),
        product=selected,
        configured_suffixes=configured_suffixes,
        service_mode=mode,
    )


def _discover_selection(
    configured_suffixes: tuple[str, ...] = (),
    service_mode: str | None = None,
) -> RemailSelection:
    mode = _service_mode(service_mode)
    projects: dict[int, dict] = {}
    errors: list[str] = []
    saw_match = False
    processed_project_ids: set[int] = set()
    weak_selection: RemailSelection | None = None
    for search in ("OpenAI", "ChatGPT", None):
        for project in _list_projects(search):
            try:
                project_id = int(project.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if project_id > 0:
                projects[project_id] = project

        matched = [project for project in projects.values() if _project_score(project) > 0]
        matched.sort(key=lambda item: (-_project_score(item), int(item.get("id") or 0)))
        if not matched:
            if search is None:
                break
            continue
        saw_match = True
        for summary in matched:
            project_id = int(summary.get("id") or 0)
            if project_id in processed_project_ids:
                continue
            processed_project_ids.add(project_id)
            try:
                project, products = _project_detail(project_id)
            except RemailError as exc:
                products = summary.get("products") if isinstance(summary.get("products"), list) else []
                project = summary
                if not products:
                    errors.append(str(exc))
                    continue
            eligible = _eligible_products(products, configured_suffixes, mode)
            if not eligible:
                suffix_note = (
                    f"并支持 emailSuffix={','.join(configured_suffixes)}"
                    if configured_suffixes
                    else ""
                )
                errors.append(
                    f"project_id={project_id} 没有已启用、有库存{suffix_note}的{_service_mode_label(mode)}商品"
                )
                continue
            product = eligible[0]
            candidate = _selection_from_product(
                project_id=project_id,
                project_name=str(project.get("name") or summary.get("name") or project_id),
                product=product,
                configured_suffixes=configured_suffixes,
                service_mode=mode,
            )
            # 名称/目标平台明确匹配的项目优先；仅描述中命中的项目最后兜底。
            if _project_score(summary) >= 20:
                return candidate
            weak_selection = weak_selection or candidate

        if search is None:
            break

    if weak_selection is not None:
        return weak_selection
    if not saw_match:
        raise RemailError(
            "Remail 当前 API Key 下未找到 OpenAI/ChatGPT 接码项目；"
            "请先在平台确认该项目可见或已获授权。"
        )

    raise RemailError(
        f"Remail OpenAI/ChatGPT 项目暂无可用{_service_mode_label(mode)}商品；"
        + "；".join(errors[:4])
    )


def _selection(force_refresh: bool = False) -> RemailSelection:
    api_key = _api_key()
    service_mode = _service_mode()
    configured_suffixes = _configured_email_suffixes()
    if any(_configured_ids()):
        return _configured_selection(
            configured_suffixes, service_mode
        )  # 显式覆盖不走缓存，便于 WebUI 热加载后立即生效。

    fingerprint = _key_fingerprint(api_key + "\0" + service_mode + "\0" + "\0".join(configured_suffixes))
    now = time.monotonic()
    with _SELECTION_LOCK:
        cached = _SELECTION_CACHE.get(fingerprint)
        if not force_refresh and cached and now - cached[0] < SELECTION_CACHE_TTL:
            return cached[1]
        selected = _discover_selection(configured_suffixes, service_mode)
        _SELECTION_CACHE[fingerprint] = (time.monotonic(), selected)
        return selected


def _order_from_payload(payload) -> dict:
    for container in _container_candidates(payload):
        order = container.get("order")
        if isinstance(order, dict):
            return order
        if container.get("orderNo") or container.get("deliveryEmail"):
            return container
    raise RemailError("Remail 下单响应缺少订单对象")


def _create_order(selection: RemailSelection) -> dict:
    service_mode = _service_mode(selection.service_mode)
    body = {"projectId": selection.project_id, "productId": selection.product_id}
    if selection.email_suffixes:
        email_suffix = random.choice(selection.email_suffixes)
        body["emailSuffix"] = email_suffix
        logger.info(
            "[Remail] 本次下单从配置白名单随机选择 emailSuffix=%s（候选=%s）",
            email_suffix,
            ",".join(selection.email_suffixes),
        )
    payload = _request(
        "POST",
        "/v1/open/orders",
        params={"serviceMode": service_mode, "supply": "private_first"},
        json=body,
        extra_headers={"Idempotency-Key": f"turb-gpt-remail-{uuid.uuid4().hex}"},
    )
    return _order_from_payload(payload)


def _order_failure(order: dict) -> str | None:
    status = str(order.get("status") or "").strip().lower()
    if status not in {"failed", "refunded", "closed"}:
        return None
    code = str(order.get("failureCode") or "").strip()
    return f"status={status}" + (f", failureCode={code}" if code else "")


def _wait_for_delivery(order: dict) -> dict:
    order_no = str(order.get("orderNo") or "").strip()
    if not order_no:
        raise RemailError("Remail 下单响应缺少 orderNo")

    deadline = time.monotonic() + DELIVERY_MAX_WAIT
    current = order
    while True:
        failure = _order_failure(current)
        if failure:
            raise RemailError(f"Remail 订单交付失败: order={order_no}; {failure}")

        email = str(current.get("deliveryEmail") or "").strip()
        token = str(current.get("serviceToken") or "").strip()
        if email and "@" in email and token:
            return current

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(DELIVERY_POLL_INTERVAL, remaining))
        payload = _request("GET", f"/v1/open/orders/{quote(order_no, safe='')}")
        current = _order_from_payload(payload)

    raise RemailError(f"等待 Remail 订单交付邮箱或 serviceToken 超时: order={order_no}")


def pick_account() -> RemailAccount:
    """自动选择 OpenAI/ChatGPT 项目，按配置模式下单领取邮箱。"""
    last_error: RemailError | None = None
    for attempt in range(2):
        selection = _selection(force_refresh=attempt > 0)
        try:
            order = _wait_for_delivery(_create_order(selection))
            email = str(order.get("deliveryEmail") or "").strip()
            token = str(order.get("serviceToken") or "").strip()
            order_no = str(order.get("orderNo") or "").strip()
            try:
                order_id = str(order.get("id") or "")
            except Exception:
                order_id = ""
            account = RemailAccount(
                email=email,
                service_token=token,
                order_no=order_no,
                project_id=selection.project_id,
                product_id=selection.product_id,
                service_mode=str(order.get("serviceMode") or selection.service_mode or "code"),
                order_id=order_id,
                status=str(order.get("status") or ""),
                receive_started_at=str(order.get("receiveStartedAt") or ""),
                receive_until=str(order.get("receiveUntil") or ""),
                activated_at=str(order.get("activatedAt") or ""),
                after_sale_until=str(order.get("afterSaleUntil") or ""),
                last_mail_received_at=str(order.get("lastMailReceivedAt") or ""),
                email_suffix=email.rsplit("@", 1)[-1].lower() if "@" in email else "",
                created_at=str(order.get("createdAt") or ""),
                updated_at=str(order.get("updatedAt") or ""),
            )
            with _CONTEXT_LOCK:
                _CONTEXT_CACHE[_cache_key(email)] = account
                # 短效模式保持原有进程内逻辑；只有长效购买写入磁盘，供未来补登。
                _persist_context(account)
            logger.info(
                "[Remail] 已领取%s邮箱: %s order=%s project=%s(%s) product=%s(%s)",
                _service_mode_label(selection.service_mode),
                email,
                order_no,
                selection.project_name,
                selection.project_id,
                selection.product_type,
                selection.product_id,
            )
            return account
        except RemailError as exc:
            last_error = exc
            text = str(exc).lower()
            retryable_selection_error = any(
                marker in text
                for marker in (
                    "insufficient_inventory",
                    "allocation_failed",
                    "project is not available",
                    "product",
                    "库存",
                )
            )
            if attempt == 0 and retryable_selection_error:
                logger.warning("[Remail] 当前商品下单失败，将刷新项目/库存后重试一次: %s", exc)
                continue
            raise
    raise last_error or RemailError("Remail 领取邮箱失败")


def get_email() -> str:
    """兼容其他临时邮箱客户端的简化入口。"""
    return pick_account().email


def get_account_context(email: str) -> RemailAccount | None:
    key = _cache_key(email)
    if not key:
        return None
    with _CONTEXT_LOCK:
        cached = _CONTEXT_CACHE.get(key)
        if cached is not None:
            return cached
        persisted = _load_persisted_contexts().get(key)
        account = _context_from_dict(persisted)
        if account is None:
            account = _context_from_registered_account(email)
        if account is not None:
            _CONTEXT_CACHE[key] = account
        return account


def refresh_account_context(email: str, *, update_registered_account: bool = True) -> RemailAccount:
    """从订单详情刷新长效邮箱的激活、质保和收件时间。"""
    account = get_account_context(email)
    if account is None or not account.is_long_lived:
        raise RemailError("Remail 长效邮箱上下文不存在")
    if not str(account.order_no or "").strip():
        raise RemailError("Remail 长效邮箱缺少订单号，无法刷新订单时间")

    payload = _request(
        "GET",
        f"/v1/open/orders/{quote(str(account.order_no), safe='')}",
    )
    order = _order_from_payload(payload)
    field_map = {
        "status": "status",
        "receiveStartedAt": "receive_started_at",
        "receiveUntil": "receive_until",
        "activatedAt": "activated_at",
        "afterSaleUntil": "after_sale_until",
        "lastMailReceivedAt": "last_mail_received_at",
        "createdAt": "created_at",
        "updatedAt": "updated_at",
    }
    with _CONTEXT_LOCK:
        for source_key, target_attr in field_map.items():
            value = order.get(source_key)
            if value not in (None, ""):
                setattr(account, target_attr, str(value))
        _CONTEXT_CACHE[_cache_key(account.email)] = account
        _persist_context(account)

    if update_registered_account:
        try:
            from core import db

            db.update_account_remail_context(
                account.email,
                _context_to_dict(account, include_secret=True),
            )
        except Exception as exc:
            logger.warning(
                "[Remail] 刷新订单时间后同步账号记录失败: email=%s error=%s",
                account.email,
                f"{type(exc).__name__}: {str(exc)[:180]}",
            )
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """释放 Remail 上下文；短效清理，长效保留 serviceToken 供后续补登。"""
    key = _cache_key(email)
    with _CONTEXT_LOCK:
        account = _CONTEXT_CACHE.get(key) or _context_from_dict(_load_persisted_contexts().get(key))
        if account is not None and account.is_long_lived:
            # 订单状态由平台管理；注册任务失败也不能把已经购买的长效邮箱凭证丢掉。
            _CONTEXT_CACHE[key] = account
            _persist_context(account)
            logger.info(
                "[Remail] 已保留长效邮箱上下文: %s order=%s（status=%s, note=%s）",
                email,
                account.order_no or "-",
                status,
                note or "",
            )
            return
        _CONTEXT_CACHE.pop(key, None)
        logger.info("[Remail] 已释放短效邮箱上下文: %s（status=%s, note=%s）", email, status, note or "")


def _timestamp(item: dict) -> float | None:
    for key in ("receivedAt", "received_at", "createdAt", "created_at", "timestamp"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
            return value / 1000 if value > 10_000_000_000 else value
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


def _next_fetch_delay(fetch_state: dict) -> float:
    """根据平台返回的收件冷却时间，避免重复触发 429。"""
    raw = fetch_state.get("nextFetchAllowedAt")
    if raw is None or raw == "":
        return 0.0
    timestamp = _timestamp({"receivedAt": raw})
    if timestamp is None:
        return 0.0
    return max(0.0, timestamp - time.time())


def _finalize_received_otp(account: RemailAccount, otp: str) -> str:
    """取码成功后刷新长效订单时间；刷新失败不影响验证码交付。"""
    if account.is_long_lived:
        try:
            refresh_account_context(account.email)
        except Exception as exc:
            logger.warning(
                "[Remail] 取码成功，但刷新长效订单时间失败（不影响验证码使用）: email=%s error=%s",
                account.email,
                f"{type(exc).__name__}: {str(exc)[:180]}",
            )
    return otp


def _otp_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "from": item.get("sender") or item.get("from") or "",
        "subject": item.get("subject") or "",
        "text": item.get("bodyPreview") or item.get("text") or "",
        "body": item.get("body") or "",
    }


def _verification_code(item: dict) -> str | None:
    direct = str(item.get("verificationCode") or "").strip()
    if re.fullmatch(r"\d{6}", direct):
        return direct
    return extract_otp(_otp_item(item))


def _pickup(account: RemailAccount) -> dict:
    payload = _request(
        "GET",
        "/v1/pickup",
        params={"email": account.email, "token": account.service_token},
        authenticated=False,
    )
    for container in _container_candidates(payload):
        if isinstance(container.get("items"), list):
            return container
    raise RemailError("Remail 取件响应缺少 items 数组")


def _pickup_status_from_error(exc: Exception) -> str:
    text = str(exc or "").strip().lower()
    if (
        "credential_invalid" in text
        or "credential is invalid" in text
        or "取件链接无效" in text
        or "凭证无效" in text
        or "http 401" in text
        or "http 403" in text
    ):
        return "credential_invalid"
    if (
        "order_unavailable" in text
        or "order is unavailable" in text
        or "订单当前不可" in text
        or "http 404" in text
        or "http 409" in text
    ):
        return "order_unavailable"
    if "rate_limited" in text or "http 429" in text or "请求过于频繁" in text:
        return "rate_limited"
    if (
        "service_unavailable" in text
        or "service is temporarily unavailable" in text
        or "http 503" in text
        or "服务暂不可用" in text
    ):
        return "service_unavailable"
    if "invalid_request" in text or "invalid request" in text or "http 400" in text or "http 422" in text:
        return "invalid_request"
    if "网络异常" in text or "网络连接失败" in text:
        return "service_unavailable"
    return "internal_error"


def _pickup_payload_has_items(payload) -> bool:
    return any(
        isinstance(container.get("items"), list)
        for container in _container_candidates(payload)
    )


def _pickup_batch_items(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def check_pickup_statuses(emails) -> dict[str, dict]:
    """批量检测长效邮箱取件凭证；只返回状态，不返回 serviceToken 或邮件内容。"""
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    results: dict[str, dict] = {}
    accounts: list[RemailAccount] = []
    seen: set[str] = set()
    for raw_email in emails or []:
        email = str(raw_email or "").strip()
        key = _cache_key(email)
        if not key or key in seen:
            continue
        seen.add(key)
        account = get_account_context(email)
        if account is None or not account.is_long_lived or not account.service_token:
            results[key] = {
                "status": "credential_invalid",
                "message": "缺少长效邮箱取件凭证",
                "checked_at": checked_at,
            }
            continue
        accounts.append(account)

    if not accounts:
        return results

    if len(accounts) == 1:
        account = accounts[0]
        key = _cache_key(account.email)
        try:
            payload = _request(
                "GET",
                "/v1/pickup",
                params={"email": account.email, "token": account.service_token},
                authenticated=False,
                max_attempts=1,
            )
            if not _pickup_payload_has_items(payload):
                raise RemailError("Remail 取件响应缺少 items 数组")
            results[key] = {
                "status": "available",
                "message": "取件凭证有效",
                "checked_at": checked_at,
            }
        except Exception as exc:
            results[key] = {
                "status": _pickup_status_from_error(exc),
                "message": str(exc)[:240],
                "checked_at": checked_at,
            }
        return results

    try:
        payload = _request(
            "POST",
            "/v1/pickup/batch",
            json={
                "items": [
                    {"email": account.email, "token": account.service_token}
                    for account in accounts
                ]
            },
            authenticated=False,
            max_attempts=1,
        )
    except Exception as exc:
        status = _pickup_status_from_error(exc)
        for account in accounts:
            results[_cache_key(account.email)] = {
                "status": status,
                "message": str(exc)[:240],
                "checked_at": checked_at,
            }
        return results

    items_by_index = {
        int(item.get("index")): item
        for item in _pickup_batch_items(payload)
        if isinstance(item.get("index"), int)
    }
    allowed_errors = {
        "credential_invalid",
        "order_unavailable",
        "service_unavailable",
        "rate_limited",
        "internal_error",
        "invalid_request",
    }
    for index, account in enumerate(accounts):
        item = items_by_index.get(index) or {}
        key = _cache_key(account.email)
        if str(item.get("status") or "").lower() == "succeeded":
            results[key] = {
                "status": "available",
                "message": "取件凭证有效",
                "checked_at": checked_at,
            }
            continue
        error = item.get("error") if isinstance(item.get("error"), dict) else {}
        code = str(error.get("code") or "internal_error").strip().lower()
        results[key] = {
            "status": code if code in allowed_errors else "internal_error",
            "message": str(error.get("message") or code or "取件检测失败")[:240],
            "checked_at": checked_at,
        }
    return results


def _message_detail(account: RemailAccount, message_id) -> dict:
    payload = _request(
        "GET",
        f"/v1/pickup/messages/{quote(str(message_id), safe='')}",
        params={"email": account.email, "token": account.service_token},
        authenticated=False,
    )
    for container in _container_candidates(payload):
        detail = container.get("message")
        if isinstance(detail, dict):
            return detail
        if container.get("id") is not None or container.get("body") is not None:
            return container
    raise RemailError(f"Remail 邮件详情响应无效: message_id={message_id}")


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """轮询 Remail 取件接口，返回领取时间后最新的 OpenAI 六位验证码。"""
    target = str(email or "").strip()
    account = get_account_context(target)
    if account is None:
        raise RemailError(
            "Remail 取码上下文不存在（缺少订单 serviceToken）；请在领取邮箱的同一运行进程中完成注册。"
        )

    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + max(0, wait_seconds)
    best_otp: str | None = None
    best_timestamp = float("-inf")
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 验证码"

    logger.info("[Remail] 开始轮询邮箱 %s，最长 %ss", target, wait_seconds)
    while time.monotonic() <= deadline:
        if best_otp and settle_until is not None and time.monotonic() >= settle_until:
            return _finalize_received_otp(account, best_otp)

        next_delay = float(interval)
        try:
            pickup = _pickup(account)
            items = pickup.get("items")
            if not isinstance(items, list):
                raise RemailError("Remail 取件响应缺少 items 数组")

            sortable = sorted(
                (item for item in items if isinstance(item, dict)),
                key=lambda item: _timestamp(item) or float("-inf"),
                reverse=True,
            )
            for summary in sortable:
                message_time = _timestamp(summary)
                if after_ts is not None and message_time is not None and message_time < after_ts - 30:
                    continue

                # Remail 的 pickup 结果已经按服务凭证/项目邮件规则过滤，
                # 不再强制要求发件人包含 openai；微软安全邮件常用独立域名。
                otp = _verification_code(summary)
                detail = summary
                if not otp and summary.get("id") is not None:
                    detail = _message_detail(account, summary.get("id"))
                    otp = _verification_code(detail)
                if not otp:
                    continue

                candidate_time = _timestamp(detail)
                candidate_time = message_time if candidate_time is None else candidate_time
                candidate_time = float("-inf") if candidate_time is None else candidate_time
                is_newer_message = candidate_time > best_timestamp
                is_updated_code = candidate_time == best_timestamp and otp != best_otp
                if best_otp is None or is_newer_message or is_updated_code:
                    best_otp = otp
                    best_timestamp = candidate_time
                    settle_until = time.monotonic() + settle
                    logger.info("[Remail] 锁定 OTP 候选，等待 %ss 确认", settle)

            fetch_state = pickup.get("fetch")
            if isinstance(fetch_state, dict):
                if fetch_state.get("lastSafeError"):
                    last_error = str(fetch_state.get("lastSafeError"))
                next_delay = max(next_delay, _next_fetch_delay(fetch_state))

            now = time.monotonic()
            if best_otp and settle_until is not None and now >= settle_until:
                return _finalize_received_otp(account, best_otp)
        except RemailError as exc:
            last_error = str(exc)
            next_delay = max(next_delay, float(exc.retry_after or 0))

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if best_otp and settle_until is not None:
            settle_remaining = settle_until - time.monotonic()
            if settle_remaining <= 0:
                return _finalize_received_otp(account, best_otp)
            next_delay = min(next_delay, settle_remaining)
        time.sleep(min(next_delay, remaining))

    if best_otp:
        return _finalize_received_otp(account, best_otp)
    raise RemailError(f"等待 Remail 验证码超时: {target}; {last_error}")
