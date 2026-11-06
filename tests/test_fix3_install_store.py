"""Review-3 fixes: one PC can't end another PC's grant (MF3), the installers enforce the pinned hashes
and stop a running Windows agent first (S18, S52), /setup and /privacy say what the code does (S40,
S41), and build_lambda.sh refuses a release cut from other kw-mcp code (C4)."""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kw_mcp import agent
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


# --- S18 / S52: the installer scripts ------------------------------------------------------------

def _stub(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _run_install_sh(tmp_path: Path, script: str, static_dir: Path, fail_hash: bool) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the served install.sh with uv, curl, kw-mcp and launchctl replaced by stubs that log."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    log = tmp_path / "calls.log"
    _stub(bin_dir, "uv", 'echo "uv $*" >> "$LOG"\n'
                         'if [ "$1 $2" = "tool dir" ]; then echo "$HOME/tools"; fi\n'
                         'if [ "$1 $2" = "pip install" ] && [ -n "${FAIL_HASH:-}" ]; then exit 1; fi\n')
    _stub(bin_dir, "curl", 'echo "curl $*" >> "$LOG"\n'
                           'while [ $# -gt 1 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
                           'cp "$STATIC/$(basename "$1")" "$out"\n')
    _stub(bin_dir, "kw-mcp", 'echo "kw-mcp $*" >> "$LOG"\n')
    _stub(bin_dir, "launchctl", 'echo "launchctl $*" >> "$LOG"\n')
    path = tmp_path / "install.sh"
    path.write_text(script.replace(" < /dev/tty", ""))  # no terminal under pytest; nothing else changes
    run_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "LOG": str(log), "STATIC": str(static_dir),
               "TMPDIR": str(tmp_path)}
    if fail_hash:
        run_env["FAIL_HASH"] = "1"
    done = subprocess.run(["sh", str(path), "kwc_test-invite"], env=run_env, capture_output=True, text=True,
                          timeout=60, check=False)
    return done, log.read_text().splitlines() if log.exists() else []


def test_install_sh_reinstalls_every_dependency_by_its_pinned_hash(client, static, key, tmp_path):  # noqa: F811
    m = ship(static, key)
    pins = m["constraints"]["file"]
    script = client.get("/install.sh").text
    done, calls = _run_install_sh(tmp_path, script, static, fail_hash=False)
    assert done.returncode == 0, done.stderr
    tool = next(i for i, line in enumerate(calls) if line.startswith("uv tool install "))
    pip = next(i for i, line in enumerate(calls) if line.startswith("uv pip install "))
    link = next(i for i, line in enumerate(calls) if line.startswith("kw-mcp link "))
    assert tool < pip < link
    home = tmp_path / "home"
    assert re.fullmatch(rf"uv pip install --quiet --python {re.escape(str(home))}/tools/kw-mcp/bin/python "
                        rf"--require-hashes --no-deps --reinstall --no-build -r \S+/{re.escape(pins)}", calls[pip])
    assert calls[link] == f"kw-mcp link kwc_test-invite --server {install_base(script)}"
    assert not [line for line in calls if "uninstall" in line or line.startswith("launchctl")]
    assert (home / ".config" / "kw-campus" / "releases" / pins).is_file()


def test_install_sh_removes_the_agent_when_a_hash_does_not_match(client, static, key, tmp_path):  # noqa: F811
    ship(static, key)
    done, calls = _run_install_sh(tmp_path, client.get("/install.sh").text, static, fail_hash=True)
    assert done.returncode == 1 and "did not match its pinned hash" in done.stderr
    assert f"launchctl bootout gui/{os.getuid()}/{agent.LAUNCHD_LABEL}" in calls
    assert "uv tool uninstall kw-mcp" in calls
    assert not [line for line in calls if line.startswith("kw-mcp ")]  # nothing unchecked gets started
    assert not (tmp_path / "home" / ".config" / "kw-campus" / "releases").exists()


def install_base(script: str) -> str:
    return re.search(r"--server \"([^\"]+)\"", script).group(1)


def test_install_ps1_stops_the_running_agent_then_checks_hashes(client, static, key):  # noqa: F811
    m = ship(static, key)
    ps1 = client.get("/install.ps1").text
    stop = ps1.index("if (Test-AgentLock)")
    tool = ps1.index("uv tool install --force")
    python_missing = ps1.index("if (-not (Test-Path -LiteralPath $toolPy))")
    hashes = ps1.index("uv pip install --quiet --python $toolPy --require-hashes --no-deps --reinstall --no-build -r $pins")
    link = ps1.index("kw-mcp link $invite")
    assert (ps1.index("Get-FileHash") < stop < ps1.index("throw 'The running KW Campus agent did not stop.") < tool
            < python_missing < hashes < link)
    # uv's tool folder is the one native output the script reads, and it is read as UTF-8 (not the
    # console's code page 949), so a Korean profile folder name survives; the encoding is put back.
    assert ps1.count("uv tool dir") == 1 and ps1.count("$(uv ") == 1
    saved = ps1.index("$consoleEncoding = [Console]::OutputEncoding")
    utf8 = ps1.index("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8")
    read = ps1.index('try { $toolDir = "$(uv tool dir)".Trim() } finally { [Console]::OutputEncoding = $consoleEncoding }')
    no_dir = ps1.index("if ($LASTEXITCODE -ne 0 -or -not $toolDir) { throw")
    assert saved < utf8 < read < no_dir < ps1.index("$toolEnv = Join-Path $toolDir 'kw-mcp'") < stop
    assert ps1.index("$toolPy = Join-Path $toolEnv 'Scripts\\python.exe'") < python_missing
    # a missing Python has its own message; both failures remove the login entry and the tool
    missing_branch, hash_branch = ps1[python_missing:hashes], ps1[hashes:link]
    assert "Remove-Agent" in missing_branch and "dependencies could not be checked" in missing_branch
    assert "pinned hash" not in missing_branch
    assert "Remove-Agent" in hash_branch and "did not match its pinned hash" in hash_branch
    remove = ps1[ps1.index("function Remove-Agent"):stop]
    assert f"-Name '{agent.WINDOWS_RUN_VALUE}'" in remove and "uv tool uninstall kw-mcp" in remove
    # PowerShell 7 must not throw on uv's exit code before the script's own check runs the cleanup
    assert ps1.index("$PSNativeCommandUseErrorActionPreference = $false") < ps1.index("Get-Command uv")
    assert f"-ne '{m['sha256']}'" in ps1  # the download is still checked before anything is stopped
    # The names the agent itself uses (kw_mcp.agent, read-only here).
    assert agent.CONFIG_DIR == Path.home() / ".config" / "kw-campus"
    assert "Join-Path $env:USERPROFILE '.config\\kw-campus'" in ps1
    assert "Join-Path $cfg 'agent.lock'" in ps1 and "Join-Path $cfg 'agent.pid'" in ps1
    lock_source = inspect.getsource(agent._try_lock)
    assert "handle.seek(0)" in lock_source and "msvcrt.LK_NBLCK, 1)" in lock_source  # byte 0, length 1
    assert "$fs.Lock(0, 1)" in ps1
    assert ps1.count(str(agent.LEGACY_LOCK_PORT)) == 2 and 'GetBytes("exit`n")' in ps1
    assert f"-Name '{agent.WINDOWS_RUN_VALUE}'" in ps1 and "uv tool uninstall kw-mcp" in ps1
    assert not re.search(r"\$pid\b", ps1, re.IGNORECASE)  # $PID is PowerShell's own process id
    assert "__" not in ps1


# --- S40 / S41: what /setup and /privacy say -------------------------------------------------------

def test_setup_and_privacy_keep_the_snapshot_qualifier(client):  # noqa: F811
    setup, privacy = client.get("/setup").text, client.get("/privacy").text
    for page in (setup, privacy):
        assert "최대 1분 다시 쓰" in page and "다음 요청을 받을 때나 서버 인스턴스가 내려갈 때" in page
    assert "최대 1분 동안 다시 쓴 뒤 메모리에서 버립니다" not in setup


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


def _check(release: Path, tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-", str(release), str(tree)], input=_release_check(), capture_output=True,
                          text=True, timeout=60, check=False)


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
    tree_check = script.index('python - "$RELEASE" "$KW_MCP_DIR" <<\'PY\'')
    assert signature < tree_check < script.index('rm -rf "$TMP" && mkdir -p "$TMP/lambda"')
