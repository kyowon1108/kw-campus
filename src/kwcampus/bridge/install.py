"""The bridge's own pages and downloads (no GitHub or PyPI needed for kw-mcp).

  <base>/start                 what KW CAMPUS does and how to install the Claude Desktop extension;
                               an invite in the fragment (/start#kwc_...) is shown by the page's script
                               alone (a URL fragment never reaches the server). /setup sends here.
  <base>/privacy               the 처리방침
  <base>/dist/<name>           the extension (.mcpb, and the same bytes as .zip), and the signed kw-mcp
                               release: wheel, dependency pins and latest.json
  <base>/dist/data/<path>      a kw-mcp data release (knowledge base and rules, kw_mcp.datasync): data.json,
                               signed with the release key, and only the files it lists

There is no terminal installer any more: the extension installs and links the PC agent itself.
Agents already installed keep updating from /dist: they verify latest.json against the release key
they carry, then fetch the wheel and pins it names.
"""

from __future__ import annotations

import json
import re
import secrets
from functools import cache
from pathlib import Path
from typing import Any

from kw_mcp import release
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from kwcampus import deptboards, invites, politeness, smallboards, wixfeeds
from kwcampus.bridge import signing, store
from kwcampus.config import DELIVERY_TTL, NOTICE_TTL, WWW_DAILY_CAP

STATIC_DIR = Path(__file__).parent / "static"
MCPB_NAME = release.MCPB_NAME  # Claude Desktop extension, packed and signed by kw-mcp's scripts/release.sh
# The same bytes as a .zip, for Claude Desktop builds whose installer closes on .mcpb files
# (Windows Store build, mcpb issue #281): unzip, then "Install Unpacked Extension".
MCPB_ZIP_NAME = "kw-campus.zip"
# Links handed out before 1.0.0 (the extension was "kw-klas" then) still get the current file.
OLD_MCPB_NAMES = {"kw-klas.mcpb": MCPB_NAME, "kw-klas.zip": MCPB_ZIP_NAME}
# Signed release manifest (kw_mcp.release). The signatures are made on the release machine; this
# server only passes them along and cannot forge one.
MANIFEST_NAME = release.MANIFEST_NAME
# A data release (kw-mcp `scripts/release.sh data`), copied in by build_lambda.sh after checking it. The
# bridge's own school_guide and rules read it too (run.sh points KW_DATA_DIR here).
DATA_DIR = STATIC_DIR / "data"
DATA_CACHE = "public, max-age=31536000, immutable"  # asked for by its signed hash (?sha256=), so never stale

# Shared by /start, /connect and /oauth/authorize (docs/copy-style.md: KW red for buttons and accents,
# white and light grey backgrounds, system fonts, 48px+ buttons, laid out for a 360px phone first).
PAGE_STYLE = """:root{--kw:#8B0E0A;--soft:#FBEFEE;--bg:#F7F8FA;--text:#191F28;--sub:#4E5968;--line:#E5E8EB}
*{box-sizing:border-box} [hidden]{display:none!important}
body{margin:0;background:#fff;color:var(--text);word-break:keep-all;overflow-wrap:anywhere;
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Pretendard","Malgun Gothic",sans-serif}
main{max-width:560px;margin:0 auto;padding:32px 20px 48px}
h1{font-size:26px;line-height:1.3;margin:0 0 8px} h2{font-size:19px;line-height:1.4;margin:36px 0 12px}
p{margin:0 0 12px} a{color:var(--kw)} b{font-weight:700}
.lead{font-size:17px;color:var(--sub)}
.muted{color:var(--sub);font-size:14px}
.card{background:var(--bg);border-radius:16px;padding:16px 18px;margin:16px 0} .card.accent{background:var(--soft)}
.card>:last-child,ol.steps>li>:last-child{margin-bottom:0}
.btn{display:flex;align-items:center;justify-content:center;width:100%;min-height:52px;margin:0 0 12px;padding:12px 16px;
border:0;border-radius:14px;background:var(--kw);color:#fff;font:inherit;font-weight:700;text-decoration:none;cursor:pointer}
.btn.sub{background:#fff;color:var(--kw);border:1px solid var(--line)} .btn[disabled]{opacity:.5}
ul.ask{list-style:none;padding:0;margin:16px 0} ul.ask li{background:var(--bg);border-radius:12px;padding:10px 14px;margin:0 0 8px}
ol.steps{list-style:none;padding:0;margin:0;counter-reset:step}
ol.steps>li{counter-increment:step;position:relative;padding:0 0 24px 44px}
ol.steps>li::before{content:counter(step);position:absolute;left:0;top:0;width:30px;height:30px;border-radius:50%;
background:var(--kw);color:#fff;font-weight:700;font-size:15px;display:flex;align-items:center;justify-content:center}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.copy{display:flex;gap:8px;margin:0 0 12px} .copy code{flex:1;min-width:0;background:#fff;border:1px solid var(--line);
border-radius:12px;padding:12px;font-size:14px;word-break:break-all}
.copy button{flex:none;min-width:72px;min-height:48px;border:1px solid var(--line);border-radius:12px;background:#fff;
color:var(--text);font:inherit;cursor:pointer}
label{display:block;font-weight:700;margin:0 0 6px}
input{width:100%;min-height:52px;margin:0 0 8px;padding:12px 14px;border:1px solid var(--line);border-radius:14px;
font:inherit;font-size:18px;letter-spacing:.08em}
details{margin:8px 0 12px} summary{padding:12px 0;color:var(--sub);font-weight:700;cursor:pointer}
.status{font-weight:700} .status:empty{margin:0}
.big{font:700 34px/1.3 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.12em;text-align:center;margin:8px 0 0}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);color:var(--sub);font-size:14px}"""

# /start: what to ask and how to install, one line each (docs/copy-style.md). An invite in the fragment
# (/start#kwc_...) is shown by the script alone: a fragment never reaches a server.
START_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>KW CAMPUS (광운 캠퍼스)</title>
<style>__STYLE__</style></head><body><main>
<h1>KW CAMPUS</h1>
<p class="lead">Claude가 내 KLAS 과제·성적·출석과 학교 공지·학식·학사일정을 보고 답해요.</p>
<ul class="ask"><li>“이번 주 과제 뭐 남았어?”</li><li>“복전하려면 몇 학점 들어야 돼?”</li><li>“내일 학식 뭐야?”</li></ul>

<div id="invite-box" class="card accent" hidden><p><b>내 초대 코드</b></p>
<div class="copy"><code id="invite"></code><button type="button" data-copy="invite">복사</button></div></div>

<h2>설치하기</h2>
<ol class="steps">
<li><p>디스코드 #설치-안내에서 ‘설치하기’를 눌러 초대 코드를 받아요.</p></li>
<li><p><a href="https://claude.ai/download" target="_blank" rel="noopener">Claude 데스크톱</a>을 설치하고 로그인해요.</p></li>
<li><p>kw-campus.mcpb를 더블클릭하고 학번·비밀번호·초대 코드를 넣어요.</p>
<a class="btn" href="/dist/kw-campus.mcpb__V__">kw-campus.mcpb 받기</a>
<p class="muted">설치 창이 바로 닫히면 <a href="/dist/kw-campus.zip__V__">kw-campus.zip</a>을 풀어 Claude 설정 → 확장 프로그램 → 고급 설정 → ‘압축 해제된 확장 설치’로 넣어요.</p></li>
<li><p>웹·폰에서도 쓰려면 Claude 데스크톱에 “웹이랑 폰에서도 쓰게 연결해 줘”라고 말해요.</p></li>
</ol>

<div class="card"><p class="muted">비밀번호는 내 PC에만 저장돼요.</p>
<p class="muted">PC가 꺼져 있으면 웹·폰에서 내 정보는 안 보여요.</p></div>
<footer>학교 공식 서비스 아님 · <a href="/privacy">개인정보 처리방침</a> · <a href="mailto:kyowon1108@gmail.com">문의</a></footer>
</main>
<script nonce="__NONCE__">
document.querySelectorAll('button[data-copy]').forEach((b) => b.onclick = async () => {
  try { await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent); b.textContent = '복사됨'; }
  catch (e) { b.textContent = '길게 눌러 복사'; }
});
let raw = location.hash.slice(1);
try { raw = decodeURIComponent(raw); } catch (e) { raw = ''; }  // a truncated %-escape: no invite
const invite = raw.trim();
// Only an invite that points back at this server (a doctored link could aim somewhere else).
let ok = invite.length <= 400 && /^kwc_[A-Za-z0-9_-]+$/.test(invite);
if (ok) {
  try {
    const b64 = invite.slice(4).replace(/-/g, '+').replace(/_/g, '/');
    const data = JSON.parse(atob(b64 + '='.repeat((4 - b64.length % 4) % 4)));
    ok = data.s === location.origin && typeof data.t === 'string';
  } catch (e) { ok = false; }
}
if (ok) {
  document.getElementById('invite').textContent = invite;
  document.getElementById('invite-box').hidden = false;
}
</script></body></html>"""


def start_page(script_nonce: str) -> str:
    rel = current_release()
    # ?v=<version> gives each release its own cache key, so a browser never reuses an older download
    version = f"?v={rel['version']}" if rel and re.fullmatch(r"[0-9][0-9A-Za-z.]*", str(rel["version"])) else ""
    return START_PAGE.replace("__STYLE__", PAGE_STYLE).replace("__NONCE__", script_nonce).replace("__V__", version)


# Rendered by privacy_page(): each __X__ value comes from the constant that enforces it.
PRIVACY_TEMPLATE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>KW Campus 개인정보 처리방침</title>
<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#222;line-height:1.7}
h3{margin-top:28px} table{border-collapse:collapse;width:100%;font-size:14px} th,td{border:1px solid #ccc;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#f4f4f5} .muted{color:#666;font-size:14px}</style></head><body>
<h2>KW Campus 개인정보 처리방침</h2>
<p>KW Campus는 광운대학교 학생 개인(운영자 kyowon1108)이 친구들과 쓰려고 만든 비공식 도구입니다. 광운대학교가 만들거나 승인하지 않았습니다.
KLAS는 사용자 본인의 PC에서 본인 계정으로 읽기만 하며, 제출·수정은 하지 않습니다. 이 방침은 「개인정보 보호법」 제30조에 따라 무엇을 왜 얼마나 처리하는지 알립니다.</p>
<p class="muted">시행일: 2026년 11월 6일</p>

<h3>1. 처리 목적</h3>
<ul>
<li><b>Claude 데스크톱 확장</b>: 학교 공지·학식·학사일정 같은 공개 정보와 새 버전 정보를 제공합니다. 개인정보는 처리하지 않습니다.
학번·비밀번호와 KLAS에서 읽은 내용은 사용자 PC 안에서만 쓰이고 이 서버로 오지 않습니다. 요청에는 초대 토큰과 검색 조건(분류·날짜·검색어)만 담깁니다.</li>
<li><b>웹·폰 연결(PC 에이전트)</b>: 사용자가 연결한 Claude·ChatGPT의 질문을 사용자 PC로 전달하고, PC가 암호화해 보낸 답을 돌려주기 위해 기기와 연결한 앱을 확인·관리하고 부정 사용을 막습니다.</li>
<li><b>초대 코드 발급</b>: Discord에서 초대 코드를 나눠 주고, 한 사람이 받는 수를 제한하고, 회수한 초대로 등록한 PC를 막습니다.</li>
<li><b>학교 공지 수집과 Discord 알림 채널</b>: 학교·학과 사이트의 공개 공지를 모아 Discord 채널과 검색 도구로 알리고, 알림 받을 분류를 고르게 합니다.</li>
<li><b>공통</b>: 서비스 운영, 장애 대응, 보안 사고 조사.</li>
</ul>

<h3>2. 처리 항목과 보유 기간</h3>
<p>학번·비밀번호는 받지도 저장하지도 않습니다. 아래는 서버(AWS)에 생기는 기록입니다. 학교 공지 두 줄, Discord 초대 기록과 서버 로그를 빼면 웹·폰 연결을 쓸 때만 생깁니다.</p>
<table>
<tr><th>항목</th><th>보유 기간</th></tr>
<tr><td>기기 ID(무작위 값), 기기 공개키, 등록 시각, 등록에 쓴 초대 코드의 해시(초대를 회수하면 그 PC도 끊으려고), 마지막 접속 시각</td><td>PC 연결을 해제할 때까지. 해제하면 바로 삭제</td></tr>
<tr><td>연결한 앱 목록(앱 이름, 돌아갈 주소의 호스트, 승인 때의 초대 코드 해시, 연결·마지막 사용·만료 시각)</td><td>그 연결을 끊거나 PC 연결을 해제할 때까지, 길어도 승인한 날부터 __GRANT_DAYS__일</td></tr>
<tr><td>앱(Claude·ChatGPT) 등록 정보(OAuth 클라이언트 이름, 돌아갈 주소)</td><td>토큰을 받기 전에는 __CLIENT_PENDING_DAYS__일, 받은 뒤에는 마지막으로 토큰을 받은 날부터 __CLIENT_DAYS__일</td></tr>
<tr><td>갱신 토큰의 해시(쓸 때마다 새로 발급. 쓴 토큰은 도용을 알아채려고 쓴 시각과 함께 남김)</td><td>그 연결이 끝날 때까지, 길어도 승인한 날부터 __GRANT_DAYS__일</td></tr>
<tr><td>승인 코드의 해시 / 승인 대기 요청(state, 돌아갈 주소, PKCE 값)</td><td>__CODE_MIN__분 / __AUTHREQ_MIN__분, 한 번 쓰면 삭제</td></tr>
<tr><td>1회용 연결 코드의 해시(Claude 데스크톱에서 웹·폰 연결을 요청하면 생김)와, 브라우저가 그 PC와 같은 네트워크에 있는지 확인하려고 함께 두는 PC 네트워크 주소의 키 해시(주소 자체는 저장하지 않음)</td><td>__PAIR_MIN__분</td></tr>
<tr><td>Discord에서 받은 초대 코드: 받은 사람의 Discord 사용자 ID, 코드의 해시, 받은 시각, 회수 여부, 그 코드로 등록한 PC 수. 사람마다 받은 코드의 해시 목록과 마지막으로 받은 시각(한 사람당 __INVITE_CAP__개까지)</td><td>보호책임자에게 삭제를 요청할 때까지. 회수한 초대는 그 초대로 등록한 PC를 계속 막으려고 회수 표시와 함께 남김</td></tr>
<tr><td>에이전트 접속 정보(WebSocket 연결 ID, 에이전트 버전, 접속 시각)</td><td>__CONN_HOURS__시간</td></tr>
<tr><td>이미 쓴 기기 서명의 해시(재사용 방지)</td><td>__SIG_MIN__분</td></tr>
<tr><td>기기별 요청 수(동시 요청 수와 분당 요청 수, 남용 방지)</td><td>2분</td></tr>
<tr><td>기기별 연결 코드 발급 수(한 시간 단위, 남용 방지)</td><td>2시간</td></tr>
<tr><td>하루 초대 코드 발급 수(모든 사람 합계, 누가 받았는지는 담지 않음)</td><td>2일</td></tr>
<tr><td>암호화된 답(요청마다 다른 키로 PC가 암호화한 KLAS 조회 결과)</td><td>전달되면 바로, 늦어도 __RESULT_MIN__분 안에 삭제</td></tr>
<tr><td>풀어낸 답(서버 메모리에만, DB·디스크에 쓰지 않음)</td><td>이어지는 질문에 최대 1분 다시 쓰고, 그 뒤 그 서버가 다음 요청을 받을 때나 서버 인스턴스가 내려갈 때 삭제</td></tr>
<tr><td>학교 공지: 공개 게시판 글의 제목·분류·부서·게시일·링크와 본문에서 뽑은 마감·대상·신청 방법(본문 자체는 저장하지 않고, 제목과 뽑은 내용의 연락처·학번·사람 이름은 저장 전에 가림)</td><td>마지막으로 바뀐 날부터 __NOTICE_DAYS__일</td></tr>
<tr><td>학교 공지 알림: 위 제목과 링크를 올린 Discord 메시지, 전달 기록(Discord 메시지·채널 ID, 올린 시각)</td><td>메시지는 Discord 채널에 남고, 삭제 요청을 받으면 지움. 전달 기록은 올린 날부터 __DELIVERY_DAYS__일이고, 공지가 바뀌면(메시지를 고치지 않는 변경이어도) 그날부터 다시 __DELIVERY_DAYS__일(공지 보유 기간보다 __RECORD_GRACE_DAYS__일 더)</td></tr>
<tr><td>서버 로그: 사건 종류(연결, 승인, 거부, 철회 같은 것)와 오류 코드, 시각</td><td>2주</td></tr>
</table>
<p>접근 토큰은 1시간짜리 서명 토큰이라 서버에 저장하지 않습니다. 서버 로그에는 IP 주소, 요청 주소(초대 토큰), 학번·이름·성적 같은 내용을 남기지 않도록 만들었습니다
(요청을 전달하는 동안 AWS가 IP 주소를 다루기는 합니다). 로그에 남기는 항목을 늘리게 되면 이 표에 먼저 적습니다.
초대 코드 자체는 받은 사람에게 한 번만 보여 주고 저장하지 않습니다.
보유 기간이 지난 기록은 DB의 만료 기능과 로그 보관 설정으로 자동 삭제합니다(DB 만료 삭제는 며칠 늦을 수 있지만, 만료된 기록은 쓰지 않습니다). 별도 백업은 두지 않으며, 백업을 켜게 되면 이 방침에 먼저 적습니다.</p>

<p><b>브라우저 쿠키</b>: 웹·폰 연결 페이지(<code>/connect</code>)를 열면 그 브라우저에 1회용 연결 코드를 담은 쿠키 <code>kw_pair</code>를 둡니다.
커넥터를 연결하는 화면에서 코드를 다시 넣지 않고 "이 PC와 연결하기"를 누를 수 있게 하려는 것입니다. 이 서버의 <code>/oauth</code> 주소에만 보내지고 페이지 스크립트는 읽을 수 없으며,
길어도 __PAIR_MIN__분 뒤(코드가 먼저 만료되면 그때) 사라지고, 연결을 시도하면 바로 지웁니다. 커넥터 연결 화면(승인 대기 요청)은 __AUTHREQ_MIN__분 동안 열어 둘 수 있습니다.</p>

<h3>3. 학교 공지 수집과 Discord 알림 채널</h3>
<p><b>수집하는 곳과 방식</b>: 학교 홈페이지(www.kw.ac.kr)의 공지·취업게시판·FAQ·학식·학사일정, 인공지능융합대학(aicon.kw.ac.kr)과 학과·단과대·학교 부서·사업단 사이트(*.kw.ac.kr)의 공지 게시판 첫 페이지,
학과 블로그 피드(__FEED_HOSTS__), __SMALL_SITES__의 공지 목록을 읽습니다. 현장실습 사이트는 robots.txt가 허용하는 첫 화면(/index.do)만 읽고, 공지 글과 첨부는 열지 않습니다. 로그인이 필요한 페이지는 읽지 않습니다. 새 공지가 올라오면 그 글의 본문·첨부·포스터 이미지를 한 번 더 읽어 마감·대상·신청 방법을 뽑고, 뽑은 내용과 본문의 해시, 무엇을 읽었는지(이미지·첨부의 개수, 못 읽은 이유, 주소의 해시)만 저장합니다(본문은 저장하지 않음). 글을 열 수 없는 곳(현장실습, 학과 블로그)은 제목만 남깁니다. 총학생회 글은 목록에 본문이 함께 와서, 연락처·이름을 가린 본문을 읽을 때까지만(길어야 __QUEUE_DAYS__일) 임시로 두고 읽으면 지우며, 포스터는 총학생회 사이트의 저장소(__THUMB_HOST__)에서 하루 __THUMB_CAP__번까지 받습니다. 이 요약은 OpenAI의 API로 만들고 일부는 Anthropic의 Claude API로 만들며, 보내기 전에 본문과 첨부 글의 연락처·학번·사람 이름을 가립니다(이미지 속 글자는 가릴 수 없음). 명단·합격·수상처럼 사람 이름이 들어갈 수 있는 공지는 본문만 보내고 이미지와 첨부는 보내지 않습니다. 그 글의 이미지와 첨부 파일은 브라우저가 글을 열 때처럼 본문 바로 뒤에 __ASSET_GAP_S__초 간격으로 받으며, 이 요청도 하루 상한에 들어갑니다. 커넥터에서 학생이 공지 하나를 직접 읽어 달라고 하면 그때 그 글을 읽어 답하고 저장하지 않습니다. 커넥터의 요청은 수집기의 요청과 따로 셉니다: 사이트마다 하루 __BRIDGE_CAP__번까지, __SMALL_CAPS__까지이고(수집기도 같은 수를 따로 씁니다. 예를 들어 총동문회 사이트에는 수집기 하루 __ALUMNI_CAP__번, 커넥터 하루 __ALUMNI_CAP__번), 수집이 쉬고 있는 서버에는 보내지 않습니다.
요청은 AWS 서울 리전에서 평일 09:00~18:40에 20분마다와 21:00에 보냅니다(학식·학사일정은 평일 07:30). 한 번 돌 때 서버마다 한 번 요청합니다.
학교 홈페이지만 예외로, 취업게시판이나 FAQ를 볼 때(한 번에 둘 중 하나)와 평일 07:30에 학식과 학사일정을 함께 받을 때 __GAP_S__초 넘게 기다렸다가 한 번 더 요청합니다.
같은 서버에는 __GAP_S__초 이상 간격을 두고, 하루 상한(학교 홈페이지 __WWW_CAP__번, 학과·부서 사이트는 서버마다 __DEPT_CAP__번, 블로그 피드는 호스트마다 __FEED_CAP__번, __SMALL_NAMES__ 사이트는 서버마다 __SMALL_CAP__번)을 넘기지 않습니다. __SMALL_NAMES__ 사이트는 __SMALL_EVERY__시간마다 한 번만 봅니다. 403·429·보안문자 화면을 받으면 그 서버를 쉬고, 세 번 연속이면 운영자가 다시 켤 때까지 요청하지 않습니다.
요청의 User-Agent에 이 페이지 주소를 적어 둡니다. 사이트 관리자께서 수집을 원하지 않으시면 아래 보호책임자 이메일로 알려 주세요. 그 사이트 수집을 멈추고 회신합니다.</p>
<p><b>공지 제목 속 이름</b>: 합격자·수상자 명단처럼 공지 제목에 사람 이름이 들어 있을 수 있습니다. 저장하기 전에 규칙으로 이름·연락처·학번을 가리지만 빠지는 경우가 있습니다.
내 이름이 나온 공지를 지우고 싶으면 보호책임자에게 이메일로 알려 주세요. 저장된 공지, Discord 메시지, 전달 기록을 지우고 다음 수집에서 다시 올리지 않습니다.</p>
<p><b>알림 역할</b>: #알림-설정의 역할 버튼을 누르면 Discord가 그 계정에 알림 역할을 붙이거나 뗍니다. 누가 어떤 역할을 가졌는지(Discord 사용자 ID)는 Discord의 역할 정보에만 있고,
우리 서버는 따로 저장하지 않습니다. 버튼 요청을 처리하는 동안만 메모리에서 쓰고, 서버 로그에는 처리 결과만 남깁니다. 알림 내용은 학교 공개 공지입니다.
역할 버튼을 다시 누르면 구독이 풀립니다. Discord 이용에는 Discord(미국)의 개인정보 처리방침이 적용됩니다.</p>
<p><b>초대 코드 발급</b>: Discord에서 '설치하기'를 누르거나 /설치를 쓰면 그 사람에게만 보이는 초대 코드를 줍니다. 한 사람이 받을 수 있는 코드는 __INVITE_CAP__개라,
누가 몇 개 받았는지 세려고 Discord 사용자 ID와 코드의 해시, 받은 시각을 저장합니다(2항의 표). 코드 자체는 저장하지 않습니다.
운영자가 초대를 회수하면 그 코드로 등록한 PC와 연결한 앱도 1분 안에 끊깁니다.</p>

<h3>4. 처리 위탁과 국외 이전</h3>
<ul>
<li><b>위탁</b>: Amazon Web Services(AWS)에 서버·DB·로그 운영을 맡깁니다. 서버의 모든 기록은 아시아 태평양(서울) 리전에 저장됩니다.</li>
<li><b>Discord(미국)</b>: 학교 공지 알림(공개 공지의 제목과 링크)을 Discord 채널에 올리므로, 올린 메시지는 Discord가 보관합니다. 역할 버튼과 초대 코드 발급도 Discord를 거쳐 처리됩니다.</li>
<li><b>공지 요약(OpenAI·Anthropic, 미국)</b>: 학교 공개 공지의 본문·첨부·포스터에서 마감·대상·신청 방법을 뽑을 때 운영자가 OpenAI API(일부는 Anthropic Claude API)에 보냅니다. 사용자 정보는 보내지 않고, 글 속 연락처·학번·사람 이름은 가린 뒤 보냅니다(이미지 속 글자는 가리지 못해 이름이 들어갈 수 있는 공지는 본문만 보냅니다).</li>
<li><b>Claude(Anthropic, 미국)·ChatGPT(OpenAI, 미국)</b>: 이 서비스가 제공하거나 위탁하는 곳이 아닙니다. 사용자가 직접 고르고 연결한 AI 서비스가 사용자 계정으로 질문하고,
PC 에이전트의 답은 그 서비스로 전달됩니다. 전달된 답은 해당 회사의 개인정보 처리방침을 따릅니다. 성적처럼 민감한 내용을 물을지는 사용자가 정합니다.</li>
</ul>

<h3>5. 정보주체의 권리와 행사 방법</h3>
<p>언제든 내 기록의 열람, 정정, 삭제, 처리 정지를 요구할 수 있습니다.</p>
<ul>
<li>Claude·ChatGPT 설정에서 커넥터를 지우면 그 앱은 더 이상 묻지 못합니다.</li>
<li>기기 기록과 연결한 앱, Discord 초대 기록을 보거나 지우려면 아래 보호책임자에게 이메일로 요청하세요(연결한 날짜나 Discord 이름을 알려 주면 찾기 쉽습니다). 10일 안에 처리하고 결과를 알려 드립니다. 대리인을 통해서도 요청할 수 있습니다.</li>
</ul>

<h3>6. 안전성 확보 조치</h3>
<ul>
<li>학번·비밀번호는 사용자 PC의 OS 키체인에만 두고 서버로 보내지 않습니다.</li>
<li>모든 통신은 HTTPS(TLS)로 암호화합니다. PC의 답은 요청마다 다른 일회용 키(AES-256-GCM)로 암호화되고, 기기의 요청은 기기 키(ECDSA P-256) 서명으로 확인합니다.</li>
<li>토큰과 코드는 해시만 저장하고, 사건 기록은 2주 보관합니다.</li>
<li>PC 에이전트는 릴리스 키로 서명되고 의존성까지 고정된 새 버전만 스스로 설치합니다.</li>
<li>AWS 계정과 릴리스 키는 운영자 한 명만 다룹니다.</li>
</ul>

<h3>7. 개인정보 유출 사고 대응</h3>
<p>유출을 알게 되면 72시간 안에 해당 이용자에게 이메일이나 초대받은 경로로 알립니다. 유출된 항목, 시점과 경위, 피해를 줄이려고 이용자가 할 수 있는 일(예: 커넥터 삭제, KLAS 비밀번호 변경),
우리가 한 조치, 문의처를 담습니다. 법령이 정한 경우에는 72시간 안에 개인정보보호위원회나 한국인터넷진흥원(KISA)에 신고합니다.</p>

<h3>8. 개인정보 보호책임자</h3>
<p>운영자 kyowon1108 · 이메일 <a href="mailto:kyowon1108@gmail.com">kyowon1108@gmail.com</a><br>
권리 행사, 문의, 불만은 이 이메일로 보내 주세요.</p>
<p class="muted">권익 침해 구제: 개인정보침해신고센터(privacy.kisa.or.kr, 국번 없이 118), 개인정보분쟁조정위원회(kopico.go.kr, 1833-6972),
대검찰청(spo.go.kr, 1301), 경찰청(ecrm.police.go.kr, 182).</p>

<h3>9. 동의와 변경</h3>
<p>Claude 데스크톱에서 웹·폰 연결을 요청하고 커넥터를 연결하면 위 2·4항의 처리(기기 등록 정보 보관, 답의 암호화 중계와 사용자가 고른 AI 서비스로의 전달)에 동의하는 것으로 봅니다.
동의하지 않으면 웹·폰 연결 없이 Claude 데스크톱에서만 쓰세요. 커넥터를 지우거나 보호책임자에게 요청해 언제든 철회할 수 있습니다.
이 방침이 바뀌면 이 페이지에 시행일과 함께 알립니다.</p>
</body></html>"""


def privacy_page() -> str:
    """/privacy with each retention period read from the constant that enforces it, so the notice
    can't drift from the code (as "180일" once did while grants ran 120 days)."""
    # the connector's notice reader (kw_mcp), only for its cap; the poster reader (httpx), only for its cap
    from kwcampus.bridge import notice_reader
    from kwcampus.extract import READ_WINDOW, TTL_MARGIN
    from kwcampus.notice_assets import THUMB_DAILY_CAP
    from kwcampus.store import BODY_QUEUE_TTL

    periods = {
        "__GRANT_DAYS__": store.GRANT_TTL // 86400,
        "__CLIENT_PENDING_DAYS__": store.CLIENT_PENDING_TTL // 86400,
        "__CLIENT_DAYS__": store.CLIENT_TTL // 86400,
        "__CODE_MIN__": store.CODE_TTL // 60,
        "__AUTHREQ_MIN__": store.AUTHREQ_TTL // 60,
        "__PAIR_MIN__": store.PAIR_TTL // 60,  # also the kw_pair cookie's longest life (app.PAIR_COOKIE_MAX_S)
        "__INVITE_CAP__": invites.MAX_PER_MEMBER,
        "__CONN_HOURS__": store.CONN_TTL // 3600,
        "__SIG_MIN__": signing.MAX_SKEW_S * 2 // 60,  # the replay window signing.py passes to use_once
        "__RESULT_MIN__": store.RESULT_TTL // 60,
        "__NOTICE_DAYS__": NOTICE_TTL.days,
        "__DELIVERY_DAYS__": DELIVERY_TTL.days,
        # discord.RECORD_GRACE: _record_ttl keeps a record at least this long past its notice's ttl
        "__RECORD_GRACE_DAYS__": (DELIVERY_TTL - NOTICE_TTL).days,
        # what the collector paragraph tells a school webmaster
        "__GAP_S__": int(politeness.MIN_INTERVAL.total_seconds()),
        "__ASSET_GAP_S__": politeness.ASSET_GAP_S,
        "__FEED_HOSTS__": ", ".join(feed.host for feed in wixfeeds.FEEDS),
        "__WWW_CAP__": WWW_DAILY_CAP,
        "__DEPT_CAP__": deptboards.DAILY_CAP,
        "__FEED_CAP__": wixfeeds.DAILY_CAP,
        "__BRIDGE_CAP__": notice_reader.DAILY_CAP,
        "__SMALL_SITES__": "·".join(f"{b.name}({b.host})" for b in smallboards.BOARDS),
        # a queue row's life, and the longest a retry can keep it (extract: READ_WINDOW, TTL_MARGIN)
        "__QUEUE_DAYS__": BODY_QUEUE_TTL.days + READ_WINDOW.days + TTL_MARGIN.days,
        "__SMALL_CAPS__": "·".join(f"{b.name} {b.daily_cap}번" for b in smallboards.BOARDS),
        "__ALUMNI_CAP__": smallboards.ALUMNI.daily_cap,
        "__THUMB_HOST__": smallboards.THUMB_HOST,
        "__THUMB_CAP__": THUMB_DAILY_CAP,
        "__SMALL_NAMES__": "·".join(b.name for b in smallboards.BOARDS),
        "__SMALL_CAP__": max(b.daily_cap for b in smallboards.BOARDS),
        "__SMALL_EVERY__": "~".join(dict.fromkeys(str(int(h)) for h in sorted(
            b.every.total_seconds() / 3600 for b in smallboards.BOARDS))),
    }
    page = PRIVACY_TEMPLATE
    for marker, value in periods.items():
        page = page.replace(marker, str(value))
    return page


@cache
def _release_at(path: Path, size: int, mtime_ns: int) -> dict[str, Any] | None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(manifest, dict) and release.verify(manifest) and release.verify_constraints(manifest)):
        return None
    pins = STATIC_DIR / manifest["constraints"]["file"]
    if not (STATIC_DIR / manifest["wheel"]).is_file() or not pins.is_file():
        return None
    cutoff = release.exclude_newer(pins.read_text(encoding="utf-8"))
    return {**manifest, "exclude_newer": cutoff} if cutoff else None


def current_release() -> dict[str, Any] | None:
    """The signed release in this bundle (latest.json verified against the release key, with its
    wheel and constraints present), or None."""
    path = STATIC_DIR / MANIFEST_NAME
    try:
        st = path.stat()
    except OSError:
        return None
    return _release_at(path, st.st_size, st.st_mtime_ns)


@cache
def _data_at(path: Path, size: int, mtime_ns: int) -> dict[str, Any] | None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest if release.verify_data(manifest) else None


def current_data() -> dict[str, Any] | None:
    """The data release in this bundle (data.json verified against the release key), or None."""
    path = DATA_DIR / release.DATA_MANIFEST_NAME
    try:
        st = path.stat()
    except OSError:
        return None
    return _data_at(path, st.st_size, st.st_mtime_ns)


def routes():
    async def start(request: Request) -> Response:
        script_nonce = secrets.token_urlsafe(16)
        return HTMLResponse(start_page(script_nonce), headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (f"default-src 'none'; script-src 'nonce-{script_nonce}'; style-src 'unsafe-inline'; "
                                        "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"),
        })

    async def setup(request: Request) -> Response:
        # The old guide's address, handed out with invites in its fragment (/setup#kwc_...): the browser
        # keeps the fragment across a redirect whose Location has none.
        return RedirectResponse("/start", status_code=302)

    async def privacy(request: Request) -> Response:
        return HTMLResponse(privacy_page(), headers={"Cache-Control": "no-cache", "X-Frame-Options": "DENY"})

    async def dist(request: Request) -> Response:
        name = request.path_params["name"]
        name = OLD_MCPB_NAMES.get(name, name)
        rel = current_release()
        if rel and name in (rel["wheel"], rel["constraints"]["file"]):
            media = "text/plain; charset=utf-8" if name.endswith(".txt") else "application/octet-stream"
            return FileResponse(STATIC_DIR / name, media_type=media)
        if name == MANIFEST_NAME and (STATIC_DIR / MANIFEST_NAME).is_file():
            return FileResponse(STATIC_DIR / MANIFEST_NAME, media_type="application/json",
                                headers={"Cache-Control": "no-cache"})
        if name in (MCPB_NAME, MCPB_ZIP_NAME) and (STATIC_DIR / MCPB_NAME).is_file():
            media = "application/zip" if name == MCPB_ZIP_NAME else "application/octet-stream"
            # no-cache: every release ships with the same 1980 Last-Modified (reproducible zips), so without it a
            # browser that once fetched an older release keeps handing out that copy for years
            return FileResponse(STATIC_DIR / MCPB_NAME, media_type=media, filename=name,
                                headers={"Cache-Control": "no-cache"})
        return PlainTextResponse("not found", status_code=404)

    async def dist_data(request: Request) -> Response:
        # Only data.json and the files it lists, by their exact package path (release.DATA_FILE: no "..").
        path = request.path_params["path"]
        data = current_data()
        if data is not None and path == release.DATA_MANIFEST_NAME:
            return FileResponse(DATA_DIR / path, media_type="application/json", headers={"Cache-Control": "no-cache"})
        entry = data["files"].get(path) if data is not None and release.DATA_FILE.fullmatch(path) else None
        wanted = request.query_params.get("sha256")
        if entry is None or (wanted is not None and wanted != entry["sha256"]) or not (DATA_DIR / path).is_file():
            return PlainTextResponse("not found", status_code=404)
        # A URL with the signed hash names those bytes for good; one without may change with the next release.
        return FileResponse(DATA_DIR / path, media_type="application/json",
                            headers={"Cache-Control": DATA_CACHE if wanted else "no-cache"})

    return [("/start", start), ("/setup", setup), ("/privacy", privacy), ("/dist/data/{path:path}", dist_data),
            ("/dist/{name}", dist)]
