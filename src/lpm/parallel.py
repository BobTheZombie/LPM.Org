"""Bounded native-I/O parallelism; portable across CPUs and affinity limits."""
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from itertools import islice


def cpu_count():
    try:
        available = max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
    # Respect cgroup v2 CPU quotas as well as affinity in build containers.
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            available = min(available, max(1, (int(quota) + int(period) - 1) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return available


def worker_count(kind='io'):
    value = os.environ.get('LPM_BUILD_JOBS' if kind == 'build' else 'LPM_IO_WORKERS')
    if value is not None:
        count = int(value)
        if count < 1:
            raise ValueError('LPM worker counts must be positive')
        return min(count, 256)
    return cpu_count() if kind == 'build' else min(8, cpu_count())


def ordered_map(function, iterable):
    """Preserve ordering and errors without submitting an unbounded file list."""
    workers = worker_count()
    if workers == 1:
        yield from map(function, iterable)
        return
    iterator = iter(iterable)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='lpm-io') as pool:
        while batch := list(islice(iterator, workers * 4)):
            yield from pool.map(function, batch)


def configure_build_environment(env):
    jobs = str(worker_count('build'))
    env['LPM_BUILD_JOBS'] = jobs
    env.setdefault('CMAKE_BUILD_PARALLEL_LEVEL', jobs)
    if not env.get('MAKEFLAGS'):
        env['MAKEFLAGS'] = '-j' + jobs
    return env
