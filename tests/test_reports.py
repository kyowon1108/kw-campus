"""제보: the 제보하기 button and /제보 open a form; a submitted form is posted to #제보-검토 without
pinging anyone, and the reporter gets a private thank-you."""

from kwcampus import interactions as it

GUILD, USER, REPORT = "100000000000000001", "100000000000000002", "100000000000000003"
CFG = {"guild": GUILD, "roles": {}, "report": {"channel": REPORT}}
TOKEN = "MTAwMDAwMDAwMDAwMDAwMDAw.abcdef.ghijklmnopqrstuvwxyz0123456789"  # three dot-separated parts, like a real token


def base(kind: int, data: dict) -> dict:
    return {"type": kind, "guild_id": GUILD, "member": {"user": {"id": USER}, "roles": []}, "data": data}


def submit(what="졸업 학점이 바뀌었어요", source="https://www.kw.ac.kr/x", where="") -> dict:
    fields = [("what", what), ("source", source), ("where", where)]
    return base(it.MODAL_SUBMIT, {"custom_id": it.REPORT_SUBMIT, "components": [
        {"type": 1, "components": [{"type": 4, "custom_id": cid, "value": value}]} for cid, value in fields]})


def test_the_button_and_the_command_open_the_same_form():
    for payload in (base(it.MESSAGE_COMPONENT, {"custom_id": it.REPORT_OPEN, "component_type": 2}),
                    base(it.APPLICATION_COMMAND, {"name": it.REPORT_COMMAND, "type": 1})):
        response, outcome = it.handle(payload, CFG, TOKEN)
        assert outcome == "report_form" and response["type"] == it.MODAL
        ids = [row["components"][0]["custom_id"] for row in response["data"]["components"]]
        assert response["data"]["custom_id"] == it.REPORT_SUBMIT and ids == ["what", "source", "where"]
        assert len(response["data"]["title"]) <= 45  # Discord's limit


def test_a_report_is_posted_quietly_and_the_reporter_is_thanked():
    sent = []
    response, outcome = it.handle(submit(), CFG, TOKEN, send=lambda *a, **k: sent.append(a))
    assert outcome == "reported" and response["data"]["flags"] == it.EPHEMERAL and "고마워요" in response["data"]["content"]
    [(method, path, _token, body)] = sent
    assert (method, path) == ("POST", f"/channels/{REPORT}/messages")
    assert body["allowed_mentions"] == {"parse": []}  # shows who reported, pings nobody
    assert f"<@{USER}>" in body["content"] and "> 졸업 학점이 바뀌었어요" in body["content"]
    assert "**어디서**" not in body["content"]  # empty optional fields are left out


def test_reporter_markup_cannot_break_out_of_the_quote():
    sent = []
    it.handle(submit(what="```\n@everyone 지금 확인\n# 큰 제목"), CFG, TOKEN, send=lambda *a, **k: sent.append(a))
    content = sent[0][3]["content"]
    assert "```" not in content and "> # 큰 제목" in content and "> @everyone 지금 확인" in content


def test_without_a_report_channel_nothing_is_sent():
    sent = []
    response, outcome = it.handle(submit(), {**CFG, "report": {}}, TOKEN, send=lambda *a, **k: sent.append(a))
    assert outcome == "report_not_ready" and sent == [] and "준비" in response["data"]["content"]


def test_a_failed_post_says_so():
    def boom(*a, **k):
        raise RuntimeError("Discord 403")
    response, outcome = it.handle(submit(), CFG, TOKEN, send=boom)
    assert outcome == "report_failed" and "못 보냈어요" in response["data"]["content"]


def test_reports_only_come_from_the_notice_server():
    payload = submit()
    payload["guild_id"] = "999999999999999999"
    _, outcome = it.handle(payload, CFG, TOKEN, send=lambda *a, **k: None)
    assert outcome == "wrong_guild"


def test_other_commands_and_forms_are_refused():
    assert it.handle(base(it.APPLICATION_COMMAND, {"name": "다른명령"}), CFG, TOKEN)[1] == "unsupported"
    assert it.handle(base(it.MODAL_SUBMIT, {"custom_id": "other"}), CFG, TOKEN)[1] == "unsupported"
