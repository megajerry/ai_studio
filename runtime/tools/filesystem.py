"""Filesystem tool — read/write/delete confined to an allowed root.

Least privilege in two dimensions:

- **Separate capabilities per operation** — a read costs only ``fs.read``, a
  write ``fs.write``, a delete ``fs.delete`` (🔴). :meth:`capabilities_for`
  reports exactly what a given call needs so the policy engine can gate it.
- **Path confinement** — every target is resolved (following symlinks) and must
  land inside the configured root. Absolute paths, ``..`` escapes, and symlinks
  that point outside the root are all rejected before any I/O happens, so a tool
  call can never read or clobber a host file outside its sandbox.
"""

from __future__ import annotations

from pathlib import Path

from ..capabilities import Capability
from .base import Tool, ToolResult

_READ = frozenset({Capability.FS_READ})
_WRITE = frozenset({Capability.FS_WRITE})
_DELETE = frozenset({Capability.FS_DELETE})

_OP_CAPS: dict[str, frozenset[Capability]] = {
    "read": _READ,
    "list": _READ,
    "write": _WRITE,
    "delete": _DELETE,
}


class PathEscapeError(Exception):
    """Raised when a requested path would resolve outside the allowed root."""


class FilesystemTool(Tool):
    """Read/write/delete files under a single confined root directory."""

    name = "filesystem"
    required_capabilities = _READ | _WRITE | _DELETE

    def __init__(self, root: str | Path) -> None:
        # Resolve once so all confinement checks compare against the real path.
        self.root = Path(root).resolve()

    def capabilities_for(self, **kwargs) -> frozenset[Capability]:
        op = kwargs.get("op", "read")
        try:
            return _OP_CAPS[op]
        except KeyError:
            # Unknown op → require everything, so the policy engine cannot be
            # tricked into under-permissioning a call we don't understand.
            return self.required_capabilities

    def _resolve(self, rel_path: str) -> Path:
        """Resolve ``rel_path`` inside the root or raise :class:`PathEscapeError`.

        ``resolve()`` collapses ``..`` and follows symlinks, so a symlink inside
        the root that points outside is caught by the containment check.
        """
        if not rel_path or not str(rel_path).strip():
            raise PathEscapeError("empty path")
        p = Path(rel_path)
        if p.is_absolute():
            raise PathEscapeError(f"absolute paths are not allowed: {rel_path!r}")
        target = (self.root / p).resolve()
        if target != self.root and self.root not in target.parents:
            raise PathEscapeError(f"path escapes confined root: {rel_path!r}")
        return target

    def execute(self, **kwargs) -> ToolResult:
        op = kwargs.get("op", "read")
        path = kwargs.get("path")
        if op not in _OP_CAPS:
            return ToolResult(ok=False, error=f"unknown op: {op!r}")
        if path is None:
            return ToolResult(ok=False, error="'path' is required")

        try:
            target = self._resolve(path)
        except PathEscapeError as exc:
            # Confinement violation is a hard failure, not a handled result.
            return ToolResult(ok=False, error=f"path confinement: {exc}")

        if op == "read":
            if not target.is_file():
                return ToolResult(ok=False, error="file not found")
            return ToolResult(
                ok=True, output=target.read_text(), metadata={"path": str(target)}
            )

        if op == "list":
            if not target.is_dir():
                return ToolResult(ok=False, error="not a directory")
            names = sorted(c.name for c in target.iterdir())
            return ToolResult(ok=True, output=names, metadata={"path": str(target)})

        if op == "write":
            content = kwargs.get("content", "")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            return ToolResult(
                ok=True,
                output={"bytes": len(content)},
                metadata={"path": str(target)},
            )

        if op == "delete":
            if not target.exists():
                return ToolResult(ok=False, error="file not found")
            if target.is_dir():
                return ToolResult(ok=False, error="refusing to delete a directory")
            target.unlink()
            return ToolResult(ok=True, output={"deleted": str(target)})

        return ToolResult(ok=False, error=f"unhandled op: {op!r}")  # pragma: no cover
