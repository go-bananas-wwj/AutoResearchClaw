"""Evidence ledger and claim checker (docs/AUTORESEARCH_ARCHITECTURE_CN.md §3.4–3.5).

The ledger collects every fact a paper may state, each with its origin:

* ``result``    – metrics of the experiment-tree best node (node, commit, file, key);
* ``setting``   – numeric constants in the best implementation (file, line, name);
* ``reference`` – real-data provenance such as published failure dates (DOI);
* ``literature``– BibTeX entries the paper may cite (key, title, DOI/URL).

:func:`check_claims` then verifies a manuscript: every number must match a
ledger value (rounding/percent variants allowed) and every ``\\cite`` key must
be a ledger literature entry.  Unsupported claims are reported, never patched.
"""

from __future__ import annotations

import ast
import json
import math
import re
from pathlib import Path
from typing import Any

LEDGER_FILE = "evidence_ledger.json"
# 结果里的长数组（样本、协方差、逐像元场）不是可写进论文的结论：展开后账本有数十万个数，
# 论文里几乎任何数字都能"碰巧"匹配某个元素，主张校验就失去意义（真实运行 318308 条）。
MAX_LIST_LEAVES = 32
# 由两个结果标量相减得到的数字（"由 413.4461 升至 547.2020，增量 133.7559"）可追溯，按派生值接受。
MAX_DERIVED_SCALARS = 2000


def _leaves(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_leaves(v) for v in value.values())
    if isinstance(value, list):
        return sum(_leaves(v) for v in value)
    return 1


def _flatten(value: Any, prefix: str = "", *, max_list: int | None = None) -> dict[str, float]:
    rows: dict[str, float] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            rows.update(_flatten(item, f"{prefix}/{key}" if prefix else str(key), max_list=max_list))
    elif isinstance(value, list):
        if max_list is not None and _leaves(value) > max_list:
            return rows
        for i, item in enumerate(value):
            rows.update(_flatten(item, f"{prefix}[{i}]", max_list=max_list))
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        rows[prefix] = float(value)
    return rows


def _settings(files: dict[str, str]) -> list[dict[str, Any]]:
    """Numeric constants assigned in the implementation (module/class level and dict literals)."""
    rows = []
    for path, code in files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id if isinstance(t, ast.Name) else getattr(t, "attr", "") for t in targets]
                value = node.value
                if value is None:
                    continue
                for name in filter(None, names):
                    try:
                        literal = ast.literal_eval(value)
                    except (ValueError, SyntaxError, TypeError):
                        continue
                    for key, number in _flatten(literal, name).items():
                        rows.append({"kind": "setting", "value": number, "name": key,
                                     "file": path, "line": node.lineno})
    return rows


def _bib_entries(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = []
    for m in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)\n\}", text, re.DOTALL):
        body = m.group(2)
        field = lambda f: (re.search(rf"\b{f}\s*=\s*[{{\"](.+?)[}}\"]\s*,?\s*$", body, re.MULTILINE | re.IGNORECASE)
                           or [None, ""])[1]
        rows.append({"kind": "literature", "key": m.group(1), "title": field("title"),
                     "doi": field("doi"), "url": field("url"), "year": field("year"), "source": str(path)})
    return rows


def build_ledger(run_dir: Path, *, data_roots: tuple[str, ...] = ()) -> dict[str, Any]:
    from researchclaw.pipeline.execution_contract import digest

    entries: list[dict[str, Any]] = []
    best = None
    try:
        best = json.loads((run_dir / "experiment_tree" / "best.json").read_text())
    except (OSError, ValueError):
        pass
    if best:
        from researchclaw.pipeline.experiment_tree import ExperimentTree
        tree = ExperimentTree(run_dir, best.get("direction", "minimize"))
        files = tree.files_at(best["commit"])
        entries += [{**row, "node": best["node"], "commit": best["commit"]} for row in _settings(files)]
        for results in sorted((run_dir / ".formal_runs").glob(f"*-{best['node']}/results.json")) + \
                sorted(run_dir.glob(f"stage-13/experiment_v*/results.json")):
            try:
                data = json.loads(results.read_text())
            except (OSError, ValueError):
                continue
            for key, value in _flatten(data, max_list=MAX_LIST_LEAVES).items():
                entries.append({"kind": "result", "value": value, "name": key, "node": best["node"],
                                "commit": best["commit"], "file": str(results.relative_to(run_dir))})
            break
        # 正式运行记录（阶段 12 回执）：用时、时间预算，只取主指标与最佳节点一致的那次
        for record in sorted((run_dir / "stage-12" / "runs").glob("run-*.json")):
            try:
                rec = json.loads(record.read_text())
            except (OSError, ValueError):
                continue
            if best.get("metric") not in (rec.get("metrics") or {}).values():
                continue
            receipt = rec.get("receipt") or {}
            for key in ("elapsed_sec", "time_budget_sec"):
                value = receipt.get(key, rec.get(key))
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    entries.append({"kind": "result", "value": float(value), "name": f"run/{key}",
                                    "node": best["node"], "file": str(record.relative_to(run_dir))})
            break
    for root in data_roots:
        try:
            manifest = json.loads((Path(root) / "data_manifest.json").read_text())
        except (OSError, ValueError):
            continue
        for e in manifest.get("entries", []):
            if e.get("kind") == "reference":
                entries.append({"kind": "reference", "name": e["id"], "text": e.get("failure_date"),
                                "source_url": e.get("source_url"), "claim": e.get("claim"), "site": e.get("site")})
    for bib in [run_dir / "stage-22" / "references.bib", run_dir / "stage-04" / "references.bib"]:
        if bib.is_file():
            entries += _bib_entries(bib)
            break
    ledger = {"entries": entries, "best": best}
    ledger["hash"] = digest(entries)
    (run_dir / LEDGER_FILE).write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    return ledger


def ledger_numbers(ledger: dict[str, Any]) -> list[tuple[float, dict[str, Any]]]:
    rows = []
    for e in ledger.get("entries", []):
        if isinstance(e.get("value"), (int, float)):
            rows.append((float(e["value"]), e))
        if e.get("kind") == "reference" and e.get("text"):
            for part in re.findall(r"\d+(?:\.\d+)?", str(e["text"])):
                rows.append((float(part), e))
    return rows


def _decimals(raw: str) -> int:
    digits = raw.rstrip("%").lstrip("+-")
    return len(digits.split(".", 1)[1]) if "." in digits else 0


def _derived(number: float, dp: int, scalars: list[float]) -> tuple[float, float] | None:
    """两个结果标量之差（按论文写出的小数位，先舍入再相减或相减再舍入均可）。"""
    target = abs(number)
    for i, a in enumerate(scalars):
        for b in scalars[i + 1:]:
            if round(abs(a - b), dp) == target or round(abs(round(a, dp) - round(b, dp)), dp) == target:
                return a, b
    return None


def _matches(number: float, value: float) -> bool:
    for candidate in (value, value * 100, value / 100):
        if candidate == number:
            return True
        if candidate != 0:
            decimals = 6
            for dp in range(decimals + 1):
                if round(candidate, dp) == number:
                    return True
    return False


_NUM = re.compile(r"(?<![\w.\\])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?")
_SKIP_COMMANDS = re.compile(r"\\(?:cite[pt]?|ref|eqref|label|includegraphics|usepackage|documentclass|"
                            r"begin|end|vspace|hspace|setlength|fontsize|bibliographystyle|bibliography|"
                            r"input|url|href)\s*(?:\[[^\]]*\])?\{[^}]*\}")


def check_claims(text: str, ledger: dict[str, Any], *, small_int_exempt: int = 10) -> dict[str, Any]:
    """Return {checked, unsupported:[{kind, value, context}], citations_checked}.

    Integers 0..``small_int_exempt`` without a % sign are treated as counts/structure
    (section numbers, "3 种方法"), all other numbers need a ledger source.
    """
    body = re.sub(r"(?m)(?<!\\)%.*$", "", text)  # LaTeX comments (incl. 批注) are not claims; keep \%
    body = body.split("\\begin{document}", 1)[-1]
    numbers = ledger_numbers(ledger)
    keys = {e["key"] for e in ledger.get("entries", []) if e.get("kind") == "literature"}
    scalars = sorted({v for v, e in numbers if e.get("kind") == "result"})[:MAX_DERIVED_SCALARS]
    unsupported, derived, checked, cites = [], [], 0, 0
    for m in re.finditer(r"\\cite[pt]?\s*(?:\[[^\]]*\])?\{([^}]*)\}", body):
        for key in (k.strip() for k in m.group(1).split(",")):
            cites += 1
            if key and key not in keys:
                unsupported.append({"kind": "citation", "value": key, "context": body[max(0, m.start() - 60):m.end() + 20]})
    scrubbed = _SKIP_COMMANDS.sub(" ", body)
    scrubbed = re.sub(r"(?m)^(\s*)\d+:", r"\1 ", scrubbed)  # 算法步骤编号 "11: ..." 是结构不是主张
    for m in _NUM.finditer(scrubbed):
        raw = m.group(0)
        percent = raw.endswith("%")
        try:
            number = float(raw.rstrip("%"))
        except ValueError:
            continue
        if not percent and number.is_integer() and 0 <= number <= small_int_exempt:
            continue
        checked += 1
        if any(_matches(number, value) for value, _ in numbers):
            continue
        dp = _decimals(raw)
        # 整数差值太容易偶然命中，只接受带小数位的派生值
        pair = _derived(number, dp, scalars) if not percent and dp >= 1 else None
        if pair:
            derived.append({"value": raw, "from": list(pair)})
        else:
            unsupported.append({"kind": "number", "value": raw,
                                "context": scrubbed[max(0, m.start() - 60):m.end() + 40].replace("\n", " ")})
    return {"checked": checked, "citations_checked": cites, "unsupported": unsupported, "derived": derived,
            "ledger_hash": ledger.get("hash")}
