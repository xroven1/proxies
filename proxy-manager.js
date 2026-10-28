// proxy-manager.js
import { ProxyAgent } from 'undici';

const WEBHOOK_URL = "https://discord.com/api/webhooks/1553383042767265912/Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc";
const PROXIES_GITHUB_URL = "https://raw.githubusercontent.com/xroven1/proxies/main/proxies.json";

let cachedProxies = [];
let lastLoad = 0;
const CACHE_TTL = 300000; // 5 minutes
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

  let alive = cachedProxies.filter(p => p.alive !== false && (p.success_rate ?? 1.0) >= 0.5);
  if (alive.length === 0) alive = cachedProxies;

  const proxy = alive[Math.floor(Math.random() * alive.length)];
  
  // Return a direct ProxyAgent instance so undici recognizes the dispatcher
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
    if (!res.ok) return;
    const data = await res.json();
    const proxies = data.proxies || [];
    const total = proxies.length;

    let aliveCount = 0;
    let deadCount = 0;

    for (const p of proxies) {
      if (p.alive === false || p.success_rate < 0.5) {
        deadCount++;
      } else {
        aliveCount++;
      }
    }

    const estimatedCapacityRequests = total * 10000;
    const lowPoolWarning = total < 15 || (aliveCount / total) < 0.4;

    let recommendation = "✅ Proxy pool health is optimal. No action required.";
    if (deadCount > aliveCount) {
      recommendation = "⚠️ High proxy failure rate detected! Consider updating your proxy pool.";
    } else if (lowPoolWarning) {
      recommendation = "🚨 Active proxy count is running low. You should add more proxies soon.";
    }

    const failedLogFormatted = Object.keys(failedCounts).length > 0
      ? Object.entries(failedCounts).map(([k, v]) => `${k}: ${v} failures`).join('\n')
      : 'No failure logs captured in current runtime.';

    const embed = {
      title: `📊 Proxy Sentinel Report — ${actionName}`,
      color: lowPoolWarning ? 16711680 : 65280,
      fields: [
        { name: "📦 Total Proxies", value: "`" + total + "`", inline: true },
        { name: "🟢 Alive / Operational", value: "`" + aliveCount + "`", inline: true },
        { name: "🔴 Dead / Flagged", value: "`" + deadCount + "`", inline: true },
        { name: "🔋 Est. Bandwidth Capacity", value: "`~1 GB (~" + estimatedCapacityRequests.toLocaleString() + " reqs)`", inline: false },
        { name: "⚠️ Recent Error Logs", value: "```" + failedLogFormatted.slice(0, 900) + "```", inline: false },
        { name: "💡 Recommendation", value: recommendation, inline: false }
      ],
      timestamp: new Date().toISOString(),
      footer: { text: "Noctrya Proxy Sentinel" }
    };

    if (statusDetails) {
      embed.fields.unshift({ name: "📌 Execution Details", value: statusDetails, inline: false });
    }

    await fetch(WEBHOOK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: "Noctrya Proxy Monitor", embeds: [embed] })
    });
  } catch (e) {
    console.error('[ANALYTICS ERROR]', e.message);
  }
}
