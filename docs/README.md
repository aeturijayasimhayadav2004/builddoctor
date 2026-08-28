# docs/

`BuildDoctor-Explained.pdf` — a technical reference for explaining this
project out loud: the request lifecycle, every module, the external services
and the alternative rejected in each case, the data model, the decisions
worth defending, and 30 examination questions with model answers.

Set as a report rather than as a web page: serif body, decimal section
numbering, tables ruled horizontally only, notes between hairlines, and one
restrained accent used for structure. Dark, because it is read on screen at
length. No page numbers — see below for why.

`BuildDoctor-Explained.html` is the source. Edit it and re-render; do not
maintain two documents.

## Regenerating

```bash
python docs/render_pdf.py
```

## Why there is a script instead of a command line

Chrome's `--print-to-pdf` command line does not reliably paint the page
background from a local `file://` URL in this environment, so `render_pdf.py`
drives headless Chrome over the DevTools Protocol instead, where
`Page.printToPDF` takes explicit control of `printBackground`.

`--remote-allow-origins=*` is the other load-bearing flag: Chrome 111+ rejects
a DevTools WebSocket whose `Origin` it does not recognise, with a 403 during
the handshake. The client here is local, so any origin is acceptable.

## There are no page numbers, and that was not the first attempt

A running footer with page numbers was built using `printToPDF`'s
`headerTemplate` / `footerTemplate`, and abandoned after it caused two
separate failures:

**First attempt** — style the footer's own `<div>` and give it a dark
background. Chrome renders each template inside its own document with the
browser's default margin still active, so the div did not reliably fill the
reserved margin box. The result was a thin unpainted white gap at the very
top of every page, cutting into the first line of body text.

**Second attempt** — add a `<style>` reset (`*{margin:0;padding:0}`) inside
the template to fix that gap. This did not fix it. It instead blanked the
**entire main page** — background and text alike — on every page,
reproducibly across repeated re-renders. Whatever Chrome does internally to
apply a template's stylesheet is not scoped the way a separate document
would suggest.

That second failure is strictly worse than the border it was meant to fix, so
the header/footer feature is not used at all. The document is numbered by
section (`1`, `1.1`, `1.2`, …) instead, which is what a reader actually
navigates by.

## Margins are zero, and the page margin is CSS, not print

`printToPDF` is called with `marginTop/Bottom/Left/Right` all `0`, and
`displayHeaderFooter: false`. This was the actual fix for **"huge borders"**:
`printToPDF` paints the background only inside its content box, never inside
its own margins, so any nonzero margin passed to it becomes a white frame
around an otherwise dark page.

The visual margin instead comes from `padding: 20mm 22mm` on `<body>` in the
HTML itself. That alone is not sufficient for a multi-page document — ordinary
padding on a box applies only at the very start and very end of its content,
not at every page break in between, so pages 2 through 26 would come out
flush to the edge with no margin at all. The fix is
`box-decoration-break: clone`, which tells Chromium to re-apply a box's
padding and background at *every* page it is fragmented across. `<body>`
spans the whole document and is fragmented once per page, so this is what
makes every page carry the same margin and the same background.

If the margin or corner treatment ever needs to change, change the `padding`
value on `body` — never add a margin back to the `printToPDF` call.

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

Font and layout defects are both silent, so check the actual output rather
than assuming the source looks right:

```bash
python -c "import pymupdf; d=pymupdf.open('docs/BuildDoctor-Explained.pdf'); \
d[0].get_pixmap(dpi=110).save('check.png'); print(len(d), 'pages')"
```

`pymupdf` (`pip install pymupdf`) rasterises a page to a PNG that can
actually be looked at — this is how both the header/footer failures above
were diagnosed, after guessing from the raw PDF byte stream repeatedly failed
to explain what was on the page.

For the fonts specifically:

```bash
python -c "import re; d=open('docs/BuildDoctor-Explained.pdf','rb').read(); \
print(sorted(set(f.decode() for f in re.findall(rb'/BaseFont\s*/([A-Za-z0-9+-]+)',d))))"
```

Expect exactly `SourceSerif4-Regular`, `SourceSerif4-SemiBold`,
`SourceSerif4-Bold`, and `JetBrainsMono-Regular`. Anything named Arial, Segoe
UI or Consolas means a face failed to load.

To confirm the dark ground reaches every page, extract the fill colours:
`0.082 0.102 0.11 rg` is the page background and should appear once per page
in the decompressed content stream.

## One character to avoid

`→` (U+2192) is absent from several Google text faces, so arrows fall back
to Arial mid-sentence. This document uses `->`. Em dash, en dash and middot
were checked against every embedded family and are safe.
