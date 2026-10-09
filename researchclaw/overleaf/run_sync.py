"""Per-run Overleaf sync —— 共享克隆 + 每 run 一个 runs/<run_id>/ 文件夹。

用户在同一个 Overleaf 项目里收纳全部自动科研产出：
  <overleaf-papers>/runs/rc-xxx/paper.tex + references.bib + figures/
同步全部通过 Git（Overleaf Git 集成），token 走环境变量 OVERLEAF_TOKEN。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from researchclaw.overleaf.sync import OverleafSync

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_DIR = REPO_ROOT / ".overleaf" / "papers"  # 共享克隆（已加入 .gitignore）


def _find_deliverables(run_dir: Path):
    """定位 run 的论文产物（tex/bib/figures），返回 (tex, bib, figures_dir)。"""
    base = run_dir / "deliverables"
    if not base.is_dir():
        base = run_dir
    tex = base / "paper.tex"
    if not tex.exists():
        cands = sorted(base.glob("*.tex"))
        tex = cands[0] if cands else None
    bib = base / "references.bib"
    figs = None
    for cand in (base / "charts", base / "figures"):
        if cand.is_dir():
            figs = cand
            break
    return (tex if tex and tex.exists() else None), (bib if bib.exists() else None), figs


def _paper_language(config: Any) -> str:
    """读 export.paper_language（zh/en），缺省 zh（本 fork 中文先行）。"""
    export_cfg = getattr(config, "export", None)
    return getattr(export_cfg, "paper_language", "zh") if export_cfg else "zh"


def sync_run_to_overleaf(
    run_dir: Path,
    run_id: str,
    config: Any,
    language: str | None = None,
) -> dict[str, Any]:
    """把指定 run 的论文产物同步到 Overleaf 共享仓库。

    按语言推送到 ``runs/<run_id>/<language>/``（zh 中文稿 / en 英文定稿），
    无 language 时维持旧行为推 ``runs/<run_id>/``。
    """
    cfg = getattr(config, "overleaf", None)
    if not cfg or not getattr(cfg, "enabled", False) or not getattr(cfg, "git_url", ""):
        return {"ok": False, "reason": "overleaf sync not enabled in config"}

    tex, bib, figs = _find_deliverables(run_dir)
    if tex is None:
        return {"ok": False, "reason": f"no .tex found under {run_dir}"}

    lang = language if language is not None else _paper_language(config)
    subdir = f"runs/{run_id}/{lang}" if lang else f"runs/{run_id}"

    SHARED_DIR.parent.mkdir(parents=True, exist_ok=True)
    sync = OverleafSync(git_url=cfg.git_url, branch=cfg.branch, auto_push=True)
    sync.setup(run_dir, local_dir=SHARED_DIR)
    pushed = sync.push_paper(tex, bib_file=bib, figures_dir=figs, subdir=subdir)
    return {"ok": True, "run_id": run_id, "folder": subdir, "pushed": pushed}


def pull_run_from_overleaf(
    run_dir: Path,
    run_id: str,
    config: Any,
    language: str = "zh",
) -> dict[str, Any]:
    """从 Overleaf 拉回用户批注/修改。

    pull 共享克隆后，把 ``runs/<run_id>/<language>/`` 下变更过的文件拷回
    ``run_dir/paper_annotations/<language>/``，返回变更文件列表。
    """
    cfg = getattr(config, "overleaf", None)
    if not cfg or not getattr(cfg, "enabled", False) or not getattr(cfg, "git_url", ""):
        return {"ok": False, "reason": "overleaf sync not enabled in config"}

    SHARED_DIR.parent.mkdir(parents=True, exist_ok=True)
    sync = OverleafSync(git_url=cfg.git_url, branch=cfg.branch, auto_push=False)
    sync.setup(run_dir, local_dir=SHARED_DIR)
    changed = sync.pull_changes()

    prefix = f"runs/{run_id}/{language}/"
    dest_dir = run_dir / "paper_annotations" / language
    copied: list[str] = []
    for rel in changed:
        if not rel.startswith(prefix):
            continue
        src = SHARED_DIR / rel
        if not src.is_file():
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / rel[len(prefix):]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(dst.name)
    return {
        "ok": True,
        "run_id": run_id,
        "language": language,
        "changed_remote": changed,
        "copied": copied,
        "pulled_to": str(dest_dir),
    }


def maybe_sync_run(run_dir: Path, run_id: str, config: Any) -> None:
    """Pipeline 收尾钩子：尽力同步，绝不阻断流水线。"""
    try:
        result = sync_run_to_overleaf(run_dir, run_id, config)
        if result.get("ok"):
            logger.info("Overleaf synced: %s", result["folder"])
    except Exception:
        logger.warning("Overleaf sync failed (non-blocking)", exc_info=True)
