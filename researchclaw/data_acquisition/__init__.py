"""Resumable, hash-verified acquisition of real public datasets (local adaptation)."""

from pathlib import Path
from typing import Any, Callable

DATASETS: dict[str, str] = {"insar-landslide": "researchclaw.data_acquisition.insar_landslide"}


def ensure(spec: str, *, emit: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Run (or resume) acquisition ``"<dataset>:<root>"`` to completion and verify every hash.

    Raises RuntimeError when the data is incomplete or a file fails verification;
    planning must not start on partial data.
    """
    import importlib

    from researchclaw.data_acquisition.manifest import Manifest

    name, _, root = spec.partition(":")
    if name not in DATASETS or not root:
        raise RuntimeError(f"未知的数据获取配置：{spec}")
    module = importlib.import_module(DATASETS[name])
    if emit:
        emit("data", f"获取真实数据：{name}", f"数据根 {root}；断点续传，HyP3 处理期间不调用模型")
    result = module.run(Path(root))
    counts = result.get("counts", {})
    failed = counts.get("FAILED", 0)
    verify = Manifest(Path(root)).verify()
    if not verify["ok"]:
        raise RuntimeError(f"数据校验失败：缺失 {len(verify['missing'])}，哈希不符 {len(verify['mismatched'])}")
    result.update(verify=verify, failed_jobs=failed)
    if emit:
        emit("data", "真实数据已就绪并通过哈希校验",
             f"{verify['files']} 个文件；HyP3 状态 {counts}（失败的处理任务如实记录，不以替代数据补齐）")
    return result
