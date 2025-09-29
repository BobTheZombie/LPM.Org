PYTHON ?= python3
NUITKA ?= $(PYTHON) -m nuitka
APP = lpm
ENTRY = $(PWD)/lpm.py
BUILD_DIR = build/nuitka
DIST_DIR = dist
SRC_FILES := $(shell find src -type f -name '*.py')
VERSION ?= $(shell $(PYTHON) tools/get_version.py)

export PYTHONPATH := $(PWD)$(if $(PYTHONPATH),:$(PYTHONPATH),)

BIN_TARGET = $(BUILD_DIR)/$(APP).bin
HOOK_BUILD_DIR = $(BUILD_DIR)/hooks
STAGING_DIR = $(DIST_DIR)/$(APP)-$(VERSION)
TARBALL = $(DIST_DIR)/$(APP)-$(VERSION).tar.gz
HOOK_SRC = usr/share/lpm/hooks
LIBLPM_HOOK_SRC = usr/libexec/lpm/hooks
LIBLPM_HOOK_BUILD_DIR = $(BUILD_DIR)/libexec_hooks

HOOK_PYTHON_SCRIPTS := $(shell $(PYTHON) tools/list_python_hooks.py $(HOOK_SRC))
HOOK_BINARIES := $(patsubst $(HOOK_SRC)/%, $(HOOK_BUILD_DIR)/%, $(HOOK_PYTHON_SCRIPTS))
LIBLPM_HOOK_PYTHON_SCRIPTS := $(shell $(PYTHON) tools/list_python_hooks.py $(LIBLPM_HOOK_SRC))
LIBLPM_HOOK_BINARIES := $(patsubst $(LIBLPM_HOOK_SRC)/%, $(LIBLPM_HOOK_BUILD_DIR)/%, $(LIBLPM_HOOK_PYTHON_SCRIPTS))

.PHONY: all stage tarball clean distclean
.ONESHELL:

all: $(BIN_TARGET) $(HOOK_BINARIES) $(LIBLPM_HOOK_BINARIES)

$(BIN_TARGET): lpm.py $(SRC_FILES)
	@mkdir -p $(BUILD_DIR)
	$(NUITKA) --onefile --include-package=src --follow-imports --output-dir=$(BUILD_DIR) --output-filename=$(APP).bin $(ENTRY)

$(HOOK_BUILD_DIR)/%: $(HOOK_SRC)/%
	@mkdir -p $(dir $@)
	$(NUITKA) --onefile --include-package=src --follow-imports --output-dir=$(dir $@) --output-filename=$(notdir $@) $<

$(LIBLPM_HOOK_BUILD_DIR)/%: $(LIBLPM_HOOK_SRC)/%
	@mkdir -p $(dir $@)
	$(NUITKA) --onefile --include-package=src --follow-imports --output-dir=$(dir $@) --output-filename=$(notdir $@) $<

$(STAGING_DIR): $(BIN_TARGET) README.md LICENSE etc/lpm/lpm.conf $(HOOK_BINARIES) $(LIBLPM_HOOK_BINARIES)
	@mkdir -p $(DIST_DIR)
	@rm -rf $@
	mkdir -p $@/bin
	cp $(BIN_TARGET) $@/bin/$(APP)
	mkdir -p $@/usr/share/lpm
	cp -R $(HOOK_SRC) $@/usr/share/lpm/
	if [ -n "$(HOOK_BINARIES)" ]; then \
		for hook in $(HOOK_BINARIES); do \
		rel=$${hook#$(HOOK_BUILD_DIR)/}; \
		dest="$@/usr/share/lpm/hooks/$${rel}"; \
		install -D -m 0755 "$$hook" "$$dest"; \
		done; \
	fi
	mkdir -p $@/usr/share/liblpm
	cp -R usr/share/liblpm/hooks $@/usr/share/liblpm/
	mkdir -p $@/usr/libexec/lpm
	cp -R $(LIBLPM_HOOK_SRC) $@/usr/libexec/lpm/
	if [ -n "$(LIBLPM_HOOK_BINARIES)" ]; then \
		for hook in $(LIBLPM_HOOK_BINARIES); do \
		rel=$${hook#$(LIBLPM_HOOK_BUILD_DIR)/}; \
		dest="$@/usr/libexec/lpm/hooks/$${rel}"; \
		install -D -m 0755 "$$hook" "$$dest"; \
		done; \
	fi
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

clean:
	rm -rf $(BUILD_DIR)

distclean: clean
	rm -rf $(DIST_DIR)
