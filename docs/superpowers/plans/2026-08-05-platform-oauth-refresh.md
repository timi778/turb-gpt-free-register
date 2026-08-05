# Platform OAuth Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Preserve the first Platform OAuth RT result on registration jobs and add safe current-RT status plus bulk OAuth refresh and immediate Codex/chatgpt2api synchronization to account management.

**Architecture:** Registration workers persist a token-free OAuth result snapshot on each job. A dedicated refresh service performs the refresh-token grant, asks the JSON persistence layer to merge rotated credentials atomically, writes a Codex credential atomically, and uploads the full Codex account to chatgpt2api. Web APIs expose only status metadata for this workflow, while the WebUI keeps immutable registration history separate from current account state.

**Tech Stack:** Python 3, Flask, requests, JSON file persistence, `ThreadPoolExecutor`, vanilla HTML/CSS/JavaScript, unittest/pytest.

---

### Task 1: Persist the original registration OAuth result

**Files:**
- Modify: `core/db.py:2038-2065`
- Modify: `core/registration_service.py:304-404`
- Modify: `webui/app.py:1370-1379`
- Test: `tests/test_platform_oauth_job_status.py`

- [x] **Step 1: Write failing snapshot tests**

Create tests for a token-free mapping helper and job serializer. The assertions must cover `success`, `partial` without RT as `missing`, `failed`, `skipped`, missing result as `not_reached`, and completed legacy jobs as `unknown`.

```python
def test_platform_oauth_snapshot_never_copies_tokens():
    snapshot = registration_service.platform_oauth_job_snapshot({
        "status": "success",
        "has_refresh_token": True,
        "refresh_token": "secret-rt",
        "access_token": "secret-at",
        "message": "已获取 Platform AT/RT",
    })
    assert snapshot["platform_oauth_status"] == "success"
    assert snapshot["platform_oauth_has_refresh_token"] is True
    assert "secret" not in repr(snapshot)
```

- [x] **Step 2: Run the new tests and verify they fail**

Run: `pytest -q tests/test_platform_oauth_job_status.py`

Expected: failure because `platform_oauth_job_snapshot` and the new job fields do not exist.

- [x] **Step 3: Add an allowlisted job snapshot update**

Extend `db.update_job` with `platform_oauth_snapshot: dict | None = None`. Copy only these keys into the task row:

```python
for key in (
    "platform_oauth_status",
    "platform_oauth_has_refresh_token",
    "platform_oauth_message",
    "platform_oauth_completed_at",
):
    if key in platform_oauth_snapshot:
        row[key] = platform_oauth_snapshot[key]
```

Add `registration_service.platform_oauth_job_snapshot(result, completed_at=None)`. Map the result status to the fixed history enum, truncate the message, and never copy credential values.

- [x] **Step 4: Save the snapshot on every terminal job path**

When `run_registration` returns, pass its `platform_oauth` result to the helper for success, failed, and stopped outcomes. Exception paths that have no registration result use `not_reached`. Do not alter retry/Codex task semantics.

- [x] **Step 5: Return safe history state from `/api/jobs`**

For jobs with no snapshot, set `platform_oauth_status` to `waiting` while the task is pending/running/stopping and `unknown` after a terminal state. Do not infer old history from `account_id`.

- [x] **Step 6: Run the snapshot tests**

Run: `pytest -q tests/test_platform_oauth_job_status.py tests/test_platform_oauth.py`

Expected: all tests pass.

- [x] **Step 7: Commit the history slice**

```powershell
git add core/db.py core/registration_service.py webui/app.py tests/test_platform_oauth_job_status.py
git commit -m "feat: preserve registration OAuth RT history"
```

### Task 2: Add current account OAuth state persistence

**Files:**
- Modify: `core/db.py:537-584`
- Modify: `core/db.py:875-975`
- Test: `tests/test_platform_oauth_refresh.py`

- [x] **Step 1: Write failing account-state tests**

Use temporary account JSON storage or patch `_load_accounts`/`_save_accounts`. Cover safe decoration, active-claim exclusion, missing RT, stale claims, successful token merge, omitted-token preservation, and failure preservation.

```python
def test_complete_refresh_preserves_omitted_refresh_token():
    assert db.claim_account_platform_oauth_refresh(7, trigger="manual_bulk") is True
    assert db.mark_account_platform_oauth_refresh_running(7) is True
    assert db.complete_account_platform_oauth_refresh(7, {
        "ok": True,
        "tokens": {"access_token": "new-at", "expires_in": 3600},
        "message": "OAuth Token 刷新成功",
    }) is True
    oauth = json.loads(saved_row["extra_json"])["platform_oauth"]
    assert oauth["access_token"] == "new-at"
    assert oauth["refresh_token"] == "old-rt"
```

- [x] **Step 2: Run the tests and verify they fail**

Run: `pytest -q tests/test_platform_oauth_refresh.py -k "account or complete or claim"`

Expected: missing persistence helpers and safe account fields.

- [x] **Step 3: Implement OAuth JSON helpers and safe decoration**

Add private parse/write helpers that preserve unrelated `extra_json` keys. `_decorate_account` exposes only:

```python
out["platform_oauth_has_refresh_token"] = bool(oauth.get("refresh_token"))
out["platform_oauth_refresh_status"] = oauth.get("refresh_status") or "never"
out["platform_oauth_refresh_message"] = oauth.get("refresh_message") or ""
out["platform_oauth_refreshed_at"] = oauth.get("refreshed_at") or ""
out["platform_oauth_upload_status"] = oauth.get("upload_status") or ""
out["platform_oauth_upload_message"] = oauth.get("upload_message") or ""
```

Do not add raw Platform AT, RT, or ID Token fields to decorated output.

- [x] **Step 4: Implement claim, running, completion, and interrupted recovery helpers**

Add `claim_account_platform_oauth_refresh`, `mark_account_platform_oauth_refresh_running`, `complete_account_platform_oauth_refresh`, `update_account_platform_oauth_sync_result`, and `recover_interrupted_platform_oauth_refreshes`. Store execution metadata inside `extra_json.platform_oauth`; use the existing queue/running stale-time constants.

- [x] **Step 5: Run account-state tests**

Run: `pytest -q tests/test_platform_oauth_refresh.py -k "account or complete or claim"`

Expected: all selected tests pass.

### Task 3: Implement refresh exchange and credential synchronization

**Files:**
- Create: `core/platform_oauth_refresh_service.py`
- Modify: `core/codex_oauth.py:992-1003`
- Test: `tests/test_platform_oauth_refresh.py`

- [x] **Step 1: Write failing exchange and synchronization tests**

Mock `requests.Session.post`, Codex persistence, and `chatgpt2api_client.upload_account`. Cover a rotated RT, a response without RT, `invalid_grant`, timeout, malformed JSON, no automatic exchange retry, Codex file failure, upload failure, and one-account failure not stopping a batch.

```python
def test_exchange_posts_refresh_grant_once(mock_post):
    mock_post.return_value = FakeResponse(200, {
        "access_token": "new-at",
        "refresh_token": "new-rt",
        "id_token": "new-id",
        "expires_in": 3600,
    })
    result = platform_oauth_refresh_service.exchange_refresh_token("old-rt")
    assert result["refresh_token"] == "new-rt"
    assert mock_post.call_count == 1
    assert mock_post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
```

- [x] **Step 2: Run service tests and verify they fail**

Run: `pytest -q tests/test_platform_oauth_refresh.py -k "exchange or upload or codex or batch"`

Expected: module or functions are missing.

- [x] **Step 3: Implement a single-attempt refresh exchange**

`exchange_refresh_token` posts form data to `PLATFORM_TOKEN_URL` with `PLATFORM_CLIENT_ID`, a finite timeout, and no retry adapter. It validates a 2xx JSON response with an access token and raises a safe error containing the OAuth error code but no submitted token.

- [x] **Step 4: Implement per-account orchestration**

The worker marks the account running, exchanges the current RT, merges omitted values, persists the account, builds a Codex storage object from the merged credentials, writes it, and uploads with:

```python
chatgpt2api_client.upload_account(
    account.get("access_token") or "",
    platform_oauth=merged_oauth,
    email=account.get("email") or "",
    password=account.get("registration_password") or "",
)
```

Persist Codex-file and upload results separately. A failed upload must not roll back the OAuth result.

- [x] **Step 5: Make Codex credential writes atomic**

Write JSON to a sibling temporary file, then replace the destination. Always remove a leftover temporary file in `finally`. Preserve the existing filename rules and return value.

- [x] **Step 6: Implement bounded asynchronous batches**

`start_batch(items, workers=3)` caps workers at 3, creates a daemon dispatcher, and catches each future independently. Startup failure completes all claimed accounts as failed.

- [x] **Step 7: Run service tests**

Run: `pytest -q tests/test_platform_oauth_refresh.py tests/test_chatgpt2api_integration.py tests/test_platform_oauth.py`

Expected: all tests pass.

- [x] **Step 8: Commit the backend refresh slice**

```powershell
git add core/db.py core/platform_oauth_refresh_service.py core/codex_oauth.py tests/test_platform_oauth_refresh.py
git commit -m "feat: refresh platform OAuth credentials"
```

### Task 4: Add safe Web API endpoints

**Files:**
- Modify: `webui/app.py:100-140`
- Modify: `webui/app.py:229-290`
- Modify: `core/db.py:1235-1270`
- Test: `tests/test_platform_oauth_refresh.py`

- [x] **Step 1: Write failing Flask endpoint tests**

Cover empty and oversized ID lists, invalid IDs, missing accounts, no RT, active refresh, accepted accounts, a hard worker cap of 3, `202` responses, and a status response that contains no raw token values.

```python
def test_oauth_refresh_bulk_never_returns_tokens(client):
    response = client.post("/api/accounts/oauth-refresh-bulk", json={"account_ids": [7]})
    assert response.status_code == 202
    body = response.get_json()
    assert "old-rt" not in json.dumps(body)
    assert "old-at" not in json.dumps(body)
```

- [x] **Step 2: Run endpoint tests and verify they fail**

Run: `pytest -q tests/test_platform_oauth_refresh.py -k "api or endpoint"`

Expected: endpoints return 404.

- [x] **Step 3: Add the bulk start endpoint**

Add `POST /api/accounts/oauth-refresh-bulk`. Deduplicate IDs, cap the request at 500 accounts, skip accounts without a current RT, claim accepted accounts atomically, and always call the service with `workers=3` regardless of untrusted client input.

- [x] **Step 4: Add the safe status endpoint**

Add `GET /api/accounts/oauth-refresh-status?limit=2000`, backed by an allowlisted DB status snapshot. Fields include identity, current RT presence, refresh state/message/timestamps, and upload state/message only.

- [x] **Step 5: Recover interrupted OAuth batches on app startup**

Invoke `recover_interrupted_platform_oauth_refreshes` next to the existing interrupted task recovery so queued/running rows become failed after a WebUI restart.

- [x] **Step 6: Run endpoint tests**

Run: `pytest -q tests/test_platform_oauth_refresh.py -k "api or endpoint or recover"`

Expected: all selected tests pass.

### Task 5: Render immutable job history and current account state

**Files:**
- Modify: `webui/templates/index.html:400-455`
- Modify: `webui/templates/index.html:794-826`
- Modify: `webui/templates/index.html:1151-1165`
- Modify: `webui/templates/index.html:1488-1535`
- Modify: `webui/templates/index.html:1686-1705`
- Modify: `webui/templates/index.html:2062-2092`
- Test: `tests/test_platform_oauth_webui.py`

- [x] **Step 1: Write failing template contract tests**

Assert the template contains the `首次 RT` and `当前 RT` columns, all agreed status labels, `btnRefreshSelectedOAuth`, the bulk endpoint, status endpoint, and selection-state wiring.

- [x] **Step 2: Run the WebUI tests and verify they fail**

Run: `pytest -q tests/test_platform_oauth_webui.py`

Expected: required labels and controls are absent.

- [x] **Step 3: Add the registration-history column**

Render `waiting` as `等待中`, `success` as `已获取`, `missing` as `未返回`, `failed` as `OAuth 失败`, `skipped` as `已跳过`, `not_reached` as `未执行`, and `unknown` as `未知`. Update empty-row `colspan`.

- [x] **Step 4: Add current RT account status**

Merge the safe OAuth status snapshot into `ACCOUNTS` during polling. Render `刷新中` for queued/running, `刷新失败` for failed, otherwise `有 RT` or `无 RT`. Put only the safe message and timestamp in the title.

- [x] **Step 5: Add the bulk action**

Add the `刷新 OAuth` toolbar button, disable it with no selection, submit selected IDs, report started/skipped counts, reload account state, and keep polling while any selected account is queued/running.

- [x] **Step 6: Run WebUI and API tests**

Run: `pytest -q tests/test_platform_oauth_webui.py tests/test_platform_oauth_refresh.py`

Expected: all tests pass.

- [x] **Step 7: Commit the WebUI slice**

```powershell
git add webui/app.py webui/templates/index.html tests/test_platform_oauth_webui.py tests/test_platform_oauth_refresh.py
git commit -m "feat: expose OAuth RT history and refresh controls"
```

### Task 6: Documentation, regression verification, and PR

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-05-platform-oauth-refresh-design.md`
- Modify: `docs/superpowers/plans/2026-08-05-platform-oauth-refresh.md`

- [x] **Step 1: Document the user-visible behavior**

Describe the separate meanings of task `首次 RT` and account `当前 RT`, the selected-account bulk action, no-browser `invalid_grant` behavior, immediate chatgpt2api synchronization, and the fact that old jobs show `未知`.

- [x] **Step 2: Run focused regression tests**

Run:

```powershell
pytest -q tests/test_platform_oauth_job_status.py tests/test_platform_oauth_refresh.py tests/test_platform_oauth_webui.py tests/test_platform_oauth.py tests/test_chatgpt2api_integration.py tests/test_token_refresh.py
```

Expected: all tests pass.

- [x] **Step 3: Run the full project checks**

Run:

```powershell
pytest
ruff check .
```

Expected: both commands exit 0. Report any unavailable or confirmed pre-existing failures separately.

- [x] **Step 4: Perform a credential-leak audit**

Inspect staged diffs and test output. Confirm no `.env`, account JSON, task JSON, Codex credential, token, password, cookie, browser profile, or runtime log is staged. Search new API serializers and logs for raw credential output.

- [x] **Step 5: Commit documentation and final adjustments**

```powershell
git add README.md docs/superpowers/specs/2026-08-05-platform-oauth-refresh-design.md docs/superpowers/plans/2026-08-05-platform-oauth-refresh.md
git commit -m "docs: explain platform OAuth refresh controls"
```

- [ ] **Step 6: Push and create or update the PR**

Push `codex/platform-oauth-http-exchange` to the configured fork remote. If PR #2 is still open, update it; otherwise create a new PR targeting `timi778/turb-gpt-free-register` with the verified behavior and commands in the body.
