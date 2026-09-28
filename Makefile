CC ?= cc
PKG_CONFIG ?= pkg-config
CPPFLAGS += $(shell $(PKG_CONFIG) --cflags alsa json-c)
CFLAGS ?= -O2 -Wall -Wextra -Werror
LDFLAGS ?=
LDLIBS += $(shell $(PKG_CONFIG) --libs alsa json-c)

PREFIX ?= /usr/local
LIBDIR ?= $(PREFIX)/lib
DATADIR ?= $(PREFIX)/share
PLUGIN = build/libasound_module_ctl_io24.so

.PHONY: all clean install uninstall test

all: $(PLUGIN)

build:
	mkdir -p "$@"

$(PLUGIN): io24_alsa_ctl.c | build
	$(CC) -shared -fPIC -DPIC $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -Wl,-soname,libasound_module_ctl_io24.so -o "$@" "$<" $(LDLIBS)

install: $(PLUGIN)
	install -d "$(DESTDIR)$(LIBDIR)/alsa-lib" "$(DESTDIR)$(DATADIR)/io24"
	install -m 755 "$(PLUGIN)" "$(DESTDIR)$(LIBDIR)/alsa-lib/libasound_module_ctl_io24.so"
	install -m 644 "alsa/io24.asoundrc" "$(DESTDIR)$(DATADIR)/io24/io24.asoundrc"

uninstall:
	@echo "Removing $(DESTDIR)$(LIBDIR)/alsa-lib/libasound_module_ctl_io24.so"
	@echo "Removing $(DESTDIR)$(DATADIR)/io24/io24.asoundrc"
	rm -f "$(DESTDIR)$(LIBDIR)/alsa-lib/libasound_module_ctl_io24.so" "$(DESTDIR)$(DATADIR)/io24/io24.asoundrc"
	-rmdir --ignore-fail-on-non-empty "$(DESTDIR)$(LIBDIR)/alsa-lib" "$(DESTDIR)$(DATADIR)/io24"
	@echo "Note: if ~/.asoundrc contains '</$(DATADIR)/io24/io24.asoundrc>', remove that line or ALSA config loading will fail."

test: $(PLUGIN)
	ALSA_PLUGIN_DIR="$(CURDIR)/build" python3 -m unittest -v tests/test_io24_alsa_ctl.py

clean:
	rm -rf build
