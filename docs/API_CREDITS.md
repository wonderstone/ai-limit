# API credit display

The main widget shows **promotional balance, purchased balance, and expiry**
for each API provider. Expiry dates use the Mac's local timezone. Subscription
usage remains separate. Account IDs, binding details, totals and observation
timestamps remain in `api-credit/v1` data, not the main window. Unknown amounts
are `null`/`Unknown`, never fabricated zeros; stale known values have a cached
marker in the provider heading.

## Local account binding and consent

`~/.config/ai-limit/api-credit-profiles.json` contains identity labels only.
`XDG_CONFIG_HOME` changes the config root. Profile names must match the local
browser label exactly, for example `Chrome/Default`.

```json
{
  "openai": {
    "profile": "Chrome/Default",
    "organization_id": "org-example",
    "organization_name": "Example organization",
    "reuse_platform_session": true
  },
  "claude": {
    "profile": "Chrome/Default",
    "organization_id": "example-organization-uuid",
    "organization_name": "Example API organization"
  }
}
```

Enable `reuse_platform_session` only with the user's authorization. It defaults
to false; revoking it changes the cache identity and immediately returns
unknown rather than reusing a prior balance. No credentials belong in this file.

## OpenAI Platform

The authorized reader opens the selected profile's existing LevelDB files in
read-only mode. It follows CURRENT/manifest live tables and current logs, reads
only the exact `https://platform.openai.com` keys `oai-auth:onboard` and
`oai/activeOrg`, and honors newer deletion records. It never copies a browser
profile, launches a browser, scrapes pages in the background, or writes tokens.

The existing access token is exchanged at
`https://api.openai.com/v1/dashboard/onboarding/login` using the Platform's
observed `{"app":"api"}` payload. The returned session key stays in memory.
The configured organization must match both the browser's active organization
and server-confirmed membership. Then a GET to
`https://api.openai.com/v1/dashboard/billing/credit_grants` includes that exact
organization header. Redirects are refused. No model/inference endpoint is used.

`total_paid_available` supplies purchased balance; the difference from the
explicit `total_available` supplies promotional balance. Known subscription
promotional grants supply expiry. If their remaining amounts do not account
for the promotional aggregate, expiry remains unknown rather than guessing
about an unclassified grant program. Future/scheduled and inactive grants are
not treated as active spendable grants. A missing or expired Platform session
requires signing in through the browser; the reader never refreshes or stores
refresh tokens itself.

## Claude Console

The exact selected browser profile and organization are enforced. Purchased
balance follows the Console's purchased-only figure from immutable chunk
`0ceool7nuv-pz.js`, with its active-tranche filter from `1167hl3e6f4nb.js`
(verified 2026-10-30). The calculation uses active, same-currency paid and
promotional tranches and the Console's one-minor-unit-per-tranche rounding
allowance. A rounding residue does not become purchased credit. Missing or
malformed inputs remain unknown.

## Refresh and resource bounds

The existing widget timer runs every 60 seconds. Successful API snapshots are
cached for 300 seconds, with two cache entries and bounded single-flight reads.
Failures back off for 60, 120, 240, 480, then at most 900 seconds. Manual refresh
bypasses the cache and retry delay and coalesces with an active refresh.
`observed_at`, `checked_at` and the provider's `source_updated_at` are distinct;
the latter remains unknown unless the provider supplies it.

Platform storage reads allow at most 64 active data files, 64 MiB total input,
250,000 records, 64 KiB target values, and 8 MiB decompressed blocks. HTTP
responses are capped at 1 MiB. Files changed during a read fail closed. The
vendored CCL helpers retain their MIT notices and have explicit block limits;
see `ai_limit/_vendor/NOTICE.txt` for pinned sources and local modifications.

## Offline preview

`.venv/bin/python preview_api_credit_widget.py` exports the actual widget rows
with explicitly synthetic data and no provider refresh. It does not prove a
live installed app's behavior.
