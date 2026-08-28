"""Render the reference document to PDF, with real page numbers.

    python docs/render_pdf.py

Chrome's `--print-to-pdf` command line cannot produce a custom footer: it
either omits headers entirely or prints its own, which includes the file://
URL and is styled for white paper. Blink also does not implement the CSS
paged-media margin boxes (`@bottom-center`), so `counter(page)` is not an
option either.

The DevTools Protocol can. `Page.printToPDF` accepts a footer template with
`.pageNumber` and `.totalPages` placeholders, which is the only route to
"- 7 -" centred at the foot of a dark page. So this drives a headless Chrome
over CDP rather than shelling out to it.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE / "BuildDoctor-Explained.html"
OUTPUT = HERE / "BuildDoctor-Explained.pdf"
PORT = 9223

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome",
    "chromium",
]

# Rendered into the page margin, which the body background has already
# painted dark - so the colour here has to be light or it is invisible.
# Chrome defaults header/footer text to font-size 0; it must be set.
FOOTER = """
<div style="width:100%;font-size:8px;color:#7C8C86;
            font-family:Georgia,serif;padding:0 22mm;
            display:flex;justify-content:space-between;">
  <span>BuildDoctor &mdash; technical reference</span>
  <span>&mdash;&nbsp;<span class="pageNumber"></span>&nbsp;&mdash;</span>
</div>
"""

# printToPDF insists on a header template when displayHeaderFooter is on.
# An empty div is the way to ask for nothing at the top.
HEADER = '<div style="font-size:0"></div>'


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if pathlib.Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    sys.exit("no Chrome or Edge found")


def wait_for_devtools(timeout: float = 30.0) -> str:
    """Chrome takes a moment to open the port. Poll rather than sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/json/version", timeout=2
            ) as response:
                return json.load(response)["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.3)
    sys.exit("Chrome never opened its debugging port")


def call(ws: websocket.WebSocket, method: str, params: dict | None = None,
         *, _id=[0]) -> dict:
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": method, "params": params or {}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == _id[0]:
            if "error" in message:
                sys.exit(f"{method} failed: {message['error']}")
            return message.get("result", {})


def main() -> int:
    if not SOURCE.exists():
        sys.exit(f"missing {SOURCE}")

    chrome = find_chrome()
    process = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            f"--remote-debugging-port={PORT}",
            # Chrome 111+ refuses DevTools sockets whose Origin it does not
            # know. The client here is local, so any origin is acceptable.
            "--remote-allow-origins=*",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--user-data-dir=" + str(HERE / ".chrome-profile"),
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        browser_ws = wait_for_devtools()
        ws = websocket.create_connection(browser_ws, timeout=120)

        target = call(ws, "Target.createTarget", {"url": "about:blank"})
        session = call(
            ws, "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True},
        )["sessionId"]

        def send(method, params=None, _id=[1000]):
            _id[0] += 1
            ws.send(json.dumps({
                "id": _id[0], "sessionId": session,
                "method": method, "params": params or {},
            }))
            while True:
                message = json.loads(ws.recv())
                if message.get("id") == _id[0]:
                    if "error" in message:
                        sys.exit(f"{method} failed: {message['error']}")
                    return message.get("result", {})

        send("Page.enable")
        send("Page.navigate", {"url": SOURCE.as_uri()})

        # Fonts are embedded as data URIs, so there is nothing to download -
        # but layout and font decoding still need a beat before printing.
        time.sleep(3)

        result = send("Page.printToPDF", {
            "printBackground": True,          # or the dark ground is dropped
            "paperWidth": 8.27,               # A4
            "paperHeight": 11.69,
            "marginTop": 0.83,                # 21mm
            "marginBottom": 0.83,
            "marginLeft": 0.87,               # 22mm
            "marginRight": 0.87,
            "displayHeaderFooter": True,
            "headerTemplate": HEADER,
            "footerTemplate": FOOTER,
            "preferCSSPageSize": False,
        })

        import base64
        OUTPUT.write_bytes(base64.b64decode(result["data"]))
        ws.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(HERE / ".chrome-profile", ignore_errors=True)

    size = OUTPUT.stat().st_size
    print(f"wrote {OUTPUT.name}  ({size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
