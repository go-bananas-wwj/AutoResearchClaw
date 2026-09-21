"""Settings router — 浏览器内查看/编辑配置文件与环境状态。

安全边界：仅允许读写仓库根目录下 config*.yaml 白名单文件；写入前做 YAML
解析校验并自动备份（*.bak.<timestamp>）。本工具定位为单人本地使用。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/settings", tags=["settings"])

REPO_ROOT = Path(__file__).resolve().parents[3]

# 检查哪些 key 已就绪（只回布尔，绝不回传值）
ENV_KEYS = ["DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "S2_API_KEY", "TAVILY_API_KEY"]


def _config_files() -> list[Path]:
    return sorted(p for p in REPO_ROOT.glob("config*.yaml"))


def _safe_path(name: str) -> Path:
    p = (REPO_ROOT / name).resolve()
    if p.parent != REPO_ROOT or not p.name.startswith("config") or not p.name.endswith(".yaml"):
        raise HTTPException(status_code=400, detail="invalid config file name")
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {name}")
    return p


class SaveRequest(BaseModel):
    name: str
    content: str


@router.get("/files")
async def list_files() -> dict[str, Any]:
    files = []
    for p in _config_files():
        files.append({
            "name": p.name,
            "size": p.stat().st_size,
            "mtime": int(p.stat().st_mtime),
            "is_symlink": p.is_symlink(),
        })
    return {"files": files, "default": "config.arc.yaml" if (REPO_ROOT / "config.arc.yaml").is_file() else ""}


@router.get("/file")
async def read_file(name: str) -> dict[str, Any]:
    p = _safe_path(name)
    # 软链目标一并返回，明确"编辑将写到哪"
    target = str(p.resolve().relative_to(REPO_ROOT)) if p.is_symlink() else p.name
    return {"name": name, "resolved": target, "content": p.resolve().read_text(encoding="utf-8")}


@router.put("/file")
async def save_file(req: SaveRequest) -> dict[str, Any]:
    p = _safe_path(req.name)
    try:
        parsed = yaml.safe_load(req.content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"YAML 解析失败: {e}") from e
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="配置根节点必须是 mapping")
    real = p.resolve()
    backup = real.with_suffix(real.suffix + f".bak.{int(time.time())}")
    backup.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    real.write_text(req.content, encoding="utf-8")
    return {"ok": True, "wrote": str(real.relative_to(REPO_ROOT)), "backup": backup.name}


@router.get("/env")
async def env_status() -> dict[str, Any]:
    import os
    return {"keys": {k: bool(os.environ.get(k)) for k in ENV_KEYS}}
