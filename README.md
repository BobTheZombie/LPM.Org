# LPM.Org
The Linux Package Manager

## Optimization

`lpm` can optimize builds based on your CPU and the selected optimization
level. The `/etc/lpm/lpm.conf` file accepts an `OPT_LEVEL` entry (`-Os`,
`-O2`, `-O3`, or `-Ofast`). During package builds the manager detects the CPU
family and automatically sets `-march`/`-mtune` along with `-pipe` and
`-fPIC` plus the configured optimization level for `CFLAGS` and `CXXFLAGS`
while `LDFLAGS` uses only the optimization level. Any `CFLAGS` defined in a
`.lpmbuild` script are appended to the defaults.

## Snapshots
LPM stores filesystem snapshots in `/var/lib/lpm/snapshots`. Configure
`MAX_SNAPSHOTS` in `/etc/lpm/lpm.conf` to limit how many snapshots are kept
(default `10`). Older entries beyond the limit are automatically pruned after
creating a new snapshot. You can trigger cleanup manually with
`lpm snapshots --prune`.

## Bootstrap

Run `lpm bootstrap /path/to/root --include vim openssh` to create a chroot-ready
filesystem tree with verified packages.

## Solver Heuristics

The resolver uses a CDCL SAT solver with VSIDS-style variable scoring and phase
saving. Variable and clause activity decay factors default to `0.95` and
`0.999` respectively, tuned from benchmarks on common dependency sets. Package
repositories can influence decision making by adding `"bias"` and `"decay"`
fields to entries in `repos.json`.

A small benchmark harness is provided at `benchmarks/solver_bench.py`. Run
`python benchmarks/solver_bench.py` to measure resolution speed with the default
tuning.
