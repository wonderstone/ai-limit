"""Read the explicitly authorized Platform session; never persist credentials.

Only two exact localStorage keys from the selected profile/origin are decoded.
Use CURRENT/manifest live tables and current logs, retaining deletion markers.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ai_limit import providers
from ai_limit._vendor import ccl_leveldb

ORIGIN = "https://platform.openai.com"
API = "https://api.openai.com/v1/dashboard"
MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 64
MAX_RECORDS = 250_000
MAX_VALUE = 64 * 1024


class PlatformBalanceError(Exception):
    def __init__(self, code, *, mismatch=False):
        super().__init__(code)
        self.code, self.mismatch = code, mismatch


def _stamp(path):
    s = path.stat()
    return (s.st_ino, s.st_size, s.st_mtime_ns)


def _latest_values(records, targets):
    latest = {}
    started = time.monotonic()
    for index, record in enumerate(records):
        if index >= MAX_RECORDS or time.monotonic() - started > 8:
            raise PlatformBalanceError("browser_storage_limit")
        key = record.user_key
        if key in targets and (key not in latest or record.seq > latest[key].seq):
            if len(record.value) > MAX_VALUE:
                raise PlatformBalanceError("browser_storage_limit")
            latest[key] = record
    # A newer tombstone wins. Never fall back to an older signed-out session.
    return {key: r.value for key, r in latest.items() if r.state == ccl_leveldb.KeyState.Live}


def _decode(value):
    if not value or value[0] not in (0, 1):
        raise PlatformBalanceError("platform_session_unavailable")
    return value[1:].decode("utf-16-le" if value[0] == 0 else "latin1")


def read_platform_session(profile):
    matches = [path for label, path in providers._chromium_profile_cookie_files() if label == profile]
    if len(matches) != 1:
        raise PlatformBalanceError("platform_profile_unavailable")
    cookie = matches[0]
    root = (cookie.parent.parent if cookie.parent.name == "Network" else cookie.parent).resolve()
    directory = root / "Local Storage" / "leveldb"
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise PlatformBalanceError("browser_storage_path")
    current = directory / "CURRENT"
    if current.is_symlink() or current.stat().st_size > 128:
        raise PlatformBalanceError("browser_storage_path")
    manifest_name = current.read_text().strip()
    if not re.fullmatch(r"MANIFEST-[0-9]{6,}", manifest_name):
        raise PlatformBalanceError("browser_storage_manifest")
    manifest = directory / manifest_name
    if manifest.is_symlink() or manifest.stat().st_size > 2 * 1024 * 1024:
        raise PlatformBalanceError("browser_storage_limit")
    tables, log_number, previous_log = set(), None, 0
    before = {current: _stamp(current), manifest: _stamp(manifest)}
    reader = ccl_leveldb.ManifestFile(manifest)
    try:
        for edit in reader:
            for item in edit.deleted_files:
                tables.discard(item.file_no)
            tables.update(item.file_no for item in edit.new_files)
            if edit.log_number is not None:
                log_number = edit.log_number
            if edit.prev_log_number is not None:
                previous_log = edit.prev_log_number
            if len(tables) > MAX_FILES:
                raise PlatformBalanceError("browser_storage_limit")
        if reader.damage or log_number is None:
            raise PlatformBalanceError("browser_storage_manifest")
    finally:
        reader.close()
    files = []
    for number in tables:
        candidates = [directory / f"{number:06d}{suffix}" for suffix in (".ldb", ".sst")]
        found = [p for p in candidates if p.exists()]
        if len(found) != 1:
            raise PlatformBalanceError("browser_storage_changed")
        files.extend(found)
    for i, path in enumerate(directory.iterdir()):
        if i >= 256:
            raise PlatformBalanceError("browser_storage_limit")
        if re.fullmatch(r"[0-9]{6,}\.log", path.name):
            number = int(path.stem)
            if number >= log_number or number == previous_log:
                files.append(path)
    if len(files) > MAX_FILES:
        raise PlatformBalanceError("browser_storage_limit")
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise PlatformBalanceError("browser_storage_path")
        before[path] = _stamp(path)
    if sum(s[1] for s in before.values()) > MAX_BYTES:
        raise PlatformBalanceError("browser_storage_limit")
    prefix = b"_https://platform.openai.com\x00\x01"
    auth_key, org_key = prefix + b"oai-auth:onboard", prefix + b"oai/activeOrg"

    def records():
        for path in sorted(files):
            reader = ccl_leveldb.LogFile(path) if path.suffix == ".log" else ccl_leveldb.LdbFile(path)
            try:
                yield from reader
                if getattr(reader, "damage", None):
                    raise PlatformBalanceError("browser_storage_changed")
            finally:
                reader.close()
    values = _latest_values(records(), {auth_key, org_key})
    if any(_stamp(path) != stamp for path, stamp in before.items()):
        raise PlatformBalanceError("browser_storage_changed")
    if auth_key not in values or org_key not in values:
        raise PlatformBalanceError("platform_session_unavailable")
    auth = json.loads(_decode(values[auth_key]))
    token = auth.get("accessToken") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token or len(token) > MAX_VALUE:
        raise PlatformBalanceError("platform_session_unavailable")
    # Return only the access token, never the stored refresh/id tokens.
    return token, _decode(values[org_key]).strip('"')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise PlatformBalanceError("platform_redirect_refused")


def _request(path, token, timeout, *, organization_id=None, login=False):
    if path not in ("/onboarding/login", "/billing/credit_grants"):
        raise PlatformBalanceError("platform_request_refused")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "Origin": ORIGIN, "Referer": ORIGIN + "/"}
    if organization_id:
        headers["OpenAI-Organization"] = organization_id
    if login:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API + path, headers=headers, data=b'{"app":"api"}' if login else None)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        # No response-body/error text: servers can echo credential material.
        raise PlatformBalanceError("platform_auth_expired" if exc.code in (401, 403) else "platform_request_failed") from None
    if len(body) > 1024 * 1024:
        raise PlatformBalanceError("platform_response_limit")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise PlatformBalanceError("platform_response_invalid")
    return value


def fetch_credit_grants(profile, organization_id, timeout=5):
    try:
        token, active_org = read_platform_session(profile)
        if active_org != organization_id:
            raise PlatformBalanceError("account_mismatch", mismatch=True)
        login = _request("/onboarding/login", token, timeout, login=True)
        user = login.get("user") or {}
        orgs = user.get("orgs") or {}
        rows = orgs.get("data") if isinstance(orgs, dict) else None
        if not isinstance(rows, list) or len(rows) > 256:
            raise PlatformBalanceError("platform_response_invalid")
        org = next((o for o in rows if isinstance(o, dict) and o.get("id") == organization_id), None)
        if org is None:
            raise PlatformBalanceError("account_mismatch", mismatch=True)
        session = user.get("session") or {}
        key = session.get("sensitive_id") if isinstance(session, dict) else None
        if not isinstance(key, str) or not key.startswith("sess-") or len(key) > MAX_VALUE:
            raise PlatformBalanceError("platform_session_unavailable")
        payload = _request("/billing/credit_grants", key, timeout, organization_id=organization_id)
        return org, payload
    except PlatformBalanceError:
        raise
    except Exception:
        raise PlatformBalanceError("platform_session_unavailable") from None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) and x >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_credit_grants(raw):
    total, paid = _number(raw.get("total_available")), _number(raw.get("total_paid_available"))
    promotional = total - paid if total is not None and paid is not None and total >= paid else None
    container = raw.get("grants")
    rows = container.get("data") if isinstance(container, dict) else None
    grants, represented = [], 0.0
    if not isinstance(rows, list) or len(rows) > 256 or container.get("has_more"):
        rows = None
    for row in rows or []:
        if not isinstance(row, dict) or not str(row.get("id", "")).startswith("chatgpt_api_credits_promo_"):
            continue
        amount, used = _number(row.get("grant_amount")), _number(row.get("used_amount"))
        start, end = _number(row.get("effective_at")), _number(row.get("expires_at"))
        if amount is None or used is None or used > amount:
            rows = None
            break
        if row.get("is_scheduled") or row.get("is_held_for_prepaid_transition") or (start is not None and start > time.time()):
            continue
        remaining = amount - used
        if remaining <= 0 or (end is not None and end <= time.time()):
            continue
        represented += remaining
        grants.append({"name": "ChatGPT API credit", "program_id": "chatgpt_api_credits_promo", "granted": amount, "remaining": remaining,
                       "expires_at": datetime.fromtimestamp(end, timezone.utc).isoformat() if end else None,
                       "granted_at": datetime.fromtimestamp(start, timezone.utc).isoformat() if start else None})
    # Unknown promotional programs keep their aggregate amount but no invented expiry.
    if rows is None or promotional is None or abs(represented - promotional) > 0.00001:
        grants = None
    return {"currency": "USD", "promotional_balance": promotional, "purchased_balance": paid, "total": total, "grants": grants}
