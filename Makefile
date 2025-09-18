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
STAGING_DIR = $(DIST_DIR)/$(APP)-$(VERSION)
TARBALL = $(DIST_DIR)/$(APP)-$(VERSION).tar.gz
HOOK_SRC = usr/share/lpm/hooks

.PHONY: all stage tarball clean distclean
.ONESHELL:

all: $(BIN_TARGET)

$(BIN_TARGET): lpm.py $(SRC_FILES)
	@mkdir -p $(BUILD_DIR)
	$(NUITKA) --onefile --include-package=src --follow-imports --output-dir=$(BUILD_DIR) --output-filename=$(APP).bin $(ENTRY)

$(STAGING_DIR): $(BIN_TARGET) README.md LICENSE etc/lpm/lpm.conf
	@mkdir -p $(DIST_DIR)
	@rm -rf $@
	mkdir -p $@/bin
	cp $(BIN_TARGET) $@/bin/$(APP)
	mkdir -p $@/usr/share/lpm
	cp -R $(HOOK_SRC) $@/usr/share/lpm/
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
