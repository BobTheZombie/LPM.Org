# LPM.Org
The Linux Package Manager

## Optimization

`lpm` can optimize builds based on your CPU and the selected optimization
level. The `/etc/lpm/lpm.conf` file accepts an `OPT_LEVEL` entry (`-Os`,
`-O2`, `-O3`, or `-Ofast`). During package builds the manager detects the CPU
family and automatically sets `-march`/`-mtune` along with the configured
optimization level for `CFLAGS`, `CXXFLAGS`, and `LDFLAGS`.
