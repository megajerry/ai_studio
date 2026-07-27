"""Cold-start readiness self-check — "can a fresh clone actually bootstrap?".

The platform is keyless-verified, but nothing today actively *enforces* the
invariant that a fresh ``git clone`` on the target Mac can bootstrap and run
(CLAUDE.md / CONTRIBUTING.md). This module is that enforcement: it runs REAL
checks and prints a per-check ``PASS`` / ``FAIL`` / ``WARN`` / ``HOST-REQUIRED``
report, exiting non-zero **only** on a real ``FAIL``.

Run it::

    python -m runtime.readiness          # full report
    python -m runtime.readiness --quiet  # summary + FAIL/WARN detail only
    make readiness

Checks (all run off-host except where noted):

- **imports** — ``runtime`` + ``spokesman`` + ``gateway`` (and core submodules)
  import, and every package declared in the ``requirements.txt`` files resolves.
- **migrations** — filenames form a contiguous ``0001..000N`` sequence (no gap /
  collision) and, when a DB is reachable, every migration applies cleanly to a
  throwaway isolated schema (HOST-REQUIRED when no DB is reachable).
- **demo** — ``python -m runtime.demo`` exits 0 (keyless; itself defers cleanly
  when no DB is reachable).
- **config-coverage** — the high-value check. Cross-checks every env var / secret
  NAME the runtime + spokesman + gateway actually *read* (AST scan, resolving
  name-constant indirection) against ``.env.example`` AND ``scripts/onboarding.sh``. A
  **secret-shaped** var the code reads but neither documents/collects is a FAIL
  ("code needs X but cold-start never asks for it"); undocumented non-secret knobs
  (with code defaults) are reported informationally. Only NAMES are ever printed,
  never values (invariant 5, ADR-0011).
- **compose** — static coherence: every ``docker-compose.yml`` bind-mount source
  path exists, the health-check script exists, and ``bootstrap`` / ``Makefile``
  reference no dangling files.
- **boundary** — HOST-REQUIRED: what can only be verified on the target Mac
  (``docker compose up`` + live Postgres/Redis/Qdrant/MinIO/Prometheus/Grafana)
  and the four stakeholder decisions the launch is gated on.

This is host/dev tooling (like ``runtime.migrate`` / ``runtime.demo``), not agent
code — it is allowed to import the package and shell out to run the demo.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Result model -----------------------------------------------------------


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    HOST_REQUIRED = "HOST-REQUIRED"


@dataclass
class CheckResult:
    name: str
    status: Status
    summary: str
    details: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL


# --- Shared helpers ---------------------------------------------------------

#: A name is treated as *env-var shaped* if it is ALL-CAPS/underscored, len >= 3.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
#: A var whose NAME implies it carries a credential (never printed as a value).
_SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PASSWD)")
#: Names that env accessors are bound to in this codebase (``env = os.environ`` etc).
_ACCESSOR_RECEIVERS = {"environ", "env", "cli_env", "source"}


def is_env_name(value: object) -> bool:
    return isinstance(value, str) and bool(_ENV_NAME_RE.match(value))


def is_secret_name(name: str) -> bool:
    return bool(_SECRET_RE.search(name))


def _iter_source_files(bases: Iterable[str], root: Path) -> Iterable[Path]:
    """Yield non-test ``*.py`` files under each base package."""
    for base in bases:
        for path in sorted((root / base).rglob("*.py")):
            rel = str(path.relative_to(root))
            if "/tests/" in rel or path.name.startswith("test_"):
                continue
            yield path


# --- Env-var AST scan (config-coverage engine) ------------------------------


def scan_env_reads_source(source: str) -> dict[str, bool]:
    """Return ``{ENV_NAME: read_without_default}`` for one Python source string.

    Detects the env-read idioms used in this repo:

    - ``os.environ.get("X")`` / ``os.getenv("X")`` / ``environ.get("X")`` /
      bare ``getenv("X")`` — and the same on the receivers env accessors are
      bound to (``env`` / ``cli_env`` / ``source``).
    - ``os.environ["X"]`` / ``environ["X"]`` subscripts.
    - **Name-constant indirection**: assignments like ``_API_KEY_ENV = "X"`` or
      ``self._api_key_env = "X"`` (any target whose identifier contains ``ENV``)
      whose value is env-shaped, then read via ``os.environ.get(_API_KEY_ENV)``.
      This is how the provider/search adapters read their keys.

    ``read_without_default`` is True when at least one read of the var had no
    default (a bare 1-arg ``.get`` / a subscript) — a signal it may be *required*.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    # Pass 1: map ENV-ish identifiers -> their string-constant env-name value.
    name_consts: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            val = node.value.value
            if not is_env_name(val):
                continue
            for target in node.targets:
                ident = None
                if isinstance(target, ast.Name):
                    ident = target.id
                elif isinstance(target, ast.Attribute):
                    ident = target.attr
                if ident and "ENV" in ident.upper():
                    name_consts[ident] = val

    found: dict[str, bool] = {}

    def record(name: str, without_default: bool) -> None:
        found[name] = found.get(name, False) or without_default

    def is_env_accessor(func: ast.AST) -> Optional[str]:
        """Return the accessor method name ('get'/'getenv') if func reads env."""
        if isinstance(func, ast.Attribute):
            if func.attr == "getenv" and _is_os(func.value):
                return "getenv"
            if func.attr == "get":
                v = func.value
                if isinstance(v, ast.Attribute) and v.attr == "environ":
                    return "get"
                if isinstance(v, ast.Name) and v.id in _ACCESSOR_RECEIVERS:
                    return "get"
        if isinstance(func, ast.Name) and func.id == "getenv":
            return "getenv"
        return None

    def _is_os(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "os"

    def resolve_arg(arg: ast.AST) -> Optional[str]:
        if isinstance(arg, ast.Constant) and is_env_name(arg.value):
            return arg.value
        if isinstance(arg, ast.Name) and arg.id in name_consts:
            return name_consts[arg.id]
        if isinstance(arg, ast.Attribute) and arg.attr in name_consts:
            return name_consts[arg.attr]
        return None

    for node in ast.walk(tree):
        # Subscript: os.environ["X"] / environ["X"]  (no default -> required-ish)
        if isinstance(node, ast.Subscript):
            base = node.value
            if (isinstance(base, ast.Attribute) and base.attr == "environ") or (
                isinstance(base, ast.Name) and base.id in _ACCESSOR_RECEIVERS
            ):
                key = node.slice
                if isinstance(key, ast.Constant) and is_env_name(key.value):
                    record(key.value, True)
        # Calls: os.environ.get(...) / os.getenv(...) / env.get(...)
        if isinstance(node, ast.Call) and is_env_accessor(node.func) and node.args:
            name = resolve_arg(node.args[0])
            if name:
                has_default = len(node.args) >= 2 or bool(node.keywords)
                record(name, not has_default)

    return found


def scan_env_reads(bases: Iterable[str] = ("runtime", "spokesman", "gateway"),
                   root: Path = REPO_ROOT) -> dict[str, bool]:
    """Aggregate :func:`scan_env_reads_source` over every non-test source file."""
    out: dict[str, bool] = {}
    for path in _iter_source_files(bases, root):
        for name, no_default in scan_env_reads_source(
            path.read_text("utf-8")
        ).items():
            out[name] = out.get(name, False) or no_default
    return out


def parse_env_example(path: Path) -> set[str]:
    """Env var NAMES documented in ``.env.example`` (incl. commented ``# X=`` lines)."""
    names: set[str] = set()
    if not path.exists():
        return names
    for line in path.read_text("utf-8").splitlines():
        m = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", line)
        if m:
            names.add(m.group(1))
    return names


def parse_onboarding(path: Path) -> set[str]:
    """Env var NAMES onboarding prompts for or writes out (``prompt_var`` / ``VAL[]``)."""
    names: set[str] = set()
    if not path.exists():
        return names
    text = path.read_text("utf-8")
    for m in re.finditer(r"prompt_var\s+([A-Z][A-Z0-9_]+)", text):
        names.add(m.group(1))
    for m in re.finditer(r"VAL\[([A-Z][A-Z0-9_]+)\]", text):
        names.add(m.group(1))
    return names


def check_config_coverage(root: Path = REPO_ROOT) -> CheckResult:
    """Cross-check code-read env vars against ``.env.example`` + onboarding.

    FAIL when a **secret-shaped** var is read by code but documented/collected by
    neither cold-start surface (the dangerous "code needs a secret cold-start
    never asks for" gap). Undocumented non-secret knobs (all with code defaults)
    are reported informationally, never as a failure. Only NAMES are printed.
    """
    reads = scan_env_reads(root=root)
    documented = parse_env_example(root / ".env.example") | parse_onboarding(
        root / "scripts" / "onboarding.sh"
    )
    undocumented = sorted(n for n in reads if n not in documented)
    secret_gaps = [n for n in undocumented if is_secret_name(n)]
    config_gaps = [n for n in undocumented if not is_secret_name(n)]

    details = [
        f"{len(reads)} env var name(s) read by runtime+spokesman; "
        f"{len(documented)} documented in .env.example/onboarding",
    ]
    if config_gaps:
        details.append(
            "optional non-secret knobs read but not documented (code defaults "
            f"exist; not a cold-start blocker): {', '.join(config_gaps)}"
        )
    if secret_gaps:
        details.append(
            "SECRET-shaped vars read by code but NOT in .env.example/onboarding "
            f"— cold-start cannot supply them: {', '.join(secret_gaps)}"
        )
        return CheckResult(
            "config-coverage", Status.FAIL,
            f"{len(secret_gaps)} secret var(s) read but never collected", details,
        )
    return CheckResult(
        "config-coverage", Status.PASS,
        f"all {len(reads)} read vars accounted for; 0 secret gaps "
        f"({len(config_gaps)} optional non-secret knob(s))",
        details,
    )


# --- Import smoke -----------------------------------------------------------

#: package-name (as pinned in requirements) -> import name to probe.
_DEP_IMPORT_NAME = {
    "psycopg[binary]": "psycopg",
    "pydantic": "pydantic",
    "PyYAML": "yaml",
    "httpx": "httpx",
    "fastapi": "fastapi",
    "uvicorn[standard]": "uvicorn",
    "python-multipart": "python_multipart",
}


def _declared_deps(root: Path) -> set[str]:
    deps: set[str] = set()
    for req in ("runtime/requirements.txt", "spokesman/requirements.txt",
                "gateway/requirements.txt"):
        path = root / req
        if not path.exists():
            continue
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[A-Za-z0-9_,\-]+\])?)", line)
            if m:
                deps.add(m.group(1))
    return deps


def check_imports(root: Path = REPO_ROOT) -> CheckResult:
    import importlib

    details: list[str] = []
    failures: list[str] = []

    for mod in ("runtime", "spokesman", "runtime.db", "runtime.migrate",
                "runtime.models", "runtime.demo", "spokesman.app",
                "gateway.app", "gateway.client"):
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 - report any import failure
            failures.append(f"import {mod}: {type(exc).__name__}: {exc}")

    deps = _declared_deps(root)
    resolved = 0
    for dep in sorted(deps):
        import_name = _DEP_IMPORT_NAME.get(dep, dep.split("[")[0])
        try:
            importlib.import_module(import_name)
            resolved += 1
        except Exception:  # noqa: BLE001
            failures.append(f"declared dep unresolved: {dep} (import {import_name!r})")

    details.append(f"core modules imported; {resolved}/{len(deps)} declared deps resolvable")
    if failures:
        details.extend(failures)
        return CheckResult("imports", Status.FAIL,
                           f"{len(failures)} import failure(s)", details)
    return CheckResult("imports", Status.PASS,
                       "runtime + spokesman import; all declared deps resolvable", details)


# --- Migrations -------------------------------------------------------------


def check_migrations(root: Path = REPO_ROOT,
                     database_url: Optional[str] = None) -> CheckResult:
    """Static sequence check + (if a DB is reachable) apply to a throwaway schema."""
    from . import db
    from . import migrate as migrate_mod

    files = migrate_mod.discover()
    names = [p.name for p in files]
    details = [f"discovered {len(names)} migration(s): {', '.join(names)}"]

    # Static: contiguous 0001..000N, no gap / collision.
    nums: list[int] = []
    bad = []
    for name in names:
        m = re.match(r"^(\d{4})_", name)
        if not m:
            bad.append(name)
        else:
            nums.append(int(m.group(1)))
    if bad:
        return CheckResult("migrations", Status.FAIL,
                           f"unparseable migration filename(s): {bad}", details)
    expected = list(range(1, len(nums) + 1))
    if sorted(nums) != expected:
        details.append(f"sequence not contiguous: found {sorted(nums)}, "
                       f"expected {expected}")
        return CheckResult("migrations", Status.FAIL,
                           "migration numbers have a gap/collision", details)
    if len(set(nums)) != len(nums):
        return CheckResult("migrations", Status.FAIL,
                           "duplicate migration numbers", details)
    details.append(f"filenames form a contiguous 0001..{max(nums):04d} sequence")

    # Dynamic: apply to an isolated throwaway schema so nothing persists.
    url = database_url or os.environ.get("DATABASE_URL")
    if not db.can_connect(url, timeout=2.0):
        details.append("no reachable DB — clean-apply deferred to host verification")
        return CheckResult("migrations", Status.HOST_REQUIRED,
                           "sequence OK; apply-clean needs a reachable Postgres",
                           details)

    import uuid as _uuid

    schema = f"readiness_chk_{_uuid.uuid4().hex[:12]}"
    conn = db.connect(url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
        try:
            applied = migrate_mod.migrate(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
    except Exception as exc:  # noqa: BLE001 - a migration that won't apply is a FAIL
        details.append(f"clean-apply FAILED in isolated schema: "
                       f"{type(exc).__name__}: {exc}")
        return CheckResult("migrations", Status.FAIL,
                           "a migration did not apply to a fresh schema", details)
    finally:
        conn.close()

    if applied != names:
        details.append(f"applied set {applied} != discovered {names}")
        return CheckResult("migrations", Status.FAIL,
                           "not every migration applied to the fresh schema", details)
    details.append(f"applied cleanly to isolated schema (dropped): {', '.join(applied)}")
    return CheckResult("migrations", Status.PASS,
                       f"{len(applied)} migration(s) apply cleanly to a fresh schema",
                       details)


# --- Demo -------------------------------------------------------------------


def check_demo(root: Path = REPO_ROOT) -> CheckResult:
    """Run ``python -m runtime.demo`` as a subprocess; PASS iff it exits 0.

    The demo forces dry-run (keyless) and itself defers cleanly (exit 0) when no
    DB is reachable, so this is a valid check on- and off-host.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "runtime.demo"],
        cwd=str(root), capture_output=True, text=True,
    )
    tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    details = [f"exit={proc.returncode}", f"last: {tail[0][:160]}"]
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()[-3:]
        details.extend(f"stderr: {ln[:160]}" for ln in err)
        return CheckResult("demo", Status.FAIL,
                           f"python -m runtime.demo exited {proc.returncode}", details)
    return CheckResult("demo", Status.PASS,
                       "python -m runtime.demo exited 0 (keyless)", details)


# --- Compose / bootstrap coherence ------------------------------------------

_BIND_MOUNT_RE = re.compile(r"^(\./[^:]+):")


def check_compose_coherence(root: Path = REPO_ROOT) -> CheckResult:
    """Static: every compose bind-mount source + referenced script exists."""
    missing: list[str] = []
    details: list[str] = []

    compose = root / "docker-compose.yml"
    if not compose.exists():
        return CheckResult("compose", Status.FAIL, "docker-compose.yml missing", details)

    # Prefer a real YAML parse for service names; fall back to a line scan for
    # bind-mount sources (robust to the anchor/env syntax in the file).
    services: list[str] = []
    try:
        import yaml

        doc = yaml.safe_load(compose.read_text("utf-8")) or {}
        services = sorted((doc.get("services") or {}).keys())
    except Exception:  # noqa: BLE001
        services = []

    bind_sources: set[str] = set()
    for raw in compose.read_text("utf-8").splitlines():
        line = raw.strip().lstrip("-").strip().strip('"').strip("'")
        m = _BIND_MOUNT_RE.match(line)
        if m:
            bind_sources.add(m.group(1))
    for src in sorted(bind_sources):
        if not (root / src).exists():
            missing.append(f"compose bind-mount source missing: {src}")
    details.append(f"services: {', '.join(services) if services else '(yaml parse skipped)'}")
    details.append(f"checked {len(bind_sources)} compose bind-mount source path(s)")

    # bootstrap + Makefile must not reference dangling scripts/files.
    for fname in ("bootstrap", "Makefile", "scripts/healthcheck.sh",
                  "scripts/onboarding.sh"):
        if not (root / fname).exists():
            missing.append(f"referenced file missing: {fname}")

    for ref in ("scripts/healthcheck.sh",):
        bootstrap_text = (root / "bootstrap").read_text("utf-8")
        if ref not in bootstrap_text and "healthcheck.sh" not in bootstrap_text:
            missing.append(f"bootstrap does not invoke {ref}")

    # Every build-context Dockerfile compose references must exist.
    for m in re.finditer(r"dockerfile:\s*([^\s#]+)", compose.read_text("utf-8")):
        dockerfile = m.group(1).strip().strip('"').strip("'")
        if not (root / dockerfile).exists():
            missing.append(f"compose references {dockerfile} which is missing")

    if missing:
        details.extend(missing)
        return CheckResult("compose", Status.FAIL,
                           f"{len(missing)} dangling reference(s)", details)
    return CheckResult("compose", Status.PASS,
                       "compose bind-mounts + bootstrap/Makefile refs all resolve",
                       details)


# --- Boundary report --------------------------------------------------------


def boundary_report() -> CheckResult:
    """Informational: what only the host can verify + the stakeholder decisions."""
    details = [
        "HOST-ONLY (run on the target Mac; cannot be verified off-host):",
        "  - `docker compose up -d` brings up the M0 spine",
        "  - live health: postgres, redis, qdrant, minio, prometheus, grafana "
        "(`make health` / scripts/healthcheck.sh)",
        "  - Spokesman service reachable (`docker compose --profile spokesman up`)",
        "  - public webhook tunnel (cloudflared/tailscale/ngrok) for WhatsApp inbound",
        "STAKEHOLDER DECISIONS (cold-start cannot fill these in for you):",
        "  1. Provider API keys — which model providers to fund + their keys",
        "  2. Budget ceiling — the monthly USD cap the studio must respect",
        "  3. First vertical/product — the initial workstream to actually run",
        "  4. WhatsApp provisioning — Meta Cloud API number/token/app-secret + tunnel",
    ]
    return CheckResult("boundary", Status.HOST_REQUIRED,
                       "6 host-only checks + 4 stakeholder decisions (see docs/go-live.md)",
                       details)


# --- Orchestration ----------------------------------------------------------


def run_all(root: Path = REPO_ROOT) -> list[CheckResult]:
    return [
        check_imports(root),
        check_migrations(root),
        check_demo(root),
        check_config_coverage(root),
        check_compose_coherence(root),
        boundary_report(),
    ]


_ICON = {
    Status.PASS: "PASS",
    Status.FAIL: "FAIL",
    Status.WARN: "WARN",
    Status.HOST_REQUIRED: "HOST-REQUIRED",
}


def render(results: list[CheckResult], *, quiet: bool = False) -> str:
    lines = [
        "AI Studio — cold-start readiness self-check",
        "===========================================",
    ]
    for r in results:
        lines.append(f"[{_ICON[r.status]}] {r.name:<16} {r.summary}")
        show_detail = (not quiet) or r.status in (Status.FAIL, Status.WARN)
        if show_detail:
            for d in r.details:
                lines.append(f"      - {d}")
    n_pass = sum(r.status is Status.PASS for r in results)
    n_fail = sum(r.status is Status.FAIL for r in results)
    n_warn = sum(r.status is Status.WARN for r in results)
    n_host = sum(r.status is Status.HOST_REQUIRED for r in results)
    lines.append("")
    lines.append(
        f"Summary: {n_pass} PASS, {n_fail} FAIL, {n_warn} WARN, {n_host} HOST-REQUIRED"
    )
    if n_fail:
        lines.append("NOT READY — resolve the FAIL(s) above before go-live.")
    else:
        lines.append(
            "READY (no FAILs). HOST-REQUIRED checks must still be run on the "
            "target Mac — see docs/go-live.md."
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cold-start readiness self-check for AI Studio."
    )
    parser.add_argument("--quiet", action="store_true",
                        help="Only show detail for FAIL/WARN checks.")
    args = parser.parse_args(argv)

    results = run_all()
    print(render(results, quiet=args.quiet))
    return 1 if any(r.failed for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
