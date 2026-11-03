"""Real-data acquisition for the landslide-acceleration InSAR project.

Plan approved by the user on 2026-11-03 (docs/DATA_PLAN_INSAR_CN.md):
four landslides with published failure times, one ascending + one descending
Sentinel-1 burst each, nearest 1–2 neighbour pairs processed by ASF HyP3
``INSAR_ISCE_BURST`` (looks 10x2), DEM from Planetary Computer
``cop-dem-glo-30``.

Every step is resumable: HyP3 jobs are matched by name + granule pair before
submitting (a restart never pays twice), products are downloaded with
:func:`fetch` and registered in ``data_manifest.json`` with SHA-256 only
after extraction succeeds.
"""

from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from researchclaw.data_acquisition.download import fetch
from researchclaw.data_acquisition.manifest import Manifest

logger = logging.getLogger(__name__)

ASF_SEARCH = "https://api.daac.asf.alaska.edu/services/search/param"
HYP3 = "https://hyp3-api.asf.alaska.edu"
EDL_TOKEN = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
LOOKS = "10x2"
KEEP_SUFFIXES = ("_wrapped_phase.tif", "_corr.tif", "_unw_phase.tif", "_conncomp.tif",
                 "_lv_theta.tif", "_lv_phi.tif", "_dem.tif", ".txt", ".md")


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    lat: float
    lon: float
    start: str
    end: str
    failure: str
    bursts: tuple[str, ...]  # fullBurstID, ascending + descending
    references: tuple[dict[str, str], ...]


SITES = (
    Site("xinmo", "茂县新磨 Xinmo", 32.069, 103.665, "2015-01-01", "2017-06-25", "2017-06-24",
         ("128_272973_IW3", "062_131909_IW2"),
         ({"doi": "https://doi.org/10.1007/s10346-017-0915-7",
           "claim": "Sentinel-1 precursors; failure early morning 2017-06-24"},)),
    Site("baige", "白格 Baige", 31.083, 98.704, "2016-06-01", "2018-10-11", "2018-10-10",
         ("099_210679_IW1", "106_226423_IW1"),
         ({"doi": "https://doi.org/10.1016/j.rse.2020.111695",
           "claim": "tertiary creep before collapse on 2018-10-10 (second failure 2018-11-03)"},)),
    Site("mudcreek", "Mud Creek (Big Sur)", 35.866, -121.432, "2015-03-01", "2017-05-21", "2017-05-20",
         ("042_088927_IW2", "035_073238_IW3"),
         ({"doi": "https://doi.org/10.1038/s41598-025-11399-8",
           "claim": "slow InSAR motion, runaway acceleration ~1 month before failure on 2017-05-20"},)),
    Site("brienz", "Brienz/Brinzauls", 46.68, 9.66, "2021-06-01", "2023-06-16", "2023-06-15",
         ("015_030346_IW2", "066_140412_IW1"),
         ({"doi": "https://doi.org/10.5194/nhess-24-3833-2024",
           "claim": "acceleration to 4 m/d and failure on 2023-06-15"},)),
)


# ------------------------------------------------------------------ auth
def earthdata_token() -> str:
    user, password = os.environ.get("EARTHDATA_USERNAME"), os.environ.get("EARTHDATA_PASSWORD")
    if not user or not password:
        raise RuntimeError("缺少 EARTHDATA_USERNAME/EARTHDATA_PASSWORD 环境变量")
    resp = requests.post(EDL_TOKEN, auth=(user, password), timeout=60)
    resp.raise_for_status()
    return resp.json()["access_token"]


# ------------------------------------------------------------------ planning
def search_bursts(full_burst_id: str, start: str, end: str) -> list[dict[str, Any]]:
    resp = requests.get(ASF_SEARCH, params={"dataset": "SLC-BURST", "fullBurstID": full_burst_id,
                                            "polarization": "VV", "start": start, "end": end,
                                            "output": "geojson", "maxResults": 2000}, timeout=120)
    resp.raise_for_status()
    by_day: dict[str, dict[str, Any]] = {}
    for feature in resp.json().get("features", []):
        props = feature["properties"]
        by_day.setdefault(props["startTime"][:10], {
            "granule": props["sceneName"], "time": props["startTime"],
            "direction": props.get("flightDirection"), "path": props.get("pathNumber")})
    return [by_day[day] for day in sorted(by_day)]


def plan_pairs(scenes: list[dict[str, Any]], neighbours: int = 2) -> list[tuple[str, str]]:
    pairs = []
    for i, ref in enumerate(scenes):
        for j in range(i + 1, min(len(scenes), i + 1 + neighbours)):
            pairs.append((ref["granule"], scenes[j]["granule"]))
    return pairs


def job_name(site: Site, burst: str) -> str:
    return f"rc-insar-{site.key}-{burst}"


# ------------------------------------------------------------------ HyP3
class Hyp3:
    def __init__(self, token: str) -> None:
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"

    def jobs(self, name: str) -> list[dict[str, Any]]:
        found, url, params = [], f"{HYP3}/jobs", {"name": name}
        while url:
            resp = self.http.get(url, params=params, timeout=120)
            resp.raise_for_status()
            body = resp.json()
            found.extend(body.get("jobs", []))
            url, params = body.get("next"), None
        return found

    def submit(self, name: str, pairs: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
        pairs = list(pairs)
        created = []
        for start in range(0, len(pairs), 100):
            batch = [{"job_type": "INSAR_ISCE_BURST", "name": name,
                      "job_parameters": {"granules": list(p), "looks": LOOKS}} for p in pairs[start:start + 100]]
            resp = self.http.post(f"{HYP3}/jobs", json={"jobs": batch}, timeout=300)
            if resp.status_code >= 400:
                raise RuntimeError(f"HyP3 提交失败 HTTP {resp.status_code}: {resp.text[:500]}")
            created.extend(resp.json().get("jobs", []))
        return created

    def credits(self) -> int:
        resp = self.http.get(f"{HYP3}/user", timeout=60)
        resp.raise_for_status()
        return int(resp.json().get("remaining_credits") or 0)


# ------------------------------------------------------------------ steps
def _state_path(root: Path) -> Path:
    return root / "acquisition_state.json"


def _save_state(root: Path, state: dict[str, Any]) -> None:
    tmp = _state_path(root).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_state_path(root))


def plan(root: Path, sites: tuple[Site, ...] = SITES) -> dict[str, Any]:
    """Search bursts and pairs for every site/track; cached in acquisition_state.json."""
    try:
        state = json.loads(_state_path(root).read_text())
    except (OSError, ValueError):
        state = {"tracks": {}}
    for site in sites:
        for burst in site.bursts:
            key = f"{site.key}/{burst}"
            if key in state["tracks"]:
                continue
            scenes = search_bursts(burst, site.start, site.end)
            state["tracks"][key] = {"site": site.key, "burst": burst, "scenes": scenes,
                                    "pairs": plan_pairs(scenes), "name": job_name(site, burst)}
            logger.info("%s: %d scenes, %d pairs", key, len(scenes), len(state["tracks"][key]["pairs"]))
    root.mkdir(parents=True, exist_ok=True)
    _save_state(root, state)
    return state


def record_references(root: Path, sites: tuple[Site, ...] = SITES) -> None:
    manifest = Manifest(root)
    for site in sites:
        for i, ref in enumerate(site.references):
            manifest.add_reference(f"{site.key}/failure_time/{i}", source_url=ref["doi"],
                                   license="citation (published article)", site=site.key,
                                   failure_date=site.failure, claim=ref["claim"],
                                   location={"lat": site.lat, "lon": site.lon})
    manifest.save()


def check_planetary_computer(root: Path) -> dict[str, Any]:
    """Record what Planetary Computer actually hosts for Sentinel-1 (verifies the phase-data gap)."""
    resp = requests.get(f"{PC_STAC}/collections", timeout=120)
    resp.raise_for_status()
    ids = sorted(c["id"] for c in resp.json().get("collections", []))
    s1 = [i for i in ids if "sentinel-1" in i]
    result = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sentinel_1_collections": s1,
              "has_slc_or_interferogram": any("slc" in i or "insar" in i or "ifg" in i for i in s1),
              "has_cop_dem_glo_30": "cop-dem-glo-30" in ids}
    (root / "planetary_computer_check.json").write_text(json.dumps(result, indent=1))
    return result


def download_dem(root: Path, sites: tuple[Site, ...] = SITES, pad: float = 0.15) -> None:
    manifest = Manifest(root)
    token = requests.get(f"{PC_SAS}/cop-dem-glo-30", timeout=60).json()["token"]
    for site in sites:
        bbox = [site.lon - pad, site.lat - pad, site.lon + pad, site.lat + pad]
        resp = requests.post(f"{PC_STAC}/search", json={"collections": ["cop-dem-glo-30"], "bbox": bbox,
                                                        "limit": 20}, timeout=120)
        resp.raise_for_status()
        for item in resp.json().get("features", []):
            href = item["assets"]["data"]["href"]
            entry_id = f"{site.key}/dem/{item['id']}"
            dest = root / "dem" / site.key / Path(href.split("?")[0]).name
            if entry_id in manifest and dest.is_file():
                continue
            fetch(f"{href}?{token}", dest)
            manifest.add_file(entry_id, dest, source_url=href, kind="dem", site=site.key,
                              license="Copernicus DEM GLO-30 (ESA, free and open licence)",
                              collection="cop-dem-glo-30", item=item["id"])
            manifest.save()


def submit_missing(root: Path, hyp3: Hyp3, state: dict[str, Any]) -> int:
    submitted = 0
    for key, track in state["tracks"].items():
        existing = {tuple(j["job_parameters"]["granules"]) for j in hyp3.jobs(track["name"])
                    if j.get("status_code") != "FAILED"}
        missing = [tuple(p) for p in track["pairs"] if tuple(p) not in existing]
        if missing:
            need = len(missing)
            if hyp3.credits() < need:
                raise RuntimeError(f"HyP3 credits 不足：{key} 需要 {need}")
            hyp3.submit(track["name"], missing)
            submitted += need
            logger.info("%s: submitted %d jobs", key, need)
    return submitted


def collect(root: Path, hyp3: Hyp3, state: dict[str, Any], *, workers: int = 4) -> dict[str, int]:
    """Download (parallel) and extract (sequential) finished products; returns counts by status."""
    from concurrent.futures import ThreadPoolExecutor

    manifest = Manifest(root)
    counts: dict[str, int] = {}
    todo = []
    for key, track in state["tracks"].items():
        wanted = {tuple(p) for p in track["pairs"]}
        for job in hyp3.jobs(track["name"]):
            pair = tuple(job["job_parameters"]["granules"])
            if pair not in wanted:
                continue
            status = job.get("status_code", "UNKNOWN")
            counts[status] = counts.get(status, 0) + 1
            if status != "SUCCEEDED":
                continue
            pair_id = f"{pair[0][14:22]}_{pair[1][14:22]}"
            product_dir = root / "products" / track["site"] / track["burst"] / pair_id
            if (product_dir / ".complete").is_file():
                # 补登记：已完成产物若有文件缺清单条目（并发写入丢失），按文件重新登记哈希
                for target in product_dir.iterdir():
                    entry_id = f"{key}/{pair_id}/{target.name}"
                    if target.name != ".complete" and entry_id not in manifest:
                        manifest.add_file(entry_id, target, source_url=job["files"][0]["url"], kind="insar_product",
                                          license="Contains modified Copernicus Sentinel data; ASF HyP3",
                                          site=track["site"], burst=track["burst"], hyp3_job_id=job["job_id"],
                                          granules=list(pair), looks=LOOKS, reconciled=True)
                continue
            todo.append((key, track, job, pair, pair_id, product_dir))

    def download(item):
        info = item[2]["files"][0]
        return fetch(info["url"], root / "downloads" / info["filename"], expected_size=int(info["size"]))

    manifest.save()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [(item, pool.submit(download, item)) for item in todo]
        for (key, track, job, pair, pair_id, product_dir), future in futures:
            try:
                zpath = future.result()
            except OSError as exc:  # only this product is retried next round
                logger.warning("product %s/%s not downloaded: %s", key, pair_id, exc)
                continue
            info = job["files"][0]
            try:
                with zipfile.ZipFile(zpath) as archive:
                    bad = archive.testzip()
                if bad:
                    raise zipfile.BadZipFile(f"CRC 校验失败：{bad}")
            except (zipfile.BadZipFile, OSError) as exc:
                # 大小相符但内容损坏（如并发写入）：删除压缩包，下一轮只重下这一个
                logger.warning("product %s/%s zip corrupt, will re-download: %s", key, pair_id, exc)
                zpath.unlink(missing_ok=True)
                continue
            product_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zpath) as archive:
                for member in archive.namelist():
                    if member.endswith(KEEP_SUFFIXES):
                        target = product_dir / Path(member).name
                        with archive.open(member) as src, target.open("wb") as dst:
                            dst.write(src.read())
                        manifest.add_file(f"{key}/{pair_id}/{target.name}", target,
                                          source_url=info["url"], kind="insar_product",
                                          license="Contains modified Copernicus Sentinel data; ASF HyP3",
                                          site=track["site"], burst=track["burst"], hyp3_job_id=job["job_id"],
                                          granules=list(pair), looks=LOOKS)
            manifest.save()
            (product_dir / ".complete").write_text(job["job_id"])
            zpath.unlink(missing_ok=True)  # extracted files are hashed; the zip is not reused
    return counts


def run(root: Path, *, poll_sec: float = 600.0, once: bool = False) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    check_planetary_computer(root)
    state = plan(root)
    record_references(root)
    hyp3 = Hyp3(earthdata_token())
    submit_missing(root, hyp3, state)  # HyP3 处理需数小时，先提交

    import threading

    def dem_worker() -> None:  # DEM 源较慢：后台下载，不阻塞产物收集
        for _ in range(20):
            try:
                download_dem(root)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("DEM download incomplete, will retry: %s", exc)
                time.sleep(60)
    dem_thread = threading.Thread(target=dem_worker, daemon=True)
    dem_thread.start()
    while True:
        counts = collect(root, hyp3, state)
        total = sum(len(t["pairs"]) for t in state["tracks"].values())
        logger.info("HyP3 status %s / %d pairs", counts, total)
        (root / "acquisition_status.json").write_text(json.dumps(
            {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "counts": counts, "pairs": total}))
        pending = sum(v for k, v in counts.items() if k in ("PENDING", "RUNNING"))
        if once or (not pending and not dem_thread.is_alive()):
            return {"counts": counts, "pairs": total, "verify": Manifest(root).verify()}
        time.sleep(poll_sec)
