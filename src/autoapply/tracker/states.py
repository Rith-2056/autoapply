"""Explicit application state machine and event types.

The current status of an application is *derived* from its events: each
event type maps to a target status, and only deliberate transitions are
allowed. Anything else raises ``InvalidTransition`` so no code path can put
an application into a nonsensical state.
"""

from __future__ import annotations

from enum import Enum


class ApplicationStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLYING = "APPLYING"
    NEEDS_INPUT = "NEEDS_INPUT"
    SUBMITTED = "SUBMITTED"
    CONFIRMATION_RECEIVED = "CONFIRMATION_RECEIVED"
    ASSESSMENT = "ASSESSMENT"
    INTERVIEW = "INTERVIEW"
    FINAL_INTERVIEW = "FINAL_INTERVIEW"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


S = ApplicationStatus

ACTIVE_PIPELINE = [S.SUBMITTED, S.CONFIRMATION_RECEIVED, S.ASSESSMENT, S.INTERVIEW, S.FINAL_INTERVIEW, S.OFFER]
TERMINAL = {S.REJECTED, S.WITHDRAWN}
NEEDS_ATTENTION = {S.NEEDS_INPUT, S.ASSESSMENT, S.INTERVIEW, S.FINAL_INTERVIEW, S.OFFER, S.FAILED}

# Allowed transitions. A status may always transition to itself (idempotent events).
TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    S.DISCOVERED: {S.READY_TO_APPLY, S.APPLYING, S.WITHDRAWN, S.SUBMITTED},
    S.READY_TO_APPLY: {S.APPLYING, S.WITHDRAWN, S.DISCOVERED},
    S.APPLYING: {S.NEEDS_INPUT, S.SUBMITTED, S.FAILED, S.WITHDRAWN, S.READY_TO_APPLY},
    S.NEEDS_INPUT: {S.APPLYING, S.SUBMITTED, S.FAILED, S.WITHDRAWN},
    S.FAILED: {S.APPLYING, S.READY_TO_APPLY, S.NEEDS_INPUT, S.WITHDRAWN, S.SUBMITTED},
    S.SUBMITTED: {S.CONFIRMATION_RECEIVED, S.ASSESSMENT, S.INTERVIEW, S.FINAL_INTERVIEW, S.OFFER, S.REJECTED, S.WITHDRAWN},
    S.CONFIRMATION_RECEIVED: {S.ASSESSMENT, S.INTERVIEW, S.FINAL_INTERVIEW, S.OFFER, S.REJECTED, S.WITHDRAWN},
    S.ASSESSMENT: {S.INTERVIEW, S.FINAL_INTERVIEW, S.OFFER, S.REJECTED, S.WITHDRAWN},
    S.INTERVIEW: {S.FINAL_INTERVIEW, S.OFFER, S.REJECTED, S.WITHDRAWN, S.ASSESSMENT},
    S.FINAL_INTERVIEW: {S.OFFER, S.REJECTED, S.WITHDRAWN},
    S.OFFER: {S.REJECTED, S.WITHDRAWN},
    S.REJECTED: set(),
    S.WITHDRAWN: {S.READY_TO_APPLY},
    S.UNKNOWN: set(ApplicationStatus),
}


class InvalidTransition(Exception):
    pass


def can_transition(frm: ApplicationStatus, to: ApplicationStatus) -> bool:
    return frm == to or to in TRANSITIONS.get(frm, set())


def transition(frm: ApplicationStatus, to: ApplicationStatus) -> ApplicationStatus:
    if not can_transition(frm, to):
        raise InvalidTransition(f"{frm.value} -> {to.value} is not allowed")
    return to


class EventType(str, Enum):
    # automation
    JOB_DISCOVERED = "JobDiscovered"
    APPLICATION_STARTED = "ApplicationStarted"
    APPLICATION_OPENED = "ApplicationOpened"
    FIELDS_FILLED = "FieldsFilled"
    RESUME_UPLOADED = "ResumeUploaded"
    QUESTION_DETECTED = "QuestionDetected"
    INPUT_REQUESTED = "InputRequested"
    ANSWER_PROVIDED = "AnswerProvided"
    REVIEW_REQUESTED = "ReviewRequested"
    APPLICATION_SUBMITTED = "ApplicationSubmitted"
    APPLICATION_FAILED = "ApplicationFailed"
    APPLICATION_SKIPPED = "ApplicationSkipped"
    APPLICATION_RESUMED = "ApplicationResumed"
    CAPTCHA_ENCOUNTERED = "CaptchaEncountered"
    NOTE = "Note"
    # email-derived
    CONFIRMATION_EMAIL_RECEIVED = "ConfirmationEmailReceived"
    ASSESSMENT_RECEIVED = "AssessmentReceived"
    INTERVIEW_INVITATION_RECEIVED = "InterviewInvitationReceived"
    INTERVIEW_SCHEDULED = "InterviewScheduled"
    FINAL_INTERVIEW_RECEIVED = "FinalInterviewReceived"
    OFFER_RECEIVED = "OfferReceived"
    REJECTED = "Rejected"
    RECRUITER_CONTACT = "RecruiterContact"
    ADDITIONAL_INFO_REQUESTED = "AdditionalInformationRequested"
    STATUS_UPDATE = "StatusUpdate"
    DEADLINE_DETECTED = "DeadlineDetected"
    # user
    WITHDRAWN = "Withdrawn"
    STATUS_SET_MANUALLY = "StatusSetManually"


# Which status an event moves the application to (None = no status change).
EVENT_STATUS: dict[EventType, ApplicationStatus | None] = {
    EventType.JOB_DISCOVERED: S.DISCOVERED,
    EventType.APPLICATION_STARTED: S.APPLYING,
    EventType.APPLICATION_OPENED: S.APPLYING,
    EventType.FIELDS_FILLED: None,
    EventType.RESUME_UPLOADED: None,
    EventType.QUESTION_DETECTED: None,
    EventType.INPUT_REQUESTED: S.NEEDS_INPUT,
    EventType.ANSWER_PROVIDED: S.APPLYING,
    EventType.REVIEW_REQUESTED: S.NEEDS_INPUT,
    EventType.APPLICATION_SUBMITTED: S.SUBMITTED,
    EventType.APPLICATION_FAILED: S.FAILED,
    EventType.APPLICATION_SKIPPED: S.WITHDRAWN,
    EventType.APPLICATION_RESUMED: S.APPLYING,
    EventType.CAPTCHA_ENCOUNTERED: S.NEEDS_INPUT,
    EventType.NOTE: None,
    EventType.CONFIRMATION_EMAIL_RECEIVED: S.CONFIRMATION_RECEIVED,
    EventType.ASSESSMENT_RECEIVED: S.ASSESSMENT,
    EventType.INTERVIEW_INVITATION_RECEIVED: S.INTERVIEW,
    EventType.INTERVIEW_SCHEDULED: S.INTERVIEW,
    EventType.FINAL_INTERVIEW_RECEIVED: S.FINAL_INTERVIEW,
    EventType.OFFER_RECEIVED: S.OFFER,
    EventType.REJECTED: S.REJECTED,
    EventType.RECRUITER_CONTACT: None,
    EventType.ADDITIONAL_INFO_REQUESTED: None,
    EventType.STATUS_UPDATE: None,
    EventType.DEADLINE_DETECTED: None,
    EventType.WITHDRAWN: S.WITHDRAWN,
    EventType.STATUS_SET_MANUALLY: None,  # target carried in metadata
}


def status_after_event(current: ApplicationStatus, event: EventType, metadata: dict | None = None) -> ApplicationStatus:
    """Return the status after applying ``event``; raises InvalidTransition if not allowed.

    A later-stage email arriving for an application that is *behind* (e.g. an
    assessment for one still SUBMITTED) is a normal forward move. An earlier-stage
    email arriving late (a confirmation after an assessment) is ignored rather
    than moving the application backwards.
    """
    if event == EventType.STATUS_SET_MANUALLY:
        target = ApplicationStatus((metadata or {}).get("status", current.value))
        return transition(current, target)
    target = EVENT_STATUS.get(event)
    if target is None or target == current:
        return current
    if target in ACTIVE_PIPELINE and current in ACTIVE_PIPELINE and ACTIVE_PIPELINE.index(target) < ACTIVE_PIPELINE.index(current):
        return current  # stale, earlier-stage signal
    if current in TERMINAL and target in ACTIVE_PIPELINE:
        return current  # a rejected application does not come back to life from an old email
    return transition(current, target)


STATUS_LABELS = {
    S.DISCOVERED: "Discovered",
    S.READY_TO_APPLY: "Ready to apply",
    S.APPLYING: "Applying",
    S.NEEDS_INPUT: "Needs input",
    S.SUBMITTED: "Submitted",
    S.CONFIRMATION_RECEIVED: "Confirmed",
    S.ASSESSMENT: "Assessment",
    S.INTERVIEW: "Interview",
    S.FINAL_INTERVIEW: "Final interview",
    S.OFFER: "Offer",
    S.REJECTED: "Rejected",
    S.WITHDRAWN: "Withdrawn",
    S.FAILED: "Failed",
    S.UNKNOWN: "Unknown",
}
