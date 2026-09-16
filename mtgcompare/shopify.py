"""Shopify storefront adapter.

Two endpoints do all the work, and neither needs an API key:

  /search/suggest.json   fuzzy product search, capped at 10 results
  /products/<handle>.js  the full variant list, with per-variant stock

The search is fuzzy enough that a bare "Sol Ring" returns Solar Auxilia
miniatures, so we always quote the query - Shopify then treats it as a phrase,
which was the single biggest accuracy win in testing.

The prices in suggest.json are the *minimum across all variants including
sold-out ones*, so they are only used to rank which products are worth a
second request. Every price we show comes from the product endpoint.
"""

import urllib.parse

from . import matching, netcache

# Prices and stock at these shops move slowly; caching for a few hours is the
# difference between a usable tool and one that gets you rate-limited.
SEARCH_MAX_AGE = 6 * 3600
PRODUCT_MAX_AGE = 3 * 3600

# How many candidate products per shop we'll open to read real variant data.
# The cheapest few by the search endpoint's rough price is almost always where
# the cheapest in-stock copy lives.
PRODUCTS_PER_STORE = 3


def _suggest_url(host, query, hide_unavailable):
    params = [
        ("q", query),
        ("resources[type]", "product"),
        ("resources[limit]", "10"),
    ]
    if hide_unavailable:
        params.append(("resources[options][unavailable_products]", "hide"))
    return "https://%s/search/suggest.json?%s" % (host, urllib.parse.urlencode(params))


def _search(host, card_name):
    """Return candidate products for a card, cheapest-looking first.

    Raises netcache.FetchError if the shop could not be reached - the caller
    needs to tell those apart from "this shop doesn't stock the card".
    """
    quoted = '"%s"' % card_name.replace('"', "")
    # Quoted first: phrase matching is far more accurate. A few shops run a
    # third-party search app that takes the quotes literally and finds nothing,
    # so fall back to the bare name for those.
    products = []
    for query in (quoted, card_name):
        payload = netcache.fetch_json(_suggest_url(host, query, True), max_age=SEARCH_MAX_AGE)
        products = ((payload.get("resources") or {}).get("results") or {}).get("products") or []
        if any(matching.title_matches(p.get("title") or "", card_name) for p in products):
            break

    candidates = []
    for product in products:
        title = product.get("title") or ""
        if not matching.title_matches(title, card_name):
            continue
        try:
            rough_price = float(product.get("price") or 0)
        except (TypeError, ValueError):
            rough_price = 0.0
        candidates.append((rough_price, product.get("handle"), title))

    candidates.sort(key=lambda c: (c[0] <= 0, c[0]))
    return candidates[:PRODUCTS_PER_STORE]


def _product(host, handle):
    return netcache.fetch_json(
        "https://%s/products/%s.js" % (host, urllib.parse.quote(handle)),
        max_age=PRODUCT_MAX_AGE,
    )


def offers_for_card(store, card_name, indexed=None):
    """Every in-stock variant of `card_name` this shop has.

    `indexed` is what the local catalogue index knows this shop carries. It is
    used as a filter, not as the answer: if the index has been built and lists
    nothing matching, the shop is skipped without a single request, which is
    where most of the saving comes from on a real decklist - no shop carries
    every card on your list.

    Where the index does have something, we still search the shop live, because
    the search endpoint returns prices and in-stock status in one request and
    the index has neither. Anything the index knows about that the search missed
    is added on afterwards, which is how printings hidden behind the search
    endpoint's ten-result cap get found.

    Returns {"offers": [...], "error": None or a message}. An empty offer list
    with no error genuinely means "not in stock here"; an error means we never
    got a usable answer, which is a different thing and is shown as such.
    """
    host = store["host"]
    offers = []

    if indexed is not None and not indexed:
        # The index has this shop's whole range and none of it is this card.
        return {"offers": [], "error": None, "cooling": False}

    try:
        candidates = _search(host, card_name)
    except netcache.HostCooling as exc:
        return {"offers": [], "error": str(exc), "cooling": True}
    except netcache.FetchError as exc:
        return {"offers": [], "error": str(exc)}

    if indexed:
        # Top up with anything the index found that the search didn't return.
        known = {handle for _price, handle, _title in candidates}
        extra = [(0.0, handle, title) for handle, title in indexed if handle not in known]
        candidates = candidates + extra[: max(0, PRODUCTS_PER_STORE - len(candidates))]

    failures = 0
    cooling = False
    for _rough_price, handle, _title in candidates:
        if not handle:
            continue
        try:
            product = _product(host, handle)
        except netcache.HostCooling as exc:
            return {"offers": offers, "error": str(exc), "cooling": True}
        except netcache.FetchError:
            failures += 1
            continue

        # The search endpoint matched on a fuzzy index; re-check the real title.
        title = product.get("title") or ""
        if not matching.title_matches(title, card_name):
            continue

        for variant in product.get("variants") or []:
            parsed = matching.parse_variant(product, variant)
            if not parsed or not parsed["available"]:
                continue
            offers.append(
                {
                    "store_id": store["id"],
                    "store_name": store["name"],
                    "host": host,
                    "product_title": title,
                    "product_url": "https://%s/products/%s" % (host, handle),
                    "image": (product.get("featured_image") or None),
                    **parsed,
                }
            )

    offers.sort(key=lambda o: (o["price"], o["condition_rank"]))
    error = None
    if not offers and failures and failures == len(candidates):
        error = "could not read this shop's product pages"
    return {"offers": offers, "error": error, "cooling": cooling}


def cart_url(host, lines):
    """A Shopify cart permalink: one click loads the whole basket for a shop.

    `lines` is a list of (variant_id, quantity). Without `?storefront=true` a
    permalink drops you straight into checkout, which is a startling place to
    arrive from a price comparison; this lands on the cart page instead, where
    you can check the cards over before committing to anything.
    """
    parts = ["%s:%d" % (variant_id, max(1, int(qty))) for variant_id, qty in lines if variant_id]
    if not parts:
        return None
    return "https://%s/cart/%s?storefront=true" % (host, ",".join(parts))
