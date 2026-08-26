"""Turn a log excerpt into 384 numbers, locally.

An embedding is a list of numbers that stands for the MEANING of a piece of
text. Two texts that mean similar things get similar numbers, even when
they share no words. That is the whole trick behind BuildDoctor's memory:
we cannot keyword-match CI logs usefully (every one of them contains
"error" and "pytest"), but we can compare meanings.

WHY A LOCAL MODEL AND NOT AN API

We already depend on Groq for the diagnosis text. A second paid API would
mean a second bill, a second key to rotate, and - the part that actually
matters - a second service whose outage takes BuildDoctor down. This model
runs on the CPU in our own container. If the internet is on fire, memory
still works.

WHY THE WEIGHTS ARE IN THE IMAGE

See the Dockerfile. Short version: the alternative is downloading them the
first time a build fails, which is the exact moment we can least afford a
network problem. HF_HUB_OFFLINE=1 is set in the image so that this is not
a preference but a rule - the library cannot reach out even if it wants to.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache

import db

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# The model reads at most 256 word-pieces and silently ignores the rest.
# Cutting the text explicitly first makes that limit visible in the code
# rather than being a surprise buried in the library. Excerpts are built
# around the error marker, so the part that survives is the part that
# carries the failure.
MAX_EMBED_CHARS = 2000

# Our own excerpt header, e.g. "--- log lines 167-182 of 189 ---". The line
# numbers change between two runs of the identical failure, so they are
# pure noise in a similarity comparison. This is the only normalisation
# done here, and it is safe because BuildDoctor writes this line itself -
# it is not evidence from the build.
_EXCERPT_HEADER = re.compile(r"^--- log lines .*---$", re.MULTILINE)

_model = None
_lock = threading.Lock()


def _load():
    """Load the model once, and only once, however many callers arrive.

    The import is inside the function on purpose: importing
    sentence_transformers drags in PyTorch, which takes seconds. Doing it
    at module import time would slow down every script that merely wants
    db.py, including the migration tools.
    """
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            print(f"  loading embedding model {MODEL_NAME} ...")
            _model = SentenceTransformer(MODEL_NAME)
            print(f"  embedding model ready ({db.EMBEDDING_DIM} dimensions)")
    return _model


def warm() -> None:
    """Load the model now, so the first real webhook does not pay for it.

    Called from the app's startup. Loading takes a few seconds; a failed
    build should not be the thing that discovers that.
    """
    _load()


def clean(text: str) -> str:
    """Trim an excerpt down to the part worth comparing."""
    return _EXCERPT_HEADER.sub("", text or "").strip()[:MAX_EMBED_CHARS]


@lru_cache(maxsize=16)
def _embed_cached(text: str) -> tuple:
    model = _load()
    # normalize_embeddings makes every vector the same length, which is
    # what lets cosine distance be compared meaningfully across rows.
    vector = model.encode(text, normalize_embeddings=True)
    values = tuple(float(x) for x in vector)
    if len(values) != db.EMBEDDING_DIM:
        # A different model with a different width would be written into a
        # vector(384) column and rejected by Postgres with a message about
        # dimensions, far from the cause. Fail here instead.
        raise RuntimeError(
            f"{MODEL_NAME} produced {len(values)} dimensions but "
            f"db.EMBEDDING_DIM is {db.EMBEDDING_DIM}; the column would "
            f"reject this vector"
        )
    return values


def embed(text: str) -> list[float]:
    """Embed one log excerpt.

    Cached, because each failure is embedded twice in the same pass - once
    to search memory with, once to store. The cache turns the second call
    into a dictionary lookup instead of a second run of the model.
    """
    return list(_embed_cached(clean(text)))
