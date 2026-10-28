import json
import time
import requests
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

class ProxyChecker:
    def __init__(self, proxy_file="proxies.json"):
        self.proxy_file = Path(proxy_file)
        self.timeout = 5
        self.test_urls = [
            "http://httpbin.org/ip",
            "http://httpbin.org/status/403"
        ]
        self.proxies = self._load()

    def _load(self):
        if self.proxy_file.exists():
            with open(self.proxy_file) as f:
                return json.load(f)["proxies"]
        return []

    def check_proxy(self, proxy):
        proxy_dict = {"http": proxy["url"], "https": proxy["url"]}
        
        result = {"alive": False, "speed_ms": None, "bypasses_403": False}
        
        try:
            start = time.time()
            resp = requests.get(self.test_urls[0], proxies=proxy_dict, timeout=self.timeout)
            speed = (time.time() - start) * 1000
            
            if resp.status_code == 200:
                result["alive"] = True
                result["speed_ms"] = int(speed)
        except Exception as e:
            return result
        
        try:
            resp = requests.get(self.test_urls[1], proxies=proxy_dict, timeout=self.timeout)
            if resp.status_code != 403:
                result["bypasses_403"] = True
        except:
            pass
        
        return result

    def check_all(self):
        logging.info(f"Starting check of {len(self.proxies)} proxies...")
        
        alive_count = 0
        
        for i, proxy in enumerate(self.proxies, 1):
            result = self.check_proxy(proxy)
            
            proxy["alive"] = result["alive"]
            proxy["speed_ms"] = result["speed_ms"]
            proxy["bypasses_403"] = result["bypasses_403"]
            proxy["last_check"] = int(time.time())
            
            if result["alive"]:
                proxy["success_rate"] = proxy.get("success_rate", 1.0) * 0.95 + 0.05
                alive_count += 1
                status = "✓"
            else:
                proxy["success_rate"] = proxy.get("success_rate", 1.0) * 0.8
                status = "✗"
            
            bypass_tag = "403✓" if result["bypasses_403"] else "403✗"
            logging.info(f"[{i}/50] {status} {proxy['ip']}:{proxy['port']} - {result['speed_ms']}ms - {bypass_tag}")
        
        # sort: alive + reliable + fast first
        self.proxies.sort(
            key=lambda p: (p.get("alive"), p.get("success_rate", 0), -(p.get("speed_ms", 9999) or 9999)),
            reverse=True
        )
        
        self._save(alive_count)

    def _save(self, alive_count):
        data = {
            "proxies": self.proxies,
            "metadata": {
                "total": len(self.proxies),
                "alive": alive_count,
                "dead": len(self.proxies) - alive_count,
                "updated_at": int(time.time())
            }
        }
        with open(self.proxy_file, "w") as f:
            json.dump(data, f, indent=2)
        
        logging.info(f"✓ Check complete - {alive_count} alive, {len(self.proxies) - alive_count} dead")

if __name__ == "__main__":
    checker = ProxyChecker()
    checker.check_all()
