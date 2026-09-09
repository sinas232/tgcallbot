"""
presence_reconciler.py — Deterministic Presence Reconciliation & Self-Healing Core.

This module is PURE and DETERMINISTIC.  It performs NO Telegram / PyTgCalls / network
I/O.  It is the authoritative decision layer that answers the four operational
questions:

   1. HOW MANY are actually present RIGHT NOW?
   2. WHICH EXACT accounts are present?
   3. IF one is missing, WHY is it missing?
   4. WHAT did the system do / will it do to restore it?

It is fed observations by the caller (services/voice_call_manager._monitor_loop) and
produces a canonical presence snapshot, an order target-state, a health state, and
incident + root-cause records.  It NEVER performs recovery itself — it decides WHAT
needs recovery and WHY; the caller (deterministic control loop) executes it.

ML is NOT the authority here.  This module is the deterministic control plane.
An ML layer may later consume the produced incident/event records for anomaly
detection, but it must never override the invariants enforced here.
"""

from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional, Set, Tuple

# ─── Account presence states (authoritative presence, not join state) ───
NOT_STARTED = "NOT_STARTED"
JOINING = "JOINING"
JOINED_UNVERIFIED = "JOINED_UNVERIFIED"
CONFIRMED_PRESENT = "CONFIRMED_PRESENT"
TEMPORARILY_UNKNOWN = "TEMPORARILY_UNKNOWN"
SUSPECTED_DISCONNECT = "SUSPECTED_DISCONNECT"
CONFIRMED_ABSENT = "CONFIRMED_ABSENT"
RECOVERING = "RECOVERING"
RECOVERED = "RECOVERED"
FAILED_RECOVERY = "FAILED_RECOVERY"
ORDER_EXPIRED = "ORDER_EXPIRED"

# ─── Order target state machine ───
ORDER_CREATED = "CREATED"
ORDER_STARTING = "STARTING"
ORDER_FILLING = "FILLING"
ORDER_TARGET_REACHED = "TARGET_REACHED"
ORDER_STABLE = "STABLE"
ORDER_DEGRADED = "DEGRADED"
ORDER_RECOVERING = "RECOVERING"
ORDER_FAILED_TO_REACH_TARGET = "FAILED_TO_REACH_TARGET"
ORDER_RUNNING = "RUNNING"
ORDER_EXPIRING = "EXPIRING"
ORDER_CLEANING = "CLEANING"
ORDER_COMPLETED = "COMPLETED"

# ─── Health states ───
HEALTH_HEALTHY = "HEALTHY"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_RECOVERING = "RECOVERING"
HEALTH_CRITICAL = "CRITICAL"
HEALTH_FAILED = "FAILED"

# ─── Root-cause classifications ───
ROOT_UNKNOWN = "UNKNOWN"
ROOT_PYTGCALLS_DISCONNECTED = "PYTGCALLS_DISCONNECTED"
ROOT_TELEGRAM_SESSION_DISCONNECTED = "TELEGRAM_SESSION_DISCONNECTED"
ROOT_TELEGRAM_RPC_ERROR = "TELEGRAM_RPC_ERROR"
ROOT_NETWORK_FAILURE = "NETWORK_FAILURE"
ROOT_VOICE_CHAT_ENDED = "VOICE_CHAT_ENDED"
ROOT_VOICE_CHAT_CHANGED = "VOICE_CHAT_CHANGED"
ROOT_MEMBERSHIP_LOST = "MEMBERSHIP_LOST"
ROOT_PRESENCE_QUERY_FAILURE = "PRESENCE_QUERY_FAILURE"
ROOT_APPLICATION_CLEANUP = "APPLICATION_CLEANUP"
ROOT_CONCURRENCY_RACE = "CONCURRENCY_RACE"
ROOT_ORDER_EXPIRATION = "ORDER_EXPIRATION"
ROOT_ACCOUNT_SPECIFIC_FAILURE = "ACCOUNT_SPECIFIC_FAILURE"

# ─── Leave reasons (audit) ───
LEAVE_ORDER_EXPIRED = "ORDER_EXPIRED"
LEAVE_ORDER_CANCELLED = "ORDER_CANCELLED"
LEAVE_VOICE_CHAT_ENDED = "VOICE_CHAT_ENDED"
LEAVE_RECOVERY_RESET = "RECOVERY_RESET"
LEAVE_FATAL_SESSION_ERROR = "FATAL_SESSION_ERROR"
LEAVE_SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"
LEAVE_ACCOUNT_UNRECOVERABLE = "ACCOUNT_UNRECOVERABLE"
LEAVE_UNKNOWN = "UNKNOWN"

# States that count toward "present".
_PRESENT_STATES = {CONFIRMED_PRESENT, RECOVERED}
# States that count toward "recovering".
_RECOVERING_STATES = {RECOVERING}
# States that count toward "unknown".
_UNKNOWN_STATES = {TEMPORARILY_UNKNOWN, JOINED_UNVERIFIED}
# Absent states (confirmed gone / not yet present but terminal).
_ABSENT_STATES = {SUSPECTED_DISCONNECT, CONFIRMED_ABSENT, FAILED_RECOVERY}


def _diagnose_message(message: str) -> str:
    """Map a diagnostic message/exception text to a root-cause classification."""
    s = (message or "").lower()
    if not s:
        return ROOT_UNKNOWN
    if "voice chat ended" in s or "voice_chat_ended" in s or "no active call" in s:
        return ROOT_VOICE_CHAT_ENDED
    if "voice chat changed" in s or "voice_chat_changed" in s:
        return ROOT_VOICE_CHAT_CHANGED
    if "pytgcalls" in s and ("disconnect" in s or "ended" in s or "closed" in s):
        return ROOT_PYTGCALLS_DISCONNECTED
    if "membership" in s or "not a member" in s or "kicked" in s or "left group" in s:
        return ROOT_MEMBERSHIP_LOST
    if "session" in s and ("revoked" in s or "expired" in s or "invalid" in s or "deactivated" in s):
        return ROOT_TELEGRAM_SESSION_DISCONNECTED
    if "rpc" in s or "420" in s or "flood" in s or "retry after" in s:
        return ROOT_TELEGRAM_RPC_ERROR
    if "network" in s or "connection" in s or "timeout" in s or "timed out" in s or \
       "transport" in s or "500" in s or "502" in s or "503" in s or "internal server" in s:
        return ROOT_NETWORK_FAILURE
    if "presence" in s or "participant" in s or "query" in s or "pagination" in s:
        return ROOT_PRESENCE_QUERY_FAILURE
    if "cleanup" in s or "stop_call" in s or "eject" in s:
        return ROOT_APPLICATION_CLEANUP
    if "expired" in s or "deadline" in s:
        return ROOT_ORDER_EXPIRATION
    if "race" in s or "concurrency" in s or "lock" in s:
        return ROOT_CONCURRENCY_RACE
    return ROOT_UNKNOWN


class PresenceReconciler:
    """Deterministic per-order presence reconciler.

    Responsibilities:
      * Track which accounts are expected for an order.
      * Accept participant observations (sets of user ids present in the Voice Chat)
        from one or more observers and reconcile them against expected accounts.
      * Produce a canonical presence snapshot (present / missing / unknown / recovering).
      * Drive the order target-state machine and health scoring.
      * Keep a bounded incident log: every unexpected disconnect gets an incident id
        with root cause, confidence, recovery plan/attempts/result.
      * Detect correlated (common-subsystem) failures.
      * Keep an append-only leave-audit log (every leave has a reason + correlation id).
    """

    def __init__(self, order_id: int, target_count: int, duration_minutes: int = 0,
                 deadline: Optional[float] = None,
                 confirmed_absent_threshold: int = 3,
                 correlated_window_seconds: float = 15.0,
                 correlated_threshold: int = 4) -> None:
        self.order_id = order_id
        self.target_count = max(0, int(target_count))
        self.duration_minutes = int(duration_minutes)
        # deadline is an epoch timestamp (seconds). If not provided, derive from now.
        self.deadline = deadline if deadline is not None else (
            time.time() + duration_minutes * 60 if duration_minutes > 0 else None
        )
        self.confirmed_absent_threshold = max(1, int(confirmed_absent_threshold))
        self.correlated_window_seconds = float(correlated_window_seconds)
        self.correlated_threshold = max(2, int(correlated_threshold))

        # expected_accounts[account_id] = {last_presence_state, confirmed_absent_cycles, ...}
        self.expected_accounts: Dict[int, Dict] = {}
        # observers -> observed participant sets stored per observation round.
        self._observations: List[Tuple[float, int, Set[int]]] = []
        # canonical presence per account (last reconciled).
        self.presence: Dict[int, str] = {}
        # Continuous presence observation failure counter (not per-account absence).
        self._unknown_rounds = 0

        # Order target state machine.
        self.order_state = ORDER_CREATED
        self.health = HEALTH_HEALTHY

        # Incidents: incident_id -> record.
        self.incidents: Dict[str, Dict] = {}
        self._incident_seq = 0
        # Correlated-failure detection: timestamp of confirmed-absence events.
        self._absence_timestamps: List[float] = []

        # Leave audit (append-only).
        self.leave_audit: List[Dict] = []

        # Recovery bookkeeping.
        self.recovery_attempts = 0
        self.recovery_successes = 0
        self.recovery_failures = 0

    # ─── Expected accounts ───

    def set_expected_accounts(self, account_ids: Set[int]) -> None:
        """Declare which accounts are expected in this Voice Chat."""
        for aid in account_ids:
            if aid not in self.expected_accounts:
                self.expected_accounts[aid] = {
                    "state": NOT_STARTED,
                    "confirmed_absent_cycles": 0,
                    "last_present": None,
                    "last_seen_state": None,
                }
        # Remove accounts no longer expected (order ended).
        current = set(account_ids)
        for aid in list(self.expected_accounts.keys()):
            if aid not in current:
                del self.expected_accounts[aid]

    def mark_joining(self, account_id: int) -> None:
        self.expected_accounts.setdefault(account_id, {
            "state": NOT_STARTED, "confirmed_absent_cycles": 0,
            "last_present": None, "last_seen_state": None,
        })["state"] = JOINING

    def mark_joined_unverified(self, account_id: int) -> None:
        acc = self.expected_accounts.setdefault(account_id, {
            "state": NOT_STARTED, "confirmed_absent_cycles": 0,
            "last_present": None, "last_seen_state": None,
        })
        acc["state"] = JOINED_UNVERIFIED

    def mark_present(self, account_id: int) -> None:
        acc = self.expected_accounts.setdefault(account_id, {
            "state": NOT_STARTED, "confirmed_absent_cycles": 0,
            "last_present": None, "last_seen_state": None,
        })
        acc["state"] = CONFIRMED_PRESENT
        acc["confirmed_absent_cycles"] = 0
        acc["last_present"] = time.time()
        self.presence[account_id] = CONFIRMED_PRESENT

    # ─── Observations ───

    def observe_participants(self, observer_account_id: Optional[int], present_ids: Optional[Set[int]]):
        """
        Feed one observer's view of the Voice Chat participant set.

        present_ids:
          Set[int] - authoritative full participant user-id set (no left members).
          None     - observer could not retrieve participants (unknown observation).
        """
        now = time.time()
        self._observations.append((now, observer_account_id, present_ids))
        # Keep a small bounded window of observations.
        while self._observations and now - self._observations[0][0] > 120:
            self._observations.pop(0)

    def _latest_observation(self) -> Optional[Set[int]]:
        """Return the most recent authoritative participant set, or None if none/unknown."""
        for ts, obs, present_ids in reversed(self._observations):
            if present_ids is not None:
                return present_ids
        return None

    # ─── Confirmed-absence threshold helpers ───

    def _record_absent_cycle(self, account_id: int, reason: str) -> str:
        """Increment confirmed-absent cycle; transition to SUSPECTED_DISCONNECT then CONFIRMED_ABSENT."""
        acc = self.expected_accounts.setdefault(account_id, {})
        acc["confirmed_absent_cycles"] = acc.get("confirmed_absent_cycles", 0) + 1
        cycles = acc["confirmed_absent_cycles"]
        if acc.get("state") in (CONFIRMED_PRESENT, RECOVERED, JOINED_UNVERIFIED, TEMPORARILY_UNKNOWN):
            acc["state"] = SUSPECTED_DISCONNECT
        self._absence_timestamps.append(time.time())
        if cycles >= self.confirmed_absent_threshold:
            acc["state"] = CONFIRMED_ABSENT
            self.presence[account_id] = CONFIRMED_ABSENT
            self._open_incident(account_id, reason)
            return CONFIRMED_ABSENT
        self.presence[account_id] = SUSPECTED_DISCONNECT
        return SUSPECTED_DISCONNECT

    # ─── Reconciliation ───

    def reconcile(self) -> Dict:
        """Reconcile expected accounts against the latest authoritative observation.

        Returns a canonical presence snapshot:
          target, present, missing, unknown, recovering, present_accounts,
          missing_accounts, unknown_accounts, recovering_accounts,
          order_state, health, deadline, remaining_seconds.
        """
        present_ids = self._latest_observation()
        now = time.time()

        if present_ids is None:
            # No authoritative observation available this round -> treat all expected
            # as TEMPORARILY_UNKNOWN (do NOT reduce present count on a query failure).
            self._unknown_rounds += 1
            for aid in self.expected_accounts:
                acc = self.expected_accounts[aid]
                if acc["state"] in (CONFIRMED_PRESENT, RECOVERED, JOINED_UNVERIFIED, TEMPORARILY_UNKNOWN):
                    acc["state"] = TEMPORARILY_UNKNOWN
                    self.presence[aid] = TEMPORARILY_UNKNOWN
        else:
            self._unknown_rounds = 0
            for aid, acc in list(self.expected_accounts.items()):
                if acc["state"] == NOT_STARTED:
                    continue
                if aid in present_ids:
                    # Confirmed present.
                    acc["state"] = CONFIRMED_PRESENT
                    acc["confirmed_absent_cycles"] = 0
                    acc["last_present"] = now
                    self.presence[aid] = CONFIRMED_PRESENT
                else:
                    # Not seen -> increment confirmed-absent cycle (bounded).
                    if acc["state"] in (CONFIRMED_PRESENT, RECOVERED, JOINED_UNVERIFIED,
                                        TEMPORARILY_UNKNOWN, SUSPECTED_DISCONNECT, CONFIRMED_ABSENT):
                        self._record_absent_cycle(aid, "not observed in canonical participant set")

        present_accounts = [aid for aid, s in self.presence.items() if s in _PRESENT_STATES]
        recovering_accounts = [aid for aid, s in self.presence.items() if s in _RECOVERING_STATES]
        unknown_accounts = [aid for aid, s in self.presence.items() if s in _UNKNOWN_STATES]
        missing_accounts = [aid for aid, s in self.presence.items() if s in _ABSENT_STATES]

        present = len(present_accounts)
        missing = len(missing_accounts)
        unknown = len(unknown_accounts)
        recovering = len(recovering_accounts)

        # Order target-state machine.
        self._update_order_state(present, missing, unknown, recovering, now)

        remaining_seconds = None
        if self.deadline is not None:
            remaining_seconds = max(0.0, self.deadline - now)

        return {
            "order_id": self.order_id,
            "target": self.target_count,
            "present": present,
            "missing": missing,
            "unknown": unknown,
            "recovering": recovering,
            "present_accounts": sorted(present_accounts),
            "missing_accounts": sorted(missing_accounts),
            "unknown_accounts": sorted(unknown_accounts),
            "recovering_accounts": sorted(recovering_accounts),
            "order_state": self.order_state,
            "health": self.health,
            "deadline": self.deadline,
            "remaining_seconds": remaining_seconds,
        }

    def _update_order_state(self, present: int, missing: int, unknown: int,
                            recovering: int, now: float) -> None:
        # Expiration check first.
        if self.deadline is not None and now >= self.deadline:
            self.order_state = ORDER_EXPIRING
            if missing == 0 and unknown == 0:
                self.health = HEALTH_HEALTHY
            else:
                self.health = HEALTH_DEGRADED
            return

        if self.order_state == ORDER_COMPLETED:
            return

        if self.order_state in (ORDER_CREATED, ORDER_STARTING):
            self.order_state = ORDER_FILLING

        if present >= self.target_count and self.target_count > 0:
            if self.order_state in (ORDER_FILLING,):
                self.order_state = ORDER_TARGET_REACHED
            elif self.order_state == ORDER_TARGET_REACHED:
                self.order_state = ORDER_STABLE
        elif self.order_state in (ORDER_TARGET_REACHED, ORDER_STABLE, ORDER_RUNNING) \
                and present < self.target_count:
            self.order_state = ORDER_DEGRADED
        elif self.order_state == ORDER_FILLING and present < self.target_count:
            self.order_state = ORDER_FILLING

        if recovering > 0:
            self.order_state = ORDER_RECOVERING

        # Health scoring based on real evidence.
        if missing == 0 and unknown == 0:
            self.health = HEALTH_HEALTHY
        elif unknown > 0 and missing == 0:
            self.health = HEALTH_DEGRADED
        elif recovering > 0:
            self.health = HEALTH_RECOVERING
        elif missing > 0:
            self.health = HEALTH_CRITICAL
        else:
            self.health = HEALTH_DEGRADED

    # ─── Recovery ───

    def begin_recovery(self, account_id: int, plan: str = "rejoin_same_account") -> str:
        """Record that we are recovering the SAME account. Returns an incident id."""
        acc = self.expected_accounts.setdefault(account_id, {})
        acc["state"] = RECOVERING
        self.presence[account_id] = RECOVERING
        self.recovery_attempts += 1
        incident_id = self._open_incident(account_id, f"recovery started: {plan}", plan=plan)
        return incident_id

    def mark_recovered(self, account_id: int) -> None:
        acc = self.expected_accounts.setdefault(account_id, {})
        acc["state"] = CONFIRMED_PRESENT
        acc["confirmed_absent_cycles"] = 0
        acc["last_present"] = time.time()
        self.presence[account_id] = CONFIRMED_PRESENT
        self.recovery_successes += 1
        # Close any open incident for this account as recovered.
        for inc in self.incidents.values():
            if inc.get("account_id") == account_id and inc.get("status") == "OPEN":
                inc["status"] = "RECOVERED"
                inc["recovery_result"] = "SUCCESS"
                inc["closed_at"] = time.time()

    def mark_failed_recovery(self, account_id: int, reason: str = "") -> None:
        acc = self.expected_accounts.setdefault(account_id, {})
        acc["state"] = FAILED_RECOVERY
        self.presence[account_id] = FAILED_RECOVERY
        self.recovery_failures += 1
        for inc in self.incidents.values():
            if inc.get("account_id") == account_id and inc.get("status") == "OPEN":
                inc["status"] = "FAILED_RECOVERY"
                inc["recovery_result"] = "FAILED"
                inc["recovery_failure_reason"] = reason
                inc["closed_at"] = time.time()

    # ─── Incidents ───

    def _open_incident(self, account_id: int, reason: str, plan: str = "rejoin_same_account") -> str:
        self._incident_seq += 1
        incident_id = (
            f"INCIDENT-{self.order_id}-{account_id}-{self._incident_seq:05d}"
        )
        root_cause = _diagnose_message(reason)
        confidence = 0.5 if root_cause == ROOT_UNKNOWN else 0.8
        self.incidents[incident_id] = {
            "incident_id": incident_id,
            "order_id": self.order_id,
            "account_id": account_id,
            "timestamp": time.time(),
            "reason": reason,
            "root_cause": root_cause,
            "confidence": confidence,
            "recovery_plan": plan,
            "recovery_attempts": 0,
            "recovery_result": "PENDING",
            "status": "OPEN",
            "closed_at": None,
        }
        return incident_id

    def register_recovery_attempt(self, incident_id: str) -> None:
        inc = self.incidents.get(incident_id)
        if inc:
            inc["recovery_attempts"] = inc.get("recovery_attempts", 0) + 1

    # ─── Correlated-failure detection ───

    def detect_correlated_failure(self) -> Optional[str]:
        """If many accounts were confirmed absent within a short window, likely a
        common-subsystem failure (Voice Chat ended / network / app), not N independent
        account problems. Returns a root-cause hint or None."""
        now = time.time()
        window = [t for t in self._absence_timestamps if now - t <= self.correlated_window_seconds]
        if len(window) >= self.correlated_threshold:
            return ROOT_VOICE_CHAT_ENDED  # heuristic: many simultaneous absences
        return None

    # ─── Leave audit ───

    def audit_leave(self, account_id: int, reason: str, caller: str = "",
                    previous_state: Optional[str] = None,
                    resulting_state: Optional[str] = None,
                    correlation_id: Optional[str] = None) -> None:
        """Append-only leave audit. Every leave must have a reason."""
        self.leave_audit.append({
            "order_id": self.order_id,
            "account_id": account_id,
            "timestamp": time.time(),
            "reason": reason,
            "caller": caller,
            "previous_state": previous_state,
            "resulting_state": resulting_state,
            "correlation_id": correlation_id,
        })

    # ─── Snapshot / report ───

    def snapshot(self) -> Dict:
        """Full per-order reconciler snapshot for observability."""
        return {
            "order_id": self.order_id,
            "target_count": self.target_count,
            "deadline": self.deadline,
            "duration_minutes": self.duration_minutes,
            "order_state": self.order_state,
            "health": self.health,
            "expected_accounts": {aid: dict(v) for aid, v in self.expected_accounts.items()},
            "presence": dict(self.presence),
            "incidents": dict(self.incidents),
            "recovery": {
                "attempts": self.recovery_attempts,
                "successes": self.recovery_successes,
                "failures": self.recovery_failures,
            },
            "leave_audit": list(self.leave_audit),
        }

    def account_report(self, account_id: int) -> Dict:
        """Detailed per-account diagnostic (answers 'why did account #N leave?')."""
        acc = self.expected_accounts.get(account_id, {})
        incidents = [v for v in self.incidents.values() if v.get("account_id") == account_id]
        leaves = [v for v in self.leave_audit if v.get("account_id") == account_id]
        return {
            "account_id": account_id,
            "order_id": self.order_id,
            "presence_state": self.presence.get(account_id),
            "expected_state": acc.get("state"),
            "confirmed_absent_cycles": acc.get("confirmed_absent_cycles", 0),
            "last_present": acc.get("last_present"),
            "incidents": incidents,
            "leave_events": leaves,
        }
