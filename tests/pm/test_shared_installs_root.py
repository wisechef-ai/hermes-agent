"""A borrowing launch may be pointed at the owner's committed dependency state.

Regression for the dev-loop churn: ``HOME=$(mktemp -d) bash scripts/run_tests.sh <file>``
re-materialised the checkout's whole generation set -- every generation, the venv, the test
environment, measured 3,405,856 KB for one dev checkout of this repo -- under the throwaway
home, although the OS account's own Hermes run had committed the identical directory
(:func:`pm.environments.install_key` hashes the *checkout* path, so both homes name the same
state). The runner can now hand the borrow the owner's tree via ``HERMES_INSTALLS_ROOT``.

Every case here calls pm and asserts *where it resolved*; none reads source text. The two
that matter most are the mirror pair: the borrow shares, and a launch whose root is the
owner itself must not, however the variable is set -- that is the isolation this change is
not allowed to buy its bytes with.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pm import environments as envs

pytestmark = pytest.mark.platforms("posix")


def _checkout(home_root: Path, *, committed: bool) -> Path:
    """A checkout living at ``<home_root>/hermes-agent``, with committed state on request."""
    checkout = home_root / "hermes-agent"
    checkout.mkdir(parents=True)
    if committed:
        state = home_root / "installs" / envs.install_key(checkout)
        state.mkdir(parents=True)
        (state / "facts.json").write_text("{}", encoding="utf-8")
    return checkout


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """The home that owns the checkout, and a second root that only borrows it.

    ``HOME`` is parked on a directory that holds nothing, so the platform default root
    :func:`pm.environments.owning_home_root` also considers can never answer for a case.
    """
    owner = tmp_path / "owner" / ".hermes"
    owner.mkdir(parents=True)
    borrower = tmp_path / "borrower"
    monkeypatch.setenv("HOME", str(tmp_path / "stray"))
    monkeypatch.delenv("HERMES_INSTALLS_ROOT", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(borrower))
    return owner, borrower


def _default_state_dir(checkout: Path) -> Path:
    """Where pm keeps this checkout's state with nothing overridden -- pm's own answer."""
    return envs.installs_root() / envs.install_key(checkout)


def test_both_homes_name_the_same_install_key(roots):
    """The premise: sharing is possible because the key is scoped to the checkout, not the home."""
    owner, _borrower = roots
    checkout = _checkout(owner, committed=True)
    assert envs.install_key(checkout) == envs.install_key(checkout.resolve())


def test_a_borrowing_launch_reuses_the_owner_state(roots, monkeypatch):
    owner, _borrower = roots
    checkout = _checkout(owner, committed=True)
    shared = owner / "shared-installs"
    shared.mkdir()
    monkeypatch.setenv("HERMES_INSTALLS_ROOT", str(shared))

    assert envs.owning_home_root(checkout) is not None, "fixture stopped being a borrow"
    assert envs.install_state_dir(checkout) == shared.resolve() / envs.install_key(checkout)


def test_the_borrow_stops_sharing_without_the_variable(roots):
    owner, _borrower = roots
    checkout = _checkout(owner, committed=True)
    default = _default_state_dir(checkout)

    resolved = envs.install_state_dir(checkout)

    assert resolved == default
    assert resolved.is_relative_to(owner.parent.parent), "default drifted out of the active root"


def test_the_owner_itself_never_shares_however_the_variable_is_set(roots, monkeypatch):
    """The isolation guarantee: a launch that owns this state keeps it private.

    A normal install -- or a profile of the owning home -- must not be re-pointed at some
    other tree by an inherited variable, or sharing would become the default and every
    install on the machine would land in one directory.
    """
    owner, _borrower = roots
    checkout = _checkout(owner, committed=True)
    monkeypatch.setenv("HERMES_HOME", str(owner))
    other = owner / "someone-elses-installs"
    other.mkdir()
    monkeypatch.setenv("HERMES_INSTALLS_ROOT", str(other))

    assert envs.owning_home_root(checkout) is None
    assert envs.install_state_dir(checkout) == owner / "installs" / envs.install_key(checkout)
    assert not (other / envs.install_key(checkout)).exists()


def test_nothing_is_shared_when_the_owner_has_no_committed_state(roots, monkeypatch, tmp_path):
    """A fresh install shares nothing: there is no committed state to reuse."""
    owner, _borrower = roots
    checkout = _checkout(owner, committed=False)
    shared = tmp_path / "shared-installs"
    shared.mkdir()
    monkeypatch.setenv("HERMES_INSTALLS_ROOT", str(shared))

    assert envs.owning_home_root(checkout) is None
    assert envs.install_state_dir(checkout) == _default_state_dir(checkout)


def test_a_blank_override_changes_nothing(roots, monkeypatch):
    owner, _borrower = roots
    checkout = _checkout(owner, committed=True)
    monkeypatch.setenv("HERMES_INSTALLS_ROOT", "   ")

    assert envs.install_state_dir(checkout) == _default_state_dir(checkout)


def test_only_the_state_of_the_committed_checkout_is_shared(roots, monkeypatch, tmp_path):
    """A second checkout the owner never installed must keep its own state beside the first."""
    owner, _borrower = roots
    committed = _checkout(owner, committed=True)
    fresh = owner / "other-checkout"
    fresh.mkdir()
    shared = tmp_path / "shared-installs"
    shared.mkdir()
    monkeypatch.setenv("HERMES_INSTALLS_ROOT", str(shared))

    assert envs.install_state_dir(committed) == shared.resolve() / envs.install_key(committed)
    assert envs.install_state_dir(fresh) == _default_state_dir(fresh)


def test_the_paths_derived_from_the_state_dir_follow_it(roots, monkeypatch, tmp_path):
    """facts.json and the activation stamps must not stay behind under the borrower."""
    owner, _borrower = roots
    checkout = _checkout(owner, committed=True)
    shared = tmp_path / "shared-installs"
    shared.mkdir()
    monkeypatch.setenv("HERMES_INSTALLS_ROOT", str(shared))
    state = shared.resolve() / envs.install_key(checkout)

    assert envs.runtime_facts_path(checkout) == state / "facts.json"
    assert envs.activation_inputs_dir(checkout) == state / "inputs"


def test_the_override_does_not_move_the_guard_anchor(roots, monkeypatch, tmp_path):
    """Publication and plugin selection compare against ``dependency_home_root``; it stays put."""
    owner, _borrower = roots
    checkout = _checkout(owner, committed=True)
    shared = tmp_path / "shared-installs"
    shared.mkdir()
    before = envs.dependency_home_root()
    monkeypatch.setenv("HERMES_INSTALLS_ROOT", str(shared))

    assert envs.install_state_dir(checkout).is_relative_to(shared.resolve())
    assert envs.dependency_home_root() == before


def test_an_unusable_override_path_is_still_the_callers_declaration(roots, monkeypatch, tmp_path):
    """pm does not second-guess the value it was handed -- the runner is what checks reuse.

    Pinning this so a later "helpful" existence check cannot silently fall back to the
    per-home layout and turn a shared run back into a 3.4 GB one with no signal.
    """
    owner, _borrower = roots
    checkout = _checkout(owner, committed=True)
    monkeypatch.setenv("HERMES_INSTALLS_ROOT", str(tmp_path / "does-not-exist-yet"))

    assert envs.install_state_dir(checkout) == (tmp_path / "does-not-exist-yet").resolve() / envs.install_key(checkout)
