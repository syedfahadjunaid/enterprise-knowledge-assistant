"""Build-time step: download the embedding model into EMBEDDING_CACHE_DIR."""

import os

from fastembed import TextEmbedding

cache_dir = os.environ["EMBEDDING_CACHE_DIR"]
TextEmbedding(os.environ["EMBEDDING_MODEL"], cache_dir=cache_dir)
print("models ready in", cache_dir)
