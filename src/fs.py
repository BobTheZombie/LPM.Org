from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from tqdm import tqdm


def read_json(p: Path) -> Any:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    tmp.replace(p)


def urlread(url: str) -> bytes:
    with urllib.request.urlopen(url) as r:
        total = int(r.headers.get("content-length", 0) or 0)
        if total == 0:
            return r.read()
        chunk_size = 1 << 14
        data = bytearray()
        with tqdm(total=total, desc="Downloading", unit="B", unit_scale=True, ncols=80, colour="cyan") as bar:
            while True:
                chunk = r.read(chunk_size)
                if not chunk:
                    break
                data.extend(chunk)
                bar.update(len(chunk))
        return bytes(data)


__all__ = ["read_json", "write_json", "urlread"]
