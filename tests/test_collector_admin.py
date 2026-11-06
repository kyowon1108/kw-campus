"""scripts/collector_admin.py: re-enabling, takedown, and the redaction report. Dry runs change nothing."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kwcampus import deptboards, politeness, public_data, store
from kwcampus.discordapi import NotFound
from kwcampus.fetch import FetchResult

_spec = importlib.util.spec_from_file_location("collector_admin",
                                               Path(__file__).parents[1] / "scripts" / "collector_admin.py")
admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin)

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
EE_PAGE = (Path(__file__).parent / "fixtures" / "dept_ee_list.html").read_text()
EE = next(b for b in deptboards.BOARDS if b.key == "ee")


def test_reenable_is_a_dry_run_unless_applied(aws):
    table, origin = aws["public"], deptboards.SERVER_E1
    for n in range(3):
        politeness.record_block(table, origin, NOW + timedelta(hours=n), "forbidden")
    assert admin.disabled(table) == [f"ORIGIN#{origin}: last forbidden"]
    assert "would reset" in admin.reenable(table, origin, None, apply=False)
    assert politeness.state(table, origin)["disabled"] is True
    assert "-> reset" in admin.reenable(table, origin, None, apply=True)
    assert politeness.acquire(table, origin, NOW + timedelta(hours=4), 32).granted and admin.disabled(table) == []
    table.put_item(Item={"PK": "SRC#kw-job", "SK": "STATE", "disabled": True, "hard_blocks": 3, "fails": 3})
    admin.reenable(table, None, "kw-job", apply=True)
    assert not politeness.source_waiting(table.get_item(Key={"PK": "SRC#kw-job", "SK": "STATE"})["Item"], NOW)


def test_takedown_deletes_the_messages_records_and_keeps_a_tombstone(aws, monkeypatch):
    public, delivery = aws["public"], aws["delivery"]
    monkeypatch.setattr(deptboards, "BOARDS", (EE,))
    deptboards.collect(NOW, public, fetch=lambda url, **kw: FetchResult(EE_PAGE, None, 200))
    dkey = {"PK": "SRC#dept-ee|N#646", "SK": "CH#department"}
    delivery.put_item(Item={**dkey, "channel_id": "300", "message_id": "400", "reply_message_id": "401"})
    sent = []

    def send(method, path, token, body=None):
        sent.append((method, path))
        if path.endswith("/401"):
            raise NotFound("gone already")
        return {}

    dry = admin.takedown(public, delivery, "dept-ee-646", apply=False, send=send, token="t")
    assert sent == [] and dry[-1].startswith("dry run") and len(dry) == 4
    assert "Item" in delivery.get_item(Key=dkey)

    admin.takedown(public, delivery, "dept-ee-646", apply=True, send=send, token="t", now=NOW)
    assert sent == [("DELETE", "/channels/300/messages/400"), ("DELETE", "/channels/300/messages/401")]
    assert "Item" not in delivery.get_item(Key=dkey)
    item = public.get_item(Key={"PK": "SRC#dept-ee", "SK": "N#646"})["Item"]
    assert item["removed"] is True and item["title"] == ""
    public_data.clear_cache()
    assert public_data.get(public, "dept-ee-646") is None

    # the board still shows the post: the next run must not bring it back
    later = NOW + timedelta(hours=3)
    changed = EE_PAGE.replace("2026 광운대학교 취업박람회", "2026 광운대학교 취업박람회(수정)")
    deptboards.collect(later, public, fetch=lambda url, **kw: FetchResult(changed, None, 200))
    assert public.get_item(Key={"PK": "SRC#dept-ee", "SK": "N#646"})["Item"]["title"] == ""


def test_post_ids_map_to_their_source():
    assert admin.notice_key("55001") == {"PK": "SRC#kw-notice", "SK": "N#55001"}
    assert admin.notice_key("dept-ee-646") == {"PK": "SRC#dept-ee", "SK": "N#646"}
    assert admin.notice_key("kw-job-53606") == {"PK": "SRC#kw-job", "SK": "N#53606"}
    assert admin.notice_key("aicon-3516") == {"PK": "SRC#aicon", "SK": "N#3516"}


def test_redaction_report_counts_without_writing(aws):
    table = aws["public"]
    for n, title in enumerate(["[부고] 전자공학과 김철수 교수 부친상", "도서관 휴관 안내", "문의 010.1234.5678"]):
        table.put_item(Item={"PK": "SRC#kw-notice", "SK": f"N#{n}", "title": title, "department": "총무팀"})
    table.put_item(Item={"PK": "SRC#kw-notice", "SK": "N#9", "title": "새 공지", "department": "총무팀",
                         "norm_v": store.REDACTION_LEVEL})
    assert admin.redaction_report(table) == {"notices": 4, "would_change": 2, "names": 1, "already_v2": 1}
    assert table.get_item(Key={"PK": "SRC#kw-notice", "SK": "N#0"})["Item"]["title"].startswith("[부고] 전자공학과 김철수")
