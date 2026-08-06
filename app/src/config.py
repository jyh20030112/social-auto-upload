from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_url: str
    max_browser_tasks: int = 2
    video_max_bytes: int = 2 * 1024 * 1024 * 1024
    image_max_bytes: int = 20 * 1024 * 1024
    login_timeout_seconds: int = 180
    video_timeout_seconds: int = 1800
    note_timeout_seconds: int = 900
    verification_timeout_seconds: int = 300
    check_timeout_seconds: int = 90
    shutdown_grace_seconds: int = 30
    terminal_retention_days: int = 7
    worker_enabled: bool = True
    debug: bool = False
    headless: bool = True

    @property
    def cookies_dir(self) -> Path:
        return self.data_dir / "cookies"

    @property
    def materials_dir(self) -> Path:
        return self.data_dir / "materials"

    @property
    def temporary_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def trash_dir(self) -> Path:
        return self.temporary_dir / "trash"

    @classmethod
    def from_env(cls) -> "Settings":
        default_data_dir = Path(__file__).resolve().parents[1] / "data"
        data_dir = Path(os.getenv("SAU_API_DATA_DIR", default_data_dir)).expanduser().resolve()
        database_url = os.getenv("SAU_API_DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'app.db'}")
        return cls(
            data_dir=data_dir,
            database_url=database_url,
            max_browser_tasks=_positive_int("SAU_API_MAX_BROWSER_TASKS", 2),
            video_max_bytes=_positive_int("SAU_API_VIDEO_MAX_BYTES", 2 * 1024 * 1024 * 1024),
            image_max_bytes=_positive_int("SAU_API_IMAGE_MAX_BYTES", 20 * 1024 * 1024),
            login_timeout_seconds=_positive_int("SAU_API_LOGIN_TIMEOUT_SECONDS", 180),
            video_timeout_seconds=_positive_int("SAU_API_VIDEO_TIMEOUT_SECONDS", 1800),
            note_timeout_seconds=_positive_int("SAU_API_NOTE_TIMEOUT_SECONDS", 900),
            verification_timeout_seconds=_positive_int("SAU_API_VERIFICATION_TIMEOUT_SECONDS", 300),
            check_timeout_seconds=_positive_int("SAU_API_CHECK_TIMEOUT_SECONDS", 90),
            shutdown_grace_seconds=_positive_int("SAU_API_SHUTDOWN_GRACE_SECONDS", 30),
            terminal_retention_days=_positive_int("SAU_API_TERMINAL_RETENTION_DAYS", 7),
            worker_enabled=os.getenv("SAU_API_WORKER_ENABLED", "true").lower() not in {"0", "false", "no"},
            debug=os.getenv("SAU_API_DEBUG", "false").lower() in {"1", "true", "yes"},
            headless=os.getenv("SAU_API_HEADLESS", "true").lower() not in {"0", "false", "no"},
        )

    def with_overrides(self, **changes) -> "Settings":
        return replace(self, **changes)

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.cookies_dir, self.materials_dir, self.temporary_dir, self.trash_dir):
            path.mkdir(parents=True, exist_ok=True)
