from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .schemas import Input, Note, Revision


class AssignResponse(Revision):
    assigned_to: int = Field(gt=0)
    coverage_id: int | None = Field(default=None, gt=0)
    instruction: Note


class TransferResponse(Input):
    to_coverage_id: int = Field(gt=0)
    request_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=100)]
    reason: Note


class FinishResponse(Input):
    note: Note


class OpenContact(Input):
    contact_role: Literal['primary', 'backup']
    purpose: Note


class RecordAttempt(Input):
    expected_attempt_id: int = Field(ge=0)
    channel: Literal['phone', 'in_person']
    outcome: Literal['no_answer', 'requested_callback', 'reached']
    note: Note


class ScheduleFollowup(Input):
    delay_minutes: Literal[15, 30, 60, 120]
    checklist: Note


class FinishFollowup(Input):
    result: Literal['stable', 'needs_attention']
    note: Note
