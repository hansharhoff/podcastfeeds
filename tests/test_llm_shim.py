"""The shim is the only component that can see whether the model actually
searched. Before this, thirteen digests were built with web search granted and
nothing anywhere recorded a single query."""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "llm_shim", Path(__file__).resolve().parent.parent / "scripts" / "llm_shim.py")
llm_shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llm_shim)


def _line(obj):
    return json.dumps(obj)


def _transcript(*events):
    return "\n".join(_line(e) for e in events)


SEARCH_RESULT = (
    'Web search results for query: "claude code session urls"\n\n'
    'Links: [{"title":"Issue 66504","url":"https://github.com/anthropics/claude-code/issues/66504"},'
    '{"title":"Docs","url":"https://docs.claude.com/en/docs/claude-code"}]'
)


def test_parses_text_and_the_searches_behind_it():
    raw = _transcript(
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "considering"},
            {"type": "tool_use", "id": "t1", "name": "WebSearch",
             "input": {"query": "claude code session urls"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": SEARCH_RESULT},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "The final script."},
        ]}},
        {"type": "result", "result": "The final script."},
    )
    text, searches = llm_shim.parse_stream_json(raw)

    assert text == "The final script."
    assert len(searches) == 1
    assert searches[0]["query"] == "claude code session urls"
    assert searches[0]["urls"] == [
        "https://github.com/anthropics/claude-code/issues/66504",
        "https://docs.claude.com/en/docs/claude-code",
    ]


def test_no_searches_is_reported_as_empty_not_missing():
    """An empty list is the finding — the model answered from nothing."""
    raw = _transcript(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Script."}]}},
        {"type": "result", "result": "Script."},
    )
    text, searches = llm_shim.parse_stream_json(raw)
    assert text == "Script."
    assert searches == []


def test_falls_back_to_assistant_text_without_a_result_event():
    raw = _transcript(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Part one."}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Part two."}]}},
    )
    text, _ = llm_shim.parse_stream_json(raw)
    assert text == "Part one.\nPart two."


def test_a_junk_line_does_not_cost_us_the_reply():
    raw = "not json at all\n" + _transcript({"type": "result", "result": "Script."})
    text, _ = llm_shim.parse_stream_json(raw)
    assert text == "Script."


def test_webfetch_is_recorded_by_url():
    raw = _transcript(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t9", "name": "WebFetch",
             "input": {"url": "https://example.com/post", "prompt": "read it"}},
        ]}},
        {"type": "result", "result": "Script."},
    )
    _, searches = llm_shim.parse_stream_json(raw)
    assert searches[0]["tool"] == "WebFetch"
    assert searches[0]["query"] == "https://example.com/post"


def test_unrelated_tools_are_not_logged_as_research():
    """The CLI loads WebSearch's schema via ToolSearch first; that is plumbing,
    not a source, and must not inflate the search count."""
    raw = _transcript(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t0", "name": "ToolSearch",
             "input": {"query": "select:WebSearch"}},
        ]}},
        {"type": "result", "result": "Script."},
    )
    _, searches = llm_shim.parse_stream_json(raw)
    assert searches == []
