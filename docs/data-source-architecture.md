# Data Source Architecture

ai-limit is a read-only quota monitor. Its preferred data path is always:

1. Reuse authentication state that already exists on the local machine.
2. Send the same narrow usage/quota request that the vendor UI or local app already uses.
3. Normalize the response into a small, display-oriented quota model.

It should not automate browser windows in the background. A browser may be opened only by an explicit user action, such as a menu item that helps the user sign in or inspect the vendor's own usage page.

## Provider Summary

| Provider | Local proof of identity | Request target | Primary payload | Background browser automation |
| --- | --- | --- | --- | --- |
| Claude | Browser cookie for `claude.ai` | `https://claude.ai/api/organizations/{orgId}/usage` | JSON | No |
| Claude Platform API credit | Browser cookie for `platform.claude.com` (`sessionKey`) | `https://platform.claude.com/api/organizations/{orgId}/prepaid/credits` | JSON | No |
| Codex / ChatGPT | Browser cookie for `chatgpt.com`, then web access token | `https://chatgpt.com/backend-api/codex/usage` | JSON | No |
| Gemini App | Chrome Google cookies plus page request tokens | `https://gemini.google.com/_/BardChatUi/data/batchexecute` | batchexecute JSON frames | No |
| Google Code Assist / Gemini CLI | `~/.gemini/oauth_creds.json` | `https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` | JSON | No |
| Antigravity | Running Antigravity local sidecar plus local CSRF token | `https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary` | gRPC-web JSON | No |
| Antigravity fallback | `agy` CLI and local logs | `agy /usage`, `~/.gemini/antigravity-cli/log` | CLI text / log text | No |
| GitHub Copilot | GitHub OAuth/PAT token from env, `copilot-cli` Keychain entry, or `gh auth token` | `https://api.github.com/copilot_internal/user` | JSON (`quota_snapshots`) | No |
| Grok | Browser cookie for `grok.com` (`sso` / `sso-rw`), searched across every Chrome profile | `https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig` | gRPC-web binary protobuf | No |

## Shared Browser-Session Pattern

Claude, Codex, and Gemini App are consumer/web products. Their usage pages are not backed by public stable quota APIs, but the pages themselves still fetch quota data from internal endpoints.

ai-limit follows the same pattern for each:

1. Read browser cookies with `browser_cookie3`.
2. Build a normal HTTP request with the cookie header and browser-like request headers.
3. Fetch only the usage endpoint needed for quota display.
4. Parse and normalize the returned JSON-like payload.

This does not bypass authentication. If the browser is not signed in, the cookie is expired, or the account lacks access, the request fails.

## Authentication Materials

ai-limit should use the smallest existing local credential material needed to read quota. It should not persist copied credentials of its own.

| Material | Used for | Where ai-limit obtains it | How it is used | Stored by ai-limit |
| --- | --- | --- | --- | --- |
| Browser cookies | Claude, Codex / ChatGPT, Gemini App | `browser_cookie3` reading Chrome/Firefox cookies from the user's existing browser profile | Sent as the request `Cookie` header or attached to a Python cookie opener | No |
| `lastActiveOrg` cookie | Claude | `.claude.ai` cookie jar | Selects the Claude organization in `/api/organizations/{orgId}/usage` | No |
| ChatGPT web access token | Codex / ChatGPT | Response from `https://chatgpt.com/api/auth/session`, requested with ChatGPT cookies | Sent as `Authorization: Bearer ...` to `backend-api/codex/usage` | No |
| Gemini page tokens: `cfb2h`, `FdrFJe`, `SNlM0e` | Gemini App | HTML returned by `https://gemini.google.com/usage`, requested with Google cookies | Used as batchexecute query/body parameters | No |
| Google OAuth refresh/access token | Google Code Assist / Gemini CLI | `~/.gemini/oauth_creds.json` | Refreshes and calls `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` | No new copy; reads existing file |
| Antigravity CSRF token | Antigravity sidecar | Antigravity language-server process arguments | Sent as `x-codeium-csrf-token` to the local sidecar | No |
| grok.com cookies (`sso`, `sso-rw`, `cf_clearance`) | Grok | `browser_cookie3` reading each Chrome profile's cookie DB, then Firefox | Sent as the request `Cookie` header | No |
| GitHub token (`gho_` / `ghu_` / `github_pat_`) | GitHub Copilot | `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`, then the `copilot-cli` Keychain item, then `gh auth token` | Sent as `Authorization: token …` to `copilot_internal/user` | No |

Important distinctions:

- Cookies prove the user has an active browser session.
- Access tokens are often derived from cookies by a first-party session endpoint.
- CSRF/page tokens prove the request follows the expected app or page flow.
- OAuth tokens belong to CLI/app login state, not browser login state.

## Claude

Claude quota uses the active `claude.ai` browser session.

Flow:

1. Read Chrome or Firefox cookies for `.claude.ai`.
2. Extract `lastActiveOrg` from cookies.
3. Request `https://claude.ai/api/organizations/{orgId}/usage`.
4. Optionally request `https://claude.ai/api/organizations/{orgId}` for plan metadata.

Risk profile:

- Unofficial internal web endpoint.
- Depends on `lastActiveOrg` and Claude's current response shape.
- No background browser launch.

### Claude Platform API credit

The settings/billing page of the Claude Platform console shows a prepaid API credit balance; Max plans include a monthly scoped API credit there. The console keeps its own `sessionKey` on `.platform.claude.com`, separate from claude.ai.

Flow:

1. Read Chrome or Firefox cookies for `platform.claude.com`. No `sessionKey` means no console sign-in, which is treated as "nothing to show", not as an error.
2. `GET https://platform.claude.com/api/organizations`. The same sign-in lists the claude.ai subscription org (`billing_type: stripe_subscription`) and the API org (`billing_type: prepaid`, capability `api`); only the API org carries a credit balance.
3. `GET /api/organizations/{orgId}/prepaid/credits`.
4. Normalize: `balance.money` or `balance.credits` (`{amount_minor, exponent}`) is the balance; the sum of `tranches[] + promo_tranches[]` `granted` amounts is the grant used for the percentage; `next_expires_at` is the expiry.

Risk profile:

- Unofficial internal console endpoint; the money shape has already moved from a bare `amount` in cents to `{amount_minor, exponent}`, and the legacy `amount` is still read as a fallback.
- Optional: a failure leaves `api_credit` empty instead of hiding the chat quota.
- No background browser launch.

#### `claude.api_credit` in the menu bar cache (consumer contract)

Since 0.3.48 the balance is fetched by the account-bound api-credit/v1 path (`ai_limit/api_credits.py`), separately from Claude quota. Other tools still read the legacy field from `~/.ai-limit-menubar-cache.json` — InfoWeave's Anthropic budget sums the `subscription_api_credit` tranches there — so the cache must keep carrying it:

- The menu bar writes `claude.api_credit` through `claude_api_credit_legacy()`, the same adapter the local API uses for its own `api_credit` field.
- Shape: `org_id`, `org_name`, `currency`, `balance`, `granted`, `remaining_percent`, `expires_at`, `tranches[]` (`name`, `program_id`, `granted`, `remaining`, `expires_at`, `granted_at`, …), `auto_reload`, `source`, plus `observed_at` (when the balance was read) and `status` (added in 0.3.51).
- Only a fresh `ok` read is written. A stale or unbound record writes `null`, because consumers that date the reading by the file's `cached_at` would otherwise treat a retained balance as newly observed. `observed_at` can trail `cached_at` by up to the five-minute api-credit cache TTL.
- 0.3.48 – 0.3.50 wrote `null` here unconditionally while the widget still showed the balance; `test_menubar_cache_api_credit.py` guards against that regression.

## Codex / ChatGPT

Codex web quota uses the active `chatgpt.com` browser session.

Flow:

1. Read Chrome or Firefox cookies for `.chatgpt.com`.
2. Request `https://chatgpt.com/api/auth/session`.
3. Extract the web access token from the session response.
4. Request `https://chatgpt.com/backend-api/codex/usage` with cookies and bearer token.
5. Normalize the primary and weekly rate-limit windows.

Fallback:

- `codex app-server` WebSocket can read rate limits through `account/rateLimits/read`.
- This fallback has a known side effect: initialization may start a new Codex 5-hour window. It must remain clearly documented and should not be treated as equivalent to the web usage endpoint.

Risk profile:

- Unofficial internal web endpoint.
- Requires an active ChatGPT browser session and Codex entitlement.
- The app-server fallback is not side-effect free.

## Gemini App

### Subscription visibility (0.3.41)

The `jSf9Qc` GetUsageInfo response's first field identifies the current tier:
`1=Free`, `2=Pro`, `3=Ultra`, `4=Plus`, `6=Ultra`. This mapping is corroborated
by [Gemini-API's `_fetch_usage_info` implementation](https://github.com/HanaokaYuzu/Gemini-API/blob/master/src/gemini_webapi/client.py)
and the local free-account response. It is an internal, version-sensitive
contract, not a public Google billing API. Unrecognized tiers remain `unknown`.
The overage-preference boolean and zero usage are not subscription evidence.

Only an `active` entitlement is displayed. `inactive` (free tier) and `unknown`
states hide the quota, but do not disable polling or change enabled services.
Each fresh response re-evaluates entitlement, allowing automatic restoration
after resubscription. Gemini cache schema 2 expires after the configured TTL
(120 seconds by default); older cache schemas are rejected. Temporary failures
may serve a labelled stale snapshot for up to 30 minutes, not a cancellation
decision. AI-credit metrics are excluded from percentage windows.

Copilot uses the named `copilot_plan` field: known paid/organization/student
entitlements are active; `free`, `free_limited_copilot`, and no subscription
(HTTP 404) are inactive. Unknown plans stay unknown; HTTP 401/403 and network
failures remain errors. Both providers retain visibility while their current
paid entitlement is active, even if renewal was cancelled. No future billing
date is inferred. The UI checks subscription status on every refresh, and legacy
unverified snapshots cannot restore a quota display.

Gemini App quota uses the active Google browser session for `gemini.google.com`.

Flow:

1. Read Chrome cookies for `.google.com`.
2. Request `https://gemini.google.com/usage`.
3. Extract page request tokens such as `cfb2h`, `FdrFJe`, and `SNlM0e`.
4. Send batchexecute requests to `https://gemini.google.com/_/BardChatUi/data/batchexecute`.
5. Prefer the known quota RPC when it returns the expected payload.
6. Fall back to other batchexecute RPCs that expose model/quota-like payloads.
7. Normalize quota buckets and model entries.

Caching:

- A fresh snapshot is reused for `AI_LIMIT_GEMINI_APP_CACHE_TTL_SEC` (default 120s) to avoid hammering the endpoint on every 60s refresh.
- An older snapshot is served only when the live request fails, up to `AI_LIMIT_GEMINI_APP_CACHE_STALE_SEC` (default 30 min).
- Cached readings are labelled in `source` (`(cached Ns)` / `(stale cache Ns)`) and carry `cached`, `cache_age_seconds`, and `cache_stale`.
- The cache must never be a long-lived first data source: the 5-hour bucket can move tens of percent within one window, and a stale reading visibly disagrees with the vendor page.

Important boundary:

- ai-limit must not copy Chrome profiles, launch Chrome with remote debugging, or use CDP to scrape the DOM in the background.
- The menu item that opens `https://gemini.google.com/usage` is a user-directed sign-in/view action, not a data collection mechanism.

Risk profile:

- Unofficial internal Gemini App endpoint.
- Depends on page token names and RPC payload shape.
- Google can change the batchexecute RPC names or response layout.

## Google Code Assist / Gemini CLI

Google Code Assist quota is separate from Gemini App quota. It uses the local Gemini CLI OAuth state.

Flow:

1. Read local OAuth credentials from `~/.gemini/oauth_creds.json`.
2. Refresh the OAuth access token when needed.
3. Request `https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota`.
4. Normalize the returned quota buckets.

Risk profile:

- Uses a Google internal `v1internal` API.
- Depends on local CLI login state and OAuth scopes.
- Does not require a browser session.

## Antigravity

Antigravity is different from the browser-session providers. It exposes quota through a running local app/CLI sidecar.

Discovery flow:

1. Read the Antigravity DevTools port from the local app data directory.
2. Request the DevTools target list.
3. Find a local HTTPS origin whose URL/title indicates Antigravity sidecar content.
4. Read the local CSRF token from the Antigravity language-server process arguments.
5. Request:

```text
https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary
```

The request uses gRPC-web JSON framing:

```text
Content-Type: application/grpc-web+json
x-grpc-web: 1
x-codeium-csrf-token: <local csrf token>
```

Fallbacks:

1. Run `agy /usage` and parse its terminal output.
2. Parse recent `~/.gemini/antigravity-cli/log` quota and reset messages.
3. Use local cache when available.

Sidecar lifecycle:

- The sidecar is usually present only while Antigravity or `agy` is running.
- If Antigravity exits, the live sidecar endpoint may disappear.
- ai-limit must degrade to CLI/log/cache instead of launching Antigravity automatically.

Remote-server observation:

- Antigravity sidecar itself connects to Google endpoints such as `daily-cloudcode-pa.googleapis.com`.
- ai-limit should not depend on reverse engineering that remote sidecar-to-Google protocol. The local sidecar quota summary is the intended boundary for this tool.

## GitHub Copilot

Copilot quota uses the GitHub sign-in that Copilot CLI (or GitHub CLI) already holds on the machine. There is no public REST endpoint for a user's remaining quota; the public Billing / usage-metrics APIs report consumption after the fact and need org/enterprise permissions. ai-limit therefore reads the same internal endpoint the Copilot CLI footer uses.

Flow:

1. Resolve a token in Copilot CLI's documented order: `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`; then the `copilot-cli` macOS Keychain item written by `copilot login`; then `gh auth token`.
2. `GET https://api.github.com/copilot_internal/user` with `Authorization: token …`.
3. Read `quota_snapshots`. `premium_interactions` is the metered monthly bucket (AI credits, or premium requests on the legacy billing platform); `chat` and `completions` are `unlimited` on paid plans.
4. Normalize `percent_remaining`, `remaining`, `entitlement`, `credits_used`, and `quota_reset_date_utc` into the shared quota model. `copilot_plan` (for example `individual_max`) supplies the plan label; `token_based_billing` picks the wording.

Risk profile:

- Internal endpoint, not in the GitHub REST reference; field names may change.
- `401`/`403` mean the token expired or lacks Copilot access; `404` means the account has no Copilot subscription. Both are surfaced as sign-in guidance, not as a quota number.
- The reset is a fixed monthly boundary (first day of the month, 00:00 UTC), not a rolling window like Claude/Codex.
- Do not fall back to scraping github.com settings pages or to the Copilot SDK server mode from the menu bar app; the single narrow read above is the intended boundary.

## Grok

To pin Grok to a specific Chrome account, put its profile directory name (for example,
`Profile 1`) in `~/.config/ai-limit/grok-chrome-profile`. This affects only Grok.
When set, no other Chrome profile or Firefox session is used as a fallback.
Remove the file to restore automatic discovery. The file contains no credentials.

Grok quota uses the active `grok.com` browser session and reads the same RPC as grok.com Settings → Usage.

Flow:

1. Read `grok.com` cookies from each Chrome profile (`Default`, `Profile N`), then Firefox; the first jar holding `sso` / `sso-rw` wins. `browser_cookie3` alone only opens Chrome's Default profile, and grok.com is often signed in under another one.
2. `POST https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig` with `Content-Type: application/grpc-web+proto`, `X-Grpc-Web: 1`, and an empty request frame (`00 00 00 00 00`).
3. Unframe the gRPC-web reply (data frame + `0x80` trailer frame); a non-zero `grpc-status` is an error, `7`/`16` meaning the session is not accepted.
4. Decode `GetGrokCreditsConfigResponse.config` (`GrokCreditsConfig`) with a small built-in protobuf reader:
   - `1 credit_usage_percent` (float): share of the pool used; shown as `ceil(used)` like the page.
   - `8 current_period` (`type` 2 = weekly, 1 = monthly; `start`/`end` timestamps): `end` is the reset time.
   - `7 product_usage[]` (`billing_product.Product` enum + `usage_percent`): Chat / Imagine / Build / Voice / API / Grok Bot share.
   - `12 prepaid_balance`, `2 on_demand_cap`, `3 on_demand_used` (`prod_charger.Cent { int64 val }`): Extra Usage Credits.
   - `11 is_unified_billing_user`: false means the account is not on the shared pool; reported as an error instead of a number.

Field numbers come from the `grok_build_billing.proto` descriptor embedded in grok.com's own JS bundle.

Risk profile:

- Internal endpoint and protobuf schema; field numbers or the RPC name may change.
- Cloudflare fronts grok.com; the request relies on the browser's `cf_clearance` cookie and a browser-like User-Agent.
- Grok Bot appears only as a product share of the same pool here; it has no separate quota in this RPC.

## Maintenance Rules

- Prefer official APIs when they exist.
- Prefer read-only local app/sidecar interfaces over scraping rendered UI.
- Prefer browser cookie + narrow HTTP request over background browser automation.
- Never copy a user's browser profile for quota collection.
- Never launch browser windows in the background to scrape usage.
- Preserve clear fallback ordering and document known side effects.
- Treat all internal endpoints as unstable: fail gracefully and show the user what login or app state is required.
