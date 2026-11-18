# Shortcuts for running the dashboard on your own computer. On the kiosk,
# systemd runs the server instead (see install.sh).
#
#   make start     run the server in the background, logging to .run/
#   make stop      stop this project's server on PORT, wherever it's running
#   make restart   stop it, then start it again in the background
#   make logs      follow the background server's log (Ctrl+C stops following)
#   make run       run the server in this terminal instead (Ctrl+C stops it)
#   make cast-setup  set up casting for `make run` (tools/cast_setup.sh)
#   make test      run all the tests and lint the shell scripts
#   make release VERSION=1.0.0   publish a release (see tools/release.sh)
#
# PORT, KPM_HOST, KPM_DATA_DIR and the other server settings pass through,
# e.g. `make run PORT=8017`. Off a Pi, the setup wizard only pretends to
# change Wi-Fi, the country and the time zone.

PORT ?= 8000
export PORT
LOG = .run/server-$(PORT).log

# After `make cast-setup`: its Python (with pychromecast), and go2rtc for
# the camera on cast screens (or one on your PATH).
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
GO2RTC ?= $(firstword $(wildcard .venv/bin/go2rtc) $(shell command -v go2rtc))

.PHONY: run start stop restart logs cast-setup test release

# go2rtc, if there is one and it isn't running already, runs alongside the
# server and stops with it.
run:
	@if [ -n "$(GO2RTC)" ] && ! lsof -nP -iTCP:1984 -sTCP:LISTEN >/dev/null 2>&1; then \
		"$(GO2RTC)" -config tools/go2rtc-dev.yaml >/dev/null 2>&1 & \
		trap 'kill $$! 2>/dev/null' EXIT INT TERM; \
	fi; \
	$(PYTHON) server/server.py

# The same, in the background: its output goes to LOG (replaced each start).
start:
	@if lsof -nP -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Something is already running on port $(PORT); \`make restart\` replaces this project's server."; exit 1; \
	fi; \
	mkdir -p .run; \
	PYTHONUNBUFFERED=1 nohup $(MAKE) --no-print-directory run >"$(LOG)" 2>&1 </dev/null & \
	for _ in $$(seq 40); do \
		lsof -nP -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1 && break; \
		kill -0 $$! 2>/dev/null || break; \
		sleep 0.25; \
	done; \
	if lsof -nP -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Started the server at http://localhost:$(PORT) (log: $(LOG), \`make logs\` to follow it)."; \
	else \
		echo "The server didn't start; the end of $(LOG):"; tail -n 20 "$(LOG)"; exit 1; \
	fi

logs:
	@if [ -f "$(LOG)" ]; then tail -n 50 -f "$(LOG)"; else echo "No log yet for port $(PORT); \`make start\` writes one."; fi

# Only a server started from this folder is stopped: another app on the same
# port (the Disney Wait Times app also defaults to 8000) is left alone.
stop:
	@pids=$$(lsof -t -nP -iTCP:$(PORT) -sTCP:LISTEN 2>/dev/null); \
	if [ -z "$$pids" ]; then echo "Nothing is running on port $(PORT)."; exit 0; fi; \
	for pid in $$pids; do \
		cwd=$$(lsof -a -p $$pid -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'); \
		if [ "$$cwd" = "$(CURDIR)" ]; then \
			kill $$pid; \
			for _ in $$(seq 20); do kill -0 $$pid 2>/dev/null || break; sleep 0.25; done; \
			echo "Stopped the server on port $(PORT) (process $$pid)."; \
		else \
			echo "Port $(PORT) is in use by something else (process $$pid, in $${cwd:-an unknown folder}); leaving it alone."; \
			exit 1; \
		fi; \
	done

restart: stop start

cast-setup:
	@tools/cast_setup.sh

test:
	python3 -m unittest discover -s tests
	bash tests/test_update.sh
	bash tests/test_release.sh
	@if command -v shellcheck >/dev/null; then \
		shellcheck install.sh kiosk/kiosk.sh kiosk/update.sh tools/release.sh tools/make_images.sh tools/cast_setup.sh \
			tests/test_update.sh tests/test_release.sh && echo "shellcheck: clean"; \
	else \
		echo "shellcheck isn't installed; skipping the shell script lint."; \
	fi

release:
	@tools/release.sh "$(VERSION)"
