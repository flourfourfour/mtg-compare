"""Work out which shops to order from.

Buying every card from whoever is cheapest looks good until it turns into nine
separate orders. The job here is to get the cards as cheaply as possible within
a limit on how many shops you are willing to order from.

Postage is not modelled - see stores.py for why - so within a fixed set of
shops the answer is simply "buy each card wherever it is cheapest". What takes
the work is choosing the set: we enumerate every subset of shops (8192 at
thirteen) and keep the one that covers the most cards, breaking ties on price.
"""

import itertools

# Enumerating subsets is exact but 2**n; past this many shops we fall back to a
# greedy search so a big store list can't hang the page.
EXACT_LIMIT = 16


def _cost_of_subset(subset_ids, cards, prices, stores_by_id):
    """Total cost of buying `cards` using only the shops in `subset_ids`."""
    assignment = {}
    subtotals = {sid: 0.0 for sid in subset_ids}

    for card in cards:
        best_id, best_unit = None, None
        for sid in subset_ids:
            unit = prices.get((card["name"], sid))
            if unit is None:
                continue
            if best_unit is None or unit < best_unit:
                best_id, best_unit = sid, unit
        if best_id is None:
            continue
        assignment[card["name"]] = best_id
        subtotals[best_id] += best_unit * card["qty"]

    goods = sum(subtotals.values())
    found = len(assignment)
    return goods, assignment, subtotals, found


def solve(cards, offers_by_card, stores_by_id, max_shops=None):
    """Choose the cheapest way to buy `cards`.

    `cards` is [{"name", "qty"}]; `offers_by_card` is {card_name: [offer, ...]}
    already filtered to what the user will accept. Returns a plan dict.
    """
    # Cheapest acceptable offer per (card, shop).
    prices = {}
    chosen_offer = {}
    for name, offers in offers_by_card.items():
        for offer in offers:
            key = (name, offer["store_id"])
            if key not in prices or offer["price"] < prices[key]:
                prices[key] = offer["price"]
                chosen_offer[key] = offer

    usable = sorted({sid for _name, sid in prices} & set(stores_by_id))
    if not usable:
        return {"shops": [], "total": 0.0,
                "unavailable": [c["name"] for c in cards], "excluded": []}

    limit = max_shops or len(usable)
    best = None
    if len(usable) <= EXACT_LIMIT:
        for size in range(1, min(limit, len(usable)) + 1):
            for subset in itertools.combinations(usable, size):
                result = _cost_of_subset(subset, cards, prices, stores_by_id)
                # More cards found always wins; cost only breaks ties.
                key = (-result[3], result[0])
                if best is None or key < best[0]:
                    best = (key, subset, result)
    else:
        # Greedy: keep adding whichever shop improves the total most.
        subset = []
        while len(subset) < limit:
            candidates = []
            for sid in usable:
                if sid in subset:
                    continue
                trial = tuple(sorted(subset + [sid]))
                result = _cost_of_subset(trial, cards, prices, stores_by_id)
                candidates.append(((-result[3], result[0]), trial, result))
            if not candidates:
                break
            candidates.sort(key=lambda c: c[0])
            if best is not None and candidates[0][0] >= best[0]:
                break
            best = candidates[0]
            subset = list(best[1])

    _key, subset, (goods, assignment, subtotals, _found) = best

    shops = []
    for sid in subset:
        lines = []
        for card in cards:
            if assignment.get(card["name"]) != sid:
                continue
            offer = chosen_offer[(card["name"], sid)]
            lines.append({
                "name": card["name"],
                "qty": card["qty"],
                "unit_price": offer["price"],
                "line_total": round(offer["price"] * card["qty"], 2),
                "condition": offer["condition"],
                "condition_known": offer["condition_known"],
                "foil": offer["foil"],
                "variant_id": offer["variant_id"],
                "product_title": offer["product_title"],
                "product_url": offer["product_url"],
            })
        if not lines:
            continue
        store = stores_by_id[sid]
        subtotal = round(sum(line["line_total"] for line in lines), 2)
        shops.append({
            "id": sid,
            "name": store["name"],
            "host": store["host"],
            "lines": sorted(lines, key=lambda line: line["name"]),
            "subtotal": subtotal,
        })

    shops.sort(key=lambda shop: -shop["subtotal"])
    bought = {line["name"] for shop in shops for line in shop["lines"]}

    # Two very different reasons a card isn't in the plan, and saying "not in
    # stock" for both is a lie that costs money: a card that only one shop
    # carries gets dropped when the shop limit is already spent elsewhere, and
    # that is a limit you can raise, not a card you cannot buy.
    stocked_somewhere = {name for name, _sid in prices}
    unavailable, excluded = [], []
    for card in cards:
        if card["name"] in bought:
            continue
        if card["name"] in stocked_somewhere:
            shops_with_it = sorted(
                {stores_by_id[sid]["name"] for name, sid in prices if name == card["name"]}
            )
            cheapest = min(price for (name, _sid), price in prices.items() if name == card["name"])
            excluded.append({
                "name": card["name"],
                "cheapest": round(cheapest, 2),
                "shops": shops_with_it,
            })
        else:
            unavailable.append(card["name"])

    return {
        "shops": shops,
        "total": round(sum(shop["subtotal"] for shop in shops), 2),
        "unavailable": unavailable,
        "excluded": excluded,
    }
