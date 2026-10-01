from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone


class ApplicationState(str, Enum):
    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    STARTED = "started"
    PROFILE_COMPLETE = "profile_complete"
    QUESTIONS_COMPLETE = "questions_complete"
    DOCUMENTS_COMPLETE = "documents_complete"
    READY_FOR_REVIEW = "ready_for_review"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    WAITING = "waiting"
    FAILED = "failed"


_ALLOWED = {
    ApplicationState.DISCOVERED: {ApplicationState.QUALIFIED, ApplicationState.FAILED},
    ApplicationState.QUALIFIED: {ApplicationState.STARTED, ApplicationState.FAILED},
    ApplicationState.STARTED: {ApplicationState.PROFILE_COMPLETE, ApplicationState.WAITING, ApplicationState.FAILED},
    ApplicationState.PROFILE_COMPLETE: {ApplicationState.QUESTIONS_COMPLETE, ApplicationState.FAILED},
    ApplicationState.QUESTIONS_COMPLETE: {ApplicationState.DOCUMENTS_COMPLETE, ApplicationState.FAILED},
    ApplicationState.DOCUMENTS_COMPLETE: {ApplicationState.READY_FOR_REVIEW, ApplicationState.FAILED},
    ApplicationState.READY_FOR_REVIEW: {ApplicationState.SUBMITTED, ApplicationState.WAITING, ApplicationState.FAILED},
    ApplicationState.SUBMITTED: {ApplicationState.CONFIRMED, ApplicationState.WAITING, ApplicationState.FAILED},
    ApplicationState.WAITING: {ApplicationState.STARTED, ApplicationState.FAILED},
    ApplicationState.CONFIRMED: set(),
    ApplicationState.FAILED: set(),
}


@dataclass
class WorkflowState:
    state: ApplicationState
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    attempts: int = 0
    notes: list[str] = field(default_factory=list)

    def transition(self, new_state: ApplicationState):
        if new_state not in _ALLOWED[self.state]:
            raise ValueError(f"Invalid transition: {self.state} -> {new_state}")
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()
        self.attempts += 1
