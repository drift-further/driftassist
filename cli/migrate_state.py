"""`assist migrate-state` — move runtime state out of the checkout, as a transaction.

Moves every state item (shared/paths.py:STATE_ITEMS) from the checkout (the
legacy layout) to the XDG config and data dirs, then selects the XDG layout
by writing install.json. `--rollback` runs the same machine the other way,
from the CURRENT XDG files, so changes made after the migration are kept.

It is a state machine, not a loop. The fixed inputs and the item inventory
(each item's sha256) are recorded in `state_dir()/migration.json`, written
durably at every phase transition:

    preflight   quiesce checks, inventory, destination checks
    copying     every item to <dst>.part, fsynced, hash-checked. Sources untouched.
    validating  every .part re-hashed; the transformed provenance receipt
                validated against the staged store; destination conflicts
                reported (rollback: a differing legacy file stops the run)
    publishing  each .part renamed to its final name; the old layout is
                still complete and still selected
    committed   install.json written (forward) or removed (rollback): the
                new layout is selected from here
    cleaning    each source whose sha256 still equals the inventory is
                deleted; one that differs is left and reported
    done

`--resume` replays from the recorded phase, comparing hashes, never trusting
the existence of a file. A marker that cannot be read means recovery by hand:
nothing is copied, cleaned or guessed. There is no --force and no take-over
of an install.json. The tool holds the layout lock exclusively from before
preflight, so no server or CLI command on the new code can start meanwhile;
old code is quiesced by refusing to run while any process names this
checkout's bin/assist or serve.py. Live tmux panes are never touched.

Imports no application state: it runs while that state is being moved.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from shared import launch_provenance as provenance
from shared import paths

PHASES = paths.MIGRATION_PHASES
RECEIPT = provenance.INITIALIZATION_RECEIPT_NAME
STORE = provenance.ROOT_NAME
POINTER = ".assist-state-moved"
RECEIPT_ORIG = "migration-receipt.orig.json"


class MigrationError(Exception):
    """Stop: the message says what and what to do. The marker stays as written."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Test hooks: named points where the tests SIGKILL or pause the tool
# ---------------------------------------------------------------------------


def _point(name):
    trace = os.environ.get("ASSIST_MIGRATE_TEST_TRACE")
    if trace:
        with open(trace, "a", encoding="utf-8") as stream:
            stream.write(name + "\n")
    if os.environ.get("ASSIST_MIGRATE_TEST_CRASH") == name:
        os.kill(os.getpid(), signal.SIGKILL)
    if os.environ.get("ASSIST_MIGRATE_TEST_PAUSE") == name:
        release = Path(os.environ.get("ASSIST_MIGRATE_TEST_RELEASE", "/nonexistent"))
        deadline = time.monotonic() + 120
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def sha256_of(path):
    """A file's sha256; a directory's covers names, modes and contents."""
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        digest = hashlib.sha256()
        for child in sorted(path.rglob("*")):
            rel = str(child.relative_to(path)).encode()
            digest.update(rel + b"\0" + oct(child.lstat().st_mode & 0o777).encode())
            if child.is_file():
                digest.update(child.read_bytes())
        return digest.hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_file(src, dst):
    _write_bytes(dst, Path(src).read_bytes(), os.stat(src).st_mode & 0o777)


def _copy_tree(src, dst):
    os.mkdir(dst, os.stat(src).st_mode & 0o777)
    os.chmod(dst, os.stat(src).st_mode & 0o777)
    for child in sorted(Path(src).iterdir()):
        if child.is_dir() and not child.is_symlink():
            _copy_tree(child, Path(dst) / child.name)
        else:
            _copy_file(child, Path(dst) / child.name)
    _fsync_dir(dst)


def _remove(path):
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _part(path):
    return Path(str(path) + ".part")


def _write_marker(marker):
    paths._write_durable(paths.migration_marker_path(), marker)
    _point(f"marker:{marker['phase']}")


# ---------------------------------------------------------------------------
# Quiesce
# ---------------------------------------------------------------------------


PROC_ROOT = Path("/proc")


def _process_argvs():
    """{pid: argv} for every process this user can see.

    Fails closed: a listing that cannot be produced, or that does not even
    contain this process, is not evidence that nothing else is running
    (finding 3867). /proc entries that vanish or are unreadable mid-scan are
    other users' or exited processes and are skipped.
    """
    out = {}
    if PROC_ROOT.is_dir():
        for entry in PROC_ROOT.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if raw:
                out[int(entry.name)] = raw.decode(errors="replace").rstrip("\0").split("\0")
    else:
        try:
            listing = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True,
                                     text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MigrationError(f"refusing: cannot list processes ({exc}). Nothing was changed.")
        if listing.returncode != 0:
            raise MigrationError(f"refusing: cannot list processes (ps exited {listing.returncode}: "
                                 f"{listing.stderr.strip()[:200]}). Nothing was changed.")
        for line in listing.stdout.splitlines():
            pid, _, args = line.strip().partition(" ")
            if pid.isdigit():
                out[int(pid)] = args.split()
    if os.getpid() not in out:
        raise MigrationError("refusing: cannot list processes reliably (this process is not in the "
                             "listing), so a running assist command could be missed. Nothing was changed.")
    return out


def _quiesce(resolved, checkout):
    """Refuse while a server or any old checkout CLI process could touch the state."""
    problems = []
    from cli.config import runs_checkout

    try:
        pid = int(Path(resolved.pid_file).read_text().strip())
        if runs_checkout(pid, checkout):
            problems.append(f"the Assist server is running (PID {pid}); run: assist stop")
    except (OSError, ValueError):
        pass
    with socket.socket() as sock:
        sock.settimeout(1)
        if sock.connect_ex(("127.0.0.1", resolved.port)) == 0:
            problems.append(f"a server answers on port {resolved.port}; stop it first (assist stop)")
    watched = {str(checkout / "bin" / "assist"), str(checkout / "serve.py")}
    watched |= {os.path.realpath(p) for p in watched}
    mine = {os.getpid(), os.getppid()}
    for pid, argv in sorted(_process_argvs().items()):
        if pid in mine:
            continue
        if any(arg in watched or os.path.realpath(arg) in watched for arg in argv if arg.startswith("/")):
            problems.append(f"PID {pid} runs this checkout's Assist: {' '.join(argv)[:160]}")
    if problems:
        raise MigrationError(
            "refusing to migrate while Assist is in use:\n  " + "\n  ".join(problems)
            + "\nStop the server and any `assist` scripts, then run this again."
        )


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def _item(label, src, dst, *, data=None):
    """`sha256` is what the destination must hold; `src_sha256` what the
    source held at inventory, which cleaning compares before deleting it.
    They differ only for the receipt, whose bytes are transformed."""
    src = Path(src)
    entry = {"label": label, "src": str(src), "dst": str(dst),
             "kind": "dir" if src.is_dir() else "file", "src_sha256": sha256_of(src)}
    entry["sha256"] = hashlib.sha256(data).hexdigest() if data is not None else entry["src_sha256"]
    return entry


def _inventory(checkout, direction, config_dir, data_dir, state_dir):
    """Every item that exists on the source side, with its source hash.

    The provenance receipt is two items: `receipt.new` (the bytes the
    destination gets: provenance_root rewritten for the destination store)
    and `receipt.orig` (forward: the original bytes, kept in the state dir
    so a rollback can prove its inverse transform; rollback: that copy,
    removed when the rollback finishes).
    """
    items = []
    legacy_receipt = checkout / RECEIPT
    xdg_receipt = data_dir / RECEIPT
    for legacy_name, root, xdg_name in paths.STATE_ITEMS:
        if legacy_name == RECEIPT:
            continue
        legacy = checkout / legacy_name
        xdg = (config_dir if root == "config" else data_dir) / xdg_name
        src, dst = (legacy, xdg) if direction == "forward" else (xdg, legacy)
        if src.exists():
            items.append(_item(legacy_name, src, dst))
    orig_path = Path(state_dir) / RECEIPT_ORIG
    if direction == "forward" and legacy_receipt.exists():
        original = legacy_receipt.read_bytes()
        moved = provenance.transform_receipt(original, data_dir / STORE)
        items.append(_item("receipt.new", legacy_receipt, xdg_receipt, data=moved))
        items.append(_item("receipt.orig", legacy_receipt, orig_path, data=original))
    elif direction == "reverse" and xdg_receipt.exists():
        current = xdg_receipt.read_bytes()
        restored = provenance.transform_receipt(current, checkout / STORE)
        if orig_path.exists() and orig_path.read_bytes() != restored:
            raise MigrationError(
                f"the provenance receipt does not round-trip: {xdg_receipt} transformed back "
                f"differs from the original kept at {orig_path}. Nothing was changed."
            )
        items.append(_item("receipt.new", xdg_receipt, legacy_receipt, data=restored))
    return items


def _staged_bytes(item, checkout, data_dir, direction):
    """What .part gets for an item: its source bytes, or the transformed receipt."""
    if item["label"] == "receipt.new":
        source = Path(item["src"]).read_bytes()
        target = (data_dir if direction == "forward" else checkout) / STORE
        return provenance.transform_receipt(source, target)
    return None


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


def _canonical(path):
    """Absolute, symlinks resolved: the physical path a journal binds to."""
    return os.path.realpath(os.path.abspath(path))


def _identity(resolved):
    """The transaction identity of THIS invocation: what a journal binds to.

    Roots come from the environment exactly once, here, and are recorded
    canonical: absolute (a relative input is taken against this cwd) with
    symlinks resolved. Recovery compares canonical to canonical, so a symlink
    retargeted since the journal was written is a mismatch, not a silent new
    destination (finding 3906). Every later step uses the journal's copy.
    """
    config_dir, data_dir = paths.xdg_roots()
    token = os.environ.get("ASSIST_AUTH_TOKEN_PATH")
    return {
        "checkout": _canonical(resolved.home),
        "config_dir": _canonical(config_dir),
        "data_dir": _canonical(data_dir),
        "state_dir": _canonical(paths.state_dir()),
        "install_json": _canonical(paths.install_json_path()),
        "token_path": _canonical(token) if token else None,
    }


def _new_marker(resolved, direction):
    return {
        "schema": paths.MARKER_SCHEMA,
        "direction": direction,
        "phase": "preflight",
        **_identity(resolved),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": [],
    }


_HEX64 = set("0123456789abcdef")


def _expected_items(marker):
    """{label: (src, dst)} the journal's roots allow, per direction."""
    checkout = Path(marker["checkout"])
    config_dir, data_dir, state_dir = (Path(marker[k]) for k in ("config_dir", "data_dir", "state_dir"))
    out = {}
    for legacy_name, root, xdg_name in paths.STATE_ITEMS:
        if legacy_name == RECEIPT:
            continue
        legacy = checkout / legacy_name
        xdg = (config_dir if root == "config" else data_dir) / xdg_name
        out[legacy_name] = (legacy, xdg) if marker["direction"] == "forward" else (xdg, legacy)
    legacy_receipt, xdg_receipt = checkout / RECEIPT, data_dir / RECEIPT
    if marker["direction"] == "forward":
        out["receipt.new"] = (legacy_receipt, xdg_receipt)
        out["receipt.orig"] = (legacy_receipt, state_dir / RECEIPT_ORIG)
    else:
        out["receipt.new"] = (xdg_receipt, legacy_receipt)
    return out


def _validate_journal(marker):
    """The whole journal, before it is allowed to drive a single file operation.

    Schema, direction, phase, absolute roots, and every item's shape and its
    exact source/destination as derived from those roots. Anything else is
    `recovery required` with no effect.
    """
    problems = []
    if marker.get("schema") != paths.MARKER_SCHEMA:
        problems.append(f"schema {marker.get('schema')!r} is not {paths.MARKER_SCHEMA}")
    if marker.get("direction") not in ("forward", "reverse"):
        problems.append(f"direction {marker.get('direction')!r}")
    if marker.get("phase") not in PHASES:
        problems.append(f"phase {marker.get('phase')!r}")
    for key in ("checkout", "config_dir", "data_dir", "state_dir", "install_json"):
        value = marker.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            problems.append(f"{key} {value!r} is not an absolute path")
    token = marker.get("token_path")
    if token is not None and (not isinstance(token, str) or not os.path.isabs(token)):
        problems.append(f"token_path {token!r}")
    items = marker.get("items")
    if not isinstance(items, list) or (not items and marker.get("phase") != "preflight"):
        problems.append("items is not a non-empty list")
        items = []
    if not problems:
        expected = _expected_items(marker)
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                problems.append("an item is not an object")
                continue
            label = item.get("label")
            if label not in expected or label in seen:
                problems.append(f"item label {label!r}")
                continue
            seen.add(label)
            src, dst = expected[label]
            if item.get("src") != str(src) or item.get("dst") != str(dst):
                problems.append(f"item {label}: {item.get('src')} -> {item.get('dst')} is not {src} -> {dst}")
            if item.get("kind") not in ("file", "dir"):
                problems.append(f"item {label}: kind {item.get('kind')!r}")
            for key in ("sha256", "src_sha256"):
                value = item.get(key)
                if not (isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64):
                    problems.append(f"item {label}: {key} {value!r}")
    if problems:
        raise MigrationError(
            f"recovery required: the journal {paths.migration_marker_path()} is not one this tool "
            "can act on (" + "; ".join(problems[:6]) + "). Nothing was done. Inspect it and the files "
            "it names by hand.",
            code=2,
        )


def _check_identity(marker, resolved):
    """The invocation must be the transaction the journal recorded."""
    now = _identity(resolved)
    differ = [
        f"{key}: journal {marker.get(key)}, this invocation {now[key]}"
        for key in ("checkout", "config_dir", "data_dir", "state_dir", "install_json", "token_path")
        if marker.get(key) != now[key]
    ]
    if differ:
        raise MigrationError(
            "refusing: this invocation is not the transaction the journal recorded:\n  "
            + "\n  ".join(differ)
            + "\nRun it again from an environment whose roots and token path resolve to the "
            "recorded ones (ASSIST_CONFIG_DIR, ASSIST_DATA_DIR, ASSIST_STATE_DIR, XDG_*, "
            "ASSIST_AUTH_TOKEN_PATH, and any symlink in them). Nothing was changed."
        )


def _read_record(marker, phase):
    """The install record at the journal's path, through the runtime contract."""
    try:
        record = paths.read_install_json(marker["install_json"])
    except paths.LayoutError as exc:
        # Mid-switch the record may be absent, so removing a bad one is the repair there too.
        steps = _recovery_steps(marker, want_record=bool(_record_wanted(marker, phase)))
        raise MigrationError(f"refusing: {exc}. Nothing was changed.\n" + steps, code=1) from exc
    return record


def _record_wanted(marker, phase):
    """Whether the selected layout at `phase` has the install record; None at
    `committed` (mid-switch), where either state is accepted."""
    order, committed_at = PHASES.index(phase), PHASES.index("committed")
    if order == committed_at:
        return None
    after = order > committed_at
    return after if marker["direction"] == "forward" else not after


def _recovery_steps(marker, *, want_record=None, damaged=None):
    """Exact manual recovery, printed instead of automating another branch.

    The first step is the one that lets `--resume` pass the check that
    refused (finding 3925): the record state this phase needs, or the damaged
    destination restored. The second is how to abandon the transaction.
    """
    resume = "then: assist migrate-state --resume"
    record = marker["install_json"]
    body = json.dumps({"schema": 1, "checkout": marker["checkout"], "created": int(time.time())})
    lines = ["To recover by hand (stop any Assist process first):"]
    if damaged is not None:
        src, dst = damaged["src"], damaged["dst"]
        if os.path.lexists(src) and sha256_of(src) == damaged["sha256"]:
            lines.append(f"  - to continue: {src} still holds the journal's bytes; copy {src} over {dst} "
                         f"(replace it whole), {resume}")
        else:
            lines.append(f"  - to continue: restore {dst} from a backup whose sha256 is {damaged['sha256']} "
                         f"(the journal's hash; {src} no longer holds it), {resume}. "
                         f"Editing {record} cannot fix this.")
    elif want_record:
        lines.append(f"  - to continue: make {record} contain exactly\n      {body}\n    {resume}")
    else:
        lines.append(f"  - to continue: remove {record}, {resume}")
    old = "the checkout" if marker["direction"] == "forward" else "the XDG dirs"
    old_record = (f"remove {record} if present" if marker["direction"] == "forward"
                  else f"make {record} contain exactly {body}")
    lines.append(
        f"  - or to abandon and stay on {old}: first check that every item in the journal "
        f"{paths.migration_marker_path()} still has its `src` with its `src_sha256`. Cleanup may "
        "already have deleted some sources, and then the only complete copy is the destinations: "
        f"continue instead. Only if every source checks out: {old_record}, then remove the journal."
    )
    return "\n".join(lines)


def _check_selection(marker, phase, *, verify_destinations=False):
    """The selected-layout invariant for this journal at this phase (finding 3905).

    Before `committed` the old layout must still be the selected one; from
    `cleaning` on the new one must be; at `committed` (mid-switch) either, but
    a record present must validate and name this checkout. Before cleanup every
    destination must also still match the journal's hash. Anything else is a
    refusal with no effect and the manual recovery steps.
    """
    record = _read_record(marker, phase)
    if record is not None and _canonical(record["checkout"]) != marker["checkout"]:
        raise MigrationError(
            f"refusing: {marker['install_json']} names the checkout {record['checkout']}, not "
            f"{marker['checkout']}; there is no take-over. Nothing was changed."
        )
    want_record = _record_wanted(marker, phase)
    if want_record is not None:
        if (record is not None) != want_record:
            state = "present" if record is not None else "missing"
            raise MigrationError(
                f"refusing: {marker['install_json']} is {state}, but a {marker['direction']} migration "
                f"in phase {phase!r} needs it {'present' if want_record else 'absent'}: the selected "
                "layout does not match the journal. Nothing was changed.\n"
                + _recovery_steps(marker, want_record=want_record)
            )
    if verify_destinations:
        for item in marker["items"]:
            dst = Path(item["dst"])
            if not dst.exists() or sha256_of(dst) != item["sha256"]:
                raise MigrationError(
                    f"refusing to clean up: {dst} does not match the journal (missing or a different "
                    "hash), so the sources are still the only good copy. Nothing was deleted.\n"
                    + _recovery_steps(marker, damaged=item)
                )


def _check_overlap(checkout, config_dir, data_dir):
    problems = paths.overlap_problems(checkout, (config_dir, data_dir))
    if problems:
        raise MigrationError(
            "refusing: " + "; ".join(problems) + ". The checkout holds code only; moving state "
            "into or out of it would publish items onto their own sources and delete them. Set "
            "ASSIST_CONFIG_DIR and ASSIST_DATA_DIR (environment or ~/.config/drift-assist/config.env) "
            "to directories outside the checkout, then run this again. Nothing was changed."
        )


def _check_token_override(items):
    """Finding 3872: an explicit ASSIST_AUTH_TOKEN_PATH (exported, or set in
    the env file, which resolve() has loaded into the environment) that names
    the token file this run would move and delete. Every consumer would keep
    reading the old path, and the server would mint a NEW token there on its
    next start, signing out every device. Refuse; credentials never rotate
    as a side effect. An override pointing anywhere else is left alone."""
    explicit = os.environ.get("ASSIST_AUTH_TOKEN_PATH")
    if not explicit:
        return
    for item in items:
        if os.path.realpath(explicit) == os.path.realpath(item["src"]):
            raise MigrationError(
                f"refusing: ASSIST_AUTH_TOKEN_PATH={explicit} names {item['src']}, which this run "
                f"would move to {item['dst']} and delete. Remove the ASSIST_AUTH_TOKEN_PATH setting "
                "(environment, ~/.config/drift-assist/config.env, or the env file) so the token "
                "moves with the rest, or point it at a file outside the checkout and the config "
                "dir. Nothing was changed."
            )


def _preflight(resolved, marker):
    checkout = Path(marker["checkout"])
    config_dir, data_dir = Path(marker["config_dir"]), Path(marker["data_dir"])
    direction = marker["direction"]
    _check_overlap(checkout, config_dir, data_dir)
    _quiesce(resolved, checkout)
    items = _inventory(checkout, direction, config_dir, data_dir, marker["state_dir"])
    if not items:
        raise MigrationError("there is no state to move.")
    _check_token_override(items)
    for item in items:
        if os.path.realpath(item["src"]) == os.path.realpath(item["dst"]) and item["label"] != "receipt.orig":
            raise MigrationError(f"refusing: {item['src']} and {item['dst']} are the same file. Nothing was changed.")
    if direction == "forward":
        for item in items:
            dst = Path(item["dst"])
            if dst.exists() and item["label"] != "receipt.orig" and sha256_of(dst) != item["sha256"]:
                raise MigrationError(
                    f"{dst} already exists and differs from {item['src']} "
                    f"({sha256_of(dst)} vs {item['sha256']}). Nothing was changed; move it aside "
                    "and run this again."
                )
    for item in items:
        if not os.access(item["src"], os.R_OK):
            raise MigrationError(f"cannot read {item['src']}")
    for directory in {Path(i["dst"]).parent for i in items}:
        directory.mkdir(parents=True, exist_ok=True)
        if directory in (config_dir, data_dir, Path(marker["state_dir"])):
            os.chmod(directory, 0o700)
        if not os.access(directory, os.W_OK):
            raise MigrationError(f"cannot write {directory}")
    marker["items"] = items
    marker["phase"] = "preflight"
    _validate_journal(marker)
    _write_marker(marker)


def _copying(marker):
    checkout, data_dir = Path(marker["checkout"]), Path(marker["data_dir"])
    for item in marker["items"]:
        _remove(_part(item["dst"]))
    marker["phase"] = "copying"
    _write_marker(marker)
    for item in marker["items"]:
        part = _part(item["dst"])
        staged = _staged_bytes(item, checkout, data_dir, marker["direction"])
        if item["label"] == "receipt.orig":
            staged = Path(item["src"]).read_bytes()
        if staged is not None:
            _write_bytes(part, staged, 0o600)
        elif item["kind"] == "dir":
            _copy_tree(item["src"], part)
        else:
            _copy_file(item["src"], part)
        _fsync_dir(part.parent)
        if sha256_of(part) != item["sha256"]:
            raise MigrationError(
                f"{item['src']} changed while it was being copied (hash mismatch); "
                "stop whatever writes it, then: assist migrate-state --resume"
            )
        _point(f"copied:{item['label']}")


def _validating(marker):
    checkout, data_dir = Path(marker["checkout"]), Path(marker["data_dir"])
    marker["phase"] = "validating"
    _write_marker(marker)
    conflicts = []
    for item in marker["items"]:
        part = _part(item["dst"])
        if not part.exists() or sha256_of(part) != item["sha256"]:
            raise MigrationError(
                f"staged copy {part} is missing or its hash differs from the inventory; "
                "run: assist migrate-state --resume (it copies again)"
            )
        dst = Path(item["dst"])
        if dst.exists() and item["label"] != "receipt.orig" and sha256_of(dst) != item["sha256"]:
            conflicts.append(f"{dst}\n      present: {sha256_of(dst)}\n      restoring: {item['sha256']}"
                             f" (from {item['src']})")
    if conflicts:
        raise MigrationError(
            "conflict: these files exist and differ from what would be restored:\n    "
            + "\n    ".join(conflicts)
            + "\nNothing was replaced. Move each one aside, then run: assist migrate-state --resume"
        )
    receipt = next((i for i in marker["items"] if i["label"] == "receipt.new"), None)
    store = next((i for i in marker["items"] if i["label"] == STORE), None)
    if receipt and store:
        final_store = Path(store["dst"])
        try:
            provenance.verify_staged(_part(final_store), _part(receipt["dst"]).read_bytes(),
                                     final_store, assist_home=checkout)
        except provenance.ProvenanceError as exc:
            raise MigrationError(f"the staged provenance store does not validate ({exc.code}); "
                                 "nothing was published") from exc
    _point("validated")


def _publishing(marker):
    marker["phase"] = "publishing"
    _write_marker(marker)
    for item in marker["items"]:
        dst, part = Path(item["dst"]), _part(item["dst"])
        if part.exists():
            if sha256_of(part) != item["sha256"]:
                raise MigrationError(f"staged copy {part} no longer matches the inventory hash")
            if dst.exists() and item["kind"] == "dir":
                _remove(dst)
            os.replace(part, dst)
            _fsync_dir(dst.parent)
        elif not dst.exists() or sha256_of(dst) != item["sha256"]:
            raise MigrationError(
                f"{dst} was expected to be published, but its hash does not match the inventory "
                "and no staged copy remains. Inspect it by hand; nothing more was changed."
            )
        _point(f"published:{item['label']}")


def _committed(marker):
    checkout = Path(marker["checkout"])
    marker["phase"] = "committed"
    _write_marker(marker)
    path = Path(marker["install_json"])  # the journal's path, never the ambient one
    _check_selection(marker, "committed")
    record = _read_record(marker, "committed")
    if marker["direction"] == "forward":
        if record is None:
            paths.write_install_json(checkout, path=path)
    elif record is not None:
        path.unlink()
        _fsync_dir(path.parent)
    _point("install-json")


def _cleaning(marker):
    # The one gate before any source is deleted, in a fresh run and a resume.
    _check_selection(marker, "cleaning", verify_destinations=True)
    marker["phase"] = "cleaning"
    _write_marker(marker)
    kept = []
    for item in marker["items"]:
        if item["label"] == "receipt.orig":
            continue  # its source is the legacy receipt, cleaned as receipt.new
        src = Path(item["src"])
        if not src.exists():
            continue
        if sha256_of(src) == item["src_sha256"]:
            _remove(src)
            _fsync_dir(src.parent)
        else:
            kept.append(str(src))
        _point(f"cleaned:{item['label']}")
    if marker["direction"] == "reverse":
        orig = Path(marker["state_dir"]) / RECEIPT_ORIG
        if orig.exists():
            orig.unlink()
    marker["kept"] = kept
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pointer = Path(marker["checkout"]) / POINTER
    if marker["direction"] == "forward":
        text = (f"Drift Assist runtime state moved out of this checkout {stamp}.\n"
                f"  config: {marker['config_dir']}\n  data:   {marker['data_dir']}\n"
                "Undo with: assist migrate-state --rollback\n")
    else:
        text = f"rolled back {stamp}: Drift Assist runtime state is in this checkout again.\n"
    _write_bytes(pointer, text.encode(), 0o644)
    _point("pointer")
    marker["phase"] = "done"
    marker["finished"] = stamp
    _write_marker(marker)
    return kept


STEPS = {
    "copying": _copying,
    "validating": _validating,
    "publishing": _publishing,
    "committed": _committed,
}


def _run_from(marker, start):
    """Run the machine from phase `start` to done. Each step writes its own marker."""
    order = ["copying", "validating", "publishing", "committed"]
    for phase in order[order.index(start):] if start in order else []:
        STEPS[phase](marker)
    return _cleaning(marker)


def _resume_phase(marker):
    """Where a valid marker resumes. Before publishing, staged copies are redone."""
    phase = marker["phase"]
    if phase in ("preflight", "copying"):
        return "copying"
    if phase == "validating":
        # Re-validate; if a staged copy is gone, copying is redone first.
        if all(_part(i["dst"]).exists() for i in marker["items"]):
            return "validating"
        return "copying"
    if phase in ("publishing", "committed"):
        return phase
    return "cleaning"


def _report(marker, kept):
    moved = "Moved" if marker["direction"] == "forward" else "Restored"
    print(f"{moved} {len(marker['items'])} items.")
    if marker["direction"] == "forward":
        print(f"  config: {marker['config_dir']}\n  data:   {marker['data_dir']}")
        print("Start the server again: assist start   (then: assist doctor)")
    else:
        print(f"State is back in {marker['checkout']}. Start the server again: assist start")
    for path in kept:
        print(f"  left in place (changed since the inventory): {path}")


def command(argv, resolved):
    parser = argparse.ArgumentParser(
        prog="assist migrate-state",
        description=(
            "Move runtime state (settings, token, history, drafts, devices, provenance) out "
            "of the checkout into the XDG config and data dirs, as a resumable transaction. "
            "Stop the server and any assist scripts first."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--resume", action="store_true", help="finish an interrupted migration or rollback")
    group.add_argument("--rollback", action="store_true", help="move the state back into the checkout")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    try:
        paths.acquire_layout_lock(shared=False)
    except paths.LayoutError as exc:
        print(f"assist migrate-state: {exc}", file=sys.stderr)
        return 1
    marker = paths.migration_state()
    if marker is not None and not marker.get("valid", True):
        print(
            f"assist migrate-state: recovery required: the journal {marker.get('path')} cannot be "
            f"used ({marker.get('error')}). Nothing was done. Inspect it and the files it names by "
            "hand before changing anything.",
            file=sys.stderr,
        )
        return 2
    try:
        if marker is not None:
            # Every path from here is bound to the journal: validated whole,
            # then matched against this invocation and the install record.
            _validate_journal(marker)
        if args.resume:
            if marker is None or marker["phase"] == "done":
                print("Nothing to resume.")
                return 0
            _check_identity(marker, resolved)
            _check_selection(marker, marker["phase"])
            _check_overlap(Path(marker["checkout"]), Path(marker["config_dir"]), Path(marker["data_dir"]))
            _quiesce(resolved, Path(marker["checkout"]))
            start = _resume_phase(marker)
            kept = _cleaning(marker) if start == "cleaning" else _run_from(marker, start)
        else:
            undo_committed = (
                args.rollback and marker is not None and marker["direction"] == "forward"
                and marker["phase"] in ("committed", "cleaning")
            )
            if marker is not None and marker["phase"] != "done" and not undo_committed:
                raise MigrationError(
                    f"a {marker['direction']} migration stopped in phase {marker['phase']!r}; "
                    "finish it with: assist migrate-state --resume"
                    + ("   (or undo it: --rollback)" if marker["phase"] in ("committed", "cleaning") else "")
                )
            if args.rollback:
                if marker is not None and marker["direction"] == "forward":
                    # Undo exactly the migration the journal recorded.
                    _check_identity(marker, resolved)
                if resolved.layout != "xdg":
                    raise MigrationError(
                        "this install is not on the XDG layout (no install.json naming this checkout); "
                        "there is nothing to roll back"
                    )
                direction = "reverse"
            else:
                if resolved.layout == "refused":
                    raise MigrationError(resolved.layout_error)
                if resolved.layout == "xdg":
                    raise MigrationError(
                        f"install.json already exists at {paths.install_json_path()}; this install is "
                        "on the XDG layout. There is no take-over: to move back, use --rollback."
                    )
                direction = "forward"
            marker = _new_marker(resolved, direction)
            _validate_journal(marker)  # never write a journal this version cannot resume
            _preflight(resolved, marker)
            kept = _run_from(marker, "copying")
    except MigrationError as exc:
        print(f"assist migrate-state: {exc}", file=sys.stderr)
        return exc.code
    except provenance.ProvenanceError as exc:
        print(f"assist migrate-state: provenance: {exc}", file=sys.stderr)
        return 1
    _report(marker, kept)
    return 0
