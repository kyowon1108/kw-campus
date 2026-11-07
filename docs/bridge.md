# 연결 서버와 공개 MCP

[README](../README.md)로 돌아가기

## 연결 서버 (kw-bridge)

- 커넥터 URL: `https://<bridge-api>.execute-api.ap-northeast-2.amazonaws.com/mcp` (OAuth 2.1 + PKCE, 동적 클라이언트 등록)
- 안내 페이지: `https://<bridge-api>/start#<초대코드>` — 무엇을 물어볼 수 있는지와 설치 순서(디스코드에서 초대 코드 → Claude 데스크톱 → `kw-campus.mcpb` 더블클릭 → 학번·비밀번호·초대 코드 입력)를 보여 줍니다. 초대 코드는 주소의 `#` 뒤에 있어 서버로 오지 않고, 페이지 스크립트만 읽어 복사 상자에 보여 줍니다. 예전 주소 `/setup`은 `/start`로 보냅니다(`#` 뒤는 브라우저가 그대로 넘김). 터미널 설치(`/install.sh`, `/install.ps1`)는 없앴습니다. 디스코드 서버 참가 링크는 싣지 않습니다(초대받은 사람만 들어오는 서버).
- 웹·폰 연결: 학생이 Claude 데스크톱에 "웹이랑 폰에서도 쓰게 연결해 줘"라고 하면 확장(`connect_other_devices`)이 PC를 등록하고 자동 실행을 켠 뒤 `POST /device/pair`로 1회용 연결 코드를 받아 브라우저로 `/connect#<코드>`를 엽니다. 그 페이지가 `POST /connect/claim`으로 코드를 확인하고(쓰지는 않음), 브라우저가 그 코드를 받은 PC와 같은 네트워크(API Gateway가 본 IPv4 주소나 IPv6 /64를 JWT 비밀값으로 HMAC한 값을 `PAIR#` 행에 둠)이면 서버가 `kw_pair` 쿠키(HttpOnly, `Path=/oauth`, `SameSite=Lax`, localhost가 아니면 `Secure`, 코드의 남은 시간과 15분 중 짧은 쪽)를 두고, 커넥터 주소(`/mcp`)와 Claude 커넥터 설정 링크를 보여 줍니다. 학생이 커넥터를 추가해 승인 화면이 열리면 그 쿠키로 "이 PC와 연결하기" 한 번에 승인합니다(같은 출처 확인, 실패 5번 제한, 코드는 한 번만). 다른 기기에서는 화면에 크게 나온 코드를 직접 넣습니다. 네트워크가 다르면 쿠키 없이 "코드를 넣어 주세요"만 보여 주고 링크 속 코드는 띄우지 않습니다(남의 PC 코드를 심는 링크 방지). 연결 코드는 PC 한 대에 한 시간 10개까지입니다. 승인 대기 요청은 30분 동안 열려 있습니다.
- 연결 뒤: PC 에이전트가 WebSocket으로 요청을 받아 KLAS에서 읽고, 요청마다 다른 키로 암호화해 올립니다(`../kw-mcp/src/kw_mcp/agent.py`). 학번·비밀번호는 PC 밖으로 나가지 않고, 서버가 KLAS에 직접 로그인하는 경로는 없습니다. PC가 꺼져 있으면 도구는 PC를 켜고 다시 물어보라고, 공지·학식·학사일정·학사 안내는 PC 없이 된다고 답합니다.
- 도구: `this_week`, `today_briefing`, `my_notices`, `my_courses`, `my_assignments`, `my_quizzes`, `my_online_lectures`, `my_team_projects`, `my_course_notices`, `my_deadlines`, `my_assignment_detail`, `my_syllabus`, `my_course_board`, `my_course_post`, `my_attachment`, `my_grades`, `my_attendance`, `my_exams`, `my_class_changes`, `my_graduation` + 공개 도구 5개. 상세 도구 셋은 에이전트에 하나씩 묻는 `call` 요청이라 에이전트 프로토콜 2 이상이 필요합니다.
- `kw-bridge` 테이블에는 기기 공개키, OAuth 클라이언트, 코드·토큰 해시, 연결 ID, 2분짜리 암호문만 둡니다. 학번·비밀번호·개인 데이터는 없습니다.
- redirect는 Claude·ChatGPT의 콜백 URL 3개(정확히 일치)와 localhost만 받습니다. 다른 사이트로 돌아가는 클라이언트로 승인을 낚는 것을 막기 위해서입니다.
- 남용 방어: 승인 요청 하나에 실패 5번이면 닫습니다. `/device/result`는 서버가 연 요청 자리에만 한 번 쓸 수 있고 300KB까지입니다. 기기 서명은 한 번만 쓰입니다. 에이전트는 설치한 서버와 초대 코드의 서버가 다르면 연결하지 않습니다. 새 앱이 연결 코드로 붙으면 에이전트 로그에 남습니다. API는 경로별로 제한합니다: 클라이언트 등록·기기 등록 초당 1건, 승인·연결 코드·코드 확인(`/connect/claim`)·연결 관리 2건, 토큰 5건, `/mcp` 5건(순간 10건), 에이전트 결과 20건, 나머지(안내 페이지·내려받기 등) 10건(순간 20건). IP별 제한이 아니어서 누가 계속 두드리면 그 경로를 쓰는 모두가 느려질 수 있지만, 가입성 경로가 막혀도 에이전트 응답은 영향을 받지 않습니다. HTTP API에는 WAF를 붙일 수 없어 비용 상한 쪽을 택했습니다: bridge Lambda 동시 실행 20개, 에이전트 결과를 받는 Lambda는 따로 5개(답을 기다리는 `/mcp` 요청이 20개를 다 써도 결과는 들어옴), 월 5달러 예산 알림(크레딧·환불은 빼고 셈), 4xx 응답이 5분에 100건 이상이면 알림.
- 내려받기도 bridge가 직접 줍니다: `/dist/kw-campus.mcpb`(Microsoft Store판 Claude용으로 같은 파일을 `/dist/kw-campus.zip`으로도), 이미 설치된 에이전트가 스스로 업데이트할 때 받는 `/dist/latest.json`·`/dist/kw_mcp-*.whl`·의존성 고정 파일. 공개 JSON API(`/c/<토큰>/api/...`)도 같은 주소에서 받습니다.

**초대**: 초대 코드는 `kwc_`로 시작하는 문자열 하나(서버 주소 + 토큰)입니다. 멤버는 디스코드 #설치-안내의 '설치하기'(또는 `/설치`)로 직접 받습니다(`src/kwcampus/invites.py`: 한 사람당 3개, 모두 합쳐 하루 50개, `kw-bridge`에 `INVITE#<해시>`·`DMEMBER#<사용자 ID>`·`DINVITES#<날짜>`로 해시와 개수만 저장). 손으로 만들 때는 토큰을 새로 만들어 해시를 `/kw/mcp/tokens`에 더한 뒤 코드를 만듭니다. 두 곳 어디에 있든 PC 등록, 커넥터, 공개 API(`/c/<토큰>/`)에 똑같이 쓰입니다.

```bash
uv run python -c "from kw_mcp.agent import make_invite; import sys; print(make_invite(sys.argv[1], sys.argv[2]))" <bridge-url> <토큰>
```

손으로 만든 초대는 `https://<bridge-api>/start#<초대코드>` 링크 하나만 보내면 됩니다. 회수하려면 그 토큰의 해시를 목록에서 지우고, 디스코드에서 나간 초대는 `invites.revoke(table, <해시>)`로 회수 표시를 합니다. 그 초대로 등록한 PC와 그 PC로 연결한 앱, 공개 API 토큰은 1분 안에 끊깁니다(액세스·refresh 토큰과 새 연결 코드를 거절).
## 공개 MCP

- 주소: `https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/c/<토큰>/mcp` (POST만, Streamable HTTP, stateless)
- 토큰: 사람마다 하나씩 줍니다. SSM `/kw/mcp/tokens`에는 SHA-256 해시 목록만 둡니다. 본인 토큰은 `~/.config/kw-campus/mcp-token`에 있습니다.
- 도구: `list_notice_categories`, `latest_notices`, `search_notices`, `get_notice`, `cafeteria_menu`, `academic_calendar`
- 로컬 kw-mcp용 JSON: `/c/<토큰>/api/notices`, `/api/menu`, `/api/calendar`
- 분류: id(`academic`), 이름(`학사`), 별칭(`장학금`, `취업관련`, `채용`, `학과`는 모든 학과·단과대), 이름 일부(`건축` → 건축공학과·건축학과)를 받습니다. 어디에도 맞지 않으면 전체를 돌려주지 않고 `unknown_category` 오류와 쓸 수 있는 분류 목록을 돌려줍니다(JSON API는 400).
- 분류 없이 받는 최신 목록과 검색어 없는 검색에는 FAQ가 빠집니다(알림도 안 보내는 참고 글). `category=FAQ`나 검색어로는 찾을 수 있습니다. 여러 학과가 같은 날 즈음 같이 올린 공지는 하나만 보이고 나머지 글 번호는 `also`에 들어갑니다.
- 내보내기 직전에 제목·부서를 한 번 더 마스킹합니다. 만료(ttl)가 지난 항목과 내린 항목은 DynamoDB가 지우기 전이라도 내보내지 않습니다.
- 학식·학사일정: `kw-campus` Lambda가 평일 07:30에 돕니다. 이번 주 식단(식사가 하나라도 있는 표)이 아직 없을 때만 학식 페이지를 읽습니다. 읽은 표는 표에 적힌 날짜의 주(월요일)로 저장하므로, 월요일 아침에 지난주 표가 아직 걸려 있으면 다음 날 07:30에 다시 읽습니다. 학사일정은 7일마다 한 번 읽습니다. 한 번에 www 요청은 2회까지이고, 두 요청 사이에는 실제로 61초를 쉽니다(요청을 보내지 않았으면 쉬지 않음). 12월에는 다음 해 학사일정도 받되, 올해 것을 읽은 날에는 다음 날로 미룹니다.
- 남용 방어: API Gateway가 초당 2건, 순간 최대 5건으로 제한합니다. GET/DELETE는 405를 돌려주어 SSE 연결이 Lambda를 붙잡지 않게 합니다.

친구에게 토큰을 줄 때는 새 토큰을 만들어 해시를 목록에 더합니다. 회수할 때는 그 해시를 지웁니다. 반영까지 최대 5분 걸립니다.
