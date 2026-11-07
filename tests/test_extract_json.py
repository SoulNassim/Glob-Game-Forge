"""The chat's actions must survive a model that wraps its JSON in stray text or answers twice."""

from forge.agent.workspace_agent import _extract_json, _is_stray_tool_syntax


def test_written_out_tool_calls_are_not_an_answer():
    assert _is_stray_tool_syntax('<parameter name="x"> </parameter> rinsic')
    assert _is_stray_tool_syntax('Checking first.\n<invoke name="list">')
    assert not _is_stray_tool_syntax("Your boss theme is ready. Press Generate when you want takes.")


def test_plain_and_fenced_json():
    assert _extract_json('{"reply": "hi", "actions": []}') == {"reply": "hi", "actions": []}
    assert _extract_json('```json\n{"reply": "hi"}\n```') == {"reply": "hi"}


def test_stray_tag_before_the_json():
    text = '<invoke name="x">\n</invoke>\n```json\n{"reply": "reading", "actions": [{"tool": "read", "path": "GAME.md"}], "done": false}\n```'
    assert _extract_json(text)["actions"] == [{"tool": "read", "path": "GAME.md"}]


def test_second_answer_wins():
    text = '{"reply": "first", "actions": []}\n```\n\nWait, only one object. Let me correct that:\n{"reply": "second", "actions": [{"tool": "list", "path": "x"}]}'
    data = _extract_json(text)
    assert data["reply"] == "second"
    assert data["actions"] == [{"tool": "list", "path": "x"}]


def test_prose_with_braces_and_no_reply_object():
    assert _extract_json("Use {curly} braces here, then {\"other\": 1} and more {text}") is None
    assert _extract_json("no json at all") is None


def test_inner_action_is_not_mistaken_for_the_reply():
    # A broken outer object: the action inside it must not be taken as the whole answer.
    assert _extract_json('{"reply": oops, "more": {"tool": "write", "path": "a.md"}} trailing }') is None
