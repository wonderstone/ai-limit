# Browser key access diagnostics — 2026-10-21

## Observed

- The API was running and /health returned ready while the Claude endpoint returned a keychain-lock error.
- The menu-bar cache retained values with stale_reason=keychain_locked, explaining why it could appear normal.
- The existing provider mapped browser_cookie3's generic “Unable to get key for cookie decryption” marker directly to a locked keychain. That marker does not establish lock state: the dependency falls back to a dummy key after any failed security command.
- Before any application fix or server restart, a read-only Security.framework probe reported the default keychain unlocked, browser key access worked, and both foreground and background quota requests recovered.
- User reports a correlation with fingerprint screen unlock versus password unlock. This is a reproduction hypothesis, not a proven OS defect. An active browser can retain an authenticated session while an external background reader cannot obtain its key.

## Change

- Read lock state using SecKeychainCopyDefault / SecKeychainGetStatus. No secret retrieval, permission change, unlock operation or interactive prompt.
- Only confirmed default-keychain lock produces keychain_locked=true. Unlocked/unavailable/unsupported status produces browser_key_unavailable instead.
- Both categories can retain explicitly stale menu-bar values; UI/export language distinguishes them. A fresh response clears stale flags.
- Claude API failures carry error_code, browser_key_unavailable and verified keychain_locked. Each subsequent request retries normally; failed results are not converted into fresh quota.
- Version 0.3.42, build 344; health now exposes version/build.

## Verification and rollout

- Focused unittest suite: 61 tests, one skipped, passing. Covers locked/unlocked/unknown, framework failures, reference release, stale labeling and API recovery.
- API restarted through its existing LaunchAgent. Live /health: ready, version 0.3.42, build 344. Claude available=true with fresh quota.
- Rebuilt menu-bar app using the repository venv. Previous installed app retained at ~/.cache/ai-limit-backups/ai-limit-before-0.3.42.app; installed and launched 0.3.42.
- No credentials logged, no keychain access controls changed, no security protections disabled.

Limit: this fixes the application's unsupported lock diagnosis and recovery reporting; it does not claim to repair macOS fingerprint/keychain behavior. Reproduce a future lock/unlock incident to distinguish confirmed lock from item-level access failure.
