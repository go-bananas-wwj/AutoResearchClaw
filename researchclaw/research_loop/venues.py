"""目标期刊档案：宿主按期刊带出页数、图数、语言与模板（不是科研决策）。"""

from __future__ import annotations

from typing import Any

VENUES: dict[str, dict[str, Any]] = {
    "grsl": {
        "name": "IEEE Geoscience and Remote Sensing Letters",
        "language": "en",
        "template": "grsl",
        "max_pages": 5,
        "figures": [3, 5],
        "abstract_words": [150, 250],
        "notes": [
            "Letters only: at most 5 pages; pages 4-5 carry page charges, papers over 5 pages are not published in GRSL.",
            "English only; IEEEtran journal two-column format; IEEE reference style; include Index Terms.",
            "Figure captions must be understandable without the text; axes use word labels, not only symbols.",
            "Content must be new and significant; experimental data complete with methods and conditions.",
        ],
    },
}


def venue_profile(name: str) -> dict[str, Any]:
    profile = VENUES.get((name or "").lower())
    if profile is None:
        raise ValueError(f"未知目标期刊 {name!r}；已支持：{', '.join(sorted(VENUES))}")
    return {"id": name.lower(), **profile}
