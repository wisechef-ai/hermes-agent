#!/usr/bin/env bash
# Run a command under a throwaway HOME that is removed when the command exits.
#
#   scripts/run-in-isolated-home.sh scripts/run_tests.sh tests/agent/test_foo.py -v
#   just test-isolated tests/agent/test_foo.py -v
#
# The form everyone reaches for first is `HOME=$(mktemp -d) CMD`, and for a test
# run that is exactly how a 3.6 GB test environment ends up orphaned: nothing
# cleans up a home the caller's own shell made, and no EXIT trap inside the repo
# survives the execs a run goes through (scripts/_isolated_home.sh). This wrapper
# is the blessed form of that recipe — the home is created here, claimed here,
# and deleted here on every exit path — so the disposable case needs no
# discipline from the person typing the command.
#
# A home is disposable precisely because nobody can name it in advance. To keep
# one warm for fast reruns, name it yourself instead:
#
#   HOME=/path/to/hermes-dev-home scripts/run_tests.sh tests/agent/test_foo.py
#
# Nothing deletes a home that is named, and a second run under the same name
# finds the activation still current and skips the provisioning that costs both
# the gigabytes and the minutes. That is the fast loop; this wrapper is the
# clean one. HERMES_TEST_ISOLATED_HOME_KEEP=1 turns this into the same thing
# after the fact, printing where the home was left.
#
# POSIX hosts. On native Windows a home is USERPROFILE (with HOMEDRIVE and
# HOMEPATH), which this wrapper does not rewrite; run_tests.sh forwards whatever
# the caller already has, so an isolated Windows run still names its own home.
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_isolated_home.sh
. "$_here/_isolated_home.sh"

if [ "$#" -eq 0 ]; then
    printf 'usage: %s CMD [ARGS...]\n' "$0" >&2
    exit 2
fi
if [ -n "${_HERMES_ISOLATED_HOME_CLAIMED:-}" ]; then
    printf 'error: already inside an isolated home (%s); refusing to nest one\n' \
        "$_HERMES_ISOLATED_HOME_CLAIMED" >&2
    exit 2
fi

root="$(hermes_isolated_home_tmproot)" || {
    # no-tmp: ok — the message names the rejected fallback root; nothing is created from this line.
    printf 'error: no usable temp root (TMPDIR=%s and /tmp are both unusable)\n' "${TMPDIR:-(unset)}" >&2
    exit 1
}
home="$(mktemp -d "$root/$_hermes_isolated_home_template")" || exit 1
# Resolve it: TMPDIR may carry a trailing slash or a link on the way, and the
# checks in _isolated_home.sh compare a home against its own realpath.
home="$(cd "$home" && pwd -P)"

# The leak recipe prefixes `env -u HERMES_HOME` for a reason, so do it here: an
# inherited HERMES_HOME points the supposedly isolated home's Hermes state back
# at the account that exported it, which makes the run neither isolated nor
# reproducible. Only that inherited default is dropped — a HERMES_HOME chosen
# for this command says something the caller means, so it is kept and reported.
case "${HERMES_HOME:-}" in
    "${HOME:-}/.hermes" | "${HOME:-}/.hermes"/*)
        printf '▶ isolated home: dropped inherited HERMES_HOME=%s; it names the account, not %s\n' \
            "$HERMES_HOME" "$home" >&2
        unset HERMES_HOME
        ;;
    "") ;;
    *)
        printf '▶ isolated home: keeping HERMES_HOME=%s; Hermes state will not be isolated by this run\n' \
            "$HERMES_HOME" >&2
        ;;
esac

export HOME="$home"
_hermes_isolated_home_repo="$(cd "$_here/.." && pwd)"
hermes_isolated_home_supervise "$@"
