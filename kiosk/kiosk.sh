#!/usr/bin/env bash
# Keep one board on one screen, forever, with nobody in the room.
#
# The television is the part of this product that nobody watches over. A
# browser that crashes at 4am, an update that closes the window, a screen
# blanker that dims the wall at lunchtime — each of them ends with a dark
# television and a sales floor that assumes the whole system is broken.
#
# So this is a loop, not a launcher. It waits for the board to answer, opens
# Chromium full screen against a profile nothing else touches, and when
# Chromium exits for any reason at all, it opens it again.
#
# It does not care where the board is served from. The same script drives a
# screen against a server in the next room and one against a server in another
# country; only KIOSK_URL differs.
set -uo pipefail

CONFIG="${KIOSK_CONFIG:-/etc/scoreboard-kiosk.conf}"
# shellcheck disable=SC1090
[ -r "$CONFIG" ] && . "$CONFIG"

KIOSK_URL="${KIOSK_URL:-}"
KIOSK_WAIT="${KIOSK_WAIT:-5}"
KIOSK_PROFILE="${KIOSK_PROFILE:-$HOME/.local/share/scoreboard-kiosk/profile}"

if [ -z "$KIOSK_URL" ]; then
  echo "kiosk: KIOSK_URL is not set. Put it in $CONFIG or the environment." >&2
  echo "kiosk: it is the display link, e.g. https://boards.example.com/tv/tv_..." >&2
  exit 2
fi

log() { echo "$(date -Is) kiosk: $*"; }

find_browser() {
  for candidate in chromium-browser chromium google-chrome-stable google-chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

BROWSER="$(find_browser)" || {
  log "no Chromium or Chrome found. Install chromium-browser."
  exit 3
}

mkdir -p "$KIOSK_PROFILE"

# The screen must not sleep, dim, or draw a blanker over the board. Failures
# are ignored: on a Wayland session these tools are simply absent, and their
# absence is not a reason to leave the television dark.
keep_awake() {
  command -v xset >/dev/null 2>&1 && { xset s off; xset -dpms; xset s noblank; } 2>/dev/null
  command -v wlr-randr >/dev/null 2>&1 && wlr-randr >/dev/null 2>&1
  true
}

# Wait for the board rather than opening Chromium on an error page. On a Pi
# that boots with the server, the network and the service are both still
# coming up, and an error page is what somebody finds on the wall an hour
# later because nothing ever retried.
wait_for_board() {
  local waited=0
  until curl -fsS --max-time 5 -o /dev/null "$KIOSK_URL"; do
    if [ "$waited" -eq 0 ]; then
      log "waiting for $KIOSK_URL"
    elif [ $((waited % 60)) -eq 0 ]; then
      log "still waiting for the board (${waited}s)"
    fi
    sleep "$KIOSK_WAIT"
    waited=$((waited + KIOSK_WAIT))
  done
  [ "$waited" -gt 0 ] && log "board answered after ${waited}s"
  true
}

log "starting; url=$KIOSK_URL browser=$BROWSER"

while true; do
  keep_awake
  wait_for_board

  # `--kiosk` is what makes full screen possible at all: a page cannot put
  # itself full screen without someone clicking first, so the board's own
  # code can never do this and the launcher has to.
  "$BROWSER" \
    --kiosk \
    --start-fullscreen \
    --start-maximized \
    --app="$KIOSK_URL" \
    --user-data-dir="$KIOSK_PROFILE" \
    --no-first-run \
    --no-default-browser-check \
    --disable-session-crashed-bubble \
    --disable-infobars \
    --disable-features=TranslateUI \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --autoplay-policy=no-user-gesture-required \
    --check-for-update-interval=31536000 \
    >/dev/null 2>&1

  # Reached whenever Chromium exits: a crash, an update, or somebody closing
  # the window. Two seconds of grace so a browser that cannot start at all
  # does not spin the processor while it fails.
  log "browser exited; reopening in 2s"
  sleep 2
done
