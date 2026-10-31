#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════
#  doctor.py v3 — SELF-HEALING POOL DOCTOR (proxies repo, GitHub Actions)
#
#  Every 2 HOURS it:
#   1. Probes Roblox direct (catches WAF/handshake changes)
#   2. Tests EVERY pool IP (users API + logout CSRF), incl. dead ones
#   3. AUTO-PRUNE: dead IPs get marked alive:false in proxies.json and
#      are committed back to the repo -> all tools stop using them
#      within 60s (engine filters alive:false). IPs that pass again
#      are auto-revived. Dead-connection = 1 strike, WAF-403 = 2 strikes.
#   4. Checks your tool sites are up (noctrya + optional bypass URL)
#   5. Optional cookie diagnostic (repo secret ROBLOX_COOKIE):
#      SAFE read-only check (GET users/authenticated — never logout!)
#      run direct AND through pool IPs -> shows "cookie valid direct
#      but fails via pool" = Roblox WAF eating AUTHED traffic on DC IPs
#   6. Sends ONE Discord embed with everything + REAL VERBATIM ERRORS
#      (actual exception messages + actual response bodies per IP,
#      not just "blocked") + NEED MORE PROXIES alert
#
#  Optional env: DISCORD_WEBHOOK, VERCEL_DOCTOR_URL, BYPASS_URL,
#                ROBLOX_COOKIE, DRY_RUN=1
# ═══════════════════════════════════════════════════════════════════

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests

# ── config ─────────────────────────────────────────────────────────
REPO_DIR     = os.path.dirname(os.path.abspath(__file__))
POOL_FILE    = os.path.join(REPO_DIR, 'proxies.json')
HEALTH_FILE  = os.path.join(REPO_DIR, 'health.json')
POOL_URL     = 'https://raw.githubusercontent.com/xroven1/proxies/main/proxies.json'
VERCEL_DOCTOR = os.environ.get('VERCEL_DOCTOR_URL',
    'https://www.noctrya-gen.ddnsfree.com/api/doctor?format=json')

# Tool homepages to check. Paste your bypass URL once below (or set
# BYPASS_URL env/secret) — leave '' to skip.
TOOLS = {
    'noctrya': 'https://www.noctrya-gen.ddnsfree.com',
    'bypass':  os.environ.get('BYPASS_URL') or '',
}

WEBHOOK = os.environ.get('DISCORD_WEBHOOK') or (
    'https://discord.com/api/webhooks/1553383042767265912/'
    'Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc')
COOKIE = (os.environ.get('ROBLOX_COOKIE') or '').strip()

UA   = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
HDRS = {
    'User-Agent': UA,
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://www.roblox.com',
    'Referer': 'https://www.roblox.com/',
}
TMO           = 8
MIN_PASS      = 3     # fewer passing IPs than this = NEED MORE PROXIES
STRIKES_WAF   = 2     # consecutive failed runs before a 403 IP is pruned
STRIKES_DEAD  = 1     # connection-dead IPs are pruned immediately
DRY_RUN       = os.environ.get('DRY_RUN') == '1'
IN_ACTIONS    = os.environ.get('GITHUB_ACTIONS') == 'true'

GREEN, YELLOW, RED = 0x2ECC71, 0xF1C40F, 0xE74C3C


def _snippet(r, n=110):
    """Real response body (flattened) — shows if a 403 is a CF challenge,
       a Roblox JSON error, or something else entirely."""
    try:
        t = ' '.join((r.text or '').split())
        return (t[:n] + ('…' if len(t) > n else '')) or '(empty body)'
    except Exception:
        return '(body unreadable)'


def _exc(e):
    """Real exception text — the thing you actually need to debug."""
    s = ' '.join(str(e).split())
    return (type(e).__name__ + ': ' + s) if s else type(e).__name__


# ── Roblox probes ──────────────────────────────────────────────────
def probe_users(session, proxies=None, cookie=None):
    """GET users API. No cookie: 401 JSON = IP clean.
       With cookie: 200 = cookie valid (SAFE, read-only).
       Always carries the REAL error (status + body / exception text)."""
    h = dict(HDRS)
    if cookie:
        h['Cookie'] = '.ROBLOSECURITY=' + cookie
    try:
        r = session.get('https://users.roblox.com/v1/users/authenticated',
                        headers=h, proxies=proxies, timeout=TMO)
        if cookie:
            ok   = (r.status_code == 200)
            note = ('200 — cookie VALID' if ok else
                    f'{r.status_code} — cookie REJECTED' if r.status_code in (401, 403)
                    else f'HTTP {r.status_code}')
        else:
            ok   = (r.status_code == 401 and '"errors"' in r.text)
            note = ('401 JSON — clean' if ok else
                    '403 — WAF BLOCKED' if r.status_code == 403 else f'HTTP {r.status_code}')
        err = None if ok else f"users HTTP {r.status_code}: {_snippet(r)}"
        return {'status': r.status_code, 'ok': ok, 'blocked': r.status_code == 403,
                'note': note, 'err': err}
    except Exception as e:
        return {'status': 0, 'ok': False, 'blocked': False,
                'note': 'DEAD — connection failed', 'err': 'users: ' + _exc(e)}


def probe_logout(session, proxies=None):
    """POST logout (NO cookie): 403 WITH X-CSRF-TOKEN = refresh chain OK."""
    try:
        r = session.post('https://auth.roblox.com/v2/logout',
                         headers=HDRS, json={}, proxies=proxies, timeout=TMO)
        csrf = r.headers.get('x-csrf-token')
        if r.status_code == 403 and csrf:
            return {'status': 403, 'ok': True, 'note': '403+CSRF — refresh chain OK', 'err': None}
        if r.status_code == 403:
            return {'status': 403, 'ok': False, 'note': '403 no CSRF — WAF BLOCKED',
                    'err': f"logout HTTP 403 (no token): {_snippet(r)}"}
        if r.status_code == 401:
            return {'status': 401, 'ok': True, 'note': '401 unauth — WAF clear', 'err': None}
        return {'status': r.status_code, 'ok': r.status_code < 500,
                'note': f'HTTP {r.status_code}',
                'err': None if r.status_code < 500 else f"logout HTTP {r.status_code}: {_snippet(r)}"}
    except Exception as e:
        return {'status': 0, 'ok': False, 'note': 'DEAD — connection failed',
                'err': 'logout: ' + _exc(e)}


def check_direct():
    s = requests.Session()
    users, logout = probe_users(s), probe_logout(s)
    return {'users': users, 'logout': logout,
            'waf_ok':
