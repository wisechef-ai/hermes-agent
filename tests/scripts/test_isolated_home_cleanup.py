"""A disposable test HOME must not outlive the run that filled it.

``HOME=$(mktemp -d) env -u HERMES_HOME bash scripts/run_tests.sh …`` is how a test
gets reproduced against a clean account. The first run under such a home has no
current activation, so the runner provisions the checkout's test environment
(~3.6 GB: uv-managed CPython plus the pm runtime generations) into
``$HOME/.hermes/installs`` — and nothing ever removed it. The ``mktemp -d``
belongs to the caller's shell, and an EXIT trap cannot help from inside the
repo: every ``exec`` in the chain (run_tests.sh → run-in-hermes-env →
``env -i`` pytest) replaces the process image and wipes the shell's traps. A
development loop iterating that one-liner leaked 15 GB in 20 minutes and filled
a CI host's disk.

``scripts/_isolated_home.sh`` owns that lifecycle and these are its behaviour
contracts. The payload comes from a stub interpreter rather than a real
``setup-hermes.sh`` run: the contract under test is who deletes the home, not
what the installer writes, and a real provisioning step would download
gigabytes into a suite that must not reach the network. Each run gets a private
``TMPDIR`` (``tmp_path``), so the assertions count only this test's directories
and no other run on the same host can satisfy or break them.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from tests.waiting import wait_until


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_TESTS = REPO_ROOT / "scripts" / "run_tests.sh"
RUN_IN_ISOLATED_HOME = REPO_ROOT / "scripts" / "run-in-isolated-home.sh"

# The shape of the leak, relative to a temp root: <home>/.hermes/installs.
INSTALLS_GLOB = "*/.hermes/installs"

# Only these are inherited from the invoking environment. Everything else is
# built fresh: HERMES_HOME would point the home's Hermes state back at the
# host, HERMES_RUNTIME_DIR / UV_PYTHON_INSTALL_DIR would provision into the
# account's shared stores instead of the home under test, and the runner's own
# re-exec markers would tell it an outer supervisor already decided this.
_INHERIT = ("PATH", "LANG", "USER", "LOGNAME", "SHELL")

_STUB_INTERPRETER = """#!/usr/bin/env bash
# Stands in for the checkout's test interpreter so a run reaches the point where
# a real setup would have filled the home, without downloading it: answers the
# runner's `import pytest` probe, no-ops the bytecode pre-compile, and writes
# the payload shape a provisioned environment leaves in the home.
if [ "$#" -gt 0 ] && [ "$1" = "-c" ]; then
  exit 0
fi
for arg in "$@"; do
  if [ "$arg" = "compileall" ]; then
    exit 0
  fi
done
if [ "__WRITES__" = "1" ]; then
  mkdir -p "$HOME/.hermes/installs/uv-python-stub"
  head -c 1048576 /dev/zero > "$HOME/.hermes/installs/uv-python-stub/payload"
fi
if [ "__HOMEFILE__" = "1" ]; then
  mkdir -p "$HOME/.config/stub"
  head -c 4096 /dev/zero > "$HOME/.config/stub/leftover"
fi
echo "stub test interpreter ran"
exit 0
"""


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env=env,
        timeout=180,
    )


def _base_env(temp_root: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _INHERIT if key in os.environ}
    env["TMPDIR"] = str(temp_root)
    return env


def _mkdtemp_in(temp_root: Path, template: str = "tmp.XXXXXXXXXX") -> Path:
    """A directory in exactly the shape ``mktemp -d`` makes, made by ``mktemp -d``."""
    result = _run(["mktemp", "-d", str(temp_root / template)], _base_env(temp_root))
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip())


def _installs_dirs(temp_root: Path) -> list[Path]:
    return sorted(temp_root.glob(INSTALLS_GLOB))


def _runner_env(
    temp_root: Path, home: Path, stub: Path, **overrides: str
) -> dict[str, str]:
    """The reported recipe's environment: a faked HOME, no HERMES_HOME, stub interpreter.

    HERMES_RUNTIME_DIR / UV_PYTHON_INSTALL_DIR are pointed inside the private
    temp root so a run that reaches setup can never touch the account's stores.
    """
    env = _base_env(temp_root)
    env.update(
        {
            "HOME": str(home),
            "HERMES_PYTHON": str(stub),
            "HERMES_RUNTIME_DIR": str(temp_root / "tool-store"),
            "UV_PYTHON_INSTALL_DIR": str(temp_root / "uv-python"),
        }
    )
    env.update(overrides)
    return env


def _write_stub(
    temp_root: Path, name: str, *, writes_installs: bool, writes_home_file: bool = False
) -> Path:
    """A stub interpreter, with the payload decision baked into the file.

    Not an environment variable: the runner re-launches its children under
    ``env -i``, so a knob the test set would not survive to the interpreter
    that actually writes the home — the quiet stub would write anyway.
    """
    path = temp_root / name
    path.write_text(
        _STUB_INTERPRETER.replace(
            "__WRITES__", "1" if writes_installs else "0"
        ).replace("__HOMEFILE__", "1" if writes_home_file else "0"),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def stub(tmp_path) -> Path:
    return _write_stub(tmp_path, "stub-interpreter", writes_installs=True)


@pytest.fixture
def quiet_stub(tmp_path) -> Path:
    """A run that provisions nothing and leaves the home exactly as found: empty."""
    return _write_stub(tmp_path, "quiet-stub", writes_installs=False)


@pytest.fixture
def unaccounted_stub(tmp_path) -> Path:
    """A run that provisions nothing yet leaves content behind in the home."""
    return _write_stub(
        tmp_path, "unaccounted-stub", writes_installs=False, writes_home_file=True
    )


# ── The leak ─────────────────────────────────────────────────────────────────


@pytest.mark.platforms("posix")
def test_repeated_clean_home_runs_leave_no_installs_dir(tmp_path, stub):
    """Three iterations of the reported one-liner leave nothing behind.

    This is the regression itself: on a host without cleanup each pass adds a
    ``<home>/.hermes/installs`` tree that outlives the process.
    """
    homes = [_mkdtemp_in(tmp_path) for _ in range(3)]
    for home in homes:
        result = _run(
            ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py", "-v"],
            _runner_env(tmp_path, home, stub),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "stub test interpreter ran" in result.stdout

    assert _installs_dirs(tmp_path) == []
    assert [home for home in homes if home.exists()] == []


@pytest.mark.platforms("posix")
def test_blessed_wrapper_removes_the_home_it_created(tmp_path, stub):
    """``run-in-isolated-home.sh`` owns the home it mktemps, so callers need not."""
    result = _run(
        [str(RUN_IN_ISOLATED_HOME), str(stub), "run-tests"], _base_env(tmp_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "stub test interpreter ran" in result.stdout
    assert _installs_dirs(tmp_path) == []
    assert list(tmp_path.glob("hermes-isolated-home-*")) == []


# ── Every exit path cleans ───────────────────────────────────────────────────


@pytest.mark.platforms("posix")
def test_failing_command_propagates_its_status_and_still_cleans(tmp_path, stub):
    """A red run is the run most likely to be repeated, so it cleans too."""
    failing = tmp_path / "failing-command"
    failing.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$HOME/.hermes/installs"\nexit 3\n',
        encoding="utf-8",
    )
    failing.chmod(0o755)
    result = _run([str(RUN_IN_ISOLATED_HOME), str(failing)], _base_env(tmp_path))
    assert result.returncode == 3
    assert _installs_dirs(tmp_path) == []


@pytest.mark.platforms("posix")
def test_terminated_run_cleans(tmp_path, stub):
    """SIGTERM (a dropped SSH session, a Ctrl-C) must not orphan a provisioned home."""
    process = subprocess.Popen(
        [
            str(RUN_IN_ISOLATED_HOME),
            "/usr/bin/env",
            "bash",
            "-c",
            'mkdir -p "$HOME/.hermes/installs"; exec sleep 60',
        ],
        cwd=REPO_ROOT,
        env=_base_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_until(
            lambda: _installs_dirs(tmp_path),
            what="the supervised command to provision the home",
        )
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    assert process.returncode in (143, 128 + signal.SIGTERM)
    assert _installs_dirs(tmp_path) == []


# ── What is never deleted ────────────────────────────────────────────────────


@pytest.mark.platforms("posix")
def test_claimed_home_the_run_left_empty_leaves_no_residue(tmp_path, quiet_stub):
    """Even a run that never provisions leaves no tombstone directory behind.

    The home was proven empty at claim and is still empty and the same directory at
    sweep, so removing it cannot lose anything. This is the shape the blessed wrapper
    ends on whenever the tests it was handed needed no test environment.
    """
    home = _mkdtemp_in(tmp_path)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, quiet_stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not home.exists()


def test_claimed_home_holding_unaccounted_content_is_kept(tmp_path, unaccounted_stub):
    """Content carrying no provisioning signature is not ours to judge, so it stays."""
    home = _mkdtemp_in(tmp_path)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, unaccounted_stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert home.is_dir()
    assert (home / ".config" / "stub" / "leftover").is_file()


@pytest.mark.platforms("posix")
def test_pre_populated_home_is_never_claimed(tmp_path, stub):
    """A home someone else put content in belongs to them, not to the runner."""
    home = _mkdtemp_in(tmp_path)
    (home / "notes.md").write_text("keep me\n", encoding="utf-8")
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "notes.md").read_text(encoding="utf-8") == "keep me\n"
    assert home.is_dir()


@pytest.mark.platforms("posix")
def test_keep_retains_the_home(tmp_path, stub):
    """Debugging a failed isolated run means being able to look inside it."""
    home = _mkdtemp_in(tmp_path)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, stub, HERMES_TEST_ISOLATED_HOME_KEEP="1"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installs_dirs(tmp_path), "the stub should have provisioned the home"
    assert home.is_dir()


@pytest.mark.platforms("posix")
def test_home_not_in_the_mktemp_shape_is_kept(tmp_path, stub):
    """Only a directory nobody else can name is disposable; a hand-made name is not."""
    home = tmp_path / "hermes-dev-home"
    home.mkdir()
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installs_dirs(tmp_path) == [home / ".hermes" / "installs"]
    assert home.is_dir()


@pytest.mark.platforms("posix")
def test_home_the_environment_already_calls_home_is_kept(tmp_path, stub):
    """A harness that exports HOME for a session must not lose it to one test run.

    The shape check cannot tell a throwaway home from a session home a harness
    exported; the rest of the environment can, and the doubt resolves to keeping.
    """
    home = _mkdtemp_in(tmp_path)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, stub, XDG_CACHE_HOME=f"{home}/.cache"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert home.is_dir()


@pytest.mark.platforms("posix")
def test_home_named_by_a_symlink_is_kept(tmp_path, stub):
    """A link is not the directory it points at: the target must survive."""
    target = tmp_path / "real-store"
    target.mkdir()
    (target / ".hermes").mkdir()
    (target / ".hermes" / "installs").mkdir()
    link = tmp_path / "tmp.LINKLINKLINK"
    link.symlink_to(target)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, link, stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.is_dir()
    assert (target / ".hermes" / "installs").is_dir()


@pytest.mark.platforms("posix")
def test_mounted_home_is_kept(tmp_path, stub):
    """Removing a mount point would delete a file system, not a test home."""
    home = _mkdtemp_in(tmp_path)
    table = tmp_path / "mounts"
    table.write_text(
        f"tmpfs {home} tmpfs rw,nosuid,nodev 0 0\n/dev/sda1 / ext4 rw,relatime 0 0\n",
        encoding="utf-8",
    )
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, stub, HERMES_ISOLATED_HOME_MOUNTS_TABLE=str(table)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert home.is_dir()
    assert _installs_dirs(tmp_path) == [home / ".hermes" / "installs"]


@pytest.mark.platforms(
    "linux"
)  # the cwd sweep reads /proc, which macOS has no counterpart for
def test_home_a_live_process_is_still_using_is_kept(tmp_path, stub):
    """A daemon the run left running in its own home is not ours to yank out."""
    home = _mkdtemp_in(tmp_path)
    holder = subprocess.Popen(["sleep", "60"], cwd=home)
    try:
        wait_until(
            lambda: Path(f"/proc/{holder.pid}/cwd").exists(),
            what="the holder process to enter the home",
        )
        result = _run(
            ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
            _runner_env(tmp_path, home, stub),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert home.is_dir()
    finally:
        holder.kill()
        holder.wait(timeout=30)


# ── The wrapper's own surface ────────────────────────────────────────────────


@pytest.mark.platforms("posix")
def test_wrapper_without_a_command_is_a_usage_error(tmp_path, stub):
    result = _run([str(RUN_IN_ISOLATED_HOME)], _base_env(tmp_path))
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()
    assert list(tmp_path.glob("hermes-isolated-home-*")) == []
