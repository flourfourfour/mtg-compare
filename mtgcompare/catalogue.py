"""A local index of what each shop sells.

Searching every shop for every card is the slow, fragile way to do this: it is
thousands of requests, it is capped at ten results per search so cheaper
printings stay hidden, and it is exactly the traffic pattern that gets the whole
machine challenged by Cloudflare.

So instead each shop's catalogue is downloaded once, in bulk, and kept here. The
index answers one question only - *which products at this shop could be the card
I want* - and it answers it offline, instantly, across the shop's whole range.

The index never quotes a price. Stock at a singles shop changes with every sale,
so the handful of products the index picks out are re-fetched live before
anything is shown to you. That is what keeps an old index harmless: its age
affects which listings get considered, never what they cost.

It is built from the shop's XML sitemaps rather than its products endpoint, for
one decisive reason: `/products.json` refuses to page past 25,000 products
(page 100 of 250), and these shops carry several times that. A sitemap file
carries 5,000 products and there is no cap, so a shop's entire catalogue costs
35-100 requests instead of an impossible 700.

Note that Shopify stamps every entry in a sitemap with the time the sitemap was
generated, not the time that product changed - all 2,500 entries in a file share
one `lastmod`. So there is no cheap "what changed?" query, and refreshing means
rebuilding. At ~60 requests a shop that is fine.

The index is stored as gzipped TSV, one product per line, and read by streaming
rather than parsing. That is not premature tidiness: the biggest of these shops
carries several hundred thousand products, and holding one parsed as Python
objects costs about 400MB - times twelve shops searched in parallel. Streaming
holds one line at a time.
"""

import gzip
import html
import json
import os
import re
import threading
import time
import urllib.parse

from . import matching, netcache

INDEX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "index")

# A sitemap file holds up to 5,000 products. MAX_FILES is a stop so a malformed
# sitemap index can't send us into a loop.
MAX_FILES = 200

_loaded = {}             # store_id -> (mtime, index dict)

_SITEMAP_LOC = re.compile(r"<loc>\s*([^<]*sitemap_products[^<]*?)\s*</loc>", re.I)
_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_HANDLE = re.compile(r"<loc>[^<]*?/products/([^<?#]+)", re.I)
_TITLE = re.compile(r"<image:title>(.*?)</image:title>", re.S | re.I)


def path_for(store_id):
    return os.path.join(INDEX_DIR, "%s.json.gz" % store_id)


def _write(store_id, meta, products):
    """Write the index: a JSON metadata line, then one `handle\ttitle` per line."""
    os.makedirs(INDEX_DIR, exist_ok=True)
    target = path_for(store_id)
    tmp = target + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(meta) + "\n")
        for handle, title in products:
            fh.write("%s\t%s\n" % (handle.replace("\t", " "), title.replace("\t", " ")))
    os.replace(tmp, target)
    _loaded.pop(store_id, None)


def load(store_id):
    """Return a shop's index metadata, or None if it has never been built.

    Deliberately does not read the products - see `stream`.
    """
    target = path_for(store_id)
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return None
    cached = _loaded.get(store_id)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with gzip.open(target, "rt", encoding="utf-8") as fh:
            meta = json.loads(fh.readline())
    except Exception:
        return None
    _loaded[store_id] = (mtime, meta)
    return meta


def stream(store_id):
    """Yield (handle, title) for every product, holding one line at a time."""
    target = path_for(store_id)
    try:
        fh = gzip.open(target, "rt", encoding="utf-8")
    except OSError:
        return
    with fh:
        fh.readline()  # metadata
        for line in fh:
            handle, _, title = line.rstrip("\n").partition("\t")
            if handle:
                yield handle, title


def status(stores):
    """Per-shop index age, for the page to show."""
    out = []
    for store in stores:
        meta = load(store["id"])
        out.append({
            "id": store["id"],
            "name": store["name"],
            "built_at": meta.get("built_at") if meta else None,
            "age_hours": round((time.time() - meta["built_at"]) / 3600.0, 1) if meta else None,
            "products": meta.get("products", 0) if meta else 0,
        })
    return out


def build(store, progress=None, should_stop=None):
    """Download a shop's whole catalogue from its sitemaps.

    `progress(files_done, files_total, products_so_far)` is called as it goes.
    """
    host = store["host"]
    index_xml = netcache.fetch_text("https://%s/sitemap.xml" % host, max_age=0, timeout=45)
    files = [html.unescape(url) for url in _SITEMAP_LOC.findall(index_xml)][:MAX_FILES]

    products = []
    seen = set()
    for position, file_url in enumerate(files, start=1):
        if should_stop and should_stop():
            break
        try:
            body = netcache.fetch_text(file_url, max_age=0, timeout=45)
        except netcache.FetchError:
            # A partial catalogue is still worth keeping; just note the gap.
            if progress:
                progress(position, len(files), len(products))
            continue

        for block in _URL_BLOCK.findall(body):
            handle_match = _HANDLE.search(block)
            if not handle_match:
                continue
            handle = urllib.parse.unquote(handle_match.group(1)).strip()
            if not handle or handle in seen:
                continue
            title_match = _TITLE.search(block)
            title = html.unescape(title_match.group(1)).strip() if title_match else ""
            if not title:
                # No image title: fall back to the handle, which Shopify derives
                # from the product name and is usually close enough to match on.
                title = handle.replace("-", " ")
            seen.add(handle)
            products.append([handle, title])

        if progress:
            progress(position, len(files), len(products))

    meta = {
        "store_id": store["id"],
        "host": host,
        "built_at": time.time(),
        "files": len(files),
        "products": len(products),
    }
    _write(store["id"], meta, products)
    return meta


_TOKEN = re.compile(r"[a-z0-9']+")


def _tokens(text):
    return _TOKEN.findall(text.lower())


def find(store_id, card_names, per_card=4):
    """Which products at this shop might be each of these cards?

    One pass over the shop's titles for the whole decklist, rather than one pass
    per card. Returns {card_name: [(handle, title), ...]}.

    The index carries no prices - a price in a file is a price that can be wrong
    - so there is nothing to rank by here. The caller fetches these candidates
    live and picks the cheapest from what comes back, which is why `per_card` is
    a small number: it is a live-request budget, not a search limit.
    """
    if not load(store_id):
        return {}

    # Card names keyed by their first word, so each title is only tested against
    # the few cards that could plausibly match it.
    by_first_token = {}
    for name in card_names:
        tokens = _tokens(name)
        if tokens:
            by_first_token.setdefault(tokens[0], []).append(name)

    hits = {name: [] for name in card_names}
    for handle, title in stream(store_id):
        candidates = set()
        for token in _tokens(title):
            for name in by_first_token.get(token, ()):
                candidates.add(name)
        for name in candidates:
            if matching.title_matches(title, name):
                hits[name].append((handle, title))

    # Shorter titles tend to be the plain printing rather than a showcase or
    # promo variant, and plain printings are usually the cheap ones.
    for name in hits:
        hits[name].sort(key=lambda hit: len(hit[1]))
        hits[name] = hits[name][:per_card]
    return hits


# --- building every shop's index as one watchable background job -------------

_build_lock = threading.Lock()
_build_state = None


def build_state():
    with _build_lock:
        return dict(_build_state) if _build_state else None


def build_all(store_list):
    """Kick off a background rebuild of every given shop's index.

    Returns immediately. Only one build runs at a time - they all share the same
    request budget, so running two would just make both slower.
    """
    global _build_state
    with _build_lock:
        if _build_state and _build_state.get("running"):
            return _build_state
        _build_state = {
            "running": True, "started": time.time(), "finished": None,
            "shops_done": 0, "shops_total": len(store_list),
            "current": None, "products": 0, "errors": [], "stop": False,
        }

    def worker():
        for store in store_list:
            with _build_lock:
                if _build_state["stop"]:
                    break
                _build_state["current"] = store["name"]

            def progress(done, total, products, store=store):
                with _build_lock:
                    _build_state["current"] = "%s (%d/%d files)" % (store["name"], done, total)
                    _build_state["products"] = products

            try:
                meta = build(store, progress=progress,
                              should_stop=lambda: build_state() and build_state()["stop"])
                with _build_lock:
                    _build_state["products"] = meta["products"]
            except Exception as exc:
                with _build_lock:
                    _build_state["errors"].append("%s: %s" % (store["name"], exc))
            with _build_lock:
                _build_state["shops_done"] += 1

        with _build_lock:
            _build_state["running"] = False
            _build_state["current"] = None
            _build_state["finished"] = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return build_state()


def stop_build():
    with _build_lock:
        if _build_state:
            _build_state["stop"] = True
