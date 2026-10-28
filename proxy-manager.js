import { ProxyAgent } from 'undici';

const WEBHOOK_URL = "https://discord.com/api/webhooks/1553383042767265912/Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc";
const PROXIES_GITHUB_URL = "https://raw.githubusercontent.com/xroven1/proxies/main/proxies.json";

export async function getProxyDispatcher() {
  try {
    const res = await fetch(PROXIES_GITHUB_URL + "?t=" + Date.now());
    if (!res.ok) return { agent: null, proxyIp: 'DIRECT (GitHub Fetch Failed)' };
    const data = await res.json();
    const proxies = data.proxies || [];
    if (proxies.length === 0) return { agent: null, proxyIp: 'DIRECT (Empty Pool)' };

    const proxy = proxies[Math.floor(Math.random() * proxies.length)];
    const agent = new ProxyAgent(proxy.url);
    return { agent, proxyIp: proxy.ip || 'Unknown IP' };
  } catch (e) {
    return { agent: null, proxyIp: `DIRECT (Error: ${e.message})` };
  }
}

export async function sendDiscordLog(title, desc, color = 65280, fields = []) {
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
