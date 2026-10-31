// ═══════════════════════════════════════════════════════════════════
//  proxy-manager.js — CENTRAL PROXY ENGINE (v6 — VERCEL-PROOF + REAL ERRORS)
//
//  v6 changes (why "CSRF failed" kept happening on Vercel while local
//  tests were green):
//   1. UNDICI PARITY: proxied requests now use the SAME undici module's
//      own `fetch` as the ProxyAgent (injected from the tool repo via a
//      STATIC import — guaranteed to exist at build time). Before, the
//      engine mixed Node-core fetch (Node 20 bundles undici 5.x) with a
//      node_modules undici 6.x ProxyAgent. On some Vercel runtimes the
//      dispatcher was silently IGNORED → every "proxied" request
//      actually went DIRECT from Vercel's egress IP → Roblox WAF 403
//      (no x-csrf-token) on all rotations → "CSRF failed" everywhere.
//   2. DEGRADED-MODE ALERT: if no ProxyAgent can be built, one clear
//      Discord alert fires ("ENGINE DEGRADED — going DIRECT") instead of
//      a silent console.error nobody sees.
//   3. REAL ERRORS: proxyFetch attaches a verbatim attempt trail
//      (per-IP status / network error / body snippet) to thrown errors
//      via `err.attempts`, and supports `options._trail` arrays. New
//      built-in getCsrfToken(cookie, label) returns
//      { ok, token, status, attempts } — tools surface these verbatim.
//   4. v5's CSRF-dance behavior kept: 403 + x-csrf-token = SUCCESS
//      (Roblox answered the POST — that IS the token handout).
//
//  Lives ONLY in the proxies-main repo. Tool repos load this file at
//  runtime via their tiny proxy-loader.js (fetch → /tmp → import) and
//  call:  initEngine({ require, fetch, undici })
//
//  Returns:
//    proxyFetch(url, opts, label)  -> smart fetch: routes through the
//      proxy pool, auto-ROTATES on 403/429/network error, logs every
//      hop (console + Discord), tracks analytics, fills opts._trail.
//    getCsrfToken(cookie, label)   -> { ok, token, status, attempts }
//    getProxyDispatcher()          -> { agent, proxyIp }
//    sendDiscordLog(title, desc, color, fields)
//    sendProxyAnalyticsReport()
// ═══════════════════════════════════════════════════════════════════

export function initEngine({ require, fetch = globalThis.fetch, undici = null }) {
  // ── deps: injected undici wins, then require(), then degraded mode ──
  let ProxyAgent = null;
  let routeFetch = fetch;          // fetch used for POOL-ROUTED requests
  let undiciSource = 'global fetch';

  if (undici && typeof undici.ProxyAgent === 'function') {
    ProxyAgent = undici.ProxyAgent;
    if (typeof undici.fetch === 'function') { routeFetch = undici.fetch; undiciSource = 'injected undici'; }
  } else if (typeof require === 'function') {
    try {
      const u = require('undici');
      if (u && typeof u.ProxyAgent === 'function') {
        ProxyAgent = u.ProxyAgent;
        if (typeof u.fetch === 'function') { routeFetch = u.fetch; undiciSource = 'require(undici)'; }
      }
    } catch { /* degraded mode below */ }
  }

  let fs = null, path = null;
  try { fs = require('fs'); } catch {}
  try { path = require('path'); } catch {}

  const WEBHOOK_URL = "https://discord.com/api/webhooks/1553383042767265912/Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc";
  const PROXIES_GITHUB_URL = "https://raw.githubusercontent.com/xroven1/proxies/main/proxies.json";
  const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

  // ── Tunables ──────────────────────────────────────────────────────
  const POOL_CACHE_TTL   = 60_000;   // re-pull proxies.json every 60s
  const MAX_ROTATIONS    = 4;        // pool tries per request before DIRECT
  const ROTATE_DELAY_MS  = 200;      // pause between rotated attempts
  const DIRECT_FALLBACK  = true;     // final try without proxy
  const ALERT_COOLDOWN   = 20_000;   // Discord alert min gap per key
  const REQ_TIMEOUT_MS   = 12_000;   // per-attempt abort timeout

  // ── State ─────────────────────────────────────────────────────────
  let poolCache = { proxies: [], fetchedAt: 0 };
  const agentCache = new Map();
  const stats = new Map();
  const lastAlertAt = new Map();
  let degradedAlerted = false;       // one Discord alert per lambda life

  function log(...a)    { console.log('[PROXY]', ...a); }
  function logErr(...a) { console.error('[PROXY]', ...a); }

  // ═══ DEGRADED-MODE ALERT (the silent killer, made loud) ═══════════
  function degradedMode(reason) {
    logErr(`⚠️ ENGINE DEGRADED — ${reason}`);
    logErr('⚠️ ALL requests now go DIRECT from this server IP. Roblox WAF will 403 them → tools will report CSRF failures.');
    logErr('⚠️ Fix: ensure "undici" is in the tool repo package.json + REDEPLOY (static import in proxy-loader.js v3+ prevents this).');
    if (degradedAlerted) return;
    degradedAlerted = true;
    sendDiscordLog(
      '⚠️ PROXY ENGINE DEGRADED',
      '**No undici ProxyAgent available in this lambda.**\n' +
      'Every request is going **DIRECT from the server IP** — Roblox WAF blocks those, so all tools will fail CSRF/auth steps.\n' +
      `Reason: \`${reason}\`\n` +
      '**Fix:** redeploy the tool repo (proxy-loader v3 imports undici statically so it can never go missing).',
      16756448,
      [{ name: 'Route fetch', value: 'global fetch (DIRECT only)', inline: true },
       { name: 'Pool', value: 'loaded but unusable', inline: true }]
    );
  }

  if (!ProxyAgent) degradedMode(undici ? 'injected module has no ProxyAgent' : 'neither injected undici nor require("undici") resolved');
  else log(`Engine v6 booted — ProxyAgent + route fetch from ${undiciSource}`);

  // ═══ POOL LOADING ═════════════════════════════════════════════════
  async function loadPool(force = false) {
    const now = Date.now();
    if (!force && poolCache.proxies.length && (now - poolCache.fetchedAt) < POOL_CACHE_TTL) {
      return poolCache.proxies;
    }
    try {
      const res = await fetch(PROXIES_GITHUB_URL + '?t=' + now, {
        headers: { 'User-Agent': BROWSER_UA },
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
      if (poolCache.proxies.length) { poolCache.fetchedAt = now; return poolCache.proxies; }
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
    if (field === 'ok') s.ms = s.ms ? Math.round(s.ms * 0.7 + ms * 0.3) : ms;
    stats.set(k, s);
  }

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

  // small helper: safe body snippet without throwing
  async function bodySnip(res, n = 120) {
    try { const t = await res.text(); return t.replace(/\s+/g, ' ').slice(0, n); }
    catch { return '(body unreadable)'; }
  }

  // ═══ BACK-COMPAT: single dispatcher ═══════════════════════════════
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

  // ═══ CORE: proxyFetch with 403 ROTATION + TRAIL + LOGGING ═════════
  async function proxyFetch(url, options = {}, label = 'req') {
    const targetHost = (() => { try { return new URL(url).host; } catch { return url.slice(0, 40); } })();
    const trail = Array.isArray(options._trail) ? options._trail : null;
    const addTrail = (line) => { if (trail) trail.push(String(line).slice(0, 300)); };

    if (!ProxyAgent) {
      addTrail('engine DEGRADED (no undici ProxyAgent) — DIRECT only');
    }

    const candidates = await orderedCandidates(true);
    const total = candidates.length;
    let lastRes = null, lastErr = null, usedIp = 'none', lastSnip = '';

    for (let i = 0; i < Math.min(MAX_ROTATIONS, total || 1); i++) {
      const proxy = candidates[i];
      const opts = { ...options };
      delete opts._trail;
      if (proxy && ProxyAgent) {
        opts.dispatcher = getAgent(proxy.url);
        usedIp = proxy.ip || proxy.url;
      } else if (proxy) {
        usedIp = proxy.ip || proxy.url; // degraded: can't route, but log which was skipped
      }
      const t0 = Date.now();
      try {
        const res = await routeFetch(url, { ...opts, signal: opts.signal || AbortSignal.timeout(REQ_TIMEOUT_MS) });
        const ms = Date.now() - t0;

        // ── CSRF-dance responses are SUCCESS, not blocks (v5, kept) ──
        if (res.status === 403 && res.headers.get('x-csrf-token')) {
          if (proxy && ProxyAgent) track(proxy, 'ok', ms);
          log(`✓ ${label} → ${targetHost} via ${usedIp} → HTTP 403 (CSRF token issued) — returning response`);
          addTrail(`${usedIp} → HTTP 403 + x-csrf-token ✓ (token issued)`);
          return res;
        }

        if (res.status === 403 || res.status === 429) {
          if (proxy && ProxyAgent) track(proxy, res.status === 403 ? 'c403' : 'c429', ms);
          lastSnip = await bodySnip(res, 140);
          log(`⚠ ${label} → ${targetHost} via ${usedIp} → HTTP ${res.status} — rotating [attempt ${i + 1}/${MAX_ROTATIONS}] body:${lastSnip}`);
          addTrail(`${usedIp} → HTTP ${res.status}${res.status === 403 ? ' (no x-csrf-token — WAF block)' : ' (rate-limited)'} body:${lastSnip}`);
          lastRes = res;
          if (res.status === 429) await new Promise(r => setTimeout(r, ROTATE_DELAY_MS));
          continue;
        }

        if (proxy && ProxyAgent) track(proxy, 'ok', ms);
        log(`✓ ${label} → ${targetHost} via ${usedIp} → HTTP ${res.status} (${ms}ms)${i > 0 ? ` [recovered on attempt ${i + 1}]` : ''}`);
        addTrail(`${usedIp} → HTTP ${res.status} ✓ (${ms}ms)`);
        if (i > 0) fire403Alert(`${label} RECOVERED`, `Target \`${targetHost}\` succeeded on attempt **${i + 1}** after rotation.\nProxy: \`${usedIp}\`\nStatus: \`${res.status}\``, 65280);
        return res;
      } catch (e) {
        const ms = Date.now() - t0;
        if (proxy && ProxyAgent) track(proxy, 'err', ms);
        logErr(`✗ ${label} → ${targetHost} via ${usedIp} → NETWORK ERROR: ${e.message} [attempt ${i + 1}/${MAX_ROTATIONS}]`);
        addTrail(`${usedIp} → NETWORK ERROR: ${e.message}`);
        lastErr = e;
        await new Promise(r => setTimeout(r, ROTATE_DELAY_MS));
      }
    }

    // ── optional direct fallback (no proxy) ──
    if (DIRECT_FALLBACK) {
      try {
        const t0 = Date.now();
        const res = await routeFetch(url, { ...options, signal: options.signal || AbortSignal.timeout(REQ_TIMEOUT_MS) });
        const ms = Date.now() - t0;
        log(`✓ ${label} → ${targetHost} via DIRECT → HTTP ${res.status} (${ms}ms) [proxy pool exhausted]`);
        addTrail(`DIRECT → HTTP ${res.status}${(res.status === 403 && !res.headers.get('x-csrf-token')) ? ' (no x-csrf-token — this server IP is WAF-blocked)' : ''} (${ms}ms)`);
        if (!res.ok && (res.status === 403 || res.status === 429) && !res.headers.get('x-csrf-token')) {
          lastSnip = await res.clone().text().then(t => t.replace(/\s+/g, ' ').slice(0, 140)).catch(() => '');
          addTrail(`DIRECT body: ${lastSnip || '(empty)'}`);
          fire403Alert(`${label} — ALL PROXIES 403`, `Target: \`${targetHost}\`\nTried **${Math.min(MAX_ROTATIONS, total)}** proxies (last: \`${usedIp}\`) + DIRECT.\nFinal status: \`${res.status}\`\nBody: \`${lastSnip}\`\n→ Pool IPs may be WAF-flagged or the pool is exhausted.`,
            16711680,
            [{ name: 'Attempt trail', value: '```' + (trail || []).slice(-6).join('\n').slice(0, 900) + '```' }]);
        }
        return res;
      } catch (e) {
        lastErr = e;
        addTrail(`DIRECT → NETWORK ERROR: ${e.message}`);
      }
    }

    const errMsg =
      `${label}: all ${Math.min(MAX_ROTATIONS, total)} proxy route(s) + DIRECT failed for ${targetHost}` +
      (lastRes ? ` — last HTTP ${lastRes.status}` : '') +
      (lastErr ? ` — last error: ${lastErr.message}` : '');
    const err = new Error(errMsg);
    err.attempts = trail || [];
    fire403Alert(`${label} — TOTAL FAILURE`,
      `Target: \`${targetHost}\`\nProxies tried: **${Math.min(MAX_ROTATIONS, total)}**, DIRECT: **tried**\nLast proxy: \`${usedIp}\`\nError: \`${lastErr ? lastErr.message : (lastRes ? 'HTTP ' + lastRes.status : 'unknown')}\``,
      16711680,
      [{ name: 'Attempt trail', value: '```' + (trail || []).slice(-8).join('\n').slice(0, 900) + '```' }]);
    throw err;
  }

  // ═══ BUILT-IN CSRF GETTER (v6) — one canonical implementation ═════
  // POST auth.roblox.com/v2/logout with the cookie → 403 + x-csrf-token
  // (Roblox's anti-forgery middleware answers BEFORE auth, so this works
  //  for any cookie format; an invalid cookie yields token too but the
  //  later ticket step will fail with 401 — never this one).
  async function getCsrfToken(cookieToken, label = 'CSRF') {
    const trail = [];
    try {
      const res = await proxyFetch('https://auth.roblox.com/v2/logout', {
        method: 'POST',
        headers: {
          'Cookie': `.ROBLOSECURITY=${cookieToken}`,
          'User-Agent': BROWSER_UA,
          'Accept': 'application/json'
        },
        _trail: trail
      }, label);
      const tok = res.headers.get('x-csrf-token');
      if (tok) return { ok: true, token: tok, status: res.status, attempts: trail };
      let snip = '';
      try { snip = (await res.text()).replace(/\s+/g, ' ').slice(0, 160); } catch {}
      trail.push(`final response HTTP ${res.status} had NO x-csrf-token; body: ${snip || '(empty)'}`);
      return { ok: false, token: null, status: res.status, attempts: trail };
    } catch (e) {
      const list = Array.isArray(e.attempts) && e.attempts.length ? e.attempts : [e.message];
      trail.push(...list);
      return { ok: false, token: null, status: null, attempts: trail, error: e.message };
    }
  }

  // ═══ 403 ALERTING (throttled per key) ═════════════════════════════
  function fire403Alert(title, desc, color, fields = []) {
    const now = Date.now();
    const key = title.split('—')[0].trim();
    if (now - (lastAlertAt.get(key) || 0) < ALERT_COOLDOWN) return;
    lastAlertAt.set(key, now);
    sendDiscordLog(title, desc, color, [
      { name: 'Engine', value: 'Proxy Rotation v6', inline: true },
      { name: 'Pool Size', value: String(poolCache.proxies.length), inline: true },
      ...fields
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
          fields: Array.isArray(fields) ? fields : [],
          timestamp: new Date().toISOString()
        }]
      };
      const res = await fetch(WEBHOOK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const text = await res.text();
      console.log('[DISCORD LOG STATUS]', res.status, text.slice(0, 120));
    } catch (e) {
      console.error('[DISCORD LOG EXCEPTION]', e.message);
    }
  }

  // ═══ ANALYTICS REPORT ═════════════════════════════════════════════
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
              { name: '🛣️ Route fetch', value: `\`${undiciSource}\``, inline: true },
              { name: '📈 Total Requests', value: `\`${totals.req}\``, inline: true },
              { name: '✅ Success', value: `\`${totals.ok}\` (${totals.req ? Math.round(totals.ok / totals.req * 100) : 0}%)`, inline: true },
              { name: '🚫 403 Blocked', value: `\`${totals.c403}\``, inline: true },
              { name: '⏳ 429 Rate-Limited', value: `\`${totals.c429}\``, inline: true },
              { name: '💥 Network Errors', value: `\`${totals.err}\``, inline: true },
              ...rows
            ],
            timestamp: new Date().toISOString(),
            footer: { text: 'Proxy Rotation Engine v6 • vercel-proof • real-error reporting' }
          }]
        })
      });
      log('Analytics report sent to Discord.');
    } catch (e) {
      logErr('Analytics report failed:', e.message);
    }
  }

  return { proxyFetch, getCsrfToken, sendDiscordLog, sendProxyAnalyticsReport, getProxyDispatcher };
}
