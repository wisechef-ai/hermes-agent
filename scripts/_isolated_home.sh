# Sourced, never executed: the lifecycle of a disposable test HOME.
# scripts/run_tests.sh and scripts/run-in-isolated-home.sh both build on it and
# neither owns any of it, the same division scripts/_activation.sh keeps.
#
# ── The leak this exists for ─────────────────────────────────────────────────
# `HOME=$(mktemp -d) env -u HERMES_HOME bash scripts/run_tests.sh …` is how a
# test gets reproduced against a clean account. The first run under such a home
# has no current activation, so the runner re-executes under
# scripts/run-in-hermes-env, which materializes the checkout's test environment
# — uv-managed CPython plus the pm runtime generations — under
# $HOME/.hermes/installs. On a warm host that is ~3.6 GB per invocation.
#
# Nothing removed it. The `mktemp -d` belongs to the caller's shell, and an EXIT
# trap cannot help from inside the repo: every `exec` in the chain (run_tests.sh
# → run-in-hermes-env → `env -i` pytest) replaces the process image and wipes
# the shell's traps, so a trap registered by any script in the chain is gone
# before the run that costs the gigabytes finishes. A development loop iterating
# that one-liner leaked 15 GB in 20 minutes and filled a CI host's disk.
#
# ── What this guarantees ─────────────────────────────────────────────────────
# A home this code CLAIMED is removed when the supervised command exits, on
# every exit path: success, failure, SIGINT, SIGTERM, SIGHUP. A home it did not
# claim is never touched, so a home a human prepared — a stable path kept warm
# for fast reruns, a partially set-up home, anything outside the temp root —
# stays theirs. Only a tree this process itself created is ever deleted, which
# is why the claim is taken while the home is still EMPTY: whatever is inside at
# sweep time was put there by the run we supervised.
#
# ── Every check that must pass before an `rm -rf` (fail-closed) ──────────────
#   claim   absolute, exactly one directory below the temp root, mktemp-shaped,
#           a real directory rather than a link, no link on the way in, owned by
#           us, not writable by group or other, not the account's own home, not
#           a mount point, not named by another home-ish variable (HERMES_HOME,
#           HERMES_RUNTIME_DIR, the XDG dirs, TMPDIR), and EMPTY; a provenance
#           record is written beside it.
#   sweep   that record still names this home, dev:ino is unchanged (the
#           directory was not swapped under us), the claim checks all still
#           pass, no live process holds it as its cwd, and either the leak
#           signature ($HOME/.hermes/installs) is present or the home is still
#           empty. A home the run never provisioned, and that holds content we
#           cannot account for, is left where it is. When the home is simply gone
#           — the supervised run, or a host sweep, already took it — the record is
#           retired too, so the temp root holds no dot-file nothing collects and a
#           later home reusing the leaf is not refused its own claim.
# A check that cannot be evaluated refuses the delete and says why. The one
# exception is the mount and cwd tables on a host without /proc, where every
# other check still applies and the cost of a missed pass is disk, not data.
#
# A supervisor killed by SIGKILL leaves its home behind — no in-process trap can
# survive that. A host-level sweep is the belt to these braces; this file is
# what makes the ordinary path not need one.

# Tag in the provenance record, so a record written by a different generation of
# runner is never trusted by this one.
_hermes_isolated_home_magic="hermes-isolated-home-v1"

# The directory a provisioned test environment leaves in the home. Its presence
# is what marks a home as one this run filled.
_hermes_isolated_home_signature=".hermes/installs"

# The leaf prefix this file hands out, and the template a caller gives mktemp from
# it. The prefix is what the shape check matches: a created home carries mktemp's
# random tail, never the template's X's, so the two must not be conflated.
_hermes_isolated_home_prefix="hermes-isolated-home"
_hermes_isolated_home_template="$_hermes_isolated_home_prefix-XXXXXX"

hermes_isolated_home_note() {
    printf '▶ isolated home: %s\n' "$*" >&2
}

# hermes_isolated_home_stat GNU_FMT BSD_FMT PATH: one stat field, printed only
# when the answer is a single field. The dialect cannot be chosen by exit
# status: GNU `stat -f` means "file system", so on Linux it answers the BSD
# request too, with a table.
hermes_isolated_home_stat() {
    local out
    # No `--` before the path: BSD stat does not accept it, and the shape check
    # already guarantees an absolute path, so there is nothing to separate.
    out="$(stat -c "$1" "$3" 2>/dev/null)" || out=""
    case "$out" in "" | *[[:space:]]*) ;; *) printf '%s\n' "$out"; return 0 ;; esac
    out="$(stat -f "$2" "$3" 2>/dev/null)" || out=""
    case "$out" in "" | *[[:space:]]*) ;; *) printf '%s\n' "$out"; return 0 ;; esac
    return 1
}

# hermes_isolated_home_tmproot: the canonical temp root, links resolved. Falling
# back to /tmp keeps a run with a stale TMPDIR cleaning up where mktemp would
# actually have written.
hermes_isolated_home_tmproot() {
    local root resolved
    # no-tmp: ok — walking the OS temp roots is the job here: $TMPDIR first, the conventional fallback second. Neither is a scratch dir we write into.
    for root in "${TMPDIR:-}" /tmp; do
        [ -n "$root" ] && [ -d "$root" ] || continue
        resolved="$(cd "$root" 2>/dev/null && pwd -P)" || continue
        [ -n "$resolved" ] || continue
        printf '%s\n' "$resolved"
        return 0
    done
    return 1
}

# hermes_isolated_home_account_home: the OS account's home, when it can be named.
hermes_isolated_home_account_home() {
    local account
    account="$(getent passwd "$EUID" 2>/dev/null | cut -d: -f6)"
    [ -n "$account" ] || account="$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | sed -n 's/^NFSHomeDirectory: *//p')"
    [ -n "$account" ] || return 1
    printf '%s\n' "$account"
}

# hermes_isolated_home_is_mounted DIR: 0 when DIR or anything under it is a
# mount point, because removing it would then delete a file system rather than a
# test home. Status 1 also covers a host with no readable table (no /proc on
# macOS/BSD): the alternative would be that such a host can never clean up.
hermes_isolated_home_is_mounted() {
    local dir="$1" table="${HERMES_ISOLATED_HOME_MOUNTS_TABLE:-/proc/mounts}" mount _dev _fstype
    [ -r "$table" ] || return 1
    # /proc/mounts is "device mountpoint fstype …"; a mount point never holds a
    # literal space (the kernel octal-escapes it), so a field split is exact.
    while read -r _dev mount _fstype; do
        [ -n "$mount" ] || continue
        case "$mount" in
            "$dir" | "$dir"/*) return 0 ;;
        esac
    done < "$table"
    return 1
}

# hermes_isolated_home_in_use DIR: 0, with the pid, when a live process still
# has DIR as its cwd — a daemon the run left running in its own home is not
# ours to yank out from under. bash's `-ef` stats both sides, so this costs one
# stat per process and no subprocess: /proc/<pid>/cwd resolves the link itself.
# Processes we cannot read are skipped; one of them would need access to a 0700
# directory we own to matter, and the host-level sweep does that exhaustive pass.
hermes_isolated_home_in_use() {
    local dir="$1" proc
    [ -d /proc ] || return 1
    for proc in /proc/[0-9]*; do
        [ "$proc" = "/proc/$$" ] && continue
        [ -e "$proc/cwd" ] || continue
        if [ "$dir" -ef "$proc/cwd" ]; then
            printf '%s\n' "${proc#/proc/}"
            return 0
        fi
    done
    return 1
}

# hermes_isolated_home_is_named_elsewhere DIR: 0, with the offending assignment,
# when the rest of the environment already treats DIR as the session's home. A
# harness that exports HOME for a whole session and points the state directories
# into it expects that home to outlive one command; refusing the claim there
# costs a directory the host sweep can collect, where deleting it would cost the
# session its home. Asymmetric on purpose: every doubt resolves toward keeping.
#
# HERMES_RUNTIME_DIR belongs in this list for the reason pm/_unstamped_store
# already states: it is the tool store, and "a new store per temporary home" is
# exactly the thing a disposable home is expected to throw away — unless a
# session named it deliberately, which is what an exported value says. It is
# read here as an inbound override only: pm exports it itself, but that happens
# during provisioning, after the claim decision has been taken, so the ordinary
# disposable run (where it is unset, or points at the account's shared store
# outside the home) is never vetoed by it.
hermes_isolated_home_is_named_elsewhere() {
    local dir="$1" named value
    for named in HERMES_HOME HERMES_RUNTIME_DIR XDG_CONFIG_HOME XDG_CACHE_HOME \
                 XDG_DATA_HOME XDG_STATE_HOME TMPDIR; do
        value="${!named:-}"
        [ -n "$value" ] || continue
        case "$value" in
            "$dir" | "$dir"/*)
                printf '%s=%s\n' "$named" "$value"
                return 0 ;;
        esac
    done
    return 1
}

# hermes_isolated_home_is_empty DIR: 0 when DIR holds nothing. Globs, not `ls`,
# so the check costs no subprocess; `.` and `..` match none of the patterns.
hermes_isolated_home_is_empty() {
    local dir="$1" entry
    for entry in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        return 1
    done
    return 0
}

# hermes_isolated_home_shape_ok DIR: 0 when DIR could only have been made by
# `mktemp -d` (or by this file's own wrapper) for this run, and naming it could
# not accidentally point somewhere a person keeps things.
hermes_isolated_home_shape_ok() {
    local dir="$1" root real owner perms account
    [ -n "$dir" ] || return 1
    case "$dir" in /*) ;; *) return 1 ;; esac
    [ "${dir%/}" = "$dir" ] || return 1
    root="$(hermes_isolated_home_tmproot)" || return 1
    case "$dir" in
        "$root" | "$root"/*/*) return 1 ;;
        "$root"/*) ;;
        *) return 1 ;;
    esac
    case "${dir##*/}" in
        tmp.?????????? | "$_hermes_isolated_home_prefix"-*) ;;
        *) return 1 ;;
    esac
    [ -d "$dir" ] && [ ! -L "$dir" ] || return 1
    real="$(cd "$dir" 2>/dev/null && pwd -P)" || return 1
    [ "$real" = "$dir" ] || return 1
    owner="$(hermes_isolated_home_stat '%u' '%u' "$dir")" || return 1
    [ "$owner" = "$EUID" ] || return 1
    perms="$(hermes_isolated_home_stat '%a' '%Lp' "$dir")" || return 1
    case "$perms" in "" | *[!0-7]*) return 1 ;; esac
    [ "$(( 0"$perms" & 8#022 ))" -eq 0 ] || return 1
    if account="$(hermes_isolated_home_account_home)" && [ "$dir" = "$account" ]; then
        return 1
    fi
    ! hermes_isolated_home_is_mounted "$dir"
}

# hermes_isolated_home_sentinel_path DIR: where DIR's provenance record lives.
# Beside the home, never inside it: a file in the home would be visible to the
# very tests the run is isolating. The leaf is mktemp's own, and the home is one
# directory below the temp root, so the name is unique and a person can see
# which home a stray record belonged to.
hermes_isolated_home_sentinel_path() {
    local dir="$1" root
    root="$(hermes_isolated_home_tmproot)" || return 1
    printf '%s/.%s.%s\n' "$root" "$_hermes_isolated_home_magic" "${dir##*/}"
}

# An existing claim is a hand-off, not a fault: whoever holds it owns the delete.
# Silence when the holder is the process that launched us, which is the ordinary
# wrapper path (scripts/run-in-isolated-home.sh claims, then runs run_tests.sh);
# a note otherwise, worded to say who is really holding it.
_hermes_isolated_home_report_existing_claim() {
    local dir="$1" sentinel="$2" holder=""
    if [ ! -r "$sentinel" ]; then
        hermes_isolated_home_note "$dir carries a claim this run cannot read; leaving it alone"
        return 0
    fi
    holder="$(sed -n 's/^pid=//p' "$sentinel" 2>/dev/null | head -n 1)"
    case "$holder" in "" | *[!0-9]*) holder="" ;; esac
    if [ -n "$holder" ] && [ "$holder" = "$PPID" ]; then
        return 0
    fi
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
        hermes_isolated_home_note "$dir is claimed by a running process ($holder); leaving it alone"
    else
        hermes_isolated_home_note "$dir still carries a claim from an earlier run; leaving it alone"
    fi
    return 0
}

# hermes_isolated_home_claim DIR: 0 when this process now owns DIR's disposal.
# The claim is what authorizes the delete, so it is taken before anything is
# written into the home: an empty home at claim time is the proof that everything
# inside it later came from this run.
hermes_isolated_home_claim() {
    local dir="$1" sentinel id named
    hermes_isolated_home_shape_ok "$dir" || return 1
    if named="$(hermes_isolated_home_is_named_elsewhere "$dir")"; then
        hermes_isolated_home_note "$dir is named by $named; leaving the session's home alone"
        return 1
    fi
    if ! hermes_isolated_home_is_empty "$dir"; then
        hermes_isolated_home_note "$dir was not empty when the run started; leaving it to its owner"
        return 1
    fi
    id="$(hermes_isolated_home_stat '%d:%i' '%d:%i' "$dir")" || return 1
    sentinel="$(hermes_isolated_home_sentinel_path "$dir")" || return 1
    if [ -e "$sentinel" ]; then
        _hermes_isolated_home_report_existing_claim "$dir" "$sentinel"
        return 1
    fi
    # A subshell for the umask: setting it here would follow the run and make
    # every file the tests create unreadable to their own group.
    (
        umask 077
        printf '%s\n' "$_hermes_isolated_home_magic"
        printf 'path=%s\n' "$dir"
        printf 'id=%s\n' "$id"
        printf 'uid=%s\n' "$EUID"
        printf 'pid=%s\n' "$$"
        printf 'repo=%s\n' "${_hermes_isolated_home_repo:-unknown}"
    ) > "$sentinel" 2>/dev/null || return 1
    _HERMES_ISOLATED_HOME_CLAIMED="$dir"
    export _HERMES_ISOLATED_HOME_CLAIMED
    return 0
}

# hermes_isolated_home_sweep: remove the home this process claimed. It never
# fails a run — a home that refuses any check is kept and reported, because a
# leaked directory costs disk while a wrong delete costs the run its data.
hermes_isolated_home_sweep() {
    local dir="${_HERMES_ISOLATED_HOME_CLAIMED:-}" sentinel refusal="" record pid=""
    local field value current="" rec_path="" rec_id="" rec_uid="" rec_pid="" gone=""
    [ -n "$dir" ] || return 0
    if [ "${HERMES_TEST_ISOLATED_HOME_KEEP:-0}" = "1" ]; then
        hermes_isolated_home_note "keeping $dir (HERMES_TEST_ISOLATED_HOME_KEEP)"
        return 0
    fi
    sentinel="$(hermes_isolated_home_sentinel_path "$dir")" || {
        hermes_isolated_home_note "keeping $dir (no temp root to check its record against)"
        return 0
    }
    if [ ! -r "$sentinel" ]; then
        hermes_isolated_home_note "keeping $dir (its provenance record is gone)"
        return 0
    fi
    record="$(cat "$sentinel" 2>/dev/null)" || record=""
    if [ "${record%%$'\n'*}" != "$_hermes_isolated_home_magic" ]; then
        refusal="its provenance record was not written by this runner"
    else
        while IFS='=' read -r field value; do
            case "$field" in
                path) rec_path="$value" ;;
                id) rec_id="$value" ;;
                uid) rec_uid="$value" ;;
                pid) rec_pid="$value" ;;
            esac
        done <<<"$record"
        current="$(hermes_isolated_home_stat '%d:%i' '%d:%i' "$dir")" || current=""
        if [ "$rec_path" != "$dir" ]; then
            refusal="its provenance record names $rec_path"
        elif [ "$rec_pid" != "$$" ] || [ "$rec_uid" != "$EUID" ]; then
            # Ownership is settled before existence. Retiring a record whose home
            # has vanished is safe only once the record is proven to be this
            # process's own, so this branch must stay ahead of the next two.
            refusal="its provenance record belongs to another process"
        elif [ -z "$current" ]; then
            if [ -e "$dir" ] || [ -L "$dir" ]; then
                # Something is still there that we cannot identify: keep it.
                refusal="its identity can no longer be read"
            else
                gone=1
            fi
        elif [ "$rec_id" != "$current" ]; then
            refusal="it is a different directory than the one this run claimed"
        fi
    fi
    # A record whose home is already gone protects nothing and costs the temp root
    # a file that nothing ever collects: the host sweep matches /tmp/tmp.* test
    # environments, not these dot-files, and a surviving record also refuses the
    # claim of any later home that reuses the leaf. Reaching here means the magic,
    # path, pid and uid all matched, so this is our own record and retiring it
    # cannot orphan somebody else's claim. A home that merely refuses a check is
    # still there, and keeps its record.
    if [ -z "$refusal" ] && [ -n "$gone" ]; then
        if rm -f -- "$sentinel" 2>/dev/null; then
            hermes_isolated_home_note "removed $dir (the run removed it already; its provenance record is retired)"
        else
            hermes_isolated_home_note "could not retire the provenance record for $dir; leaving it for the host sweep"
        fi
        return 0
    fi
    if [ -z "$refusal" ] && ! hermes_isolated_home_shape_ok "$dir"; then
        refusal="it no longer has the shape of a disposable home"
    fi
    # The signature is what proves the run filled the home. Without it the home is
    # kept — unless it is still empty, the one case where a delete cannot lose
    # anything: we claimed it empty, it is the same directory, and there is nothing
    # in it. Dropping that tombstone is what lets the blessed wrapper leave no
    # residue at all, on runs that never provision as well as the ones that do.
    if [ -z "$refusal" ] && [ ! -d "$dir/$_hermes_isolated_home_signature" ] \
            && ! hermes_isolated_home_is_empty "$dir"; then
        refusal="the run never provisioned a test environment into it and it holds content we cannot account for"
    fi
    if [ -z "$refusal" ] && hermes_isolated_home_is_mounted "$dir"; then
        refusal="it, or something under it, is a mount point"
    fi
    if [ -z "$refusal" ] && pid="$(hermes_isolated_home_in_use "$dir")"; then
        refusal="pid $pid still has it as its cwd"
    fi
    if [ -n "$refusal" ]; then
        hermes_isolated_home_note "keeping $dir ($refusal)"
        return 0
    fi
    if rm -rf -- "$dir"; then
        hermes_isolated_home_note "removed $dir"
    else
        hermes_isolated_home_note "could not remove $dir; leaving it for the host sweep"
    fi
    rm -f -- "$sentinel" 2>/dev/null || :
    return 0
}

# hermes_isolated_home_supervise CMD [ARGS…]: run CMD with $HOME claimed for
# this process, then clean up. CMD must be a child, not an exec: the parent that
# survives CMD is the only process in the chain still able to delete anything,
# which is precisely what an EXIT trap cannot be across CMD's own execs.
# Falls through to a plain exec of CMD when the home is not ours to clean.
hermes_isolated_home_supervise() {
    local dir="${HOME:-}" rc=0 child="" signal="" polls=0
    [ "$#" -gt 0 ] || return 2
    if [ -z "$dir" ] || ! hermes_isolated_home_claim "$dir"; then
        exec "$@"
    fi
    hermes_isolated_home_note "claimed $dir for this run; it is removed when the run exits (HERMES_TEST_ISOLATED_HOME_KEEP=1 keeps it)"
    trap 'hermes_isolated_home_sweep' EXIT
    trap 'signal=HUP;  [ -n "$child" ] && kill -HUP  "$child" 2>/dev/null' HUP
    trap 'signal=INT;  [ -n "$child" ] && kill -INT   "$child" 2>/dev/null' INT
    trap 'signal=TERM; [ -n "$child" ] && kill -TERM  "$child" 2>/dev/null' TERM
    "$@" & child=$!
    # wait returns >128 both when CMD dies of a signal and when our own wait is
    # interrupted by a forwarded one, so the status alone cannot end this loop:
    # keep waiting while CMD still exists, which also guarantees the sweep never
    # deletes a tree a supervised process is still writing.
    while :; do
        if wait "$child"; then rc=0; else rc=$?; fi
        kill -0 "$child" 2>/dev/null || break
        polls=$(( polls + 1 ))
        if [ "$polls" -ge 100000 ]; then
            # Cannot prove the command is gone, so cannot prove the tree is idle:
            # stand down and leave the home for the host sweep rather than delete
            # under a process that may still be writing it.
            hermes_isolated_home_note "giving up waiting on pid $child; leaving $dir alone"
            unset _HERMES_ISOLATED_HOME_CLAIMED
            rc=1
            break
        fi
    done
    case "$signal" in
        HUP) rc=129 ;;
        INT) rc=130 ;;
        TERM) rc=143 ;;
    esac
    exit "$rc"
}
