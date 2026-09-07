#!/usr/bin/env bash
# Turn a Raspberry Pi, or any Linux desktop, into a screen that shows one board.
#
# Run it on the machine attached to the television:
#
#     sudo ./install.sh https://boards.example.com/tv/tv_xxxxxxxx
#
# It installs the watchdog, writes the display link to a config file, and
# arranges for the watchdog to start with the graphical session. Nothing about
# the server changes; this is the television's half.
set -euo pipefail

URL="${1:-}"
if [ -z "$URL" ]; then
  echo "usage: sudo ./install.sh <display link>" >&2
  echo "the link is created in the console under Displays, and shown once." >&2
  exit 2
fi
case "$URL" in
  http://*|https://*) ;;
  *) echo "that does not look like a link: $URL" >&2; exit 2 ;;
esac

# Who owns the desktop session. Under sudo this is the invoking user, not root:
# Chromium has to run as the person logged in at the screen, and a browser
# profile owned by root is a television that never starts.
TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
if [ -z "$TARGET_HOME" ]; then
  echo "cannot find a home directory for $TARGET_USER" >&2
  exit 3
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN=/usr/local/bin/scoreboard-kiosk
CONF=/etc/scoreboard-kiosk.conf

echo "Installing for user: $TARGET_USER"

if ! command -v chromium-browser >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
  echo "Installing Chromium…"
  apt-get update -qq
  apt-get install -y -qq chromium-browser || apt-get install -y -qq chromium
fi
command -v curl >/dev/null 2>&1 || apt-get install -y -qq curl

install -m 0755 "$HERE/kiosk.sh" "$BIN"

# The link is a credential: it is the whole authentication a television has.
# Readable by the user who runs the browser, and by nobody else.
cat > "$CONF" <<CONFIG
# Written by install.sh. The display link is the television's only credential
# — anyone who can read this file can watch this board.
KIOSK_URL="$URL"
KIOSK_PROFILE="$TARGET_HOME/.local/share/scoreboard-kiosk/profile"
CONFIG
chown "$TARGET_USER" "$CONF"
chmod 0600 "$CONF"

# Two ways in, because Raspberry Pi OS moved. Bookworm and later use labwc on
# Wayland and read ~/.config/labwc/autostart; older desktops read the XDG
# autostart directory. Both are written, and each is harmless where it is not
# used, which is better than detecting wrongly and leaving a dark television.
AUTOSTART_LABWC="$TARGET_HOME/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART_LABWC")"
touch "$AUTOSTART_LABWC"
if ! grep -q "$BIN" "$AUTOSTART_LABWC" 2>/dev/null; then
  echo "$BIN &" >> "$AUTOSTART_LABWC"
fi
chown -R "$TARGET_USER" "$TARGET_HOME/.config/labwc"

XDG_DIR="$TARGET_HOME/.config/autostart"
mkdir -p "$XDG_DIR"
cat > "$XDG_DIR/scoreboard-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Scoreboard kiosk
Exec=$BIN
X-GNOME-Autostart-enabled=true
DESKTOP
chown -R "$TARGET_USER" "$XDG_DIR"

mkdir -p "$TARGET_HOME/.local/share/scoreboard-kiosk"
chown -R "$TARGET_USER" "$TARGET_HOME/.local/share/scoreboard-kiosk"

echo
echo "Installed."
echo "  watchdog : $BIN"
echo "  link     : $CONF"
echo
echo "Start it now without rebooting:   $BIN &"
echo "Or reboot, and the television comes up on the board by itself."
echo
echo "The board reloads itself when you press Refresh TV in the console, and"
echo "after a deployment. Neither needs anyone to touch this machine."
