"""Persistent, exact reservation accounting; this is not a provider billing cap.

Reservations are charged before external execution. Unknown cost consumes the
whole reservation, and a reported overspend blocks subsequent calls. No stale
lock is removed automatically: an interrupted owner requires explicit recovery.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
import errno
import json
import os
from pathlib import Path
import time
import uuid

from ..protocol import canonical_json


class BudgetError(ValueError):
    """The ledger is invalid, unavailable, or cannot authorize another attempt."""


_STATE_FIELDS = {"v", "limit_usd", "max_calls", "over_budget", "attempts"}
_ATTEMPT_FIELDS = {"id", "reserved_usd", "reported_cost_usd", "charged_usd",
                   "settled", "cost_status", "status", "outcome_status"}


def _amount(value, *, positive=False) -> Decimal:
    if type(value) not in (str, int, float, Decimal):
        raise BudgetError("USD amount must be a finite nonnegative number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise BudgetError("invalid USD amount") from error
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise BudgetError("USD amount must be finite and positive" if positive
                          else "USD amount must be finite and nonnegative")
    return amount


def _text(value: Decimal) -> str:
    return format(value, "f")


def _total(values) -> Decimal:
    """Sum finite decimal amounts without the ambient context rounding cents."""
    values = list(values)
    if not values:
        return Decimal(0)
    precision = (max(value.adjusted() for value in values)
                 - min(value.as_tuple().exponent for value in values)
                 + len(str(len(values))) + 2)
    with localcontext() as context:
        context.prec = max(precision, 1)
        return sum(values, Decimal(0))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetError("duplicate budget ledger key")
        result[key] = value
    return result


class BudgetLedger:
    def __init__(self, path: Path, limit_usd: float, max_calls: int = 10):
        self.path = Path(path).resolve()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.limit = _amount(limit_usd)
        if type(max_calls) is not int or max_calls < 0:
            raise BudgetError("max_calls must be a nonnegative integer")
        self.max_calls = max_calls
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if self.path.exists():
                self._read()
            else:
                self._write({"v": 1, "limit_usd": _text(self.limit), "max_calls": max_calls,
                             "over_budget": False, "attempts": []})

    @contextmanager
    def _lock(self):
        deadline = time.monotonic() + 2
        while True:
            try:
                descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except (FileExistsError, PermissionError) as error:
                # Windows exclusive creation can report EACCES during lock-file
                # contention rather than the usual FileExistsError.
                # Retry acquisition only; never remove or take over that lock.
                if isinstance(error, PermissionError) and not (os.name == "nt" and error.errno == errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise BudgetError("budget ledger is locked; no stale lock was removed") from error
                time.sleep(.01)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_json({"pid": os.getpid()}) + b"\n")
                stream.flush()
            yield
        finally:
            self.lock_path.unlink()

    def _write(self, state):
        temporary = self.path.with_name(self.path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_json(state) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _read(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_object)
        except (OSError, ValueError) as error:
            raise BudgetError("budget ledger cannot be read") from error
        if (type(state) is not dict or set(state) != _STATE_FIELDS
                or type(state["v"]) is not int or state["v"] != 1
                or type(state["limit_usd"]) is not str or _amount(state["limit_usd"]) != self.limit
                or type(state["max_calls"]) is not int or state["max_calls"] != self.max_calls
                or type(state["over_budget"]) is not bool or type(state["attempts"]) is not list
                or len(state["attempts"]) > self.max_calls):
            raise BudgetError("budget ledger policy or structure is invalid")
        ids = set()
        overspent = False
        for attempt in state["attempts"]:
            if (type(attempt) is not dict or set(attempt) != _ATTEMPT_FIELDS
                    or type(attempt["id"]) is not str or not attempt["id"] or attempt["id"] in ids
                    or type(attempt["reserved_usd"]) is not str or type(attempt["charged_usd"]) is not str
                    or type(attempt["settled"]) is not bool
                    or type(attempt["status"]) is not str or not attempt["status"]):
                raise BudgetError("invalid budget attempt receipt")
            ids.add(attempt["id"])
            reserved = _amount(attempt["reserved_usd"], positive=True)
            charged = _amount(attempt["charged_usd"])
            reported = attempt["reported_cost_usd"]
            if not attempt["settled"]:
                if (reported is not None or charged != reserved or attempt["cost_status"] != "reserved"
                        or attempt["status"] != "reserved" or attempt["outcome_status"] is not None):
                    raise BudgetError("invalid pending reservation")
            elif reported is None:
                if charged != reserved or attempt["cost_status"] != "unknown":
                    raise BudgetError("unknown cost must retain its reservation")
            else:
                if type(reported) is not str or charged != _amount(reported):
                    raise BudgetError("reported cost and charge differ")
                exceeded = charged > reserved
                if attempt["cost_status"] != ("over_budget" if exceeded else "reported"):
                    raise BudgetError("invalid reported cost status")
                overspent |= exceeded
            if attempt["settled"] and (type(attempt["outcome_status"]) is not str or not attempt["outcome_status"]):
                raise BudgetError("settled attempt has no outcome")
        overspent |= self._used(state) > self.limit
        if state["over_budget"] != overspent:
            raise BudgetError("budget overspend status is inconsistent")
        return state

    @staticmethod
    def _used(state) -> Decimal:
        return _total(_amount(row["charged_usd"]) for row in state["attempts"])

    def reserve(self, attempt_id: str, amount_usd: float) -> dict:
        if type(attempt_id) is not str or not attempt_id or len(attempt_id) > 256 or "\x00" in attempt_id:
            raise BudgetError("attempt_id must be a nonempty identifier")
        amount = _amount(amount_usd, positive=True)
        with self._lock():
            state = self._read()
            if any(row["id"] == attempt_id for row in state["attempts"]):
                raise BudgetError("attempt already reserved; do not execute it again")
            if state["over_budget"] or len(state["attempts"]) >= self.max_calls:
                raise BudgetError("planner call budget is exhausted")
            if _total((self._used(state), amount)) > self.limit:
                raise BudgetError("planner USD reservation budget is exhausted")
            receipt = {"id": attempt_id, "reserved_usd": _text(amount), "reported_cost_usd": None,
                       "charged_usd": _text(amount), "settled": False,
                       "cost_status": "reserved", "status": "reserved", "outcome_status": None}
            state["attempts"].append(receipt)
            self._write(state)
            return dict(receipt)

    def settle(self, attempt_id: str, *, reported_cost_usd: float | None, status: str) -> dict:
        if type(status) is not str or not status.strip() or len(status) > 100 or "\x00" in status:
            raise BudgetError("attempt outcome status is required")
        amount = None if reported_cost_usd is None else _amount(reported_cost_usd)
        with self._lock():
            state = self._read()
            receipt = next((row for row in state["attempts"] if row["id"] == attempt_id), None)
            if receipt is None or receipt["settled"]:
                raise BudgetError("attempt is absent or already settled")
            reserved = _amount(receipt["reserved_usd"])
            exceeded = amount is not None and amount > reserved
            receipt.update(settled=True, reported_cost_usd=_text(amount) if amount is not None else None,
                           charged_usd=_text(amount) if amount is not None else receipt["reserved_usd"],
                           cost_status="unknown" if amount is None else "over_budget" if exceeded else "reported",
                           status="over_budget" if exceeded else status, outcome_status=status)
            state["over_budget"] = state["over_budget"] or exceeded or self._used(state) > self.limit
            self._write(state)
            return dict(receipt)

    def snapshot(self) -> dict:
        with self._lock():
            state = self._read()
            used = self._used(state)
            reserved = _total(_amount(row["charged_usd"]) for row in state["attempts"] if not row["settled"])
            return {**state, "used_usd": _text(used), "reserved_usd": _text(reserved),
                    "charged_usd": _text(_total((used, reserved.copy_negate()))),
                    "remaining_usd": _text(max(Decimal(0), _total((self.limit, used.copy_negate())))),
                    "remaining_calls": 0 if state["over_budget"] else self.max_calls - len(state["attempts"]),
                    "cost_measured": all(row["settled"] and row["reported_cost_usd"] is not None
                                         for row in state["attempts"]),
                    "billing_cap_enforced": False}

    def remaining(self) -> str:
        return self.snapshot()["remaining_usd"]

    def remaining_calls(self) -> int:
        return self.snapshot()["remaining_calls"]
