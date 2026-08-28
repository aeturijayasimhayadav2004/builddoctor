# docs/

`BuildDoctor-Explained.pdf` — a 30-page technical reference for explaining
this project out loud: the request lifecycle, every file, the external
services and the alternative rejected in each case, the data model, the
decisions worth defending, and 30 interview questions with answers.

`BuildDoctor-Explained.html` is the source. The PDF is rendered from it, so
edit the HTML and re-render rather than maintaining two documents.

## Regenerating the PDF

```bash
chrome --headless --disable-gpu --print-to-pdf-no-header \
  --print-to-pdf="docs/BuildDoctor-Explained.pdf" \
  "file:///absolute/path/to/docs/BuildDoctor-Explained.html"
```

Chrome needs an **absolute `file://` URL** and an absolute output path.
A relative path fails with `Access is denied`, which reads like a permissions
problem and is not one.

## The fonts are embedded, and that is deliberate

The HTML carries five webfaces inline as base64 rather than linking
`fonts.googleapis.com`. Two reasons, both learned the hard way:

**Headless Chrome does not reliably fetch webfonts when printing a local
file.** The first render silently came out in Arial, Segoe UI and Consolas —
the fallback stack — with no error. Nothing in the output says the
typography was lost; you only notice by inspecting the PDF's font list.

**Google serves *variable* fonts to modern browsers, and Chrome's PDF
printer drops them.** Reproduced in isolation on a three-paragraph page:
JetBrains Mono (static) embedded correctly while Source Sans 3 and Familjen
Grotesk (variable) fell back to Arial. Both the `css` and `css2` endpoints
return a variable build to a current Chrome user agent.

The fix is to request the fonts with an **old user agent** (IE11 works),
which still gets per-weight static instances. The tell for a bad build is
byte equality: if two weights of one family arrive as identical bytes, a
variable file was served to both.

If you ever re-embed fonts, verify afterwards that the PDF actually uses
them:

```bash
python -c "import re; d=open('docs/BuildDoctor-Explained.pdf','rb').read(); \
print(sorted(set(f.decode() for f in re.findall(rb'/BaseFont\s*/([A-Za-z0-9+-]+)',d))))"
```

Expect `FamiljenGrotesk-SemiBold`, `FamiljenGrotesk-Bold`,
`SourceSans3ExtraLight-Regular`, `SourceSans3ExtraLight-SemiBold` and
`JetBrainsMono-Regular`. The `ExtraLight` in those names is Google's internal
naming for the static build — the real `usWeightClass` values are 400 and
600, which was checked rather than assumed.

## One character to avoid

`→` (U+2192) is **not in Source Sans 3**, so every arrow fell back to Arial
mid-sentence. The document uses `->` instead. Em dash, en dash and middot are
all present in all three families and are safe to use.
