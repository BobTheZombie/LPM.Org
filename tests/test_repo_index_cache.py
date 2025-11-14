import contextlib
import json
from typing import List

from lpm import app


def _make_repo(name: str = "main", url: str = "https://example.invalid/repo") -> app.Repo:
    return app.Repo(name=name, url=url, priority=10, bias=1.0, decay=0.95)


def test_fetch_repo_index_uses_in_memory_cache(monkeypatch):
    calls: List[str] = []
    payload = json.dumps({
        "packages": [
            {
                "name": "alpha",
                "version": "1.0",
                "release": "1",
                "arch": "noarch",
            }
        ]
    }).encode("utf-8")

    def fake_urlread(url: str, timeout: float | None = 10):  # pragma: no cover - signature matches stub
        calls.append(url)
        return payload, None

    original_resolver = app._resolve_lpm_attr

    def fake_resolver(name, default):
        if name == "urlread":
            return fake_urlread
        return original_resolver(name, default)

    monkeypatch.setattr(app, "_resolve_lpm_attr", fake_resolver)
    app.invalidate_repo_index_cache()
    repo = _make_repo()

    first = app.fetch_repo_index(repo)
    second = app.fetch_repo_index(repo)

    assert len(calls) == 1
    assert first[0].name == "alpha"
    assert first[0] is not second[0]
    app.invalidate_repo_index_cache()


def test_save_repos_invalidates_repo_cache(monkeypatch):
    call_count = 0

    def fake_urlread(url: str, timeout: float | None = 10):  # pragma: no cover - signature matches stub
        nonlocal call_count
        version = f"1.{call_count}"
        call_count += 1
        payload = json.dumps({
            "packages": [
                {
                    "name": "alpha",
                    "version": version,
                    "release": "1",
                    "arch": "noarch",
                }
            ]
        }).encode("utf-8")
        return payload, None

    original_resolver = app._resolve_lpm_attr

    def fake_resolver(name, default):
        if name == "urlread":
            return fake_urlread
        return original_resolver(name, default)

    monkeypatch.setattr(app, "_resolve_lpm_attr", fake_resolver)
    monkeypatch.setattr(app, "operation_phase", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(app, "write_json", lambda *a, **k: None)
    app.invalidate_repo_index_cache()

    repo = _make_repo()

    first = app.fetch_repo_index(repo)
    assert call_count == 1

    cached = app.fetch_repo_index(repo)
    assert call_count == 1
    assert cached[0].version == first[0].version

    app.save_repos([repo])

    refreshed = app.fetch_repo_index(repo)
    assert call_count == 2
    assert refreshed[0].version != first[0].version
    app.invalidate_repo_index_cache()
