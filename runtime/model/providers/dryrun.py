"""Dry-run provider — runs the whole model path with NO network and NO key.

This is the default whenever ``MODELS_DRY_RUN=1`` or the selected model's real
provider key is absent, so the studio boots and every code path (route → call →
cost → events → spent_tokens) exercises end-to-end keyless. It returns a
deterministic stub text and SYNTHETIC token counts derived purely from the input
length, so tests and cost math are reproducible.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..registry import Usage
from .base import Completion, Message, messages_char_len

#: Rough chars-per-token used to synthesize input token counts (~4 is typical
#: for English). Only needs to be a stable, plausible constant.
_CHARS_PER_TOKEN = 4
#: Synthetic output tokens as a fraction of synthetic input tokens.
_OUTPUT_RATIO = 4  # output ~= input // 4


class DryRunProvider:
    """A keyless, networkless provider producing deterministic stubs."""

    name = "dryrun"

    def available(self) -> bool:
        # Always available — that is the whole point.
        return True

    def complete(
        self, model_id: str, messages: list[Message], **opts: Any
    ) -> Completion:
        chars = messages_char_len(messages)
        input_tokens = max(1, chars // _CHARS_PER_TOKEN)
        output_tokens = max(1, input_tokens // _OUTPUT_RATIO)
        # A short, deterministic fingerprint so identical inputs yield identical
        # stub text (useful for snapshotting) without echoing the prompt.
        digest = hashlib.sha256(
            f"{model_id}:{chars}".encode("utf-8")
        ).hexdigest()[:12]
        text = f"[dry-run:{model_id}] stub completion ({digest})"
        usage = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=0,
        )
        return Completion(
            text=text,
            usage=usage,
            model_id=model_id,
            provider=self.name,
            raw={"dry_run": True},
        )
