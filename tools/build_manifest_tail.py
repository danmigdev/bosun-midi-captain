#!/usr/bin/env python3
"""Build the deterministic static tail of the Captain MANIFEST response.

The request id is dynamic, so the on-device protocol writes this prefix itself::

    {"type":"MANIFEST","id":<encoded id>

It can then copy ``firmware/lib/captain/manifest-tail.json`` verbatim.  This
tool derives that tail from the canonical core-message and shipped-plugin
sources, validates the assembled response against ``PluginRegistry.manifest()``,
and either atomically publishes or checks the artifact.

Plugin sources are evaluated with imports disabled.  Their manifest declarations
must therefore remain host-safe and cannot accidentally import CircuitPython
hardware modules while this build runs.
"""

from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_NAME = "firmware/lib/captain/manifest-tail.json"
MESSAGES_SOURCE = "firmware/lib/captain/messages.py"
REGISTRY_SOURCE = "firmware/lib/captain/plugin.py"
PLUGIN_SOURCES = (
    "firmware/lib/plugins/kemper.py",
    "firmware/lib/plugins/headrush_core.py",
    "firmware/lib/plugins/ampero.py",
    "firmware/lib/plugins/line6_helix.py",
    "firmware/lib/plugins/generic_midi.py",
)
EXPECTED_PLUGIN_COUNT = len(PLUGIN_SOURCES)
VALIDATION_ID = "__bosun_manifest_tail_validation__"


class ManifestBuildError(RuntimeError):
    """A source or generated manifest is unsafe, invalid, or stale."""


def _repo_file(repo_root: Path, relative_name: str) -> Path:
    """Resolve one required regular file without permitting a path escape."""

    try:
        root = repo_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ManifestBuildError(f"repository root does not exist: {repo_root}") from exc
    if not root.is_dir():
        raise ManifestBuildError(f"repository root is not a directory: {root}")

    logical = PurePosixPath(relative_name)
    if logical.is_absolute() or any(part in ("", ".", "..") for part in logical.parts):
        raise ManifestBuildError(f"invalid repository-relative path: {relative_name}")
    candidate = root.joinpath(*logical.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ManifestBuildError(
            f"required manifest source is missing or outside the repository: {relative_name}"
        ) from exc
    if not resolved.is_file() or candidate.is_symlink():
        raise ManifestBuildError(f"manifest source is not a safe regular file: {relative_name}")
    return resolved


def _load_source_module(
    source: Path,
    logical_name: str,
    *,
    allowed_imports: Iterable[str] = (),
) -> types.ModuleType:
    """Evaluate a source module while rejecting undeclared imports.

    The five plugin modules and ``messages.py`` are declarative and need no
    imports. ``captain.plugin`` is allowed its single standard-library ``os``
    import so this uses the real registry implementation as the semantic oracle.
    """

    allowed_roots = frozenset(allowed_imports)
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".", 1)[0]
        if level or root not in allowed_roots:
            raise ManifestBuildError(
                f"forbidden import {name!r} while loading manifest source "
                f"{source.name}"
            )
        return real_import(name, globals, locals, fromlist, level)

    module_builtins = dict(vars(builtins))
    module_builtins["__import__"] = guarded_import
    module = types.ModuleType(logical_name)
    module.__dict__.update(
        {
            "__builtins__": module_builtins,
            "__file__": str(source),
            "__package__": logical_name.rpartition(".")[0],
        }
    )
    try:
        source_text = source.read_text(encoding="utf-8")
        code = compile(source_text, str(source), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except ManifestBuildError:
        raise
    except Exception as exc:
        raise ManifestBuildError(f"cannot load manifest source {source}: {exc}") from exc
    return module


def load_manifest_values(repo_root: Path) -> tuple[dict, dict]:
    """Return core schemas and the real registry manifest for shipped plugins."""

    messages = _load_source_module(
        _repo_file(repo_root, MESSAGES_SOURCE), "captain.messages"
    )
    core_messages = getattr(messages, "CORE_MESSAGE_TYPES", None)
    if not isinstance(core_messages, dict):
        raise ManifestBuildError("captain.messages.CORE_MESSAGE_TYPES must be a dict")

    registry_module = _load_source_module(
        _repo_file(repo_root, REGISTRY_SOURCE),
        "captain.plugin",
        allowed_imports=("os",),
    )
    registry_class = getattr(registry_module, "PluginRegistry", None)
    if not isinstance(registry_class, type):
        raise ManifestBuildError("captain.plugin.PluginRegistry is missing")
    registry = registry_class()

    source_names: list[str] = []
    for relative_name in PLUGIN_SOURCES:
        source = _repo_file(repo_root, relative_name)
        logical_name = "plugins." + source.stem
        module = _load_source_module(source, logical_name)
        plugin_name = getattr(module, "NAME", None)
        if not isinstance(plugin_name, str) or not plugin_name:
            raise ManifestBuildError(f"{relative_name} has no non-empty string NAME")
        if plugin_name in source_names:
            raise ManifestBuildError(f"duplicate shipped plugin NAME: {plugin_name}")
        source_names.append(plugin_name)
        if not registry.register(module):
            raise ManifestBuildError(f"PluginRegistry rejected shipped plugin: {relative_name}")

    plugins = registry.manifest()
    if not isinstance(plugins, dict):
        raise ManifestBuildError("PluginRegistry.manifest() must return a dict")
    if len(plugins) != EXPECTED_PLUGIN_COUNT or set(plugins) != set(source_names):
        raise ManifestBuildError(
            "PluginRegistry manifest does not contain exactly the five shipped plugins"
        )
    return core_messages, plugins


def _canonical_json(value) -> bytes:
    """Encode compact, stable, ASCII-only JSON and reject non-JSON numbers."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ManifestBuildError(f"manifest value is not JSON serializable: {exc}") from exc
    return encoded.encode("ascii")


def _json_semantics(value):
    """Normalize tuples and other JSON-compatible containers as they appear on wire."""

    return json.loads(_canonical_json(value))


def validate_tail(tail: bytes, core_messages: dict, plugins: dict) -> dict:
    """Assemble a dynamic-id response and compare it to registry semantics."""

    if not tail.startswith(b',"core_messages":'):
        raise ManifestBuildError("manifest tail has the wrong prefix")
    if not tail.endswith(b"}}\n"):
        raise ManifestBuildError("manifest tail has the wrong terminator")
    if tail.count(b"\n") != 1:
        raise ManifestBuildError("manifest tail must contain exactly one final newline")
    try:
        tail.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ManifestBuildError("manifest tail must be ASCII JSON") from exc

    prefix = b'{"type":"MANIFEST","id":' + _canonical_json(VALIDATION_ID)
    try:
        decoded = json.loads(prefix + tail)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestBuildError(f"assembled manifest is invalid JSON: {exc}") from exc

    expected = {
        "type": "MANIFEST",
        "id": VALIDATION_ID,
        "core_messages": _json_semantics(core_messages),
        "plugins": _json_semantics(plugins),
    }
    if decoded != expected:
        raise ManifestBuildError(
            "generated tail is not semantically equal to PluginRegistry manifest shape"
        )
    return decoded


def build_manifest_tail(repo_root: Path) -> bytes:
    """Derive and fully validate the canonical wire tail."""

    core_messages, plugins = load_manifest_values(repo_root)
    tail = (
        b',"core_messages":'
        + _canonical_json(core_messages)
        + b',"plugins":'
        + _canonical_json(plugins)
        + b"}\n"
    )
    validate_tail(tail, core_messages, plugins)
    return tail


def output_path(repo_root: Path) -> Path:
    """Return the canonical artifact path, requiring its parent to exist."""

    root = repo_root.expanduser().resolve(strict=True)
    destination = root.joinpath(*PurePosixPath(OUTPUT_NAME).parts)
    parent = destination.parent.resolve(strict=True)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ManifestBuildError("manifest output directory escapes repository") from exc
    if not parent.is_dir() or destination.is_symlink():
        raise ManifestBuildError("manifest output path is unsafe")
    return destination


def check_artifact(expected: bytes, destination: Path) -> None:
    """Fail on a missing or byte-different artifact without changing it."""

    try:
        actual = destination.read_bytes()
    except FileNotFoundError as exc:
        raise ManifestBuildError(f"manifest tail artifact is missing: {destination}") from exc
    except OSError as exc:
        raise ManifestBuildError(f"cannot read manifest tail artifact: {exc}") from exc
    if actual != expected:
        raise ManifestBuildError(
            "manifest tail artifact is stale: "
            f"expected {hashlib.sha256(expected).hexdigest()}, "
            f"found {hashlib.sha256(actual).hexdigest()}"
        )


def write_artifact(data: bytes, destination: Path) -> bool:
    """Atomically publish a changed artifact; leave an identical file untouched."""

    try:
        if destination.is_file() and destination.read_bytes() == data:
            return False
    except OSError as exc:
        raise ManifestBuildError(f"cannot inspect manifest tail artifact: {exc}") from exc

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root (default: parent of tools/)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="atomically update the artifact")
    mode.add_argument("--check", action="store_true", help="verify only; write nothing")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tail = build_manifest_tail(args.repo_root)
        destination = output_path(args.repo_root)
        if args.check:
            check_artifact(tail, destination)
            state = "current"
        else:
            state = "updated" if write_artifact(tail, destination) else "already current"
    except (ManifestBuildError, OSError) as exc:
        print(f"manifest tail build failed: {exc}", file=sys.stderr)
        return 2

    digest = hashlib.sha256(tail).hexdigest()
    print(f"[ok  ] {OUTPUT_NAME} {state}: {len(tail)} bytes")
    print(f"MANIFEST_TAIL_SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
