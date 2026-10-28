#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════
#  doctor.py v2 — SELF-HEALING POOL DOCTOR (proxies repo, GitHub Actions)
#
#  Every 30 min it:
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
#   6. Sends ONE Discord embed with everything + NEED MORE PROXIES alert
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


# ── Roblox probes ──────────────────────────────────────────────────
def probe_users(session, proxies=None, cookie=None):
    """GET users API. No cookie: 401 JSON = IP clean.
       With cookie: 200 = cookie valid (SAFE, read-only)."""
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
        return {'status': r.status_code, 'ok': ok, 'blocked': r.status_code == 403, 'note': note}
    except Exception as e:
        return {'status': 0, 'ok': False, 'blocked': False, 'note': f'FAIL {type(e).__name__}'}


def probe_logout(session, proxies=None):
    """POST logout (NO cookie): 403 WITH X-CSRF-TOKEN = refresh chain OK."""
    try:
        r = session.post('https://auth.roblox.com/v2/logout',
                         headers=HDRS, json={}, proxies=proxies, timeout=TMO)
        csrf = r.headers.get('x-csrf-token')
        if r.status_code == 403 and csrf:
            return {'status': 403, 'ok': True, 'note': '403+CSRF — refresh chain OK'}
        if r.status_code == 403:
            return {'status': 403, 'ok': False, 'note': '403 no CSRF — WAF BLOCKED'}
        if r.status_code == 401:
            return {'status': 401, 'ok': True, 'note': '401 unauth — WAF clear'}
        return {'status': r.status_code, 'ok': r.status_code < 500, 'note': f'HTTP {r.status_code}'}
    except Exception as e:
        return {'status': 0, 'ok': False, 'note': f'FAIL {type(e).__name__}'}


def check_direct():
    s = requests.Session()
    users, logout = probe_users(s), probe_logout(s)
    return {'users': users, 'logout': logout,
            'waf_ok': users['ok'] or logout['ok'],
            'changed': logout['status'] == 200}


# ── tool / vercel checks ───────────────────────────────────────────
def check_tool(name, url):
    if not url:
        return {'up': None, 'note': 'not configured'}
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=12)
        return {'up': r.status_code < 500, 'note': f'HTTP {r.status_code}'}
    except Exception as e:
        return {'up': False, 'note': f'DOWN ({type(e).__name__})'}


def check_vercel():
    """Reads the optional Noctrya doctor endpoint. If you deleted it,
       this just reports 'not deployed' and everything else still works."""
    try:
        r = requests.get(VERCEL_DOCTOR, headers={'User-Agent': UA}, timeout=20)
        d = r.json()
        dv  = d.get('direct_from_vercel', {})
        uok = bool(dv.get('users_api', {}).get('ok'))
        lok = bool(dv.get('csrf_authed_logout', {}).get('ok'))
        return {'reachable': True, 'ip': d.get('vercel_egress_ip', '?'),
                'flagged': not (uok and lok)}
    except Exception:
        return {'reachable': False, 'ip': '?', 'flagged': None}


# ── pool IP check ──────────────────────────────────────────────────
def check_ip(p):
    px = {'http': p['url'], 'https': p['url']}
    s = requests.Session()
    t0 = time.time()
    users  = probe_users(s, px)
    logout = probe_logout(s, px)
    ms = int((time.time() - t0) * 1000)
    passed = users['ok'] or logout['ok']
    if users['status'] == 0 and logout['status'] == 0:
        state = 'DEAD'
    elif not passed:
        state = 'WAF-BLOCKED'
    else:
        state = 'OK'
    return {'ip': p['ip'], 'state': state, 'ms': ms,
            'users': users, 'logout': logout, 'passed': passed}


# ── cookie diagnostic (SAFE: never calls logout with the cookie) ───
def check_cookie(ok_ips):
    if not COOKIE:
        return None
    out = {'direct': probe_users(requests.Session(), None, COOKIE)['note'],
           'via_pool': [], 'note': ''}
    s = requests.Session()
    for p in ok_ips[:3]:
        px = {'http': p['url'], 'https': p['url']}
        out['via_pool'].append({'ip': p['ip'],
                                'note': probe_users(s, px, COOKIE)['note']})
    via_ok = sum(1 for v in out['via_pool'] if 'VALID' in v['note'])
    direct_ok = 'VALID' in out['direct']
    if direct_ok and via_ok == len(out['via_pool']) and out['via_pool']:
        out['note'] = 'Cookie VALID and works through pool — cookies are fine'
    elif direct_ok and out['via_pool'] and via_ok < len(out['via_pool']):
        out['note'] = ('Cookie is VALID but Roblox WAF rejects AUTHED traffic '
                       'through your pool IPs → this is why tools error. '
                       'Datacenter IPs get challenged when authenticated. '
                       'FIX: add ISP/residential proxies.')
    elif not direct_ok:
        out['note'] = 'Cookie REJECTED even direct from the runner → cookie is expired/invalid, get a fresh one'
    return out


# ── auto-prune: update flags + commit ──────────────────────────────
def apply_prune(data, results):
    try:
        with open(HEALTH_FILE) as f:
            health = json.load(f).get('ips', {})
    except Exception:
        health = {}

    strikes, pruned, revived = {}, [], []
    by_ip = {r['ip']: r for r in results}
    for p in data.get('proxies', []):
        ip = p['ip']
        r  = by_ip.get(ip)
        if not r:
            continue
        st = health.get(ip, {'failures': 0})
        if r['state'] == 'OK':
            if st['failures'] > 0 or p.get('alive') is False:
                revived.append(ip)
            st['failures'] = 0
            p['alive'] = True
            p['success_rate'] = 1.0
        else:
            st['failures'] = st.get('failures', 0) + 1
            limit = STRIKES_DEAD if r['state'] == 'DEAD' else STRIKES_WAF
            if st['failures'] >= limit and p.get('alive') is not False:
                pruned.append(ip)
            p['alive'] = st['failures'] < limit
            p['success_rate'] = 0.0
        p['last_check'] = int(time.time())
        strikes[ip] = {'failures': st['failures'], 'last': r['state']}

    pool = data.get('proxies', [])
    data['metadata'] = {
        'total': len(pool),
        'alive': sum(1 for p in pool if p.get('alive') is not False),
        'dead':  sum(1 for p in pool if p.get('alive') is False),
        'updated_at': int(time.time()),
    }
    with open(POOL_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    with open(HEALTH_FILE, 'w') as f:
        json.dump({'ips': strikes}, f, indent=2)
    return pruned, revived


def git_commit():
    if not IN_ACTIONS:
        print('[GIT] not in Actions — skipping commit')
        return
    try:
        subprocess.run(['git', 'config', 'user.name', 'pool-doctor-bot'], cwd=REPO_DIR, check=True)
        subprocess.run(['git', 'config', 'user.email', 'actions@github.com'], cwd=REPO_DIR, check=True)
        subprocess.run(['git', 'add', 'proxies.json', 'health.json'], cwd=REPO_DIR, check=True)
        c = subprocess.run(['git', 'commit', '-m', 'doctor: pool health auto-update'],
                           cwd=REPO_DIR, capture_output=True, text=True)
        if c.returncode == 0:
            subprocess.run(['git', 'push'], cwd=REPO_DIR, check=True)
            print('[GIT] pushed pool health update')
        else:
            print('[GIT] nothing to commit')
    except Exception as e:
        print('[GIT ERROR]', e)


# ── discord ────────────────────────────────────────────────────────
def send(embed):
    if DRY_RUN:
        print('[DRY_RUN] embed not sent:\n' + json.dumps(embed, indent=2)[:1800])
        return
    try:
        r = requests.post(WEBHOOK, json={'username': 'Noctrya Pool Doctor',
                                         'embeds': [embed]}, timeout=15)
        print('[DISCORD]', r.status_code, r.text[:200])
    except Exception as e:
        print('[DISCORD ERROR]', e)


# ── main ───────────────────────────────────────────────────────────
def main():
    try:
        with open(POOL_FILE) as f:
            data = json.load(f)
        src = 'local proxies.json'
    except Exception:
        try:
            data = requests.get(POOL_URL, timeout=15).json()
            src = 'raw fallback (read-only)'
        except Exception as e:
            print('FATAL: cannot load pool:', e)
            return 1

    pool = data.get('proxies', [])
    direct = check_direct()
    vercel = check_vercel()
    tools  = {n: check_tool(n, u) for n, u in TOOLS.items()}

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(pool) or 1))) as ex:
        results = list(ex.map(check_ip, pool)) if pool else []

    ok_ips  = [r for r in results if r['state'] == 'OK']
    blocked = [r for r in results if r['state'] == 'WAF-BLOCKED']
    dead    = [r for r in results if r['state'] == 'DEAD']
    passed  = len(ok_ips)
    need_more = pool and passed < MIN_PASS

    pruned, revived = ([], [])
    if not DRY_RUN and src == 'local proxies.json':
        pruned, revived = apply_prune(data, results)
        git_commit()

    cookie = check_cookie(ok_ips)

    # ── verdict ──
    actions = []
    if direct['changed']:
        color, verdict = RED, 'ROBLOX CHANGED THE HANDSHAKE — engine code needs updating, proxies will not help'
        actions.append('Update engine code (proxy-manager.js / proxy-engine.py)')
    elif passed == 0 and pool:
        color, verdict = RED, 'ENTIRE POOL FAILED — all IPs dead or WAF-burned'
        actions.append('NEED MORE PROXIES — buy RESIDENTIAL/ISP IPs, run proxy_checker.py, push proxies.json')
    elif need_more:
        color, verdict = RED, f'POOL TOO SMALL — only {passed}/{len(pool)} IPs passing (min {MIN_PASS})'
        actions.append('NEED MORE PROXIES — top up with ISP/residential IPs')
    elif cookie and 'REJECTED even direct' in cookie['note']:
        color, verdict = RED, 'COOKIE IS EXPIRED/INVALID — rejected even without any proxy'
        actions.append('Get a fresh cookie')
    elif cookie and 'Datacenter' in cookie['note']:
        color, verdict = RED, 'COOKIE VALID BUT POOL BLOCKS AUTHED TRAFFIC — your IPs are datacenter; Roblox challenges authenticated requests'
        actions.append('Replace pool with ISP/residential proxies (this fixes the tool errors)')
    elif vercel['flagged'] and passed > 0:
        color, verdict = YELLOW, f'VERCEL IP FLAGGED (expected) but pool healthy: {passed}/{len(pool)} — tools route through proxies, system OK'
    elif any(t['up'] is False for t in tools.values()):
        color, verdict = YELLOW, 'A TOOL SITE IS DOWN'
        actions.append('Check the down tool on Vercel')
    elif passed < len(pool):
        color, verdict = YELLOW, f'POOL DEGRADED — {passed}/{len(pool)} passing (auto-prune handled the rest)'
    else:
        color, verdict = GREEN, f'ALL HEALTHY — {passed}/{len(pool)} IPs passing, Roblox unchanged'
    if not direct['waf_ok']:
        actions.append('Roblox WAF also blocks the GitHub runner — possible WAF-wide change, watch next runs')

    # ── per-IP lines ──
    icon = {'OK': '✅', 'WAF-BLOCKED': '⛔', 'DEAD': '💀'}
    prune_set = set(pruned)
    lines = []
    for r in results:
        mark = ' 🪦pruned' if r['ip'] in prune_set else (' 🩹revived' if r['ip'] in revived else '')
        lines.append(f"{icon[r['state']]} `{r['ip']:<15}` {r['ms']:>5}ms  {r['users']['note']}{mark}")
    extra = ''
    if len(lines) > 20:
        extra = f"\n…and {len(lines) - 20} more"
        lines = lines[:20]

    tools_txt = '\n'.join(
        f"{'✅' if t['up'] else ('➖' if t['up'] is None else '❌')} {n}: {t['note']}"
        for n, t in tools.items())

    fields = [
        {'name': '🤖 Roblox WAF (direct)',
         'value': f"users: {direct['users']['note']}\nlogout: {direct['logout']['note']}",
         'inline': True},
        {'name': '🌐 Vercel IP',
         'value': ('not monitored\n(doctor.js removed)' if not vercel['reachable'] else
                   f"`{vercel['ip']}`\n" + ('⛔ FLAGGED' if vercel['flagged'] else '✅ not flagged')),
         'inline': True},
        {'name': '🧊 Pool',
         'value': (f"**{passed}/{len(pool)}** passing\n⛔ {len(blocked)} · 💀 {len(dead)}"
                   f"\n🪦 {len(pruned)} pruned · 🩹 {len(revived)} revived"),
         'inline': True},
        {'name': '🔧 Tools',
         'value': tools_txt or '—', 'inline': False},
    ]
    if cookie:
        pool_lines = '\n'.join(f"• `{v['ip']}`: {v['note']}" for v in cookie['via_pool']) or '—'
        fields.append({'name': '🍪 Cookie check (read-only)',
                       'value': f"direct: {cookie['direct']}\n{pool_lines}\n**{cookie['note']}**",
                       'inline': False})
    if actions:
        fields.append({'name': '⚡ Actions needed',
                       'value': '\n'.join('• ' + a for a in actions[:8]), 'inline': False})

    embed = {
        'title': '🩺 Pool Doctor — ' + ('ALL HEALTHY' if color == GREEN else
                 'ATTENTION NEEDED' if color == YELLOW else 'CRITICAL'),
        'description': ('```\n' + '\n'.join(lines) + extra + '\n```'
                        if lines else '_pool empty — proxies.json has no IPs_'),
        'color': color,
        'fields': fields,
        'footer': {'text': 'proxies-main v2 · auto-prune ON · every 30 min'},
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
    }

    print('=' * 60)
    print(f"VERDICT: {verdict}")
    print(f"pool {passed}/{len(pool)} | pruned: {pruned} | revived: {revived}")
    if cookie:
        print(f"cookie: {cookie['note']}")
    send(embed)
    return 0 if color != RED else 1


if __name__ == '__main__':
    raise SystemExit(main())
