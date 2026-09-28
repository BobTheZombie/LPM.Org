#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SELF_DIR/../.." && pwd)"

JHALFS_REPO="${JHALFS_REPO:-https://git.linuxfromscratch.org/jhalfs.git}"
JHALFS_REF="${JHALFS_REF:-master}"
WORK_DIR="${WORK_DIR:-$REPO_ROOT/build/jhalfs-lpm}"
JHALFS_SRC="${JHALFS_SRC:-$WORK_DIR/jhalfs-src}"
LFS_ROOT="${LFS_ROOT:-/mnt/lpm-lfs}"
LPM_BINARY="${LPM_BINARY:-$REPO_ROOT/build/nuitka/lpm.bin}"
PACKAGE_REPO="${PACKAGE_REPO:-https://gitlab.com/lpm-org/packages.git}"
PACKAGE_REF="${PACKAGE_REF:-main}"
JOBS="${JOBS:-$(nproc)}"

usage() {
    cat <<EOF
Usage: $0 COMMAND

Commands:
  prepare    Clone jhalfs and install the LPM custom profile
  configure  Open jhalfs configuration with LPM-safe defaults
  generate   Generate the LFS command tree and Makefile
  build      Build LFS, then run the LPM post-system stages
  validate   Validate the completed target root
  all        Run prepare, configure (if needed), generate, build, validate

Environment:
  LFS_ROOT       Target root (default: /mnt/lpm-lfs)
  LPM_BINARY     Standalone LPM executable to install into the root
  WORK_DIR       Persistent jhalfs workspace
  PACKAGE_REPO   lpmbuild repository URL
  PACKAGE_REF    lpmbuild repository branch/tag
EOF
}

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "error: required host command not found: $1" >&2
        exit 2
    }
}

read_config_value() {
    local key="$1"
    sed -n "s/^${key}=\"\(.*\)\"$/\1/p" "$JHALFS_SRC/configuration" | tail -n 1
}

prepare() {
    for tool in git make python3 sudo xsltproc wget; do
        need "$tool"
    done

    mkdir -p "$WORK_DIR"
    if [[ ! -d "$JHALFS_SRC/.git" ]]; then
        git clone "$JHALFS_REPO" "$JHALFS_SRC"
    fi
    git -C "$JHALFS_SRC" fetch --depth=1 origin "$JHALFS_REF"
    git -C "$JHALFS_SRC" checkout --detach FETCH_HEAD

    mkdir -p "$JHALFS_SRC/custom/config"
    cp -f "$SELF_DIR"/custom/* "$JHALFS_SRC/custom/config/"
    cp -f "$SELF_DIR/config/packages.list" "$WORK_DIR/packages.list"
    echo "Prepared jhalfs profile in $JHALFS_SRC"
}

configure() {
    [[ -d "$JHALFS_SRC/.git" ]] || prepare
    if [[ ! -f "$JHALFS_SRC/configuration" ]]; then
        cp "$SELF_DIR/config/jhalfs.config" "$JHALFS_SRC/configuration"
    fi

    echo "Configure jhalfs with these required settings:"
    echo "  Book: LFS systemd"
    echo "  Build method: chroot"
    echo "  Build directory: $LFS_ROOT"
    echo "  Retrieve sources: yes"
    echo "  Add custom tools support: yes"
    echo "  Run the Makefile: no"
    (
        cd "$JHALFS_SRC"
        CONFIG_="" KCONFIG_CONFIG=configuration \
            python3 menu/menuconfig.py Config.in
    )
}

stage_inputs() {
    [[ -x "$LPM_BINARY" ]] || {
        echo "error: standalone LPM binary not found: $LPM_BINARY" >&2
        echo "Build it first with: make build/nuitka/lpm.bin" >&2
        exit 2
    }

    sudo install -d -m755 "$LFS_ROOT/sources/lpm-bootstrap"
    sudo install -m755 "$LPM_BINARY" "$LFS_ROOT/sources/lpm-bootstrap/lpm"
    sudo install -m644 "$WORK_DIR/packages.list" \
        "$LFS_ROOT/sources/lpm-bootstrap/packages.list"
    sudo install -m644 "$REPO_ROOT/etc/lpm/lpm.conf" \
        "$LFS_ROOT/sources/lpm-bootstrap/lpm.conf"

    sudo mkdir -p "$LFS_ROOT/sources/lpm-bootstrap/share" \
        "$LFS_ROOT/sources/lpm-bootstrap/libexec"
    sudo cp -a "$REPO_ROOT/usr/share/liblpm" \
        "$LFS_ROOT/sources/lpm-bootstrap/share/"
    sudo cp -a "$REPO_ROOT/usr/libexec/lpm" \
        "$LFS_ROOT/sources/lpm-bootstrap/libexec/"

    if [[ ! -d "$WORK_DIR/packages/.git" ]]; then
        git clone --branch "$PACKAGE_REF" --depth=1 \
            "$PACKAGE_REPO" "$WORK_DIR/packages"
    else
        git -C "$WORK_DIR/packages" fetch --depth=1 origin "$PACKAGE_REF"
        git -C "$WORK_DIR/packages" checkout -B "$PACKAGE_REF" FETCH_HEAD
    fi
    sudo mkdir -p "$LFS_ROOT/sources/lpm-packages"
    sudo cp -a "$WORK_DIR/packages/." "$LFS_ROOT/sources/lpm-packages/"
}

generate() {
    [[ -f "$JHALFS_SRC/configuration" ]] || {
        echo "error: run '$0 configure' first" >&2
        exit 2
    }

    # Keep generation separate so bootstrap inputs can be staged before make.
    sed -i 's/^RUNMAKE=.*/RUNMAKE=n/' "$JHALFS_SRC/configuration"
    sed -i 's/^CUSTOM_TOOLS=.*/CUSTOM_TOOLS=y/' "$JHALFS_SRC/configuration"
    sed -i 's|^BUILDDIR=.*|BUILDDIR="'"$LFS_ROOT"'"|' "$JHALFS_SRC/configuration"

    (
        cd "$JHALFS_SRC"
        yes yes | ./jhalfs run
    )
    stage_inputs
}

build() {
    local generated_dir
    generated_dir="$(read_config_value JHALFSDIR)"
    [[ -n "$generated_dir" ]] || generated_dir="$LFS_ROOT/jhalfs"
    # jhalfs stores literal variable references in some configurations.
    generated_dir="${generated_dir//\$BUILDDIR/$LFS_ROOT}"
    generated_dir="${generated_dir//\$SCRIPT_ROOT/jhalfs}"
    [[ -f "$generated_dir/Makefile" ]] || {
        echo "error: generated Makefile missing; run '$0 generate' first" >&2
        exit 2
    }
    # The generated graph uses sudo only for the privileged steps. Running the
    # whole graph as root would make its logs and resumable state root-owned.
    make -C "$generated_dir" -j"$JOBS"
}

validate() {
    sudo env LFS_ROOT="$LFS_ROOT" "$SELF_DIR/scripts/validate-root.sh"
}

command="${1:-}"
case "$command" in
    prepare) prepare ;;
    configure) configure ;;
    generate) generate ;;
    build) build ;;
    validate) validate ;;
    all)
        prepare
        [[ -f "$JHALFS_SRC/configuration" ]] || configure
        generate
        build
        validate
        ;;
    -h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
esac
