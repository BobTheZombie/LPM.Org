HOST_PYTHON ?= python3
PYTHON ?= $(HOST_PYTHON)
NUITKA ?= $(PYTHON) -m nuitka
NUITKA_REPO ?= https://github.com/BobTheZombie/Nuitka.git
NUITKA_REF ?= develop
APP = lpm
ENTRY = $(PWD)/lpm.py
UI_APP ?= no
UI_ENTRY = $(PWD)/luminosity.py
UI_APP_NAME = Luminosity
BUILD_DIR = build/nuitka
DIST_DIR = dist
SRC_FILES := $(shell find src -type f -name '*.py')
VERSION ?= $(shell $(HOST_PYTHON) tools/get_version.py)

UI_APP_FLAG = $(firstword $(filter --UI_APP=%,$(MAKEFLAGS)))
UI_APP_EFFECTIVE = $(if $(UI_APP_FLAG),$(patsubst --UI_APP=%,%,$(UI_APP_FLAG)),$(UI_APP))

ifneq ($(UI_APP_EFFECTIVE),)
ifneq ($(UI_APP_EFFECTIVE),yes)
ifneq ($(UI_APP_EFFECTIVE),no)
$(error Invalid UI_APP value: $(UI_APP_EFFECTIVE). Use yes or no.)
endif
endif
endif

UI_APP_ENABLED = $(filter yes,$(UI_APP_EFFECTIVE))

NUITKA_FLAGS ?= \
        --onefile \
        --include-package=src \
        --follow-imports \
        --lto=yes \
        --jobs=$(shell nproc) \
        --python-flag=-O

STATIC_LIBPYTHON ?= no

STATIC_LIBPYTHON_FLAG = $(firstword $(filter --static-libpython=%,$(MAKEFLAGS)))
STATIC_LIBPYTHON_EFFECTIVE = $(if $(STATIC_LIBPYTHON_FLAG),$(patsubst --static-libpython=%,%,$(STATIC_LIBPYTHON_FLAG)),$(STATIC_LIBPYTHON))

maybe_static = $(if $(filter yes,$(STATIC_LIBPYTHON_EFFECTIVE)),--static-libpython=yes,$(if $(filter no,$(STATIC_LIBPYTHON_EFFECTIVE)),,$(error Invalid STATIC_LIBPYTHON value: $(STATIC_LIBPYTHON_EFFECTIVE). Use yes or no.)))

# Static Python toolchain configuration -------------------------------------------------------
STATIC_PYTHON_VERSION ?= 3.12.3
STATIC_PYTHON_URL ?= https://www.python.org/ftp/python/$(STATIC_PYTHON_VERSION)/Python-$(STATIC_PYTHON_VERSION).tar.xz
STATIC_PYTHON_BASE := build/static-python
STATIC_PYTHON_TARBALL := $(STATIC_PYTHON_BASE)/Python-$(STATIC_PYTHON_VERSION).tar.xz
STATIC_PYTHON_SRC := $(STATIC_PYTHON_BASE)/Python-$(STATIC_PYTHON_VERSION)
STATIC_PYTHON_PREFIX := $(STATIC_PYTHON_BASE)/install
STATIC_PYTHON_BIN := $(STATIC_PYTHON_PREFIX)/bin/python3
STATIC_PYTHON_BUILD_STAMP := $(STATIC_PYTHON_PREFIX)/.built
STATIC_PYTHON_MODULES_STAMP := $(STATIC_PYTHON_PREFIX)/.modules
STATIC_PYTHON_SETUP_STDLIB := $(abspath $(STATIC_PYTHON_SRC))/Modules/Setup.stdlib
STATIC_PYTHON_SETUP_LOCAL := $(abspath $(STATIC_PYTHON_SRC))/Modules/Setup.local

STATIC_PYTHON_VERSION_PARTS := $(subst ., ,$(STATIC_PYTHON_VERSION))
STATIC_PYTHON_MAJOR := $(word 1,$(STATIC_PYTHON_VERSION_PARTS))
STATIC_PYTHON_MINOR := $(word 2,$(STATIC_PYTHON_VERSION_PARTS))
STATIC_PYTHON_MAJOR_MINOR := $(STATIC_PYTHON_MAJOR).$(STATIC_PYTHON_MINOR)

STATIC_PYTHON_CONFIGURE_FLAGS ?= \
        --prefix=$(abspath $(STATIC_PYTHON_PREFIX)) \
        --disable-shared \
        --with-static-libpython=yes \
        --enable-optimizations \
        --with-lto \
        --with-ensurepip=install

# Building the auxiliary CPython toolchain with ``LDFLAGS=-static`` causes the
# PGO instrumentation step (--enable-optimizations) to fail because the build
# system needs to create a handful of shared test modules.  Keep the default
# environment minimal so the configure script can decide how to link the
# interpreter while still allowing callers to inject custom flags when
# necessary.
STATIC_PYTHON_ENV ?= CPPFLAGS="" CFLAGS=""

STATIC_PYTHON_READY :=

ifeq ($(STATIC_LIBPYTHON_EFFECTIVE),yes)
STATIC_PYTHON_READY := $(STATIC_PYTHON_MODULES_STAMP)
PYTHON := $(STATIC_PYTHON_BIN)
NUITKA := $(PYTHON) -m nuitka
endif

NUITKA_FLAGS += $(maybe_static)

# Additional modules required when doing a fully static build.  Nuitka can
# sometimes miss dynamic imports when Python's stdlib or third party packages
# are frozen, so enumerate the modules that are used throughout the project to
# make sure they are bundled correctly.
STATIC_MODULES := \
	_hashlib \
	_ssl \
	_decimal \
	_datetime \
	_sha3 \
	_blake2 \
	_struct \
	_socket \
	_random \
	_pickle \
	math \
	sqlite3 \
	zlib \
	argparse \
	collections \
	concurrent \
	contextlib \
	dataclasses \
	email \
	errno \
	fnmatch \
	hashlib \
	heapq \
	importlib \
	io \
	itertools \
	json \
	logging \
	os \
	packaging \
	pathlib \
	re \
	shlex \
	shutil \
	src \
	stat \
	subprocess \
	sys \
	tarfile \
	tempfile \
	time \
	tqdm \
	typing \
	urllib \
	zstandard

STATIC_MODULE_FLAGS := $(addprefix --include-module=,$(STATIC_MODULES))

NUITKA_FLAGS += $(if $(filter yes,$(STATIC_LIBPYTHON_EFFECTIVE)),$(STATIC_MODULE_FLAGS))

export PYTHONPATH := $(PWD)$(if $(PYTHONPATH),:$(PYTHONPATH),)

PREFIX ?= /usr/local

BIN_TARGET = $(BUILD_DIR)/$(APP).bin
ALL_BIN_TARGETS = $(BIN_TARGET)

ifneq ($(UI_APP_ENABLED),)
UI_BIN_TARGET = $(BUILD_DIR)/$(UI_APP_NAME).bin
ALL_BIN_TARGETS += $(UI_BIN_TARGET)
endif
STAGING_DIR = $(DIST_DIR)/$(APP)-$(VERSION)
TARBALL = $(DIST_DIR)/$(APP)-$(VERSION).tar.gz
HOOK_SRC = usr/share/lpm/hooks
LIBLPM_HOOK_SRC = usr/libexec/lpm/hooks
NUITKA_SOURCE_DIR ?= build/nuitka-src
NUITKA_STAMP_FILE := $(abspath $(NUITKA_SOURCE_DIR)/.installed-commit)

.PHONY: static-python

static-python: $(STATIC_PYTHON_READY)
	@:

$(STATIC_PYTHON_TARBALL):
	@mkdir -p $(dir $@)
	@printf 'Downloading Python %s...\n' '$(STATIC_PYTHON_VERSION)'
	@curl -L --fail -o "$@.tmp" "$(STATIC_PYTHON_URL)"
	@mv "$@.tmp" "$@"

$(STATIC_PYTHON_SRC): $(STATIC_PYTHON_TARBALL)
	@mkdir -p $(STATIC_PYTHON_BASE)
	@rm -rf "$(STATIC_PYTHON_SRC)"
	@tar -C $(STATIC_PYTHON_BASE) -xf $<

$(STATIC_PYTHON_BUILD_STAMP): $(STATIC_PYTHON_SRC)
	@printf 'Configuring static Python toolchain...\n'
	@$(MAKE) -C $(abspath $(STATIC_PYTHON_SRC)) distclean >/dev/null 2>&1 || true
	@cd $(STATIC_PYTHON_SRC) && $(STATIC_PYTHON_ENV) ./configure $(STATIC_PYTHON_CONFIGURE_FLAGS)
	@$(HOST_PYTHON) $(abspath tools/force_static_stdlib.py) $(STATIC_PYTHON_SETUP_STDLIB) $(STATIC_PYTHON_SETUP_LOCAL)
	@$(MAKE) -C $(abspath $(STATIC_PYTHON_SRC)) -j$$(nproc)
	@$(MAKE) -C $(abspath $(STATIC_PYTHON_SRC)) install
	@touch "$@"

$(STATIC_PYTHON_MODULES_STAMP): $(STATIC_PYTHON_BUILD_STAMP)
	@printf 'Preparing standard library for static Python...\n'
	@$(STATIC_PYTHON_BIN) -m ensurepip --upgrade
	@$(STATIC_PYTHON_BIN) -m pip install --upgrade pip wheel setuptools
	@$(STATIC_PYTHON_BIN) -m compileall -q -f $(STATIC_PYTHON_PREFIX)/lib/python$(STATIC_PYTHON_MAJOR_MINOR)
	@touch "$@"

.PHONY: all stage tarball clean distclean nuitka-install install
.ONESHELL:

all: $(ALL_BIN_TARGETS)

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

$(NUITKA_STAMP_FILE): $(STATIC_PYTHON_READY) | $(NUITKA_SOURCE_DIR)
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

ifneq ($(UI_APP_ENABLED),)
$(UI_BIN_TARGET): luminosity.py $(SRC_FILES) | nuitka-install
	@mkdir -p $(BUILD_DIR)
	$(NUITKA) $(NUITKA_FLAGS) --output-dir=$(BUILD_DIR) --output-filename=$(UI_APP_NAME).bin $(UI_ENTRY)
endif

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
	rm -f $(STATIC_PYTHON_BUILD_STAMP) $(STATIC_PYTHON_MODULES_STAMP)

distclean: clean
	rm -rf $(DIST_DIR)
	rm -rf $(NUITKA_SOURCE_DIR)
	rm -rf $(STATIC_PYTHON_BASE)
	rm -f $(NUITKA_STAMP_FILE)
