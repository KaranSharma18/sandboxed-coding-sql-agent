"""Stop rules, checked after every failed attempt (design section 5).

Order matters: budget first (checked before spending, in the driver), then repeated failures,
going in circles, policy streaks, and finally repairs used up.
"""

from __future__ import annotations

from dataclasses import dataclass

from fixloop_core.hashing import sha256_text
from fixloop_core.models import AttemptRecord, LoopState, Outcome
from fixloop_core.outputs import FailureClass
from fixloop_core.traces import normalize_message


def text_signature(failure_class: FailureClass | None, message: str) -> str:
    """A short, stable signature for a failure that has no test trace (apply, policy, SQL errors)."""
    name = failure_class.value if failure_class else "unknown"
    return f"{name}:{sha256_text(normalize_message(message)[:400])[:20]}"


@dataclass(frozen=True)
class StopDecision:
    outcome: Outcome
    reason: str


def register_failure(state: LoopState, attempt: AttemptRecord) -> StopDecision | None:
    """Update the loop state with a failed attempt and decide whether to stop.

    - The first time a failure signature repeats, the next repair is escalated (stronger model,
      deep trace, explicit "try something different"). A second repeat ends the task.
    - Two policy-violating attempts in a row end the task.
    """
    signature = attempt.failure_signature
    if signature:
        repeats = state.signatures.count(signature)
        attempt.signature_repeated = repeats >= 1
        state.signatures.append(signature)
        if repeats >= 2:
            return StopDecision(
                Outcome.no_progress, f"the same failure repeated three times (signature {signature})"
            )
        if repeats == 1:
            state.escalate_next = True
    if attempt.failure_class == FailureClass.policy_violation:
        state.policy_streak += 1
        if state.policy_streak >= 2:
            return StopDecision(Outcome.policy_violation, "two policy-violating attempts in a row")
    else:
        state.policy_streak = 0
    return None


def should_escalate(state: LoopState, attempt_no: int, max_repairs: int, escalation_enabled: bool) -> bool:
    """The last repair always escalates; an earlier one does after a repeated signature."""
    if not escalation_enabled or attempt_no == 0:
        return False
    return state.escalate_next or attempt_no == max_repairs
