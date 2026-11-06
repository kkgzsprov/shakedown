"""Approval mutation detection and cross-process reservation accounting."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import errno
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401
from shakedown.planner.approval import approve_document, verify_approval
from shakedown.planner.budget import BudgetError, BudgetLedger


def targets():
    return {"schema_version": 1, "targets": [
        {"metric": "survival_rate", "profile": "cautious", "map": None,
         "min": .4, "max": .6, "rationale": "운영자 검토용 목표"}], "notes": "draft"}


def reserve_worker(path, attempt, queue):
    try:
        BudgetLedger(Path(path), .3, 10).reserve(attempt, .1)
        queue.put("reserved")
    except BudgetError:
        queue.put("denied")


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifact = self.root / "targets.json"
        self.artifact.write_text(json.dumps(targets(), ensure_ascii=False), encoding="utf-8")
        self.receipt = self.root / "targets.approval.json"

    def test_explicit_approval_binds_contents_path_and_context(self):
        with self.assertRaises(ValueError):
            approve_document("targets", self.artifact, self.receipt)
        self.assertFalse(self.receipt.exists())
        result = approve_document("targets", self.artifact, self.receipt, approved=True,
                                  approved_by="test operator", context_hash="a" * 64)
        self.assertEqual(verify_approval("targets", self.artifact, self.receipt,
                                         context_hash="a" * 64), result)
        for context in (None, "b" * 64):
            with self.subTest(context=context), self.assertRaises(ValueError):
                verify_approval("targets", self.artifact, self.receipt, context_hash=context)

    def test_content_mutation_invalidates_approval_but_formatting_does_not(self):
        approve_document("targets", self.artifact, self.receipt, approved=True)
        self.artifact.write_text(json.dumps(targets(), indent=4), encoding="utf-8")
        verify_approval("targets", self.artifact, self.receipt)
        changed = targets()
        changed["targets"][0]["max"] = .9
        self.artifact.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            verify_approval("targets", self.artifact, self.receipt)

    def test_receipt_cannot_be_reused_for_a_different_path_or_kind(self):
        approve_document("targets", self.artifact, self.receipt, approved=True)
        copy = self.root / "copied.json"
        copy.write_bytes(self.artifact.read_bytes())
        with self.assertRaises(ValueError):
            verify_approval("targets", copy, self.receipt)
        with self.assertRaises(ValueError):
            verify_approval("campaign", self.artifact, self.receipt)
        with self.assertRaises(FileExistsError):
            approve_document("targets", self.artifact, self.receipt, approved=True)

    def test_false_or_malformed_receipt_never_approves(self):
        result = approve_document("targets", self.artifact, self.receipt, approved=True)
        for key, value in (("approved", False), ("v", True), ("sha256", "0" * 64),
                           ("approved_at", "yesterday"), ("approved_by", "")):
            with self.subTest(key=key):
                self.receipt.write_text(json.dumps({**result, key: value}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    verify_approval("targets", self.artifact, self.receipt)

    def test_invalid_or_ambiguous_documents_cannot_be_approved(self):
        for text in ('{"schema_version":1,"schema_version":1}',
                     '{"schema_version":1,"targets":[],"notes":""}',
                     '{"schema_version":1,"targets":NaN,"notes":""}'):
            self.artifact.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                approve_document("targets", self.artifact, self.receipt, approved=True)
        self.assertFalse(self.receipt.exists())


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "planner-budget.json"

    def test_zero_budget_and_call_limit_prevent_reservation(self):
        ledger = BudgetLedger(self.path, 0)
        with self.assertRaises(BudgetError):
            ledger.reserve("first", .1)
        other = BudgetLedger(self.root / "no-calls.json", 1, 0)
        with self.assertRaises(BudgetError):
            other.reserve("first", .1)
        self.assertEqual(ledger.snapshot()["attempts"], [])

    def test_decimal_reservations_and_known_cost_release_exact_remainder(self):
        ledger = BudgetLedger(self.path, .3, 3)
        ledger.reserve("a", .1)
        ledger.reserve("b", .2)
        self.assertEqual(Decimal(ledger.remaining()), Decimal(0))
        with self.assertRaises(BudgetError):
            ledger.reserve("c", .01)
        ledger.settle("a", reported_cost_usd=.05, status="success")
        self.assertEqual(Decimal(ledger.remaining()), Decimal(".05"))
        ledger.reserve("c", .05)
        self.assertEqual(ledger.remaining_calls(), 0)

    def test_unknown_failure_consumes_reservation_and_is_not_zero_cost(self):
        ledger = BudgetLedger(self.path, 1)
        ledger.reserve("failed-call", .4)
        receipt = ledger.settle("failed-call", reported_cost_usd=None, status="timeout")
        self.assertEqual(receipt["reported_cost_usd"], None)
        self.assertEqual(receipt["cost_status"], "unknown")
        self.assertEqual(Decimal(receipt["charged_usd"]), Decimal(".4"))
        self.assertEqual(Decimal(ledger.remaining()), Decimal(".6"))
        self.assertFalse(ledger.snapshot()["cost_measured"])
        self.assertFalse(ledger.snapshot()["billing_cap_enforced"])

    def test_long_decimal_amounts_cannot_round_past_the_limit(self):
        ledger = BudgetLedger(self.path, "0.10000000000000000000000000001")
        ledger.reserve("a", ".1")
        self.assertEqual(Decimal(ledger.remaining()), Decimal("1e-29"))
        with self.assertRaises(BudgetError):
            ledger.reserve("too-much", "2e-29")
        ledger.reserve("exact", "1e-29")
        self.assertEqual(Decimal(ledger.remaining()), Decimal(0))

    def test_actual_overrun_blocks_calls_even_with_unspent_ledger_balance(self):
        ledger = BudgetLedger(self.path, 1)
        ledger.reserve("overrun", .1)
        receipt = ledger.settle("overrun", reported_cost_usd=.2, status="success")
        self.assertEqual(receipt["status"], "over_budget")
        self.assertTrue(ledger.snapshot()["over_budget"])
        self.assertEqual(ledger.remaining_calls(), 0)
        with self.assertRaises(BudgetError):
            ledger.reserve("another", .1)

    def test_reopening_preserves_reservations_and_refuses_policy_change_or_reexecution(self):
        ledger = BudgetLedger(self.path, 1, 2)
        ledger.reserve("attempt", .3)
        reopened = BudgetLedger(self.path, 1.0, 2)
        self.assertEqual(reopened.snapshot()["attempts"], ledger.snapshot()["attempts"])
        with self.assertRaises(BudgetError):
            reopened.reserve("attempt", .3)
        with self.assertRaises(BudgetError):
            BudgetLedger(self.path, 2, 2)
        with self.assertRaises(BudgetError):
            BudgetLedger(self.path, 1, 3)
        reopened.settle("attempt", reported_cost_usd=None, status="error")
        with self.assertRaises(BudgetError):
            reopened.settle("attempt", reported_cost_usd=0, status="success")

    def test_invalid_amounts_and_tampered_ledger_fail_closed(self):
        for value in (True, -1, float("nan"), float("inf"), "NaN"):
            with self.subTest(value=value), self.assertRaises(BudgetError):
                BudgetLedger(self.root / "invalid.json", value)
        ledger = BudgetLedger(self.path, 1)
        for value in (True, -1, 0, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(BudgetError):
                ledger.reserve("invalid", value)
        ledger.reserve("valid", .2)
        state = json.loads(self.path.read_text())
        state["attempts"][0]["charged_usd"] = "0"
        self.path.write_text(json.dumps(state))
        with self.assertRaises(BudgetError):
            ledger.snapshot()

    def test_stale_lock_is_not_deleted_automatically(self):
        ledger = BudgetLedger(self.path, 1)
        ledger.lock_path.write_text("previous owner")
        with patch("shakedown.planner.budget.time.monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(BudgetError, "locked"):
                ledger.reserve("blocked", .1)
        self.assertEqual(ledger.lock_path.read_text(), "previous owner")

    def test_transient_windows_lock_access_denial_retries_exclusive_creation(self):
        ledger = BudgetLedger(self.path, 1)
        original_open = os.open
        attempted = []
        def opening(path, flags, mode=0o777):
            if path == ledger.lock_path:
                attempted.append(flags)
                if len(attempted) == 1:
                    raise PermissionError(errno.EACCES, "lock deletion pending")
            return original_open(path, flags, mode)
        with patch("shakedown.planner.budget.os.name", "nt"), \
                patch("shakedown.planner.budget.os.open", side_effect=opening), \
                patch("shakedown.planner.budget.time.sleep") as sleep:
            ledger.reserve("after-contention", .1)
        self.assertEqual(len(attempted), 2)
        self.assertTrue(all(flags & os.O_CREAT and flags & os.O_EXCL for flags in attempted))
        sleep.assert_called_once_with(.01)
        self.assertFalse(ledger.lock_path.exists())
        self.assertEqual(Decimal(ledger.remaining()), Decimal(".9"))

    def test_permanent_windows_lock_denial_times_out_without_changing_ledger(self):
        ledger = BudgetLedger(self.path, 1)
        before = self.path.read_bytes()
        denied = PermissionError(errno.EACCES, "access denied")
        with patch("shakedown.planner.budget.os.name", "nt"), \
                patch("shakedown.planner.budget.os.open", side_effect=denied), \
                patch("shakedown.planner.budget.time.monotonic", side_effect=[0, 3]):
            with self.assertRaises(BudgetError) as captured:
                ledger.reserve("denied", .1)
        self.assertIs(captured.exception.__cause__, denied)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(ledger.lock_path.exists())

    def test_posix_and_other_permission_errors_are_not_lock_contention(self):
        ledger = BudgetLedger(self.path, 1)
        for platform, code in (("posix", errno.EACCES), ("nt", errno.EPERM)):
            with self.subTest(platform=platform, code=code):
                denied = PermissionError(code, "permission denied")
                with patch("shakedown.planner.budget.os.name", platform), \
                        patch("shakedown.planner.budget.os.open", side_effect=denied), \
                        patch("shakedown.planner.budget.time.sleep") as sleep:
                    with self.assertRaises(PermissionError) as captured:
                        ledger.reserve("denied", .1)
                self.assertIs(captured.exception, denied)
                sleep.assert_not_called()
        self.assertEqual(ledger.snapshot()["attempts"], [])

    def test_threads_cannot_overreserve_one_shared_ledger(self):
        ledger = BudgetLedger(self.path, .5, 10)

        def reserve(index):
            try:
                BudgetLedger(self.path, .5, 10).reserve(str(index), .1)
                return True
            except BudgetError:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            accepted = list(executor.map(reserve, range(8)))
        self.assertEqual(sum(accepted), 5)
        self.assertEqual(Decimal(ledger.snapshot()["used_usd"]), Decimal(".5"))

    def test_processes_cannot_overreserve_one_shared_ledger(self):
        ledger = BudgetLedger(self.path, .3, 10)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        workers = [context.Process(target=reserve_worker, args=(str(self.path), str(index), queue))
                   for index in range(4)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            outcomes = [queue.get(timeout=2) for _ in workers]
            self.assertEqual(outcomes.count("reserved"), 3)
            self.assertEqual(outcomes.count("denied"), 1)
            self.assertEqual(Decimal(ledger.remaining()), Decimal(0))
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            queue.close()
            queue.join_thread()


if __name__ == "__main__":
    unittest.main()
