#!/usr/bin/env python3
"""Manage authenticated Divisare session (Phase 0).

Two ways to authenticate, in order of recommended reliability:

  1. Cookie import (RECOMMENDED — most reliable):
     Log in to divisare.com in your browser, open DevTools → Application →
     Cookies → divisare.com. Copy the full cookie header (or just the
     `_divisare_session` value). Set it via env var:

         export DIVISARE_SESSION_COOKIE="_divisare_session=...; cf_clearance=..."

     Then run:
         python3 divisare_auth.py import

  2. Email/password login (best-effort — may break if Divisare changes form):
     Set credentials via env vars in .env or shell:
         export DIVISARE_EMAIL="you@example.com"
         export DIVISARE_PASSWORD="..."
     Then:
         python3 divisare_auth.py login

Verify the saved session works on a real project page:
     python3 divisare_auth.py verify

Refresh: re-run `import` or `login` whenever the session expires (cookies
typically last weeks-to-months for paid members; CDN tokens shorter).

Session is stored at `data/.divisare_session.json` (gitignored via `data/`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from core import config
from core.utils import create_session, logger

load_dotenv(os.path.join(config.BASE_DIR, ".env"))


# ---------------------------------------------------------------------------
# Session file I/O
# ---------------------------------------------------------------------------

def _load_session_data() -> dict | None:
    if not os.path.exists(config.DIVISARE_SESSION_PATH):
        return None
    with open(config.DIVISARE_SESSION_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_session_data(data: dict) -> None:
    os.makedirs(os.path.dirname(config.DIVISARE_SESSION_PATH), exist_ok=True)
    with open(config.DIVISARE_SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # Restrict to user-only since it contains an authenticated session
    try:
        os.chmod(config.DIVISARE_SESSION_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public helper for downstream callers (divisare_crawler.py)
# ---------------------------------------------------------------------------

def get_authenticated_session():
    """Return a `requests.Session` pre-loaded with saved Divisare cookies.

    Raises RuntimeError if no session has been saved. Used by divisare_crawler.
    """
    data = _load_session_data()
    if data is None:
        raise RuntimeError(
            "No Divisare session saved. Run "
            "`python3 divisare_auth.py import` or `login` first."
        )

    sess = create_session()
    sess.headers.update({"User-Agent": config.DIVISARE_USER_AGENT})

    # Set cookies on the requests Session (correct domain/path so they get
    # sent on every divisare.com request).
    cookies = data.get("cookies") or {}
    for name, value in cookies.items():
        sess.cookies.set(name, value, domain=".divisare.com")
    return sess


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _parse_cookie_header(header: str) -> dict:
    """Turn 'a=1; b=2; c=3' into {'a': '1', 'b': '2', 'c': '3'}."""
    out: dict = {}
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def cmd_import() -> int:
    cookie = os.environ.get("DIVISARE_SESSION_COOKIE")
    if not cookie:
        print("Paste your Divisare cookie header (DevTools → Application → Cookies):")
        try:
            cookie = input("> ").strip()
        except EOFError:
            cookie = ""
    if not cookie:
        print("ERROR: no cookie provided.")
        return 1

    cookies = _parse_cookie_header(cookie)
    if not cookies:
        print("ERROR: could not parse cookie header.")
        return 1

    _save_session_data({
        "cookies": cookies,
        "source": "import",
    })
    print(f"Saved {len(cookies)} cookies → {config.DIVISARE_SESSION_PATH}")
    print("Next: python3 divisare_auth.py verify")
    return 0


def do_login(email: str, pw: str, *, verbose: bool = True) -> bool:
    """Programmatic login (callable from divisare_crawler for auto-relogin).

    Returns True on success (session file updated), False on any failure.
    """
    def _say(msg: str) -> None:
        if verbose:
            print(msg)

    sess = create_session()
    sess.headers.update({"User-Agent": config.DIVISARE_USER_AGENT})

    _say(f"Fetching login page: {config.DIVISARE_LOGIN_URL}")
    r = sess.get(config.DIVISARE_LOGIN_URL, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        _say(f"ERROR: login page returned HTTP {r.status_code}.")
        return False

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "lxml")
    csrf_input = soup.find("input", {"name": "authenticity_token"})
    csrf_token = csrf_input.get("value") if csrf_input else None
    if not csrf_token:
        _say("ERROR: could not find authenticity_token in login form.")
        return False

    payload = {
        "utf8": "✓",
        "authenticity_token": csrf_token,
        "person[email]":       email,
        "person[password]":    pw,
        "person[remember_me]": "1",
        "commit":              "Log in",
    }
    _say("Submitting credentials...")
    r = sess.post(config.DIVISARE_LOGIN_POST_URL, data=payload,
                  timeout=30, allow_redirects=False)

    if r.status_code == 302:
        location = r.headers.get("Location", "")
        if "login" in location.lower() or "sign_in" in location.lower():
            _say(f"ERROR: login rejected — redirected back to {location}")
            return False
    elif r.status_code == 200:
        body_lower = r.text.lower()
        if any(s in body_lower for s in ("invalid", "incorrect", "wrong password")):
            _say("ERROR: login form mentions 'invalid'/'incorrect' — bad credentials.")
            return False
    else:
        _say(f"WARN: unexpected POST status {r.status_code}")

    cookies = {c.name: c.value for c in sess.cookies}
    if "remember_person_token" not in cookies and "_divisare_com_session" not in cookies:
        _say("ERROR: no Divisare auth cookies set after login.")
        return False

    _save_session_data({"cookies": cookies, "source": "login"})
    _say(f"Saved {len(cookies)} cookies → {config.DIVISARE_SESSION_PATH}")
    return True


def cmd_login() -> int:
    email = os.environ.get("DIVISARE_EMAIL")
    pw = os.environ.get("DIVISARE_PASSWORD")
    if not (email and pw):
        print("ERROR: set DIVISARE_EMAIL and DIVISARE_PASSWORD env vars first.")
        print("Or use cookie-import mode (more reliable): "
              "python3 divisare_auth.py import")
        return 1
    if do_login(email, pw, verbose=True):
        print("Next: python3 divisare_auth.py verify")
        return 0
    print("Fall back to cookie-import: python3 divisare_auth.py import")
    return 1


def cmd_verify() -> int:
    try:
        sess = get_authenticated_session()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1

    test_url = config.DIVISARE_TEST_PROJECT_URL
    print(f"Fetching test page: {test_url}")
    r = sess.get(test_url, timeout=30, allow_redirects=True)
    print(f"  HTTP {r.status_code} ({len(r.text)} chars)")

    if r.status_code != 200:
        print("FAIL — non-200 response.")
        if r.status_code in (401, 403):
            print("Session likely expired or rejected. Re-import or re-login.")
        elif r.status_code == 404:
            print("Test project URL is bad. Update DIVISARE_TEST_PROJECT_URL "
                  "in config.py to a known-good project.")
        return 1

    # Tiny content sniff to confirm it's a real project page, not a login wall
    title_start = r.text.find("<title>")
    if title_start > 0:
        title_end = r.text.find("</title>", title_start)
        title = r.text[title_start + 7:title_end].strip()
        print(f"  Page title: {title[:120]!r}")
        if "log in" in title.lower() or "sign in" in title.lower():
            print("FAIL — page title looks like a login wall, not a project.")
            return 1

    print("PASS — authenticated access verified.")
    return 0


def cmd_status() -> int:
    data = _load_session_data()
    if data is None:
        print(f"No session saved at {config.DIVISARE_SESSION_PATH}")
        return 1
    print(f"Session file: {config.DIVISARE_SESSION_PATH}")
    print(f"  Source: {data.get('source', '?')}")
    print(f"  Cookie names: {sorted((data.get('cookies') or {}).keys())}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Divisare authenticated session management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("import", help="Import cookie from DIVISARE_SESSION_COOKIE env or stdin")
    sub.add_parser("login",  help="Login with DIVISARE_EMAIL + DIVISARE_PASSWORD")
    sub.add_parser("verify", help="Fetch a known project page to verify session")
    sub.add_parser("status", help="Print whether a session is saved")
    args = parser.parse_args()

    dispatch = {
        "import": cmd_import,
        "login":  cmd_login,
        "verify": cmd_verify,
        "status": cmd_status,
    }
    fn = dispatch.get(args.cmd)
    if fn is None:
        parser.print_help()
        return 1
    return fn()


if __name__ == "__main__":
    sys.exit(main())
