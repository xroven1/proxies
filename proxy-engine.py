# ═══════════════════════════════════════════════════════════════════
#  proxy-engine.py — CENTRAL PYTHON PROXY ENGINE (v3)
#  Lives ONLY in the proxies-main repo. Every Python tool (bypass-main,
#  directories.py, future tools) loads this file at runtime via
#  proxy_loader.py — so when you push an update here, every tool
#  automatically picks it up on its next cold start.
#
#  Exposes (drop-in, back-compatible):
#    selector                    — shared ProxySelector instance
#    proxied_request(method, url, cookie=, headers=, json_body=,
#                    timeout=, max_rotations=, label=)
#    get_best_proxy()            — back-compat shim
#    mark_ok(proxy_obj)          — back-compat shim
#    mark_fail(proxy_obj, status=)  — back-compat shim
#    refresh_cookie_through_proxy(original_cookie_value, headers,
#                                 proxy_dict, timeout=)
#    get_roblox_headers()
#    USER_AGENTS, PROXIES_GITHUB_URL, MAX_ROTATIONS, ALERT_COOLDOWN
# ═══════════════════════════════════════════════════════════════════

import os
import re
import json as jsonlib
import random
import time
import threading
import requests
from datetime import datetime
from urllib.parse import urlparse

# ── Tunables ────────────────────────────────────────────────────────
PROXIES_GITHUB_URL = "https://raw.githubusercontent.com/xroven1/proxies/main"
POOL_CACHE_TTL     = 60          # re-pull proxies.json every 60s
MAX_ROTATIONS      = 4           # proxy attempts per request before giving up
ALERT_COOLDOWN     = 20          # min seconds between Discord 403-storm alerts
WEBHOOK_URL        = "https://discord.com/api/webhooks/1553383042767265912/Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
]


class ProxySelector:
    """Pool loader + 403-aware ranker. One shared instance for the whole process."""

    def __init__(self, base_url):
        self.base_url = base_url
        self.proxies = []
        self.last_load = 0
        self.load_interval = POOL_CACHE_TTL
        self._lock = threading.Lock()
        self._stats = {}        # ip -> {req, ok, c403, c429, err}
        self._last_alert = {}   # label -> ts (Discord alert throttle)
        self.load()

    def _ua(self):
        return random.choice(USER_AGENTS) if USER_AGENTS else "Mozilla/5.0"

    def load(self):
        now = time.time()
        if now - self.last_load < self.load_interval and self.proxies:
            return
        try:
            r = requests.get(f"{self.base_url}/proxies.json", timeout=5,
                             headers={"User-Agent": self._ua()})
            if r.status_code == 200:
                data = r.json()
                proxies = data.get("proxies", [])
                if proxies:
                    with self._lock:
                        self.proxies = proxies
                    self.last_load = now
                    print(f"[PROXY] Pool refreshed from GitHub — {len(proxies)} entries "
                          f"({data.get('metadata', {}).get('alive', '?')} alive)")
                    return
        except Exception as e:
            print(f"[PROXY] GitHub pool fetch failed: {e}")

        # local fallback — proxies.json shipped next to this engine
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(here, "proxies.json"),
                     os.path.join(here, "..", "proxies.json")):
            try:
                with open(cand) as f:
                    proxies = jsonlib.load(f).get("proxies", [])
                if proxies:
                    with self._lock:
                        self.proxies = proxies
                    self.last_load = now
                    print(f"[PROXY] Pool loaded from LOCAL fallback ({cand}) — {len(proxies)} entries")
                    return
            except Exception:
                continue
        if not self.proxies:
            print("[PROXY] No proxy pool available (GitHub + local all failed)")

    def mark_result(self, ip, ok, status=0):
        with self._lock:
            s = self._stats.setdefault(ip, {"req": 0, "ok": 0, "c403": 0, "c429": 0, "err": 0})
            s["req"] += 1
            if ok:
                s["ok"] += 1
            elif status == 403:
                s["c403"] += 1
            elif status == 429:
                s["c429"] += 1
            else:
                s["err"] += 1

    def _stat(self, ip):
        return self._stats.get(ip, {"req": 0, "ok": 0, "c403": 0, "c429": 0, "err": 0})

    def _ranked(self):
        """alive 403-bypassers first, then fewest 403s, then fastest."""
        def key(p):
            s = self._stat(p.get("ip", "?"))
            rate403 = (s["c403"] / s["req"]) if s["req"] else 0.0
            return (
                0 if p.get("alive", True) else 1,
                0 if p.get("bypasses_403") else 1,
                rate403,
                p.get("speed_ms", 9999) or 9999
            )
        with self._lock:
            return sorted(self.proxies, key=key)

    def get_best(self):
        self.load()
        ranked = self._ranked()
        alive = [p for p in ranked if p.get("alive")]
        return alive[0] if alive else (ranked[0] if ranked else None)

    def get_403_bypass(self):
        self.load()
        bypass = [p for p in self._ranked() if p.get("alive") and p.get("bypasses_403")]
        return bypass[0] if bypass else self.get_best()

    def get_rotation_candidates(self, max_candidates=MAX_ROTATIONS):
        self.load()
        return self._ranked()[:max_candidates]

    def get_random_alive(self):
        self.load()
        alive = [p for p in self.proxies if p.get("alive")]
        return random.choice(alive) if alive else None

    def get_all_alive(self):
        self.load()
        return [p for p in self.proxies if p.get("alive")]

    def proxy_dict(self, proxy):
        if not proxy:
            return None
        url = proxy.get("url")
        return {"http": url, "https": url} if url else None


# Single shared selector for the whole process.
selector = ProxySelector(PROXIES_GITHUB_URL)


# ═══ CENTRAL PROXY ROTATION LAYER (403-aware, logged) ══════════════
def _short_host(url):
    try:
        return urlparse(url).netloc or url[:40]
    except Exception:
        return url[:40]


def alert_403_storm(label, url, detail):
    """Throttled Discord alert when every proxy route got blocked."""
    now = time.time()
    if now - selector._last_alert.get(label, 0) < ALERT_COOLDOWN:
        return
    selector._last_alert[label] = now
    embed = {
        "title": f"🚫 [Proxy] {label} — All Routes Blocked",
        "description": f"Target: `{_short_host(url)}`\n{detail}\nPool size: `{len(selector.proxies)}` → fresh IPs may be needed.",
        "color": 16711680,
        "timestamp": datetime.utcnow().isoformat()
    }
    def _fire():
        try:
            requests.post(WEBHOOK_URL, json={"username": "Noctrya Proxy Sentinel", "embeds": [embed]}, timeout=8)
        except Exception:
            pass
    threading.Thread(target=_fire, daemon=True).start()


def proxied_request(method, url, cookie=None, headers=None, json_body=None,
                    timeout=10, max_rotations=MAX_ROTATIONS, label="REQ"):
    """Central request layer — routes through the proxy pool and AUTO-ROTATES
    to a fresh IP on 403 / 429 / network error. Every hop is logged with the
    proxy IP, HTTP status and latency. Returns the last Response (or None)."""
    host = _short_host(url)
    candidates = selector.get_rotation_candidates(max_rotations)
    last_resp, last_exc = None, None

    for i, proxy_obj in enumerate(candidates):
        pdict = selector.proxy_dict(proxy_obj)
        ip = proxy_obj.get("ip", "?")
        t0 = time.time()
        try:
            kwargs = {"headers": headers, "timeout": timeout, "proxies": pdict}
            if json_body is not None:
                kwargs["json"] = json_body
            if cookie:
                kwargs["cookies"] = {".ROBLOSECURITY": cookie}
            r = requests.request(method, url, **kwargs)
            ms = int((time.time() - t0) * 1000)

            if r.status_code in (403, 429):
                selector.mark_result(ip, ok=False, status=r.status_code)
                csrf = r.headers.get("X-CSRF-TOKEN") or r.headers.get("x-csrf-token")
                if r.status_code == 403 and csrf and headers is not None:
                    headers = dict(headers); headers["X-CSRF-TOKEN"] = csrf
                    print(f"[PROXY] ⚠ {label} → {host} via {ip} → HTTP 403 (stale CSRF, token refreshed) — rotating")
                    continue
                print(f"[PROXY] ⚠ {label} → {host} via {ip} → HTTP {r.status_code} — rotating to next proxy [{i + 1}/{len(candidates)}]")
                last_resp = r
                time.sleep(0.15)
                continue

            selector.mark_result(ip, ok=True, status=r.status_code)
            print(f"[PROXY] ✓ {label} → {host} via {ip} → HTTP {r.status_code} ({ms}ms)"
                  + (f" [recovered on attempt {i + 1}]" if i > 0 else ""))
            return r
        except Exception as e:
            selector.mark_result(ip, ok=False, status=0)
            print(f"[PROXY] ✗ {label} → {host} via {ip} → NETWORK ERROR: {e} [{i + 1}/{len(candidates)}]")
            last_exc = e
            time.sleep(0.1)

    # pool exhausted — one direct attempt so webhook-style targets still work
    try:
        kwargs = {"headers": headers, "timeout": timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        if cookie:
            kwargs["cookies"] = {".ROBLOSECURITY": cookie}
        r = requests.request(method, url, **kwargs)
        print(f"[PROXY] ✓ {label} → {host} via DIRECT → HTTP {r.status_code} [pool exhausted]")
        if r.status_code in (403, 429):
            alert_403_storm(label, url, f"Tried {len(candidates)} proxies + DIRECT. Final status: {r.status_code}.")
        return r
    except Exception as e:
        last_exc = e

    alert_403_storm(label, url, f"Tried {len(candidates)} proxies + DIRECT. Last error: {last_exc or 'unknown'}.")
    return last_resp


# ── compat shims used by directories.py / app.py ──
def get_best_proxy():
    return selector.get_best()

def mark_ok(proxy_obj):
    if isinstance(proxy_obj, dict):
        selector.mark_result(proxy_obj.get("ip", "?"), ok=True)

def mark_fail(proxy_obj, status=0):
    if isinstance(proxy_obj, dict):
        selector.mark_result(proxy_obj.get("ip", "?"), ok=False, status=status)

def get_roblox_headers():
    ua = random.choice(USER_AGENTS)
    return {
        "User-Agent": ua,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.roblox.com",
        "Referer": "https://www.roblox.com/",
    }


# ═══ ROBLOX AUTH-TICKET REFRESH CHAIN (rides rotation layer) ════════
def refresh_cookie_through_proxy(original_cookie_value, headers, proxy_dict, timeout=10):
    info = {"success": False, "new_cookie": None, "fail_step": None, "fail_reason": None}
    rotation = selector.get_rotation_candidates(MAX_ROTATIONS) or [None]

    for attempt in range(3):
        proxy_obj = rotation[attempt % len(rotation)]
        proxy_ip = (proxy_obj or {}).get("ip", "DIRECT")
        session = requests.Session()
        use_pdict = selector.proxy_dict(proxy_obj) or proxy_dict
        if use_pdict:
            session.proxies.update(use_pdict)
        session.headers.update(headers)
        session.cookies.set(".ROBLOSECURITY", original_cookie_value, domain=".roblox.com", path="/")
        print(f"[PROXY] ⟳ REFRESH attempt {attempt + 1}/3 via {proxy_ip}")

        csrf = None
        for csrf_attempt in range(2):
            try:
                init_r = session.post("https://auth.roblox.com/v2/logout", json={}, timeout=timeout)
                csrf = init_r.headers.get("X-CSRF-TOKEN") or init_r.headers.get("x-csrf-token")

                if csrf:
                    print(f"[PROXY] ✓ REFRESH CSRF acquired via {proxy_ip}")
                    break
                elif init_r.status_code == 403:
                    selector.mark_result(proxy_ip, ok=False, status=403)
                    print(f"[PROXY] ⚠ REFRESH CSRF 403 via {proxy_ip} — rotating inside attempt")
                    time.sleep(0.1)
            except Exception:
                pass

        if not csrf:
            info["fail_step"] = "get_csrf"
            info["fail_reason"] = f"CSRF token retrieval failed on attempt {attempt + 1}"
            session.close()
            continue

        session.headers["X-CSRF-TOKEN"] = csrf
        session.headers["RBXAuthenticationNegotiation"] = "1"

        ticket = None
        for ticket_attempt in range(3):
            try:
                r = session.post("https://auth.roblox.com/v1/authentication-ticket", json={}, timeout=timeout)
                ticket = r.headers.get("rbx-authentication-ticket")

                if ticket:
                    print(f"[PROXY] ✓ REFRESH ticket acquired via {proxy_ip}")
                    selector.mark_result(proxy_ip, ok=True)
                    break
                elif r.status_code == 403:
                    selector.mark_result(proxy_ip, ok=False, status=403)
                    new_csrf = r.headers.get("X-CSRF-TOKEN") or r.headers.get("x-csrf-token")
                    if new_csrf:
                        session.headers["X-CSRF-TOKEN"] = new_csrf
                    print(f"[PROXY] ⚠ REFRESH ticket 403 via {proxy_ip} (CSRF refreshed)")
                    time.sleep(0.15)
            except Exception:
                pass

        if not ticket:
            info["fail_step"] = "get_ticket"
            info["fail_reason"] = f"Ticket generation failed on attempt {attempt + 1}"
            session.close()
            continue

        session.cookies.clear()
        session.headers.pop("X-CSRF-TOKEN", None)
        session.headers["RBXAuthenticationNegotiation"] = "1"

        try:
            r = session.post(
                "https://auth.roblox.com/v1/authentication-ticket/redeem",
                json={"authenticationTicket": ticket},
                timeout=timeout
            )
            if r.status_code != 200:
                info["fail_step"] = "redeem_ticket"
                info["fail_reason"] = f"Ticket redeem returned HTTP {r.status_code}"
                session.close()
                continue

            new_cookie = None
            for cookie in session.cookies:
                if cookie.name == ".ROBLOSECURITY":
                    new_cookie = cookie.value
                    break

            if not new_cookie:
                set_cookie = r.headers.get("Set-Cookie", "")
                match = re.search(r'\.ROBLOSECURITY=([^;]+)', str(set_cookie))
                if match:
                    new_cookie = match.group(1)

            if not new_cookie:
                new_cookie = original_cookie_value

            if not new_cookie.startswith("_|WARNING"):
                new_cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_" + new_cookie

            info["success"] = True
            info["new_cookie"] = new_cookie
            session.close()
            return new_cookie, info

        except Exception as e:
            info["fail_step"] = "redeem_ticket"
            info["fail_reason"] = str(e)
            session.close()

        time.sleep(0.2)

    return None, info
