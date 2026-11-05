#!/usr/bin/env bash
# deploy/set-news-key.sh: put the scraping proxy into ~/.config/newsfeed/newsfeed.env.
#
# Run it ON the box, in your own terminal, so the paste stays hidden:
#   ssh -t provenance-1 ~/NewsScraper/deploy/set-news-key.sh
#
# It asks for the proxy line as host:port:user:pass and:
#  1. reads it with `read -rsp` (no echo, nothing in shell history);
#  2. checks it: one request to an IP-echo service directly and one through the proxy. The proxy
#     must answer, and its exit IP must not be the box's own;
#  3. only then writes NEWS_PROXY_URL=http://user:pass@host:port, through a temporary file and a
#     rename, mode 600, LF only. Every other line in the file is kept.
# The value is passed on stdin, never in an argument, so `ps` cannot show it. Only the outcome is
# printed. Nothing is written when the check fails.
#
# FAKE TEST (the real file is not touched, the check is skipped):
#   NEWS_KEY_FAKE_DIR=/some/empty/dir deploy/set-news-key.sh
set -euo pipefail

env_file="${XDG_CONFIG_HOME:-$HOME/.config}/newsfeed/newsfeed.env"
skip_check=0
if [[ -n "${NEWS_KEY_FAKE_DIR:-}" ]]; then
  env_file="$NEWS_KEY_FAKE_DIR/newsfeed.env"
  skip_check=1
  echo "Fake mode: writing to $env_file, and the proxy check is skipped."
fi

read -rsp "Paste the proxy line (host:port:user:pass), then press Enter: " line
echo
if [[ -z "$line" ]]; then
  echo "Nothing pasted. Nothing written."
  exit 1
fi

# printf is a shell builtin, so the value goes down the pipe without ever being an argument.
printf '%s' "$line" | NEWS_KEY_FILE="$env_file" NEWS_KEY_SKIP_CHECK="$skip_check" python3 -c '
import json, os, sys, tempfile, urllib.error, urllib.parse, urllib.request

ECHO = "https://api.ipify.org?format=json"
raw = sys.stdin.read().strip()

parts = raw.split(":", 3)
if len(parts) != 4 or not all(parts) or not parts[1].isdigit() or any(c.isspace() for c in raw):
    sys.exit("Refused: that is not host:port:user:pass. Nothing written.")
host, port, user, password = parts
quote = lambda text: urllib.parse.quote(text, safe="")
url = f"http://{quote(user)}:{quote(password)}@{host}:{port}"

def exit_ip(proxy):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    with opener.open(urllib.request.Request(ECHO, headers={"user-agent": "set-news-key"}), timeout=30) as answer:
        return json.loads(answer.read().decode("utf-8"))["ip"]

if os.environ["NEWS_KEY_SKIP_CHECK"] != "1":
    try:
        own = exit_ip(None)
    except Exception as error:
        sys.exit(f"Refused: the box could not reach the IP-echo service by itself ({type(error).__name__}). Nothing written.")
    try:
        through = exit_ip(url)
    except urllib.error.HTTPError as error:
        sys.exit(f"Refused: the proxy answered HTTP {error.code}. Check the line. Nothing written.")
    except Exception as error:
        # The message of a proxy error can hold the URL, password included, so print the kind only.
        sys.exit(f"Refused: no answer through the proxy ({type(error).__name__}). Check the line. Nothing written.")
    if through == own:
        sys.exit("Refused: the exit IP through the proxy is the box'"'"'s own IP. Nothing written.")

path = os.environ["NEWS_KEY_FILE"]
folder = os.path.dirname(path)
os.makedirs(folder, mode=0o700, exist_ok=True)
lines = []
if os.path.exists(path):
    with open(path, encoding="utf-8") as handle:
        lines = [line.rstrip("\r\n") for line in handle]
lines = [line for line in lines if not line.startswith("NEWS_PROXY_URL=")] + ["NEWS_PROXY_URL=" + url]
handle, temporary = tempfile.mkstemp(dir=folder, prefix=".newsfeed-env-")
with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
    out.write("\n".join(lines) + "\n")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
checked = "skipped (fake mode)" if os.environ["NEWS_KEY_SKIP_CHECK"] == "1" else "the proxy answered, and its exit IP is not the box'"'"'s own"
print(f"Done: NEWS_PROXY_URL is set in {path} (mode 600). Check: {checked}.")
'
unset line
echo "No value was printed. The next scrape run reads it."
