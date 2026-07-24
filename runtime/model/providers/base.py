"""Provider abstraction — the boundary the call wrapper talks to (ADR-0012).

A :class:`Provider` turns ``(model_id, messages)`` into a :class:`Completion`
carrying the generated text and its :class:`Usage`. Concrete adapters
(anthropic/openai/google) read their API key from the environment **inside
themselves** and never return or log it (ADR-0011, invariant 5). The
:class:`DryRunProvider` needs no key at all.

Agents never construct or call a provider directly — everything goes through
:func:`runtime.model.call.call_model`, the single instrumented call site.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..registry import Usage

# A message is the usual chat shape: {"role": "user"|"system"|..., "content": str}.
Message = dict[str, Any]


class ProviderFallback(Exception):
    """A provider could not serve this call — the caller should try the fallback.

    Raised by an adapter (e.g. the subprocess-based
    :class:`~runtime.model.providers.cursor_cli.CursorCliProvider` on a
    timeout / non-zero exit / unparseable output) to signal a *recoverable*
    failure: the call is NOT lost, it should be retried on the next model in the
    routed tier's fallback chain (a metered model). The single instrumented call
    site (:func:`runtime.model.call.call_model`) catches this in its provider-
    dispatch step and walks to that fallback. It is deliberately distinct from a
    hard :class:`RuntimeError` (a misconfiguration bug) so that only genuine
    provider-unavailable conditions trigger a fallback rather than masking bugs.
    """


class Completion:
    """The result of one model call: generated text + token usage.

    Deliberately a plain object (not pydantic) so adapters can attach a raw
    provider payload without it being serialized into events — only ``.usage``
    numbers and cost ever reach the log.
    """

    __slots__ = ("text", "usage", "model_id", "provider", "raw")

    def __init__(
        self,
        *,
        text: str,
        usage: Usage,
        model_id: str,
        provider: str,
        raw: Any = None,
    ) -> None:
        self.text = text
        self.usage = usage
        self.model_id = model_id
        self.provider = provider
        self.raw = raw

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Completion(provider={self.provider!r}, model_id={self.model_id!r}, "
            f"usage={self.usage!r})"
        )


@runtime_checkable
class Provider(Protocol):
    """Anything that can complete a chat request for a model.

    ``name`` matches the ``provider`` field on a :class:`~runtime.model.registry.ModelSpec`.
    ``available()`` reports whether the provider can actually run (its key is
    present) so the call wrapper can fall back to dry-run when it can't.
    """

    name: str

    def available(self) -> bool:
        ...

    def complete(self, model_id: str, messages: list[Message], **opts: Any) -> Completion:
        ...


def messages_char_len(messages: list[Message]) -> int:
    """Total characters across message contents — the basis for synthetic usage.

    Kept here (not in the dry-run provider) so any adapter can reuse it for a
    cheap pre-call token estimate.
    """
    total = 0
    for m in messages or []:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
        else:
            total += len(str(content))
    return total
