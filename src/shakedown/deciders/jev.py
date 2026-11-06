"""Optional pinned Jev HTTP decisions with conservative shared admission costs.

The provider reports tokens, not a monetary charge. Every HTTP attempt retains
its full USD reservation; tariff estimates are separate from actual billing.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import Decision, DeciderError
from .jev_defaults import DEFAULT_JEV_MODEL
from .random import RandomDecider
from ..planner.budget import BudgetError, BudgetLedger
from ..protocol import canonical_json, request_hash


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024 * 1024


def _number(value, name, *, positive=True):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
                 and (value > 0 if positive else value >= 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"invalid Jev {name}")
    return value


def _endpoint(value):
    if type(value) is not str or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("invalid Jev endpoint")
    parsed = urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise ValueError("invalid Jev endpoint port") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.query):
        raise ValueError("Jev endpoint requires an HTTP(S) URL without credentials or query")
    if parsed.scheme == "http":
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname == "localhost"
        if not local:
            raise ValueError("Jev HTTP is permitted only for loopback fixtures")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject(value):
    raise ValueError("nonfinite JSON value")


def _json(data):
    return json.loads(data, object_pairs_hook=_object, parse_constant=_reject)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class AdaptiveLimiter:
    """One runtime's concurrency cap; existing requests are never abandoned."""
    def __init__(self, capacity):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Jev concurrency must be a positive integer")
        self.cap = capacity
        self.active = 0
        self.events = []
        self.condition = threading.Condition()

    @contextmanager
    def slot(self, deadline):
        with self.condition:
            while self.active >= self.cap:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.condition.wait(remaining)
            if deadline <= time.monotonic():
                raise TimeoutError
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def rate_limited(self):
        with self.condition:
            previous = self.cap
            self.cap = max(1, self.cap // 2)
            self.events.append({"status": 429, "previous_cap": previous, "cap": self.cap})
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"cap": self.cap, "cap_events": list(self.events)}


class JevRuntime:
    """Shared ledger, cache and limiter for all hosted runs in one batch."""
    def __init__(self, settings: dict, ledger_path: Path, max_concurrency: int = 1):
        self.model = settings.get("model", DEFAULT_JEV_MODEL)
        if type(self.model) is not str or re.fullmatch(r"jev-\d+\.\d+\.\d+", self.model) is None:
            raise ValueError("Jev requires an explicitly pinned jev-X.Y.Z model")
        self.endpoint = _endpoint(settings.get("endpoint", "https://api.typesafe.ai/v1/systemone"))
        key_env = settings.get("api_key_env", "TYPESAFE_API_KEY")
        if type(key_env) is not str or not key_env or "\x00" in key_env:
            raise ValueError("invalid Jev API key environment name")
        self._key = os.environ.get(key_env)
        if not self._key or any(ord(character) < 33 or ord(character) > 126 for character in self._key):
            raise ValueError("Jev API key environment variable is missing or invalid")
        self.call_budget = _number(settings.get("call_budget_usd", .001), "call budget")
        total = _number(settings.get("budget_usd", 0), "total budget")
        self.timeout = _number(settings.get("timeout_s", 3), "timeout")
        self.max_elapsed = _number(settings.get("max_elapsed_s", 12), "elapsed limit")
        self.backoff = _number(settings.get("backoff_s", .1), "backoff", positive=False)
        self.price = _number(settings.get("input_price_per_million", .042), "tariff", positive=False)
        self.retries = settings.get("retries", 2)
        if type(self.retries) is not int or not 0 <= self.retries <= 2:
            raise ValueError("Jev retries must be between zero and two")
        max_calls = settings.get("max_calls", 1000)
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("Jev max_calls must be positive")
        self.limiter = AdaptiveLimiter(max_concurrency)
        self.ledger = BudgetLedger(ledger_path, total, max_calls)
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._deciders_lock = threading.Lock()
        self._deciders = []
        self._cache_path = Path(settings["cache"]).resolve() if settings.get("cache") else None
        if self._cache_path is not None:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self._database() as connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS decisions (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            except sqlite3.Error:
                raise DeciderError("Jev cache could not be initialized") from None

    def _reserve(self):
        identifier = uuid.uuid4().hex
        self.ledger.reserve(identifier, self.call_budget)
        return identifier

    @contextmanager
    def _database(self):
        with closing(sqlite3.connect(self._cache_path)) as connection:
            with connection:
                yield connection

    def new_decider(self, *, seed=0, run_id: str):
        if type(run_id) is not str or not run_id or "\x00" in run_id:
            raise ValueError("Jev run_id must be a nonempty identifier")
        reservation = self._reserve()
        decider = JevDecider(self, seed, reservation)
        with self._deciders_lock:
            self._deciders.append(decider)
        return decider

    def _cached(self, key):
        with self._cache_lock:
            if key in self._cache:
                return self._cache[key]
            if self._cache_path is not None:
                try:
                    with self._database() as connection:
                        row = connection.execute("SELECT value FROM decisions WHERE key=?", (key,)).fetchone()
                    if row:
                        value = _json(row[0])
                        self._cache[key] = value
                        return value
                except (sqlite3.Error, ValueError, TypeError, RecursionError):
                    raise DeciderError("Jev cached answer is invalid or unavailable") from None
        return None

    def _store(self, key, value):
        with self._cache_lock:
            self._cache[key] = value
            if self._cache_path is not None:
                try:
                    with self._database() as connection:
                        connection.execute("INSERT OR REPLACE INTO decisions VALUES (?, ?)",
                                           (key, canonical_json(value).decode("utf-8")))
                except sqlite3.Error:
                    raise DeciderError("Jev cache could not retain its answer") from None

    def summary(self):
        with self._deciders_lock:
            summaries = [decider._statistics() for decider in self._deciders]
        attempts = sum(row["http_attempts"] for row in summaries)
        complete = all(row["usage_complete"] for row in summaries)
        known = sum(row["known_estimated_cost_usd"] for row in summaries)
        observed = sorted({model for row in summaries for model in row["observed_models"]})
        return {"model": self.model, "requested_model": self.model, "models": observed,
                "http_attempts": attempts, "cache_hits": sum(row["cache_hits"] for row in summaries),
                "cost_usd": None if attempts else 0., "estimated_cost_usd": known if complete else None,
                "known_estimated_cost_usd": known, "usage_complete": complete,
                "usage_tokens": {name: sum(row["usage_tokens"][name] for row in summaries)
                                 for name in ("input_tokens", "output_tokens")},
                "cost_basis": "reported tokens times configured tariff; actual charge unknown",
                "input_price_per_million": self.price, "tariff_reference_date": "2026-11-06",
                "invalid_reasons": list(dict.fromkeys(reason for row in summaries for reason in row["invalid_reasons"])),
                "budget": self.ledger.snapshot(), **self.limiter.snapshot()}


class JevDecider:
    def __init__(self, runtime: JevRuntime, seed, reservation):
        self.runtime = runtime
        self._reservation = reservation
        self._fallback = RandomDecider(seed)
        self._closed = False
        self._lock = threading.Lock()
        self._attempts = self._hits = 0
        self._input = self._output = 0
        self._known_cost = Decimal(0)
        self._usage_complete = True
        self._invalid = []
        self._observed = False

    def _statistics(self):
        with self._lock:
            return {"model": self.runtime.model, "http_attempts": self._attempts,
                    "requested_model": self.runtime.model,
                    "observed_models": [self.runtime.model] if self._observed else [],
                    "cache_hits": self._hits, "cost_usd": None if self._attempts else 0.,
                    "estimated_cost_usd": float(self._known_cost) if self._usage_complete else None,
                    "known_estimated_cost_usd": float(self._known_cost),
                    "usage_complete": self._usage_complete,
                    "usage_tokens": {"input_tokens": self._input, "output_tokens": self._output},
                    "invalid_reasons": list(self._invalid)}

    def summary(self):
        return {**self._statistics(), "budget": self.runtime.ledger.snapshot(),
                "cost_basis": "reported tokens times configured tariff; actual charge unknown",
                **self.runtime.limiter.snapshot()}

    def _fatal(self, reason):
        with self._lock:
            self._invalid.append(reason)
            if self._attempts:
                self._usage_complete = False
        raise DeciderError(reason)

    def _answer(self, value, names):
        if type(value) is not dict or value.get("model") != self.runtime.model:
            self._fatal("Jev response model does not match the pinned model")
        answer = value.get("answer")
        if type(answer) is not dict or answer.get("type") != "choice":
            self._fatal("Jev response has no valid choice answer")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if (type(probabilities) is not dict or set(probabilities) != set(names)
                or any(type(p) not in (int, float) or not 0 <= p <= 1 or not math.isfinite(p)
                       for p in probabilities.values())
                or abs(math.fsum(probabilities.values()) - 1) > 1e-5
                or type(confidence) not in (int, float) or not 0 <= confidence <= 1
                or not math.isfinite(confidence) or answer.get("choice") not in names):
            self._fatal("Jev response probabilities or choice are invalid")
        choice = min(names, key=lambda name: (-probabilities[name], name))
        if probabilities[answer["choice"]] != probabilities[choice]:
            self._fatal("Jev response choice is not a probability maximum")
        return choice, dict(probabilities)

    @staticmethod
    def _retry_delay(headers, default):
        try:
            milliseconds = headers.get("retry-after-ms")
            if milliseconds is not None:
                value = float(milliseconds) / 1000
            else:
                raw = headers.get("Retry-After")
                if raw is None:
                    return default
                try:
                    value = float(raw)
                except ValueError:
                    when = parsedate_to_datetime(raw)
                    value = (when - datetime.now(timezone.utc)).total_seconds()
            return max(0., value) if math.isfinite(value) else default
        except (TypeError, ValueError, OverflowError):
            return default

    def decide(self, request: dict, instructions: str) -> Decision:
        if self._closed:
            raise DeciderError("Jev decider is closed")
        started = time.monotonic()
        deadline = started + self.runtime.max_elapsed
        names = sorted(request["candidates"])
        if not 1 <= len(names) <= 255 or type(instructions) is not str:
            raise DeciderError("invalid Jev decision request")
        identity = hashlib.sha256(canonical_json({"request": request_hash(request),
            "instructions": instructions, "model": self.runtime.model,
            "endpoint": self.runtime.endpoint})).hexdigest()
        cached = self.runtime._cached(identity)
        if cached is not None:
            choice, probabilities = self._answer(cached, names)
            with self._lock:
                self._hits += 1
                self._observed = True
            return Decision(choice, probabilities, "cache", model=self.runtime.model,
                            latency_ms=(time.monotonic() - started) * 1000)
        payload = canonical_json({"model": self.runtime.model, "state": request["state"],
            "questions": {"tactic": {"type": "choice", "instructions": instructions,
                                      "criteria": request["candidates"]}}})
        if len(payload) > MAX_REQUEST_BYTES:
            self._fatal("Jev request exceeds the documented body limit")
        attempts = 0
        for index in range(self.runtime.retries + 1):
            if time.monotonic() >= deadline:
                break
            delay = self.runtime.backoff * (2 ** index)
            reservation = None
            sent = False
            try:
                with self.runtime.limiter.slot(deadline):
                    try:
                        reservation = self._reservation or self.runtime._reserve()
                    except (OSError, BudgetError):
                        self._fatal("Jev budget admission is unavailable or exhausted")
                    self._reservation = None
                    message = urllib.request.Request(self.runtime.endpoint, data=payload,
                        headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.runtime._key},
                        method="POST")
                    attempts += 1
                    with self._lock:
                        self._attempts += 1
                    sent = True
                    opener = urllib.request.build_opener(_NoRedirect)
                    with opener.open(message, timeout=max(.001, min(self.runtime.timeout, deadline - time.monotonic()))) as response:
                        chunks, size = [], 0
                        while size <= MAX_RESPONSE_BYTES:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError
                            # CPython's HTTPResponse exposes its connected socket
                            # through the buffered reader. Refresh its timeout so
                            # a slow response cannot restart the total deadline.
                            connected = getattr(getattr(response.fp, "raw", None), "_sock", None)
                            if connected is not None:
                                connected.settimeout(min(self.runtime.timeout, remaining))
                            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            size += len(chunk)
                        raw = b"".join(chunks)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        self._fatal("Jev response exceeds the output limit")
                    try:
                        envelope = _json(raw.decode("utf-8"))
                    except (ValueError, UnicodeError, RecursionError):
                        self._fatal("Jev response is not finite JSON")
                    if type(envelope) is not dict or type(envelope.get("answers")) is not dict:
                        self._fatal("Jev response has an invalid envelope")
                    value = {"model": envelope.get("model"), "answer": envelope["answers"].get("tactic")}
                    choice, probabilities = self._answer(value, names)
                    usage = envelope.get("usage")
                    valid_usage = (type(usage) is dict and all(type(usage.get(name)) is int and 0 <= usage[name] <= 2 ** 63 - 1
                                   for name in ("input_tokens", "output_tokens")))
                    with self._lock:
                        self._observed = True
                        if valid_usage:
                            self._input += usage["input_tokens"]
                            self._output += usage["output_tokens"]
                            self._known_cost += Decimal(usage["input_tokens"]) * Decimal(str(self.runtime.price)) / Decimal(1000000)
                        else:
                            self._usage_complete = False
                    self.runtime._store(identity, value)
                    return Decision(choice, probabilities, "jev", attempts=attempts, model=self.runtime.model,
                                    latency_ms=(time.monotonic() - started) * 1000)
            except urllib.error.HTTPError as error:
                status = error.code
                headers = error.headers
                error.close()
                with self._lock:
                    self._usage_complete = False
                if status == 429:
                    self.runtime.limiter.rate_limited()
                if status != 429 and not 500 <= status <= 599:
                    self._fatal(f"Jev HTTP {status}; no automatic retry")
                delay = self._retry_delay(headers, delay)
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
                with self._lock:
                    self._usage_complete = False
            except http.client.HTTPException:
                self._fatal("Jev returned an invalid HTTP response")
            except DeciderError:
                with self._lock:
                    self._usage_complete = False
                raise
            except BudgetError:
                self._fatal("Jev call budget is exhausted")
            finally:
                if reservation is not None:
                    self.runtime.ledger.settle(reservation, reported_cost_usd=None if sent else 0,
                                               status="http_attempt" if sent else "unused")
            remaining = deadline - time.monotonic()
            if index == self.runtime.retries or delay >= remaining:
                break
            if delay:
                time.sleep(delay)
        fallback = self._fallback.decide(request, instructions)
        return Decision(fallback.choice, {}, "fallback", attempts=attempts, model=None,
                        latency_ms=(time.monotonic() - started) * 1000)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._reservation is not None:
            self.runtime.ledger.settle(self._reservation, reported_cost_usd=0, status="unused")
            self._reservation = None
