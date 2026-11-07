"""Review-3 fixes: one PC can't end another PC's grant (MF3), /privacy says what the code does (S40,
S41), and build_lambda.sh refuses a release cut from other kw-mcp code (C4). The installer scripts
(S18, S52) are gone: the Claude Desktop extension installs the agent."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_bridge import (  # noqa: F401
    FakeDevice,
    env,
    grants,
    mcp_call,
    oauth,
    refresh,
    register,
    revoke,
)
from test_install import client, key, ship, static  # noqa: F401

from kwcampus import campus, deptboards, discord, politeness, wixfeeds, wwwboards
from kwcampus.bridge import signing, store
from kwcampus.config import DELIVERY_TTL, NOTICE_TTL, WWW_DAILY_CAP

ROOT = Path(__file__).resolve().parents[1]
BUILD_LAMBDA = ROOT / "scripts" / "build_lambda.sh"


# --- MF3: revoke reaches only the caller's own grant ------------------------------------------

def test_a_pc_that_knows_another_pcs_grant_id_cannot_end_it(env):  # noqa: F811
    c, t = env["client"], env["table"]
    alice, bob = register(c, FakeDevice()), register(c, FakeDevice())
    reg_a, tok_a = oauth(c, alice)
    reg_b, tok_b = oauth(c, bob)
    bob_gid = grants(c, bob)[0]["id"]
    assert tok_b["refresh_token"].startswith(bob_gid + ".")  # the id is in plain sight in the token

    assert revoke(c, alice, bob_gid).status_code == 404
    assert t.query(KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": f"GRANT#{bob_gid}"})["Items"]
    again = refresh(c, reg_b, tok_b["refresh_token"])
    assert again.status_code == 200, again.text  # Bob's connector keeps working
    assert mcp_call(c, again.json()["access_token"], "tools/list").status_code == 200
    assert [g["id"] for g in grants(c, bob)] == [bob_gid]

    alice_gid = grants(c, alice)[0]["id"]  # revoking one's own grant still ends it, refresh tokens too
    assert revoke(c, alice, alice_gid).status_code == 200
    assert refresh(c, reg_a, tok_a["refresh_token"]).json()["error"] == "invalid_grant"
    assert not t.query(KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": f"GRANT#{alice_gid}"})["Items"]


def test_revoke_grant_without_the_callers_row_touches_nothing(env):  # noqa: F811
    t = env["table"]
    store.put_grant(t, "u_owner", "g_shared", "c_x", "Claude", "claude.ai")
    store.put_refresh(t, "g_shared", "h1", "u_owner", "c_x", store._now() + 3600)
    assert store.revoke_grant(t, "u_other", "g_shared") is False
    assert store.get_grant(t, "u_owner", "g_shared") and store.get_refresh(t, "g_shared", "h1")
    assert store.revoke_grant(t, "u_owner", "g_shared") is True
    assert store.get_grant(t, "u_owner", "g_shared") is None and store.get_refresh(t, "g_shared", "h1") is None
    assert store.revoke_grant(t, "u_owner", "g_shared") is False  # already gone


# --- S40 / S41: what /privacy says -------------------------------------------------------------------

def test_privacy_keeps_the_snapshot_qualifier(client):  # noqa: F811
    privacy = client.get("/privacy").text
    assert "최대 1분 다시 쓰" in privacy and "다음 요청을 받을 때나 서버 인스턴스가 내려갈 때" in privacy
    assert "최대 1분 동안 다시 쓴 뒤 메모리에서 버립니다" not in privacy


def test_privacy_periods_and_collector_facts_come_from_the_code(client):  # noqa: F811
    page = client.get("/privacy").text
    rows = {re.sub(r"<[^>]+>", "", m[0]): re.sub(r"<[^>]+>", "", m[1])
            for m in re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td></tr>", page)}

    def row(start: str) -> str:
        return next(v for k, v in rows.items() if k.startswith(start))

    days = store.GRANT_TTL // 86400
    assert f"승인한 날부터 {days}일" in row("연결한 앱 목록") and f"승인한 날부터 {days}일" in row("갱신 토큰의 해시")
    assert row("앱(Claude·ChatGPT) 등록 정보") == (f"토큰을 받기 전에는 {store.CLIENT_PENDING_TTL // 86400}일, "
                                                f"받은 뒤에는 마지막으로 토큰을 받은 날부터 {store.CLIENT_TTL // 86400}일")
    assert row("승인 코드의 해시").startswith(f"{store.CODE_TTL // 60}분 / {store.AUTHREQ_TTL // 60}분")
    assert row("1회용 연결 코드의 해시") == f"{store.PAIR_TTL // 60}분"
    assert row("에이전트 접속 정보") == f"{store.CONN_TTL // 3600}시간"
    assert row("이미 쓴 기기 서명의 해시") == f"{signing.MAX_SKEW_S * 2 // 60}분"
    assert f"늦어도 {store.RESULT_TTL // 60}분" in row("암호화된 답")
    assert f"{NOTICE_TTL.days}일" in row("학교 공지:")
    delivered = row("학교 공지 알림:")
    assert (f"올린 날부터 {DELIVERY_TTL.days}일" in delivered
            and f"공지가 바뀌면(메시지를 고치지 않는 변경이어도) 그날부터 다시 {DELIVERY_TTL.days}일" in delivered
            and f"공지 보유 기간보다 {discord.RECORD_GRACE.days}일 더" in delivered)
    # every attribute store.py keeps on a device and a grant row is named (invite_hash, last_seen)
    device = next(k for k in rows if k.startswith("기기 ID"))
    assert "초대 코드의 해시" in device and "마지막 접속 시각" in device
    assert "초대 코드 해시" in next(k for k in rows if k.startswith("연결한 앱 목록"))
    # the collector paragraph: hosts, schedule, caps, region, opt-out
    collector = page.split("<b>수집하는 곳과 방식</b>")[1].split("</p>")[0]
    for needed in ("www.kw.ac.kr", "aicon.kw.ac.kr", "*.kw.ac.kr", *(f.host for f in wixfeeds.FEEDS),
                   "평일 09:00~18:40에 20분마다와 21:00", "서울", f"학교 홈페이지 {WWW_DAILY_CAP}번",
                   f"서버마다 {deptboards.DAILY_CAP}번", f"호스트마다 {wixfeeds.DAILY_CAP}번", "보호책임자 이메일"):
        assert needed in collector, needed
    # one request per server and run, except www's second one (job board or FAQ after the main board,
    # cafeteria then calendar at 07:30), always after the gate's full gap
    gap = int(politeness.MIN_INTERVAL.total_seconds())
    assert "서버마다 한 번만" not in collector
    for needed in ("한 번 돌 때 서버마다 한 번 요청합니다", "학교 홈페이지만 예외로", "취업게시판이나 FAQ를 볼 때",
                   "07:30에 학식과 학사일정을 함께 받을 때", f"{gap}초 넘게 기다렸다가 한 번 더 요청합니다",
                   f"같은 서버에는 {gap}초 이상 간격"):
        assert needed in collector, needed
    assert campus.MAX_REQUESTS == 2 and {b.path for b in wwwboards.BOARDS} == {"/ko/life/job.jsp", "/ko/life/faq.jsp"}
    assert "공지 제목 속 이름" in page and "Discord(미국)" in page.split("<h3>4.")[1].split("<h3>5.")[0]


def test_delivery_record_period_on_the_page_is_what_discord_keeps():
    """/privacy: 올린 날부터 150일, and again 150 days from each change of the notice, whether the
    message is edited or only a keep job moves the record (store.upsert has moved the notice's ttl to
    120 days after the change by then; test_fix3_collectors runs the keep job itself)."""
    posted = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    item = {"ttl": int((posted + NOTICE_TTL).timestamp())}
    assert discord._record_ttl(item, posted) == int((posted + DELIVERY_TTL).timestamp())
    edited = posted + timedelta(days=100)
    item = {"ttl": int((edited + NOTICE_TTL).timestamp())}
    assert discord._record_ttl(item, edited) == int((edited + DELIVERY_TTL).timestamp())
    assert discord.RECORD_GRACE == DELIVERY_TTL - NOTICE_TTL


# --- C4: build_lambda.sh refuses a release cut from other kw-mcp code --------------------------------

def _release_check() -> str:
    return re.search(r"<<'PY'\n(.*?)\nPY\n", BUILD_LAMBDA.read_text(), re.DOTALL).group(1)


def _tree_and_release(tmp_path: Path, version: str = "1.0.0") -> tuple[Path, Path]:
    tree, release = tmp_path / "kw-mcp", tmp_path / "kw-mcp" / "dist" / "release"
    (tree / "src" / "kw_mcp" / "sub").mkdir(parents=True)
    (tree / "pyproject.toml").write_text(f'[project]\nname = "kw-mcp"\nversion = "{version}"\n')
    files = {"kw_mcp/__init__.py": b"", "kw_mcp/service.py": b"def f():\n    return 1\n",
             "kw_mcp/sub/x.py": "# 한글\n".encode()}
    for name, data in files.items():
        (tree / "src" / name).write_bytes(data)
    release.mkdir(parents=True)
    wheel = f"kw_mcp-{version}-py3-none-any.whl"
    with zipfile.ZipFile(release / wheel, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        z.writestr("kw_mcp/py.typed", "")
        z.writestr(f"kw_mcp-{version}.dist-info/METADATA", f"Name: kw-mcp\nVersion: {version}\n")
    (release / "latest.json").write_text(json.dumps({"version": version, "wheel": wheel}))
    return tree, release


def _check(release: Path, tree: Path, data: Path | None = None) -> subprocess.CompletedProcess:
    data = data or tree / "dist" / "data"  # build_lambda passes $DATA_RELEASE whether it exists or not
    return subprocess.run([sys.executable, "-", str(release), str(tree), str(data)], input=_release_check(),
                          capture_output=True, text=True, timeout=60, check=False)


def _data_release(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """A dist/data folder: the files under their package paths and data.json listing them (unsigned; the
    signature is check-data's job, run before this check)."""
    data = tmp_path / "data"
    for name, body in files.items():
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_bytes(body)
    listed = {name: {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)} for name, body in files.items()}
    (data / "data.json").write_text(json.dumps({"files": listed}))
    return data


def test_build_lambda_accepts_a_release_of_exactly_the_tree(tmp_path):
    tree, release = _tree_and_release(tmp_path)
    (tree / "src" / "kw_mcp" / "__pycache__").mkdir()
    (tree / "src" / "kw_mcp" / "__pycache__" / "service.cpython-313.pyc").write_bytes(b"\0")
    done = _check(release, tree)
    assert done.returncode == 0, done.stderr


def test_build_lambda_refuses_another_version_or_other_code(tmp_path):
    tree, release = _tree_and_release(tmp_path)
    (tree / "pyproject.toml").write_text('[project]\nname = "kw-mcp"\nversion = "1.0.1"\n')
    done = _check(release, tree)
    assert done.returncode == 1 and "holds kw-mcp 1.0.0" in done.stderr and "is 1.0.1" in done.stderr

    tree, release = _tree_and_release(tmp_path / "edited")
    (tree / "src" / "kw_mcp" / "service.py").write_bytes(b"def f():\n    return 2\n")  # a fix after the release
    done = _check(release, tree)
    assert done.returncode == 1 and "kw_mcp/service.py" in done.stderr

    tree, release = _tree_and_release(tmp_path / "added")
    (tree / "src" / "kw_mcp" / "new_module.py").write_text("X = 1\n")
    done = _check(release, tree)
    assert done.returncode == 1 and "kw_mcp/new_module.py" in done.stderr


def test_build_lambda_runs_the_tree_check_before_building():
    script = BUILD_LAMBDA.read_text()
    signature = script.index('python -m kw_mcp.release check "$RELEASE"')
    tree_check = script.index('python - "$RELEASE" "$KW_MCP_DIR" "$DATA_RELEASE" <<\'PY\'')
    assert signature < tree_check < script.index('rm -rf "$TMP" && mkdir -p "$TMP/lambda"')


def test_build_lambda_takes_knowledge_and_rules_from_a_data_release(tmp_path):
    """A data-only change: the tree's knowledge/ and rules/ files differ from the wheel but equal the data
    release, so the build goes on; the code must still equal the wheel, and the data must equal the release."""
    tree, release = _tree_and_release(tmp_path)
    (tree / "src" / "kw_mcp" / "knowledge").mkdir()
    (tree / "src" / "kw_mcp" / "knowledge" / "faq.json").write_bytes(b'{"entries": [2]}')
    done = _check(release, tree)
    assert done.returncode == 1 and "kw_mcp/knowledge/faq.json" in done.stderr  # no data release: wheel rules

    data = _data_release(tmp_path, {"knowledge/faq.json": b'{"entries": [2]}'})
    done = _check(release, tree, data)
    assert done.returncode == 0, done.stderr

    stale = _data_release(tmp_path / "stale", {"knowledge/faq.json": b'{"entries": [1]}'})
    done = _check(release, tree, stale)
    assert done.returncode == 1 and "kw_mcp/knowledge/faq.json" in done.stderr

    (tree / "src" / "kw_mcp" / "service.py").write_bytes(b"def f():\n    return 2\n")
    done = _check(release, tree, data)
    assert done.returncode == 1 and "kw_mcp/service.py" in done.stderr  # code still needs a release
