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


def enqueue(state, script, outdir, meta, dependencies, overrides=None, final_dependencies=()):
    validate_dependencies(dependencies)
    validate_dependencies(final_dependencies)
    directory = Path(state) / "stage2"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(Path(script).read_bytes()).hexdigest()
    key = hashlib.sha256(f"{meta.name}:{meta.version}:{meta.release}:{meta.arch}".encode()).hexdigest()
    record = dict(name=meta.name, version=meta.version, release=meta.release,
                  arch=meta.arch, script=str(Path(script).resolve()), digest=digest,
                  outdir=str(Path(outdir).resolve()), dependencies=dependencies,
                  final_dependencies=list(final_dependencies),
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
            and (stage is None or meta.get("build_stage", 0) == stage))


def plan(directory, installed, fetch, read_recipe):
    """Expand strict final-stage dependencies and reject cycles before compiling.

    An edge A -> B means A's final build waits for B's final installation.
    Ordinary deferred dependencies remain bootstrap-capable.
    """
    directory = Path(directory)
    jobs = {}
    for path in sorted(directory.glob("*.json")):
        job = json.loads(path.read_text())
        if job["name"] in jobs and jobs[job["name"]] != job:
            raise RuntimeError("Multiple queued revisions for " + job["name"])
        jobs[job["name"]] = job
    order, visiting, done = [], [], set()

    def visit(name):
        if name in visiting:
            cycle = visiting[visiting.index(name):] + [name]
            raise RuntimeError("Stage 2 dependency cycle: " + " -> ".join(n + "@2" for n in cycle))
        if name in done:
            return
        visiting.append(name)
        job = jobs[name]
        script = Path(job["script"])
        if not script.is_file() or hashlib.sha256(script.read_bytes()).hexdigest() != job["digest"]:
            raise RuntimeError(f"Stage 2 recipe changed or is missing: {script}; rebuild its bootstrap package")
        for dep in job.get("final_dependencies", []):
            validate_dependencies([dep])
            if dep not in jobs:
                dep_script = directory / (dep + ".lpmbuild")
                fetch(dep, dep_script)
                scal, arrays, _ = read_recipe(dep_script, build_stage=1)
                if scal.get("NAME") != dep:
                    raise RuntimeError("Stage 2 dependency must identify a package, not a provider: " + dep)
                strict = arrays.get("STAGE_2_BUILD_REQUIRES", [])
                ordinary = list(dict.fromkeys(arrays.get("STAGE_2_REQUIRES", []) + strict))
                if not ordinary:
                    raise RuntimeError("Required package has no stage 2 recipe: " + dep)
                validate_dependencies(ordinary)
                jobs[dep] = dict(name=dep, version=scal["VERSION"], release=scal.get("RELEASE", "1"),
                    arch=scal.get("ARCH", job["arch"]), script=str(dep_script.resolve()),
                    digest=hashlib.sha256(dep_script.read_bytes()).hexdigest(),
                    dependencies=ordinary, final_dependencies=strict,
                    outdir=job["outdir"], overrides=job["overrides"])
            visit(dep)
        visiting.pop()
        done.add(name)
        order.append(name)

    for name in list(jobs):
        visit(name)
    return jobs, order


def drain(state, installed, build, fetch, report=lambda message: None, *, read_recipe=None, plan_only=False):
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
        planned = {}
        if read_recipe:
            planned, order = plan(directory, installed(), fetch, read_recipe)
            graph = {"order": [n + "@2" for n in order], "edges": [
                {"package": name + "@2", "requires": dep + "@2"}
                for name, job in planned.items() for dep in job.get("final_dependencies", [])]}
            if plan_only:
                return graph
            report("[stage2] final build order: " + " -> ".join(graph["order"])
                   if graph["order"] else "[stage2] no pending final rebuilds")
            # Persist discovered jobs before any compilation, for crash recovery.
            with (directory / "queue.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                for name in order:
                    job = planned[name]
                    key = hashlib.sha256(f"{name}:{job['version']}:{job['release']}:{job['arch']}".encode()).hexdigest()
                    path = directory / (key + ".json")
                    if not path.exists():
                        temp = path.with_suffix(".tmp")
                        temp.write_text(json.dumps(job))
                        temp.replace(path)
        attempted = set()
        while True:
            jobs = [path for path in sorted(directory.glob("*.json"))
                    if path not in attempted]
            if not jobs:
                break
            if read_recipe:
                planned, order = plan(directory, installed(), fetch, read_recipe)
                rank = {name: i for i, name in enumerate(order)}
                jobs.sort(key=lambda p: rank[json.loads(p.read_text())["name"]])
            for path in jobs:
                attempted.add(path)
                job = json.loads(path.read_text(encoding="utf-8"))
                current = installed()
                base = current.get(job["name"])
                if _matches(base, job, 2):
                    path.unlink()
                    continue
                if job["name"] in planned and not base:
                    build(Path(job["script"]), Path(job["outdir"]), 1, job["overrides"])
                    base = installed().get(job["name"])
                # Refused bootstrap installs and superseded versions stay pending.
                if not (_matches(base, job, 1) or
                        (_matches(base, job) and isinstance(base, dict) and base.get("build_stage", 0) == 0)):
                    report(f"[stage2] waiting for matching bootstrap installation: {job['name']}")
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
                if any(not _matches(current.get(dep), planned[dep], 2)
                       for dep in job.get("final_dependencies", [])):
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
