"""The shop registry.

Every shop here was checked by hand to be running Shopify with its singles
catalogue in the main product search - see tools/probe_stores.py.

Postage is deliberately not modelled. Shipping rules live in each shop's
checkout, vary by weight and destination, and change often, so any figure here
would be a guess dressed up as a number - and a wrong guess sends the buy plan
to the wrong shops. The plan is costed on cards alone, and the "split across at
most N shops" control is what keeps you from paying delivery five times over.
"""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STORES_FILE = os.path.join(DATA_DIR, "stores.json")

# host, display name, and the shop's own "where do I browse singles" page.
DEFAULT_STORES = [
    ("axion-now", "Axion Now", "axionnow.com"),
    ("boards-and-swords", "Boards and Swords", "www.boardsandswords.co.uk"),
    ("boss-minis", "Boss Minis", "bossminis.co.uk"),
    ("cosmic-collectables", "Cosmic Collectables", "cosmiccollectables.co.uk"),
    ("gathering-point", "Gathering Point Games", "gatheringpointgames.co.uk"),
    ("gearhead-games", "Gearhead Games", "gearheadgames.co.uk"),
    ("highlander-games", "Highlander Games", "highlandergames.co.uk"),
    ("london-magic-traders", "London Magic Traders", "londonmagictraders.co.uk"),
    ("lvl-up-gaming", "Lvl Up Gaming", "lvlupgaming.co.uk"),
    ("mighty-lancer", "Mighty Lancer Games", "www.mightylancergames.co.uk"),
    ("mox-in-the-hole", "Mox In The Hole", "moxinthehole.co.uk"),
    ("total-cards", "Total Cards", "www.totalcards.net"),
    # Troll Trader's old CrystalCommerce shop is dead; their live site is Shopify.
    ("troll-trader", "Troll Trader Cards", "www.trolltradercards.com"),
]

# Shops we deliberately do not search automatically, because their robots.txt
# says not to. They still sell the cards, so the app hands you a one-click
# search link per card instead and you look yourself - a person clicking a link
# is a customer, not a crawler.
#
#   Skyward Fire  "User-agent: *  /  Disallow: /"   (Googlebot only)
#   Manaleak      "Disallow: /*?" blocks its own search URL, plus Crawl-delay: 50
MANUAL_STORES = [
    {
        "id": "skyward-fire",
        "name": "Skyward Fire",
        "host": "www.skywardfire.com",
        "search": "https://www.skywardfire.com/products/search?q={card}",
        "reason": "their robots.txt allows Googlebot only",
    },
    {
        "id": "manaleak",
        "name": "Manaleak",
        "host": "www.manaleak.com",
        "search": "https://www.manaleak.com/index.php?route=product/search&search={card}",
        "reason": "their robots.txt disallows search URLs and asks for 50s between requests",
    },
]


def manual():
    return [dict(store) for store in MANUAL_STORES]


def _defaults():
    return [
        dict(id=sid, name=name, host=host, enabled=True, platform=platform)
        for sid, name, host, platform in (
            (sid, name, host, "shopify") for sid, name, host in DEFAULT_STORES
        )
    ]


def load():
    """Return the store list, seeding data/stores.json on first run."""
    if not os.path.exists(STORES_FILE):
        save(_defaults())
    with open(STORES_FILE, encoding="utf-8") as fh:
        stores = json.load(fh)

    # Pick up shops added to DEFAULT_STORES after the file was first written,
    # without clobbering which shops the user has switched off.
    known = {s["id"] for s in stores}
    added = [s for s in _defaults() if s["id"] not in known]
    if added:
        stores.extend(added)
        save(stores)
    return stores


def save(stores):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(stores, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, STORES_FILE)


def update(updates):
    """Apply {store_id: {enabled: bool}}."""
    stores = load()
    by_id = {s["id"]: s for s in stores}
    for store_id, values in (updates or {}).items():
        store = by_id.get(store_id)
        if not store:
            continue
        if "enabled" in values:
            store["enabled"] = bool(values["enabled"])
    save(stores)
    return stores
