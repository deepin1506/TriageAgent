"""Build the KB index, or make sure one exists (the demo container starts with an empty disk)."""

import logging

from triage.config import Settings

from .chunking import load_chunks
from .embeddings import make_embedder
from .store import KBIndex, index_dir

log = logging.getLogger(__name__)


def build_index(settings: Settings) -> KBIndex:
    index = KBIndex.build(load_chunks(settings.kb_dir), make_embedder(settings))
    index.save(index_dir(settings))
    return index


def ensure_index(settings: Settings) -> bool:
    """Load the index, building it first if it's missing or was built with another embedder.
    False if that fails (e.g. the embedding API is down): search_kb is then disabled and
    triage carries on without the KB, as everywhere else."""
    try:
        KBIndex.load(index_dir(settings), make_embedder(settings))
        return True
    except Exception:
        pass
    try:
        manifest = build_index(settings).manifest
        log.info("Built the KB index: %s chunks with %s", manifest["chunks"], manifest["embedder"])
        return True
    except Exception as err:
        log.warning("Could not build the KB index, continuing without the KB: %s", err)
        return False
