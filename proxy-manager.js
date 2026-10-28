// proxy-manager.js (Root Directory)
import { ProxyAgent } from 'undici';

const WEBHOOK_URL = "https://discord.com/api/webhooks/1553383042767265912/Ln_8PuRiK4FuhyWtP7pqj8PDxPt1VxU4rL5-EPxVFgufaAsbXJ-arQSr_e1JWOB46vEc";
const PROXIES_GITHUB_URL = "https://raw.githubusercontent.com/xroven1/proxies/main/proxies.json";

let cachedProxies = [];
let lastLoad = 0;
const CACHE_TTL = 300000; // 5 minutes

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
  return {
    agent: new ProxyAgent(proxy.url),
    ip: proxy.ip,
    url: proxy.url
  };
}

export async function sendProxyAnalyticsReport() {
  try {
    const res = await fetch(PROXIES_GITHUB_URL);
    if (!res.ok) return;
    const data = await res.json();
    const proxies = data.proxies || [];
    const total = proxies.length;

    let aliveCount = 0;
    let deadCount = 0;
    let failedReasons = {};

    for (const p of proxies) {
      if (p.alive === false || p.success_rate < 0.5) {
        deadCount++;
        const reason = p.fail_reason || 'Connection timeout / 403 Block';
        failedReasons[reason] = (failedReasons[reason] || 0) + 1;
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

    const topFailureReason = Object.keys(failedReasons).length > 0 
      ? Object.entries(failedReasons).sort((a,b) => b[1] - a[1])[0][0] 
      : 'None (All operational)';

    const embed = {
      title: "📊 Proxy Pool Analytics & Health Report",
      color: lowPoolWarning ? 16711680 : 65280,
      fields: [
        { name: "📦 Total Proxies", value: "`" + total + "`", inline: true },
        { name: "🟢 Alive / Operational", value: "`" + aliveCount + "`", inline: true },
        { name: "🔴 Dead / Flagged", value: "`" + deadCount + "`", inline: true },
        { name: "🔋 Est. Bandwidth Capacity", value: "`~1 GB (~" + estimatedCapacityRequests.toLocaleString() + " reqs)`", inline: false },
        { name: "❌ Primary Failure Cause", value: "`" + topFailureReason + "`", inline: false },
        { name: "💡 Recommendation", value: recommendation, inline: false }
      ],
      timestamp: new Date().toISOString(),
      footer: { text: "Noctrya Proxy Sentinel" }
    };

    await fetch(WEBHOOK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: "Noctrya Proxy Monitor", embeds: [embed] })
    });
  } catch (e) {
    console.error('[ANALYTICS ERROR]', e.message);
  }
}
