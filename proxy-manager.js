// proxy-manager.js (Fixed to force synchronous logging and verify payload delivery)
import { ProxyAgent } from 'undici';

const WEBHOOK_URL = "https://discord.com/api/webhooks/1553383042767265912/Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc";
const PROXIES_GITHUB_URL = "https://raw.githubusercontent.com/xroven1/proxies/main/proxies.json";

let cachedProxies = [];
let lastLoad = 0;
const CACHE_TTL = 300000;
const failedCounts = {};

export async function getProxyDispatcher() {
  const now = Date.now();
  if (now - lastLoad > CACHE_TTL || cachedProxies.length === 0) {
    try {
      const res = await fetch(PROXIES_GITHUB_URL);
      if (res.ok) {
        const data = await res.json();
        cachedProxies = data.proxies || [];
        lastLoad = now;
      }
    } catch (e) {
      console.error('[PROXY FETCH ERROR]', e.message);
    }
  }

  if (cachedProxies.length === 0) return null;

  let alive = cachedProxies.filter(p => p.alive !== false);
  if (alive.length === 0) alive = cachedProxies;

  const proxy = alive[Math.floor(Math.random() * alive.length)];
  const agent = new ProxyAgent(proxy.url);
  agent.proxyIp = proxy.ip;
  agent.proxyUrl = proxy.url;
  return agent;
}

export function reportProxyFailure(agent, status, reason) {
  if (!agent || !agent.proxyIp) return;
  const key = `${agent.proxyIp} (${status || 'ERR'})`;
  failedCounts[key] = (failedCounts[key] || 0) + 1;
}

export async function sendProxyAnalyticsReport(actionName = "Routine Check", statusDetails = null) {
  try {
    const res = await fetch(PROXIES_GITHUB_URL);
    if (!res.ok) {
      console.error('[ANALYTICS] Failed to fetch proxies JSON, status:', res.status);
      return;
    }
    const data = await res.json();
    const proxies = data.proxies || [];
    const total = proxies.length;

    let aliveCount = proxies.filter(p => p.alive !== false).length;
    let deadCount = total - aliveCount;

    const failedLogFormatted = Object.keys(failedCounts).length > 0
      ? Object.entries(failedCounts).map(([k, v]) => `${k}: ${v} failures`).join('\n')
      : 'No failure logs captured.';

    const embed = {
      title: `📊 Proxy Sentinel — ${actionName}`,
      color: 65280,
      fields: [
        { name: "📦 Total Proxies", value: "`" + total + "`", inline: true },
        { name: "🟢 Alive", value: "`" + aliveCount + "`", inline: true },
        { name: "🔴 Dead", value: "`" + deadCount + "`", inline: true },
        { name: "⚠️ Error Logs", value: "```" + failedLogFormatted.slice(0, 900) + "```", inline: false }
      ],
      timestamp: new Date().toISOString()
    };

    if (statusDetails) {
      embed.fields.unshift({ name: "📌 Status Details", value: String(statusDetails), inline: false });
    }

    const discordRes = await fetch(WEBHOOK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: "Noctrya Proxy Monitor", embeds: [embed] })
    });

    if (!discordRes.ok) {
      const errText = await discordRes.text();
      console.error('[DISCORD WEBHOOK ERROR]', discordRes.status, errText);
    }
  } catch (e) {
    console.error('[ANALYTICS ERROR EXCEPTION]', e.message);
  }
}
