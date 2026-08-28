# docs/

`BuildDoctor-Explained.pdf` — a technical reference for explaining this
project out loud: the request lifecycle, every module, the external services
and the alternative rejected in each case, the data model, the decisions
worth defending, and 30 examination questions with model answers.

Set as a report rather than as a web page: serif body, decimal section
numbering, tables ruled horizontally only, notes between hairlines, page
numbers, and one restrained accent used for structure. Dark, because it is
read on screen at length.

`BuildDoctor-Explained.html` is the source. Edit it and re-render; do not
maintain two documents.

## Regenerating

```bash
python docs/render_pdf.py
```

## Why there is a script instead of a command line

Chrome's `--print-to-pdf` **cannot produce a custom footer**. It either omits
headers entirely or prints its own, which includes the `file://` URL and is
styled for white paper. Blink also does not implement the CSS paged-media
margin boxes, so `@bottom-center { content: counter(page) }` is not available
either — there is no route to a page number through CSS alone.

`render_pdf.py` drives headless Chrome over the DevTools Protocol instead,
where `Page.printToPDF` accepts a footer template containing `.pageNumber`.
That is the only way to get "— 7 —" at the foot of a dark page.

Two flags in there are load-bearing and non-obvious:

- `--remote-allow-origins=*` — Chrome 111 and later reject a DevTools
  WebSocket whose `Origin` they do not recognise, with a 403 during the
  handshake. The client is local, so any origin is acceptable.
- `printBackground: true` — without it the dark ground is dropped and the
  PDF prints as dark text on white.

The footer text is set to a light colour deliberately: it renders into the
page margin, which the body background has already painted dark, so the
default near-black would be invisible.

## The fonts are embedded, and that is deliberate

The HTML carries four faces inline as base64 rather than linking
`fonts.googleapis.com`. Two reasons, both found the hard way:

**Headless Chrome does not reliably fetch webfonts when printing a local
file.** An early render came out entirely in Arial, Segoe UI and Consolas —
the fallback stack — with no error of any kind. Nothing in the output says
the typography was lost; it is visible only by inspecting the PDF's font
table.

**Google serves *variable* fonts to modern browsers, and Chrome's PDF
printer silently drops them.** Reproduced in isolation on a three-paragraph
page: JetBrains Mono, which is static, embedded correctly while two variable
families on the same page fell back to Arial. Both the `css` and `css2`
endpoints return a variable build to a current user agent.

The fix is to request the CSS with an **old user agent** — IE11 works — which
still yields per-weight static instances. The tell for a bad build is byte
equality: if two weights of one family arrive as identical bytes, a variable
file was served to both, and the PDF will quietly come out in Arial.

## Verifying a render

Font substitution is silent, so check rather than assume:

```bash
python -c "import re; d=open('docs/BuildDoctor-Explained.pdf','rb').read(); \
print(sorted(set(f.decode() for f in re.findall(rb'/BaseFont\s*/([A-Za-z0-9+-]+)',d))))"
```

Expect `SourceSerif4-Regular`, `SourceSerif4-SemiBold`, `SourceSerif4-Bold`,
`JetBrainsMono-Regular`, and `Georgia`. Georgia is used **only** in the
footer template, so its presence is the proof that the running foot and page
numbers rendered.

Anything named Arial, Segoe UI or Consolas means a face failed to load.

To confirm the dark ground survived, extract the fill colours: `0.082 0.102
0.11 rg` is the page background and should appear on every page.

## One character to avoid

`→` (U+2192) is absent from several Google text faces, so arrows fall back
to Arial mid-sentence. This document uses `->`. Em dash, en dash and middot
were checked against every embedded family and are safe.
