"""Decide whether a shop listing really is the card you asked for.

Shop product titles are wildly inconsistent between stores - compare
"Lightning Bolt (806)", "Lightning Bolt [Revised Edition]" and
"MTG - Marvel Super Heroes: Commander - 0606 : Lightning Bolt (Non Foil)".
The rules here are deliberately conservative: a false positive puts a card you
did not order into your basket, which is worse than missing a listing.
"""

import re

# Grade buckets, best first. Shops use their own vocabulary; "Excellent" is a
# second tier below Near Mint at most UK singles shops, not a synonym for it.
_CONDITION_RANKS = [
    (0, "Near Mint", ("mint", "near mint", "nm", "m", "nm-mint", "near-mint", "unplayed", "new")),
    (1, "Lightly Played", ("excellent", "ex", "lightly played", "lp", "slightly played", "sp", "light play")),
    (2, "Moderately Played", ("good", "gd", "moderately played", "mp", "moderate play")),
    (3, "Heavily Played", ("played", "pl", "heavily played", "hp", "heavy play", "well played")),
    (4, "Damaged", ("poor", "po", "damaged", "dmg", "bad")),
]
_CONDITION_LOOKUP = {}
for _rank, _label, _words in _CONDITION_RANKS:
    for _word in _words:
        _CONDITION_LOOKUP[_word] = (_rank, _label)

UNKNOWN_CONDITION_RANK = 0  # shops that don't grade sell Near Mint by default

# Things that are not a playable single, however much the title matches.
_NOT_A_SINGLE = re.compile(
    r"\b(booster|bundle|box set|collector box|display|playmat|play mat|sleeve|deck box|"
    r"dice|die set|binder|toploader|prerelease|pre-release|theme deck|commander deck|"
    r"precon|starter kit|gift pack|art series|token|proxy|playtest|poster|pin badge|"
    r"t-shirt|mystery pack|repack|bulk|lot of|jumpstart pack|draft pack|pack of)\b",
    re.IGNORECASE,
)

# Lowercase connectives: if the card name is followed by one of these, we've
# matched a prefix of a longer card name ("Island" inside "Island of Wak-Wak").
_CONTINUATION = re.compile(r"^\s*(of|the|and|to|in|on|for|from|with|at|s\b|'s)\b", re.IGNORECASE)


def _normalise(text):
    text = (text or "").replace("’", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def title_matches(title, card_name):
    """True if `title` is a listing for `card_name` and not something else."""
    title_n = _normalise(title)
    name_n = _normalise(card_name)
    if not title_n or not name_n:
        return False
    if _NOT_A_SINGLE.search(title_n):
        return False

    haystack = title_n.lower()
    needle = name_n.lower()
    # Split cards: shops list them either way round, so try the front face too.
    candidates = [needle]
    if " // " in needle:
        candidates.append(needle.replace(" // ", " / "))
        candidates.append(needle.split(" // ")[0])

    for candidate in candidates:
        for match in re.finditer(re.escape(candidate), haystack):
            before = haystack[: match.start()]
            after = title_n[match.end() :]

            # Word boundary at the start, so "Fire" doesn't match "Fireball".
            if before and before[-1].isalnum():
                continue
            # The back face of a double-faced card is not the card we want:
            # reject "Emeritus of Conflict // Lightning Bolt".
            if before.rstrip().endswith("//") and " // " not in candidate:
                continue
            if after[:1].isalnum():
                continue
            # "Island" inside "Island of Wak-Wak".
            if _CONTINUATION.match(after):
                continue
            return True
    return False


def parse_variant(product, variant):
    """Work out condition, finish and price for one Shopify variant.

    Shopify gives us both an `options` list on the product and per-variant
    option values; we read whichever is present rather than parsing the
    variant title, because option ordering differs between shops.
    """
    values = []
    variant_options = variant.get("options")
    if isinstance(variant_options, list):
        values.extend(str(v) for v in variant_options if v)
    if not values:
        values.extend(str(v) for v in str(variant.get("title") or "").split(" / ") if v)

    condition_rank = None
    condition_label = None
    foil = None

    for value in values:
        token = _normalise(value).lower().strip(" .-")
        if token in ("default title", "default", ""):
            continue
        # A single option often carries both facts ("NM-Mint Foil"), so read the
        # finish, strip it out, and keep grading what's left.
        if "non-foil" in token or "nonfoil" in token or "non foil" in token:
            foil = False if foil is None else foil
            token = re.sub(r"non[- ]?foil", " ", token).strip(" -/")
        elif re.search(r"\bfoils?\b", token):
            foil = True
            token = re.sub(r"\bfoils?\b", " ", token).strip(" -/")
        token = re.sub(r"\s+", " ", token).strip()
        if not token:
            continue

        graded = _CONDITION_LOOKUP.get(token)
        if graded is None:
            # "NM-Mint Foil", "Near Mint Non-Foil" and similar compounds.
            for word, entry in _CONDITION_LOOKUP.items():
                if len(word) > 2 and re.search(r"\b%s\b" % re.escape(word), token):
                    graded = entry
                    break
        if graded and (condition_rank is None or graded[0] < condition_rank):
            condition_rank, condition_label = graded

    if foil is None:
        title = _normalise(product.get("title") or "").lower()
        if "non-foil" in title or "non foil" in title:
            foil = False
        elif re.search(r"\bfoil\b", title):
            foil = True
        else:
            foil = False

    price_pence = variant.get("price")
    try:
        price = int(price_pence) / 100.0
    except (TypeError, ValueError):
        return None

    return {
        "variant_id": variant.get("id"),
        "price": round(price, 2),
        "available": bool(variant.get("available")),
        "condition_rank": UNKNOWN_CONDITION_RANK if condition_rank is None else condition_rank,
        "condition": condition_label or "Ungraded",
        "condition_known": condition_label is not None,
        "foil": bool(foil),
        "variant_title": _normalise(variant.get("title") or ""),
    }
