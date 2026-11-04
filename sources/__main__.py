"""Command-line entry for source workers."""

import sys

from .incident.cli import main as incident_main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "incident-ukraine":
        raise SystemExit(incident_main(sys.argv[2:]))
    raise SystemExit("Usage: python -m sources incident-ukraine [--fixture PATH] [--write] [--no-deepseek]")
