#!/usr/bin/env python3
"""build_guide_pdf.py — render a docs/ markdown file to a sendable PDF.

The beta guide has to go to people who are not going to read Markdown in a
terminal, so it needs to exist as a file you can attach to an email.

**The PDF is a build output.** Edit the Markdown and re-run this; never edit
the PDF. That is the same rule the project already applies to its other SOP
deliverables.

Two external tools, neither a Python dependency and neither needed to run the
application:

* **pandoc** turns Markdown into HTML. ``brew install pandoc``
* **Google Chrome** prints that HTML to PDF. Already required by this project,
  which drives it for everything else.

Chrome rather than a Python PDF library because the guide is mostly tables,
code blocks and callouts, and a browser engine lays those out properly with no
work. A library would mean rebuilding typography by hand.

Usage:
    python tools/build_guide_pdf.py                       # the beta guide
    python tools/build_guide_pdf.py --source docs/X.md --out X.pdf
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

DEFAULT_SOURCE = os.path.join(REPO, "docs", "BETA-TESTER-GUIDE.md")
DEFAULT_OUT = os.path.join(REPO, "dist", "LinkedIn-Autocomment-Beta-Guide.pdf")

# Print styling. Deliberately plain: this is an instruction sheet somebody will
# follow with a laptop open, not a brochure. What matters is that code blocks
# are unambiguous, callouts stand out, and nothing breaks across a page in a
# way that hides a step.
CSS = """
@page { size: Letter; margin: 18mm 16mm 20mm 16mm; }

body {
  font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.55; color: #1a1a1a; margin: 0;
}

h1 {
  font-size: 22pt; line-height: 1.2; margin: 0 0 4pt 0;
  border-bottom: 3px solid #0a66c2; padding-bottom: 10pt; color: #0a1929;
}
h2 {
  font-size: 14pt; margin: 22pt 0 8pt 0; color: #0a66c2;
  border-bottom: 1px solid #d8dee4; padding-bottom: 4pt;
  /* A step heading stranded at the foot of a page loses its instructions. */
  break-after: avoid; break-inside: avoid;
}
h3 { font-size: 11.5pt; margin: 14pt 0 5pt 0; color: #0a1929; break-after: avoid; }

p { margin: 0 0 8pt 0; }
ol, ul { margin: 0 0 8pt 0; padding-left: 20pt; }
li { margin-bottom: 4pt; }
strong { color: #000; }

/* Commands people will copy. High contrast, and never split across pages:
   half a command is worse than none. */
pre {
  background: #f5f7f9; border: 1px solid #d8dee4; border-left: 3px solid #0a66c2;
  border-radius: 3px; padding: 9pt 11pt; margin: 8pt 0;
  font-size: 9.5pt; line-height: 1.45; white-space: pre-wrap;
  word-break: break-word; break-inside: avoid;
}
code {
  font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 9.5pt;
}
p > code, li > code, td > code {
  background: #eef1f4; padding: 1pt 4pt; border-radius: 3px; color: #333;
}
pre > code { background: none; padding: 0; }

/* Callouts. The guide uses blockquotes for the things that go wrong if
   skipped, so they must not read as decoration. */
blockquote {
  margin: 10pt 0; padding: 8pt 12pt; background: #fff8e6;
  border-left: 4px solid #e8a33d; border-radius: 3px; break-inside: avoid;
}
blockquote p:last-child { margin-bottom: 0; }

table {
  border-collapse: collapse; width: 100%; margin: 10pt 0;
  font-size: 9.5pt; break-inside: avoid;
}
th {
  background: #0a1929; color: #fff; text-align: left;
  padding: 6pt 8pt; font-weight: 600;
}
td { padding: 5pt 8pt; border-bottom: 1px solid #e1e6ea; vertical-align: top; }
tr:nth-child(even) td { background: #fafbfc; }

hr { border: none; border-top: 1px solid #e1e6ea; margin: 16pt 0; }

a { color: #0a66c2; text-decoration: none; }
"""


def _wait_for_pdf(out, process, timeout=90.0, settle=1.0):
    """Wait until ``out`` exists and has stopped growing.

    Size-stable rather than merely present, because the file appears as soon as
    Chrome starts writing it and a PDF truncated mid-write opens to a blank
    page in some readers and an error in others.
    """
    import time

    deadline = time.monotonic() + timeout
    last_size, stable_since = -1, None

    while time.monotonic() < deadline:
        if process.poll() is not None and not os.path.exists(out):
            raise RuntimeError(
                f"Chrome exited with {process.returncode} without writing a PDF")

        size = os.path.getsize(out) if os.path.exists(out) else -1
        if size > 0 and size == last_size:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= settle:
                return
        else:
            stable_since = None
        last_size = size
        time.sleep(0.2)

    raise RuntimeError(f"Chrome did not finish writing {out} within {timeout:.0f}s")


def require(condition, message):
    if not condition:
        print(f"ERROR: {message}", file=sys.stderr)
        sys.exit(1)


def build(source, out):
    require(os.path.isfile(source), f"no such file: {source}")
    require(shutil.which("pandoc"),
            "pandoc is not installed. Install it with: brew install pandoc")
    require(os.path.exists(CHROME),
            f"Google Chrome not found at {CHROME}")

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        css_path = os.path.join(tmp, "style.css")
        with open(css_path, "w", encoding="utf-8") as handle:
            handle.write(CSS)

        html_path = os.path.join(tmp, "guide.html")
        subprocess.run(
            ["pandoc", source, "--standalone", "--from", "gfm",
             "--metadata", "title=", "--css", css_path,
             "--embed-resources", "--output", html_path],
            check=True,
        )

        # A dedicated profile directory keeps this from touching the user's
        # real Chrome session, which for this project is a logged-in LinkedIn.
        #
        # --headless=new, not --headless: the old mode hung indefinitely on
        # --print-to-pdf here rather than failing, so the first version of this
        # script timed out with no error to read.
        #
        # --virtual-time-budget makes Chrome fast-forward its timers and print
        # as soon as layout settles, instead of waiting on the page.
        #
        # And do not wait for Chrome to exit. It writes the PDF and then keeps
        # running, so waiting on the process times out even though the job is
        # done. Watch for the file to appear and stop growing instead, then
        # stop Chrome. The first two versions of this both "failed" on a PDF
        # that had already been written correctly.
        if os.path.exists(out):
            os.remove(out)

        chrome = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={os.path.join(tmp, 'chrome')}",
             "--virtual-time-budget=5000",
             # Without this Chrome stamps today's date and the file:// path
             # across the top of every page, which is not something to send
             # to somebody.
             "--no-pdf-header-footer",
             f"--print-to-pdf={out}", f"file://{html_path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_pdf(out, chrome)
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()

    require(os.path.isfile(out), "Chrome reported success but wrote no file")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    out = build(args.source, args.out)
    size_kb = os.path.getsize(out) / 1024
    print(f"Built: {out}  ({size_kb:.0f} KB)")
    print()
    print("This is a build output. Edit the Markdown and re-run; do not edit "
          "the PDF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
