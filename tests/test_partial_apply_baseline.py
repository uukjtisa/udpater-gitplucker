"""The merge baseline after a PARTIAL apply.

Reported symptom: "there are no modifications from me but it still says
conflict". Mechanism: a partial apply wrote upstream's new content to disk but
never refreshed the merge baseline for those files, so their common ancestor
stayed at the OLD content. On the next check the planner compared local (new,
from the apply) against base (old) and concluded the user had edited the file.
That forced a 3-way merge against a superseded ancestor, and anywhere the next
commit touched the same lines the previous one did, BOTH sides read as changed
-> a conflict in a file nobody had ever hand-edited. It compounded with every
partial apply, which is why it looked intermittent.

Selective applies are the normal path in the host app (files are individually
tickable and protected ones start unticked), so this was hit constantly.
"""
from gitplucker.planner import build_file_plan, FileOp
from gitplucker.models import ChangeType, Channel as Ch, UpdatePlan
from gitplucker import Channel, RepoSubscription, UpdaterConfig
from gitplucker.events import EventEmitter
from gitplucker.state import StateStore
from gitplucker.strategies import get_strategy


def _cfg(tmp_path, install):
    return UpdaterConfig(
        install_root=install,
        allowed_repos=["me/app"],
        subscriptions=[RepoSubscription("me/app", channel=Channel.PYTHON_SOURCE)],
        state_dir=tmp_path / "state",
        backup=False,
    )


def _apply(cfg, state, payload, selected, rels):
    plan = UpdatePlan("me/app", "main", Ch.PYTHON_SOURCE, None, "v2", has_update=True)
    plan._ops = [FileOp(r, "copy", src=payload / r) for r in rels]
    plan._selected = set(selected) if selected is not None else None
    plan._payload_root = payload
    plan._subscription = cfg.subscriptions[0]
    plan._is_package = False
    res = get_strategy("whole_app").apply(cfg, plan, EventEmitter(), state)
    assert res.success
    return plan


def test_partial_apply_refreshes_baseline_for_applied_files(tmp_path):
    install = tmp_path / "install"; install.mkdir()
    payload = tmp_path / "payload"; payload.mkdir()
    cfg = _cfg(tmp_path, install)
    state = StateStore(cfg.state_dir)

    # v1 is installed and is the baseline.
    for name, body in (("a.py", "v1-a\n"), ("b.py", "v1-b\n")):
        (install / name).write_text(body, encoding="utf-8")
        (payload / name).write_text(body, encoding="utf-8")
    state.snapshot_base("me/app", "main", payload, ["a.py", "b.py"])

    # v2 arrives; the user applies ONLY a.py.
    (payload / "a.py").write_text("v2-a\n", encoding="utf-8")
    (payload / "b.py").write_text("v2-b\n", encoding="utf-8")
    plan = _apply(cfg, state, payload, {"a.py"}, ["a.py", "b.py"])
    state.snapshot_base("me/app", "main", payload, sorted(plan._selected), replace=False)

    # The applied file's ancestor moved forward...
    assert state.read_base_file("me/app", "main", "a.py") == "v2-a\n"
    # ...and the DESELECTED file's ancestor did not (it was not updated on disk).
    assert state.read_base_file("me/app", "main", "b.py") == "v1-b\n"


def test_applied_file_is_not_reported_as_locally_modified(tmp_path):
    """The end-to-end symptom: after a partial apply, the file the user applied
    must not come back as a local edit / conflict on the next check."""
    install = tmp_path / "install"; install.mkdir()
    payload = tmp_path / "payload"; payload.mkdir()
    cfg = _cfg(tmp_path, install)
    state = StateStore(cfg.state_dir)

    (install / "a.py").write_text("v1\n", encoding="utf-8")
    (payload / "a.py").write_text("v1\n", encoding="utf-8")
    state.snapshot_base("me/app", "main", payload, ["a.py"])

    # v2: apply a.py selectively, then refresh its baseline (the fix).
    (payload / "a.py").write_text("v2\n", encoding="utf-8")
    plan = _apply(cfg, state, payload, {"a.py"}, ["a.py"])
    state.snapshot_base("me/app", "main", payload, sorted(plan._selected), replace=False)

    # v3 lands upstream, touching the same line the v2 commit did.
    (payload / "a.py").write_text("v3\n", encoding="utf-8")
    changes, _ops, _warn = build_file_plan(cfg, cfg.subscriptions[0], "main", payload, state)
    a = next(c for c in changes if c.path == "a.py")
    assert not a.locally_modified, "applied file must not read as a user edit"
    assert a.change is not ChangeType.CONFLICT


def test_stale_baseline_is_what_produced_the_phantom_conflict(tmp_path):
    """Pins the mechanism itself: with the baseline left stale (the old
    behaviour), the very same sequence DOES produce a phantom local edit."""
    install = tmp_path / "install"; install.mkdir()
    payload = tmp_path / "payload"; payload.mkdir()
    cfg = _cfg(tmp_path, install)
    state = StateStore(cfg.state_dir)

    (install / "a.py").write_text("v1\n", encoding="utf-8")
    (payload / "a.py").write_text("v1\n", encoding="utf-8")
    state.snapshot_base("me/app", "main", payload, ["a.py"])

    (payload / "a.py").write_text("v2\n", encoding="utf-8")
    _apply(cfg, state, payload, {"a.py"}, ["a.py"])
    # deliberately DO NOT refresh the baseline — the pre-fix behaviour

    (payload / "a.py").write_text("v3\n", encoding="utf-8")
    changes, _ops, _warn = build_file_plan(cfg, cfg.subscriptions[0], "main", payload, state)
    a = next(c for c in changes if c.path == "a.py")
    assert a.locally_modified, "stale baseline should fabricate a local edit"


def test_partial_apply_drops_baseline_entry_for_an_applied_deletion(tmp_path):
    install = tmp_path / "install"; install.mkdir()
    payload = tmp_path / "payload"; payload.mkdir()
    cfg = _cfg(tmp_path, install)
    state = StateStore(cfg.state_dir)

    (payload / "gone.py").write_text("bye\n", encoding="utf-8")
    state.snapshot_base("me/app", "main", payload, ["gone.py"])
    assert state.read_base_file("me/app", "main", "gone.py") == "bye\n"

    (payload / "gone.py").unlink()          # upstream deleted it
    state.snapshot_base("me/app", "main", payload, ["gone.py"], replace=False)
    assert state.read_base_file("me/app", "main", "gone.py") is None


def test_full_apply_still_rebuilds_the_whole_baseline(tmp_path):
    install = tmp_path / "install"; install.mkdir()
    payload = tmp_path / "payload"; payload.mkdir()
    cfg = _cfg(tmp_path, install)
    state = StateStore(cfg.state_dir)

    (payload / "old.py").write_text("old\n", encoding="utf-8")
    state.snapshot_base("me/app", "main", payload, ["old.py"])
    (payload / "old.py").unlink()
    (payload / "new.py").write_text("new\n", encoding="utf-8")
    state.snapshot_base("me/app", "main", payload, ["new.py"], replace=True)

    assert state.read_base_file("me/app", "main", "new.py") == "new\n"
    assert state.read_base_file("me/app", "main", "old.py") is None


# ── the Windows line-ending half of the same bug ────────────────────────────
# Path.write_text() translates "\n" to os.linesep, so a file the updater installs
# lands on disk as CRLF, while snapshot_base copies the payload byte-for-byte and
# keeps the LF it came with. Comparing a RAW BYTE hash of the former against the
# DECODED text of the latter marks every text file on Windows as locally
# modified -- on every check, forever, with no user edit anywhere.

def test_crlf_on_disk_is_not_a_local_edit(tmp_path):
    install = tmp_path / "install"; install.mkdir()
    payload = tmp_path / "payload"; payload.mkdir()
    cfg = _cfg(tmp_path, install)
    state = StateStore(cfg.state_dir)

    # baseline as downloaded (LF), installed copy as Windows writes it (CRLF)
    (payload / "a.py").write_bytes(b"import os\nimport sys\n")
    state.snapshot_base("me/app", "main", payload, ["a.py"])
    (install / "a.py").write_bytes(b"import os\r\nimport sys\r\n")

    (payload / "a.py").write_bytes(b"import os\nimport json\n")   # upstream change
    changes, _ops, _warn = build_file_plan(cfg, cfg.subscriptions[0], "main", payload, state)
    a = next(c for c in changes if c.path == "a.py")
    assert not a.locally_modified, "line endings are not authorship"
    assert a.change is not ChangeType.CONFLICT


def test_a_genuine_edit_is_still_detected_on_a_crlf_working_copy(tmp_path):
    """The fix must not blind the planner to real local edits."""
    install = tmp_path / "install"; install.mkdir()
    payload = tmp_path / "payload"; payload.mkdir()
    cfg = _cfg(tmp_path, install)
    state = StateStore(cfg.state_dir)

    (payload / "a.py").write_bytes(b"import os\nimport sys\n")
    state.snapshot_base("me/app", "main", payload, ["a.py"])
    (install / "a.py").write_bytes(b"import os\r\nimport sys\r\nMY_EDIT = 1\r\n")

    (payload / "a.py").write_bytes(b"import os\nimport json\n")
    changes, _ops, _warn = build_file_plan(cfg, cfg.subscriptions[0], "main", payload, state)
    a = next(c for c in changes if c.path == "a.py")
    assert a.locally_modified, "a real local edit must still be seen"


def test_known_limitation_binary_baselines_are_compared_lossily(tmp_path):
    """KNOWN GAP, pinned so it is documented rather than rediscovered.

    StateStore.read_base_file reads every baseline with Path.read_text(), which
    applies universal newlines AND utf-8 errors="replace". For a NON-text file
    that is lossy in both directions, so the byte-hash comparison in the planner
    is really comparing mangled text -- a binary differing only in CR bytes reads
    as unmodified here.

    Impact is small and unrelated to the conflict bug this file covers: binaries
    have no merge path (is_text_file gates it), so they are overwritten wholesale
    either way. Fixing it properly means storing baselines as BYTES, which
    changes the on-disk state format -- deliberately out of scope for 0.7.1.
    """
    install = tmp_path / "install"; install.mkdir()
    payload = tmp_path / "payload"; payload.mkdir()
    cfg = _cfg(tmp_path, install)
    state = StateStore(cfg.state_dir)

    NUL, CRB, LFB = bytes([0]), bytes([13]), bytes([10])
    (payload / "blob.bin").write_bytes(NUL + b"head" + CRB + LFB + b"tail")
    state.snapshot_base("me/app", "main", payload, ["blob.bin"])
    (install / "blob.bin").write_bytes(NUL + b"head" + LFB + b"tail")

    (payload / "blob.bin").write_bytes(NUL + b"head" + CRB + LFB + b"CHANGED")
    changes, _ops, _warn = build_file_plan(cfg, cfg.subscriptions[0], "main", payload, state)
    blob = next(c for c in changes if c.path == "blob.bin")
    assert not blob.is_text
    # Documents the gap: the CR difference is invisible through the text decode.
    assert not blob.locally_modified
