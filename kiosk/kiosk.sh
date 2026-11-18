#!/usr/bin/env bash
# Launches Chromium in kiosk mode and makes sure the dashboard actually loads.
#
# At boot, the desktop session can start before Wi-Fi and the local server are
# ready, so first wait (up to a limit) for both. Even then, Chromium sometimes
# opens without ever requesting the page and sits on a blank screen until
# someone reloads it. So after launching, watch the tab through Chromium's
# local DevTools endpoint and reload it if the dashboard hasn't loaded.
#
# Started by the autostart entry that install.sh creates. Writes what it saw to
# ~/.cache/klipper-monitor-kiosk.log.

MAX_WAIT_SECS=90
APP_URL="http://127.0.0.1:8000/?kiosk"  # ?kiosk hides the mouse pointer
APP_TITLE="Klipper Printer Monitor"
DEBUG_PORT=9222
LOAD_TIMEOUT_SECS=60
MAX_RETRIES=3
# Chromium only allows the DevTools port with a non-default profile.
PROFILE_DIR="$HOME/.config/klipper-monitor-kiosk-chromium"
LOG_FILE="$HOME/.cache/klipper-monitor-kiosk.log"

mkdir -p "$(dirname "$LOG_FILE")"
log() { echo "$(date '+%F %T') $*" >> "$LOG_FILE"; }

CHROMIUM_BIN="$(command -v chromium-browser || command -v chromium)"

log "kiosk.sh starting"
# Wait for the local server, and for the server to reach the printer (its
# status says "connected": true). The page copes with neither, but starting
# the camera before the printer answers just leaves it blank for a while.
for ((i = 0; i < MAX_WAIT_SECS; i++)); do
  if curl -fsS --max-time 4 "http://127.0.0.1:8000/api/status" 2>/dev/null | grep -q '"connected": true'; then
    break
  fi
  sleep 1
done
log "server/printer ready after ${SECONDS}s"

# Start from a fresh profile every boot. A reboot kills Chromium uncleanly,
# and a profile left in that state can stall the first page load; nothing
# worth keeping lives here.
rm -rf "$PROFILE_DIR"

# --autoplay-policy lets the camera's muted video start without a tap.
launched=$SECONDS
"$CHROMIUM_BIN" --password-store=basic --kiosk --noerrdialogs --disable-infobars --incognito \
  --user-data-dir="$PROFILE_DIR" \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port="$DEBUG_PORT" \
  --autoplay-policy=no-user-gesture-required \
  --no-first-run --no-default-browser-check --disable-default-apps \
  --disable-component-update --disable-background-networking --disable-sync \
  --disable-features=Translate,MediaRouter,OptimizationHints \
  "$APP_URL" &
CHROMIUM_PID=$!

# Chromium can take well over a minute to start on a slow Pi. Don't start the
# load timeout until it's actually up, or a slow start looks like a stall.
for ((s = 0; s < 180; s++)); do
  curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$DEBUG_PORT/json/version" && break
  sleep 1
done
log "chromium responding after $((SECONDS - launched))s"

# Prints "<id> <title>" for each open page tab.
list_tabs() {
  curl -fsS --max-time 2 "http://127.0.0.1:$DEBUG_PORT/json/list" 2>/dev/null \
    | python3 -c 'import json, sys
for t in json.load(sys.stdin):
    if t.get("type") == "page":
        print(t["id"], t.get("title", ""), "|", t.get("url", ""))' 2>/dev/null
}

for ((attempt = 0; attempt <= MAX_RETRIES; attempt++)); do
  started=$SECONDS
  while ((SECONDS - started < LOAD_TIMEOUT_SECS)); do
    sleep 1
    if list_tabs | grep -qF "$APP_TITLE"; then
      log "dashboard loaded (attempt $attempt, $((SECONDS - started))s later; $((SECONDS - launched))s after launch)"
      wait "$CHROMIUM_PID"
      exit
    fi
  done

  log "dashboard not loaded after ${LOAD_TIMEOUT_SECS}s (attempt $attempt); tabs were:"
  list_tabs | sed 's/^/    /' >> "$LOG_FILE"

  # Tell the stuck tab to load the dashboard again, like pressing Ctrl+R.
  # (Opening a new tab instead would land outside the incognito kiosk window.)
  for id in $(list_tabs | cut -d' ' -f1); do
    python3 "$(dirname "$0")/kiosk_navigate.py" "$DEBUG_PORT" "$id" "$APP_URL" >> "$LOG_FILE" 2>&1
  done
done

log "gave up after $MAX_RETRIES retries"
wait "$CHROMIUM_PID"
