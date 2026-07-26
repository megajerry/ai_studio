"""Unit tests for the cold-start readiness self-check (:mod:`runtime.readiness`).

The high-value assertion: the config-coverage check DETECTS a seeded env var the
code reads but cold-start never documents/collects (and PASSES on the real repo),
and it prints only env-var NAMES, never secret VALUES (invariant 5 / ADR-0011).
The import / migration-sequence / compose / demo checks pass on this repo. The
DB-backed apply-clean step degrades to HOST-REQUIRED (never FAIL) with no DB.
"""

from __future__ import annotations

from pathlib import Path

from runtime import readiness as R
from runtime.readiness import Status


# --- env-read AST scanner ---------------------------------------------------


def test_scan_detects_direct_reads():
    src = (
        "import os\n"
        "a = os.environ.get('ALPHA_VAR')\n"
        "b = os.getenv('BETA_VAR', 'default')\n"
        "c = os.environ['GAMMA_VAR']\n"
        "env = os.environ\n"
        "d = env.get('DELTA_VAR')\n"
    )
    found = R.scan_env_reads_source(src)
    assert {"ALPHA_VAR", "BETA_VAR", "GAMMA_VAR", "DELTA_VAR"} <= set(found)
    # No default -> flagged required; with a default -> not.
    assert found["ALPHA_VAR"] is True
    assert found["GAMMA_VAR"] is True  # subscript never has a default
    assert found["BETA_VAR"] is False


def test_scan_resolves_name_constant_indirection():
    # This is how the provider/search adapters read their keys.
    src = (
        "import os\n"
        "_API_KEY_ENV = 'SEEDED_PROVIDER_KEY'\n"
        "class P:\n"
        "    _api_key_env = 'OTHER_PROVIDER_KEY'\n"
        "    def go(self):\n"
        "        return os.environ.get(self._api_key_env)\n"
        "def top():\n"
        "    return os.environ.get(_API_KEY_ENV)\n"
    )
    found = R.scan_env_reads_source(src)
    assert "SEEDED_PROVIDER_KEY" in found
    assert "OTHER_PROVIDER_KEY" in found


def test_scan_ignores_non_env_string_literals():
    src = (
        "import os\n"
        "d = {'SOME_KEY': 1}\n"           # dict literal, not an env read
        "s = 'ANOTHER_CONSTANT'\n"        # bare literal, never read via env
        "x = os.environ.get('REAL_VAR')\n"
    )
    found = R.scan_env_reads_source(src)
    assert set(found) == {"REAL_VAR"}


# --- config coverage --------------------------------------------------------


def _seed_repo(tmp_path: Path, *, read_var: str, code_line: str) -> Path:
    """Build a minimal fake repo: a runtime file that reads ``read_var`` but with
    empty ``.env.example`` + onboarding, so ``read_var`` is an undocumented gap."""
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "mod.py").write_text("import os\n" + code_line + "\n")
    (tmp_path / ".env.example").write_text("AI_STUDIO_ENV=dev\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "onboarding.sh").write_text(
        'prompt_var POSTGRES_PASSWORD "pw" genpass\n'
    )
    return tmp_path


def test_config_coverage_flags_seeded_missing_secret(tmp_path):
    root = _seed_repo(
        tmp_path,
        read_var="SEEDED_MISSING_API_KEY",
        code_line="k = os.environ['SEEDED_MISSING_API_KEY']",
    )
    res = R.check_config_coverage(root)
    assert res.status is Status.FAIL
    # Named explicitly so the operator knows exactly what cold-start must collect.
    assert "SEEDED_MISSING_API_KEY" in " ".join(res.details)


def test_config_coverage_non_secret_gap_is_not_a_failure(tmp_path):
    # A non-secret knob with a code default is reported, but never fails the check.
    root = _seed_repo(
        tmp_path,
        read_var="SEEDED_TUNING_KNOB",
        code_line="k = os.environ.get('SEEDED_TUNING_KNOB', '5')",
    )
    res = R.check_config_coverage(root)
    assert res.status is Status.PASS
    assert "SEEDED_TUNING_KNOB" in " ".join(res.details)


def test_config_coverage_passes_on_real_repo():
    res = R.check_config_coverage(R.REPO_ROOT)
    assert res.status is Status.PASS, res.details


def test_config_coverage_prints_names_not_values(tmp_path, monkeypatch):
    # Seed an UNDOCUMENTED secret read AND give it a value in the environment;
    # the report must list the NAME but never the VALUE.
    root = _seed_repo(
        tmp_path,
        read_var="LEAKY_SECRET_TOKEN",
        code_line="k = os.environ['LEAKY_SECRET_TOKEN']",
    )
    monkeypatch.setenv("LEAKY_SECRET_TOKEN", "sk-DO-NOT-LOG-THIS-VALUE")
    res = R.check_config_coverage(root)
    blob = res.summary + " " + " ".join(res.details)
    assert "LEAKY_SECRET_TOKEN" in blob
    assert "sk-DO-NOT-LOG-THIS-VALUE" not in blob


# --- documentation parsers --------------------------------------------------


def test_parse_env_example_includes_commented_names():
    names = R.parse_env_example(R.REPO_ROOT / ".env.example")
    assert {"POSTGRES_PASSWORD", "ANTHROPIC_API_KEY", "TAVILY_API_KEY"} <= names
    # Commented `# GRAFANA_ADMIN_USER=` line still documents the name.
    assert "GRAFANA_ADMIN_USER" in names


def test_parse_onboarding_extracts_prompted_vars():
    names = R.parse_onboarding(R.REPO_ROOT / "scripts" / "onboarding.sh")
    assert {"POSTGRES_PASSWORD", "WHATSAPP_ACCESS_TOKEN", "CURSOR_API_KEY"} <= names


# --- other checks on the real repo ------------------------------------------


def test_imports_check_passes():
    assert R.check_imports().status is Status.PASS


def test_compose_coherence_passes():
    res = R.check_compose_coherence()
    assert res.status is Status.PASS, res.details


def test_migrations_sequence_is_contiguous():
    # Static sequence must be clean; the DB apply-clean step is PASS with a
    # reachable DB, else HOST-REQUIRED — never FAIL on this repo.
    res = R.check_migrations()
    assert res.status in (Status.PASS, Status.HOST_REQUIRED), res.details
    assert any("contiguous" in d for d in res.details)


def test_render_marks_fail_and_sets_nonzero_intent():
    results = [
        R.CheckResult("ok", Status.PASS, "fine"),
        R.CheckResult("bad", Status.FAIL, "broken", ["reason"]),
    ]
    out = R.render(results)
    assert "[FAIL] bad" in out
    assert "NOT READY" in out
    assert any(r.failed for r in results)


def test_render_all_pass_is_ready():
    results = [R.CheckResult("ok", Status.PASS, "fine")]
    out = R.render(results)
    assert "READY (no FAILs)" in out
    assert not any(r.failed for r in results)
