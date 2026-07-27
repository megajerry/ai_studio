"""Shared pytest fixtures — no live credentials, all values are fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from spokesman.channel import CHANNEL_WHATSAPP
from spokesman.config import Settings

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
PHONE_NUMBER_ID = "1234567890"
STAKEHOLDER = "15550001111"
API_TOKEN = "test-api-token"


def make_settings(
    state_dir: Path,
    *,
    dry_run: bool = True,
    api_token: str = API_TOKEN,
    channel: str = CHANNEL_WHATSAPP,
) -> Settings:
    return Settings(
        channel=channel,
        phone_number_id=PHONE_NUMBER_ID,
        access_token="test-token",
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
        stakeholder_number=STAKEHOLDER,
        graph_api_base="https://graph.facebook.com",
        graph_api_version="v21.0",
        twilio_account_sid="ACtest",
        twilio_auth_token="twilio-auth",
        twilio_from_number="+15550000000",
        api_token=api_token,
        dry_run=dry_run,
        port=8080,
        state_dir=state_dir,
    )


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    (d / "inbox").mkdir(parents=True)
    (d / "status.md").write_text("# Studio status\n\nAll systems nominal.\n", "utf-8")
    return d


@pytest.fixture
def settings(state_dir: Path) -> Settings:
    return make_settings(state_dir, dry_run=True)
