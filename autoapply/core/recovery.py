from dataclasses import dataclass
import time


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 2.0


class RecoverableFailure(Exception):
    """An operation that may succeed after retrying."""


class RecoveryExecutor:
    def __init__(self, policy: RetryPolicy | None = None):
        self.policy = policy or RetryPolicy()

    def run(self, operation, on_failure=None):
        last_error = None
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                return operation()
            except RecoverableFailure as exc:
                last_error = exc
                if on_failure:
                    on_failure(attempt, exc)
                if attempt < self.policy.max_attempts:
                    time.sleep(self.policy.base_delay_seconds * attempt)
        raise last_error
