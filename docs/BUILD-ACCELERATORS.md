# Large-package build accelerators

Install ccache or sccache in the build environment, then rebuild LPM from the
revision containing the accelerators module. Set in /etc/lpm/lpm.conf:

    LPM_COMPILER_CACHE=ccache
    LPM_COMPILER_CACHE_SIZE=10G
    LPM_LINK_JOBS=1

The default is `auto`: ccache first, then sccache, otherwise no cache. Explicitly
requesting a missing backend fails early. `none` disables it. ccache size defaults
to 10 GiB; sccache size/server settings use its native configuration. Optional
`LPM_COMPILER_CACHE_DIR` overrides persistent storage under LPM's state cache.
Source-tree cleanup does not delete the compiler cache.

Compiler wrappers preserve compiler arguments and cache calls for CC/CXX and
HOSTCC/HOSTCXX. Compiler-content checking is the default for ccache; no sloppiness
options are enabled. Changes to source, headers, flags and compiler are handled
by the cache backend. Correctness-preserving misses are preferable to unsafe hits.
Caches speed repeated compilation, not initial full builds or every linker step.
Do not force ccache into compiler bootstrap builds that require uncached stages;
use LPM_COMPILER_CACHE=none for those recipes/builds.

## Resource limits

Default compile concurrency accounts for CPU affinity, cgroup v2 CPU quota,
MemAvailable and cgroup v2 memory headroom. Twenty percent of available memory
is reserved. These are scheduling estimates, not a guarantee against OOM:

| Profile | Estimated MiB per compile job |
| --- | ---: |
| generic | 768 |
| kernel | 768 |
| qt | 1536 |

Names linux/kernel/linux-lts/linux-zen select kernel; qt5*/qt6*/qtbase* select qt.
Override with LPM_BUILD_PROFILE=generic/kernel/qt, or LPM_MEMORY_PER_JOB_MB.
Explicit LPM_BUILD_JOBS takes precedence over the estimate. Existing MAKEFLAGS,
CMAKE_BUILD_PARALLEL_LEVEL and CMAKE_TOOLCHAIN_FILE are preserved. Recipes that
hardcode `-j$(nproc)` override the resource budget: change those invocations to
`-j"$LPM_BUILD_JOBS"` or `--parallel "$LPM_BUILD_JOBS"`.

For CMake/Ninja, a generated toolchain file declares compile and link job pools,
with one link job by default. Override LPM_LINK_JOBS if memory allows. This
applies to fresh CMake configuration, not an already configured build directory.
If supplying your own toolchain, include `$ENV{LPM_CMAKE_JOB_POOLS_FILE}` there
(or merge the pool settings manually). Project-specific pool overrides win.
Raw Ninja and Meson projects retain their own scheduling; no unsupported global
link concurrency flag is injected. LTO may still spawn linker-internal workers.

## Linux kernel recipe

Kbuild may override environment CC/HOSTCC, so use the generated helper, which
passes configured compilers as make arguments:

    build() {
        set -euxo pipefail
        cd "$SRCROOT/${NAME}-${VERSION}"
        "$LPM_KERNEL_MAKE" O=_build olddefconfig
        "$LPM_KERNEL_MAKE" O=_build all
    }

Its final arguments may override defaults. Pass every relevant Kbuild setting
consistently (O=, LLVM=, architecture, and so on). For cross builds or LLVM, export
CC/CXX/CROSS_COMPILE in LPM's environment before the build; do not change the
compiler only after LPM constructs its wrappers. Existing custom commands such
as `CC="ccache clang"` are left unwrapped. No ISA or kernel configuration changes
are made by the accelerator.

## Qt/CMake recipe

    build() {
        set -euxo pipefail
        cd "$SRCROOT/${NAME}-${VERSION}"
        cmake -S . -B _build -G Ninja \
            -DCMAKE_INSTALL_PREFIX=/usr \
            -DCMAKE_BUILD_TYPE=Release
        cmake --build _build --parallel "$LPM_BUILD_JOBS"
    }

CC/CXX wrappers are picked up on the first configuration. Avoid enabling a second
compiler launcher on top of these wrappers. Qt module dependencies and configure
features remain controlled by the package recipe.

## Sandbox and verification

Bubblewrap receives a writable bind of only the compiler-cache directory and
uses /build/accelerators wrappers/toolchain settings. The rest of the host stays
read-only. fakeroot/unsandboxed builds use the same persistent cache on the host.
This does not enable simultaneous package builds or database writers.

Inspect a cache using its configured directory:

    CCACHE_DIR=/var/lib/lpm/cache/compiler/ccache ccache --show-stats

Validation includes real GCC compilation through the wrappers and a real ccache
cold/repeat test: first invocation missed, second was a direct cache hit. This
is not a kernel/Qt build-time benchmark. No complete kernel or Qt build has been
performed as part of this change.
