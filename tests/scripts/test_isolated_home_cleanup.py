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
if [ "__SCAFFOLD__" = "1" ]; then
  # What a run leaves when the checkout's dependency state is SHARED rather than
  # provisioned into the home: no .hermes/installs at all, but uv opened its
  # cache and Hermes opened its log dir. Measured, not imagined — this is the
  # byte-for-byte shape of the 92 KB home a HERMES_INSTALLS_ROOT iteration left
  # on wisechef-agents, which the signature check alone kept forever.
  mkdir -p "$HOME/.cache/uv/sdists-v10" "$HOME/.cache/uv/interpreter-v4" \
           "$HOME/.hermes/cache/uv" "$HOME/.hermes/logs/update_receipts"
  : > "$HOME/.cache/uv/.gitignore"
  : > "$HOME/.cache/uv/.lock"
  : > "$HOME/.cache/uv/CACHEDIR.TAG"
fi
if [ "__HERMESSTRAY__" = "1" ]; then
  mkdir -p "$HOME/.hermes"
  printf 'not written by the runner\n' > "$HOME/.hermes/state.json"
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
    temp_root: Path,
    name: str,
    *,
    writes_installs: bool,
    writes_home_file: bool = False,
    writes_scaffolding: bool = False,
    writes_hermes_stray: bool = False,
) -> Path:
    """A stub interpreter, with the payload decision baked into the file.

    Not an environment variable: the runner re-launches its children under
    ``env -i``, so a knob the test set would not survive to the interpreter
    that actually writes the home — the quiet stub would write anyway.
    """
    path = temp_root / name
    path.write_text(
        _STUB_INTERPRETER.replace("__WRITES__", "1" if writes_installs else "0")
        .replace("__HOMEFILE__", "1" if writes_home_file else "0")
        .replace("__SCAFFOLD__", "1" if writes_scaffolding else "0")
        .replace("__HERMESSTRAY__", "1" if writes_hermes_stray else "0"),
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


@pytest.fixture
def scaffold_stub(tmp_path) -> Path:
    """A run whose checkout shared the account's dependency state.

    No test environment is ever written into the home — that is the point of
    sharing — but uv and Hermes still leave their scaffolding, so the home is
    neither signed nor empty. This is the shape the dev loop on wisechef-agents
    runs in every iteration.
    """
    return _write_stub(
        tmp_path, "scaffold-stub", writes_installs=False, writes_scaffolding=True
    )


@pytest.fixture
def scaffolded_and_stray_stub(tmp_path) -> Path:
    """The sharing shape plus one file the runner's tooling does not write."""
    return _write_stub(
        tmp_path,
        "scaffolded-stray-stub",
        writes_installs=False,
        writes_scaffolding=True,
        writes_hermes_stray=True,
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


@pytest.mark.platforms("posix")
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
def test_claimed_home_holding_only_runner_scaffolding_is_removed(
    tmp_path, scaffold_stub
):
    """A sharing run's home is removed even though it was never provisioned.

    The leak signature is the proof that a run filled a home, and a run that
    shared the account's dependency state never writes it — so on a box where
    sharing is the chosen state, the signature alone keeps *every* home the loop
    creates, at 92 KB each, plus one provenance record per iteration that nothing
    ever collects (the host sweep matches ``/tmp/tmp.*``, not these dot-files).
    The claim already proved the home empty before the run started and the sweep
    re-proves it is the same directory, so scaffolding the runner's own tooling
    wrote is accountable without the signature.
    """
    home = _mkdtemp_in(tmp_path)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, scaffold_stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isolated home: claimed" in result.stderr
    assert "isolated home: removed" in result.stderr, result.stderr
    assert not home.exists()
    assert _installs_dirs(tmp_path) == []
    assert list(tmp_path.glob(".hermes-isolated-home-v1.*")) == []


@pytest.mark.platforms("posix")
def test_runner_scaffolding_beside_a_stray_file_is_kept(
    tmp_path, scaffolded_and_stray_stub
):
    """Accounting for the tooling's own trees does not widen the delete to the home.

    The accountable set names ``.hermes/cache`` and ``.hermes/logs``, not ``.hermes``
    itself, so one file the runner did not write — here under ``.hermes`` — still
    refuses the delete and stays on disk with its scaffolding beside it.
    """
    home = _mkdtemp_in(tmp_path)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, scaffolded_and_stray_stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert home.is_dir()
    assert (home / ".hermes" / "state.json").read_text(encoding="utf-8") == (
        "not written by the runner\n"
    )
    assert (home / ".cache" / "uv" / "CACHEDIR.TAG").is_file()


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
    # 0700, not the host umask's default (002 hosts mkdir 0775): a
    # group-writable mode would make the permission check the one refusing the
    # claim, and this test would stop pinning the name-shape check it names.
    home.chmod(0o700)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(tmp_path, home, stub),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installs_dirs(tmp_path) == [home / ".hermes" / "installs"]
    assert home.is_dir()


@pytest.mark.platforms("posix")
def test_group_writable_home_is_kept(tmp_path, stub):
    """A home other writers can reach is not the run's private throwaway.

    mktemp's own mode is 0700; a home widened past that is one someone set up
    to share. The leaf is mktemp-shaped, so the name check passes and the
    permission check is the only thing refusing this claim — proven red on its
    own, not behind the name check's shadow.
    """
    home = _mkdtemp_in(tmp_path)
    home.chmod(0o775)
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
def test_home_named_by_the_runtime_store_is_kept(tmp_path, stub):
    """A session that named this home its tool store must not lose it to a run.

    ``HERMES_RUNTIME_DIR`` is PM's inbound override for the tool store, so an
    exported value naming the home says a session chose that location on purpose.
    Deleting it is precisely the "a new store per temporary home" failure
    ``pm/_unstamped_store`` warns about, so the doubt resolves toward keeping; the
    veto costs a directory the host sweep collects, where deleting it costs the
    session its store. The installs assertion is what proves the run really
    provisioned here, so the home surviving cannot be green by doing nothing.
    """
    home = _mkdtemp_in(tmp_path)
    result = _run(
        ["bash", str(RUN_TESTS), "tests/scripts/test_bash_shebangs.py"],
        _runner_env(
            tmp_path, home, stub, HERMES_RUNTIME_DIR=f"{home}/.hermes/runtime"
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installs_dirs(tmp_path) == [home / ".hermes" / "installs"]
    assert home.is_dir()
    assert (home / ".hermes" / "installs").is_dir()


@pytest.mark.platforms("posix")
def test_record_is_retired_when_the_command_removes_its_own_home(tmp_path):
    """A run that cleaned up after itself must not leave its provenance record.

    The record sits beside the home, and the sweep only removed it beside a home
    the sweep itself deleted. A supervised command that removed its own home
    therefore stranded the record in the temp root forever: nothing collects these
    dot-files (the host sweep matches ``/tmp/tmp.*`` environments, not them), and
    a survivor also refused the claim of any later home that reused the leaf.
    """
    self_cleaner = tmp_path / "self-cleaner"
    self_cleaner.write_text(
        '#!/usr/bin/env bash\n'
        'mkdir -p "$HOME/.hermes/installs"\n'
        'rm -rf "$HOME"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    self_cleaner.chmod(0o755)
    result = _run([str(RUN_IN_ISOLATED_HOME), str(self_cleaner)], _base_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(tmp_path.glob("hermes-isolated-home-*")) == []
    assert list(tmp_path.glob(".hermes-isolated-home-v1.*")) == []


@pytest.mark.platforms("posix")
def test_record_rewritten_to_another_owner_refuses_the_delete(tmp_path):
    """The provenance record's owner is load-bearing, not decoration.

    The record is what authorizes the delete, so its pid and uid are compared
    with this process before anything is removed — the same comparison that makes
    retiring a record for an already-gone home safe. Without it, planting a
    record beside a directory would be enough to license deleting it. The
    supervised command rewrites its own record rather than the test doing it
    afterwards: the sweep runs once the command has already exited, so a
    parent-side edit would land either before the claim or after the decision.
    """
    tamperer = tmp_path / "tamperer"
    tamperer.write_text(
        "#!/usr/bin/env bash\n"
        'mkdir -p "$HOME/.hermes/installs"\n'
        'record="$TMPDIR/.hermes-isolated-home-v1.${HOME##*/}"\n'
        'test -f "$record"\n'
        "sed -i 's/^pid=.*/pid=999999/' \"$record\"\n"
        "sed -i 's/^uid=.*/uid=0/' \"$record\"\n"
        'grep -q "^pid=999999$" "$record"\n'
        'grep -q "^uid=0$" "$record"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    tamperer.chmod(0o755)
    result = _run([str(RUN_IN_ISOLATED_HOME), str(tamperer)], _base_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    homes = list(tmp_path.glob("hermes-isolated-home-*"))
    assert len(homes) == 1, f"the home was deleted despite a foreign record: {homes}"
    assert (homes[0] / ".hermes" / "installs").is_dir()
    assert list(tmp_path.glob(".hermes-isolated-home-v1.*")), (
        "a refused delete must keep its record: someone else may hold the claim"
    )


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
