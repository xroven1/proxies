import json
import random
from pathlib import Path

class ProxySelector:
    def __init__(self, proxy_file="proxies.json"):
        self.proxy_file = Path(proxy_file)
        self.proxies = self._load()

    def _load(self):
        try:
            with open(self.proxy_file) as f:
                return json.load(f)["proxies"]
        except:
            return []

    def get_best(self):
        """Get fastest, most reliable alive proxy"""
        alive = [p for p in self.proxies if p.get("alive")]
        return alive[0] if alive else None

    def get_random(self):
        """Random alive proxy (load balance)"""
        alive = [p for p in self.proxies if p.get("alive")]
        return random.choice(alive) if alive else None

    def get_403_bypass(self):
        """Get proxy that can bypass 403 errors"""
        bypass = [p for p in self.proxies if p.get("alive") and p.get("bypasses_403")]
        return bypass[0] if bypass else self.get_best()

    def get_all_alive(self):
        """Get all working proxies"""
        return [p for p in self.proxies if p.get("alive")]

    def get_url(self, proxy=None):
        """Get just the URL string"""
        if proxy is None:
            proxy = self.get_best()
        return proxy["url"] if proxy else None

    def get_dict(self, proxy=None):
        """Get as requests dict format"""
        url = self.get_url(proxy)
        if not url:
            return {}
        return {"http": url, "https": url}

# usage in your tools:
# from proxy_selector import ProxySelector
# 
# selector = ProxySelector("proxies.json")
# 
# # get best proxy
# proxy = selector.get_best()
# requests.get(url, proxies=selector.get_dict(proxy))
# 
# # for 403 targets
# proxy = selector.get_403_bypass()
# requests.get(url, proxies=selector.get_dict(proxy))
# 
# # random (load balance)
# proxy = selector.get_random()
# requests.get(url, proxies=selector.get_dict(proxy))
