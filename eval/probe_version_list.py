"""Read-only probe: is the long pip version list driving the similarity?

Compares mid-01 against row 9 as written, then again with mid-01's version
list padded to the same length as the real one. If the second is much
higher, the list is what the embedding is mostly about.
"""
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db, embeddings

golden = json.loads((pathlib.Path(__file__).parent / "golden_set.json").read_text(encoding="utf-8"))
mid01 = next(c for c in golden["cases"] if c["id"] == "mid-01")["log_excerpt"]

with db.Session() as s:
    row9 = s.get(db.Diagnosis, 9).log_excerpt
    row2 = s.get(db.Diagnosis, 2).log_excerpt

def sim(a, b):
    import numpy as np
    va, vb = embeddings.embed(a), embeddings.embed(b)
    return float(np.dot(va, vb))

# The real long list, lifted straight out of row 9.
start = row9.find("(from versions:")
end = row9.find(")", start)
real_list = row9[start:end + 1]
print(f"  row 9's version list is {len(real_list)} chars long\n")

# mid-01, but with the same wall of versions.
padded = mid01.replace(
    "(from versions: 2.0.0, 2.0.1, 2.1.0, 2.2.1, 2.31.0, 2.32.3)", real_list
)
assert padded != mid01, "substitution failed"

print(f"  sim(mid-01 as written      , row 9) = {sim(mid01,  row9):.4f}")
print(f"  sim(mid-01 + real long list, row 9) = {sim(padded, row9):.4f}")
print(f"  sim(mid-01 as written      , row 2) = {sim(mid01,  row2):.4f}")
print(f"  sim(mid-01 + real long list, row 2) = {sim(padded, row2):.4f}")

# And the control: does an UNRELATED failure carrying the same long list
# also score high against row 9? If it does, the list alone is enough to
# fake a match, which is the alarming version of this finding.
fake = row2.replace("ERROR: file or directory not found: tests/",
                    "ERROR: file or directory not found: tests/\n" + real_list)
print(f"\n  control - row 2's failure with row 9's version list pasted in:")
print(f"  sim(that, row 9) = {sim(fake, row9):.4f}   (row 2 alone scores {sim(row2, row9):.4f})")
