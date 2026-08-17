"""Call and cost guard. Hard-stops; never warns and continues.

A budget that warns is not a budget. The failure this prevents is a loop over a
few thousand keys quietly turning into a few hundred dollars, and by the time a
warning is read the money is gone. So :meth:`BudgetGuard.reserve` raises
:class:`BudgetExceeded` **before** the call that would breach a limit, and the run
stops with a message saying exactly which limit and by how much.

Silent truncation is not an option either: finishing early and reporting on
partial data would put a coverage lie into the output, which is the one class of
error this whole tool is built to avoid.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from interlayer.core.errors import InterlayerError

DEFAULT_MAX_CALLS_PER_RUN = 1000
DEFAULT_MAX_ESTIMATED_COST_USD = 25.0


class BudgetExceeded(InterlayerError):
    """A call would breach the run's call or cost ceiling. The run stops here."""


@dataclass(frozen=True)
class CostEstimate:
    """One line of the ``--dry-run`` table."""

    provider: str
    n_keys: int
    usd: float


@dataclass
class BudgetGuard:
    """Per-run ceiling on calls and spend."""

    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN
    max_estimated_cost_usd: float = DEFAULT_MAX_ESTIMATED_COST_USD
    dry_run: bool = False
    calls_made: int = field(default=0, init=False)
    cost_spent_usd: float = field(default=0.0, init=False)

    # -- accounting --------------------------------------------------------

    @property
    def calls_remaining(self) -> int:
        return max(0, self.max_calls_per_run - self.calls_made)

    @property
    def cost_remaining_usd(self) -> float:
        return max(0.0, self.max_estimated_cost_usd - self.cost_spent_usd)

    def would_exceed(self, *, calls: int = 1, cost_usd: float = 0.0) -> str | None:
        """The reason this would breach a limit, or ``None``."""
        if self.calls_made + calls > self.max_calls_per_run:
            return (
                f"call limit reached: {self.calls_made} of {self.max_calls_per_run} "
                f"calls already made, {calls} more requested"
            )
        projected = self.cost_spent_usd + cost_usd
        if projected > self.max_estimated_cost_usd + 1e-9:
            return (
                f"cost limit reached: ${projected:.2f} would exceed the "
                f"${self.max_estimated_cost_usd:.2f} ceiling "
                f"(${self.cost_spent_usd:.2f} spent so far)"
            )
        return None

    def check(self, *, calls: int = 1, cost_usd: float = 0.0) -> None:
        """Raise if this call would breach a limit. Does not record anything."""
        reason = self.would_exceed(calls=calls, cost_usd=cost_usd)
        if reason is not None:
            raise BudgetExceeded(
                f"{reason}. The run stopped before making the call rather than "
                "silently returning partial results. Raise the limit explicitly, or "
                "narrow the key set."
            )

    def record(self, *, calls: int = 1, cost_usd: float = 0.0) -> None:
        """Book a call that has happened."""
        self.calls_made += calls
        self.cost_spent_usd += cost_usd

    def reserve(self, *, calls: int = 1, cost_usd: float = 0.0) -> None:
        """Check then record — what a provider calls immediately before a request.

        In ``dry_run`` the reservation still raises on a breach (so a dry run
        surfaces the same failure a real run would) but books nothing.
        """
        self.check(calls=calls, cost_usd=cost_usd)
        if not self.dry_run:
            self.record(calls=calls, cost_usd=cost_usd)

    # -- pre-flight --------------------------------------------------------

    def preflight(self, estimates: Sequence[CostEstimate]) -> None:
        """Validate a whole plan before any of it runs."""
        total_calls = sum(estimate.n_keys for estimate in estimates)
        total_cost = sum(estimate.usd for estimate in estimates)
        self.check(calls=total_calls, cost_usd=total_cost)

    def summary(self) -> dict[str, float | int | bool]:
        """Machine-readable state, for the run report."""
        return {
            "dry_run": self.dry_run,
            "calls_made": self.calls_made,
            "max_calls_per_run": self.max_calls_per_run,
            "cost_spent_usd": round(self.cost_spent_usd, 6),
            "max_estimated_cost_usd": self.max_estimated_cost_usd,
        }


def format_estimates(estimates: Sequence[CostEstimate]) -> str:
    """Render the dry-run table. Zero-cost providers are shown, not hidden."""
    if not estimates:
        return "no providers enabled; nothing to estimate"
    width = max(len(estimate.provider) for estimate in estimates)
    lines = [f"{'provider'.ljust(width)}  {'keys':>6}  {'est. USD':>9}"]
    for estimate in estimates:
        lines.append(
            f"{estimate.provider.ljust(width)}  {estimate.n_keys:>6}  {estimate.usd:>9.2f}"
        )
    total = sum(estimate.usd for estimate in estimates)
    lines.append(f"{'total'.ljust(width)}  {sum(e.n_keys for e in estimates):>6}  {total:>9.2f}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MAX_CALLS_PER_RUN",
    "DEFAULT_MAX_ESTIMATED_COST_USD",
    "BudgetExceeded",
    "BudgetGuard",
    "CostEstimate",
    "format_estimates",
]
