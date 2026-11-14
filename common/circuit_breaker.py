"""Stateful circuit breaker and anti-flapping guardrail for NextSRE mutations.

Enforces:
1. Per-resource cooldown window (e.g. 300s) to prevent rapid oscillating mutations.
2. Per-incident mutation budget ceiling to prevent runaway automated changes.
"""

from __future__ import annotations

import threading
import time

from common.env import env_int

DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_MAX_INCIDENT_MUTATIONS = 5


class CircuitBreakerError(RuntimeError):
    """Raised when a mutation is rejected by the anti-flapping or quota circuit breaker."""


class MutationCircuitBreaker:
    """Thread-safe sliding-window circuit breaker for infrastructure mutations."""

    def __init__(
        self,
        *,
        cooldown_seconds: float | None = None,
        max_mutations: int | None = None,
    ) -> None:
        self.cooldown_seconds = (
            cooldown_seconds
            if cooldown_seconds is not None
            else float(
                env_int("REMEDIATION_COOLDOWN_SECONDS", default=int(DEFAULT_COOLDOWN_SECONDS))
            )
        )
        self.max_mutations = (
            max_mutations
            if max_mutations is not None
            else env_int("MAX_INCIDENT_MUTATIONS", default=DEFAULT_MAX_INCIDENT_MUTATIONS)
        )
        self._lock = threading.Lock()
        self._history: dict[tuple[str, str], float] = {}
        self._total_mutations: int = 0

    def check_and_record(self, namespace: str, resource: str) -> None:
        """Check cooldown and budget before executing a mutation.

        Raises:
            CircuitBreakerError: If cooldown is active or budget is exceeded.
        """
        key = (namespace.strip().lower(), resource.strip().lower())
        now = time.monotonic()

        with self._lock:
            # 1. Check total mutation budget
            if self._total_mutations >= self.max_mutations:
                raise CircuitBreakerError(
                    f"Incident mutation budget exhausted: maximum {self.max_mutations} "
                    "mutations reached. Further autonomous mutations are blocked; escalate."
                )

            # 2. Check per-resource cooldown
            last_time = self._history.get(key)
            if last_time is not None:
                elapsed = now - last_time
                if elapsed < self.cooldown_seconds:
                    remaining = int(self.cooldown_seconds - elapsed)
                    raise CircuitBreakerError(
                        f"Circuit breaker active: workload '{namespace}/{resource}' was mutated "
                        f"{int(elapsed)}s ago. Cooldown remaining: {remaining}s. "
                        "Repeated rapid mutations are blocked to prevent flapping."
                    )

            # Record mutation
            self._history[key] = now
            self._total_mutations += 1

    def reset(self) -> None:
        """Reset history and budget (used between incidents or tests)."""
        with self._lock:
            self._history.clear()
            self._total_mutations = 0


# Global singleton used by mutation tools
circuit_breaker = MutationCircuitBreaker()
