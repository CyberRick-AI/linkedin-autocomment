"""The app's window must answer the page's alert(), confirm() and prompt().

**The bug this exists for.** WKWebView, unlike a browser, shows no JavaScript
dialogs on its own: with no ``WKUIDelegate`` every ``alert()`` is dropped and
every ``confirm()`` returns false. The dashboard uses dozens of them — every
"Delete this?" guard and most error reports — so inside the macOS app those
buttons silently did nothing, while the same page worked in a browser.

The delegate is tested with an injected ``ask`` function instead of a real
``NSAlert``, which would block on a modal no test can click.
"""

import sys

import pytest

if sys.platform != "darwin":
    pytest.skip("the app window is macOS-only", allow_module_level=True)
pytest.importorskip("AppKit")
pytest.importorskip("WebKit")

from linkedin_automation import macapp_ui  # noqa: E402


class FakeAsk:
    """Records what the delegate asked, answers with a canned reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, kind, message, default=None):
        self.calls.append((kind, message, default))
        return self.reply


def _delegate(reply):
    ask = FakeAsk(reply)
    return macapp_ui.WebUIDelegate.alloc().initWithAsk_(ask), ask


def test_alert_is_shown_and_the_page_is_released():
    delegate, ask = _delegate(None)
    done = []
    delegate.webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(
        None, "Profile saved", None, lambda: done.append(True))
    assert ask.calls == [("alert", "Profile saved", None)]
    assert done == [True]


@pytest.mark.parametrize("answer", [True, False])
def test_confirm_returns_the_operators_answer(answer):
    delegate, ask = _delegate(answer)
    got = []
    delegate.webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
        None, "Delete this profile?", None, got.append)
    assert ask.calls == [("confirm", "Delete this profile?", None)]
    assert got == [answer]


@pytest.mark.parametrize("answer", ["rick", None])
def test_prompt_returns_the_text_or_null_on_cancel(answer):
    delegate, ask = _delegate(answer)
    got = []
    delegate.webView_runJavaScriptTextInputPanelWithPrompt_defaultText_initiatedByFrame_completionHandler_(
        None, "Profile name?", "default", None, got.append)
    assert ask.calls == [("prompt", "Profile name?", "default")]
    assert got == [answer]


def test_a_failing_dialog_still_releases_the_page():
    """A completion handler that is never called hangs the page for good."""
    def boom(*_):
        raise RuntimeError("no window server")
    delegate = macapp_ui.WebUIDelegate.alloc().initWithAsk_(boom)
    got = []
    delegate.webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
        None, "Delete?", None, got.append)
    assert got == [False]


def test_the_window_installs_the_delegate():
    """The wiring: a delegate nobody installs fixes nothing."""
    import inspect
    source = inspect.getsource(macapp_ui.DashboardWindow.__init__)
    assert "setUIDelegate_" in source
