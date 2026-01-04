from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Any, Optional, Tuple

from tqdm import tqdm

from .lpm.atomic_io import safe_write


def read_json(p: Path) -> Any:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(p: Path, obj: Any) -> None:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    safe_write(p, payload, mode=0o644)


def _content_disposition_filename(header: str | None) -> Optional[str]:
    if not header:
        return None

    msg = Message()
    msg["content-disposition"] = header
    filename = msg.get_filename()
    if not filename:
        return None

    try:
        filename = str(make_header(decode_header(filename)))
    except Exception:
        # If decoding fails, fall back to the raw value.
        pass

    return Path(filename).name


def urlread(
    url: str,
    timeout: float | None = 10,
    *,
    cafile: str | bytes | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> Tuple[bytes, Optional[str]]:
    context = ssl_context
    if context is None and cafile:
        context = ssl.create_default_context(cafile=cafile)
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as r:
            meta_filename = _content_disposition_filename(r.headers.get("content-disposition"))
            final_url = r.geturl()
            total = int(r.headers.get("content-length", 0) or 0)
            if total == 0:
                data = r.read()
                return data, meta_filename or final_url
            chunk_size = 1 << 14
            data = bytearray()
            with tqdm(total=total, desc="Downloading", unit="B", unit_scale=True, ncols=80, colour="cyan") as bar:
                while True:
                    chunk = r.read(chunk_size)
                    if not chunk:
                        break
                    data.extend(chunk)
                    bar.update(len(chunk))
            return bytes(data), meta_filename or final_url
    except urllib.error.URLError as e:
        detail = ""
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            detail = f" (SSL error: {e.reason})"
        raise RuntimeError(f"Failed to read URL {url}{detail}") from e
    except ssl.SSLError as e:
        raise RuntimeError(f"Failed to read URL {url} (SSL error: {e})") from e


__all__ = ["read_json", "write_json", "urlread"]
