"""Validate and canonicalise card names against Scryfall.

Two jobs: turn whatever the user pasted into the card's real name, and tell them
plainly which lines are not cards. Doing this once up front means every store
query below uses a name the shops will actually recognise.
"""

import json
import time

from . import netcache

API = "https://api.scryfall.com"

# Scryfall asks for 50-100ms between requests; the collection endpoint keeps us
# to one request per 75 cards anyway.
_COLLECTION_CHUNK = 75
_CACHE_AGE = 7 * 24 * 3600


def _post_collection(names):
    identifiers = [{"name": n} for n in names]
    body = json.dumps({"identifiers": identifiers}).encode("utf-8")
    return netcache.fetch_json(
        API + "/cards/collection",
        data=body,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )


def resolve(names):
    """Map each requested name to its canonical Scryfall name.

    Returns (resolved, unknown) where `resolved` is {requested_lower: card_dict}
    and `unknown` is the list of requested names Scryfall could not identify.
    """
    names = list(dict.fromkeys(names))
    resolved = {}
    unknown = []

    # Scryfall's collection endpoint rejects the full "Fire // Ice" form and
    # wants the front face, so ask by front face and map the answer back.
    lookup = {}
    for name in names:
        lookup.setdefault(name.split(" // ")[0].strip().lower(), []).append(name)
    queries = list(lookup)

    for start in range(0, len(queries), _COLLECTION_CHUNK):
        chunk = queries[start : start + _COLLECTION_CHUNK]
        try:
            payload = _post_collection(chunk)
        except netcache.FetchError:
            # Without Scryfall we still want to search, just with the raw names.
            for query in chunk:
                for name in lookup[query]:
                    resolved[name.lower()] = {"name": name, "unverified": True}
            continue

        by_query = {}
        for card in payload.get("data") or []:
            by_query[card["name"].strip().lower()] = card
            by_query.setdefault(card["name"].split(" // ")[0].strip().lower(), card)

        for query in chunk:
            card = by_query.get(query)
            if card is None:
                unknown.extend(lookup[query])
                continue
            info = {
                "name": card["name"],
                "scryfall_uri": card.get("scryfall_uri"),
                "image": ((card.get("image_uris") or {}).get("small"))
                or (((card.get("card_faces") or [{}])[0].get("image_uris") or {}).get("small")),
            }
            for name in lookup[query]:
                resolved[name.lower()] = info
        time.sleep(0.1)

    return resolved, unknown

