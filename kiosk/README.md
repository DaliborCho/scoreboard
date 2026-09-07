# The television's half

Everything in this directory runs on the machine attached to a screen. None of
it runs on the server, and the server does not know it exists.

That separation is the point. The same watchdog drives a television against a
server in the next room and one against a server in another country; only the
link differs.

## Install

On the machine plugged into the television:

```bash
sudo ./install.sh https://boards.example.com/tv/tv_xxxxxxxxxx
```

The link is created in the console under **Displays** and shown once.

Then either reboot, or start it now:

```bash
scoreboard-kiosk &
```

## What it does

- Waits for the board to answer before opening anything. A screen that boots
  with the server would otherwise land on an error page and stay there, and
  somebody finds it an hour later.
- Opens Chromium full screen against a profile nothing else touches, so a
  normal browser window on the same machine cannot interfere with it.
- Stops the screen sleeping, dimming or blanking.
- **Reopens the browser whenever it exits** — a crash, an update, somebody
  closing the window. This is a loop, not a launcher.
- Starts with the graphical session, on both Raspberry Pi OS Bookworm
  (labwc/Wayland) and older desktops.

## What it deliberately does not do

**It has no connection to the server.** No agent, no port, no credential
beyond the display link itself. A screen is a browser holding a URL, and that
is the whole trust relationship.

Everything remote happens the other way round: the board is already asking the
server a question every few seconds, so the answer carries what it needs.

| From the console | How it reaches the screen |
|---|---|
| **Refresh TV** | a number in the next poll; the board reloads itself |
| A deploy | the served page's own digest changes; every board reloads |
| Changing the screen | the next poll returns different content |
| Rotation | the server names the next screen in the payload |
| **Revoke** | the link stops answering and the board says so |

So the only thing this script is needed for is the one thing a web page is not
allowed to do to itself: **go full screen without somebody clicking first.**

## Where things are

| | |
|---|---|
| Watchdog | `/usr/local/bin/scoreboard-kiosk` |
| Link | `/etc/scoreboard-kiosk.conf` — mode `0600`, it is a credential |
| Browser profile | `~/.local/share/scoreboard-kiosk/profile` |
| Autostart | `~/.config/labwc/autostart` and `~/.config/autostart/` |

## Running the server on the same machine

A single office does not need a server anywhere else. The whole product runs
on the machine behind the television:

```bash
docker compose up -d
sudo ./kiosk/install.sh http://localhost:8000/tv/tv_xxxxxxxxxx
```

Nothing leaves the building, there is no monthly cost, and there is nothing to
explain to anyone's IT department. The multi-tenant machinery is still there —
it simply has one tenant, and stays out of the way until there is a second.

A Raspberry Pi 4 or 5 with 4GB runs this comfortably. Anything smaller wants a
separate box for the server; the television's half is undemanding either way.

## Checking on it

```bash
journalctl -f            # if started from the session, watch its output there
pgrep -af scoreboard-kiosk
```

The console's **Displays** page shows when each screen last asked for data —
`live · 4s ago` against `3 days ago` is the difference between a wall that is
working and one that has been dark since Friday without anybody noticing.
