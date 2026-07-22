"""FilesystemTool — confinement + per-op capabilities (no DB, temp root only)."""

from __future__ import annotations

import os

import pytest

from runtime.capabilities import Capability
from runtime.tools.filesystem import FilesystemTool


@pytest.fixture
def tool(tmp_path):
    (tmp_path / "hello.txt").write_text("hi")
    return FilesystemTool(root=tmp_path)


def test_capabilities_are_per_operation(tool):
    assert tool.capabilities_for(op="read") == frozenset({Capability.FS_READ})
    assert tool.capabilities_for(op="write") == frozenset({Capability.FS_WRITE})
    assert tool.capabilities_for(op="delete") == frozenset({Capability.FS_DELETE})


def test_read_inside_root(tool):
    r = tool.execute(op="read", path="hello.txt")
    assert r.ok and r.output == "hi"


def test_write_then_read_inside_root(tool):
    w = tool.execute(op="write", path="sub/dir/new.txt", content="data")
    assert w.ok
    r = tool.execute(op="read", path="sub/dir/new.txt")
    assert r.ok and r.output == "data"


def test_delete_inside_root(tool):
    assert tool.execute(op="delete", path="hello.txt").ok
    assert not tool.execute(op="read", path="hello.txt").ok


def test_read_missing_file_is_handled(tool):
    r = tool.execute(op="read", path="nope.txt")
    assert r.ok is False and "not found" in r.error


def test_rejects_parent_escape(tool):
    r = tool.execute(op="read", path="../../etc/passwd")
    assert r.ok is False and "confinement" in r.error


def test_rejects_absolute_path(tool):
    r = tool.execute(op="read", path="/etc/passwd")
    assert r.ok is False and "confinement" in r.error


def test_rejects_write_escape(tool):
    r = tool.execute(op="write", path="../evil.txt", content="x")
    assert r.ok is False and "confinement" in r.error
    # And nothing was written outside the root.
    assert not (tool.root.parent / "evil.txt").exists()


def test_rejects_symlink_escape(tool, tmp_path):
    # A symlink inside the root pointing outside must not grant access.
    outside_dir = tmp_path.parent / "outside_secret"
    outside_dir.mkdir()
    secret = outside_dir / "secret.txt"
    secret.write_text("top secret")
    link = tool.root / "link"
    try:
        os.symlink(outside_dir, link)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlink
        pytest.skip("symlinks not supported here")
    r = tool.execute(op="read", path="link/secret.txt")
    assert r.ok is False and "confinement" in r.error


def test_delete_refuses_directory(tool):
    (tool.root / "d").mkdir()
    r = tool.execute(op="delete", path="d")
    assert r.ok is False


def test_unknown_op(tool):
    r = tool.execute(op="chmod", path="hello.txt")
    assert r.ok is False and "unknown op" in r.error
