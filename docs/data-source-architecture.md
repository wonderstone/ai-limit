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
| Codex / ChatGPT | Browser cookie for `chatgpt.com`, then web access token | `https://chatgpt.com/backend-api/codex/usage` | JSON | No |
| Gemini App | Chrome Google cookies plus page request tokens | `https://gemini.google.com/_/BardChatUi/data/batchexecute` | batchexecute JSON frames | No |
| Google Code Assist / Gemini CLI | `~/.gemini/oauth_creds.json` | `https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` | JSON | No |
| Antigravity | Running Antigravity local sidecar plus local CSRF token | `https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary` | gRPC-web JSON | No |
| Antigravity fallback | `agy` CLI and local logs | `agy /usage`, `~/.gemini/antigravity-cli/log` | CLI text / log text | No |

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

## Maintenance Rules

- Prefer official APIs when they exist.
- Prefer read-only local app/sidecar interfaces over scraping rendered UI.
- Prefer browser cookie + narrow HTTP request over background browser automation.
- Never copy a user's browser profile for quota collection.
- Never launch browser windows in the background to scrape usage.
- Preserve clear fallback ordering and document known side effects.
- Treat all internal endpoints as unstable: fail gracefully and show the user what login or app state is required.
