#!/usr/bin/env bash
# Run a command against this account's real X display, or fail saying why.
#
# WHY THIS EXISTS. Reuters is fetched through installed Google Chrome, and DataDome
# scores the browser's WebGL renderer. Under xvfb-run there is no GPU, so Chrome falls
# back to SwiftShader and every Reuters section is answered HTTP 401. Measured on this
# box on 2026-09-18, same public IP, minutes apart:
#
#   real display :0            ANGLE (Mesa, zink ... NVIDIA GeForce GTX 1650 ...)  200
#   xvfb-run                   ANGLE (Google, ... SwiftShader driver)              401
#   xvfb-run --use-gl=egl      the same SwiftShader string, so no better
#   xvfb-run --use-angle=vulkan  no WebGL at all, which is a worse signal still
#
# navigator.webdriver was false in every one of those, so the renderer is the signal.
# An earlier reading of this failure blamed request rate and made the schedule slower
# and shallower. That did not fix it: 23 of the 24 hourly runs to 2026-09-18 00:22
# still failed. Rate is not the cause.
#
# THE COST, AND IT IS REAL. This ties Reuters to a logged-in graphical session. The
# other four outlets need no display and are unaffected. When the display is gone this
# exits 3 and says so, rather than falling back to a display that gets refused --
# a legible failure beats an hourly 401 that reads like Reuters blocking the machine.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: reuters-display.sh <command> [args...]" >&2
  exit 2
fi

# An explicit setting wins, so a host with an unusual seat can say so in newsfeed.env.
display="${NEWS_SCRAPER_DISPLAY:-}"

# Otherwise ask logind for this account's graphical session.
if [ -z "$display" ] && command -v loginctl >/dev/null 2>&1; then
  for session in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
    value="$(loginctl show-session "$session" -p Display --value 2>/dev/null || true)"
    if [ -n "$value" ]; then display="$value"; break; fi
  done
fi

# logind leaves Display empty for a session started outside a display manager, which is
# the common case on a box someone logged into at a tty. The socket is still the truth.
if [ -z "$display" ]; then
  for socket in /tmp/.X11-unix/X*; do
    [ -e "$socket" ] || continue
    display=":${socket##*/X}"
    break
  done
fi

if [ -z "$display" ]; then
  echo "No X display for $USER, so Reuters cannot run: Chrome would fall back to" >&2
  echo "SwiftShader and DataDome answers that with 401. Log in on the seat, or set" >&2
  echo "NEWS_SCRAPER_DISPLAY in the settings file. The other four outlets are fine." >&2
  exit 3
fi
export DISPLAY="$display"

# SDDM and KDE write a per-session cookie under /run/user with a random suffix, so it
# has to be found rather than named. ~/.Xauthority is the fallback for other setups.
if [ -z "${XAUTHORITY:-}" ]; then
  for candidate in /run/user/"$(id -u)"/xauth_* "$HOME/.Xauthority"; do
    if [ -r "$candidate" ]; then export XAUTHORITY="$candidate"; break; fi
  done
fi

# Prove the display answers before spending a Reuters request on it. A DISPLAY that is
# set but dead fails later, inside Chrome, as a timeout that reads like a network fault.
if command -v xdpyinfo >/dev/null 2>&1; then
  if ! xdpyinfo >/dev/null 2>&1; then
    echo "DISPLAY=$DISPLAY did not answer (XAUTHORITY=${XAUTHORITY:-unset})." >&2
    echo "The session may have ended or the cookie may belong to another session." >&2
    exit 3
  fi
fi

exec "$@"
