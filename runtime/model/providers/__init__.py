"""Provider adapters + the name→adapter map the call wrapper resolves against.

``ADAPTERS`` maps a registry ``provider`` string to its adapter class. A provider
absent from this map (e.g. ``openweight``) has no concrete adapter yet, so the
call wrapper serves it in dry-run — the intended keyless default. The
:class:`DryRunProvider` is not in the map; it is the fallback, not a routable
provider.
"""

from __future__ import annotations

from typing import Callable

from .anthropic import AnthropicProvider
from .base import Completion, Message, Provider, messages_char_len
from .dryrun import DryRunProvider
from .google import GoogleProvider
from .openai import OpenAIProvider

#: provider name -> factory. Factories take no args so callers never inject
#: secrets; each adapter reads its own key from env inside itself (ADR-0011).
ADAPTERS: dict[str, Callable[[], Provider]] = {
    AnthropicProvider.name: AnthropicProvider,
    OpenAIProvider.name: OpenAIProvider,
    GoogleProvider.name: GoogleProvider,
}


def get_adapter(provider: str) -> Provider | None:
    """Instantiate the adapter for ``provider``, or ``None`` if none is wired."""
    factory = ADAPTERS.get(provider)
    return factory() if factory is not None else None


__all__ = [
    "ADAPTERS",
    "AnthropicProvider",
    "Completion",
    "DryRunProvider",
    "GoogleProvider",
    "Message",
    "OpenAIProvider",
    "Provider",
    "get_adapter",
    "messages_char_len",
]
