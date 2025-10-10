import json
from pathlib import Path

import src.config as config
from src import maintainer_mode
from src.lpm.app import PkgMeta


def _snapshot_config(keys):
    return {key: getattr(config, key) for key in keys}


def _restore_config(snapshot):
    for key, value in snapshot.items():
        setattr(config, key, value)


def test_handle_lpmbuild_publishes_artifacts(tmp_path):
    cfg_keys = [
        "DISTRO_MAINTAINER_MODE",
        "DISTRO_REPO_ROOT",
        "DISTRO_SOURCE_ROOT",
        "DISTRO_LPMBUILD_ROOT",
        "DISTRO_REPO_BASE_URL",
        "DISTRO_NAME",
        "DISTRO_GIT_ENABLED",
        "DISTRO_GIT_ROOT",
        "DISTRO_GIT_REMOTE",
        "DISTRO_GIT_BRANCH",
    ]
    snapshot = _snapshot_config(cfg_keys)
    try:
        repo_root = tmp_path / "repo"
        source_root = tmp_path / "sources"
        lpmbuild_root = tmp_path / "lpmbuilds"
        git_root = tmp_path / "git"
        for directory in (repo_root, source_root, lpmbuild_root, git_root):
            directory.mkdir(parents=True, exist_ok=True)

        config.DISTRO_MAINTAINER_MODE = True
        config.DISTRO_REPO_ROOT = repo_root
        config.DISTRO_SOURCE_ROOT = source_root
        config.DISTRO_LPMBUILD_ROOT = lpmbuild_root
        config.DISTRO_REPO_BASE_URL = "https://mirror.example.com"
        config.DISTRO_NAME = "TestDistro"
        config.DISTRO_GIT_ENABLED = False
        config.DISTRO_GIT_ROOT = git_root
        config.DISTRO_GIT_REMOTE = "origin"
        config.DISTRO_GIT_BRANCH = "main"

        pkg = tmp_path / "foo-1.0-1.x86_64.zst"
        pkg.write_bytes(b"payload")
        pkg.with_suffix(pkg.suffix + ".sig").write_text("sig", encoding="utf-8")
        script = tmp_path / "foo.lpmbuild"
        script.write_text("# sample", encoding="utf-8")
        source_tree = tmp_path / "srcroot"
        source_tree.mkdir()
        (source_tree / "README").write_text("source", encoding="utf-8")

        meta = PkgMeta(name="foo", version="1.0", release="1", arch="x86_64")

        result = maintainer_mode.handle_lpmbuild(
            primary=(meta, pkg),
            split_records=(),
            script_path=script,
            source_tree=source_tree,
        )
        assert result is not None
        arch_dir = repo_root / "x86_64"
        assert (arch_dir / pkg.name).exists()
        assert (arch_dir / (pkg.name + ".sig")).exists()

        metadata_path = lpmbuild_root / "foo" / "1.0" / "build.json"
        assert metadata_path.exists()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["name"] == "foo"
        assert metadata["distribution"] == "TestDistro"

        calls = []

        def fake_gen(path: Path, base_url: str | None, arch_filter: str | None = None):
            calls.append((path, base_url, arch_filter))
            (path / "index.json").write_text("[]", encoding="utf-8")

        maintainer_mode.generate_indexes(result, fake_gen)
        assert calls == [(arch_dir, "https://mirror.example.com/x86_64", "x86_64")]
        assert (arch_dir / "index.json") in result.staged_after_index

        maintainer_mode.finalize_git(result)  # git disabled -> no-op
    finally:
        _restore_config(snapshot)
