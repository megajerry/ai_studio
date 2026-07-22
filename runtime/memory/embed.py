"""Embeddings — text → float vector, routed through the model registry (ADR-0005).

Mirrors the M3b provider pattern (:mod:`runtime.model`): a provider-agnostic
registry entry names the model; a keyless :class:`DryRunEmbeddingProvider` is the
default so the whole memory path runs with NO network and NO key; real adapters
(google/openai/voyage) read their key from env INSIDE themselves (ADR-0011,
invariant 5) and are never exercised in tests. Anthropic has no embedding model,
so the registry's embedding tier points at a non-Anthropic provider (ADR-0005).

The dry-run embedding is DETERMINISTIC: identical text → identical vector, and
similar text → closer vectors (signed feature-hashing of character n-grams, then
L2-normalized). This makes cosine search reproducible and meaningful offline.

Nothing sensitive is emitted here — :func:`embed` returns a vector; the memory
API is the only place that touches the event log, and it logs counts/ids only.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Any, Optional, Protocol, runtime_checkable

from ..model.registry import Registry, Tier, load_registry

#: Dimensionality of the dry-run embedding. Fixed so every dry-run vector in a
#: store shares a length and cosine is well-defined. Real providers return their
#: own dimensionality; a deployment uses one embedding model consistently.
EMBED_DIM = 256

#: Character n-gram size for the dry-run feature hasher. 3 gives good overlap for
#: near-identical text while staying cheap.
_NGRAM = 3

_DRY_RUN_ENV = "MODELS_DRY_RUN"


# --- Pure vector helpers (unit-tested, no DB / no network) ------------------


def l2_normalize(vec: list[float]) -> list[float]:
    """Return ``vec`` scaled to unit L2 norm (unchanged if it is the zero vector)."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return list(vec)
    return [x / norm for x in vec]


def dryrun_vector(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic, L2-normalized embedding of ``text`` via signed feature hashing.

    Lower-cased character n-grams (plus whole words) are hashed into ``dim``
    buckets with a per-feature sign, so identical text yields the identical vector
    and text sharing n-grams lands nearby in cosine space. No randomness, no state.
    """
    vec = [0.0] * dim
    norm_text = " ".join(text.lower().split())
    if not norm_text:
        return vec

    features: list[str] = []
    padded = f" {norm_text} "
    for i in range(len(padded) - _NGRAM + 1):
        features.append(padded[i : i + _NGRAM])
    # Whole-word features reinforce token-level similarity on top of the n-grams.
    features.extend(norm_text.split())

    for feat in features:
        digest = hashlib.sha256(feat.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if (digest[4] & 1) else -1.0
        vec[idx] += sign

    return l2_normalize(vec)


# --- Provider abstraction (mirrors runtime.model.providers) -----------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Anything that can turn text into a vector for a given model id."""

    name: str

    def available(self) -> bool:
        ...

    def embed(self, model_id: str, text: str) -> list[float]:
        ...


class DryRunEmbeddingProvider:
    """Keyless, networkless, deterministic embeddings — the default."""

    name = "dryrun"

    def available(self) -> bool:
        return True

    def embed(self, model_id: str, text: str) -> list[float]:
        return dryrun_vector(text)


class _HttpEmbeddingProvider:
    """Shared structural base for real HTTP embedding adapters.

    Structural only: the key is read from env inside the adapter and never logged
    or returned; ``httpx`` is imported lazily so the keyless path never needs it.
    Subclasses set ``name``, ``_api_key_env`` and implement :meth:`_request`.
    Tests never call :meth:`embed`.
    """

    name = "http"
    _api_key_env = ""

    def available(self) -> bool:
        return bool(os.environ.get(self._api_key_env))

    def _api_key(self) -> str:
        key = os.environ.get(self._api_key_env)
        if not key:
            raise RuntimeError(
                f"{self.name}: {self._api_key_env} is not set (use dry-run instead)"
            )
        return key

    def _request(self, model_id: str, text: str, api_key: str) -> list[float]:  # pragma: no cover - structural
        raise NotImplementedError

    def embed(self, model_id: str, text: str) -> list[float]:  # pragma: no cover - structural
        return self._request(model_id, text, self._api_key())


class GoogleEmbeddingProvider(_HttpEmbeddingProvider):
    """Google (Vertex/Gemini) text embeddings. Structural; key from env."""

    name = "google"
    _api_key_env = "GOOGLE_API_KEY"

    def _request(self, model_id: str, text: str, api_key: str) -> list[float]:  # pragma: no cover - structural
        import httpx  # lazy

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_id}:embedContent?key={api_key}"
        )
        body = {"content": {"parts": [{"text": text}]}}
        resp = httpx.post(url, json=body, timeout=60.0)
        resp.raise_for_status()
        return [float(x) for x in resp.json()["embedding"]["values"]]


class OpenAIEmbeddingProvider(_HttpEmbeddingProvider):
    """OpenAI embeddings. Structural; key from env."""

    name = "openai"
    _api_key_env = "OPENAI_API_KEY"

    def _request(self, model_id: str, text: str, api_key: str) -> list[float]:  # pragma: no cover - structural
        import httpx  # lazy

        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
        resp = httpx.post(
            f"{base}/v1/embeddings",
            json={"model": model_id, "input": text},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )
        resp.raise_for_status()
        return [float(x) for x in resp.json()["data"][0]["embedding"]]


class VoyageEmbeddingProvider(_HttpEmbeddingProvider):
    """Voyage AI embeddings. Structural; key from env."""

    name = "voyage"
    _api_key_env = "VOYAGE_API_KEY"

    def _request(self, model_id: str, text: str, api_key: str) -> list[float]:  # pragma: no cover - structural
        import httpx  # lazy

        resp = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            json={"model": model_id, "input": [text]},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )
        resp.raise_for_status()
        return [float(x) for x in resp.json()["data"][0]["embedding"]]


#: provider name -> factory. A provider absent here (or without its key) is served
#: by the dry-run embedder — the intended keyless default. DryRun is the fallback,
#: not a routable provider, so it is not in the map (mirrors runtime.model).
ADAPTERS: dict[str, Any] = {
    GoogleEmbeddingProvider.name: GoogleEmbeddingProvider,
    OpenAIEmbeddingProvider.name: OpenAIEmbeddingProvider,
    VoyageEmbeddingProvider.name: VoyageEmbeddingProvider,
}


def _dry_run_forced() -> bool:
    return os.environ.get(_DRY_RUN_ENV, "").strip() in {"1", "true", "yes", "on"}


def _embedding_model(registry: Registry) -> Optional[str]:
    """Resolve the embedding model id from the registry's ``embedding`` tier."""
    for model_id in registry.policy.candidates(Tier.EMBEDDING):
        if registry.get(model_id) is not None:
            return model_id
    by_tier = registry.by_tier(Tier.EMBEDDING)
    return by_tier[0].id if by_tier else None


def select_embedding_provider(
    provider_name: Optional[str], *, force_dry_run: bool = False
) -> EmbeddingProvider:
    """Pick the embedding provider: dry-run when forced / no adapter / no key."""
    if force_dry_run or _dry_run_forced() or not provider_name:
        return DryRunEmbeddingProvider()
    factory = ADAPTERS.get(provider_name)
    if factory is None:
        return DryRunEmbeddingProvider()
    adapter = factory()
    if not adapter.available():
        return DryRunEmbeddingProvider()
    return adapter


def embed(
    text: str,
    *,
    registry: Optional[Registry] = None,
    force_dry_run: bool = False,
) -> list[float]:
    """Embed ``text`` through the registry's embedding model.

    Resolves the embedding-tier model from the registry, selects its provider
    (dry-run when forced / no adapter / no key), and returns the vector. Runs
    fully keyless by default. Emits nothing — the memory API owns event logging.
    """
    if registry is None:
        registry = load_registry()
    model_id = _embedding_model(registry)
    spec = registry.get(model_id) if model_id else None
    provider_name = spec.provider if spec else None
    provider = select_embedding_provider(provider_name, force_dry_run=force_dry_run)
    # Dry-run ignores the model id but we still pass it for adapter parity.
    return provider.embed(model_id or "dryrun-embed", text)
