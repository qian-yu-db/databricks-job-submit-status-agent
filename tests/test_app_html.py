"""Guard test for the orchestrator app's inline chat HTML/JS.

Regression: `_CHAT_HTML` was a normal triple-quoted string, so Python turned
the `\\n` inside the inline <script>'s JS string literals into REAL newlines.
A real newline inside a JS string literal is a SyntaxError, which prevents
`go()` from being defined, so clicking "Investigate" did nothing in the browser.
The served HTML must carry a literal backslash-n (which JS interprets), so the
Python source must escape it (raw string or `\\n`).
"""


def test_chat_html_js_uses_literal_newline_escapes():
    from agent.app import _CHAT_HTML

    # Both JS newline usages ("Starting investigation ...\n" and l+'\n') must be
    # literal backslash-n in the served HTML, not Python-interpreted newlines.
    assert _CHAT_HTML.count("\\n") >= 2, (
        "inline <script> JS string literals contain real newlines "
        "(Python-interpreted \\n) — this is a SyntaxError in the browser"
    )


def test_chat_html_has_session_and_background_wiring():
    from agent.app import _CHAT_HTML
    # session persisted in localStorage and sent to the server
    assert "localStorage" in _CHAT_HTML
    assert "randomUUID" in _CHAT_HTML
    assert "session_id" in _CHAT_HTML
    # the background poller flags itself so the server skips the LLM/memory
    assert "background:true" in _CHAT_HTML or "background: true" in _CHAT_HTML
    # a way to start a fresh conversation
    assert "New conversation" in _CHAT_HTML
