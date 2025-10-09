PYTHON ?= python3
NUITKA ?= $(PYTHON) -m nuitka
NUITKA_REPO ?= https://github.com/BobTheZombie/Nuitka.git
NUITKA_REF ?= develop
APP = lpm
ENTRY = $(PWD)/lpm.py
BUILD_DIR = build/nuitka
DIST_DIR = dist
SRC_FILES := $(shell find src -type f -name '*.py')
VERSION ?= $(shell $(PYTHON) tools/get_version.py)

NUITKA_FLAGS ?= \
        --onefile \
        --include-package=src \
        --follow-imports \
        --lto=yes \
        --jobs=$(shell nproc) \
        --python-flag=-O

STATIC_LIBPYTHON ?= auto

PYTHON_STATIC_LIB := $(strip $(shell $(PYTHON) -c "import sysconfig as s, pathlib as p; libname=s.get_config_var('LIBRARY'); bases=(s.get_config_var('LIBPL'), s.get_config_var('LIBDIR')); candidates=[str(p.Path(base)/libname) for base in bases if base and libname and (p.Path(base)/libname).exists()]; print(candidates[0] if candidates else '', end='')"))

ifeq ($(STATIC_LIBPYTHON),yes)
    ifeq ($(PYTHON_STATIC_LIB),)
        $(error Requested static libpython but none was found for $(PYTHON))
    endif
    $(info Using static libpython: $(PYTHON_STATIC_LIB))
    NUITKA_FLAGS += --static-libpython=yes
else ifeq ($(STATIC_LIBPYTHON),no)
    # Intentionally left blank - rely on the shared libpython.
else ifeq ($(STATIC_LIBPYTHON),auto)
    ifneq ($(PYTHON_STATIC_LIB),)
        $(info Using static libpython: $(PYTHON_STATIC_LIB))
        NUITKA_FLAGS += --static-libpython=yes
    endif
else
    $(error Invalid STATIC_LIBPYTHON value: $(STATIC_LIBPYTHON). Use yes, no, or auto.)
endif

export PYTHONPATH := $(PWD)$(if $(PYTHONPATH),:$(PYTHONPATH),)

PREFIX ?= /usr/local

BIN_TARGET = $(BUILD_DIR)/$(APP).bin
STAGING_DIR = $(DIST_DIR)/$(APP)-$(VERSION)
TARBALL = $(DIST_DIR)/$(APP)-$(VERSION).tar.gz
HOOK_SRC = usr/share/lpm/hooks
LIBLPM_HOOK_SRC = usr/libexec/lpm/hooks
NUITKA_SOURCE_DIR ?= build/nuitka-src
NUITKA_STAMP_FILE := $(abspath $(NUITKA_SOURCE_DIR)/.installed-commit)

.PHONY: all stage tarball clean distclean nuitka-install install
.ONESHELL:

all: $(BIN_TARGET)

$(NUITKA_SOURCE_DIR):
	@mkdir -p $(dir $(NUITKA_SOURCE_DIR))
	@if [ -d $(NUITKA_SOURCE_DIR)/.git ]; then \
		git -C $(NUITKA_SOURCE_DIR) remote set-url origin $(NUITKA_REPO); \
	else \
		git clone --depth=1 --branch $(NUITKA_REF) $(NUITKA_REPO) $(NUITKA_SOURCE_DIR); \
	fi
	@git -C $(NUITKA_SOURCE_DIR) fetch origin $(NUITKA_REF)
	@git -C $(NUITKA_SOURCE_DIR) checkout $(NUITKA_REF)
	@git -C $(NUITKA_SOURCE_DIR) reset --hard origin/$(NUITKA_REF)

nuitka-install: $(NUITKA_STAMP_FILE)
        @:

$(NUITKA_STAMP_FILE): | $(NUITKA_SOURCE_DIR)
	@mkdir -p $(dir $@)
	@REV=$$(git -C $(NUITKA_SOURCE_DIR) rev-parse HEAD); \
	INSTALLED=$$(cat $@ 2>/dev/null || true); \
	if [ "$$REV" != "$$INSTALLED" ]; then \
		$(PYTHON) -m pip install --upgrade pip wheel; \
		cd $(NUITKA_SOURCE_DIR) && $(PYTHON) -m pip install --upgrade .; \
		echo "$$REV" > "$@"; \
	else \
		printf 'Nuitka already installed at %s; skipping reinstall.\n' "$$REV"; \
	fi

$(BIN_TARGET): lpm.py $(SRC_FILES) | nuitka-install
	@mkdir -p $(BUILD_DIR)
	$(NUITKA) $(NUITKA_FLAGS) --output-dir=$(BUILD_DIR) --output-filename=$(APP).bin $(ENTRY)

$(STAGING_DIR): $(BIN_TARGET) README.md LICENSE etc/lpm/lpm.conf
	@mkdir -p $(DIST_DIR)
	@rm -rf $@
	mkdir -p $@/bin
	cp $(BIN_TARGET) $@/bin/$(APP)
	mkdir -p $@/usr/share/lpm
	cp -R $(HOOK_SRC) $@/usr/share/lpm/
	mkdir -p $@/usr/share/liblpm
	cp -R usr/share/liblpm/hooks $@/usr/share/liblpm/
	mkdir -p $@/usr/libexec/lpm
	cp -R $(LIBLPM_HOOK_SRC) $@/usr/libexec/lpm/
	mkdir -p $@/etc/lpm
	cp etc/lpm/lpm.conf $@/etc/lpm/lpm.conf
	cp README.md LICENSE $@
	cat <<-'INSTALL_SH' > $@/install.sh
	#!/bin/sh
	set -eu
	PREFIX="$${PREFIX:-/usr/local}"
	DESTDIR="$${DESTDIR:-}"
	ROOT="$$(CDPATH= cd -- "$$(dirname "$$0")" && pwd)"
	
	mkdir -p "$${DESTDIR}$${PREFIX}/bin"
	install -m 0755 "$${ROOT}/bin/lpm" "$${DESTDIR}$${PREFIX}/bin/lpm"
	
	HOOK_DEST="$${DESTDIR}/usr/share/lpm"
	rm -rf "$${HOOK_DEST}/hooks"
	mkdir -p "$${HOOK_DEST}"
	cp -R "$${ROOT}/usr/share/lpm/hooks" "$${HOOK_DEST}/"

	LIBLPM_HOOK_DEST="$${DESTDIR}/usr/share/liblpm"
	rm -rf "$${LIBLPM_HOOK_DEST}/hooks"
	mkdir -p "$${LIBLPM_HOOK_DEST}"
	cp -R "$${ROOT}/usr/share/liblpm/hooks" "$${LIBLPM_HOOK_DEST}/"

	EXEC_HOOK_DEST="$${DESTDIR}/usr/libexec/lpm"
	rm -rf "$${EXEC_HOOK_DEST}/hooks"
	mkdir -p "$${EXEC_HOOK_DEST}"
	cp -R "$${ROOT}/usr/libexec/lpm/hooks" "$${EXEC_HOOK_DEST}/"
	
	STATE_DIR="$${DESTDIR}/var/lib/lpm"
	mkdir -p "$${STATE_DIR}/cache" "$${STATE_DIR}/snapshots"
	if [ ! -f "$${STATE_DIR}/repos.json" ]; then
	    printf '[]\n' > "$${STATE_DIR}/repos.json"
	fi
	if [ ! -f "$${STATE_DIR}/pins.json" ]; then
	    cat > "$${STATE_DIR}/pins.json" <<'JSON'
	{
	  "hold": [],
	  "prefer": {}
	}
	JSON
	fi
	
	CONF_DIR="$${DESTDIR}/etc/lpm"
	mkdir -p "$${CONF_DIR}"
	CONF_SRC="$${ROOT}/etc/lpm/lpm.conf"
	CONF_DEST="$${CONF_DIR}/lpm.conf"
	if [ ! -f "$${CONF_DEST}" ]; then
	    install -m 0644 "$${CONF_SRC}" "$${CONF_DEST}"
	else
	    printf 'Keeping existing configuration: %s\n' "$${CONF_DEST}"
	fi
	INSTALL_SH
	chmod +x $@/install.sh

$(TARBALL): $(STAGING_DIR)
	mkdir -p $(DIST_DIR)
	tar -C $(DIST_DIR) -czf $@ $(APP)-$(VERSION)

stage: $(STAGING_DIR)

tarball: $(TARBALL)

install: $(STAGING_DIR)
	PREFIX="$(PREFIX)" DESTDIR="$(DESTDIR)" $</install.sh

clean:
	rm -rf $(BUILD_DIR)

distclean: clean
	rm -rf $(DIST_DIR)
