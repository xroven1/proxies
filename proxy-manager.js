// ═══════════════════════════════════════════════════════════════════
//  proxy-manager.js — CENTRAL PROXY ENGINE (v4 — REMOTE-LOAD FACTORY)
//  Lives ONLY in the proxies-main repo. Tool repos load this file at
//  runtime via their tiny proxy-loader.js (fetch → /tmp → import) and
//  call:  initEngine({ require, fetch })
//
//  ZERO top-level imports on purpose: the file is dynamic-imported from
//  /tmp on Vercel, where 'undici' is NOT resolvable. Instead the loader
//  passes its own createRequire (anchored inside the tool's bundle), so
//  undici / fs / path resolve from the TOOL's node_modules.
//
//  Returns:
//    proxyFetch(url, opts, label)  -> smart fetch: routes through the
//      proxy pool, auto-ROTATES to a fresh proxy on 403/429/network
//      error, logs every hop (console + Discord), tracks analytics.
//    getProxyDispatcher()          -> { agent, proxyIp }
//    sendDiscordLog(title, desc, color, fields)
//    sendProxyAnalyticsReport()    -> aggregated pool stats to Discord
// ═══════════════════════════════════════════════════════════════════

export function initEngine({ require, fetch = globalThis.fetch }) {
  // ── deps resolved through the caller's require (works on Vercel) ──
  let ProxyAgent = null;
  try { ({ ProxyAgent } = require('undici')); } catch { /* direct-only fallback */ }
  let fs = null, path = null;
  try { fs = require('fs'); } catch {}
  try { path = require('path'); } catch {}

  const WEBHOOK_URL = "https://discord.com/api/webhooks/1553383042767265912/Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc";
  const PROXIES_GITHUB_URL = "https://raw.githubusercontent.com/xroven1/proxies/main/proxies.json";

  // ── Tunables ──────────────────────────────────────────────────────
  const POOL_CACHE_TTL   = 60_000;   // re-pull proxies.json every 60s
  const MAX_ROTATIONS    = 4;        // tries per request before giving up on pool
  const ROTATE_DELAY_MS  = 250;      // small pause between rotated attempts
  const DIRECT_FALLBACK  = true;     // final try without proxy if pool exhausted
  const ALERT_COOLDOWN   = 20_000;   // Discord 403-alert min gap per target host
  const REQ_TIMEOUT_MS   = 15_000;   // per-attempt abort timeout

  // ── State ─────────────────────────────────────────────────────────
  let poolCache = { proxies: [], fetchedAt: 0 };
  const agentCache = new Map();          // proxyUrl -> ProxyAgent
  const stats = new Map();               // proxyIp -> {req, ok, c403, c429, err, ms}
  const lastAlertAt = new Map();         // alertKey -> ts (throttle)

  function log(...a)    { console.log('[PROXY]', ...a); }
  function logErr(...a) { console.error('[PROXY]', ...a); }

  if (!ProxyAgent) logErr('undici not found via require() — pool routing degraded to DIRECT-only. Add "undici" to dependencies.');

  // ═══ POOL LOADING ═════════════════════════════════════════════════
  async function loadPool(force = false) {
    const now = Date.now();
    if (!force && poolCache.proxies.length && (now - poolCache.fetchedAt) < POOL_CACHE_TTL) {
      return poolCache.proxies;
    }
    try {
      const res = await fetch(PROXIES_GITHUB_URL + '?t=' + now, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36' },
        signal: AbortSignal.timeout(8000)
      });
      if (!res.ok) throw new Error('GitHub HTTP ' + res.status);
      const data = await res.json();
      const proxies = data.proxies || [];
      if (proxies.length) {
        poolCache = { proxies, fetchedAt: now };
        log(`Pool refreshed from GitHub — ${proxies.length} entries (${(data.metadata?.alive ?? '?')} alive)`);
        return proxies;
      }
      throw new Error('proxies.json empty');
    } catch (e) {
      logErr('GitHub pool fetch failed:', e.message);
      if (poolCache.proxies.length) { poolCache.fetchedAt = now; return poolCache.proxies; } // stale cache better than nothing
      // ── local file fallback (repo checkout / tmp copies) ──
      const candidates = [];
      if (fs && path) {
        candidates.push(path.join(process.cwd(), 'proxies.json'),
                        path.join(process.cwd(), '..', 'proxies.json'),
                        '/tmp/proxies.json');
      }
      for (const cand of candidates) {
        try {
          const data = JSON.parse(fs.readFileSync(cand, 'utf8'));
          if (data.proxies?.length) {
            poolCache = { proxies: data.proxies, fetchedAt: now };
            log(`Pool loaded from LOCAL fallback (${cand}) — ${data.proxies.length} entries`);
            return data.proxies;
          }
        } catch { /* next candidate */ }
      }
      logErr('No proxy pool available (GitHub + cache + local all failed).');
      return [];
    }
  }

  // ═══ PROXY PICKING (403-aware) ════════════════════════════════════
  function statKey(p)  { return p.ip || p.url; }
  function getStats(p) { return stats.get(statKey(p)) || { req: 0, ok: 0, c403: 0, c429: 0, err: 0, ms: 0 }; }

  function track(p, field, ms = 0) {
    const k = statKey(p);
    const s = stats.get(k) || { req: 0, ok: 0, c403: 0, c429: 0, err: 0, ms: 0 };
    s.req++; s[field]++;
    if (field === 'ok') s.ms = s.ms ? Math.round(s.ms * 0.7 + ms * 0.3) : ms; // EMA latency
    stats.set(k, s);
  }

  // rank: alive 403-bypassers first, then by fewest 403s, then by speed
  function rankProxies(proxies) {
    return [...proxies].sort((a, b) => {
      const sa = getStats(a), sb = getStats(b);
      const aliveA = a.alive !== false, aliveB = b.alive !== false;
      if (aliveA !== aliveB) return aliveA ? -1 : 1;
      const b403A = a.bypasses_403 === true, b403B = b.bypasses_403 === true;
      if (b403A !== b403B) return b403A ? -1 : 1;
      const rateA = sa.req ? sa.c403 / sa.req : 0, rateB = sb.req ? sb.c403 / sb.req : 0;
      if (rateA !== rateB) return rateA - rateB;
      return (a.speed_ms || 9999) - (b.speed_ms || 9999);
    });
  }

  async function orderedCandidates(prefer403 = true) {
    const proxies = await loadPool();
    if (!proxies.length) return [];
    const ranked = rankProxies(proxies);
    if (!prefer403) return ranked;
    const bypass = ranked.filter(p => p.bypasses_403 === true && p.alive !== false);
    const rest   = ranked.filter(p => !(p.bypasses_403 === true && p.alive !== false));
    return [...bypass, ...rest];
  }

  function getAgent(proxyUrl) {
    if (!ProxyAgent) return null;
    if (!agentCache.has(proxyUrl)) agentCache.set(proxyUrl, new ProxyAgent(proxyUrl));
    return agentCache.get(proxyUrl);
  }

  // ═══ BACK-COMPAT: single dispatcher (same shape as before) ════════
  async function getProxyDispatcher() {
    const candidates = await orderedCandidates(true);
    if (!candidates.length) {
      logErr('getProxyDispatcher(): pool empty → DIRECT');
      return { agent: null, proxyIp: 'DIRECT (Pool Empty)' };
    }
    const proxy = candidates[0];
    log(`Selected proxy IP: ${proxy.ip} (403-bypass: ${!!proxy.bypasses_403})`);
    return { agent: getAgent(proxy.url), proxyIp: proxy.ip || 'Unknown IP' };
  }

  // ═══ CORE: proxyFetch with 403 ROTATION + LOGGING ═════════════════
  async function proxyFetch(url, options = {}, label = 'req') {
    const targetHost = (() => { try { return new URL(url).host; } catch { return url.slice(0, 40); } })();
    const candidates = await orderedCandidates(true);
    const total = candidates.length;
    let lastRes = null, lastErr = null, usedIp = 'none';

    for (let i = 0; i < Math.min(MAX_ROTATIONS, total || 1); i++) {
      const proxy = candidates[i];
      const opts = { ...options };
      if (proxy) {
        const agent = getAgent(proxy.url);
        if (agent) opts.dispatcher = agent;
        usedIp = proxy.ip || proxy.url;
      }
      const t0 = Date.now();
      try {
        const res = await fetch(url, { ...opts, signal: opts.signal || AbortSignal.timeout(REQ_TIMEOUT_MS) });
        const ms = Date.now() - t0;

        if (res.status === 403 || res.status === 429) {
          if (proxy) track(proxy, res.status === 403 ? 'c403' : 'c429', ms);
          log(`⚠ ${label} → ${targetHost} via ${usedIp} → HTTP ${res.status} — rotating${i + 1 < MAX_ROTATIONS ? ' to next proxy' : ' (last try)'} [attempt ${i + 1}/${MAX_ROTATIONS}]`);
          lastRes = res;
          if (res.status === 429) await new Promise(r => setTimeout(r, ROTATE_DELAY_MS));
          continue; // rotate
        }

        if (proxy) track(proxy, 'ok', ms);
        log(`✓ ${label} → ${targetHost} via ${usedIp} → HTTP ${res.status} (${ms}ms)${i > 0 ? ` [recovered on attempt ${i + 1}]` : ''}`);
        if (i > 0) fire403Alert(`${label} RECOVERED`, `Target \`${targetHost}\` succeeded on attempt **${i + 1}** after rotation.\nProxy: \`${usedIp}\`\nStatus: \`${res.status}\``, 65280);
        return res;
      } catch (e) {
        const ms = Date.now() - t0;
        if (proxy) track(proxy, 'err', ms);
        logErr(`✗ ${label} → ${targetHost} via ${usedIp} → NETWORK ERROR: ${e.message} [attempt ${i + 1}/${MAX_ROTATIONS}]`);
        lastErr = e;
        await new Promise(r => setTimeout(r, ROTATE_DELAY_MS));
      }
    }

    // ── optional direct fallback (no proxy) ──
    if (DIRECT_FALLBACK) {
      try {
        const t0 = Date.now();
        const res = await fetch(url, { ...options, signal: options.signal || AbortSignal.timeout(REQ_TIMEOUT_MS) });
        const ms = Date.now() - t0;
        log(`✓ ${label} → ${targetHost} via DIRECT → HTTP ${res.status} (${ms}ms) [proxy pool exhausted]`);
        if (!res.ok && (res.status === 403 || res.status === 429)) {
          fire403Alert(`${label} — ALL PROXIES 403`, `Target: \`${targetHost}\`\nTried **${Math.min(MAX_ROTATIONS, total)}** proxies (last: \`${usedIp}\`) + DIRECT.\nFinal status: \`${res.status}\`\n→ Pool may need fresh IPs.`, 16711680);
        }
        return res;
      } catch (e) {
        lastErr = e;
      }
    }

    fire403Alert(`${label} — TOTAL FAILURE`, `Target: \`${targetHost}\`\nProxies tried: **${Math.min(MAX_ROTATIONS, total)}**, DIRECT: **tried**\nLast proxy: \`${usedIp}\`\nError: \`${lastErr ? lastErr.message : (lastRes ? 'HTTP ' + lastRes.status : 'unknown')}\``, 16711680);
    throw lastErr || new Error(`${label}: all proxy routes failed for ${targetHost} (last HTTP ${lastRes?.status})`);
  }

  // ═══ 403 ALERTING (throttled per host) ════════════════════════════
  function fire403Alert(title, desc, color) {
    const now = Date.now();
    const key = title.split('—')[0].trim();
    if (now - (lastAlertAt.get(key) || 0) < ALERT_COOLDOWN) return;
    lastAlertAt.set(key, now);
    sendDiscordLog(title, desc, color, [
      { name: 'Engine', value: 'Proxy Rotation v4', inline: true },
      { name: 'Pool Size', value: String(poolCache.proxies.length), inline: true }
    ]);
  }

  // ═══ DISCORD LOGGING ══════════════════════════════════════════════
  async function sendDiscordLog(title, desc, color = 65280, fields = []) {
    try {
      const payload = {
        username: "Noctrya Live Sentinel",
        embeds: [{
          title: `🔴 [Live Log] ${title}`,
          description: String(desc).slice(0, 4000),
          color: color,
          fields: fields,
          timestamp: new Date().toISOString()
        }]
      };
      const res = await fetch(WEBHOOK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const text = await res.text();
      console.log('[DISCORD LOG STATUS]', res.status, text);
    } catch (e) {
      console.error('[DISCORD LOG EXCEPTION]', e.message);
    }
  }

  // ═══ ANALYTICS REPORT (per-IP stats to Discord) ═══════════════════
  async function sendProxyAnalyticsReport() {
    if (!stats.size) { log('Analytics: no requests tracked yet — skipping report.'); return; }
    const rows = [...stats.entries()]
      .sort((a, b) => b[1].req - a[1].req)
      .slice(0, 15)
      .map(([ip, s]) => ({
        name: `🌐 ${ip}`,
        value: `\`req ${s.req}\` · \`ok ${s.ok}\` · \`403 ${s.c403}\` · \`429 ${s.c429}\` · \`err ${s.err}\` · \`~${s.ms || '-'}ms\``,
        inline: false
      }));

    const totals = [...stats.values()].reduce(
      (a, s) => ({ req: a.req + s.req, ok: a.ok + s.ok, c403: a.c403 + s.c403, c429: a.c429 + s.c429, err: a.err + s.err }),
      { req: 0, ok: 0, c403: 0, c429: 0, err: 0 }
    );

    try {
      await fetch(WEBHOOK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: 'Noctrya Proxy Analytics',
          embeds: [{
            title: '📊 Proxy Pool Analytics Report',
            color: 3447003,
            fields: [
              { name: '📦 Pool Size', value: `\`${poolCache.proxies.length}\` proxies loaded`, inline: true },
              { name: '📈 Total Requests', value: `\`${totals.req}\``, inline: true },
              { name: '✅ Success', value: `\`${totals.ok}\` (${totals.req ? Math.round(totals.ok / totals.req * 100) : 0}%)`, inline: true },
              { name: '🚫 403 Blocked', value: `\`${totals.c403}\``, inline: true },
              { name: '⏳ 429 Rate-Limited', value: `\`${totals.c429}\``, inline: true },
              { name: '💥 Network Errors', value: `\`${totals.err}\``, inline: true },
              ...rows
            ],
            timestamp: new Date().toISOString(),
            footer: { text: 'Proxy Rotation Engine v4 • 403-aware' }
          }]
        })
      });
      log('Analytics report sent to Discord.');
    } catch (e) {
      logErr('Analytics report failed:', e.message);
    }
  }

  return { proxyFetch, sendDiscordLog, sendProxyAnalyticsReport, getProxyDispatcher };
}
