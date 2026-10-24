"""Conservative input sizing and lossless per-paper reading partitions."""
from __future__ import annotations

import json


def input_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def pack(items, limit, overhead=2048):
    """Keep every item in order; a single oversized item is a configuration error."""
    groups, group, size = [], [], overhead
    for item in items:
        amount = input_bytes(item) + 2
        if amount + overhead > limit:
            raise ValueError("A source item exceeds the configured input capacity")
        if group and size + amount > limit:
            groups.append(group); group, size = [], overhead
        group.append(item); size += amount
    if group:
        groups.append(group)
    return groups
