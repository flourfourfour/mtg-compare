"""Run a whole decklist across every enabled shop, as a background job.

A 60-card list is several hundred requests at a deliberately gentle rate, so
this runs in a worker thread and the page polls for progress. Results stream in
per card, which means the table starts filling before the sweep has finished.
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import catalogue, netcache, optimiser, shopify, stores

# One worker per shop: the real limiter is the rate budget in netcache, not
# thread count, and this keeps each shop's requests neatly in its own lane.
MAX_WORKERS = 12

# How long we'll wait for a shop's cooldown to lapse before the retry pass.
RETRY_WAIT_CAP = 200

_jobs = {}
_jobs_lock = threading.Lock()


def _acceptable(offer, max_condition_rank, foil_mode):
    if offer["condition_rank"] > max_condition_rank:
        return False
    if foil_mode == "non-foil" and offer["foil"]:
        return False
    if foil_mode == "foil" and not offer["foil"]:
        return False
    return True


class Job(object):
    def __init__(self, cards, options):
        self.id = uuid.uuid4().hex[:12]
        self.cards = cards
        self.options = options
        self.started = time.time()
        self.finished = None
        self.done_units = 0
        self.total_units = 0
        self.lock = threading.Lock()
        self.offers = {card["name"]: [] for card in cards}
        self.store_errors = {}
        self.cancelled = False
        self.retrying = []
        self.retry_wait = 0
        self.error = None
        self.plan = None

    def snapshot(self):
        with self.lock:
            return {
                "id": self.id,
                "done": self.done_units,
                "total": self.total_units,
                "running": self.finished is None and self.error is None,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
                "error": self.error,
                "store_errors": dict(self.store_errors),
                "retrying": list(self.retrying),
                "retry_wait": self.retry_wait,
                "plan": self.plan,
                "results": self._results_locked(),
            }

    def _results_locked(self):
        max_rank = int(self.options.get("max_condition_rank", 0))
        foil_mode = self.options.get("foil", "any")
        rows = []
        for card in self.cards:
            offers = sorted(
                (o for o in self.offers[card["name"]] if _acceptable(o, max_rank, foil_mode)),
                key=lambda o: o["price"],
            )
            rows.append({
                "name": card["name"],
                "qty": card["qty"],
                "image": card.get("image"),
                "offers": offers,
                "best": offers[0] if offers else None,
                "shops_with_stock": len({o["store_id"] for o in offers}),
            })
        return rows


def _run(job):
    enabled = [s for s in stores.load() if s.get("enabled", True)]
    with job.lock:
        job.total_units = len(enabled) * len(job.cards)

    names = [card["name"] for card in job.cards]

    def sweep(store):
        # One offline pass over this shop's catalogue for the whole list, when
        # it has been indexed. Cards this shop has never carried are then
        # skipped without a request, which on a real decklist is most of them.
        try:
            index_hits = catalogue.find(store["id"], names)
        except Exception:
            index_hits = {}

        remaining = list(job.cards)
        while remaining:
            if job.cancelled:
                return
            card = remaining.pop(0)
            result = shopify.offers_for_card(
                store, card["name"], index_hits.get(card["name"]) if index_hits else None)
            with job.lock:
                job.done_units += 1
                if result["error"]:
                    job.store_errors[store["id"]] = "%s: %s" % (store["name"], result["error"])
                else:
                    job.store_errors.pop(store["id"], None)
                    # Keep everything, including offers the current filters
                    # reject: loosening "condition" afterwards has to be able to
                    # find them again, and a card that exists only in a
                    # condition you refused is not the same as one nobody has.
                    job.offers[card["name"]].extend(result["offers"])
                if result.get("cooling"):
                    # No point walking the rest of the list at a shop that has
                    # stopped answering - count them off and move on.
                    job.done_units += len(remaining)
                    remaining = []

    try:
        with ThreadPoolExecutor(min(MAX_WORKERS, max(1, len(enabled)))) as pool:
            list(pool.map(sweep, enabled))

        # A shop that started refusing part-way through has left a hole in the
        # results, and the hole looks exactly like "nobody stocks this card".
        # Its cooldown has usually passed by the time the other shops finish, so
        # give those shops one more go before calling anything unavailable.
        if not job.cancelled:
            with job.lock:
                cooled = [s for s in enabled if s["id"] in job.store_errors]
            if cooled:
                # Retrying straight away just hits the same closed door: the
                # cooldown outlasts the rest of the sweep. Wait it out - a
                # couple of minutes is worth it to avoid reporting a card as
                # unavailable when a shop simply stopped answering.
                wait = max([netcache.cooling_remaining(s["host"]) for s in cooled] + [0])
                wait = min(wait + 2, RETRY_WAIT_CAP)
                with job.lock:
                    job.retrying = [s["name"] for s in cooled]
                    job.retry_wait = round(wait)
                    job.total_units += len(cooled) * len(job.cards)
                deadline = time.time() + wait
                while time.time() < deadline and not job.cancelled:
                    time.sleep(1)
                    with job.lock:
                        job.retry_wait = max(0, round(deadline - time.time()))
                with ThreadPoolExecutor(min(MAX_WORKERS, len(cooled))) as pool:
                    list(pool.map(sweep, cooled))
                with job.lock:
                    job.retrying = []
                    job.retry_wait = 0

        if not job.cancelled:
            by_id = {s["id"]: s for s in enabled}
            with job.lock:
                raw = {name: list(offers) for name, offers in job.offers.items()}
            plan = _build_plan(job.cards, raw, by_id, job.options)
            with job.lock:
                job.plan = plan
    except Exception as exc:  # a broken shop shouldn't take the whole run down
        with job.lock:
            job.error = str(exc) or exc.__class__.__name__
    finally:
        with job.lock:
            job.finished = time.time()


def _build_plan(cards, all_offers, stores_by_id, options):
    """Filter offers to what the user will accept, then plan the purchase.

    Anything the filters threw away is reported separately: a card that is only
    available Heavily Played when you asked for Near Mint is a decision for you,
    not an out-of-stock card.
    """
    max_rank = int(options.get("max_condition_rank", 0))
    foil_mode = options.get("foil", "any")

    acceptable = {}
    for name, offers in all_offers.items():
        acceptable[name] = [
            o for o in offers
            if _acceptable(o, max_rank, foil_mode) and o["store_id"] in stores_by_id
        ]

    plan = optimiser.solve(cards, acceptable, stores_by_id, max_shops=options.get("max_shops"))

    rejected = []
    still_missing = []
    for name in plan.get("unavailable", []):
        others = [o for o in all_offers.get(name, []) if o["store_id"] in stores_by_id]
        if not others:
            still_missing.append(name)
            continue
        best = min(others, key=lambda o: o["price"])
        rejected.append({
            "name": name,
            "cheapest": best["price"],
            "condition": best["condition"],
            "foil": best["foil"],
            "shop": best["store_name"],
            "url": best["product_url"],
        })
    plan["unavailable"] = still_missing
    plan["filtered_out"] = rejected
    return plan


def start(cards, options):
    job = Job(cards, options)
    with _jobs_lock:
        # Keep the last few jobs only; this is a single-user local tool.
        for old_id in list(_jobs)[:-4]:
            _jobs.pop(old_id, None)
        _jobs[job.id] = job
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return job


def get(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def replan(job_id, options):
    """Re-plan from results we already have, with new settings.

    Changing what you'll accept, or which shops are in play, shouldn't cost
    another sweep. Every offer found is kept, so this widens as well as narrows.
    """
    job = get(job_id)
    if job is None:
        return None
    enabled = {s["id"]: s for s in stores.load() if s.get("enabled", True)}
    with job.lock:
        raw = {name: list(offers) for name, offers in job.offers.items()}
    plan = _build_plan(job.cards, raw, enabled, options)
    with job.lock:
        job.plan = plan
        job.options = options
    return job


def cart_links(plan):
    """A one-click basket link per shop in the plan."""
    links = {}
    for shop in (plan or {}).get("shops", []):
        links[shop["id"]] = shopify.cart_url(
            shop["host"], [(line["variant_id"], line["qty"]) for line in shop["lines"]]
        )
    return links
