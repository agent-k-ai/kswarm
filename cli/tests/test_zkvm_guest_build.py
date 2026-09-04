"""One declaration of the RISC Zero toolchain, one script that compiles the guest.

The branch canonicalization guest's image id is a function of the RISC Zero
component versions, of the absolute path the guest is compiled at, and of the
`$HOME` of the user compiling it (`protocol/zkvm-reducer/IMAGE_ID.md`). While
those values lived in the Dockerfiles, two images that build the same guest
compiled it at different paths under different homes and produced different ids,
and `docker/protocol-node` could not be built at all.

These tests hold the shape that fixed it: `protocol/risc0-toolchain.env` is the
only place the values appear, and no Dockerfile installs a RISC Zero component or
compiles the guest by itself. They also exercise the guard rails of the two
scripts, which are the parts that run before anything is downloaded.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DECLARATION = REPO_ROOT / "protocol" / "risc0-toolchain.env"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-risc0-toolchain.sh"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-zkvm-guest.sh"
DOCKERFILES = sorted(REPO_ROOT.glob("docker/*/Dockerfile"))

DECLARED_KEYS = (
    "RISC0_RUST_VERSION",
    "RISC0_CPP_VERSION",
    "RISC0_R0VM_VERSION",
    "RISC0_CARGO_RISCZERO_VERSION",
    "RISC0_GUEST_BUILDER_DIGEST",
    "ZKVM_GUEST_BUILD_ROOT",
    "ZKVM_GUEST_BUILD_HOME",
)


def read_declaration() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in DECLARATION.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def test_declaration_names_every_input() -> None:
    values = read_declaration()
    assert set(DECLARED_KEYS) <= set(values), sorted(set(DECLARED_KEYS) - set(values))
    for key in ("RISC0_RUST_VERSION", "RISC0_CPP_VERSION", "RISC0_R0VM_VERSION", "RISC0_CARGO_RISCZERO_VERSION"):
        assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", values[key]), f"{key}={values[key]!r} is not an exact version"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", values["RISC0_GUEST_BUILDER_DIGEST"])
    for key in ("ZKVM_GUEST_BUILD_ROOT", "ZKVM_GUEST_BUILD_HOME"):
        assert values[key].startswith("/"), f"{key} must be an absolute path, got {values[key]!r}"


def instructions(path: Path) -> str:
    """The Dockerfile with comment lines removed, so prose about a rule is not the rule."""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )


def test_no_dockerfile_installs_a_risc0_component_itself() -> None:
    """`rzup install <component>` in a Dockerfile is how the four versions drift apart."""
    offenders = [path.relative_to(REPO_ROOT) for path in DOCKERFILES if "rzup install" in instructions(path)]
    assert offenders == [], f"{offenders} must call scripts/install-risc0-toolchain.sh instead"


def test_every_dockerfile_that_wants_rzup_uses_the_shared_installer() -> None:
    for path in DOCKERFILES:
        text = instructions(path)
        if "install-risc0-toolchain.sh" not in text:
            continue
        assert "protocol/risc0-toolchain.env" in text, (
            f"{path.relative_to(REPO_ROOT)} runs the installer without copying the declaration it reads"
        )


def test_no_dockerfile_compiles_the_guest_itself() -> None:
    """Only scripts/build-zkvm-guest.sh may compile it: it owns the path and $HOME."""
    offenders = []
    for path in DOCKERFILES:
        text = instructions(path)
        if "zkvm-reducer" in text and re.search(r"cargo build", text):
            if "build-zkvm-guest.sh" not in text:
                offenders.append(path.relative_to(REPO_ROOT))
    assert offenders == [], f"{offenders} compile the guest without scripts/build-zkvm-guest.sh"


def test_the_scripts_are_executable_and_read_the_declaration() -> None:
    for script in (INSTALL_SCRIPT, BUILD_SCRIPT):
        assert script.stat().st_mode & 0o111, f"{script} is not executable"
        text = script.read_text(encoding="utf-8")
        assert "protocol/risc0-toolchain.env" in text
        assert "RISC0_TOOLCHAIN_ENV" in text


def test_swarm_build_context_admits_the_declaration_and_the_scripts() -> None:
    """The swarm ignore file is an allowlist; a missing entry breaks the COPY."""
    allowlist = (REPO_ROOT / "docker" / "swarm" / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    for needed in (
        "!protocol/risc0-toolchain.env",
        "!scripts/install-risc0-toolchain.sh",
        "!scripts/build-zkvm-guest.sh",
    ):
        assert needed in allowlist, needed


def test_guest_crate_toolchain_is_pinned_not_floating() -> None:
    toml = (REPO_ROOT / "protocol" / "zkvm-reducer" / "rust-toolchain.toml").read_text(encoding="utf-8")
    channel = re.search(r'^channel\s*=\s*"([^"]+)"', toml, re.M)
    assert channel, "rust-toolchain.toml declares no channel"
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", channel.group(1)), (
        f'channel = "{channel.group(1)}" floats; the host binary that carries the guest '
        "would then differ between two builds of the same commit"
    )


def test_pinned_image_id_is_a_digest() -> None:
    pinned = (REPO_ROOT / "protocol" / "zkvm-reducer" / "IMAGE_ID").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{64}", pinned), pinned


def test_bonsol_builder_reads_the_guest_builder_digest_from_the_declaration() -> None:
    script = (REPO_ROOT / "protocol" / "scripts" / "run-bonsol-builder.sh").read_text(encoding="utf-8")
    assert "protocol/risc0-toolchain.env" in script
    assert "RISC0_GUEST_BUILDER_DIGEST" in script


def run_build_script(tmp_path: Path, declaration: str, *args: str) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "risc0-toolchain.env"
    env_file.write_text(declaration, encoding="utf-8")
    return subprocess.run(
        [str(BUILD_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "RISC0_TOOLCHAIN_ENV": str(env_file)},
    )


def test_build_script_requires_both_arguments(tmp_path: Path) -> None:
    result = run_build_script(tmp_path, "RISC0_RUST_VERSION=1.88.0\n")
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_build_script_rejects_a_source_tree_without_the_crates(tmp_path: Path) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    result = run_build_script(
        tmp_path,
        "RISC0_RUST_VERSION=1.88.0\nZKVM_GUEST_BUILD_ROOT=/src\nZKVM_GUEST_BUILD_HOME=/root\n",
        str(source),
        str(tmp_path / "out"),
    )
    assert result.returncode == 1
    assert "has no protocol/zkvm-reducer" in result.stderr


def test_build_script_refuses_a_home_it_cannot_own(tmp_path: Path) -> None:
    """A different $HOME is a different image id, so there is no fallback."""
    source = tmp_path / "tree"
    (source / "protocol" / "zkvm-reducer").mkdir(parents=True)
    (source / "protocol" / "bonsol-aggregate-reducer").mkdir(parents=True)
    result = run_build_script(
        tmp_path,
        "RISC0_RUST_VERSION=1.88.0\n"
        "ZKVM_GUEST_BUILD_ROOT=/src\n"
        "ZKVM_GUEST_BUILD_HOME=/proc/kswarm-guest-build-home\n",
        str(source),
        str(tmp_path / "out"),
    )
    assert result.returncode == 1
    assert "is not writable" in result.stderr
    assert "panic locations" in result.stderr


def test_build_script_reports_a_declaration_missing_a_value(tmp_path: Path) -> None:
    source = tmp_path / "tree"
    (source / "protocol" / "zkvm-reducer").mkdir(parents=True)
    result = run_build_script(tmp_path, "RISC0_RUST_VERSION=1.88.0\n", str(source), str(tmp_path / "out"))
    assert result.returncode == 1
    assert "does not declare ZKVM_GUEST_BUILD_ROOT" in result.stderr


def test_install_script_reports_a_declaration_missing_a_version(tmp_path: Path) -> None:
    env_file = tmp_path / "risc0-toolchain.env"
    env_file.write_text("RISC0_RUST_VERSION=1.88.0\n", encoding="utf-8")
    result = subprocess.run(
        [str(INSTALL_SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "RISC0_TOOLCHAIN_ENV": str(env_file)},
    )
    assert result.returncode == 1
    assert "does not declare RISC0_CPP_VERSION" in result.stderr


def test_install_script_reports_a_missing_declaration(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(INSTALL_SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "RISC0_TOOLCHAIN_ENV": str(tmp_path / "absent.env")},
    )
    assert result.returncode == 1
    assert "no declaration at" in result.stderr


@pytest.mark.parametrize("script", [INSTALL_SCRIPT, BUILD_SCRIPT])
def test_scripts_parse_under_bash(script: Path) -> None:
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
