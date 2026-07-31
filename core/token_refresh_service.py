# -*- coding: utf-8 -*-
"""账号 accessToken 批量刷新调度。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core import db
from core.roxy_token_refresh import run_roxy_token_refresh

logger = logging.getLogger(__name__)


def _run_one(item: dict, batch_id: str, index: int, total: int) -> dict:
    acc_id = int(item["id"])
    email = str(item.get("email") or "").strip()
    if not db.mark_account_token_refresh_running(acc_id):
        return {"ok": False, "status": "skipped", "id": acc_id, "email": email, "error": "账号已删除或任务状态已被重置"}
    logger.info("[Token刷新] 批量任务 %s %s/%s：%s", batch_id, index, total, email)
    try:
        result = run_roxy_token_refresh(email=email, password=str(item.get("password") or ""))
        if not isinstance(result, dict):
            result = {"ok": False, "error": "Token 刷新器返回了无效结果"}
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    db.complete_account_token_refresh(acc_id, result)
    logger.info("[Token刷新] 批量任务 %s 完成：%s ok=%s", batch_id, email, bool(result.get("ok")))
    return {"id": acc_id, "email": email, **result}


def _run_batch(items: list[dict], workers: int, batch_id: str) -> None:
    logger.info("[Token刷新] 启动批量任务：batch=%s count=%s workers=%s", batch_id, len(items), workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"token-refresh-{batch_id}") as executor:
        futures = [
            executor.submit(_run_one, item, batch_id, index, len(items))
            for index, item in enumerate(items, 1)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                logger.exception("[Token刷新] 批量子任务异常：batch=%s", batch_id)
    logger.info("[Token刷新] 批量任务完成：batch=%s", batch_id)


def start_batch(items: list[dict], workers: int) -> dict:
    """异步启动一批已 claim 的账号；items 不会被写入磁盘。"""
    if not items:
        raise ValueError("Token 刷新任务为空")
    workers = max(1, min(16, int(workers)))
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        threading.Thread(
            target=_run_batch,
            args=(list(items), workers, batch_id),
            name=f"token-refresh-dispatch-{batch_id}",
            daemon=True,
        ).start()
    except Exception as exc:
        for item in items:
            db.complete_account_token_refresh(int(item["id"]), {"ok": False, "error": f"批量任务启动失败: {exc}"})
        raise
    return {"batch_id": batch_id, "workers": workers, "count": len(items)}
