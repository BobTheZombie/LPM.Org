"""Persistent deferred builds, run by the build scheduler after install commits.

Install hooks never compile code. Recipes and local sources must remain available
until their queued final build completes. Each recipe revision has a separate job.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import re
from pathlib import Path


def validate_dependencies(dependencies):
    for dependency in dependencies:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]*", dependency):
            raise ValueError("STAGE_2_REQUIRES accepts package names only: " + dependency)


def enqueue(state, script, outdir, meta, dependencies, overrides=None):
    validate_dependencies(dependencies)
    directory = Path(state) / "stage2"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(Path(script).read_bytes()).hexdigest()
    key = hashlib.sha256(f"{meta.name}:{meta.version}:{meta.release}:{meta.arch}".encode()).hexdigest()
    record = dict(name=meta.name, version=meta.version, release=meta.release,
                  arch=meta.arch, script=str(Path(script).resolve()), digest=digest,
                  outdir=str(Path(outdir).resolve()), dependencies=dependencies,
                  overrides=dataclasses.asdict(overrides) if overrides else None)
    with (directory / "queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        temporary = directory / (key + ".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(directory / (key + ".json"))


def _matches(meta, job, stage=None):
    if not meta:
        return False
    if not isinstance(meta, dict):
        meta = dataclasses.asdict(meta)
    return (all(str(meta.get(key, "")) == str(job[key])
                for key in ("version", "release", "arch"))
            and (stage is None or meta.get("build_stage", 2) == stage))


def drain(state, installed, build, fetch, report=lambda message: None):
    """Return final build results; retain declined/failed jobs for retry.

    A nonblocking scheduler lock prevents two builders rebuilding the same job.
    The queue lock is held only for mutations, so nested dependency builds may
    safely enqueue their own final passes.
    """
    directory = Path(state) / "stage2"
    if not directory.exists():
        return {}
    results = {}
    with (directory / "scheduler.lock").open("a") as scheduler_lock:
        try:
            fcntl.flock(scheduler_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return results
        attempted = set()
        while True:
            jobs = [path for path in sorted(directory.glob("*.json"))
                    if path not in attempted]
            if not jobs:
                break
            for path in jobs:
                attempted.add(path)
                job = json.loads(path.read_text(encoding="utf-8"))
                current = installed()
                base = current.get(job["name"])
                if _matches(base, job, 2):
                    path.unlink()
                    continue
                # Refused bootstrap installs and superseded versions stay pending.
                if not _matches(base, job, 1):
                    continue
                script = Path(job["script"])
                if not script.is_file() or hashlib.sha256(script.read_bytes()).hexdigest() != job["digest"]:
                    raise RuntimeError(f"Stage 2 recipe changed or is missing: {script}; rebuild its bootstrap package")
                for dependency in job["dependencies"]:
                    current = installed()
                    capabilities = set(current)
                    for meta in current.values():
                        provides = meta.get("provides", []) if isinstance(meta, dict) else meta.provides
                        capabilities.update(re.split(r"[<>= ]", cap, 1)[0] for cap in provides)
                    if dependency in capabilities:
                        continue
                    dep_script = directory / (dependency + ".lpmbuild")
                    fetch(dependency, dep_script)
                    build(dep_script, Path(job["outdir"]), 1, job["overrides"])
                current = installed()
                capabilities = set(current)
                for meta in current.values():
                    provides = meta.get("provides", []) if isinstance(meta, dict) else meta.provides
                    capabilities.update(re.split(r"[<>= ]", cap, 1)[0] for cap in provides)
                if not all(dep in capabilities for dep in job["dependencies"]):
                    continue
                report(f"[stage2] rebuilding {job["name"]} after deferred dependencies were installed")
                result = build(script, Path(job["outdir"]), 2, job["overrides"])
                results[job["name"]] = result
                if _matches(installed().get(job["name"]), job, 2):
                    with (directory / "queue.lock").open("a") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        # A concurrent bootstrap may have replaced the job.
                        if path.exists() and json.loads(path.read_text()) == job:
                            path.unlink()
    return results
