# ═══════════════════════════════════════════════════════════════════
#  proxy_checker.py — ROBLOX-AWARE POOL BUILDER (v2)
#  Lives in proxies-main. Run this locally, push the resulting
#  proxies.json → every tool picks it up within 60s. No redeploys.
#
#  WHAT IT DOES
#   1. Reads NEW proxies from new_proxies.txt (one per line, any of:
#         ip:port:user:pass          ← most providers' export format
#         user:pass@ip:port
#         http://user:pass@ip:port
#      Lines starting with # are ignored. Existing entries in
#      proxies.json are kept and re-checked too.)
#   2. Tests EVERY proxy against the REAL Roblox endpoints:
#         • POST https://auth.roblox.com/v2/logout
#           → a GOOD IP answers 403 **with an X-CSRF-TOKEN header**
#             (that header is exactly what the refresh chain needs —
#             if it's missing, the IP is WAF-blocked)
#         • GET  https://users.roblox.com/v1/users/authenticated
#           → a GOOD IP answers 401 JSON (not a 403 challenge page)
#   3. Writes proxies.json in the exact schema the engines read:
#         { ip, port, user, pass, url, alive, speed_ms,
#           bypasses_403, last_check, success_rate }
#
#  USAGE
#     pip install requests
#     python proxy_checker.py
#     git add proxies.json && git commit -m "refresh pool" && git push
# ═══════════════════════════════════════════════════════════════════

import json
import re
import time
import requests
from pathlib import Path

HERE = Path(__file__).parent
PROXY_FILE = HERE / "proxies.json"
NEW_PROXIES_FILE = HERE / "new_proxies.txt"

TIMEOUT = 8
CSRF_URL = "https://auth.roblox.com/v2/logout"          # 403 + X-CSRF-TOKEN = healthy
AUTH_URL = "https://users.roblox.com/v1/users/authenticated"  # 401 JSON = healthy

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


# ── parse new_proxies.txt lines into pool entries ──
def parse_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # http://user:pass@ip:port
    m = re.match(r"^(?:https?://)?([^:@/]+):([^@/]+)@([^:/]+):(\d+)/?$", line)
    if m:
        user, pw, ip, port = m.group(1), m.group(2), m.group(3), m.group(4)
        return _entry(ip, port, user, pw)

    # user:pass@ip:port
    m = re.match(r"^([^:@/]+):([^@/]+)@([^:/]+):(\d+)$", line)
    if m:
        user, pw, ip, port = m.group(1), m.group(2), m.group(3), m.group(4)
        return _entry(ip, port, user, pw)

    # ip:port:user:pass
    parts = line.split(":")
    if len(parts) == 4:
        ip, port, user, pw = parts
        return _entry(ip, port, user, pw)

    # ip:port (no auth)
    if len(parts) == 2 and parts[1].isdigit():
        return _entry(parts[0], parts[1], "", "")

    print(f"[SKIP] unrecognised line: {line[:60]}")
    return None


def _entry(ip, port, user, pw):
    ip, port, user, pw = ip.strip(), port.strip(), user.strip(), pw.strip()
    if user and pw:
        url = f"http://{user}:{pw}@{ip}:{port}"
    else:
        url = f"http://{ip}:{port}"
    return {
        "ip": ip, "port": int(port), "user": user, "pass": pw, "url": url,
        "alive": True, "speed_ms": None, "bypasses_403": False,
        "last_check": 0, "success_rate": 1.0,
    }


# ── the real Roblox tests ──
def check_proxy(proxy):
    pd = {"http": proxy["url"], "https": proxy["url"]}
    result = {"alive": False, "speed_ms": None, "bypasses_403": False, "reason": ""}

    # TEST 1 — the exact call the cookie-refresh chain depends on:
    # a non-WAF-blocked IP gets 403 WITH an X-CSRF-TOKEN header back.
    t0 = time.time()
    try:
        r = requests.post(CSRF_URL, json={}, headers=BROWSER_HEADERS,
                          proxies=pd, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        csrf = r.headers.get("X-CSRF-TOKEN") or r.headers.get("x-csrf-token")
        if r.status_code == 403 and csrf:
            result["alive"] = True
            result["bypasses_403"] = True
            result["speed_ms"] = ms
            result["reason"] = "CSRF ok"
        elif csrf:
            result["alive"] = True
            result["bypasses_403"] = True
            result["speed_ms"] = ms
            result["reason"] = f"CSRF ok (HTTP {r.status_code})"
        elif r.status_code == 403:
            result["reason"] = "403 WITHOUT CSRF header → WAF-blocked IP"
        elif r.status_code == 407:
            result["reason"] = "407 → wrong proxy credentials"
        else:
            result["reason"] = f"unexpected HTTP {r.status_code}"
    except Exception as e:
        result["reason"] = f"network error: {type(e).__name__}"
        return result

    # TEST 2 — users API must answer with JSON 401 (not a challenge page)
    try:
        r2 = requests.get(AUTH_URL, headers=BROWSER_HEADERS, proxies=pd, timeout=TIMEOUT)
        if r2.status_code == 401:
            result["reason"] += " · users-api ok"
        elif r2.status_code == 403:
            result["bypasses_403"] = False
            result["reason"] += " · users-api WAF-blocked"
        else:
            result["reason"] += f" · users-api HTTP {r2.status_code}"
    except Exception:
        pass

    return result


def main():
    # 1. load existing pool (keep stats)
    existing = []
    if PROXY_FILE.exists():
        try:
            existing = json.load(PROXY_FILE.open()).get("proxies", [])
        except Exception:
            existing = []

    # 2. merge in new proxies from new_proxies.txt
    seen_urls = {p.get("url") for p in existing}
    added = 0
    if NEW_PROXIES_FILE.exists():
        for line in NEW_PROXIES_FILE.read_text().splitlines():
            entry = parse_line(line)
            if entry and entry["url"] not in seen_urls:
                existing.append(entry)
                seen_urls.add(entry["url"])
                added += 1
        print(f"[MERGE] {added} new proxy(ies) added from new_proxies.txt")
    else:
        print("[MERGE] no new_proxies.txt found — re-checking existing pool only")

    print(f"[CHECK] testing {len(existing)} proxies against Roblox...")
    alive_count = bypass_count = 0

    for i, proxy in enumerate(existing, 1):
        res = check_proxy(proxy)
        proxy["alive"] = res["alive"]
        proxy["speed_ms"] = res["speed_ms"]
        proxy["bypasses_403"] = res["bypasses_403"]
        proxy["last_check"] = int(time.time())
        proxy["success_rate"] = round(
            proxy.get("success_rate", 1.0) * 0.95 + 0.05
        ) if res["alive"] else round(proxy.get("success_rate", 1.0) * 0.8, 2)

        if res["alive"]:
            alive_count += 1
            if res["bypasses_403"]:
                bypass_count += 1

        tag = "✓" if res["alive"] else "✗"
        byp = "403✓" if res["bypasses_403"] else "403✗"
        print(f"  [{i}/{len(existing)}] {tag} {proxy['ip']}:{proxy['port']} "
              f"{proxy.get('speed_ms') or '-'}ms {byp} — {res['reason']}")

    # 3. rank: alive 403-bypassers first, then by success_rate, then speed
    existing.sort(key=lambda p: (
        0 if p.get("alive") else 1,
        0 if p.get("bypasses_403") else 1,
        -(p.get("success_rate", 0) or 0),
        p.get("speed_ms", 9999) or 9999,
    ))

    data = {
        "proxies": existing,
        "metadata": {
            "total": len(existing),
            "alive": alive_count,
            "bypasses_403": bypass_count,
            "dead": len(existing) - alive_count,
            "updated_at": int(time.time()),
        },
    }
    PROXY_FILE.write_text(json.dumps(data, indent=2))
    print(f"\n[DONE] {alive_count} alive · {bypass_count} bypass 403 · "
          f"{len(existing) - alive_count} dead → written to {PROXY_FILE.name}")
    if alive_count == 0:
        print("[WARN] pool is EMPTY-ALIVE — tools will fall back to DIRECT "
              "(that's the CSRF failure you're seeing). Add fresh residential IPs.")


if __name__ == "__main__":
    main()
