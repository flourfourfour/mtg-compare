#!/usr/bin/env python3
"""Sanity-check a built index against cards every shop is expected to carry.

The index is built from each product's sitemap image title, which Shopify
defaults to the product name but a shop is free to override. If a shop did
override it - to something generic like "card back" - the index would match
nothing and the search would silently skip that shop for every card. This
catches that.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mtgcompare import catalogue, stores  # noqa: E402

# Staples any Magic singles shop of any size will have listed at some point.
PROBE = ["Sol Ring", "Lightning Bolt", "Counterspell", "Brainstorm",
         "Arcane Signet", "Swords to Plowshares", "Island", "Cultivate"]


def main():
    print("%-24s %9s  %s" % ("SHOP", "PRODUCTS", "PROBE CARDS FOUND"))
    for store in stores.load():
        meta = catalogue.load(store["id"])
        if not meta:
            print("%-24s %9s  (no index built)" % (store["name"], "-"))
            continue
        hits = catalogue.find(store["id"], PROBE)
        found = [name for name, listings in hits.items() if listings]
        verdict = "%d/%d" % (len(found), len(PROBE))
        if not found:
            verdict += "  *** matched nothing - titles may be unusable ***"
        elif len(found) < len(PROBE) // 2:
            verdict += "  (low - worth a look)"
        print("%-24s %9s  %s" % (store["name"], format(meta["products"], ","), verdict))


if __name__ == "__main__":
    main()
