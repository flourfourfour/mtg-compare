# MTG Compare

A local web app for Josh Flower to buy a Magic: the Gathering decklist from UK singles shops: paste a decklist, it searches multiple shops, works out the cheapest way to buy the whole list, and gives one-click links into each shop's basket.

Inspired by [Compare the Magic](https://compare-the-magic.netlify.app), which does single-card search well but caps multi-card search at ten cards — this project's reason for existing.

**Read this file before making non-trivial changes.** Most of what's below came from a real bug found by testing, not a hypothetical — the comments explain *why*, not just *what*. `README.md` is the user-facing install/usage doc; this file is for whoever (human or AI) works on the code next.

## Status as of 2026-09-16

Feature-complete and in daily use by Josh. Verified against real shops, real decklists (up to 51 cards), with three real bugs found and fixed by testing (see "Bugs found" below). Tracked in git; `data/` is gitignored (see "Moving this project" below for why and what to do about it on a new machine).

Two servers may be running locally from earlier sessions — check `lsof -ti :8777` / `:8778` / `:8779` before assuming a clean slate.

## Architecture

```
run.sh                start the app (wraps server.py)
server.py             stdlib HTTP server + JSON API, no framework, no dependencies
mtgcompare/
  netcache.py          HTTP transport: pooled connections, rate limiting, backoff, disk cache
  decklist.py           parse pasted decklists (Arena/Moxfield/Archidekt/hand-typed)
  scryfall.py            validate card names against Scryfall's API
  stores.py               the shop registry + manually-checked (non-scraped) shops
  catalogue.py             local per-shop product index, built from XML sitemaps
  shopify.py                 the Shopify storefront adapter (search + product fetch + cart links)
  matching.py                  "is this listing really this card?" — conservative title matching
  search.py                     runs a whole decklist across every shop as a background job
  optimiser.py                   cheapest way to buy the list within a shop-count limit
  export.py                       buy plan as CSV or plain-text shopping list
web/                   the page: index.html, app.js, styles.css — no build step, no framework
tools/
  probe_stores.py      one-off: check whether a candidate domain runs Shopify
  check_index.py       one-off: sanity-check a built catalogue index against known staples
data/
  stores.json          the shop list (editable in-app; enabled/disabled per shop)
  index/               one gzipped TSV per shop, the catalogue index — safe to delete, rebuildable
  cache/               gzipped JSON cache of live shop responses — safe to delete anytime
```

Zero external dependencies: stdlib Python 3 + `curl` (already on macOS). No npm, no build step, no framework on either side.

## How a search actually works

1. **Parse** the pasted decklist into `[{name, qty}]`, stripping set codes / collector numbers / Arena's `*F*` markers / section headers.
2. **Validate** names against Scryfall's `/cards/collection` endpoint (batched, cached a week) so a typo fails here, not silently at the shops.
3. **Index filter**: each shop's local catalogue index (built from its XML sitemaps) is checked offline first. If a shop's whole indexed range contains nothing matching a card, that shop is skipped for that card with zero requests.
4. **Live search**: for everything else, the shop's own `/search/suggest.json` endpoint is queried (quoted phrase first, falls back to bare name — some shops' search apps treat quotes literally and return nothing). This is real-time price + stock, always fetched live, never cached longer than a few hours.
5. **Top-up from the index**: the search endpoint caps at 10 results and ranks by relevance, which hides cheap printings behind premium ones. Anything the index knows about that the search didn't surface gets fetched too (up to `PRODUCTS_PER_STORE`, currently 3).
6. **Match**: every candidate title is checked against the card name with conservative rules (rejects DFC back faces, art-series prints, sealed product, prefix collisions like "Island" inside "Island of Wak-Wak").
7. **Optimise**: pick the subset of shops (up to your chosen limit) that covers the most cards, cheapest first, and buy each card from whichever shop in that subset has it cheapest.
8. **Cart links**: Shopify cart permalinks (`/cart/<variant_id>:<qty>,...?storefront=true`) load the exact basket at each shop in the plan, landing on the cart page (not checkout) so you can review before buying.

## Key design decisions (read before changing any of this)

- **The index is a filter in front of live search, not a replacement for it.** First design was "index has everything, never search live" — wrong, because the index has no prices and sitemaps list out-of-stock products too, so it only saves ~6% of requests as a pure filter. Its real value is accuracy: search's 10-result relevance ranking hides cheap printings, and the index-assisted top-up cut a real test basket from £93.03 to £56.14 (one card, Swords to Plowshares, went from £12.30 to £1.68). Never let the index supply a price or stock status directly — it only decides what's worth fetching live.

- **The index is built from XML sitemaps, not `/products.json`.** Shopify's products endpoint refuses to page past 25,000 products (page 100 of 250) — several of these shops carry 3–6x that. Sitemaps have no such cap: ~35–200 files of 5,000 products each. One real gotcha: every entry in a Shopify sitemap file carries the time *that file* was generated, not the time the product changed — all 2,500 entries in one file share a `lastmod`. So there's no cheap "what changed since last week" query; refreshing means rebuilding. At ~60 requests/shop that's fine (whole-catalogue rebuild across 13 shops: ~10 minutes, ~1.1M products, 20MB on disk).

- **The index is stored as gzipped TSV, streamed, never loaded whole into memory.** Original design parsed each shop's index as one JSON blob — measured at ~767 bytes/product in Python object overhead, which is ~400MB for the largest shop alone. Rewritten to one `handle\ttitle` line per product, read with a generator (`catalogue.stream()`), which costs ~2KB regardless of catalogue size.

- **Cloudflare rate limits are real and punishing, and were hit repeatedly during development.** At ~10 req/s aggregate (not per-shop — aggregate across *all* Shopify-hosted shops from one IP), Cloudflare serves a bot challenge disguised as HTTP 429 to every shop at once, for the best part of an hour. Current limits in `netcache.py` (`MAX_CONCURRENT_PER_HOST = 1`, `MIN_GAP_SECONDS = 0.5`, `GLOBAL_REQUESTS_PER_SECOND = 4.0`) sit well under where that started happening. **Do not raise these without a real reason and a way to verify you haven't locked the machine out again** — the recovery isn't instant and there's no way to check remotely except waiting and retrying.

- **Transport is pooled `http.client` connections, with `curl` as a one-shot fallback.** Discovered that opening a fresh TLS connection per request is both slower and more bot-like to Cloudflare than reusing one; also discovered Python's own TLS stack occasionally gets served a challenge where curl's handshake passes, hence the fallback on 403/429.

- **A shop that starts refusing mid-search gets a second pass, not silence.** `netcache.py` has a per-host circuit breaker (`COOLDOWN_AFTER_REFUSALS = 3`, `COOLDOWN_SECONDS = 180`) — once tripped, that shop is skipped instantly rather than retried per-card, which used to make a bad run crawl. `search.py` then does one retry pass after the main sweep, *waiting out the actual remaining cooldown* (not retrying immediately, which was the first, broken version of this fix) before calling anything genuinely unavailable.

- **Every offer found is kept; filtering happens at plan time, not search time.** Original design filtered by condition/foil during the search and discarded the rest — this silently broke the "loosen the condition filter and re-plan without re-searching" UI promise, because there was nothing left to loosen into. Now `search.py` stores every offer found and `_build_plan()` filters fresh each time, so widening a filter recovers cards instantly from data already fetched.

- **"Not in stock" has three distinct causes and they are reported separately — this was gotten wrong three separate times, each in a different way, each caught by testing on real data:**
  1. Genuinely not stocked at any searched shop → `unavailable`.
  2. In stock, but only in a condition/finish you didn't ask for (e.g. Heavily Played when you set Near Mint) → `filtered_out`, shown with price/condition/shop so you can decide.
  3. In stock, but the shop that has it wasn't included in the plan because you capped it at N shops and that shop wasn't needed for anything else → `excluded`, shown with price and which shop has it.

  If you ever see a fourth kind of "card went missing" bug, check whether it's actually one of these three mislabelled, before assuming something new.

- **Postage is deliberately NOT modelled.** Original version had per-shop postage + free-delivery-threshold estimates seeded as placeholders. Josh's call: remove entirely, because a wrong guess actively sends the optimiser to the wrong shops, which is worse than not having the number. The optimiser now purely minimises card cost within a shop-count limit (`max_shops`); postage is left to the shop's own checkout. Do not reintroduce postage modelling without being asked — this was an explicit, considered removal, not an oversight.

- **Only shops whose robots.txt allows it are scraped.** Two candidate shops were deliberately excluded on this basis, not for technical difficulty:
  - **Skyward Fire** (CrystalCommerce): `User-agent: * / Disallow: /` — Googlebot allowed, everything else blocked.
  - **Manaleak** (OpenCart): `Disallow: /*?` blocks its own search URL outright, plus `Crawl-delay: 50` (a 50-card list would take ~9 hours even if it were allowed).

  These two are in `stores.MANUAL_STORES` instead — the UI gives a one-click search-page link per card for them, on the reasoning that a person clicking a link is a customer, not a crawler. **Do not build automated adapters for these two without Josh first getting explicit permission from the shop** — this was a deliberate ethical line, not a technical limitation. If he does get permission, both platforms' HTML structure has already been reverse-engineered (CrystalCommerce's `variant-row` markup even exposes exact stock counts, which Shopify never does) — ask a prior session's transcript or re-derive from a live page if needed.

- **Troll Trader Cards is on Shopify, not CrystalCommerce.** `trolltrader.com` (the domain on the original reference site, Compare the Magic) is now a parked domain for sale. The real, current shop is `trolltradercards.com`, which turned out to already be Shopify — added with zero new code. If any other shop from Compare the Magic's list "doesn't work", check whether its domain has simply changed before assuming it needs a new adapter.

## Known limitations (real, not hypothetical)

- Only Shopify shops are searched automatically. Non-Shopify UK shops not yet covered: Magic Madhouse (BigCommerce), Chaos Cards, Patriot Games Leeds (Zen Cart) — each would need its own adapter, none attempted yet.
- Stock *quantity* is invisible on Shopify (available/unavailable only, no count). CrystalCommerce shops do expose it, for whenever/if that platform gets added.
- The listing matcher (`matching.py`) is conservative by design — it will occasionally miss a real listing rather than risk a false positive (wrong card in the basket). If a card that should clearly be in stock somewhere comes back empty, check the shop's actual title format against `matching.title_matches()` before assuming it's out of stock.
- No git repo. Consider `git init` if ongoing multi-machine work is expected — nothing here is a secret, all data is either regeneratable (the index, the cache) or a plain shop list (`stores.json`).

## Moving this project to another machine

Tracked in git; `data/` is gitignored on purpose — it's regenerated working data, not source, and shouldn't travel with the code (the shop list reseeds itself, the search cache is disposable, the catalogue index rebuilds in ~10 minutes across 13 shops). Needs Python 3 and `curl`, both standard on macOS; nothing to `pip install`.

```bash
git clone <repo-url> && cd MTGCompare
./run.sh                      # first run seeds data/stores.json automatically
# then in the app: "Build / refresh catalogues" (~10 min, one-off)
```

## Getting started as a new developer/session

1. Read this file, then skim `README.md` for the user-facing framing.
2. There's no automated test suite. Verification throughout this project has been: (a) `python3 -m py_compile` on every edited file, (b) small inline scripts with real or mock data run via Bash to check logic before wiring it up, (c) actually running `server.py` and driving it end-to-end via the browser tool or direct HTTP calls against real shops.
3. **Before touching `netcache.py`'s rate limits**, re-read the Cloudflare section above. If you do need to raise them, do it in small increments and verify with `curl` against one shop before running a full search — a lockout costs the better part of an hour and there's no way to check remotely except waiting.
4. Rebuilding the catalogue index: `POST /api/index/build` (or the "Build / refresh catalogues" button in the app). `tools/check_index.py` sanity-checks a built index against known staples — run it after any change to sitemap parsing.
5. `tools/probe_stores.py` is the starting point for adding a new shop candidate — it checks whether a domain exposes Shopify's `/products.json`.
