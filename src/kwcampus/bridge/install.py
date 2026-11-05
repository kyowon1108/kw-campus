"""One-line PC agent install, served by the bridge itself (no GitHub or PyPI needed).

  macOS:   curl -fsSL <base>/install.sh | sh -s -- kwc_...
  Windows: $env:KWC='kwc_...'; irm <base>/install.ps1 | iex
  <base>/setup#kwc_... explains the two ways to connect (Claude Desktop extension, PC agent) with the
  invite filled in (a URL fragment never reaches the server).

The scripts install uv (Astral's Python tool runner) if missing, install the kw-mcp wheel shipped
in this Lambda bundle, and run `kw-mcp link`, which asks for the KLAS login and stores it in the
OS keychain.
"""

from __future__ import annotations

import hashlib
import re
from functools import cache
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

STATIC_DIR = Path(__file__).parent / "static"
MCPB_NAME = "kw-klas.mcpb"  # Claude Desktop extension, packed in ../kw-mcp with `mcpb pack`
# Signed release manifest PC agents poll to update themselves (kw_mcp.release). The signature is
# made on the release machine; this server only passes it along and cannot forge one.
MANIFEST_NAME = "latest.json"
_WHEEL_NAME = re.compile(r"^kw_mcp-[\w.]+-py3-none-any\.whl$")

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
echo "Installing the KW Campus agent..."
# Download first: uv's own download probes the size with HEAD, which API Gateway answers with 0.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL -o "$TMP/__WHEEL__" "__BASE__/dist/__WHEEL__"
echo "__SHA256__  $TMP/__WHEEL__" | shasum -a 256 -c - >/dev/null
uv tool install --force --quiet --python 3.13 "kw-mcp @ file://$TMP/__WHEEL__"
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
Write-Host 'Installing the KW Campus agent...'
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $tmp | Out-Null
$whl = Join-Path $tmp '__WHEEL__'
Invoke-WebRequest -UseBasicParsing -Uri '__BASE__/dist/__WHEEL__' -OutFile $whl
if ((Get-FileHash -Algorithm SHA256 $whl).Hash.ToLower() -ne '__SHA256__') { throw 'The downloaded agent failed its checksum.' }
uv tool install --force --quiet --python 3.13 ("kw-mcp @ file:///" + $whl.Replace('\\', '/'))
if ($LASTEXITCODE -ne 0) { throw 'uv could not install the agent.' }
Remove-Item -Recurse -Force $tmp
kw-mcp link $invite --server '__BASE__'
"""

SETUP_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>KW Campus 설치</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#222;line-height:1.6}
pre{background:#f4f4f5;padding:12px;border-radius:8px;white-space:pre-wrap;word-break:break-all;margin:6px 0}
button,a.btn{font-size:14px;padding:6px 12px;border-radius:6px;border:1px solid #aaa;background:#fff;cursor:pointer;color:#222;text-decoration:none}
h3{margin-top:32px} .muted{color:#666;font-size:14px} .tag{font-size:12px;background:#8b1d2c;color:#fff;border-radius:4px;padding:1px 6px}</style></head><body>
<h2>KW Campus 연결하기</h2>
<p>Claude·ChatGPT에서 내 KLAS 과제·퀴즈·온라인강의·시간표·과목 공지와 학교 공지·학식·학사일정을 물어볼 수 있게 합니다.
읽기 전용이며, 광운대학교가 만들거나 승인한 도구가 아닙니다.</p>
<p>초대 코드</p><pre id="invite"></pre><button data-copy="invite">복사</button>
<p id="noinvite" class="muted" hidden>초대 링크로 열면 여기에 초대 코드가 채워집니다.</p>

<h3>방법 1. Claude 데스크톱 앱 확장 <span class="tag">가장 쉬움</span></h3>
<p><a class="btn" href="/dist/kw-klas.mcpb">kw-klas.mcpb 받기</a> → 파일을 더블클릭해 Claude 데스크톱에 설치 → 학번·비밀번호·초대 코드 입력.
모든 처리가 내 PC 안에서 일어나고 비밀번호는 OS 키체인에 저장됩니다. Claude 데스크톱에서만 쓸 수 있습니다.</p>

<h3>방법 2. PC 에이전트 (Claude 웹·모바일, ChatGPT)</h3>
<p>PC에 작은 에이전트를 한 번 설치하면, Claude·ChatGPT 커넥터가 물을 때 이 PC가 내 KLAS 로그인으로 읽어 답합니다.
비밀번호는 PC 밖으로 나가지 않고, 브라우저는 켜 둘 필요 없습니다. PC가 켜져 있어야 합니다.</p>
<p><b>macOS</b> — 터미널에 붙여 넣기</p><pre id="mac"></pre><button data-copy="mac">복사</button>
<p><b>Windows</b> — PowerShell에 붙여 넣기</p><pre id="win"></pre><button data-copy="win">복사</button>
<p class="muted">학번·비밀번호를 물어보면 입력하세요. 끝나면 연결 코드가 나옵니다.
그다음 Claude(설정 → 커넥터 → 커스텀 커넥터 추가) 또는 ChatGPT(설정 → 앱과 커넥터 → 만들기)에 아래 URL을 넣고, 열리는 승인 페이지에 연결 코드를 넣으세요.</p>
<pre id="mcp"></pre><button data-copy="mcp">복사</button>
<p class="muted">KLAS는 해외로 보이는 IP의 로그인에 2차 인증을 요구하므로 VPN은 끄고 쓰세요.
에이전트는 로그인할 때마다 자동으로 실행되고 KLAS 세션이 끊기면 다시 로그인합니다. PC가 켜져 있어야 합니다.
상태 <code>kw-mcp status</code> · 새 연결 코드 <code>kw-mcp code</code> · 비밀번호 변경 후 <code>kw-mcp setup</code> · 해제 <code>kw-mcp unlink</code></p>
<script>
const invite = decodeURIComponent(location.hash.slice(1)).trim();
// Only show commands for an invite that points back at this server (a doctored link could aim elsewhere).
let ok = /^kwc_[A-Za-z0-9_-]+$/.test(invite);
if (ok) {
  try {
    const b64 = invite.slice(4).replace(/-/g, '+').replace(/_/g, '/');
    ok = JSON.parse(atob(b64 + '='.repeat((4 - b64.length % 4) % 4))).s === location.origin;
  } catch (e) { ok = false; }
}
document.getElementById('noinvite').hidden = ok;
const base = location.origin;
document.getElementById('invite').textContent = ok ? invite : '(초대받은 사람에게 받으세요)';
document.getElementById('mcp').textContent = `${base}/mcp`;
document.getElementById('mac').textContent = `curl -fsSL ${base}/install.sh | sh -s -- ${ok ? invite : ''}`.trim();
document.getElementById('win').textContent = (ok ? `$env:KWC='${invite}'; ` : '') + `irm ${base}/install.ps1 | iex`;
document.querySelectorAll('button[data-copy]').forEach(b => b.onclick = async () => {
  await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent);
  b.textContent = '복사됨';
});
</script></body></html>"""


def wheel() -> Path | None:
    wheels = sorted(p for p in STATIC_DIR.glob("kw_mcp-*.whl") if _WHEEL_NAME.match(p.name))
    return wheels[-1] if wheels else None


@cache
def _sha256(path: Path, size: int) -> str:  # size in the key: a redeploy with a new wheel gets a new hash
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _script(template: str, base_url: str) -> Response:
    w = wheel()
    if not w:
        return PlainTextResponse("echo 'installer is not available yet'; exit 1\n", status_code=503)
    body = (template.replace("__BASE__", base_url).replace("__WHEEL__", w.name)
            .replace("__SHA256__", _sha256(w, w.stat().st_size)))
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-cache"})


def routes(base_url: str):
    async def install_sh(request: Request) -> Response:
        return _script(INSTALL_SH, base_url)

    async def install_ps1(request: Request) -> Response:
        return _script(INSTALL_PS1, base_url)

    async def setup(request: Request) -> Response:
        return HTMLResponse(SETUP_PAGE, headers={"Cache-Control": "no-cache", "Referrer-Policy": "no-referrer",
                                                 "X-Frame-Options": "DENY"})

    async def dist(request: Request) -> Response:
        name = request.path_params["name"]
        w = wheel()
        if w and name == w.name:
            return FileResponse(w, media_type="application/octet-stream")
        if name == MANIFEST_NAME and (STATIC_DIR / MANIFEST_NAME).is_file():
            return FileResponse(STATIC_DIR / MANIFEST_NAME, media_type="application/json",
                                headers={"Cache-Control": "no-cache"})
        if name == MCPB_NAME and (STATIC_DIR / MCPB_NAME).is_file():
            return FileResponse(STATIC_DIR / MCPB_NAME, media_type="application/octet-stream", filename=MCPB_NAME)
        return PlainTextResponse("not found", status_code=404)

    return [("/install.sh", install_sh), ("/install.ps1", install_ps1), ("/setup", setup), ("/dist/{name}", dist)]
