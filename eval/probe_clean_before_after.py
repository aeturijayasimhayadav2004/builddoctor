"""Read-only probe: what changed in what gets embedded, for row 9.

Phase 8.5. Compares the OLD clean() (strip our header, truncate) against
the NEW one (also collapse runs of version numbers), and shows what the
256-word-piece window actually contains in each case.
"""
import re, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db, embeddings

MAX = embeddings.MAX_EMBED_CHARS

def old_clean(text):
    """clean() exactly as it was before Phase 8.5."""
    return embeddings._EXCERPT_HEADER.sub("", text or "").strip()[:MAX]

model = embeddings._load()
tok = model.tokenizer

def window(text, n=256):
    ids = tok.encode(text, add_special_tokens=True)
    return ids, tok.decode(ids[:n], skip_special_tokens=True)

with db.Session() as s:
    row = s.get(db.Diagnosis, 9)
    raw = row.log_excerpt

for label, cleaned in [("BEFORE", old_clean(raw)), ("AFTER", embeddings.clean(raw))]:
    ids, seen = window(cleaned)
    dropped = max(0, len(ids) - 256)
    pct = 100 * dropped / len(ids) if ids else 0
    print("=" * 74)
    print(f"{label}   raw={len(raw)} chars -> cleaned={len(cleaned)} chars -> {len(ids)} tokens")
    print(f"         the model reads 256 of them and DISCARDS {dropped} ({pct:.0f}%)")
    print("-" * 74)
    print("what the model actually sees:")
    print(seen)
    print()

# The number that matters: does the error survive into the window?
_, after_seen = window(embeddings.clean(raw))
_, before_seen = window(old_clean(raw))
for label, seen in [("BEFORE", before_seen), ("AFTER", after_seen)]:
    has_error = "no matching distribution" in seen.lower()
    print(f"{label}: is the actual error line inside the window?  "
          f"{'YES' if has_error else 'NO'}")
