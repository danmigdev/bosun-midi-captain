#!/usr/bin/env python3
"""Reproducibly build Bosun's precompiled CircuitPython modules.

This tool deliberately does not discover ``mpy-cross`` on PATH.  The PyPI
package with that name is MicroPython's compiler; its mpy-v6 output is not
interchangeable with CircuitPython bytecode even though the ABI number looks
the same.  Callers must provide a compiler executable, and its complete
``--version`` identity is checked before any source is compiled.

No mode talks to a Captain or deploys firmware.  ``--check`` is read-only,
``--output-root`` writes a separate staging tree, and updating the checked-out
firmware artifacts requires the explicit ``--write`` flag.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIRMWARE_ROOT = REPO_ROOT / "firmware"

CIRCUITPYTHON_VERSION = "9.2.7"
MPY_ABI = "6.3"
EXPECTED_COMPILER_BANNER = (
    "CircuitPython 9.2.7 on 2025-04-01; "
    "mpy-cross emitting mpy v6.3"
)

# Production modules are explicit rather than derived at build time, so their
# order and output set cannot vary with directory enumeration.  A separate
# inventory check below fails when a new runtime source has not been classified.
RUNTIME_SOURCE_DIRS = (
    "lib/captain",
    "lib/plugins",
)
# Lazy helpers live outside the captain package so unloading them does not
# leave a reference on that package. Do not inventory unrelated Adafruit
# modules installed alongside them in /lib.
RUNTIME_ROOT_SOURCE_GLOB = "captain_*.py"

# Keep package markers as source deliberately. captain/__init__.py is also the
# plaintext VERSION source consumed by the desktop installer and version tools;
# plugins/__init__.py contains no executable statements worth precompiling.
SOURCE_ONLY_RUNTIME = (
    "lib/captain/__init__.py",
    "lib/plugins/__init__.py",
)

# The source siblings remain in the repository for review and host-side tests;
# deployment tools prefer each compatible .mpy when both forms are present.
DEFAULT_SOURCES = (
    "lib/captain/app.py",
    "lib/captain/bindings.py",
    "lib/captain/board.py",
    "lib/captain/config.py",
    "lib/captain/display.py",
    "lib/captain/expression.py",
    "lib/captain/leds.py",
    "lib/captain/manifest_dynamic.py",
    "lib/captain/messages.py",
    "lib/captain/midi.py",
    "lib/captain/navigation.py",
    "lib/captain/plugin.py",
    "lib/captain/protocol.py",
    "lib/captain/store.py",
    "lib/captain_ota.py",
    "lib/plugins/ampero.py",
    "lib/plugins/generic_midi.py",
    "lib/plugins/headrush_core.py",
    "lib/plugins/kemper.py",
    "lib/plugins/line6_helix.py",
)

Run = Callable[..., subprocess.CompletedProcess[str]]


class BuildError(RuntimeError):
    """A safe, user-facing build failure."""


@dataclass(frozen=True)
class Target:
    source: Path
    source_name: str
    output_name: str


def discover_runtime_sources(firmware_root: Path) -> tuple[str, ...]:
    """Return Bosun package sources and its explicitly named root helpers.

    Discovery is used only as a fail-closed completeness check.  Compilation
    still follows :data:`DEFAULT_SOURCES` exactly and deterministically.
    """

    try:
        root = firmware_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BuildError(f"firmware root does not exist: {firmware_root}") from exc
    if not root.is_dir():
        raise BuildError(f"firmware root is not a directory: {root}")

    found: list[str] = []

    def include_source(candidate: Path) -> None:
        try:
            source = candidate.resolve(strict=True)
            relative = source.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise BuildError(
                f"runtime source is missing or outside firmware root: {candidate}"
            ) from exc
        if not source.is_file():
            raise BuildError(f"runtime source is not a file: {relative}")
        found.append(relative)

    for relative_dir in RUNTIME_SOURCE_DIRS:
        directory = root.joinpath(*PurePosixPath(relative_dir).parts)
        try:
            resolved_dir = directory.resolve(strict=True)
            resolved_dir.relative_to(root)
        except (OSError, ValueError) as exc:
            raise BuildError(
                f"runtime source directory is missing or outside firmware root: "
                f"{relative_dir}"
            ) from exc
        if not resolved_dir.is_dir():
            raise BuildError(f"runtime source path is not a directory: {relative_dir}")

        for candidate in resolved_dir.rglob("*.py"):
            include_source(candidate)
    for candidate in (root / "lib").glob(RUNTIME_ROOT_SOURCE_GLOB):
        include_source(candidate)
    return tuple(sorted(found))


def validate_default_source_inventory(firmware_root: Path) -> None:
    """Require an explicit decision for every production Python module."""

    if tuple(sorted(DEFAULT_SOURCES)) != DEFAULT_SOURCES:
        raise BuildError("DEFAULT_SOURCES must remain sorted deterministically")
    if tuple(sorted(SOURCE_ONLY_RUNTIME)) != SOURCE_ONLY_RUNTIME:
        raise BuildError("SOURCE_ONLY_RUNTIME must remain sorted deterministically")

    defaults = set(DEFAULT_SOURCES)
    source_only = set(SOURCE_ONLY_RUNTIME)
    duplicates = defaults & source_only
    invalid_exclusions = {
        source for source in source_only if not source.endswith("/__init__.py")
    }
    discovered = set(discover_runtime_sources(firmware_root))
    classified = defaults | source_only

    problems: list[str] = []
    if len(defaults) != len(DEFAULT_SOURCES):
        problems.append("duplicate entry in DEFAULT_SOURCES")
    if len(source_only) != len(SOURCE_ONLY_RUNTIME):
        problems.append("duplicate entry in SOURCE_ONLY_RUNTIME")
    if duplicates:
        problems.append("both compiled and source-only: " + ", ".join(sorted(duplicates)))
    if invalid_exclusions:
        problems.append(
            "only package __init__.py files may be source-only: "
            + ", ".join(sorted(invalid_exclusions))
        )
    unclassified = discovered - classified
    if unclassified:
        problems.append("unclassified runtime source: " + ", ".join(sorted(unclassified)))
    missing = classified - discovered
    if missing:
        problems.append("listed runtime source is missing: " + ", ".join(sorted(missing)))
    if problems:
        raise BuildError("runtime source inventory mismatch:\n  " + "\n  ".join(problems))


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    runner: Run | None = None,
) -> subprocess.CompletedProcess[str]:
    invoke = runner if runner is not None else subprocess.run
    try:
        result = invoke(
            list(command),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"command timed out after {timeout:g}s: {command[0]}") from exc
    except OSError as exc:
        raise BuildError(f"cannot execute compiler {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic").strip()
        raise BuildError(
            f"compiler exited with status {result.returncode}: {detail}"
        )
    return result


def verify_compiler(compiler: Path, runner: Run | None = None) -> str:
    """Require the exact CircuitPython 9.2.7 compiler identity.

    Checking only ``mpy v6.3`` is insufficient: current MicroPython compilers
    can report the same ABI while emitting files CircuitPython rejects.
    """
    compiler = compiler.expanduser().resolve()
    if not compiler.is_file():
        raise BuildError(f"compiler is not a file: {compiler}")

    result = _run_process(
        [str(compiler), "--version"],
        cwd=REPO_ROOT,
        timeout=10,
        runner=runner,
    )
    streams = [part.strip() for part in (result.stdout, result.stderr) if part.strip()]
    identity = "\n".join(streams)
    if identity == EXPECTED_COMPILER_BANNER:
        return identity

    if re.search(r"\bMicroPython\b", identity, flags=re.IGNORECASE):
        raise BuildError(
            "refusing the MicroPython mpy-cross compiler (commonly installed "
            "from PyPI); Bosun requires CircuitPython 9.2.7 mpy-cross"
        )
    raise BuildError(
        "wrong mpy-cross identity; expected exactly:\n"
        f"  {EXPECTED_COMPILER_BANNER}\n"
        f"received:\n  {identity or '<empty output>'}"
    )


def resolve_targets(firmware_root: Path, sources: Iterable[str]) -> list[Target]:
    """Resolve sources below firmware_root and assign stable POSIX names."""
    try:
        root = firmware_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BuildError(f"firmware root does not exist: {firmware_root}") from exc
    if not root.is_dir():
        raise BuildError(f"firmware root is not a directory: {root}")

    targets: list[Target] = []
    seen: set[str] = set()
    for raw in sources:
        portable = str(raw).replace("\\", "/")
        logical = PurePosixPath(portable)
        if (
            not portable
            or logical.is_absolute()
            or re.match(r"^[A-Za-z]:/", portable)
            or any(part in ("", ".", "..") for part in logical.parts)
        ):
            raise BuildError(f"source must be a normalized relative path: {raw}")
        if logical.suffix != ".py":
            raise BuildError(f"source must end in .py: {raw}")

        candidate = root.joinpath(*logical.parts)
        try:
            source = candidate.resolve(strict=True)
            relative = source.relative_to(root)
        except (OSError, ValueError) as exc:
            raise BuildError(f"source is missing or outside firmware root: {raw}") from exc
        if not source.is_file():
            raise BuildError(f"source is not a file: {raw}")

        source_name = relative.as_posix()
        if source_name in seen:
            raise BuildError(f"duplicate source: {source_name}")
        seen.add(source_name)
        targets.append(
            Target(
                source=source,
                source_name=source_name,
                output_name=relative.with_suffix(".mpy").as_posix(),
            )
        )

    if not targets:
        raise BuildError("no sources selected")
    return targets


def validate_mpy(data: bytes, source_name: str) -> None:
    """Reject empty, MicroPython, or wrong-ABI output before publication."""
    if len(data) < 4:
        raise BuildError(f"compiler produced a truncated file for {source_name}")
    if data[0] == ord("M"):
        raise BuildError(
            f"compiler produced MicroPython bytecode for {source_name}; "
            "expected CircuitPython magic 'C'"
        )
    if data[0] != ord("C") or data[1] != 6:
        raise BuildError(
            f"compiler produced an incompatible header for {source_name}: "
            f"{data[:4].hex(' ')}"
        )


def compile_target(
    compiler: Path,
    firmware_root: Path,
    target: Target,
    output: Path,
    runner: Run | None = None,
) -> bytes:
    """Compile one target with a location-independent embedded source name."""
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(compiler.expanduser().resolve()),
        "-s",
        target.source_name,
        "-o",
        str(output),
        target.source_name,
    ]
    _run_process(
        command,
        cwd=firmware_root.resolve(),
        timeout=60,
        runner=runner,
    )
    try:
        data = output.read_bytes()
    except OSError as exc:
        raise BuildError(
            f"compiler did not create output for {target.source_name}: {output}"
        ) from exc
    validate_mpy(data, target.source_name)
    return data


def build_reproducibly(
    compiler: Path,
    firmware_root: Path,
    sources: Iterable[str],
    runner: Run | None = None,
) -> dict[str, bytes]:
    """Compile every target twice and require byte-identical results."""
    compiler = compiler.expanduser().resolve()
    root = firmware_root.expanduser().resolve()
    verify_compiler(compiler, runner=runner)
    targets = resolve_targets(root, sources)

    artifacts: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="bosun-mpy-") as temp_name:
        temp = Path(temp_name)
        for target in targets:
            relative_output = Path(*PurePosixPath(target.output_name).parts)
            first = compile_target(
                compiler,
                root,
                target,
                temp / "pass-1" / relative_output,
                runner=runner,
            )
            second = compile_target(
                compiler,
                root,
                target,
                temp / "pass-2" / relative_output,
                runner=runner,
            )
            if first != second:
                raise BuildError(
                    f"non-reproducible compiler output for {target.source_name}: "
                    f"{hashlib.sha256(first).hexdigest()} != "
                    f"{hashlib.sha256(second).hexdigest()}"
                )
            artifacts[target.output_name] = first
    return artifacts


def check_artifacts(artifacts: dict[str, bytes], output_root: Path) -> None:
    """Compare generated bytes with existing files without modifying them."""
    mismatches: list[str] = []
    for output_name, expected in artifacts.items():
        destination = output_root.joinpath(*PurePosixPath(output_name).parts)
        try:
            actual = destination.read_bytes()
        except FileNotFoundError:
            mismatches.append(f"missing {output_name}")
            continue
        except OSError as exc:
            mismatches.append(f"cannot read {output_name}: {exc}")
            continue
        if actual != expected:
            mismatches.append(
                f"different {output_name}: "
                f"expected {hashlib.sha256(expected).hexdigest()}, "
                f"found {hashlib.sha256(actual).hexdigest()}"
            )
    if mismatches:
        raise BuildError("artifact check failed:\n  " + "\n  ".join(mismatches))


def _atomic_write(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_artifacts(artifacts: dict[str, bytes], output_root: Path) -> None:
    """Publish only after the complete two-pass build has succeeded."""
    for output_name, data in artifacts.items():
        destination = output_root.joinpath(*PurePosixPath(output_name).parts)
        _atomic_write(destination, data)


def describe_artifacts(artifacts: dict[str, bytes]) -> None:
    for output_name, data in artifacts.items():
        print(
            f"{output_name:<34} {len(data):>7} B  "
            f"sha256={hashlib.sha256(data).hexdigest()}"
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compiler",
        required=True,
        type=Path,
        help=(
            "explicit CircuitPython 9.2.7 mpy-cross executable; PATH lookup "
            "and the PyPI MicroPython package are intentionally unsupported"
        ),
    )
    parser.add_argument(
        "--firmware-root",
        type=Path,
        default=DEFAULT_FIRMWARE_ROOT,
        help="firmware tree (default: repository firmware/)",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        default=list(DEFAULT_SOURCES),
        metavar="RELATIVE.py",
        help="source paths relative to firmware/ (default: Bosun precompiled set)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="compile twice and compare with firmware/*.mpy; write nothing",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="explicitly replace the selected firmware/*.mpy artifacts",
    )
    mode.add_argument(
        "--output-root",
        type=Path,
        help="write a separate staging tree, preserving firmware/*.mpy",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    firmware_root = args.firmware_root.expanduser().resolve()
    try:
        if tuple(args.files) == DEFAULT_SOURCES:
            validate_default_source_inventory(firmware_root)
        artifacts = build_reproducibly(
            args.compiler,
            firmware_root,
            args.files,
        )
        describe_artifacts(artifacts)
        if args.check:
            check_artifacts(artifacts, firmware_root)
            print("all checked-in .mpy artifacts are byte-identical")
        elif args.write:
            write_artifacts(artifacts, firmware_root)
            print("updated firmware .mpy artifacts")
        else:
            output_root = args.output_root.expanduser().resolve()
            if output_root == firmware_root:
                raise BuildError(
                    "--output-root cannot be firmware/; use explicit --write"
                )
            write_artifacts(artifacts, output_root)
            print(f"wrote staging artifacts under {output_root}")
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
