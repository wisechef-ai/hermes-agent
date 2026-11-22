"""``scripts/run_tests.sh``: a faked-HOME run shares the account's dependency state.

The helpers live in the runner itself (upstream keeps the faked-HOME store logic there), so
this sources just those functions out of the script and drives them in bash -- the same
shape as the ``_account_tool_store`` contract they sit beside: share when the account
already holds committed state for THIS checkout, fall back to today's per-home layout for
anything else, and hand over nothing but the one variable.

The account home is shimmed with a ``getent`` on ``PATH``; ``_account_home`` reads it
through ``getent passwd $(id -u)`` (``dscl`` on macOS), so the fake needs no root.

Cases assert on what the helpers *do* -- exit status, what they print, what they export --
never on the script's text (tests/AGENTS.md: "Never read source code in tests").
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Documented hook so the same contract can certify a checkout that already carries the
# change (a hot-patched dev tree) without copying this file into it. Unset = this checkout.
RUN_TESTS = Path(os.environ.get("HERMES_RUN_TESTS_SH") or (REPO_ROOT / "scripts" / "run_tests.sh"))

# Every lane that has bash and getent/dscl; Windows runs the .ps1 lanes instead.
pytestmark = pytest.mark.platforms("posix")

# The helpers this contract owns, plus the ones they call. Required helpers are asserted to
# exist (a rename must fail loudly); optional ones are extracted when the checkout has them,
# because a lane may resolve paths with a different helper (`cd -P`, say).
_REQUIRED = ("_install_key", "_account_installs_state", "_choose_installs_root")
_OPTIONAL = ("_account_home", "_resolved_path", "_account_stores", "_which_store",
             "_account_tool_store", "_store_has_pins")


def _source_helpers(required: tuple[str, ...] = _REQUIRED,
                    optional: tuple[str, ...] = _OPTIONAL) -> str:
    """The named top-level functions of run_tests.sh, verbatim.

    Sourcing the whole script would run it, and its provisioning branch ``exec``s away.
    Extracting by name keeps the test honest about what it runs: a renamed or deleted
    helper fails here rather than silently testing nothing.
    """
    lines = RUN_TESTS.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    for name in (*required, *optional):
        header = f"{name}() {{"
        matches = [index for index, line in enumerate(lines) if line.startswith(header)]
        if not matches:
            if name in required:
                pytest.fail(f"{name}() is gone from {RUN_TESTS}")
            continue
        for index in range(matches[0], len(lines)):
            body.append(lines[index])
            if lines[index] == "}":
                break
        else:
            pytest.fail(f"{name}() has no closing brace at column 0")
    return "\n".join(body)


class _Shell:
    """Drives bash with the runner's helpers loaded, over a fake account + faked HOME."""

    def __init__(self, shim: Path, account: Path, fake: Path, checkout: Path) -> None:
        self.shim, self.account, self.fake, self.checkout = shim, account, fake, checkout

    def run(self, body: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        base = {
            "PATH": f"{self.shim}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.fake),
            "REPO_ROOT": str(self.checkout),
            "BASH": "/bin/bash",
        }
        base.update(env or {})
        script = "set -u\n" + _source_helpers() + "\n" + textwrap.dedent(body)
        return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script],
                              capture_output=True, text=True, env=base, check=False)


@pytest.fixture
def shell(tmp_path):
    """``shell.account`` is what ``getent`` reports as the OS account's home;
    ``shell.fake`` is the ``mktemp -d`` home the dev session runs under."""
    account = tmp_path / "account"
    fake = tmp_path / "tmp.AAAAAAAAAA"
    fake.mkdir()
    checkout = account / ".hermes" / "hermes-agent"
    checkout.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")

    shim = tmp_path / "bin"
    shim.mkdir()
    getent = shim / "getent"
    getent.write_text(f'#!/bin/sh\nprintf \'fake:x:1000:1000:fake:{account}:/bin\\n\'\n', encoding="utf-8")
    getent.chmod(0o755)

    return _Shell(shim, account, fake, checkout)


def _committed(checkout: Path, account: Path) -> Path:
    """Leave the record a committed sync leaves in the ACCOUNT root, at this checkout's key."""
    state = account / ".hermes" / "installs" / hashlib.sha256(
        str(checkout).encode("utf-8")).hexdigest()[:16]
    state.mkdir(parents=True, exist_ok=True)
    (state / "facts.json").write_text('{"packages": {}}', encoding="utf-8")
    return state


def _last(result: subprocess.CompletedProcess) -> str:
    return result.stdout.splitlines()[-1] if result.stdout else ""


def test_the_runner_scripts_parse():
    result = subprocess.run(["/bin/bash", "-n", str(RUN_TESTS)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_install_key_matches_pms_own_hash(shell):
    """The bash hash must be the value pm computes, or sharing would name the wrong dir."""
    from pm.environments import install_key

    result = shell.run('printf \'%s\\n\' "$(_install_key)"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == install_key(shell.checkout)
    assert result.stdout.strip() == hashlib.sha256(
        str(shell.checkout).encode("utf-8")).hexdigest()[:16]


def test_a_key_that_cannot_be_computed_shares_nothing(shell):
    """No sha256 tool means no sharing -- never a guessed key over two checkouts' state.

    Empty PATH: `command -v` finds none of sha256sum / shasum / openssl, so the key is
    unverifiable and the run must keep its own layout rather than aim at someone else's.
    """
    result = shell.run('_choose_installs_root\nprintf \'%s\\n\' "${HERMES_INSTALLS_ROOT:-<unset>}"',
                       env={"PATH": "/nonexistent-dir-for-this-test"})
    assert result.returncode == 0, result.stderr
    assert _last(result) == "<unset>"


def test_shares_when_the_account_holds_committed_state(shell):
    _committed(shell.checkout, shell.account)
    result = shell.run('_choose_installs_root\nprintf \'%s\\n\' "${HERMES_INSTALLS_ROOT:-<unset>}"')
    assert result.returncode == 0, result.stderr
    assert _last(result) == str(shell.account / ".hermes" / "installs")
    assert "sharing the account's dependency state" in result.stdout


def test_it_exports_the_shared_root_and_nothing_else(shell):
    """One variable, no side effects on the neighbouring store knobs.

    The tool store and the bootstrap interpreter cache are chosen by
    ``_choose_tool_store``; an adoption that also moved those would silently re-point the
    run's tools, which is a different decision with a different guard.
    """
    _committed(shell.checkout, shell.account)
    before = shell.run("env | sort")
    after = shell.run("_choose_installs_root >/dev/null\nenv | sort")
    added = set(after.stdout.splitlines()) - set(before.stdout.splitlines())
    assert added == {f"HERMES_INSTALLS_ROOT={shell.account / '.hermes' / 'installs'}"}, added
    assert not any(key.startswith(("HERMES_RUNTIME_DIR", "UV_", "HERMES_HOME"))
                   for key in added)


def test_an_explicit_root_of_this_runs_own_wins(shell):
    """An operator or lane that already named a root is never overridden."""
    _committed(shell.checkout, shell.account)
    own = shell.fake / ".hermes" / "installs"
    result = shell.run('_choose_installs_root\nprintf \'%s\\n\' "$HERMES_INSTALLS_ROOT"',
                       env={"HERMES_INSTALLS_ROOT": str(own)})
    assert result.returncode == 0, result.stderr
    assert _last(result) == str(own)
    assert "sharing the account's dependency state" not in result.stdout


def test_nothing_is_shared_when_the_account_has_no_state_for_this_checkout(shell):
    result = shell.run('_choose_installs_root\nprintf \'%s\\n\' "${HERMES_INSTALLS_ROOT:-<unset>}"')
    assert result.returncode == 0, result.stderr
    assert _last(result) == "<unset>"


def test_nothing_is_shared_when_the_faked_home_already_holds_state(shell):
    """A home that owns its own state keeps it: sharing must not steal a run's own tree."""
    _committed(shell.checkout, shell.account)
    own = shell.fake / ".hermes" / "installs"
    key = hashlib.sha256(str(shell.checkout).encode("utf-8")).hexdigest()[:16]
    (own / key).mkdir(parents=True)
    (own / key / "facts.json").write_text('{"packages": {}}', encoding="utf-8")
    result = shell.run('_choose_installs_root\nprintf \'%s\\n\' "${HERMES_INSTALLS_ROOT:-<unset>}"')
    assert _last(result) == "<unset>"


def test_a_custom_hermes_home_shares_nothing(shell):
    """An inherited HERMES_HOME (an agent's shell exports one) is the caller's scope."""
    _committed(shell.checkout, shell.account)
    other = shell.fake / "elsewhere"
    other.mkdir()
    result = shell.run('_choose_installs_root\nprintf \'%s\\n\' "${HERMES_INSTALLS_ROOT:-<unset>}"',
                       env={"HERMES_HOME": str(other)})
    assert _last(result) == "<unset>"


def test_an_unwritable_account_root_is_left_alone(shell):
    """Read-only to us means no sharing: a half-shared run writes facts somewhere surprising."""
    _committed(shell.checkout, shell.account)
    (shell.account / ".hermes" / "installs").chmod(0o500)
    try:
        result = shell.run('_choose_installs_root\nprintf \'%s\\n\' "${HERMES_INSTALLS_ROOT:-<unset>}"')
    finally:
        (shell.account / ".hermes" / "installs").chmod(0o755)
    assert _last(result) == "<unset>"
