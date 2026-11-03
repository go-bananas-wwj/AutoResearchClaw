"""Generic data-source tools.  They execute parameters; they make no research choices.

Which targets, places, time windows and sources an experiment needs is decided
by the pipeline model in ``data_plan.json`` (see :mod:`.planner`).  Each tool
here offers ``estimate(params)`` (real catalogue queries, no downloads) and
``acquire(params, ctx)`` (resumable, hash-registered downloads).

Shared storage under ``<base>/shared/`` is keyed by source identifiers
(burst pair, STAC item, URL hash), so different runs reuse files and HyP3
jobs instead of paying twice.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from researchclaw.data_acquisition.download import fetch
from researchclaw.data_acquisition.manifest import Manifest

logger = logging.getLogger(__name__)

ASF_SEARCH = "https://api.daac.asf.alaska.edu/services/search/param"
HYP3 = "https://hyp3-api.asf.alaska.edu"
EDL_TOKEN = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
INSAR_KEEP = ("_wrapped_phase.tif", "_corr.tif", "_unw_phase.tif", "_conncomp.tif",
              "_lv_theta.tif", "_lv_phi.tif", "_dem.tif", ".txt", ".md")


@dataclass
class Context:
    base: Path               # data base dir (shared store + per-run manifests)
    manifest: Manifest       # this run's manifest (paths relative to base)
    target: dict[str, Any]   # the plan target this acquisition serves
    should_stop: Callable[[], bool]
    emit: Callable[[str, str], None]
    poll_sec: float = 300.0


def _sleep(ctx: Context, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if ctx.should_stop():
            raise InterruptedError("运行已被停止")
        time.sleep(min(5.0, end - time.time()))


# ------------------------------------------------------------------ ASF burst InSAR via HyP3
def earthdata_token() -> str:
    user, password = os.environ.get("EARTHDATA_USERNAME"), os.environ.get("EARTHDATA_PASSWORD")
    if not user or not password:
        raise RuntimeError("缺少 EARTHDATA_USERNAME/EARTHDATA_PASSWORD 环境变量")
    resp = requests.post(EDL_TOKEN, auth=(user, password), timeout=60)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _bursts(params: dict[str, Any], target: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Scenes per full burst ID at the target point; one burst per requested flight direction."""
    query = {"dataset": "SLC-BURST", "polarization": "VV", "start": params["start"], "end": params["end"],
             "output": "geojson", "maxResults": 5000}
    if params.get("full_burst_ids"):
        groups: dict[str, list] = {}
        for fid in params["full_burst_ids"]:
            groups[fid] = _search({**query, "fullBurstID": fid})
        return groups
    query["intersectsWith"] = f"POINT({target['lon']} {target['lat']})"
    by_burst: dict[tuple[str, str], list] = {}
    for scene in _search(query):
        by_burst.setdefault((scene["direction"], scene["burst"]), []).append(scene)
    chosen: dict[str, list] = {}
    for direction in params.get("directions") or ["ASCENDING", "DESCENDING"]:
        options = [(len(v), k[1]) for k, v in by_burst.items() if k[0] == direction]
        if options:  # deterministic host rule: the burst over the point with most acquisitions
            burst = max(options)[1]
            chosen[burst] = by_burst[(direction, burst)]
    return chosen


def _search(query: dict[str, Any]) -> list[dict[str, Any]]:
    resp = requests.get(ASF_SEARCH, params=query, timeout=180)
    resp.raise_for_status()
    by_day: dict[tuple[str, str], dict[str, Any]] = {}
    for feature in resp.json().get("features", []):
        p = feature["properties"]
        burst = p["burst"]["fullBurstID"]
        by_day.setdefault((burst, p["startTime"][:10]), {
            "granule": p["sceneName"], "time": p["startTime"], "burst": burst,
            "direction": p.get("flightDirection"), "path": p.get("pathNumber")})
    return [by_day[k] for k in sorted(by_day)]


def _pairs(scenes: list[dict[str, Any]], neighbours: int) -> list[tuple[str, str]]:
    scenes = sorted(scenes, key=lambda s: s["time"])
    return [(a["granule"], scenes[j]["granule"]) for i, a in enumerate(scenes)
            for j in range(i + 1, min(len(scenes), i + 1 + neighbours))]


class Hyp3:
    def __init__(self, token: str) -> None:
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self._all: list[dict[str, Any]] | None = None

    def all_jobs(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._all is None or refresh:
            found, url, params = [], f"{HYP3}/jobs", {"job_type": "INSAR_ISCE_BURST"}
            while url:
                resp = self.http.get(url, params=params, timeout=180)
                resp.raise_for_status()
                body = resp.json()
                found.extend(body.get("jobs", []))
                url, params = body.get("next"), None
            self._all = found
        return self._all

    def submit(self, name: str, pairs: list[tuple[str, str]], looks: str) -> None:
        for start in range(0, len(pairs), 100):
            batch = [{"job_type": "INSAR_ISCE_BURST", "name": name,
                      "job_parameters": {"granules": list(p), "looks": looks}} for p in pairs[start:start + 100]]
            resp = self.http.post(f"{HYP3}/jobs", json={"jobs": batch}, timeout=300)
            if resp.status_code >= 400:
                raise RuntimeError(f"HyP3 提交失败 HTTP {resp.status_code}: {resp.text[:500]}")

    def credits(self) -> int:
        resp = self.http.get(f"{HYP3}/user", timeout=60)
        resp.raise_for_status()
        return int(resp.json().get("remaining_credits") or 0)


class AsfInsarBurst:
    """Sentinel-1 burst interferograms (wrapped phase, coherence, geometry) via ASF HyP3."""

    name = "asf_insar_burst"
    describe = ("Sentinel-1 IW 突发（burst）干涉对，经 ASF HyP3 INSAR_ISCE_BURST 生成地理编码的缠绕相位、"
                "相干性、解缠相位、连通分量、入射/方位角与 DEM（约 40 m@10x2）。参数：start,end(YYYY-MM-DD，"
                "2014-10 之后)，directions(ASCENDING/DESCENDING 列表，可选)，full_burst_ids(可选)，"
                "neighbours(每景与后续 1–3 景组对)，looks('10x2'|'20x4'|'5x1')。每对消耗 1 credit。")

    def plan(self, params: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        groups = _bursts(params, target)
        neighbours = int(params.get("neighbours", 2))
        return {burst: {"scenes": scenes, "pairs": _pairs(scenes, neighbours)} for burst, scenes in groups.items()}

    def estimate(self, params: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        plan = self.plan(params, target)
        pairs = sum(len(v["pairs"]) for v in plan.values())
        return {"bursts": {b: {"scenes": len(v["scenes"]), "pairs": len(v["pairs"])} for b, v in plan.items()},
                "credits": pairs, "gb": round(pairs * 0.025, 2)}

    def acquire(self, params: dict[str, Any], ctx: Context) -> dict[str, Any]:
        looks = params.get("looks", "10x2")
        plan = self.plan(params, ctx.target)
        hyp3 = Hyp3(earthdata_token())
        known = {tuple(j["job_parameters"]["granules"]): j for j in hyp3.all_jobs()
                 if j.get("status_code") != "FAILED" and j["job_parameters"].get("looks", "20x4") == looks}
        for burst, item in plan.items():
            missing = [p for p in item["pairs"] if tuple(p) not in known]
            if missing:
                if hyp3.credits() < len(missing):
                    raise RuntimeError(f"HyP3 credits 不足：{burst} 需要 {len(missing)}")
                hyp3.submit(f"rc-{burst}", missing, looks)
                ctx.emit("提交 HyP3 处理", f"{burst}：{len(missing)} 对")
        wanted = {tuple(p): burst for burst, item in plan.items() for p in item["pairs"]}
        while True:
            jobs = {tuple(j["job_parameters"]["granules"]): j for j in hyp3.all_jobs(refresh=True)
                    if tuple(j["job_parameters"]["granules"]) in wanted
                    and j["job_parameters"].get("looks", "20x4") == looks}
            counts: dict[str, int] = {}
            for pair, job in jobs.items():
                counts[job["status_code"]] = counts.get(job["status_code"], 0) + 1
                if job["status_code"] == "SUCCEEDED":
                    self._collect(pair, wanted[pair], job, ctx, looks)
            ctx.manifest.save()
            ctx.emit("HyP3 进度", str(counts))
            if not any(k in counts for k in ("PENDING", "RUNNING")):
                return {"pairs": len(wanted), "counts": counts}
            _sleep(ctx, ctx.poll_sec)

    def _collect(self, pair: tuple[str, str], burst: str, job: dict[str, Any], ctx: Context, looks: str) -> None:
        pair_id = f"{pair[0][14:22]}_{pair[1][14:22]}"
        product_dir = ctx.base / "shared" / "hyp3" / looks / burst / pair_id
        info = job["files"][0]
        if not (product_dir / ".complete").is_file():
            zpath = ctx.base / "shared" / "hyp3" / "_downloads" / info["filename"]
            fetch(info["url"], zpath, expected_size=int(info["size"]))
            try:
                with zipfile.ZipFile(zpath) as archive:
                    if archive.testzip():
                        raise zipfile.BadZipFile("CRC 校验失败")
                    product_dir.mkdir(parents=True, exist_ok=True)
                    for member in archive.namelist():
                        if member.endswith(INSAR_KEEP):
                            with archive.open(member) as src, (product_dir / Path(member).name).open("wb") as dst:
                                dst.write(src.read())
            except (zipfile.BadZipFile, OSError) as exc:
                logger.warning("product %s corrupt, will re-download: %s", pair_id, exc)
                zpath.unlink(missing_ok=True)
                return
            (product_dir / ".complete").write_text(job["job_id"])
            zpath.unlink(missing_ok=True)
        for path in sorted(product_dir.iterdir()):
            entry_id = f"{ctx.target['id']}/{burst}/{pair_id}/{path.name}"
            if path.name != ".complete" and entry_id not in ctx.manifest:
                ctx.manifest.add_file(entry_id, path, source_url=info["url"], kind="insar_product",
                                      license="Contains modified Copernicus Sentinel data; ASF HyP3",
                                      target=ctx.target["id"], burst=burst, granules=list(pair),
                                      hyp3_job_id=job["job_id"], looks=looks)


# ------------------------------------------------------------------ Planetary Computer STAC
class PlanetaryComputer:
    name = "planetary_computer"
    describe = ("Microsoft Planetary Computer STAC 集合（匿名，SAS 签名下载）。参数：collection(须为目录中真实存在的 id)，"
                "asset(资产键，默认 data)，pad_deg(以目标点为中心的边长一半，度)，datetime(可选 'A/B')，max_items。"
                "注意：该平台不提供 Sentinel-1 SLC/干涉图。")

    def _items(self, params: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
        pad = float(params.get("pad_deg", 0.1))
        body = {"collections": [params["collection"]], "limit": int(params.get("max_items", 20)),
                "bbox": [target["lon"] - pad, target["lat"] - pad, target["lon"] + pad, target["lat"] + pad]}
        if params.get("datetime"):
            body["datetime"] = params["datetime"]
        resp = requests.post(f"{PC_STAC}/search", json=body, timeout=120)
        resp.raise_for_status()
        return resp.json().get("features", [])

    def estimate(self, params: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        collections = {c["id"] for c in requests.get(f"{PC_STAC}/collections", timeout=120).json()["collections"]}
        if params.get("collection") not in collections:
            raise ValueError(f"Planetary Computer 不存在集合 {params.get('collection')!r}")
        items = self._items(params, target)
        return {"items": len(items), "credits": 0, "gb": round(len(items) * 0.05, 2)}

    def acquire(self, params: dict[str, Any], ctx: Context) -> dict[str, Any]:
        collection, asset = params["collection"], params.get("asset", "data")
        token = requests.get(f"{PC_SAS}/{collection}", timeout=60).json()["token"]
        items = self._items(params, ctx.target)
        for item in items:
            href = item["assets"][asset]["href"]
            dest = ctx.base / "shared" / "stac" / collection / Path(href.split("?")[0]).name
            entry_id = f"{ctx.target['id']}/{collection}/{item['id']}"
            if entry_id in ctx.manifest and dest.is_file():
                continue
            if ctx.should_stop():
                raise InterruptedError("运行已被停止")
            fetch(f"{href}?{token}", dest)
            ctx.manifest.add_file(entry_id, dest, source_url=href, kind="stac_asset", target=ctx.target["id"],
                                  license=f"see Planetary Computer collection {collection}", collection=collection,
                                  item=item["id"])
            ctx.manifest.save()
        return {"items": len(items)}


# ------------------------------------------------------------------ plain public URL
class HttpFile:
    name = "http_file"
    describe = "公开 HTTPS 文件直链（论文补充数据、监测数据集等）。参数：url，license，filename(可选)。"

    def estimate(self, params: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        head = requests.head(params["url"], allow_redirects=True, timeout=60)
        if head.status_code >= 400:
            raise ValueError(f"URL 不可访问（HTTP {head.status_code}）：{params['url']}")
        size = int(head.headers.get("Content-Length") or 0)
        return {"credits": 0, "gb": round(size / 1e9, 3)}

    def acquire(self, params: dict[str, Any], ctx: Context) -> dict[str, Any]:
        url = params["url"]
        name = params.get("filename") or Path(url.split("?")[0]).name or "download"
        dest = ctx.base / "shared" / "http" / hashlib.sha256(url.encode()).hexdigest()[:16] / name
        entry_id = f"{ctx.target['id']}/http/{dest.parent.name}/{name}"
        if entry_id not in ctx.manifest or not dest.is_file():
            fetch(url, dest)
            ctx.manifest.add_file(entry_id, dest, source_url=url, kind="http_file", target=ctx.target["id"],
                                  license=params.get("license", "unspecified"))
            ctx.manifest.save()
        return {"files": 1}


REGISTRY = {tool.name: tool for tool in (AsfInsarBurst(), PlanetaryComputer(), HttpFile())}
