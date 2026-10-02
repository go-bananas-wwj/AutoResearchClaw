"""Per-run Overleaf sync —— 共享克隆 + 每 run 一个 runs/<run_id>/ 文件夹。

用户在同一个 Overleaf 项目里收纳全部自动科研产出：
  <overleaf-papers>/runs/rc-xxx/paper.tex + references.bib + figures/
同步全部通过 Git（Overleaf Git 集成），token 走环境变量 OVERLEAF_TOKEN。
"""

from __future__ import annotations

import logging
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


def sync_run_to_overleaf(run_dir: Path, run_id: str, config: Any) -> dict[str, Any]:
    """把指定 run 的论文产物同步到 Overleaf 共享仓库的 runs/<run_id>/ 下。"""
    cfg = getattr(config, "overleaf", None)
    if not cfg or not getattr(cfg, "enabled", False) or not getattr(cfg, "git_url", ""):
        return {"ok": False, "reason": "overleaf sync not enabled in config"}

    tex, bib, figs = _find_deliverables(run_dir)
    if tex is None:
        return {"ok": False, "reason": f"no .tex found under {run_dir}"}

    SHARED_DIR.parent.mkdir(parents=True, exist_ok=True)
    sync = OverleafSync(git_url=cfg.git_url, branch=cfg.branch, auto_push=True)
    sync.setup(run_dir, local_dir=SHARED_DIR)
    pushed = sync.push_paper(tex, bib_file=bib, figures_dir=figs, subdir=f"runs/{run_id}")
    return {"ok": True, "run_id": run_id, "folder": f"runs/{run_id}", "pushed": pushed}


def maybe_sync_run(run_dir: Path, run_id: str, config: Any) -> None:
    """Pipeline 收尾钩子：尽力同步，绝不阻断流水线。"""
    try:
        result = sync_run_to_overleaf(run_dir, run_id, config)
        if result.get("ok"):
            logger.info("Overleaf synced: %s", result["folder"])
    except Exception:
        logger.warning("Overleaf sync failed (non-blocking)", exc_info=True)
