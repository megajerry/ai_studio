"""Configuration, loaded strictly from the environment (ADR-0011).

No secret or personal value is ever hardcoded or committed. Required values are
validated lazily so the app imports cleanly for tests and runs in dry-run mode
with no live credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from environment variables."""

    # --- WhatsApp / Meta Graph API ---
    phone_number_id: str
    access_token: str
    app_secret: str
    verify_token: str
    stakeholder_number: str
    graph_api_base: str
    graph_api_version: str

    # --- Control-plane auth (gates /notify + /digest/flush) ---
    api_token: str

    # --- Behavior ---
    dry_run: bool
    port: int
    state_dir: Path

    @property
    def messages_url(self) -> str:
        """Graph API endpoint for sending messages."""
        base = self.graph_api_base.rstrip("/")
        return f"{base}/{self.graph_api_version}/{self.phone_number_id}/messages"

    def require(self, *names: str) -> None:
        """Fail clearly if any named field is empty (skipped in dry-run)."""
        if self.dry_run:
            return
        missing = [n for n in names if not getattr(self, _FIELD_BY_ENV.get(n, n), "")]
        if missing:
            raise ConfigError(
                "Missing required environment variables (set them via "
                "scripts/onboarding.sh or run with SPOKESMAN_DRY_RUN=1): "
                + ", ".join(sorted(missing))
            )


# Map env var name -> Settings attribute, for clear error messages.
_FIELD_BY_ENV = {
    "WHATSAPP_PHONE_NUMBER_ID": "phone_number_id",
    "WHATSAPP_ACCESS_TOKEN": "access_token",
    "WHATSAPP_APP_SECRET": "app_secret",
    "WHATSAPP_VERIFY_TOKEN": "verify_token",
    "STAKEHOLDER_WHATSAPP_NUMBER": "stakeholder_number",
}


def load_settings() -> Settings:
    """Build a :class:`Settings` from the current environment."""
    repo_root = Path(__file__).resolve().parent.parent
    state_dir = Path(
        os.environ.get("AI_STUDIO_STATE_DIR", str(repo_root / "state"))
    ).expanduser()

    return Settings(
        phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
        app_secret=os.environ.get("WHATSAPP_APP_SECRET", ""),
        verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
        stakeholder_number=os.environ.get("STAKEHOLDER_WHATSAPP_NUMBER", ""),
        graph_api_base=os.environ.get("GRAPH_API_BASE", "https://graph.facebook.com"),
        graph_api_version=os.environ.get("GRAPH_API_VERSION", "v21.0"),
        api_token=os.environ.get("SPOKESMAN_API_TOKEN", ""),
        dry_run=_bool("SPOKESMAN_DRY_RUN", default=False),
        port=int(os.environ.get("SPOKESMAN_PORT", "8080")),
        state_dir=state_dir,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton for the running process."""
    return load_settings()
