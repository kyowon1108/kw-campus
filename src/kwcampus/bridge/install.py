"""One-line PC agent install, served by the bridge itself (no GitHub or PyPI needed for kw-mcp).

  macOS:   curl -fsSL <base>/install.sh | sh -s -- kwc_...
  Windows: $env:KWC='kwc_...'; irm <base>/install.ps1 | iex
  <base>/setup#kwc_... explains the two ways to connect (Claude Desktop extension, PC agent) with the
  invite filled in (a URL fragment never reaches the server). <base>/privacy is the 처리방침.

The scripts install uv (Astral's Python tool runner) if missing, then the kw-mcp release in this
Lambda bundle: the wheel and its dependency pins (constraints exported from kw-mcp's uv.lock), both
named and hashed in the signed latest.json, installed with nothing newer than the release and no
source builds. Then `kw-mcp link` asks for the KLAS login and stores it in the OS keychain.

What the scripts check comes from this server, so a first install trusts it (over TLS). Background
updates are different: agents verify latest.json against the release key they carry.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Any

from kw_mcp import release
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

STATIC_DIR = Path(__file__).parent / "static"
MCPB_NAME = release.MCPB_NAME  # Claude Desktop extension, packed and signed by kw-mcp's scripts/release.sh
# The same bytes as a .zip, for Claude Desktop builds whose installer closes on .mcpb files
# (Windows Store build, mcpb issue #281): unzip, then "Install Unpacked Extension".
MCPB_ZIP_NAME = "kw-klas.zip"
# Signed release manifest (kw_mcp.release). The signatures are made on the release machine; this
# server only passes them along and cannot forge one.
MANIFEST_NAME = release.MANIFEST_NAME

INSTALL_SH = """#!/bin/sh
# KW Campus PC agent installer (macOS). Usage: curl -fsSL __BASE__/install.sh | sh -s -- kwc_...
set -eu
INVITE="${1:-}"
if [ -z "$INVITE" ]; then
  printf 'Invite code (kwc_...): ' > /dev/tty
  read -r INVITE < /dev/tty
fi
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python tool runner)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
echo "Installing the KW Campus agent __VERSION__..."
# Download first: uv's own download probes the size with HEAD, which API Gateway answers with 0.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL -o "$TMP/__WHEEL__" "__BASE__/dist/__WHEEL__"
curl -fsSL -o "$TMP/__CONSTRAINTS__" "__BASE__/dist/__CONSTRAINTS__"
echo "__SHA256__  $TMP/__WHEEL__" | shasum -a 256 -c - >/dev/null
echo "__CONSTRAINTS_SHA256__  $TMP/__CONSTRAINTS__" | shasum -a 256 -c - >/dev/null
# Every dependency at the version this release was tested with, nothing uploaded later, no source builds.
uv tool install --force --quiet --python 3.13 --no-build --constraints "$TMP/__CONSTRAINTS__" \\
  --exclude-newer "__EXCLUDE_NEWER__" "kw-mcp @ file://$TMP/__WHEEL__"
# Keep this release: the agent goes back to it if a later update fails to start.
mkdir -p "$HOME/.config/kw-campus/releases" && chmod 700 "$HOME/.config/kw-campus"
cp "$TMP/__WHEEL__" "$TMP/__CONSTRAINTS__" "$HOME/.config/kw-campus/releases/"
kw-mcp link "$INVITE" --server "__BASE__" < /dev/tty
"""

INSTALL_PS1 = """# KW Campus PC agent installer (Windows). Usage: $env:KWC='kwc_...'; irm __BASE__/install.ps1 | iex
$ErrorActionPreference = 'Stop'
$invite = $env:KWC
if (-not $invite) { $invite = Read-Host 'Invite code (kwc_...)' }
$env:Path = "$env:USERPROFILE\\.local\\bin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host 'Installing uv (Python tool runner)...'
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
}
Write-Host 'Installing the KW Campus agent __VERSION__...'
# Kept here: the agent goes back to this release if a later update fails to start.
$rel = Join-Path $env:USERPROFILE '.config\\kw-campus\\releases'
New-Item -ItemType Directory -Force -Path $rel | Out-Null
$whl = Join-Path $rel '__WHEEL__'
$pins = Join-Path $rel '__CONSTRAINTS__'
Invoke-WebRequest -UseBasicParsing -Uri '__BASE__/dist/__WHEEL__' -OutFile $whl
Invoke-WebRequest -UseBasicParsing -Uri '__BASE__/dist/__CONSTRAINTS__' -OutFile $pins
if ((Get-FileHash -Algorithm SHA256 $whl).Hash.ToLower() -ne '__SHA256__' -or
    (Get-FileHash -Algorithm SHA256 $pins).Hash.ToLower() -ne '__CONSTRAINTS_SHA256__') {
  Remove-Item -Force $whl, $pins
  throw 'The downloaded agent failed its checksum.'
}
# Every dependency at the version this release was tested with, nothing uploaded later, no source builds.
# AbsoluteUri percent-encodes spaces and Korean letters in the profile path.
uv tool install --force --quiet --python 3.13 --no-build --constraints $pins --exclude-newer '__EXCLUDE_NEWER__' ("kw-mcp @ " + ([System.Uri]$whl).AbsoluteUri)
if ($LASTEXITCODE -ne 0) { throw 'uv could not install the agent.' }
kw-mcp link $invite --server '__BASE__'
"""

SETUP_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>KW Campus 설치</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#222;line-height:1.6}
pre{background:#f4f4f5;padding:12px;border-radius:8px;white-space:pre-wrap;word-break:break-all;margin:6px 0}
button,a.btn{font-size:14px;padding:6px 12px;border-radius:6px;border:1px solid #aaa;background:#fff;cursor:pointer;color:#222;text-decoration:none}
h3{margin-top:32px} .muted{color:#666;font-size:14px} .tag{font-size:12px;background:#8b1d2c;color:#fff;border-radius:4px;padding:1px 6px}
.tag.sub{background:#666}</style></head><body>
<h2>KW Campus 연결하기</h2>
<p>Claude·ChatGPT에서 내 KLAS 과제·퀴즈·온라인강의·시간표·과목 공지와 학교 공지·학식·학사일정을 물어볼 수 있게 합니다.
읽기 전용이며, 광운대학교가 만들거나 승인한 도구가 아닙니다.</p>
<p>초대 코드</p><pre id="invite"></pre><button data-copy="invite">복사</button>
<p id="noinvite" class="muted" hidden>초대 링크로 열면 여기에 초대 코드가 채워집니다.</p>

<h3>방법 1. Claude 데스크톱 앱 확장 <span class="tag">기본</span></h3>
<p><a class="btn" href="/dist/kw-klas.mcpb">kw-klas.mcpb 받기</a> → 파일을 더블클릭해 Claude 데스크톱에 설치 → 학번·비밀번호·초대 코드 입력.
모든 처리가 이 PC 안에서 일어나고, 학번과 비밀번호는 OS 키체인에 암호화해 저장되며 KLAS로만 갑니다. 우리 서버로는 개인 정보가 오지 않습니다.
Claude 데스크톱에서만 쓸 수 있고, 초대 코드를 넣어 두면 새 버전이 나올 때 확장이 알려 줍니다.</p>
<p class="muted">받은 파일이 깨지지 않았는지 확인(SHA-256): <code>__MCPB_SHA256__</code><br>
설치 창이 그냥 닫히면(Windows Microsoft Store판 Claude에서 생기는 문제):
<a href="/dist/kw-klas.zip">kw-klas.zip</a>을 받아 압축을 풀고, Claude 설정 → 확장 프로그램 → 고급 설정 → "압축 해제된 확장 설치"에서 푼 폴더를 고르세요.
출처를 모르는 다른 확장은 같이 설치하지 마세요(다른 확장이 저장된 비밀번호를 꺼낼 수 있다는 보고가 있습니다).</p>

<h3>방법 2. Claude 웹·모바일, ChatGPT: PC 에이전트 <span class="tag sub">보조</span></h3>
<p><b>보조 경로입니다. 항상 켜 두는 PC(집 데스크톱 등)가 있어야 합니다.</b> 그 PC가 꺼져 있거나 잠자기 상태면 모든 기기에서 응답하지 않습니다.
Claude 데스크톱을 쓴다면 방법 1이 더 간단하고 서버를 거치지 않습니다.</p>
<p>그 PC에 작은 에이전트를 설치하면, 다른 기기의 Claude·ChatGPT 커넥터가 물을 때 그 PC가 내 KLAS 로그인으로 읽어 답합니다.
비밀번호는 그 PC 밖으로 나가지 않습니다. 답은 암호화되어 이 서버를 거쳐 전달되고, 서버는 이어지는 질문에 최대 1분 동안 다시 쓴 뒤 메모리에서 버립니다.</p>
<p><b>macOS</b> — 터미널에 붙여 넣기</p><pre id="mac"></pre><button data-copy="mac">복사</button>
<p><b>Windows</b> — PowerShell에 붙여 넣기</p><pre id="win"></pre><button data-copy="win">복사</button>
<p class="muted">설치 명령을 실행하면 <a href="/privacy">개인정보 처리방침</a>의 PC 에이전트 항목(기기 등록 정보 보관, 답의 암호화 중계)에 동의하는 것으로 봅니다.
동의하지 않으면 방법 1을 쓰세요. <code>kw-mcp unlink</code>로 언제든 철회할 수 있습니다.</p>
<p class="muted">학번·비밀번호를 물어보면 입력하세요. 끝나면 연결 코드가 나옵니다.
그다음 Claude(설정 → 커넥터 → 커스텀 커넥터 추가) 또는 ChatGPT(설정 → 앱과 커넥터 → 만들기)에 아래 URL을 넣고, 열리는 승인 페이지에 연결 코드를 넣으세요.
연결 코드는 비밀번호처럼 다루세요(화면을 찍어 올리지 마세요).</p>
<pre id="mcp"></pre><button data-copy="mcp">복사</button>
<p class="muted">KLAS는 해외로 보이는 IP의 로그인에 2차 인증을 요구하므로 VPN은 끄고 쓰세요.
에이전트는 로그인할 때마다 자동으로 실행되고, KLAS 세션이 끊기면 다시 로그인하며, 릴리스 키로 서명된 새 버전만 스스로 설치합니다.
상태 <code>kw-mcp status</code> · 새 연결 코드 <code>kw-mcp code</code> · 연결한 앱 <code>kw-mcp connections</code> · 앱 하나 끊기 <code>kw-mcp revoke &lt;ID&gt;</code> ·
지금 업데이트 <code>kw-mcp update</code> · 비밀번호 변경 후 <code>kw-mcp setup</code> · 해제 <code>kw-mcp unlink</code></p>
<script>
const base = location.origin;
// What doesn't depend on the invite first, so a bad fragment can't leave the page blank.
document.getElementById('mcp').textContent = `${base}/mcp`;
document.querySelectorAll('button[data-copy]').forEach(b => b.onclick = async () => {
  await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent);
  b.textContent = '복사됨';
});
let raw = location.hash.slice(1);
try { raw = decodeURIComponent(raw); } catch (e) { /* a truncated %-escape: treat as no invite */ }
const invite = raw.trim();
// Only show commands for an invite that points back at this server (a doctored link could aim elsewhere).
let ok = /^kwc_[A-Za-z0-9_-]+$/.test(invite);
if (ok) {
  try {
    const b64 = invite.slice(4).replace(/-/g, '+').replace(/_/g, '/');
    ok = JSON.parse(atob(b64 + '='.repeat((4 - b64.length % 4) % 4))).s === location.origin;
  } catch (e) { ok = false; }
}
document.getElementById('noinvite').hidden = ok;
document.getElementById('invite').textContent = ok ? invite : '(초대받은 사람에게 받으세요)';
document.getElementById('mac').textContent = `curl -fsSL ${base}/install.sh | sh -s -- ${ok ? invite : ''}`.trim();
document.getElementById('win').textContent = (ok ? `$env:KWC='${invite}'; ` : '') + `irm ${base}/install.ps1 | iex`;
</script>
<p class="muted"><a href="/privacy">개인정보 처리방침</a></p></body></html>"""

PRIVACY_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
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
<li><b>Claude 데스크톱 확장(방법 1)</b>: 학교 공지·학식·학사일정 같은 공개 정보와 새 버전 정보를 제공합니다. 개인정보는 처리하지 않습니다.
학번·비밀번호와 KLAS에서 읽은 내용은 사용자 PC 안에서만 쓰이고 이 서버로 오지 않습니다. 요청에는 초대 토큰과 검색 조건(분류·날짜·검색어)만 담깁니다.</li>
<li><b>PC 에이전트(방법 2)</b>: 사용자가 연결한 Claude·ChatGPT의 질문을 사용자 PC로 전달하고, PC가 암호화해 보낸 답을 돌려주기 위해 기기와 연결한 앱을 확인·관리하고 부정 사용을 막습니다.</li>
<li><b>Discord 알림 채널</b>: 학교 공지 알림을 받을 분류를 고르게 합니다.</li>
<li><b>공통</b>: 서비스 운영, 장애 대응, 보안 사고 조사.</li>
</ul>

<h3>2. 처리 항목과 보유 기간</h3>
<p>학번·비밀번호는 받지도 저장하지도 않습니다. 아래는 PC 에이전트를 쓸 때 서버(AWS)에 생기는 기록입니다.</p>
<table>
<tr><th>항목</th><th>보유 기간</th></tr>
<tr><td>기기 ID(무작위 값), 기기 공개키, 등록 시각</td><td><code>kw-mcp unlink</code>로 해제할 때까지. 해제하면 바로 삭제</td></tr>
<tr><td>연결한 앱 목록(앱 이름, 돌아갈 주소의 호스트, 연결·마지막 사용 시각)</td><td><code>kw-mcp revoke</code>로 끊거나 기기를 해제할 때까지, 길어도 갱신 토큰 만료(180일)까지</td></tr>
<tr><td>앱(Claude·ChatGPT) 등록 정보(OAuth 클라이언트 이름, 돌아갈 주소)</td><td>1년</td></tr>
<tr><td>갱신 토큰의 해시(쓸 때마다 새로 발급)</td><td>180일</td></tr>
<tr><td>승인 코드의 해시 / 승인 대기 요청(state, 돌아갈 주소, PKCE 값)</td><td>5분 / 10분, 한 번 쓰면 삭제</td></tr>
<tr><td>1회용 연결 코드의 해시(<code>kw-mcp code</code>)</td><td>15분</td></tr>
<tr><td>에이전트 접속 정보(WebSocket 연결 ID, 에이전트 버전, 접속 시각)</td><td>3시간</td></tr>
<tr><td>이미 쓴 기기 서명의 해시(재사용 방지)</td><td>10분</td></tr>
<tr><td>암호화된 답(요청마다 다른 키로 PC가 암호화한 KLAS 조회 결과)</td><td>전달되면 바로, 늦어도 2분 안에 삭제</td></tr>
<tr><td>풀어낸 답(서버 메모리에만, DB·디스크에 쓰지 않음)</td><td>이어지는 질문에 최대 1분 다시 쓴 뒤 폐기</td></tr>
<tr><td>서버 로그: 사건 종류(연결, 승인, 거부, 철회 같은 것)와 오류 코드, 시각</td><td>2주</td></tr>
</table>
<p>접근 토큰은 1시간짜리 서명 토큰이라 서버에 저장하지 않습니다. 서버 로그에는 IP 주소, 요청 주소(초대 토큰), 학번·이름·성적 같은 내용을 남기지 않도록 만들었습니다
(요청을 전달하는 동안 AWS가 IP 주소를 다루기는 합니다). 로그에 남기는 항목을 늘리게 되면 이 표에 먼저 적습니다.
보유 기간이 지난 기록은 DB의 만료 기능과 로그 보관 설정으로 자동 삭제합니다(DB 만료 삭제는 며칠 늦을 수 있지만, 만료된 기록은 쓰지 않습니다). 별도 백업은 두지 않으며, 백업을 켜게 되면 이 방침에 먼저 적습니다.</p>

<h3>3. Discord 알림 채널</h3>
<p>#알림-설정의 역할 버튼을 누르면 Discord가 그 계정에 알림 역할을 붙이거나 뗍니다. 누가 어떤 역할을 가졌는지(Discord 사용자 ID)는 Discord의 역할 정보에만 있고,
우리 서버는 따로 저장하지 않습니다. 버튼 요청을 처리하는 동안만 메모리에서 쓰고, 서버 로그에는 처리 결과만 남깁니다. 알림 내용은 학교 공개 공지입니다.
역할 버튼을 다시 누르면 구독이 풀립니다. Discord 이용에는 Discord(미국)의 개인정보 처리방침이 적용됩니다.</p>

<h3>4. 처리 위탁과 국외 이전</h3>
<ul>
<li><b>위탁</b>: Amazon Web Services(AWS)에 서버·DB·로그 운영을 맡깁니다. 모든 기록은 아시아 태평양(서울) 리전에 저장됩니다.</li>
<li><b>Claude(Anthropic, 미국)·ChatGPT(OpenAI, 미국)</b>: 이 서비스가 제공하거나 위탁하는 곳이 아닙니다. 사용자가 직접 고르고 연결한 AI 서비스가 사용자 계정으로 질문하고,
PC 에이전트의 답은 그 서비스로 전달됩니다. 전달된 답은 해당 회사의 개인정보 처리방침을 따릅니다. 성적처럼 민감한 내용을 물을지는 사용자가 정합니다.</li>
</ul>

<h3>5. 정보주체의 권리와 행사 방법</h3>
<p>언제든 내 기록의 열람, 정정, 삭제, 처리 정지를 요구할 수 있습니다. 대부분은 PC에서 바로 할 수 있습니다.</p>
<ul>
<li>연결한 앱 보기 <code>kw-mcp connections</code> · 앱 하나 끊기 <code>kw-mcp revoke &lt;ID&gt;</code> · 기기 기록과 모든 연결 삭제 <code>kw-mcp unlink</code></li>
<li>Claude·ChatGPT 설정에서 커넥터를 지우면 그 앱은 더 이상 묻지 못합니다.</li>
<li>PC를 잃어버렸거나 명령을 쓸 수 없으면 아래 보호책임자에게 이메일로 요청하세요(<code>kw-mcp status</code>에 나오는 기기 ID 앞자리나 연결한 날짜를 알려 주면 찾기 쉽습니다). 10일 안에 처리하고 결과를 알려 드립니다. 대리인을 통해서도 요청할 수 있습니다.</li>
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
<p>유출을 알게 되면 72시간 안에 해당 이용자에게 이메일이나 초대받은 경로로 알립니다. 유출된 항목, 시점과 경위, 피해를 줄이려고 이용자가 할 수 있는 일(예: <code>kw-mcp unlink</code>, 커넥터 삭제),
우리가 한 조치, 문의처를 담습니다. 법령이 정한 경우에는 72시간 안에 개인정보보호위원회나 한국인터넷진흥원(KISA)에 신고합니다.</p>

<h3>8. 개인정보 보호책임자</h3>
<p>운영자 kyowon1108 · 이메일 <a href="mailto:kyowon1108@gmail.com">kyowon1108@gmail.com</a><br>
권리 행사, 문의, 불만은 이 이메일로 보내 주세요.</p>
<p class="muted">권익 침해 구제: 개인정보침해신고센터(privacy.kisa.or.kr, 국번 없이 118), 개인정보분쟁조정위원회(kopico.go.kr, 1833-6972),
대검찰청(spo.go.kr, 1301), 경찰청(ecrm.police.go.kr, 182).</p>

<h3>9. 동의와 변경</h3>
<p>PC 에이전트 설치 명령을 실행하고 커넥터를 승인하면 위 2·4항의 처리(기기 등록 정보 보관, 답의 암호화 중계와 사용자가 고른 AI 서비스로의 전달)에 동의하는 것으로 봅니다.
동의하지 않으면 방법 1(데스크톱 확장)을 쓰세요. <code>kw-mcp unlink</code>로 언제든 철회할 수 있습니다.
이 방침이 바뀌면 이 페이지에 시행일과 함께 알립니다.</p>
</body></html>"""


@cache
def _sha256(path: Path, size: int, mtime_ns: int = 0) -> str:  # size, mtime in the key: a redeploy gets a new hash
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _script(template: str, base_url: str) -> Response:
    rel = current_release()
    if not rel:
        return PlainTextResponse("echo 'installer is not available yet'; exit 1\n", status_code=503)
    # Every value below was checked by release.verify* (fixed file-name patterns, hex hashes, a timestamp).
    body = (template.replace("__BASE__", base_url).replace("__VERSION__", rel["version"])
            .replace("__WHEEL__", rel["wheel"]).replace("__SHA256__", rel["sha256"])
            .replace("__CONSTRAINTS__", rel["constraints"]["file"])
            .replace("__CONSTRAINTS_SHA256__", rel["constraints"]["sha256"])
            .replace("__EXCLUDE_NEWER__", rel["exclude_newer"]))
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-cache"})


def routes(base_url: str):
    async def install_sh(request: Request) -> Response:
        return _script(INSTALL_SH, base_url)

    async def install_ps1(request: Request) -> Response:
        return _script(INSTALL_PS1, base_url)

    async def setup(request: Request) -> Response:
        mcpb = STATIC_DIR / MCPB_NAME
        digest = _sha256(mcpb, mcpb.stat().st_size, mcpb.stat().st_mtime_ns) if mcpb.is_file() else "(아직 없음)"
        return HTMLResponse(SETUP_PAGE.replace("__MCPB_SHA256__", digest),
                            headers={"Cache-Control": "no-cache", "Referrer-Policy": "no-referrer",
                                     "X-Frame-Options": "DENY"})

    async def privacy(request: Request) -> Response:
        return HTMLResponse(PRIVACY_PAGE, headers={"Cache-Control": "no-cache", "X-Frame-Options": "DENY"})

    async def dist(request: Request) -> Response:
        name = request.path_params["name"]
        rel = current_release()
        if rel and name in (rel["wheel"], rel["constraints"]["file"]):
            media = "text/plain; charset=utf-8" if name.endswith(".txt") else "application/octet-stream"
            return FileResponse(STATIC_DIR / name, media_type=media)
        if name == MANIFEST_NAME and (STATIC_DIR / MANIFEST_NAME).is_file():
            return FileResponse(STATIC_DIR / MANIFEST_NAME, media_type="application/json",
                                headers={"Cache-Control": "no-cache"})
        if name in (MCPB_NAME, MCPB_ZIP_NAME) and (STATIC_DIR / MCPB_NAME).is_file():
            media = "application/zip" if name == MCPB_ZIP_NAME else "application/octet-stream"
            return FileResponse(STATIC_DIR / MCPB_NAME, media_type=media, filename=name)
        return PlainTextResponse("not found", status_code=404)

    return [("/install.sh", install_sh), ("/install.ps1", install_ps1), ("/setup", setup), ("/privacy", privacy),
            ("/dist/{name}", dist)]
