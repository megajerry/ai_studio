"""Registry loading + cost computation + policy resolution (no DB, no network)."""

from __future__ import annotations

import pytest

from runtime.model.registry import (
    Registry,
    Tier,
    Usage,
    cost_usd,
    load_registry,
    resolve_registry_path,
)


def test_example_registry_loads_and_seeds_shortlist():
    reg = load_registry()
    # The shortlist models from docs/model-shortlist.md are all present.
    for model_id in [
        "claude-opus-4.8",
        "claude-sonnet-5",
        "claude-haiku-4.5",
        "gemini-3.1-pro",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "deepseek-v4.5",
        "text-embedding-005",
    ]:
        assert reg.get(model_id) is not None, model_id
    # Every spec carries provenance + date (ADR-0005).
    for spec in reg.models.values():
        assert spec.provenance
        assert spec.provenance_date


def test_resolve_path_defaults_to_example(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_STUDIO_MODELS_FILE", raising=False)
    assert resolve_registry_path().name == "models.example.yaml"
    # Explicit env override wins.
    monkeypatch.setenv("AI_STUDIO_MODELS_FILE", str(tmp_path / "x.yaml"))
    assert resolve_registry_path() == tmp_path / "x.yaml"


def test_tiers_are_assigned():
    reg = load_registry()
    assert reg.get("claude-opus-4.8").tier is Tier.PM
    assert reg.get("claude-sonnet-5").tier is Tier.MID
    assert reg.get("claude-haiku-4.5").tier is Tier.CHEAP
    assert reg.get("text-embedding-005").tier is Tier.EMBEDDING


def test_cursor_is_coding_tier_flat_rate_only():
    reg = load_registry()
    cursor = reg.get("cursor-composer")
    assert cursor is not None
    assert cursor.provider == "cursor-cli"
    assert cursor.tier is Tier.CODING
    # Flat-rate Ultra: no per-token marginal cost modelled in the registry.
    assert cursor.price_in == 0.0 and cursor.price_out == 0.0
    # It appears ONLY in the coding tier's chain — never cheap/classify/embed.
    for tier in (Tier.CHEAP, Tier.MID, Tier.PM, Tier.EMBEDDING):
        assert "cursor-composer" not in reg.policy.candidates(tier)
    assert reg.policy.candidates(Tier.CODING)[0] == "cursor-composer"
    # The coding chain carries a metered fallback after the flat-rate substrate.
    assert "claude-opus-4.8" in reg.policy.candidates(Tier.CODING)


def test_coding_tier_downshifts_to_mid():
    reg = load_registry()
    assert reg.policy.cheaper_tier(Tier.CODING) is Tier.MID


def test_cost_usd_is_tokens_times_registry_price():
    reg = load_registry()
    opus = reg.get("claude-opus-4.8")  # 5 / 25 per 1M
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    # 1M in * $5 + 1M out * $25 = $30.
    assert cost_usd(opus, usage) == pytest.approx(30.0)


def test_cost_usd_discounts_cached_input():
    reg = load_registry()
    opus = reg.get("claude-opus-4.8")  # cache_read_multiplier 0.1
    # 1M input, all cached, no output: 1M * $5 * 0.1 = $0.50.
    usage = Usage(input_tokens=1_000_000, cached_tokens=1_000_000, output_tokens=0)
    assert cost_usd(opus, usage) == pytest.approx(0.5)


def test_embeddings_have_zero_output_cost():
    reg = load_registry()
    emb = reg.get("text-embedding-005")  # 0.006 / 0
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    assert cost_usd(emb, usage) == pytest.approx(0.006)


def test_policy_tier_for_and_fallback():
    reg = load_registry()
    pol = reg.policy
    assert pol.tier_for("plan", "high") is Tier.PM
    assert pol.tier_for("classify", "standard") is Tier.CHEAP
    # Unlisted task_type falls through to default.
    assert pol.tier_for("totally-unknown", "standard") is Tier.MID


def test_policy_candidates_and_downshift():
    reg = load_registry()
    pol = reg.policy
    assert pol.candidates(Tier.PM)[0] == "claude-opus-4.8"
    assert pol.cheaper_tier(Tier.PM) is Tier.MID
    assert pol.cheaper_tier(Tier.MID) is Tier.CHEAP
    # cheap has no cheaper tier configured.
    assert pol.cheaper_tier(Tier.CHEAP) is None


def test_duplicate_model_id_rejected():
    data = {
        "models": [
            {"id": "dup", "provider": "x", "tier": "mid", "price_in": 1,
             "price_out": 1, "context_window": 1},
            {"id": "dup", "provider": "x", "tier": "mid", "price_in": 1,
             "price_out": 1, "context_window": 1},
        ]
    }
    with pytest.raises(ValueError):
        Registry.from_dict(data)


def test_unknown_quality_and_tier_errors():
    reg = load_registry()
    with pytest.raises(KeyError):
        reg.policy.tier_for("plan", "bogus")
