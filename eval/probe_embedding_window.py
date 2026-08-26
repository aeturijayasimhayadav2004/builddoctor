"""Read-only probe: what does the embedder actually SEE for each row?

Not part of the eval. Written to check one specific hypothesis about an
unexpected result, and kept so the finding can be re-checked.
"""
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db, embeddings

golden = json.loads((pathlib.Path(__file__).parent / "golden_set.json").read_text(encoding="utf-8"))
mid01 = next(c for c in golden["cases"] if c["id"] == "mid-01")

from sentence_transformers import SentenceTransformer
model = embeddings._load()
tok = model.tokenizer

def show(label, text):
    cleaned = embeddings.clean(text)
    ids = tok.encode(cleaned, add_special_tokens=True)
    kept = tok.decode(ids[:256], skip_special_tokens=True)
    print(f"\n{'='*72}\n{label}")
    print(f"  raw chars={len(text)}  after clean()={len(cleaned)}  tokens={len(ids)}")
    print(f"  the model reads the first 256 tokens; that is {len(kept)} chars:")
    print("  " + "-"*68)
    for line in kept.splitlines()[:14]:
        print("   |", line[:110])
    if len(ids) > 256:
        dropped = tok.decode(ids[256:], skip_special_tokens=True)
        print(f"  ...and DISCARDS {len(ids)-256} tokens / {len(dropped)} chars, starting:")
        print("   x", dropped[:150].replace("\n", " ")[:150])

with db.Session() as s:
    for rid in (9, 12, 2):
        row = s.get(db.Diagnosis, rid)
        show(f"ROW {rid}  (lane={row.lane})", row.log_excerpt)

show("mid-01  (my synthetic requests==999.999.999 case)", mid01["log_excerpt"])
