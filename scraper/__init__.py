"""News sources, by the outlet id written to each row."""

from . import bbc, guardian, nyt, pbs, reuters

SOURCES = {module.NAME: module for module in (reuters, bbc, guardian, pbs, nyt)}
