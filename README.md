# MTG Compare

A local web app for buying a Magic: the Gathering decklist from UK shops.

Paste a decklist, and it searches every shop in the list for each card, shows
who has it in stock and for how much, then works out the cheapest way to buy the
whole list in as few orders as you're willing to place. Each shop in the plan
gets a one-click link that loads the exact cards into that shop's basket.

It is inspired by [Compare the Magic](https://compare-the-magic.netlify.app),
which does single-card search well but caps multi-card search at ten cards.

## Running it

Needs Python 3 and `curl`, both of which macOS already has. Nothing to install.

```bash
./run.sh
```

It prints a `http://127.0.0.1:8777/` address and opens your browser there.
Stop it with Ctrl-C.

## How it works

1. **Card names** are checked against [Scryfall](https://scryfall.com) so a typo
   fails here rather than silently finding nothing at the shops.
2. **Each shop's catalogue is indexed locally**, from its XML sitemaps. That is
   ~60 requests for a shop's entire range, and it means a search doesn't have to
   ask twelve shops about every card on your list.
3. **The index never quotes a price.** It only decides which listings are worth
   looking at; those are then fetched live, so every price and stock figure you
   see was fetched at search time. An old index costs you coverage, never
   accuracy.
4. **Listings are matched conservatively.** A false positive puts the wrong card
   in your basket, which is worse than missing a listing, so the matcher rejects
   things like the back face of a double-faced card, art-series prints and
   sealed product even when the title contains your card's name.
5. **The buy plan** enumerates every combination of shops, keeps the one that
   covers the most of your list, and breaks ties on price. "Split across at most
   N shops" is what stops it sending you to nine different checkouts.

## Things to know

**Postage is not included.** Shipping rules live in each shop's checkout, vary
by weight and destination, and change often, so any figure here would be a guess
dressed up as a number — and a wrong guess sends the buy plan to the wrong
shops. Totals are for cards only; each shop adds delivery at its own checkout.
Keep the order count low with "split across at most N shops".

**Build the catalogues first.** Press "Build / refresh catalogues" before your
first search. Measured on the twelve shops here: 1,075,175 products, 20MB on
disk, about nine and a half minutes. Rebuild every week or two — the index only
affects which listings get considered, so it going stale costs you a
newly-stocked card, not a wrong price.

The index is worth building for accuracy, not speed. A shop's search endpoint
ranks by relevance and returns ten results, which buries cheap printings under
premium ones: on an eight-card test list, searching alone found Swords to
Plowshares at £12.30 and the whole basket at £93.03, while the same search with
the index found it at £1.68 and the basket at £56.14.

**Timings** from that eight-card list across twelve shops: about two minutes
cold, two seconds once cached. Cache entries last a few hours.

**If a shop stops answering mid-search**, the app finishes the other shops, waits
out that shop's cooldown, and tries it again before reporting anything as
unavailable — a shop that went quiet looks exactly like a card nobody stocks,
and those are very different things.

**The shops sit behind Cloudflare bot protection.** Requests are deliberately
slow and spaced out. If you push too hard, Cloudflare starts serving challenges
to your whole machine for every Shopify-hosted shop at once, and it stays that
way for the best part of an hour — the rate limits in `mtgcompare/netcache.py`
are set well below where that started happening, so leave them alone. When a
shop does start refusing, the app stops asking it and says so, rather than
quietly reporting the card as out of stock.

**Two Shopify limits shaped the design**, in case you wonder why it's built this
way. `/products.json` refuses to page past 25,000 products, which is a fraction
of what these shops carry — hence sitemaps. And every entry in a Shopify sitemap
carries the time the *sitemap* was generated, not the time the product changed,
so there is no cheap "what changed since last week?" query and refreshing means
rebuilding.

**Stock quantity is not visible.** Shopify's storefront tells us whether a
variant is available, not how many are left. If you need 4 copies, the shop's
own basket is what will tell you whether it has 4.

**A card missing from the plan is reported with its actual reason.** There are
three, and they need very different responses:

* *Not in stock at any shop we searched* — nothing to be done here.
* *In stock, but not in the condition or finish you asked for* — shown with the
  price and grade, so you can decide. Loosening "condition" re-plans instantly.
* *In stock, but over your shop limit* — a card only one shop carries gets
  dropped when the limit is spent elsewhere. Raise "split across at most".

Every offer found is kept, not just the ones that pass your current filters, so
changing what you'll accept re-plans from what's already been fetched and can
widen the results as well as narrow them.

**Thirteen shops are searched**, all running Shopify, each checked by hand:

Axion Now · Boards and Swords · Boss Minis · Cosmic Collectables ·
Gathering Point Games · Gearhead Games · Highlander Games ·
London Magic Traders · Lvl Up Gaming · Mighty Lancer Games ·
Mox In The Hole · Total Cards · Troll Trader Cards

**Two shops are deliberately not searched**, because their robots.txt asks not
to be. The app gives you a one-click search link per card for them instead —
a person clicking a link is a customer, not a crawler.

* **Skyward Fire** runs CrystalCommerce, and its robots.txt allows Googlebot and
  blocks everything else (`User-agent: * / Disallow: /`). Worth noting for
  anyone revisiting this: CrystalCommerce pages expose the exact number of
  copies in stock, which Shopify does not.
* **Manaleak** runs OpenCart. Its robots.txt disallows every query-string URL,
  which is what its own search endpoint is, and asks for 50 seconds between
  requests — a 44-card list would take about nine hours even if it were allowed.

Magic Madhouse (BigCommerce), Chaos Cards and Patriot Games Leeds (Zen Cart) run
on other platforms and would each need their own adapter. Troll Trader used to
be on CrystalCommerce; their live shop is Shopify, so it needed no new code.

## Layout

```
run.sh              start the app
server.py           the local web server and its JSON API
mtgcompare/
  netcache.py       HTTP transport: caching, rate limiting, backoff
  decklist.py       decklist parsing
  scryfall.py       card-name validation
  shopify.py        the Shopify storefront adapter
  matching.py       "is this listing really that card?"
  catalogue.py      the local index of each shop's range, built from sitemaps
  optimiser.py      cheapest split across a limited number of shops
  export.py         buy plan as CSV or a shopping list
  search.py         runs a whole list as a background job
  stores.py         the shop registry
web/                the page itself
tools/
  probe_stores.py   one-off helper for checking whether a shop runs Shopify
data/
  stores.json       your shop list — safe to edit
  index/            the catalogue index, one file per shop; rebuildable
  cache/            cached shop responses; delete it any time
```

## Adding a shop

Add a candidate domain to `tools/probe_stores.py` and run it. If it reports
`shopify`, add the shop to `DEFAULT_STORES` in `mtgcompare/stores.py` and delete
`data/stores.json` so it gets reseeded.
