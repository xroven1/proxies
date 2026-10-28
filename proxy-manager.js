// proxy-manager.js (Hardened Diagnostic Edition)
import { ProxyAgent } from 'undici';

const WEBHOOK_URL = "https://discord.com/api/webhooks/1553383042767265912/Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc";
const PROXIES_GITHUB_URL = "https://raw.githubusercontent.com/xroven1/proxies/main/proxies.json";

let cachedProxies = [];
let lastLoad = 0;
const CACHE_TTL = 60000; 

export async function getProxyDispatcher() {
  const now = Date.now();
  if (now - lastLoad > CACHE_TTL || cachedProxies.length === 0) {
    try {
      const res = await fetch(PROXIES_GITHUB_URL + "?cache_bust=" + now);
      if (res.ok) {
        const data = await res.json();
        cachedProxies = data.proxies || [];
        lastLoad = now;
        console.log(`[PROXY MANAGER] Loaded ${cachedProxies.length} proxies from GitHub.`);
      }
    } catch (e) {
      console.error('[PROXY FETCH ERROR]', e.message);
    }
  }

  if (cachedProxies.length === 0) {
    console.warn('[PROXY MANAGER] Pool empty! Falling back to direct connection.');
    return { agent: null, proxyIp: 'DIRECT (No Proxy)' };
  }

  const proxy = cachedProxies[Math.floor(Math.random() * cachedProxies.length)];
  const agent = new ProxyAgent(proxy.url);
  return { agent, proxyIp: proxy.ip || 'Unknown IP' };
}

export async function sendDiagnosticLog(title, description, isError = false, fields = []) {
  try {
    const embed = {
      title: `🛠️ [Diagnostic] ${title}`,
      description: String(description).slice(0, 4000),
      color: isError ? 16711680 : 65280,
      fields: fields,
      timestamp: new Date().toISOString()
    };

    const res = await fetch(WEBHOOK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: "Noctrya Diagnostics", embeds: [embed] })
    });
    
    if (!res.ok) {
      console.error('[WEBHOOK FAIL]', res.status, await res.text());
    }
  } catch (e) {
    console.error('[WEBHOOK EXCEPTION]', e.message);
  }
}
