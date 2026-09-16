#!/usr/bin/env python3
"""One-off discovery helper: given candidate domains for UK MTG singles shops,
find which resolve and which expose Shopify's JSON endpoints.

Not part of the running app - run it by hand when adding a new shop.
"""
import json
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

CANDIDATES = {
    "7th City Collectables": ["7thcitycollectables.co.uk", "7thcity.co.uk"],
    "Axion Now": ["axionnow.com", "www.axionnow.com"],
    "Boards and Swords": ["www.boardsandswords.co.uk"],
    "Boss Minis": ["bossminis.com", "bossminis.co.uk"],
    "Cosmic Collectables": ["cosmiccollectables.co.uk", "www.cosmiccollectables.co.uk"],
    "Game HQ": ["gamehq.co.uk", "thegamehq.co.uk", "gamehq.uk"],
    "Gathering Point Games": ["gatheringpointgames.co.uk", "gatheringpoint.games", "www.gatheringpointgames.com"],
    "Gearhead Games": ["gearheadgames.co.uk", "gearhead.games", "gearheadgames.com"],
    "Highlander Games": ["highlandergames.co.uk", "www.highlandergames.co.uk"],
    "London Magic Traders": ["londonmagictraders.co.uk", "www.londonmagictraders.com"],
    "Lvl Up Gaming": ["lvlupgaming.uk", "lvlupgaming.co.uk"],
    "Magic Card Trader": ["magiccardtrader.co.uk", "www.magiccardtrader.co.uk"],
    "Magic Madhouse": ["www.magicmadhouse.co.uk"],
    "Manaleak": ["www.manaleak.com", "manaleak.com"],
    "Mighty Lancer Games": ["www.mightylancergames.co.uk", "mightylancergames.co.uk"],
    "Mox In The Hole": ["moxinthehole.co.uk", "www.moxinthehole.co.uk"],
    "Mr Card Singles": ["mrcardsingles.co.uk", "www.mrcardsingles.com"],
    "Patriot Games Leeds": ["patriotgamesleeds.co.uk", "www.patriotgames.uk", "patriotgames.co.uk"],
    "Skyward Fire": ["skywardfire.com", "www.skywardfire.co.uk", "skywardfiregames.co.uk"],
    "Troll Trader": ["www.troll-trader.co.uk", "trolltrader.com", "www.trolltrader.co.uk"],
    "Unicorn Cards": ["www.unicorncards.co.uk", "unicorncards.co.uk"],
}


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.geturl(), r.read(200_000)


def probe(item):
    name, domains = item
    for d in domains:
        try:
            final, body = get("https://%s/products.json?limit=1" % d)
        except Exception:
            continue
        try:
            data = json.loads(body)
        except Exception:
            data = None
        if isinstance(data, dict) and "products" in data:
            host = urllib.parse.urlsplit(final).netloc or d
            return name, host, "shopify"
        # resolved but not Shopify - record the platform we can see
        try:
            _, home = get("https://%s/" % d)
            text = home.decode("utf-8", "replace").lower()
        except Exception:
            continue
        for plat in ("bigcommerce", "woocommerce", "magento", "crystalcommerce", "lightspeed"):
            if plat in text:
                return name, d, plat
        return name, d, "unknown"
    return name, None, "unreachable"


def main():
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, host, plat in pool.map(probe, CANDIDATES.items()):
            print("%-24s %-34s %s" % (name, host or "-", plat))


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
