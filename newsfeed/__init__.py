"""The hosted pipeline: a store, a schedule and (later) the AI layer and the Provenance upload.

Designed in docs/ARCHITECTURE.md. Each stage is a separate command over one SQLite database and
resumes where it stopped, so the host's scheduler runs them and no Python process stays alive.

Built so far: milestone M1, the store and the scrape stage. Every other stage exits with a message
naming its milestone.
"""

__all__ = ["settings", "store", "identity", "scrape"]
