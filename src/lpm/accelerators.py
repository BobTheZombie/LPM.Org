"""Optional compiler caches and conservative resource budgets for large builds."""
from __future__ import annotations
from pathlib import Path
import shlex
import shutil
from .parallel import cpu_count

PROFILES = {'generic': 768, 'kernel': 768, 'qt': 1536}


def available_memory_mb():
    limits = []
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                limits.append(int(line.split()[1]) // 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        maximum = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if maximum != 'max':
            current = int(Path('/sys/fs/cgroup/memory.current').read_text())
            limits.append(max(0, (int(maximum)-current)//(1024*1024)))
    except (OSError, ValueError):
        pass
    return min(limits) if limits else None


def positive(value, name):
    number = int(value)
    if number < 1:
        raise ValueError(f'{name} must be positive')
    return number


def configure(env, buildroot, state, *, profile='generic', sandbox=False, report=lambda text: None):
    profile = env.get('LPM_BUILD_PROFILE', profile)
    if profile not in PROFILES:
        raise ValueError('LPM_BUILD_PROFILE must be generic, kernel or qt')
    env['LPM_BUILD_PROFILE'] = profile
    explicit = env.get('LPM_BUILD_JOBS')
    memory = available_memory_mb()
    per_job = positive(env.get('LPM_MEMORY_PER_JOB_MB', PROFILES[profile]), 'LPM_MEMORY_PER_JOB_MB')
    jobs = min(256, positive(explicit, 'LPM_BUILD_JOBS')) if explicit else cpu_count()
    if explicit is None and memory is not None:
        jobs = min(jobs, max(1, int(memory * .8) // per_job))
    env['LPM_BUILD_JOBS'] = str(jobs)
    env.setdefault('CMAKE_BUILD_PARALLEL_LEVEL', str(jobs))
    if not env.get('MAKEFLAGS'):
        env['MAKEFLAGS'] = f'-j{jobs}'
    links = min(jobs, positive(env.get('LPM_LINK_JOBS', '1'), 'LPM_LINK_JOBS'))
    env['LPM_LINK_JOBS'] = str(links)
    directory = Path(buildroot)/'accelerators'
    directory.mkdir(parents=True, exist_ok=True)
    visible = Path('/build/accelerators') if sandbox else directory
    # CMake initializes this from the environment on a fresh configure. An
    # explicit project/user toolchain remains authoritative.
    pools = directory/'job-pools.cmake'
    # CMAKE_JOB_POOLS is ignored when a project explicitly sets JOB_POOLS.
    # Nested LLVM builds append their own pools to that global property.
    # Register ours explicitly, without duplicates when toolchains are reloaded.
    pools.write_text(
        f'set(CMAKE_JOB_POOLS "lpm_compile={jobs};lpm_link={links}" CACHE STRING "LPM Ninja pools")\n'
        'get_property(_lpm_registered_pools GLOBAL PROPERTY JOB_POOLS)\n'
        'if(NOT ";${_lpm_registered_pools};" MATCHES ";lpm_compile=[0-9]+;")\n'
        f'  set_property(GLOBAL APPEND PROPERTY JOB_POOLS "lpm_compile={jobs}")\n'
        'endif()\n'
        'if(NOT ";${_lpm_registered_pools};" MATCHES ";lpm_link=[0-9]+;")\n'
        f'  set_property(GLOBAL APPEND PROPERTY JOB_POOLS "lpm_link={links}")\n'
        'endif()\n'
        'unset(_lpm_registered_pools)\n'
        'set(CMAKE_JOB_POOL_COMPILE lpm_compile CACHE STRING "LPM compile pool")\n'
        'set(CMAKE_JOB_POOL_LINK lpm_link CACHE STRING "LPM link pool")\n'
    )
    env.setdefault('CMAKE_TOOLCHAIN_FILE', str(visible/pools.name))
    env['LPM_CMAKE_JOB_POOLS_FILE'] = str(visible/pools.name)
    kernel_make = directory/'kernel-make'
    kernel_make.write_text('#!/bin/sh\nset -eu\n'
        'set -- -j"$LPM_BUILD_JOBS" "$@"\n'
        'if [ -n "${HOSTCXX:-}" ]; then set -- "HOSTCXX=$HOSTCXX" "$@"; fi\n'
        'if [ -n "${HOSTCC:-}" ]; then set -- "HOSTCC=$HOSTCC" "$@"; fi\n'
        'if [ -n "${CC:-}" ]; then set -- "CC=$CC" "$@"; fi\n'
        'exec make "$@"\n')
    kernel_make.chmod(0o755)
    env['LPM_KERNEL_MAKE'] = str(visible/kernel_make.name)
    mode = env.get('LPM_COMPILER_CACHE', 'auto')
    if mode not in ('auto', 'none', 'ccache', 'sccache'):
        raise ValueError('LPM_COMPILER_CACHE must be auto, none, ccache or sccache')
    cache = None
    if mode != 'none':
        for candidate in (('ccache', 'sccache') if mode == 'auto' else (mode,)):
            cache = shutil.which(candidate, path=env.get('PATH'))
            if cache:
                mode = candidate
                break
        if not cache and mode != 'auto':
            raise RuntimeError(f'Requested compiler cache {mode} is not installed')
    if not cache:
        env['LPM_COMPILER_CACHE_ACTIVE'] = 'none'
        report(f'[accelerators] profile={profile} jobs={jobs} link-jobs={links} cache=none')
        return env
    # User-owned persistent cache, never within the source directory cleaned by
    # prepare(). No relaxed correctness/sloppiness settings are enabled.
    cache_dir = Path(env.get('LPM_COMPILER_CACHE_DIR', str(Path(state)/'cache/compiler'/mode))).expanduser().resolve()
    if cache_dir == Path('/'):
        raise ValueError('Compiler cache cannot be the filesystem root')
    cache_dir.mkdir(parents=True, exist_ok=True)
    env['LPM_COMPILER_CACHE_DIR'] = str(cache_dir)
    env['LPM_COMPILER_CACHE_ACTIVE'] = mode
    runtime_cache = '/lpm-compiler-cache' if sandbox else str(cache_dir)
    env['CCACHE_DIR' if mode == 'ccache' else 'SCCACHE_DIR'] = runtime_cache
    if mode == 'ccache':
        env.setdefault('CCACHE_COMPILERCHECK', 'content')
        env.setdefault('CCACHE_MAXSIZE', env.get('LPM_COMPILER_CACHE_SIZE', '10G'))
    elif sandbox:
        # Keep the sccache daemon's socket and temporary files writable in bwrap.
        env['SCCACHE_SERVER_UDS'] = runtime_cache+'/server.sock'
    prefix = env.get('CROSS_COMPILE', '')
    for variable, default in (('CC', prefix+'gcc' if prefix else 'cc'),
                              ('CXX', prefix+'g++' if prefix else 'c++'),
                              ('HOSTCC','cc'), ('HOSTCXX','c++')):
        parts = shlex.split(env.get(variable, default))
        if not parts:
            raise ValueError(f'Empty compiler command: {variable}')
        if Path(parts[0]).name in ('ccache', 'sccache'):
            continue
        compiler = shutil.which(parts[0], path=env.get('PATH'))
        if not compiler:
            if variable in env:
                raise RuntimeError(f'Cannot resolve {variable} compiler: {parts[0]}')
            continue
        wrapper = directory/(variable.lower()+'-cached')
        wrapper.write_text('#!/bin/sh\nexec '+shlex.join([cache, compiler, *parts[1:]])+' "$@"\n')
        wrapper.chmod(0o755)
        env[variable] = str(visible/wrapper.name)
    report(f'[accelerators] profile={profile} jobs={jobs} link-jobs={links} cache={mode} dir={cache_dir}')
    return env
