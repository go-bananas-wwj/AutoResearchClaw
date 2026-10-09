"""Overleaf Git-based bidirectional sync engine."""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from researchclaw.overleaf.conflict import ConflictResolver

logger = logging.getLogger(__name__)


class OverleafSync:
    """Bidirectional sync between pipeline output and Overleaf via Git."""

    def __init__(
        self,
        git_url: str,
        branch: str = "main",
        auto_push: bool = True,
        auto_pull: bool = False,
    ) -> None:
        self.git_url = git_url
        self.branch = branch
        self.auto_push = auto_push
        self.auto_pull = auto_pull
        self.local_dir: Path | None = None
        self._last_sync: datetime | None = None
        self._conflict_resolver = ConflictResolver()

    def setup(self, run_dir: Path, local_dir: Path | None = None) -> Path:
        """Clone or update the Overleaf repo into the run directory."""
        self.local_dir = (
            local_dir if local_dir is not None else run_dir / "overleaf_repo"
        )
        if self.local_dir.exists() and (self.local_dir / ".git").exists():
            logger.info("Pulling latest from Overleaf...")
            self._git("pull", "origin", self.branch)
        else:
            logger.info("Cloning Overleaf repo: %s", self.git_url)
            self.local_dir.mkdir(parents=True, exist_ok=True)
            self._git_clone()
        # 容器/最小环境里 git 无全局身份，提交前必须补本地身份
        self._git("config", "user.email", "researchclaw@local")
        self._git("config", "user.name", "ResearchClaw")
        return self.local_dir

    def _auth_git_url(self) -> str:
        """为 git.overleaf.com 裸 URL 注入 OVERLEAF_TOKEN（token 不落配置文件）。"""
        import os

        if "git.overleaf.com" in self.git_url and "@" not in self.git_url:
            token = os.environ.get("OVERLEAF_TOKEN", "")
            if token:
                return self.git_url.replace("https://", f"https://git:{token}@", 1)
        return self.git_url

    def push_paper(
        self,
        paper_tex: Path,
        bib_file: Path | None = None,
        figures_dir: Path | None = None,
        subdir: str = "",
    ) -> bool:
        """Push pipeline-generated paper to Overleaf.

        Copies .tex, .bib, and figures into the local clone (under *subdir*
        when given, e.g. ``runs/<run_id>/``), then commits and pushes.
        """
        if not self.local_dir:
            raise RuntimeError("Call setup() before push_paper()")

        base = self.local_dir / subdir if subdir else self.local_dir
        base.mkdir(parents=True, exist_ok=True)

        # Copy main tex file
        dst_tex = base / paper_tex.name
        shutil.copy2(paper_tex, dst_tex)
        logger.info("Copied %s -> %s", paper_tex, dst_tex)

        # Copy bib file
        if bib_file and bib_file.exists():
            dst_bib = base / bib_file.name
            shutil.copy2(bib_file, dst_bib)

        # Copy figures（保持源目录名：stage-22 的 tex 里引用的是 charts/xxx.png，
        # 若强行改名 figures/ 会导致 Overleaf 上图全断）
        if figures_dir and figures_dir.is_dir():
            dst_figs = base / figures_dir.name
            if dst_figs.exists():
                shutil.rmtree(dst_figs)
            shutil.copytree(figures_dir, dst_figs)

        # Git add, commit, push（共享仓库模式下只暂存本 run 的子目录）
        self._git("add", subdir if subdir else "-A")
        status = self._git("status", "--porcelain")
        if not status.strip():
            logger.info("No changes to push")
            return False

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._git("commit", "-m", f"AutoResearchClaw sync{' ' + subdir if subdir else ''}: {ts}")
        self._git("push", "origin", self.branch)
        self._last_sync = datetime.now(timezone.utc)
        logger.info("Pushed paper to Overleaf")
        return True

    def pull_changes(self) -> list[str]:
        """Pull human edits from Overleaf and return changed file names."""
        if not self.local_dir:
            raise RuntimeError("Call setup() before pull_changes()")

        # Record current HEAD
        old_head = self._git("rev-parse", "HEAD").strip()
        self._git("pull", "origin", self.branch)
        new_head = self._git("rev-parse", "HEAD").strip()

        if old_head == new_head:
            return []

        # Get list of changed files
        diff_output = self._git("diff", "--name-only", old_head, new_head)
        changed = [f.strip() for f in diff_output.splitlines() if f.strip()]
        self._last_sync = datetime.now(timezone.utc)
        return changed

    def get_status(self) -> dict[str, Any]:
        """Return sync status information."""
        status: dict[str, Any] = {
            "git_url": self.git_url,
            "branch": self.branch,
            "local_dir": str(self.local_dir) if self.local_dir else None,
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "auto_push": self.auto_push,
            "auto_pull": self.auto_pull,
        }
        if self.local_dir and (self.local_dir / ".git").exists():
            pending = self._git("status", "--porcelain").strip()
            status["pending_changes"] = len(pending.splitlines()) if pending else 0
        return status

    def resolve_conflicts(self, strategy: str = "ours") -> list[str]:
        """Resolve merge conflicts using the given strategy."""
        if not self.local_dir:
            raise RuntimeError("Call setup() before resolve_conflicts()")
        return self._conflict_resolver.resolve(self.local_dir, strategy)

    # ── git helpers ───────────────────────────────────────────────

    def _git(self, *args: str) -> str:
        """Run a git command in the local repo directory."""
        cmd = ["git", "-C", str(self.local_dir), *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 and "conflict" not in result.stderr.lower():
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def _git_clone(self) -> None:
        """Clone the Overleaf repo."""
        result = subprocess.run(
            ["git", "clone", "-b", self.branch, self._auth_git_url(), str(self.local_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
