#!/usr/bin/env bash
# Install the NewsScraper user timers on a Linux host.
#
# User units, not system units: the scraper runs as one person's account, reads that account's
# settings file and, for Reuters, needs a display. Run this as that account, not with sudo.
#
#   deploy/install-systemd.sh          install and enable the timers
#   deploy/install-systemd.sh --status show what is installed
#   deploy/install-systemd.sh --remove stop and remove the timers
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
units_source="$repo/deploy/systemd"
units_target="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
env_file="${XDG_CONFIG_HOME:-$HOME/.config}/newsfeed/newsfeed.env"
units=(newsfeed-scrape.service newsfeed-scrape.timer
       newsfeed-scrape-reuters.service newsfeed-scrape-reuters.timer)
timers=(newsfeed-scrape.timer newsfeed-scrape-reuters.timer)

case "${1:-install}" in
  --status)
    systemctl --user list-timers 'newsfeed-*' --all
    exit 0
    ;;
  --remove)
    for timer in "${timers[@]}"; do
      systemctl --user disable --now "$timer" 2>/dev/null || true
    done
    for unit in "${units[@]}"; do rm -f "$units_target/$unit"; done
    systemctl --user daemon-reload
    echo "Removed. The store in NEWSFEED_DATA_DIR was left alone."
    exit 0
    ;;
  install) ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac

if [ ! -x "$repo/.venv/bin/python" ]; then
  echo "No virtual environment at $repo/.venv. Run:" >&2
  echo "  python3 -m venv $repo/.venv && $repo/.venv/bin/pip install -r $repo/requirements.txt" >&2
  exit 1
fi

mkdir -p "$units_target" "$(dirname "$env_file")"
if [ ! -f "$env_file" ]; then
  install -m 600 "$repo/deploy/newsfeed.env.example" "$env_file"
  echo "Wrote $env_file from the example. Edit it before the AI stages are built."
fi

# The units say %h/NewsScraper, so the checkout has to be at ~/NewsScraper for them to resolve.
if [ "$repo" != "$HOME/NewsScraper" ]; then
  echo "Warning: this checkout is $repo, but the units expect $HOME/NewsScraper." >&2
  echo "Either move the checkout or edit WorkingDirectory and ExecStart in $units_source/*.service." >&2
fi

for unit in "${units[@]}"; do
  install -m 644 "$units_source/$unit" "$units_target/$unit"
done
systemctl --user daemon-reload

# Reuters needs this account's REAL display: DataDome refuses the SwiftShader renderer Chrome
# falls back to without a GPU. See deploy/reuters-display.sh for the measurement.
if ! ls /tmp/.X11-unix/X* >/dev/null 2>&1 && [ -z "${DISPLAY:-}" ]; then
  echo "Warning: no X display found, so the Reuters timer will fail with exit 3." >&2
  echo "  Log in on the seat, or set NEWS_SCRAPER_DISPLAY in $env_file." >&2
  echo "  The other four outlets need no display and are unaffected." >&2
fi
if ! command -v xdpyinfo >/dev/null 2>&1; then
  echo "Note: xdpyinfo is missing, so the Reuters wrapper cannot prove the display answers" >&2
  echo "before it spends a request on it. It will still run." >&2
  echo "  sudo dnf install -y xorg-x11-utils" >&2
fi
if ! command -v google-chrome-stable >/dev/null 2>&1 && ! command -v google-chrome >/dev/null 2>&1; then
  echo "Warning: Google Chrome is missing, so Reuters will fail. DataDome refuses every other browser." >&2
  echo "  sudo dnf install -y google-chrome-stable" >&2
fi

for timer in "${timers[@]}"; do
  systemctl --user enable --now "$timer"
done

# Without lingering, user timers stop when the account logs out, which looks exactly like an
# outage of every outlet. This needs an administrator once.
#
# Lingering is necessary but NOT sufficient for Reuters. That one needs a live graphical session
# as well, because it drives a real Chrome window on the seat's display. A logged-out box with
# lingering on keeps the other four outlets running and fails Reuters loudly, which is the
# intended behaviour rather than a bug to work around.
if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo
  echo "Timers stop when this account logs out. To keep them running, an administrator runs:"
  echo "  sudo loginctl enable-linger $USER"
fi

echo
systemctl --user list-timers 'newsfeed-*' --all
