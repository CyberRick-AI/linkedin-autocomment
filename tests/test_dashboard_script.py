"""The dashboard's inline script must parse. All of it, on every change.

Written after breaking it. A stray edit removed one closing parenthesis, and
the failure mode was total and silent: the browser aborted the script parse,
**every** function in the page became undefined, and the only symptom was that
buttons did nothing. Not the ones near the typo — all of them, including
``startLogin``, which was fine an hour earlier.

That is the worst shape a failure can take in this project, and it is the exact
class Phase 5b existed to remove: a control that declines to act and says
nothing. 850 passing tests said nothing about it, because none of them parse
the script.
"""

import shutil
import subprocess

import pytest

from pathlib import Path


TEMPLATE = Path(__file__).parent.parent / "linkedin_automation" / "templates" / "dashboard.html"


def extract_script() -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.count("<script>") == 1, "more than one script block; update this test"
    return html.split("<script>", 1)[1].rsplit("</script>", 1)[0]


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the syntax gate needs a JS parser")
def test_the_inline_script_parses(tmp_path):
    js = tmp_path / "dashboard.js"
    js.write_text(extract_script(), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(js)],
                            capture_output=True, text=True)
    assert result.returncode == 0, (
        "the dashboard's inline script has a syntax error, which makes EVERY "
        "button in the page dead, not just the one near the mistake:\n"
        + result.stderr)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_gate_catches_a_deliberate_break(tmp_path):
    """A gate that has never failed has not been tested. Same discipline as
    Phase 1's CI proof."""
    js = tmp_path / "broken.js"
    js.write_text(extract_script() + "\nfunction broken( {\n", encoding="utf-8")
    result = subprocess.run(["node", "--check", str(js)],
                            capture_output=True, text=True)
    assert result.returncode != 0


def test_every_onclick_handler_exists_in_the_script():
    """Cheap and parser-free: catches a renamed or deleted handler, which is a
    dead button with no error in the console at all."""
    import re

    html = TEMPLATE.read_text(encoding="utf-8")
    script = extract_script()
    handlers = set(re.findall(r'on(?:click|change)="(\w+)\(', html))
    assert handlers, "no inline handlers found; the markup shape changed"

    missing = [name for name in sorted(handlers)
               if not re.search(rf"(?:async\s+)?function\s+{name}\s*\(", script)]
    assert missing == [], f"handlers with no function: {missing}"
