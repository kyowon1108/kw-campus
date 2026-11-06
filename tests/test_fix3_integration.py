"""1.0 integration: what the bridge passes on reaches the PC agent in the form the agent reads.

Made-up courses, titles and ids only; no KLAS, school servers or AWS (moto)."""

import re
from datetime import UTC, datetime
from pathlib import Path

import test_bridge
from kw_mcp import agent as kw_agent
from kw_mcp import release
from test_bridge import FakeDevice, mcp_call, oauth, register, tool

from kwcampus.bridge import app as bridge
from kwcampus.bridge import install, store

env = test_bridge.env


def _connected(env, version=kw_agent.PROTOCOL_VERSION):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1", version)
    _, tokens = oauth(c, device)
    return c, tokens["access_token"]


def _calls(env):
    return [m for _, m in env["sent"] if m["type"] == "call"]


# --- S27: the numeric ids my_deadlines hands out --------------------------------------------------

def test_detail_tools_take_the_numeric_ids_the_deadline_rows_give(env):
    """my_deadlines passes on the agent's rows, where an assignment's id is KLAS's number (an int).
    The detail tools take it as given and send it as text, the only form the agent reads."""
    c, token = _connected(env)
    for name, args in (("my_assignment_detail", {"title": "HW3", "id": 3}),
                       ("my_course_post", {"title": "공지", "id": 5}),
                       ("my_attachment", {"title": "HW3", "source": "assignment", "id": 7, "file_index": 2}),
                       ("my_assignment_detail", {"title": "HW3", "id": "3"})):
        out = tool(c, token, name, course="데이터", **args)
        assert not out.get("isError"), out
    sent = _calls(env)
    assert [(m["op"], m["args"]["id"]) for m in sent] == [
        ("assignment_detail", "3"), ("course_post", "5"), ("read_attachment", "7"), ("assignment_detail", "3")]
    assert all(isinstance(v, str) for m in sent for v in m["args"].values())
    # the agent renames them to the service's parameters and keeps the text
    assert kw_agent.call_args("assignment_detail", sent[0]["args"])["task_id"] == "3"
    assert kw_agent.call_args("course_post", sent[1]["args"])["post_id"] == "5"
    picked = kw_agent.call_args("read_attachment", sent[2]["args"])
    assert picked["item_id"] == "7" and picked["file_index"] == "2" and picked["source"] == "assignment"


def test_detail_tools_advertise_numeric_ids(env):
    c, token = _connected(env)
    listed = {t["name"]: t for t in mcp_call(c, token, "tools/list", {}).json()["result"]["tools"]}
    for name in ("my_assignment_detail", "my_course_post", "my_attachment"):
        types = {o.get("type") for o in listed[name]["inputSchema"]["properties"]["id"]["anyOf"]}
        assert {"string", "integer"} <= types, (name, types)


def test_an_id_left_out_is_not_sent(env):
    c, token = _connected(env)
    tool(c, token, "my_assignment_detail", course="데이터", title="HW3")
    assert "id" not in _calls(env)[0]["args"]
    assert bridge._id_arg(None) is None and bridge._id_arg(0) == "0" and bridge._id_arg("5-678") == "5-678"


# --- the first install and an agent update install a release the same way -------------------------

def _flags(argv: list[str]) -> set[str]:
    return {a for a in argv if a.startswith("--")}


def _line(script: str, start: str) -> list[str]:
    """The words of the script line that starts with `start` (after the shell's `if !`)."""
    found = [line for line in script.splitlines() if line.lstrip("if! ").startswith(start)]
    assert len(found) == 1, (start, found)
    return found[0].replace("\\", " ").split()


def test_installers_check_hashes_and_refuse_builds_like_an_agent_update(tmp_path):
    pins = tmp_path / release.constraints_name("1.0.0")
    exported = "httpx==0.28.1 \\\n    --hash=sha256:" + "0" * 64 + "\n"
    pins.write_text(release.constraints_text(exported, "1.0.0", datetime(2026, 11, 1, tzinfo=UTC)))
    wheel = tmp_path / "kw_mcp-1.0.0-py3-none-any.whl"
    install_step, hash_step = kw_agent.install_steps("uv", wheel, pins, Path("python"))
    assert "--exclude-newer" in install_step and "--no-build" in hash_step
    for script in (install.INSTALL_SH, install.INSTALL_PS1):
        assert _flags(_line(script, "uv pip install")) == _flags(hash_step)
        tool_line = " ".join(_line(script, "uv tool install"))
        tool_flags = _flags(tool_line.split()) | (_flags(_line(script, "--exclude-newer"))
                                                   if "--exclude-newer" not in tool_line else set())
        assert tool_flags == _flags(install_step)
        assert re.search(r"--exclude-newer ['\"]__EXCLUDE_NEWER__['\"]", script)
