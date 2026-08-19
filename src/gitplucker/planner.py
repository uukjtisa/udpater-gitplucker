"""Dry-run diff + merge planning.

Given the freshly-downloaded payload, the currently-installed tree, and the
stored merge base, compute the list of :class:`FileChange` (for display) and the
concrete :class:`FileOp` list (for the apply strategy to execute). No files are
written here — this is what makes :meth:`Updater.check` a safe preview.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ConflictPolicy, RepoSubscription, UpdaterConfig
from .errors import MergeConflictError
from .fsutil import is_text_file, list_files, sha256_bytes, sha256_file
from .merge import merge_text
from .models import Channel, ChangeType, FileChange
from .state import StateStore


@dataclass
class FileOp:
    relpath: str
    kind: str            # "copy" (from payload) | "write" (text) | "delete"
    src: Path | None = None
    text: str | None = None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def build_file_plan(
    cfg: UpdaterConfig,
    sub: RepoSubscription,
    branch: str,
    payload_root: Path,
    state: StateStore,
) -> tuple[list[FileChange], list[FileOp], list[str]]:
    changes: list[FileChange] = []
    ops: list[FileOp] = []
    warnings: list[str] = []

    payload_root = Path(payload_root)
    install_root = cfg.install_root
    payload_files = list_files(payload_root, sub.include_globs, sub.exclude_globs)
    payload_set = set(payload_files)

    for rel in payload_files:
        remote_path = payload_root / rel
        install_path = install_root / rel
        remote_hash = sha256_file(remote_path)
        remote_is_text = is_text_file(remote_path)

        if not install_path.exists():
            changes.append(FileChange(rel, ChangeType.ADDED, remote_hash=remote_hash,
                                      is_text=remote_is_text))
            ops.append(FileOp(rel, "copy", src=remote_path))
            continue

        local_hash = sha256_file(install_path)
        if local_hash == remote_hash:
            changes.append(FileChange(rel, ChangeType.UNCHANGED,
                                      remote_hash=remote_hash, local_hash=local_hash,
                                      is_text=remote_is_text))
            continue

        change_is_text = remote_is_text and is_text_file(install_path)

        # Bytes differ but the decoded text is identical after newline
        # normalization (e.g. CRLF vs LF, or a BOM) -> not a real textual change.
        # Skip it so it never shows up as a change or gets auto-selected.
        if change_is_text and _read_text(install_path) == _read_text(remote_path):
            changes.append(FileChange(rel, ChangeType.UNCHANGED, remote_hash=remote_hash,
                                      local_hash=local_hash, is_text=True))
            continue

        base_text = state.read_base_file(sub.repo, branch, rel)
        locally_modified = False
        if base_text is not None:
            if change_is_text:
                # Compare DECODED, newline-normalized text -- never raw bytes.
                # local_hash hashes the file as it sits on disk, but Windows
                # writes text files with os.linesep, so an installed file is
                # CRLF while the baseline copied straight from the payload is
                # LF. Hashing those against each other marks EVERY file on
                # Windows as locally modified, which forces every update
                # through the 3-way merge path and (against a stale ancestor)
                # manufactures conflicts in files nobody ever edited.
                # _read_text and read_base_file both apply universal newlines,
                # so this comparison is line-ending agnostic by construction.
                locally_modified = _read_text(install_path) != base_text
            else:
                base_hash = sha256_bytes(base_text.encode("utf-8", "replace"))
                locally_modified = base_hash != local_hash

        can_merge = (
            sub.channel is Channel.PYTHON_SOURCE
            and cfg.merge
            and base_text is not None
            and locally_modified
            and is_text_file(remote_path)
            and is_text_file(install_path)
        )

        if can_merge:
            res = merge_text(
                base_text, _read_text(install_path), _read_text(remote_path),
                local_label=f"{rel} (local)", remote_label=f"{sub.repo}@{branch}",
            )
            if res.clean:
                changes.append(FileChange(rel, ChangeType.MERGED, remote_hash=remote_hash,
                                          local_hash=local_hash, locally_modified=True,
                                          note="local edits preserved", is_text=True))
                ops.append(FileOp(rel, "write", text=res.text))
            else:
                fc = FileChange(rel, ChangeType.CONFLICT, remote_hash=remote_hash,
                                local_hash=local_hash, locally_modified=True,
                                conflict_lines=res.conflict_lines, is_text=True)
                if cfg.conflict_policy == ConflictPolicy.ABORT:
                    raise MergeConflictError(f"conflict in {rel} ({res.conflicts} hunks)")
                elif cfg.conflict_policy == ConflictPolicy.LOCAL:
                    fc.note = "conflict; kept local"
                    # no op: local file stays as-is
                elif cfg.conflict_policy == ConflictPolicy.REMOTE:
                    fc.note = "conflict; took remote"
                    ops.append(FileOp(rel, "copy", src=remote_path))
                else:  # MARK
                    fc.note = "conflict; wrote markers"
                    ops.append(FileOp(rel, "write", text=res.text))
                changes.append(fc)
        else:
            note = ""
            if locally_modified:
                note = "local changes overwritten (no merge available)"
                warnings.append(f"{rel}: {note}")
            changes.append(FileChange(rel, ChangeType.MODIFIED, remote_hash=remote_hash,
                                      local_hash=local_hash, locally_modified=locally_modified,
                                      note=note, is_text=change_is_text))
            ops.append(FileOp(rel, "copy", src=remote_path))

    # Deletions: files that came from the repo before but are gone upstream now.
    base_root = state.base_dir(sub.repo, branch)
    if base_root.exists():
        base_files = list_files(base_root, sub.include_globs, sub.exclude_globs)
        for rel in base_files:
            if rel in payload_set:
                continue
            install_path = install_root / rel
            if not install_path.exists():
                continue
            # Only remove if the user hasn't diverged from the tracked version.
            base_hash = sha256_file(base_root / rel)
            if sha256_file(install_path) == base_hash:
                changes.append(FileChange(rel, ChangeType.DELETED, local_hash=base_hash,
                                          is_text=is_text_file(install_path)))
                ops.append(FileOp(rel, "delete"))
            else:
                warnings.append(f"{rel}: removed upstream but modified locally; kept")

    return changes, ops, warnings
