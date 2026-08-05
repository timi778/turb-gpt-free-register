# Platform OAuth Refresh Design

## Background

The registration flow can already obtain Platform OAuth access, refresh, and ID tokens, save them in the account record, write a Codex credential file, and upload the account to chatgpt2api. The WebUI does not currently distinguish the refresh token captured during the original registration from the account's current refresh token, and the existing "update token" action refreshes the ChatGPT session token through Roxy rather than refreshing Platform OAuth credentials.

## Goals

- Preserve whether each registration task obtained a Platform OAuth refresh token during its original run.
- Show the current Platform OAuth refresh-token state in account management.
- Add a bulk "Refresh OAuth" action for selected accounts.
- Persist refreshed credentials, update the Codex credential file, and immediately synchronize each successfully refreshed account to chatgpt2api.
- Never return or log raw OAuth credentials in WebUI-facing paths.

## Non-goals

- Do not open a browser, perform a new authorization flow, or request an email OTP when a refresh token is invalid.
- Do not combine Platform OAuth refresh with the existing Roxy ChatGPT session-token refresh.
- Do not reconstruct historical OAuth results for old tasks from the account's current state.
- Do not change registration success semantics.

## Architecture

Introduce a dedicated Platform OAuth refresh service. It owns the refresh-token grant, credential persistence, Codex file update, and chatgpt2api synchronization. The service is independent from the existing Roxy token-refresh implementation.

Registration task completion stores an immutable snapshot of the original Platform OAuth result on the task. Account APIs derive current credential availability from the account record and expose only safe status metadata.

## Data Model

### Registration task snapshot

New task fields:

- `platform_oauth_status`: `success`, `missing`, `failed`, `skipped`, or `not_reached`.
- `platform_oauth_has_refresh_token`: whether the original registration returned a non-empty Platform refresh token.
- `platform_oauth_message`: a short result message that contains no credential data.
- `platform_oauth_completed_at`: completion time of the original Platform OAuth step.

These fields are written when the registration task finishes and are not modified by later account refreshes. Existing tasks without the fields are displayed as `unknown`.

### Current account state

`extra_json.platform_oauth` remains the source of truth for current Platform credentials:

- `access_token`
- `refresh_token`
- `id_token`

Refresh metadata is stored alongside the credentials:

- `refresh_status`: `queued`, `running`, `success`, `failed`, or `never`.
- `refresh_message`: a safe summary with no token content.
- `refreshed_at`: the most recent refresh completion time.
- `upload_status`: `success`, `failed`, or `skipped` for the most recent chatgpt2api synchronization.
- `upload_message`: a safe upload result summary.

If OpenAI returns a new refresh token, it replaces the old token. If the response omits a refresh token, the existing token is retained. Missing optional access or ID token values likewise do not erase existing values.

## Backend Flow

### Registration history

The registration worker reads the `platform_oauth` object returned by the registration driver and maps it to the task snapshot. A task that ends before Platform OAuth is reached is `not_reached`. No raw token is copied to the task record.

### Bulk refresh

1. The WebUI submits selected account IDs.
2. The backend validates and deduplicates the IDs and starts bounded background work.
3. An account without a current Platform refresh token is skipped.
4. The refresh service sends a refresh-token grant to `https://auth.openai.com/api/accounts/oauth/token` using the Platform OAuth client ID.
5. A valid response is merged with the existing credential set without clearing omitted token fields.
6. The account record is saved as the primary source of truth.
7. The Codex credential file is atomically updated.
8. The full Codex account is uploaded to chatgpt2api immediately for that account.
9. Safe per-account refresh and upload results are made available to the WebUI.

The batch does not wait for all accounts before synchronizing completed accounts. At most three accounts are refreshed concurrently to avoid a burst of OAuth and upload requests.

## API Surface

- The account list adds safe current OAuth fields such as `platform_oauth_has_refresh_token`, `platform_oauth_refresh_status`, `platform_oauth_refreshed_at`, and upload status fields.
- `POST /api/accounts/oauth-refresh-bulk` accepts a non-empty list of account IDs, enforces a maximum batch size, and returns safe started/skipped results.
- A status endpoint, following the existing background account-action pattern, returns safe per-account progress and completion results.
- The job list returns the immutable registration snapshot fields. Missing snapshot fields map to `unknown`.

No endpoint returns `access_token`, `refresh_token`, `id_token`, Codex file contents, or chatgpt2api credentials as part of this feature.

## WebUI

### Registration tasks

Add a `首次 RT` column with these labels:

- `已获取`
- `未返回`
- `OAuth 失败`
- `已跳过`
- `未执行`
- `未知` for old tasks

The displayed history does not change after an account OAuth refresh.

### Account management

Add a `当前 RT` column with `有 RT`, `无 RT`, `刷新中`, and `刷新失败` states. The latest safe result and timestamp are available as secondary text or a tooltip.

Add a bulk `刷新 OAuth` button to the existing account toolbar. It is disabled when no accounts are selected. Starting a batch refresh updates row state, and completion reports success, failure, skipped, and upload-failure counts without exposing credentials.

## Error Handling

- Do not automatically retry the OAuth token exchange. A server may rotate a refresh token even when the client times out before receiving the response.
- Network errors, timeouts, invalid responses, and `invalid_grant` preserve local credential values and record a failed refresh. They do not trigger browser authorization.
- If account persistence fails, stop before writing the Codex file or uploading.
- If the Codex file update fails after account persistence, report a partial failure without clearing the saved credentials.
- If chatgpt2api upload fails, keep the refreshed OAuth credentials and report upload failure separately. Upload failure does not roll back or invalidate the refresh result.
- One account failure does not stop the remaining batch.

## Security

- Token values are accepted only inside backend service boundaries.
- Logs include account identity and safe status only, never raw token values or credential payloads.
- API serializers use an explicit allowlist of safe OAuth status fields.
- Existing management authentication applies to the new endpoints.
- Local account files, Codex credential files, configuration secrets, and generated runtime data remain untracked.

## Verification

Targeted tests cover:

- New refresh token replacement and omitted refresh token preservation.
- Preservation of existing access and ID tokens when omitted.
- `invalid_grant`, timeout, malformed response, persistence failure, file failure, and upload failure.
- No automatic OAuth retry.
- Registration task snapshot mapping and `unknown` behavior for old tasks.
- Accounts without a refresh token being skipped.
- Per-account immediate chatgpt2api synchronization with the full Codex payload.
- Bulk API validation, bounded work, partial failure, and safe response serialization.
- WebUI labels, selection behavior, and refresh progress.
- Assertions that API responses and logs do not expose token values.

Before completion, run the directly related test set, full `pytest`, and `ruff check .`. Any unavailable or pre-existing failure must be reported separately.
