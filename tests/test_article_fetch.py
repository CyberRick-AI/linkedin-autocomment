"""The post creator's article path (Phase 12b).

Found 2026-08-02 by Rick pressing **Generate from Article**. Thought
leadership worked and published; the article half raised
``ValueError: Could not fetch article content``.

**Phase 12 covered the post creator and missed this.** It tested
``generate_thought_leadership`` and never ``generate_from_article``, so it
proved half a surface while reporting the surface covered. Testing the entry
point Rick happened to press first is not the same as testing the entry
points, and the article path would have failed on the first line of any test
that touched it.

Three defects, stacked, each hiding the next:

1. ``beautifulsoup4`` was imported by this module and declared nowhere. Both
   fetch strategies funnel through ``_parse_html``, so the whole feature was
   dead on any clean install, not just Rick's.
2. The request advertised ``Accept-Encoding: gzip, deflate, br``. ``requests``
   decodes Brotli only when the brotli package is installed, and it is not.
   Substack honours the header, so the body came back as 29KB of undecodable
   binary instead of 188KB of HTML, and parsed to nothing.
3. Nothing said so. The parser returned ``None`` without logging when it
   extracted no text, the two fetch strategies logged their failures at debug,
   and in-process dashboard jobs never captured this package's log records at
   all. The one message that named the real cause was written at ERROR level
   and displayed nowhere.

Offline: every test here parses a local HTML string or a fake response. No
network, per PROJECT.md section 8.
"""

import logging

import pytest

from linkedin_automation import post_generator as pg


ARTICLE_HTML = """
<html><head><title>How to Use Grok 4.5 in VS Code</title></head>
<body>
  <header>site chrome</header>
  <nav>menu</nav>
  <article>
    <h1>How to Use Grok 4.5 in VS Code</h1>
    <p>%s</p>
    <p>%s</p>
  </article>
  <footer>footer junk</footer>
  <script>var tracking = 1;</script>
</body></html>
""" % ("Yes, you can run it inside the editor today. " * 6,
       "The setup takes about five minutes end to end. " * 6)


@pytest.fixture
def generator():
    """A PostGenerator with no provider, since fetching never calls one."""
    return pg.PostGenerator.__new__(pg.PostGenerator)


# ─── The dependency that was never declared ───────────────────────────────────

def test_beautifulsoup_is_importable():
    """The whole feature is dead without it, and it was declared nowhere.

    Deliberately a plain import check rather than a parse. `requirements.txt`
    is the fix; this fails on an install that did not apply it, which is the
    state every clean clone was in.
    """
    import bs4  # noqa: F401


def test_requests_is_importable():
    import requests  # noqa: F401


def test_both_fetch_strategies_depend_on_the_parser(generator):
    """Why a missing parser killed the feature rather than degrading it.

    The requests path and the Selenium path look like redundancy. They are
    not: they differ only in how the HTML is obtained and both hand it to
    ``_parse_html``. A fallback that shares the broken component is not a
    fallback, and that is why a single missing import took the whole feature.
    """
    import inspect

    for name in ("_fetch_with_requests", "_fetch_with_selenium"):
        source = inspect.getsource(getattr(pg.PostGenerator, name))
        assert "_parse_html" in source, f"{name} no longer routes through the parser"


# ─── The header that promised an encoding we cannot decode ────────────────────

def test_the_fetcher_does_not_advertise_an_encoding_it_cannot_decode(generator):
    """The Brotli defect, asserted at the source rather than over the network.

    ``requests`` sets ``Accept-Encoding`` from the codecs actually installed.
    Overriding it with a hardcoded list is how the client came to promise
    Brotli it could not read, and the failure was invisible: HTTP 200, a body
    of the wrong bytes, and a parser that found no article.
    """
    import inspect

    source = inspect.getsource(pg.PostGenerator._fetch_with_requests)
    header_lines = [
        line for line in source.splitlines()
        if "'Accept-Encoding'" in line and not line.strip().startswith("#")
    ]
    assert header_lines == [], (
        "Accept-Encoding is being set by hand again. Let requests advertise "
        f"what it can decode. Found: {header_lines}"
    )


def test_undecodable_bytes_are_reported_not_silently_empty(generator, caplog):
    """The exact shape of the Brotli failure: valid response, unusable body.

    This is the test that would have caught it. Binary in, nothing extracted,
    and before the fix that combination produced no log line at all, so it was
    indistinguishable from a page that genuinely has no article.
    """
    garbage = "\x1f\x8b\x08\x00" + "".join(chr((i * 7) % 256) for i in range(3000))

    with caplog.at_level(logging.WARNING, logger="linkedin_automation.post_generator"):
        result = generator._parse_html(garbage)

    assert result is None
    assert caplog.records, "extracting nothing from 3KB of bytes was not reported"
    assert "no article text" in caplog.text


# ─── The parser, on a page shaped like the one that failed ────────────────────

def test_the_parser_extracts_the_article_and_drops_the_chrome(generator):
    """Real extraction, on markup shaped like the page Rick submitted."""
    text = generator._parse_html(ARTICLE_HTML)

    assert text
    assert "How to Use Grok 4.5 in VS Code" in text
    assert "run it inside the editor today" in text
    assert "five minutes end to end" in text

    for noise in ("site chrome", "menu", "footer junk", "var tracking"):
        assert noise not in text, f"{noise!r} survived into the article text"


def test_a_page_with_no_text_at_all_returns_none_and_says_why(generator, caplog):
    """Empty. A document with markup but no readable content."""
    with caplog.at_level(logging.WARNING, logger="linkedin_automation.post_generator"):
        result = generator._parse_html(
            "<html><body><script>x=1</script><svg></svg></body></html>")

    assert result is None
    assert "no article text" in caplog.text


def test_a_thin_page_is_rejected_one_level_up_not_by_the_parser(generator, monkeypatch):
    """The length bar lives in ``_fetch_article``, and that is the right layer.

    ``_parse_html`` returns whatever it found, however little. A page holding
    one short sentence is not an error and not an article, and the caller is
    what decides that a fetch produced too little to react to. Asserted so a
    later change does not move the bar into the parser and make the two
    strategies disagree about what counts as success.
    """
    thin = "<html><body><p>Hi.</p></body></html>"

    assert generator._parse_html(thin) is not None

    monkeypatch.setattr(pg.PostGenerator, "_fetch_with_requests", lambda self, url: "Hi.")
    monkeypatch.setattr(pg.PostGenerator, "_fetch_with_selenium", lambda self, url: "Hi.")

    assert generator._fetch_article("https://example.com/a") is None


def test_the_parser_survives_malformed_html(generator):
    """Malformed. A truncated response must not raise into the job."""
    truncated = ARTICLE_HTML[: len(ARTICLE_HTML) // 2]

    result = generator._parse_html(truncated)

    assert result is None or isinstance(result, str)


def test_an_empty_response_body_returns_none(generator):
    """Empty input, the degenerate case."""
    assert generator._parse_html("") is None


# ─── The error the operator actually sees ─────────────────────────────────────

def test_a_missing_dependency_names_the_dependency(generator, monkeypatch, caplog):
    """The message that existed, was correct, and reached nobody.

    Asserted on content, because "could not fetch article content" is true of
    a missing library, a paywall, a blocked request and a JS-only page, and
    tells the operator which of those to do something about: none.
    """
    def no_bs4(name, *args, **kwargs):
        if name == "bs4":
            raise ImportError("No module named 'bs4'", name="bs4")
        return original(name, *args, **kwargs)

    original = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__
    monkeypatch.setattr("builtins.__import__", no_bs4)

    with caplog.at_level(logging.ERROR, logger="linkedin_automation.post_generator"):
        result = generator._fetch_with_requests("https://example.com/a")

    assert result is None
    assert "bs4" in caplog.text
    assert "requirements.txt" in caplog.text, (
        "the error must say how to fix it, not just what broke"
    )


def test_generate_from_article_refuses_rather_than_posting_an_empty_reaction(
    generator, monkeypatch
):
    """The consequence that matters: no fetch means no post, loudly.

    Degrading to an empty or hallucinated reaction would be worse than the
    error, because it ends up on LinkedIn under Rick's name. PROJECT.md
    section 5 says to degrade by stopping.
    """
    monkeypatch.setattr(pg.PostGenerator, "_fetch_article", lambda self, url: None)

    with pytest.raises(ValueError) as exc:
        generator.generate_from_article("https://example.com/a")

    assert "https://example.com/a" in str(exc.value)


# ─── The job log that showed the operator none of this ────────────────────────

def test_a_library_warning_reaches_the_job_log(monkeypatch):
    """The dashboard defect, which is why the actionable message was invisible.

    Jobs that shell out are diagnosable: ``run_subprocess`` streams stdout and
    logs stderr. Jobs that call a library in-process had no such route, so the
    job log held only what the job function passed to ``log_job`` by hand.
    Everything the library reported went to the server console.

    That is the Phase 5b defect in a second place: a correct message that
    reaches nobody is the same as no message.
    """
    from linkedin_automation import dashboard as dash

    job_id = "test_job_1"
    dash.jobs[job_id] = {"status": "running", "log": [], "progress": ""}
    handler = dash._JobLogHandler(job_id)
    package_logger = logging.getLogger("linkedin_automation")
    package_logger.addHandler(handler)
    try:
        logging.getLogger("linkedin_automation.post_generator").warning(
            "Article fetching needs beautifulsoup4")
        logging.getLogger("linkedin_automation.post_generator").debug(
            "chatty tracing nobody needs")
    finally:
        package_logger.removeHandler(handler)
        entries = dash.jobs.pop(job_id)["log"]

    assert any("beautifulsoup4" in line for line in entries), (
        f"the warning never reached the job log: {entries}"
    )
    assert not any("chatty tracing" in line for line in entries), (
        "debug records are tracing; a job log that scrolls is one nobody reads"
    )


def test_the_handler_is_removed_when_a_job_finishes(monkeypatch):
    """A handler left attached keeps writing into a job that already ended.

    Asserted because the removal sits in a ``finally`` whose whole purpose is
    the failure path, and nothing else would notice it missing until a later
    job's warnings started appearing under an earlier job's id.
    """
    from linkedin_automation import dashboard as dash

    package_logger = logging.getLogger("linkedin_automation")
    before = list(package_logger.handlers)

    def job(jid):
        raise RuntimeError("boom")

    job_id = dash.run_job("test_job_2", job)

    deadline = 50
    while dash.jobs[job_id]["status"] == "running" and deadline:
        import time as _t
        _t.sleep(0.01)
        deadline -= 1

    try:
        assert dash.jobs[job_id]["status"] == "failed"
        assert package_logger.handlers == before, (
            "a job handler outlived its job"
        )
    finally:
        dash.jobs.pop(job_id, None)
