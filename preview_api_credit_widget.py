#!/usr/bin/env python3
"""Export the real API-credit widget rows using explicitly synthetic data.

This process imports the widget row builder only. It does not start the app,
refresh providers, inspect browser profiles, or read local account bindings.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest import mock


def _install_headless_gui_stubs() -> None:
    try:
        import rumps  # noqa: F401
        import AppKit  # noqa: F401
        return
    except ImportError:
        pass

    rumps = types.ModuleType("rumps")

    class App:
        def __init__(self, *_args, **_kwargs):
            pass

    class MenuItem:
        def __init__(self, *_args, **_kwargs):
            self.title = _args[0] if _args else ""

        def add(self, *_args):
            pass

    def timer(_interval):
        def decorate(func):
            return func
        return decorate

    rumps.App = App
    rumps.MenuItem = MenuItem
    rumps.timer = timer
    rumps.quit_application = lambda *_args: None
    sys.modules["rumps"] = rumps
    sys.modules["AppKit"] = mock.MagicMock()


def _load_widget_module():
    _install_headless_gui_stubs()
    repo = Path(__file__).resolve().parent
    sys.path.insert(0, str(repo))
    path = repo / "menubar" / "ai-limit-app.py"
    spec = importlib.util.spec_from_file_location("ai_limit_preview_widget", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the widget renderer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    module = _load_widget_module()
    app = module.AiLimitApp.__new__(module.AiLimitApp)
    app._state = {"lang": "en", "global": "7d", "services": ["claude", "codex"], "widget": True}
    app._claude = {
        "5h_left": 82,
        "7d_left": 71,
        "5h_reset": None,
        "7d_reset": None,
        "weekly_models": [],
        "plan": "SAMPLE",
    }
    app._codex = app._deepseek = app._google = app._gemini = app._copilot = app._grok = None
    app._api_credit_preview_label = "SYNTHETIC FIXTURE — NOT LIVE OR HISTORICAL ACCOUNT DATA"
    app._api_credits_v1 = {
        "openai": {
            "contract": "api-credit/v1",
            "provider": "openai",
            "status": "unknown",
            "account_identity": {"profile": "Example OpenAI profile", "label": "Example OpenAI account"},
            "organization_identity": {"id": "org-example", "name": "Example OpenAI organization"},
            "binding_status": "unverified",
            "currency": None,
            "promotional_balance": None,
            "purchased_balance": None,
            "total": None,
            "grants": None,
            "observed_at": None,
            "checked_at": "2026-10-30T00:00:00Z",
            "source_updated_at": None,
            "source": "unverified",
            "error_code": "source_unverified",
            "next_grant_at": None,
            "usage_restrictions": None,
            "billing_url": "https://platform.openai.com/settings/organization/billing/overview",
        },
        "claude": {
            "contract": "api-credit/v1",
            "provider": "claude",
            "status": "ok",
            "account_identity": {"profile": "Example profile", "label": "Example account"},
            "organization_identity": {"id": "example-org", "name": "Example API organization"},
            "binding_status": "bound",
            "currency": "USD",
            "promotional_balance": 19.99,
            "purchased_balance": None,
            "total": 19.99,
            "grants": [{
                "name": "Sample promotional grant",
                "program_id": "fixture_only",
                "granted": 20.0,
                "remaining": 19.99,
                "expires_at": "2099-12-31T00:00:00Z",
                "granted_at": "2099-01-01T00:00:00Z",
                "expired": False,
                "spendable": True,
            }],
            "observed_at": "2026-10-30T00:00:00Z",
            "checked_at": "2026-10-30T00:00:00Z",
            "source_updated_at": None,
            "source": "platform.claude.com",
            "error_code": None,
            "next_grant_at": None,
            "usage_restrictions": None,
            "billing_url": "https://platform.claude.com/settings/billing",
        },
    }
    rows = app._widget_detail_rows()
    print(json.dumps({
        "preview_label": app._api_credit_preview_label,
        "contract": "api-credit/v1",
        "renderer": "AiLimitApp._widget_detail_rows",
        "rows": rows,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
