"""实验准备：阶段 9（实验设计、数据获取）与阶段 10（实现方法）之间的独立环节。

阶段 10 的代码此前要自己摸索原始数据清单格式、文件类型和路径规则，又要证明读了真实文件，
真实运行中由此产生了一整类失败（docs/E2E_RUN_ISSUES_CN.md #5、#10）。本环节把
"数据从原始文件变成实验可直接读取的数组"拆出来，作为一个可验收、可复用的产物：

1. 复核原始数据清单（宿主重算 SHA-256）。
2. 模型（经补丁协议）编写 ``prepare.py``：读取原始文件，按研究对象裁剪/堆叠/对齐，
   把计划中每个数据集写成 ``prepared/`` 下的 .npy/.npz，并写 ``prepared/index.json``
   说明每个文件属于哪个数据集、含义、单位和来源清单条目；计划中的合成数据也在这里生成并落盘。
3. 在实验沙箱中实际运行 ``prepare.py``（原始数据只读挂载）。
4. 宿主加载每个输出文件，核对来源条目确实在原始清单中，生成 ``dataset_card.json``
   （形状、dtype、有限值比例、取值范围、SHA-256），并把准备好的文件登记进
   ``<data_base>/runs/<run_id>/prepared/data_manifest.json``。失败原因写回模型修复，
   按与阶段 10 相同的进展式预算重试。

产物位置：工作区、沙箱暂存与记录在 ``run_dir/prep/``（不是阶段目录，恢复时不被归档）；
准备好的数据在 ``<data_base>/runs/<run_id>/prepared/``，与原始数据一样只读挂入实验沙箱。
输入（规格、计划、原始清单）的哈希不变时直接复用，不重复调用模型或重跑。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from researchclaw.pipeline.activity import emit

logger = logging.getLogger(__name__)

PREP_VERSION = 3  # 3：拒绝有效掩码全为 False 的数组（真实运行中 bperp 未解析却照样输出）； 2：按研究对象坐标提取 + 跨对象数组去重（v1 产物在真实运行中两个对象数组相同）
PREP_DIR = "prep"
SCRIPT = "prepare.py"
OK_MARK = "RC_PREP_OK"

INSTRUCTION = """编写 prepare.py（本步骤是"实验准备"，只准备数据，不实现方法、不计算指标）：
- 按 ExperimentSpec.real_data 的 path_rule（files_root + entry['path']）读取原始数据清单中的文件；
  只能读清单登记的文件，不得联网下载，不得用随机数冒充真实数据。
- 把 ExperimentSpec.datasets 中的每个数据集准备成实验可直接读取的数组，写到相对工作目录的 prepared/ 下，
  格式只用 .npy 或 .npz（numpy.save / numpy.savez，不要 pickle），按研究对象、几何、时间组织文件。
  对真实数据完成裁剪、按时间堆叠、相位与相干性等对齐、单位换算等预处理，并保留日期/坐标等元数据数组。
- 每个研究对象必须按它在 ExperimentSpec.real_data.references 中的 location（经纬度）在影像上定位并裁剪/提取，
  不得用景中心或固定窗口；多个对象位于同一 burst 时，各自的数组也必须来自各自坐标处的像素。
  不同研究对象的观测数组（相位、相干性、DEM 等二维及以上数组）若完全相同，宿主会判为不独立并拒绝。
- kind=synthetic 的数据集在这里按其 generator 生成（固定随机种子）并落盘，与真实数据分开存放。
- 写 prepared/index.json：JSON 列表，每个输出文件一项
  {"dataset_id": 计划中的数据集 id, "file": 相对 prepared/ 的路径, "description": 内容说明,
   "units": 单位, "sources": [该文件读取的原始清单条目 id，合成数据为空列表]}。
- 支持环境变量 RC_VALIDATION=1 时只处理少量对象/时段（便于快速检查），否则处理计划需要的全部数据。
- 成功结束时单独一行打印 RC_PREP_OK；任何对象处理失败要如实报错退出，不得静默跳过后宣称成功。"""


class PrepError(RuntimeError):
    pass


def prepared_root(config: Any, run_dir: Path) -> Path | None:
    base = getattr(config.experiment.execution_check, "data_base", "")
    return Path(base) / "runs" / run_dir.name / "prepared" if base else None


def enabled(config: Any) -> bool:
    check = config.experiment.execution_check
    return bool(check.enabled and check.data_base and getattr(check, "prepare_data", True))


def _input_key(spec: dict, plan_text: str) -> str:
    from researchclaw.pipeline.execution_contract import digest
    return digest([PREP_VERSION, spec.get("hashes"), spec.get("datasets"),
                   [d.get("manifest_sha256") for d in spec.get("real_data") or [] if isinstance(d, dict)],
                   plan_text])


def _raw_entries(spec: dict) -> dict[str, dict]:
    """Every registered raw file, by manifest entry id, after recomputing hashes."""
    from researchclaw.data_acquisition.manifest import Manifest
    entries: dict[str, dict] = {}
    for real in spec.get("real_data") or []:
        if not isinstance(real, dict) or not real.get("root"):
            continue
        manifest = Manifest(Path(real["root"]))
        report = manifest.verify()
        if not report["ok"]:
            raise PrepError(f"原始数据清单复核失败：缺失 {len(report['missing'])}，哈希不符 {len(report['mismatched'])}")
        entries.update({e["id"]: e for e in manifest.data["entries"]})
    return entries


def _describe_array(path: Path) -> list[dict]:
    import numpy as np
    arrays: list[tuple[str, Any]] = []
    if path.suffix == ".npy":
        arrays.append(("", np.load(path, allow_pickle=False, mmap_mode="r")))
    else:
        with np.load(path, allow_pickle=False) as data:
            arrays.extend((key, data[key]) for key in data.files)
    rows = []
    for key, arr in arrays:
        row: dict[str, Any] = {"array": key, "shape": list(arr.shape), "dtype": str(arr.dtype)}
        if arr.dtype.kind in "fiuc" and arr.size:
            values = np.asarray(arr)
            finite = np.isfinite(values) if values.dtype.kind in "fc" else np.ones(values.shape, bool)
            row["finite_fraction"] = float(finite.mean())
            if finite.any():
                sample = values[finite]
                if values.dtype.kind == "c":
                    sample = np.abs(sample)
                row["min"], row["max"] = float(sample.min()), float(sample.max())
        # 用于跨研究对象的独立性检查；常数数组（如全 True 的有效掩码）相同不说明问题，不参与
        if arr.ndim >= 2 and arr.size >= 1024 and not np.all(np.asarray(arr) == np.asarray(arr).flat[0]):
            import hashlib
            row["content_sha256"] = hashlib.sha256(
                np.ascontiguousarray(arr).tobytes() + str(arr.shape).encode() + str(arr.dtype).encode()).hexdigest()
        rows.append(row)
    return rows


def _all_invalid(path: Path, names: list[str]) -> list[str]:
    """Arrays X whose companion mask X_valid / X_mask is all False."""
    import numpy as np
    if path.suffix != ".npz":
        return []
    dead = []
    with np.load(path, allow_pickle=False) as data:
        for name in names:
            for suffix in ("_valid", "_mask"):
                mask = name + suffix
                if mask in data.files and data[mask].dtype == bool and data[mask].size and not data[mask].any():
                    dead.append(name)
    return dead


def check_prepared(out: Path, spec: dict, raw: dict[str, dict]) -> tuple[list[str], list[dict]]:
    """Host-side acceptance of prepare.py output; returns (errors, card rows)."""
    from researchclaw.data_acquisition.download import sha256_file
    errors: list[str] = []
    index_path = out / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["缺少 prepared/index.json"], []
    except ValueError as exc:
        return [f"prepared/index.json 不是合法 JSON：{exc}"], []
    if not isinstance(index, list) or not index:
        return ["prepared/index.json 必须是非空列表"], []
    datasets = {d.get("id"): d for d in spec.get("datasets") or [] if isinstance(d, dict)}
    rows: list[dict] = []
    covered: set[str] = set()
    for item in index:
        if not isinstance(item, dict):
            errors.append("index.json 条目必须是对象"); continue
        did, rel = item.get("dataset_id"), str(item.get("file") or "")
        if did not in datasets:
            errors.append(f"{rel}: dataset_id {did!r} 不在计划的数据集中"); continue
        path = (out / rel).resolve()
        if not rel or not path.is_relative_to(out.resolve()) or not path.is_file() or not path.stat().st_size:
            errors.append(f"{rel}: 文件不存在、为空或越出 prepared/"); continue
        if path.suffix not in (".npy", ".npz"):
            errors.append(f"{rel}: 只接受 .npy/.npz"); continue
        sources = item.get("sources")
        kind = datasets[did].get("kind")
        if not isinstance(sources, list):
            errors.append(f"{rel}: sources 必须是列表"); continue
        unknown = [s for s in sources if s not in raw]
        if unknown:
            errors.append(f"{rel}: sources 中有不在原始清单的条目 {unknown[:3]}"); continue
        if kind != "synthetic" and not sources:
            errors.append(f"{rel}: 真实数据集 {did} 的文件必须列出读取的原始清单条目"); continue
        try:
            arrays = _describe_array(path)
        except Exception as exc:  # noqa: BLE001 - 读不出来就是不合格
            errors.append(f"{rel}: numpy 无法加载（{exc}）"); continue
        empty = [a["array"] or rel for a in arrays if not a["shape"] or 0 in a["shape"]]
        if empty:
            errors.append(f"{rel}: 数组为空 {empty[:3]}"); continue
        if kind != "synthetic" and any(a.get("finite_fraction", 1.0) == 0.0 for a in arrays):
            errors.append(f"{rel}: 数组全部为非有限值"); continue
        # 有效掩码全为 False 等于没有这项数据（真实运行中 bperp 全 0、bperp_valid 全 False，原始元数据里其实有）
        dead = _all_invalid(path, [a["array"] for a in arrays])
        if dead:
            errors.append(f"{rel}: 数组 {dead} 的有效掩码全部为 False，等于没有这项数据：须从原始文件正确解析；"
                          "原始文件确实没有该信息时不要输出该数组，并在 description 中如实说明缺失")
            continue
        covered.add(did)
        rows.append({"dataset_id": did, "kind": kind, "file": rel, "path": str(path),
                     "description": item.get("description"), "units": item.get("units"),
                     "sources": sources, "bytes": path.stat().st_size, "sha256": sha256_file(path),
                     "arrays": arrays})
    # 独立性：不同研究对象的观测数组逐字节相同，说明没有按各自位置提取（留一验证会变成拿训练数据测试）
    seen: dict[str, tuple[str, frozenset]] = {}
    clashes: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        if row["kind"] == "synthetic":
            continue
        targets = frozenset(str(raw[s].get("target") or raw[s].get("site") or "") for s in row["sources"])
        for arr in row["arrays"]:
            digest_ = arr.get("content_sha256")
            if not digest_:
                continue
            if digest_ in seen and seen[digest_][1].isdisjoint(targets):
                clashes.setdefault((row["file"], seen[digest_][0]), []).append(arr["array"])
            seen.setdefault(digest_, (row["file"], targets))
    for (file, other), names in clashes.items():
        errors.append(f"{file} 与 {other} 属于不同研究对象，但数组 {names} 完全相同：不是独立样本，"
                      "须按各对象 references.location 在影像上定位提取，不能用景中心或固定窗口")
    missing = [did for did in datasets if did not in covered]
    if missing:
        errors.append(f"计划中的数据集没有准备好的文件：{missing}")
    return errors, rows


def _publish(staged: Path, dest: Path, rows: list[dict], script: str) -> dict:
    """Move accepted output to the mounted data root and register it with hashes."""
    from researchclaw.data_acquisition.manifest import Manifest
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(staged, tmp)
    if dest.exists():
        shutil.rmtree(dest)
    tmp.replace(dest)
    manifest = Manifest(dest)
    for row in rows:
        row["path"] = str((dest / row["file"]).resolve())
        manifest.add_file(row["dataset_id"] + "/" + row["file"], dest / row["file"], source_url="prepare.py",
                          license="derived from registered raw data" if row["sources"] else "synthetic",
                          kind="prepared", dataset_id=row["dataset_id"], sources=row["sources"])
    manifest.save()
    from researchclaw.pipeline.execution_contract import digest
    return {"root": str(dest), "manifest": str(dest / "data_manifest.json"),
            "path_rule": "每个文件的 path 是绝对路径，实验代码直接用 numpy.load 读取；只读，不要写入",
            "prepare_script_sha256": digest(script), "files": rows}


def card_summary(card: dict) -> dict:
    """What stage 10 sees: every prepared file with shapes, no raw-manifest details."""
    return {"root": card["root"], "path_rule": card["path_rule"],
            "files": [{k: r.get(k) for k in ("dataset_id", "kind", "path", "description", "units", "arrays")}
                      for r in card["files"]]}


def ensure_prepared(config: Any, run_dir: Path, llm: Any, spec: dict, plan_text: str) -> dict | None:
    """Run (or reuse) experiment preparation; returns the dataset card or None when disabled."""
    if not enabled(config):
        return None
    from researchclaw.experiment.factory import create_sandbox
    from researchclaw.pipeline.execution_contract import failure_signature, write_json
    from researchclaw.pipeline.patch_agent import PatchCodeAgent, PatchError

    prep = run_dir / PREP_DIR
    prep.mkdir(exist_ok=True)
    dest = prepared_root(config, run_dir)
    key = _input_key(spec, plan_text)

    def note(title: str, detail: str = "", status: str = "running") -> None:
        emit("prep", "实验准备：" + title, detail, run_dir=run_dir, phase="prep", status=status)

    record_path = prep / "record.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        card = json.loads((prep / "dataset_card.json").read_text(encoding="utf-8"))
        if record.get("ok") and record.get("key") == key and _card_intact(card):
            note("复用已完成的数据准备", card["root"], "completed")
            return card
    except (OSError, ValueError, KeyError):
        pass

    note("复核原始数据清单")
    raw = _raw_entries(spec)
    workspace = prep / "workspace"
    workspace.mkdir(exist_ok=True)
    agent = PatchCodeAgent(llm, run_dir=run_dir, stage_dir=prep, spec=spec, plan_text="【实验准备】\n" + plan_text,
                           step_max_tokens=config.experiment.code_agent.step_max_tokens,
                           emit=lambda kind, title, detail="": note(title, detail))
    files = {p.name: p.read_text(encoding="utf-8") for p in workspace.glob("*.py")}
    check = config.experiment.execution_check
    exp = config.experiment
    instruction, step = INSTRUCTION, "prep:write"
    same, total, last_sig = 0, 0, None
    while True:
        calls_before = agent.model_calls
        try:
            files = agent.step(step, instruction, files)
        except PatchError as exc:
            raise PrepError(f"prepare.py 编写未完成：{exc}") from exc
        for name, body in files.items():
            (workspace / name).parent.mkdir(parents=True, exist_ok=True)
            (workspace / name).write_text(body, encoding="utf-8")
        if SCRIPT not in files:
            errors, stdout = [f"工作区缺少 {SCRIPT}"], ""
        else:
            note("在实验沙箱中运行 prepare.py")
            sandbox_dir = prep / "sandbox"
            if sandbox_dir.exists():
                shutil.rmtree(sandbox_dir)
            sandbox = create_sandbox(exp, sandbox_dir)
            result = sandbox.run_project(workspace, entry_point=SCRIPT, timeout_sec=exp.time_budget_sec)
            run_no = getattr(sandbox, "_run_counter", 1)
            staging = sandbox.workdir / (f"_docker_project_{run_no}" if exp.mode == "docker" else f"_project_{run_no}")
            stdout = (result.stdout or "")[-4000:] + "\n" + (result.stderr or "")[-4000:]
            if result.returncode != 0 or result.timed_out or OK_MARK not in (result.stdout or ""):
                errors = [f"prepare.py 运行失败或未打印 {OK_MARK}（returncode={result.returncode}，"
                          f"timed_out={result.timed_out}）"]
            else:
                errors, rows = check_prepared(staging / "prepared", spec, raw)
        write_json(prep / "attempt.json", {"step": step, "errors": errors, "output_tail": stdout})
        if not errors:
            break
        note("准备结果未通过宿主检查", "\n".join(errors), "failed")
        feedback = json.dumps({"error": "; ".join(errors)}, ensure_ascii=False) + "\n" + stdout
        sig = failure_signature(feedback)
        same = same + 1 if sig == last_sig else 0
        last_sig = sig
        if same >= check.max_code_repairs or total >= check.max_code_repairs_total:
            write_json(record_path, {"ok": False, "key": key, "errors": errors})
            raise PrepError("实验准备未通过：" + "; ".join(errors))
        if agent.model_calls > calls_before:  # 按缓存重放的修复不计入总上限
            total += 1
        step = "prep:fix:" + sig[:8]
        instruction = ("修复 prepare.py，使宿主检查通过，不改变计划中的数据集与方法：\n- " + "\n- ".join(errors)
                       + "\n\n运行输出末尾：\n" + stdout[-3000:] + "\n\n原始要求：\n" + INSTRUCTION)
    card = _publish(staging / "prepared", dest, rows, files[SCRIPT])
    write_json(prep / "dataset_card.json", card)
    write_json(record_path, {"ok": True, "key": key, "files": len(rows)})
    note("数据准备完成并登记", f"{len(rows)} 个文件 → {card['root']}", "completed")
    return card


def _card_intact(card: dict) -> bool:
    from researchclaw.data_acquisition.download import sha256_file
    for row in card.get("files") or []:
        path = Path(row["path"])
        if not path.is_file() or path.stat().st_size != row["bytes"] or sha256_file(path) != row["sha256"]:
            return False
    return bool(card.get("files"))


def attach_card(spec: dict, run_dir: Path) -> dict:
    """把已验收的数据卡挂到规格上（阶段 10 实现与验收修复用同一份，提示词与 Tower 状态一致）。"""
    try:
        record = json.loads((run_dir / PREP_DIR / "record.json").read_text(encoding="utf-8"))
        card = json.loads((run_dir / PREP_DIR / "dataset_card.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return spec
    if record.get("ok"):
        spec["prepared_data"] = card_summary(card)
    return spec
