"""Docker sandbox runner — run a command in a locked-down throwaway container.

Implements the :class:`~runtime.tools.shell.SandboxRunner` protocol
(``run(command, **kwargs) -> (exit_code, stdout, stderr)``) using the ``docker``
CLI via :mod:`subprocess`. Architecture §8 (zero trust): the command runs with no
host access — the container is the only thing it can touch.

**Strong-by-default hardening** (each is a separate, independently verifiable
flag; see ``build_invocation``):

- ``--network none`` — no network at all by default (config: ``SANDBOX_NETWORK``).
- ``--rm`` — the container is deleted the moment it exits (no state accretion).
- ``--user <uid:gid>`` — runs **non-root** (default ``65534:65534`` = nobody).
- ``--read-only`` rootfs + ``--tmpfs /tmp`` — the image filesystem is immutable;
  only a small tmpfs and the explicitly-mounted workdir are writable.
- ``--memory`` / ``--cpus`` / ``--pids-limit`` — hard resource ceilings.
- ``--cap-drop ALL`` + ``--security-opt no-new-privileges`` — drop every Linux
  capability and forbid privilege escalation.
- **Scoped bind mount only.** At most one host directory (``SANDBOX_WORKDIR``) is
  bind-mounted, at a fixed container path. Mounting the host root ``/`` is
  refused outright (:class:`SandboxConfigError`).
- **No host env / secrets leak in.** The container receives **only** the env vars
  named in ``allowed_env`` (default: none). Their values are handed to the
  ``docker`` CLI process, never written into the argv (so they can't be read off
  ``ps``), and every other host variable — every secret — is withheld
  (CLAUDE.md invariant 5, ADR-0011).
- **Timeout kills.** A command exceeding ``SANDBOX_TIMEOUT_S`` has its container
  force-removed and returns :data:`TIMEOUT_EXIT_CODE` (124).

Nothing here imports Docker at module load; ``docker`` is only invoked when
:meth:`DockerSandboxRunner.run` runs (and mocked out in tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterable, Mapping

#: Fixed path the scoped workdir is bind-mounted to inside the container.
CONTAINER_WORKDIR = "/workspace"

#: Exit code returned when a command is killed for exceeding its timeout
#: (matches the GNU ``timeout(1)`` convention).
TIMEOUT_EXIT_CODE = 124

#: Operational env vars the ``docker`` CLI itself may need to reach the daemon.
#: These configure the *client*; they are NOT forwarded into the container.
_DOCKER_CLI_ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CONTEXT",
)

# Strong defaults (all overridable via env / constructor).
_DEFAULT_IMAGE = "ai-studio-sandbox:latest"
_DEFAULT_NETWORK = "none"
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_MEMORY = "256m"
_DEFAULT_CPUS = "1.0"
_DEFAULT_USER = "65534:65534"  # nobody:nogroup — present in virtually every image
_DEFAULT_PIDS_LIMIT = 256


class SandboxConfigError(ValueError):
    """Raised when a sandbox is configured unsafely (e.g. mounting host root)."""


def _abspath(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


class DockerSandboxRunner:
    """Run a command inside a hardened, throwaway Docker container.

    Config resolves from constructor args first, then environment, then a strong
    built-in default:

    ==========================  ====================  ==============================
    env var                     constructor arg       default
    ==========================  ====================  ==============================
    ``SANDBOX_IMAGE``           ``image``             ``ai-studio-sandbox:latest``
    ``SANDBOX_NETWORK``         ``network``           ``none``
    ``SANDBOX_TIMEOUT_S``       ``timeout_s``         ``30``
    ``SANDBOX_MEMORY``          ``memory``            ``256m``
    ``SANDBOX_CPUS``            ``cpus``              ``1.0``
    ``SANDBOX_WORKDIR``         ``workdir``           *(none — no bind mount)*
    ``SANDBOX_USER``            ``user``              ``65534:65534``
    ==========================  ====================  ==============================

    ``allowed_env`` is the **explicit allowlist** of host env var names to forward
    into the container (default: none). Anything not listed — every secret — stays
    out. Values are passed to the ``docker`` client's environment, never embedded
    in the argv.
    """

    def __init__(
        self,
        *,
        image: str | None = None,
        network: str | None = None,
        timeout_s: float | None = None,
        memory: str | None = None,
        cpus: str | None = None,
        workdir: str | None = None,
        user: str | None = None,
        allowed_env: Iterable[str] | None = None,
        pids_limit: int | None = None,
        read_only_rootfs: bool = True,
        workdir_readonly: bool = False,
        docker_bin: str = "docker",
        env: Mapping[str, str] | None = None,
    ) -> None:
        source = os.environ if env is None else env
        self._env: Mapping[str, str] = source

        self.image = image or source.get("SANDBOX_IMAGE") or _DEFAULT_IMAGE
        self.network = network or source.get("SANDBOX_NETWORK") or _DEFAULT_NETWORK
        self.memory = memory or source.get("SANDBOX_MEMORY") or _DEFAULT_MEMORY
        self.cpus = cpus or source.get("SANDBOX_CPUS") or _DEFAULT_CPUS
        self.user = user or source.get("SANDBOX_USER") or _DEFAULT_USER
        self.pids_limit = pids_limit if pids_limit is not None else _DEFAULT_PIDS_LIMIT
        self.read_only_rootfs = read_only_rootfs
        self.workdir_readonly = workdir_readonly
        self.docker_bin = docker_bin

        timeout_raw = timeout_s if timeout_s is not None else source.get("SANDBOX_TIMEOUT_S")
        self.timeout_s = float(timeout_raw) if timeout_raw not in (None, "") else _DEFAULT_TIMEOUT_S

        raw_workdir = workdir if workdir is not None else source.get("SANDBOX_WORKDIR")
        self.workdir: str | None = self._validate_workdir(raw_workdir) if raw_workdir else None

        self.allowed_env: tuple[str, ...] = tuple(allowed_env or ())

    @staticmethod
    def _validate_workdir(path: str) -> str:
        """Resolve ``path`` and refuse to mount the host root."""
        resolved = _abspath(path)
        if resolved == os.sep:
            raise SandboxConfigError(
                "refusing to bind-mount the host root '/' into the sandbox"
            )
        return resolved

    # -- command construction (pure; unit-tested without Docker) --------------

    def build_invocation(
        self, command: str, container_name: str
    ) -> tuple[list[str], dict[str, str]]:
        """Build ``(argv, env)`` for ``docker run`` — no side effects.

        ``argv`` never contains a secret value; ``env`` is the minimal environment
        handed to the ``docker`` client (operational passthrough + the values of
        the ``allowed_env`` allowlist only).
        """
        argv: list[str] = [
            self.docker_bin,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            self.network,
            "--user",
            self.user,
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            str(self.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
        ]

        if self.read_only_rootfs:
            # Immutable image fs; give the process a small writable /tmp.
            argv += ["--read-only", "--tmpfs", "/tmp"]

        if self.workdir is not None:
            mode = "ro" if self.workdir_readonly else "rw"
            argv += [
                "-v",
                f"{self.workdir}:{CONTAINER_WORKDIR}:{mode}",
                "-w",
                CONTAINER_WORKDIR,
            ]

        # Forward ONLY allowlisted env, and by name only (docker reads the value
        # from its own environment) so nothing lands in the argv / process list.
        for name in self.allowed_env:
            if name in self._env:
                argv += ["-e", name]

        argv += [self.image, "sh", "-c", command]
        return argv, self._docker_cli_env()

    def _docker_cli_env(self) -> dict[str, str]:
        """Minimal environment for the ``docker`` client subprocess.

        Operational passthrough (so the CLI can reach the daemon) plus the values
        of allowlisted vars (so the by-name ``-e`` forwarding resolves). Every
        other host variable — every secret — is excluded.
        """
        env: dict[str, str] = {
            k: self._env[k] for k in _DOCKER_CLI_ENV_PASSTHROUGH if k in self._env
        }
        for name in self.allowed_env:
            if name in self._env:
                env[name] = self._env[name]
        return env

    # -- execution -----------------------------------------------------------

    def run(self, command: str, **kwargs: object) -> tuple[int, str, str]:
        """Run ``command`` in a fresh hardened container.

        Returns ``(exit_code, stdout, stderr)``. On timeout the container is
        force-removed and :data:`TIMEOUT_EXIT_CODE` is returned.
        """
        container_name = self._container_name()
        argv, env = self.build_invocation(command, container_name)
        try:
            proc = subprocess.run(
                argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Killing the `docker run` client does NOT stop the container, so
            # explicitly force-remove it, then report the timeout.
            self._force_remove(container_name, env)
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
            note = f"sandbox: command exceeded {self.timeout_s}s timeout; container killed"
            stderr = f"{stderr}\n{note}" if stderr else note
            return TIMEOUT_EXIT_CODE, stdout, stderr
        return proc.returncode, proc.stdout, proc.stderr

    def _force_remove(self, container_name: str, env: Mapping[str, str]) -> None:
        """Best-effort ``docker rm -f`` of a container (timeout cleanup)."""
        try:
            subprocess.run(
                [self.docker_bin, "rm", "-f", container_name],
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            # Cleanup is best-effort; --rm will also reap it on exit.
            pass

    @staticmethod
    def _container_name() -> str:
        return f"aistudio-sbx-{uuid.uuid4().hex[:12]}"

    def docker_available(self) -> bool:
        """True if a ``docker`` binary is on PATH. Never raises; never runs a
        container. Callers may use this to decide whether to wire the runner."""
        return shutil.which(self.docker_bin) is not None


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value
