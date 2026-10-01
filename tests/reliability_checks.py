"""Deterministic checks for the live application reliability primitives."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autoapply.core.recovery import RecoverableFailure, RecoveryExecutor, RetryPolicy
from autoapply.core.state_machine import ApplicationState, WorkflowState


def main():
    wf = WorkflowState(ApplicationState.DISCOVERED)
    for state in (
        ApplicationState.QUALIFIED,
        ApplicationState.STARTED,
        ApplicationState.PROFILE_COMPLETE,
        ApplicationState.QUESTIONS_COMPLETE,
        ApplicationState.DOCUMENTS_COMPLETE,
        ApplicationState.READY_FOR_REVIEW,
        ApplicationState.SUBMITTED,
        ApplicationState.CONFIRMED,
    ):
        wf.transition(state)
    assert wf.state is ApplicationState.CONFIRMED
    assert wf.attempts == 8

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RecoverableFailure("transient")
        return "ok"

    assert RecoveryExecutor(RetryPolicy(max_attempts=3, base_delay_seconds=0)).run(flaky) == "ok"
    assert calls["n"] == 3
    print("ok reliability state machine and bounded recovery")


if __name__ == "__main__":
    main()
