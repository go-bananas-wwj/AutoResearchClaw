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


def _flatten(value: Any, prefix: str = "") -> dict[str, float]:
    rows: dict[str, float] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            rows.update(_flatten(item, f"{prefix}/{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            rows.update(_flatten(item, f"{prefix}[{i}]"))
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
            for key, value in _flatten(data).items():
                entries.append({"kind": "result", "value": value, "name": key, "node": best["node"],
                                "commit": best["commit"], "file": str(results.relative_to(run_dir))})
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
    unsupported, checked, cites = [], 0, 0
    for m in re.finditer(r"\\cite[pt]?\s*(?:\[[^\]]*\])?\{([^}]*)\}", body):
        for key in (k.strip() for k in m.group(1).split(",")):
            cites += 1
            if key and key not in keys:
                unsupported.append({"kind": "citation", "value": key, "context": body[max(0, m.start() - 60):m.end() + 20]})
    scrubbed = _SKIP_COMMANDS.sub(" ", body)
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
        if not any(_matches(number, value) for value, _ in numbers):
            unsupported.append({"kind": "number", "value": raw,
                                "context": scrubbed[max(0, m.start() - 60):m.end() + 40].replace("\n", " ")})
    return {"checked": checked, "citations_checked": cites, "unsupported": unsupported,
            "ledger_hash": ledger.get("hash")}
