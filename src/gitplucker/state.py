"""On-disk state: version manifest, merge base snapshots, and backups.

Lives in ``install_root/.gitplucker`` by default. The *base snapshot* is the
crucial bit for 3-way merge: after every successful apply we store a copy of
the exact files that came from the repo, so the next update can tell what the
user changed locally versus what upstream changed.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self._manifest = self._load()

    def _load(self) -> dict:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"repos": {}}

    def _save(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self._manifest, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _entry(self, repo: str, branch: str) -> dict:
        return self._manifest.setdefault("repos", {}).setdefault(repo, {}).setdefault(branch, {})

    # -- version / modules ------------------------------------------------
    def get_version(self, repo: str, branch: str) -> str | None:
        return self._entry(repo, branch).get("version")

    def set_version(self, repo: str, branch: str, version: str) -> None:
        e = self._entry(repo, branch)
        e["version"] = version
        e["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save()

    def get_known_modules(self, repo: str, branch: str) -> set[str]:
        return set(self._entry(repo, branch).get("modules", []))

    def set_known_modules(self, repo: str, branch: str, modules: set[str]) -> None:
        self._entry(repo, branch)["modules"] = sorted(modules)
        self._save()

    # -- merge base snapshot ---------------------------------------------
    def base_dir(self, repo: str, branch: str) -> Path:
        return self.dir / "base" / _slug(repo) / branch

    def read_base_file(self, repo: str, branch: str, relpath: str) -> str | None:
        p = self.base_dir(repo, branch) / relpath
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        return None

    def snapshot_base(self, repo: str, branch: str, payload_root: Path,
                      relpaths: list[str], *, replace: bool = True) -> None:
        """Record the common ancestor the next 3-way merge will diff against.

        ``replace=True`` (a FULL apply) rebuilds the whole baseline from the
        payload. ``replace=False`` (a PARTIAL apply) merges just ``relpaths``
        into the existing baseline, leaving the entries for unselected files
        alone — they were not updated on disk either, so their old ancestor is
        still the correct one.

        Partial applies used to skip this entirely. The applied files then kept
        a stale ancestor forever, so every later check saw them as "locally
        modified" and 3-way merged them against content that was already
        superseded — which surfaced as conflicts in files the user had never
        edited. See test_partial_apply_baseline.py.
        """
        base = self.base_dir(repo, branch)
        if replace and base.exists():
            shutil.rmtree(base, ignore_errors=True)
        for rel in relpaths:
            src = Path(payload_root) / rel
            dst = base / rel
            if not src.is_file():
                # Upstream no longer ships this path and the user applied that
                # deletion: drop it from the baseline too, or it stays the
                # ancestor of a file that no longer exists.
                if not replace and dst.is_file():
                    dst.unlink()
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # -- backups / rollback snapshots -------------------------------------
    def new_backup_dir(self, repo: str, branch: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        d = self.dir / "backups" / _slug(repo) / f"{branch}-{stamp}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_manifest(self, backup_dir: Path, data: dict) -> None:
        """Store the rollback manifest for a snapshot (what to restore/delete)."""
        (Path(backup_dir) / "manifest.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")

    def read_manifest(self, backup_dir: Path) -> dict | None:
        p = Path(backup_dir) / "manifest.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def list_backups(self, repo: str, branch: str) -> list[Path]:
        """All snapshot dirs for repo/branch, newest first (full traceback history)."""
        base = self.dir / "backups" / _slug(repo)
        if not base.exists():
            return []
        dirs = [d for d in base.iterdir()
                if d.is_dir() and d.name.startswith(f"{branch}-")]
        return sorted(dirs, key=lambda d: d.name, reverse=True)
