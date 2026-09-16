"""Polite HTTP fetching with an on-disk cache.

Every outbound request in this app goes through `fetch_json`. That one choke
point is what keeps us from hammering small independent shops: a 60-card list
across 12 shops is a few hundred requests, so we cache hard, cap how fast we go
per shop, and back off the moment a shop pushes back.

Three things were learned the hard way and are worth keeping:

* Connections are held open and reused. Opening a fresh TLS connection for every
  request is both slow (0.3s of the round trip) and the thing that looks most
  like a bot to Cloudflare; one connection per shop per run does not.
* Going too fast gets you a Cloudflare challenge - served as HTTP 429 with a
  `Cf-Mitigated: challenge` header - and it stays sticky for the best part of an
  hour, across every Shopify-hosted shop at once. The rate limits below sit well
  under where that started, and `_slow_down` pulls them lower if a shop objects.
* When a challenge does land, curl sometimes gets through where Python's TLS
  stack does not, so it is used as a one-shot fallback before giving up.
"""

import gzip
import hashlib
import json
import os
import random
import http.client
import subprocess
import threading
import time
import urllib.parse

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")

# A real browser UA - these are ordinary storefront requests, the same ones the
# shop's own search box makes, and an unrecognised agent gets challenged.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# These are set well below where Cloudflare started challenging us. Raising
# them gets the whole machine locked out of every Shopify-hosted shop at once,
# for long enough to be genuinely annoying, so leave them alone.
# Per-shop is the limit that matters to Cloudflare, so that one is strict: one
# request at a time, half a second apart. The global ceiling just stops a dozen
# shops' worth of traffic leaving this machine in one burst.
MAX_CONCURRENT_PER_HOST = 1
MIN_GAP_SECONDS = 0.5           # at most 2 requests a second to any one shop
GLOBAL_REQUESTS_PER_SECOND = 3.0
MIN_GLOBAL_RPS = 0.75

_host_locks = {}
_host_last_call = {}
_registry_lock = threading.Lock()

# If a shop starts turning us away, stop asking. Without this, a run where
# every shop is challenging us spends minutes backing off per card instead of
# failing fast and saying so.
COOLDOWN_AFTER_REFUSALS = 3
COOLDOWN_SECONDS = 180
_host_refusals = {}
_host_cooling_until = {}

_bucket_lock = threading.Lock()
_bucket_rate = GLOBAL_REQUESTS_PER_SECOND
_bucket_tokens = GLOBAL_REQUESTS_PER_SECOND
_bucket_stamp = time.time()
_last_pushback = 0.0


class FetchError(Exception):
    """A request failed after retries, or came back as something unusable."""


class HostCooling(FetchError):
    """This shop is turning us away, so we've stopped asking for a while."""


def _cooling_for(host):
    """Seconds left on this host's cooldown, or 0 if it's fine to ask."""
    with _registry_lock:
        until = _host_cooling_until.get(host, 0.0)
    return max(0.0, until - time.time())


def cooling_remaining(host):
    """Seconds until this host is worth asking again. 0 means now."""
    return _cooling_for(host)


def _note_refusal(host):
    with _registry_lock:
        count = _host_refusals.get(host, 0) + 1
        _host_refusals[host] = count
        if count >= COOLDOWN_AFTER_REFUSALS:
            _host_cooling_until[host] = time.time() + COOLDOWN_SECONDS


def _note_success(host):
    with _registry_lock:
        if _host_refusals.pop(host, None):
            _host_cooling_until.pop(host, None)


def _host_gate(host):
    with _registry_lock:
        gate = _host_locks.get(host)
        if gate is None:
            gate = _host_locks[host] = threading.Semaphore(MAX_CONCURRENT_PER_HOST)
        return gate


def _space_out(host):
    """Keep at least MIN_GAP_SECONDS between the starts of two calls to a host."""
    with _registry_lock:
        last = _host_last_call.get(host, 0.0)
        wait = MIN_GAP_SECONDS - (time.time() - last)
        _host_last_call[host] = time.time() + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)


def _take_global_token():
    """Block until the shared budget allows another request."""
    global _bucket_tokens, _bucket_stamp, _bucket_rate
    while True:
        with _bucket_lock:
            now = time.time()
            # Drift back up to full speed once a shop has stopped complaining.
            if _bucket_rate < GLOBAL_REQUESTS_PER_SECOND and now - _last_pushback > 30:
                _bucket_rate = min(GLOBAL_REQUESTS_PER_SECOND, _bucket_rate * 1.5)
            _bucket_tokens = min(_bucket_rate, _bucket_tokens + (now - _bucket_stamp) * _bucket_rate)
            _bucket_stamp = now
            if _bucket_tokens >= 1.0:
                _bucket_tokens -= 1.0
                return
            shortfall = (1.0 - _bucket_tokens) / _bucket_rate
        time.sleep(min(shortfall, 0.25))


def _slow_down():
    """A shop pushed back - drop the shared rate for everyone."""
    global _bucket_rate, _last_pushback
    with _bucket_lock:
        _bucket_rate = max(MIN_GLOBAL_RPS, _bucket_rate / 2.0)
        _last_pushback = time.time()


def _cache_path(url):
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, digest[:2], digest + ".json.gz")


def cache_read(url, max_age):
    path = _cache_path(url)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None
    if age > max_age:
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def cache_write(url, payload):
    path = _cache_path(url)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp%d.%d" % (path, os.getpid(), threading.get_ident())
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def clear_cache():
    """Drop every cached response. Used by the 'fresh prices' option."""
    removed = 0
    for root, _dirs, files in os.walk(CACHE_DIR):
        for name in files:
            try:
                os.unlink(os.path.join(root, name))
                removed += 1
            except OSError:
                pass
    return removed


class _Pool(object):
    """One reusable HTTPS connection per host, handed out under a lock.

    MAX_CONCURRENT_PER_HOST is 1, so a single connection per host is all we
    need; a dead connection is simply dropped and reopened on the next call.
    """

    def __init__(self):
        self._connections = {}
        self._lock = threading.Lock()

    def acquire(self, host):
        with self._lock:
            return self._connections.pop(host, None) or http.client.HTTPSConnection(host, timeout=30)

    def release(self, host, connection):
        with self._lock:
            self._connections[host] = connection

    def discard(self, connection):
        try:
            connection.close()
        except Exception:
            pass


_pool = _Pool()


def _decode(response, raw):
    if (response.getheader("Content-Encoding") or "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw.decode("utf-8", "replace")


def _request(url, timeout, headers, data, redirects=3):
    """One request over a kept-alive connection. Returns (status, body)."""
    split = urllib.parse.urlsplit(url)
    host = split.netloc
    path = split.path + (("?" + split.query) if split.query else "")

    connection = _pool.acquire(host)
    connection.timeout = timeout
    send_headers = dict(headers)
    send_headers.setdefault("Accept-Encoding", "gzip")
    send_headers.setdefault("Connection", "keep-alive")
    send_headers["User-Agent"] = USER_AGENT
    if data is not None:
        send_headers.setdefault("Content-Length", str(len(data)))

    try:
        connection.request("POST" if data is not None else "GET", path, body=data, headers=send_headers)
        response = connection.getresponse()
        body = _decode(response, response.read())
        status = response.status
    except Exception as exc:
        _pool.discard(connection)
        return 0, str(exc) or exc.__class__.__name__

    if response.getheader("Connection", "").lower() == "close":
        _pool.discard(connection)
    else:
        _pool.release(host, connection)

    if status in (301, 302, 303, 307, 308) and redirects > 0:
        location = response.getheader("Location")
        if location:
            return _request(urllib.parse.urljoin(url, location), timeout, headers,
                            data if status in (307, 308) else None, redirects - 1)
    return status, body


def _curl(url, timeout, headers, data):
    """Fallback transport. curl's handshake occasionally gets through a
    Cloudflare challenge that Python's does not, so it is worth one try."""
    command = [
        "curl", "--silent", "--show-error", "--compressed", "--location",
        "--max-time", str(timeout),
        "--user-agent", USER_AGENT,
        "--write-out", "\n%{http_code}",
    ]
    for key, value in headers.items():
        command += ["--header", "%s: %s" % (key, value)]
    if data is not None:
        command += ["--data-binary", "@-"]
    command.append(url)

    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=timeout + 10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0, "curl fallback unavailable"

    output = result.stdout.decode("utf-8", "replace")
    if "\n" not in output:
        return 0, (result.stderr.decode("utf-8", "replace").strip() or "no response")
    body, _, status = output.rpartition("\n")
    try:
        return int(status.strip()), body
    except ValueError:
        return 0, body


def fetch_text(url, max_age=3600, timeout=25, attempts=4, headers=None):
    """Same transport as fetch_json, for responses that aren't JSON (sitemaps)."""
    return _fetch(url, max_age, timeout, attempts, headers, None, parse_json=False)


def fetch_json(url, max_age=3600, timeout=25, attempts=4, headers=None, data=None):
    """GET (or POST, when `data` is given) a URL and parse it as JSON.

    Cached responses skip the network entirely. A 404 is cached as a miss, so we
    don't keep asking a shop for a card it has never stocked.
    """
    return _fetch(url, max_age, timeout, attempts, headers, data, parse_json=True)


def _fetch(url, max_age, timeout, attempts, headers, data, parse_json):
    cacheable = data is None
    if cacheable:
        cached = cache_read(url, max_age)
        if cached is not None:
            if cached.get("miss"):
                raise FetchError("cached miss")
            return cached["body"]

    host = urllib.parse.urlsplit(url).netloc
    request_headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://%s/" % host,
    }
    if headers:
        request_headers.update(headers)

    cooling = _cooling_for(host)
    if cooling:
        raise HostCooling(
            "this shop is turning us away; leaving it alone for another %d min" % max(1, round(cooling / 60))
        )

    gate = _host_gate(host)
    last_error = "unknown error"
    for attempt in range(attempts):
        gate.acquire()
        try:
            _space_out(host)
            _take_global_token()
            status, body = _request(url, timeout, request_headers, data)
            if status in (403, 429):
                # Might be a challenge rather than a real refusal; curl's
                # handshake sometimes passes where ours doesn't.
                fallback_status, fallback_body = _curl(url, timeout, request_headers, data)
                if fallback_status == 200:
                    status, body = fallback_status, fallback_body
        finally:
            gate.release()

        if status == 200:
            _note_success(host)
            if not parse_json:
                if cacheable:
                    cache_write(url, {"miss": False, "body": body})
                return body
            try:
                parsed = json.loads(body)
            except ValueError:
                last_error = "the shop returned a page, not data"
                if cacheable:
                    cache_write(url, {"miss": True})
                raise FetchError(last_error)
            if cacheable:
                cache_write(url, {"miss": False, "body": parsed})
            return parsed

        if status in (404, 410):
            if cacheable:
                cache_write(url, {"miss": True})
            raise FetchError("HTTP %d" % status)
        if status in (400, 401, 403):
            raise FetchError("HTTP %d" % status)

        if status == 429:
            last_error = "the shop asked us to slow down"
            _slow_down()
            _note_refusal(host)
            if _cooling_for(host):
                raise HostCooling(
                    "this shop is turning us away; leaving it alone for %d min"
                    % (COOLDOWN_SECONDS // 60)
                )
            time.sleep((1.5 * (2 ** attempt)) + random.random())
            continue

        last_error = "HTTP %d" % status if status else body[:120]
        time.sleep((0.5 * (2 ** attempt)) + random.random() * 0.3)

    raise FetchError(last_error)
