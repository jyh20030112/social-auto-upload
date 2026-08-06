from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_VERIFICATION = "waiting_verification"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TaskOperation(StrEnum):
    LOGIN = "login"
    UPLOAD_MATERIALS = "upload_materials"
    PUBLISH_VIDEO = "publish_video"
    PUBLISH_NOTE = "publish_note"


class TaskStage(StrEnum):
    QUEUED = "queued"
    VALIDATING_ACCOUNT = "validating_account"
    LAUNCHING_BROWSER = "launching_browser"
    UPLOADING_MATERIAL = "uploading_material"
    PROCESSING_MATERIALS = "processing_materials"
    FILLING_METADATA = "filling_metadata"
    WAITING_VERIFICATION = "waiting_verification"
    PUBLISHING = "publishing"
    PERSISTING_COOKIE = "persisting_cookie"
    COMPLETED = "completed"


TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.INTERRUPTED,
}

ACTIVE_TASK_STATUSES = {
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_VERIFICATION,
}
