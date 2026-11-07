# kw-campus

광운대 공개 정보를 AWS(서울)에서 모아 Discord 채널로 알리고, Claude·ChatGPT에서 쓰는 MCP로 제공합니다. 연결 서버(bridge)를 거치면 각자 자기 KLAS 과제·퀴즈·온라인강의·시간표·과목 공지도 함께 물어볼 수 있습니다. 전체 계획은 `../kw-mcp/docs/plan.md`에 있습니다.

## 4단계: 연결 서버 (kw-bridge)

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

## 2단계: 공개 MCP

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

## 5단계: 학과 공지 (인공지능융합대학)

- 출처: 인공지능융합대학 홈의 "대학 공지" 위젯(`https://aicon.kw.ac.kr/main/main.php`). 요청 한 번으로 4개 학부(컴퓨터정보공학부·소프트웨어학부·정보융합학부·로봇학부)의 최신 공지 5개를 받습니다. 학부 게시판 1페이지는 고정 글뿐이라 새 글은 위젯에서만 보입니다.
- 수집: 메인 게시판과 같은 예약 실행(평일 09:00~18:40 20분마다와 21:00)에서 E3 학과 게시판과 번갈아 요청합니다. 차례는 시각이 아니라 이 서버에 마지막으로 요청한 쪽(`last_by`)으로 정하므로 21:00 실행도 번갈아 갑니다. 게시판이 차례를 못 쓰면 위젯은 80분 뒤에 다시 갑니다. ce/cs/ic/cni는 이 호스트의 별칭이고 kwlife·kwlaw·kwgame·kwsports·ingenium·liberal·semi·huss·inno도 같은 서버(E3, 211.233.72.248)에 있어서, 요청 간격·하루 상한(32회)·쿨다운은 이 서버 하나 기준이고 학과 게시판과 같이 씁니다. 예전 `ORIGIN#aicon.kw.ac.kr` 상태(쿨다운·차단 횟수·오늘 횟수)는 첫 실행 때 `ORIGIN#211.233.72.248`로 옮기고 지웁니다.
- 실패: 서버 신호(403·429·보안문자 화면·연결 실패·5xx)만 E3에 기록합니다. 위젯 주소의 404·리디렉트·바뀐 화면은 위젯만 40분 → 80분 → … 최대 24시간 쉬고(SourceFailed 지표), 학과 게시판은 그대로 돕니다. 빈 목록은 E3의 빈 응답 횟수에 더해, 위젯과 게시판 한 바퀴(10번) 내내 비면 서버를 쉬게 합니다. 첫 수집은 기준점만 저장합니다(알림 없음). 이 사이트가 실패해도 메인 게시판 수집은 그대로 끝납니다.
- 링크: 위젯의 href는 쓰지 않고, 이 사이트의 공지 링크에서 잡은 글 번호로 `https://aicon.kw.ac.kr/administration/notice.php?admin_mode=read&no=<번호>`를 만듭니다. 다른 호스트로 가는 링크는 버립니다.
- 학부 게시판 2쪽(`department_office/lecture.php?site_type=N&page=2`)은 읽지 않습니다. E3 요청 1회를 위젯과 게시판 9개가 이미 나눠 쓰고 있어(게시판마다 6시간), 4개를 더하면 그 간격이 8시간 40분으로 늘어납니다. 2쪽 글은 대부분 위젯에 먼저 나오고, 따로 읽으면 같은 글이 다른 글 번호(`aicon-<번호>`와 학부 게시판 번호)로 두 번 저장됩니다.
- 저장: `kw-public`의 `SRC#aicon`. 공개 데이터에서 글 번호는 `aicon-<번호>`, 분류는 "인공지능융합대학"(별칭 "인융대"; "학과"는 모든 학과·단과대). 학교 공지 검색·최신 공지·`my_notices`의 `department`에 같이 나옵니다.
- 알림: 분류 `dept-aicon` → 채널 키 `department`. `/kw/discord/channels`에 `department`가 없으면 학교-공지(`notice`) 채널로 보냅니다.

## 5단계: 학과·단과대 게시판

- 출처: 학과 사이트들이 같이 쓰는 PHP 게시판 두 종류입니다. A형은 `BoardMode=list&BOARD_CODE=...`(글 `BoardMode=view&UID=`), B형은 aicon과 같은 엔진(글 `admin_mode=read&no=`)입니다. 게시판 목록과 학과 이름은 `src/kwcampus/deptboards.py`의 `BOARDS` 한 곳에만 있고, 게시판을 늘릴 때는 한 줄을 더합니다. 전자정보공과대학 공지는 학과 사이트마다 똑같이 걸려 있어 ei에서만 읽습니다.
- 학교 사이트는 외부 IDC 서버 네 대에 있고, 호스트 이름이 아니라 서버마다 요청 간격·하루 상한(32회: 평일 낮 30회 + 21시 1회 + 여유 1회)·쿨다운을 둡니다. 실행마다 서버마다 1회만 요청하고, 그 서버에서 가장 오래 안 읽은 게시판을 돌아가며 읽습니다. 한 서버에서 예외가 나도 다른 서버는 그 실행에서 그대로 읽습니다.

| 서버 | 게시판 (키: BOARD_CODE) | 게시판마다 읽는 간격 |
|---|---|---|
| E1 61.111.8.4 | 전자정보공과대학 ei:notice, 전자공학과 ee:bachelor, 전자통신공학과 elcomm:officenotice, 전자융합공학과 radiowave:notice, 전기공학과 electric:notice, 전자재료공학과 snme:college, 반도체시스템공학부 semicon:notice, 법학부 law:notice, 전기공학과 취업정보 electricjob:job, 경력개발팀 career(BOARD_CODE 없음), 창업지원단 startup:notice | 3시간 40분 |
| E2 61.111.6.200 | 건축공학과 archi:note, 인문사회과학대학 chss:notice, 국어국문학과 korean:notice, 영어산업학과 english:notice(EUC-KR), 산업심리학과 psy:notice, 동북아문화산업학부 dnaci:notice, 행정학과 kwpa:faculty, 경영대학 biz:note, 한국어교육센터 kwiie:notice, 인권센터 humanrights:notice, 국제처 유학생 oia:note, 일반대학원 grad:notice | 4시간 |
| E3 211.233.72.248 | 인공지능융합대학 위젯(위 절), 참빛인재대학 kwlife:notice, 금융부동산법무학과 kwlaw:notice, 게임콘텐츠학과 kwgame:notice, 스포츠상담재활학과 kwsports:notice, 인제니움학부대학 ingenium:notice, 자율전공학부 liberal:notice, 반도체특성화대학사업단 semi:notice, HUSS 사업단 huss:notice, 대학혁신사업단 inno:notice | 위젯 40분, 게시판 6시간 |
| E4 114.203.87.122 | 화학공학과 chemng, 스포츠융합과학과 sports (B형) | 40분 |

- 1페이지의 고정 글도 읽고(경영대학은 85행 중 65행이 고정), 새 글은 위치가 아니라 글 번호(UID·no)와 등록일로 판단합니다. 처음 보는 글이 지난번 1페이지의 가장 큰 번호보다 크거나, 등록일이 최근 14일 안이면 새 글입니다. 번호가 작고 오래된 글(1페이지로 밀려 올라온 글, 120일이 지나 다시 보이는 글)은 알림 없이 저장합니다. 그래서 게시판 번호가 다시 매겨지거나 한 번 파싱에 실패한 글도 놓치지 않습니다. 1페이지의 가장 큰 번호가 내려가면(최신 글 삭제, 번호 재설정) `dept_uid_regressed` 로그와 UidRegressed 지표를 남기고 그 페이지를 새 기준으로 씁니다. 같은 번호의 글이 제목과 등록일만 바꿔 다시 올라오면(새 학기 [필독]) 수정으로 저장하고 등록일과 ttl도 새로 씁니다. 경영대학처럼 번호에 쉼표가 있는 행("2,157")도 일반 글로 읽습니다.
- 한 게시판이 빈 목록을 주면 그 게시판만 건너뛰고, 한 바퀴(그 서버의 게시판 수만큼, E3는 위젯까지) 내내 빈 목록이면 서버를 쉬게 합니다. 게시판 하나의 404·리디렉트·바뀐 화면은 서버를 쉬게 하지 않고 그 게시판만 2시간 → 4시간 → … 최대 24시간 쉽니다.
- 게시판을 더해도 요청 수는 그대로입니다(서버마다 실행당 1회, 하루 31회 + 여유 1회 = 32회). 늘어나는 것은 게시판마다 다시 읽는 간격뿐이고, 공지 본문 읽기(extract.py)의 몫도 그대로 하루 상한에서 남은 실행 수를 뺀 만큼입니다.
- kwsports는 2024년 글 3개뿐이고 kwgame도 글이 5개라 거의 바뀌지 않습니다(E3 차례를 줄이려면 이 둘부터 뺄 만합니다).
- 넣지 않은 곳: 건축공학과 자료실(마지막 글이 2025-03 졸업논문 합본이고 2026-10 글은 제목 없는 광고), 지능형로봇학과(share.kw.ac.kr, `table.tbl-noti`라 지금 읽기 방식에 맞지 않음), 경영대학 첫 화면 위젯(이미 읽는 biz 공지 게시판 글로 연결되고 목록 모양이 다름), 현장실습(robots.txt가 `/index.do`만 허용하고 그 첫 화면은 다른 모양), 산학협력단(`type=view&uid` 게시판), 경력개발팀 채용정보(robots.txt가 `/recommend/`를 막음), 광운대신문 RSS(Wix 피드가 아님).
- 저장: 게시판마다 `SRC#dept-<키>`(예: `SRC#dept-ee`). 글 번호는 `dept-ee-<UID>`, 분류와 부서는 학과·단과대 이름입니다(별칭 "전정대", "인사대", "경영대", "참빛", "인제니움"). 첫 수집은 기준점만 저장합니다(알림 없음).
- 알림: 분류 `dept-<키>` → 채널 키 `department`(없으면 학교-공지). 구독 역할은 단과대 단위입니다(`routing.COLLEGE_OF`, 예: `경영대학 알림`).
- 학교 부서·사업단 게시판(`office=True`)은 분류가 `office-<키>`이고 학과로 치지 않습니다. 채널은 `routing.CATEGORY_TO_CHANNEL`: 경력개발팀 → 취업, 국제처 유학생 → 국제, 반도체특성화대학사업단·HUSS 사업단·일반대학원 → 학사, 한국어교육센터·인권센터·대학혁신사업단·창업지원단 → 학교-공지(일반). 이 게시판들은 긴급 역할을 울리지 않습니다.
- 게시판이 따로 없는 학부(정보융합학부·컴퓨터정보공학부·소프트웨어학부·로봇학부·경영학부·국제통상학부)를 분류로 주면 단과대 게시판에서 그 학부 글과 단과대 전체 글만 돌려줍니다(`kw_mcp.combined.COLLEGE_OF_DEPARTMENT`). 같은 글이 단과대 전체와 그 학부로 함께 올라오면 학부 쪽 글을 돌려줍니다.

## 학과 블로그 피드 (Wix)

- 건축학과 `https://www.kwuarchitecture.com/blog-feed.xml`, 미디어커뮤니케이션학부 `https://www.kwmedia.info/blog-feed.xml`. 목록은 `src/kwcampus/wixfeeds.py`의 `FEEDS`에 있습니다. 환경공학과(www.envkw.com)는 `/blog-feed.xml`이 404라 넣지 않았습니다.
- Wix CDN 주소는 모든 Wix 사이트가 같이 쓰므로 여기서는 호스트마다 요청 간격·하루 상한(7회: 2시간마다 5회 + 21시 1회 + 여유 1회)·쿨다운을 둡니다. 피드마다 2시간에 한 번, 실행마다 호스트당 1회 읽습니다. robots.txt는 `*?lightbox=`만 막고 피드는 허용합니다(2026-11-06 확인).
- 피드에는 최신 20개가 오래된 글(2021년)까지 섞여 있어서, 새 글은 pubDate로 판단합니다. 전에 본 가장 늦은 pubDate보다 늦으면 새 글이고, 그보다 이른 글은 알림 없이 저장합니다. pubDate가 하루 넘게 미래인 글(작성자가 날짜를 잘못 넣은 경우)은 기준점을 내일까지만 밀고 `wix_future_date` 로그를 남깁니다. 그래서 그 날짜까지 진짜 새 글이 묻히지 않습니다.
- 저장: `SRC#dept-architecture`, `SRC#dept-media`. 글 번호는 `dept-media-<Wix 글 ID>`, 분류와 부서는 "건축학과", "미디어커뮤니케이션학부"(별칭 "미컴"). 알림은 채널 키 `department`. 미디어커뮤니케이션학부의 실제 공지 목록(notices-2026)은 이 피드에 일부만 나옵니다.

## 기숙사 식단 (넣지 않음)

`kw.happydorm.or.kr`의 `/food/indexFoodList.do`(`locgbn=KW` POST)는 2026-11-06에 `{"root":[]}`만 돌려줬습니다. 등록된 식단이 없고, 행복기숙사 첫 화면의 '오늘의식단' 칸도 HTML 주석으로 막혀 있습니다. 데이터가 다시 올라오면 학식과 같은 07:30 작업에 붙이면 됩니다.

## 취업게시판·종합 FAQ (www.kw.ac.kr)

- 메인 공지와 같은 목록 형식이지만 분류 이름이 달라서(`[취업관련]`, 부서 이름) 자체 분류 "취업"(`job`), "FAQ"(`faq`)로 저장합니다. `SRC#kw-job`, `SRC#kw-faq`, 글 번호 `kw-job-<번호>`, `kw-faq-<번호>`.
- www의 요청 간격·하루 44회 상한(`config.WWW_DAILY_CAP`)을 메인 공지와 같이 씁니다. 그래서 취업게시판은 2시간에 한 번, FAQ는 하루에 한 번만 읽고, 한 실행에서 둘 중 하나만 읽습니다. 메인 공지 요청 뒤 실제로 61초를 쉬고 읽습니다(그래서 수집 Lambda 제한 시간은 150초입니다). 평일 하루 www 요청: 메인 31(낮 30 + 21시) + 취업 6 + FAQ 1 + 학식·학사일정 2 이하 = 40 이하.
- 게시판마다 실패 횟수를 따로 셉니다. 메인 공지가 잘 돼도 취업게시판 경로만 계속 403·보안문자를 받으면 그 게시판은 2시간부터 두 배씩 쉬고, 3번 연속이면 그 게시판만 끕니다(NeedsHuman). 404·리디렉트·바뀐 화면은 www를 쉬게 하지 않고 그 게시판만 쉽니다.
- 알림: 취업 → 채널 키 `career`. FAQ는 검색만 되고 알림은 보내지 않습니다.

## 1단계 구성

```
EventBridge Scheduler (평일 09:00~18:59 KST, 20분마다)
  → kw-collector: 메인 공지 1페이지 GET 1회 → 파싱·마스킹 → kw-public (DynamoDB)
  → DynamoDB Streams → kw-dispatcher → SQS kw-discord → kw-discordsender → Discord 채널
```

- **학교 서버 예절**
  - 한 번 실행할 때 서버마다 요청은 1회입니다. 예외는 www뿐입니다: 수집 실행이 2시간에 한 번쯤 61초 뒤에 1회 더 요청하고(취업게시판·FAQ, 한 실행에 둘 중 하나), 평일 07:30 `kw-campus` Lambda가 학식과 학사일정을 61초 간격으로 최대 2회 받습니다. 처리방침 3항(`/privacy`)도 이렇게 적어 두었으니 바꾸면 같이 고칩니다.
  - 같은 서버(origin)에는 60초 간격을 두고(요청하는 그 시각 기준), 하루 44회(학과 게시판 서버는 32회, Wix 피드 호스트는 7회)로 제한합니다. 수집 Lambda는 동시에 하나만 돌아서(reserved concurrency 1) 손으로 돌린 실행이 예약 실행과 겹치지 않습니다.
  - 수집 시각: 평일 09:00~18:40 20분마다와 평일 21:00(KST). 휴강·통제 공지가 전날 저녁에 올라오는 경우가 많아 저녁에 한 번 더 봅니다. 주말은 공지가 없어(첫 309건 중 0건) 보지 않습니다.
  - 강한 차단(403·429·보안문자/챌린지 화면): 그 서버가 30분 → 60분 → 2시간 … 최대 24시간 쉽니다. 429·503의 Retry-After가 더 길면 그 값(최대 7일)을 따릅니다. 3번 연속이면 그 origin을 끄고, 끈 origin을 건너뛸 때마다 OriginDisabled 지표를 남겨 `kw-collector-origin-disabled` 알람이 계속 울립니다. 다시 켜는 건 사람이 합니다: `uv run python scripts/collector_admin.py reenable --origin <서버>`(기본은 보기만, `--apply`로 차단 횟수·쿨다운·꺼짐을 한꺼번에 되돌림).
  - 일시 장애(연결 실패·시간 초과·5xx): 첫 번째는 다음 실행까지만 기다리고, 그다음부터 30분 → 60분 … 최대 6시간 쉽니다. 끄지는 않고, 6번 연속이면 NeedsHuman 알람을 보냅니다. 빈 목록(모르는 차단 화면일 수 있음)은 30분부터 쉽니다.
  - 게시판 하나의 문제(404·리디렉트·예상과 다른 화면·크기 초과)는 서버를 쉬게 하지 않고 그 게시판만 쉽니다(SourceFailed).
  - 보안문자 판정은 목록을 못 읽은 화면에서만 합니다. 정상 목록 화면에 Cloudflare 분석 스크립트나 CDN 주소가 있어도 차단으로 보지 않습니다.
  - 요청 하나는 소켓 단계마다 15초, 본문 전체 30초를 넘기면 끊고(`slow`), Lambda 남은 시간이 요청 하나의 최악 시간보다 적으면 그 서버는 건너뜁니다.
  - robots.txt: 수집하는 호스트마다 2026-11-06에 손으로 한 번씩 확인하고 결과를 `politeness.ROBOTS`에 적었습니다. 기록이 없는 호스트와 Disallow 경로는 요청하지 않습니다(NeedsHuman). 지금 수집하는 경로는 모두 허용입니다. 같은 E1 서버의 job.kw.ac.kr은 `/recommend/`, `/internship/`, `/job/`, `/notice/news.php` 등을 막아서 수집하지 않습니다.
  - User-Agent: `kw-campus/1.0 (+<bridge>/privacy; student non-commercial notice alerts)`(`config.USER_AGENT`). AWS 공용 IP에서 나가므로 학교 관리자가 무엇을 하는 프로젝트인지와 연락처·수집 거부 방법(처리방침 3항)을 찾을 수 있게 했습니다. 저장소는 비공개라 링크하지 않습니다(관리자가 받는 건 404뿐). 개인 이메일 주소도 넣지 않습니다.
- **저장하는 것**: 제목, 분류, 부서, 날짜, 링크뿐입니다. 제목과 부서는 저장 전에 마스킹합니다(redaction v3, 항목의 `norm_v`=3, 규칙 전체는 `kwnotice/redaction.py` 첫머리):
  - 연락처: 메일(`[at]`·`(at)`·`골뱅이`로 쓴 것 포함), 휴대폰·지역번호·대표번호·국제 전화번호(점·괄호·유니코드 대시로 쓴 것, `02)940-5114`처럼 괄호를 하나만 쓴 것 포함).
  - 번호: 주민등록번호, 학번·사번·수험번호·계좌처럼 이름표가 붙은 번호, 10~14자리 숫자, `2021****56`처럼 일부 가린 학번. 공지 자체의 번호(`공지번호 …`)만 남깁니다.
  - 사람 이름: 님·씨·학생·교수·선생님·조교·동문·박사·석사·학우나 `외 N명` 앞의 이름(사이에 소속 괄호가 있거나 쉼표로 늘어놓은 이름도), `외`·상·합격 앞의 전체 이름, 가린 번호 옆의 이름, 합격자·명단·선발 결과·수상·장학생·대상자·학위논문·공개발표·심사·졸업생·인터뷰·우승·○○상 같은 명단·수상 제목의 이름(`대상` 한 단어는 뒤에 수상·받·차지·영예가 오거나, 대회·공모전 같은 말 바로 뒤이거나, 제목에 다른 상이 있을 때만 상으로 보고, `재학생 대상 …`의 '대상'은 그대로 둠), 부고 제목의 이름, `홍*동`·`김○수`·`김OO` 같은 일부 가린 이름. 이름처럼 보는 말은 흔한 성으로 시작하는 한글 2~4음절이고, 과·팀·센터·학·생 같은 끝이나 제목에 흔한 말이면 이름으로 보지 않습니다(`교수학습센터 안내`, `국가장학금 대상자 안내`는 그대로).
  - 이미 가린 표시는 다시 건드리지 않아서 두 번 돌려도 결과가 같습니다. 규칙이 바뀌어 `norm_v`만 오르는 변경은 조용한 재정규화로 봅니다(아래 '수정 공지').
  - 본문과 첨부는 가져오지 않습니다. 마지막으로 바뀐 날부터 120일이 지나면 자동으로 지웁니다.
- **내리기**: 학교가 글을 지웠거나 삭제 요청이 오면 `uv run python scripts/collector_admin.py takedown <글 번호>`로 확인하고 `--apply`로 Discord 메시지(수정됨 답글 포함), 전달 기록, kw-public 항목을 지웁니다. 항목은 빈 표시(removed)로 남아 다음 수집이 되살리지 않습니다. 전달 기록은 올린 날부터 150일, 공지가 바뀔 때마다 그날부터 다시 150일 보관해(공지보다 30일 더, `discord.RECORD_GRACE`) 공지가 남아 있는 동안은 메시지를 찾을 수 있습니다. 메시지에 보이지 않는 변경(고정·학과)이나 알림이 꺼져 있을 때의 변경도 dispatcher가 keep 작업으로 기록 보관 기간만 늘립니다.
- **첫 수집**: 알림 없이 기준점으로만 저장합니다. 그 뒤 새 글만 알립니다. 오래된 고정 글은 알리지 않습니다.
- **채널 배정**

| 채널 | 분류 |
|---|---|
| 학교-공지 | 일반, 학생, 봉사, 입학, 시설, 병무 |
| 학사 | 학사 |
| 장학 | 등록/장학 |
| 취업 | 외부, 취업(취업게시판) |
| 학과-공지 | 인공지능융합대학, 학과·단과대 게시판, 학과 블로그 피드 (`department` 채널 ID가 없으면 학교-공지) |
| 국제 | 국제교류, 국제학생 |

## Discord 알림: 수정 공지·구독 역할·읽기 전용 채널

- **확인 시각**: 수집은 평일 09:00~18:40에 20분마다, 그리고 평일 21:00에 돕니다. 밤과 주말에 올라온 공지는 다음 확인 때 올라갑니다. 그래서 22:00~08:00 무음 규칙은 지금 일정에서는 쓰이지 않고, SQS 재시도가 늦게 도착했을 때만 의미가 있습니다. #알림-설정의 안내 문구(`interactions.MENU_CONTENT`)도 이 시각을 적어 두었으니 일정을 바꾸면 같이 고칩니다.
- **수정 공지**: 이미 알린 공지의 제목·수정일·분류가 바뀌면 원래 메시지를 고칩니다. 메시지는 처음 올린 채널에서 찾습니다(분류가 바뀌어 채널이 달라져도). 학교가 제목이나 수정일을 바꿨고 올린 지 30분이 넘었으면 `✏️ 수정됨 · <수정일>` 답글을 링크와 함께 답니다. 수정일이 없는 게시판은 바뀐 걸 본 날짜를 씁니다. 답글은 공지 하나에 한국 시간 하루 한 번까지이고, `kw-delivery`의 `reply_day`로 셉니다.
  - 조용히 고치는 경우(답글 없음): 분류만 바뀜, 또는 우리 쪽 가림·정규화 규칙(`norm_v`)이 바뀌고 학교 수정일은 그대로.
  - 재게시: 학과 게시판이 같은 글 번호에 새 제목과 30일 넘게 뒤의 등록일을 붙이면 새 글로 봅니다. 전에 알린 적이 있으면 그 메시지를 고치고, 없으면 새로 올립니다.
  - 누가 원래 메시지를 지웠으면 다시 만들지 않습니다. 같은 작업이 SQS에서 두 번 와도 한 번만 올리고(90초 안의 다른 시도는 건드리지 않고, 메시지마다 nonce), 답글을 올린 뒤 응답을 못 받고 죽은 시도는 채널의 최근 메시지에서 그 답글을 찾아 다시 올리지 않습니다. Discord가 429를 주면 그 배치의 나머지는 SQS가 다시 줄 때까지 보내지 않습니다.
- **구독 역할**: 주제마다 역할 하나(`긴급 알림`, `학사 알림`, `장학 알림`, `취업 알림`, `국제 알림`, `일반 공지 알림`, 단과대 9개, 목록과 이름은 `topics.TOPICS` 한 곳). #알림-설정의 버튼을 누르면 그 역할이 켜지거나 꺼지고, 누른 사람에게만 보이는 답이 옵니다. 새 공지는 자기 주제 역할만 멘션합니다(`allowed_mentions`에 그 역할만, @everyone·@here 없음). 같은 역할은 30분에 한 번만 멘션합니다(학과 사이트 다섯 곳에 같은 취업박람회 글이 올라오는 경우). 공지를 고쳐도 다시 울리지 않습니다.
- **긴급**: 학교 전체 공지(메인 게시판, 취업게시판)의 제목에 휴강·결강·휴교·강의실/시험 장소 변경·비대면 전환·출입 통제·폐쇄·정전·단수·시스템 장애·운행 중단 같은 말이 있으면(`routing.is_urgent`) `긴급 알림`도 멘션합니다. 긴급은 30분 제한 없이 매번 울립니다. 학과 게시판의 과목 휴강·강의실 변경은 그 단과대 역할만 멘션합니다. 모집·신청·제출·계획서·교원용·연휴·개교기념일·정기점검이 든 제목과 출입국·장애인·내부통제·출입통제시스템 같은 비슷한 말은 뺍니다.
- **이름이 들어갈 수 있는 제목**: 제목의 연락처·학번·사람 이름은 저장 전에 가리지만(redaction v3) 모든 이름을 잡는 규칙은 없습니다. 그래서 학과 게시판·피드의 수상·명단·합격·부고·조의·당선·선발 결과·축하·학위논문·공개발표·심사·졸업생·인터뷰·입상·우승·○○상 글과, 어느 게시판이든 명단·수상·합격 제목에 아직 전체 이름처럼 보이는 말이 남은 제목(`redaction.may_name_someone`), `홍*동`·`김○○`처럼 가린 이름이 보이는 제목은 Discord에 `[분류] 새 글 (제목은 링크에서 확인)`과 링크만, 멘션 없이 올립니다(`routing.name_risk`).
- **버튼 처리**: `kw-discordinteractions` Lambda(`POST /discord/interactions`, API `kw-discord`). Discord 서명(Ed25519)을 확인하고, 버튼 ID에 맞는 역할을 `/kw/discord/roles`에서만 찾아 그 역할 하나만 붙이거나 뗍니다. 요청에 든 역할 ID는 쓰지 않습니다. 서명 시각이 60초보다 오래되었거나 숫자 12자리를 넘는 요청은 거절합니다. 이 함수는 SSM 세 값(`bot-token`, `public-key`, `roles`)을 한 번에 읽고 테이블 권한은 없습니다. 아직 역할이 없는 주제 버튼은 역할 목록을 한 번 더 읽어 보고, 그래도 없으면 "준비 중"이라고 답합니다. 역할을 못 바꾼 클릭이 한 시간에 3번이면 `kw-discord-buttons-failed` 알람이 옵니다(봇 역할이 구독 역할 아래로 내려갔을 때, setup 순서가 틀렸을 때 등). 공개키가 없다는 결과는 캐시하지 않아서, 넣은 즉시 포털 저장이 됩니다.
- **읽기 전용 채널**: 공지 채널, #알림-설정, #학과-공지는 @everyone에게 기록 보기를 허용하고 메시지·스레드 만들기·스레드 메시지를 막습니다. 보기 권한은 건드리지 않아서 숨긴 채널·분류는 숨긴 채로 남습니다. 봇에는 그 채널에서 보기·보내기·링크 첨부·기록 보기와 역할 멘션만 줍니다.

### 서버 설정 (`scripts/discord_setup.py`)

인자 없이 돌리면 계획만 보여 주고 아무것도 바꾸지 않습니다. `--apply`를 붙여야 실제로 만듭니다. 몇 번을 돌려도 없는 것만 만들고 다른 것만 고칩니다.
- 채널에 원래 있던 다른 권한 설정은 그대로 둡니다. 새로 만드는 채널은 #학교-공지가 있는 분류의 권한을 물려받고, 같은 이름의 채널은 그 분류 안에 있을 때만 씁니다.
- 손으로 넣은 `department` 채널도 플래그 없이 늘 읽기 전용·봇 멘션 허용을 맞춥니다. 서버에 없는 ID면 경고합니다.
- 멈추는 경우(아무것도 바꾸지 않음): 봇에 관리자 권한이 있을 때(exit 2), 같은 이름의 기존 역할에 권한이 있거나 연동 역할이거나 봇 역할보다 위에 있을 때(exit 1).
- 고치는 경우: 기존 구독 역할이 '누구나 멘션 가능'이면 끄고, `topics.TOPICS`에서 이름이 바뀌었으면(2028년 단과대 개편 등) 이름만 바꿉니다. #알림-설정의 채널 주제 문구가 다르면 고칩니다. #학과-공지의 주제는 `--department-channel`을 붙여 돌릴 때만 고칩니다(플래그 없이는 권한만 맞춤).
- 알려 주는 것: 봇 역할의 위험한 권한(추방·차단·서버 관리·메시지 관리·웹후크 등, 이름 없는 비트는 16진수), @everyone의 '모든 역할 멘션', 서버 기본 알림이 '모든 메시지'인지.
- 역할 목록(`/kw/discord/roles`)은 버튼 메시지를 고치기 전에 씁니다. SSM 쓰기가 실패하면 그대로 실행할 `aws ssm put-parameter` 명령을 보여 주고 exit 1로 끝납니다(값에 비밀은 없음).
- 실행하는 AWS 자격 증명에는 `/kw/discord/bot-token` 읽기(GetParameter, 복호화)와 `/kw/discord/roles`·`/kw/discord/channels` 쓰기(PutParameter)가 둘 다 있어야 합니다. 아래 '배포 권한'의 두 권한 세트 중 하나만으로는 끝까지 못 돕니다. 루트나 두 권한을 다 가진 프로필로 돌리거나, 쓰기 단계에서 출력된 명령을 쓰기 권한 프로필로 실행합니다.

```bash
uv run --with 'botocore[crt]' python scripts/discord_setup.py --department-channel           # 계획만
uv run --with 'botocore[crt]' python scripts/discord_setup.py --department-channel --apply   # 적용
```

`--apply`가 하는 일: 구독 역할 만들기(권한 없음, 멤버는 멘션 불가) → #알림-설정 만들기(`--department-channel`이면 #학과-공지도) → 채널 권한 설정 → #운영 권한 맞추기(아래 '운영') → SSM `/kw/discord/roles` 쓰기 → 버튼 메시지 올리기/고치기(새 메시지면 그 ID를 다시 씀) → `--department-channel`이면 `/kw/discord/channels`에 `department` 추가, #운영을 찾았으면 `ops` 추가 → `/제보`·`/설치`와 운영자용 `/초대회수`·`/초대현황` 등록. `--region`의 기본값은 `ap-northeast-2`입니다.

처음 켤 때 순서:
1. `cdk deploy` 뒤 출력 `DiscordInteractionsUrl`을 확인합니다.
2. 공개키를 SSM에 넣습니다. Discord 개발자 포털 → 앱 → General Information → Public Key를 복사한 뒤 터미널에서 직접 실행합니다.
   ```bash
   aws ssm put-parameter --region ap-northeast-2 --name /kw/discord/public-key --type String --overwrite --value "$(pbpaste)" && pbcopy < /dev/null
   ```
3. 개발자 포털 → General Information → Interactions Endpoint URL에 1번 주소를 넣고 저장합니다. Discord가 바로 서명 확인 요청을 보내므로 2번이 먼저 되어 있어야 저장됩니다. 버튼 메시지보다 먼저 해 두어야 처음 누른 버튼이 실패하지 않습니다.
4. 서버 설정 → 역할 → 봇 역할(KW Campus)에 `역할 관리`와 `채널 관리`를 켭니다. 봇 역할이 구독 역할보다 위에 있어야 합니다(새로 만드는 역할은 맨 아래에 생김). 관리자 권한은 주지 않습니다.
5. 계획을 보고 `--apply`로 적용합니다.
6. 적용이 끝나면 봇 역할에서 `채널 관리`를 끕니다. 봇 역할에 남길 권한은 채널 보기·메시지 보내기·링크 첨부·메시지 기록 보기·역할 관리(권한 값 268520448)입니다.

`topics.TOPICS`에 주제를 더하거나 키를 바꾼 뒤에는 `cdk deploy`를 먼저 하고 `--apply`를 돌립니다. 순서가 바뀌면 새 버튼 메시지가 먼저 올라가고, 아직 예전 목록을 쓰는 `kw-discordinteractions`는 그 버튼에 "이 버튼은 이제 쓰지 않습니다"라고만 답합니다. 이 답은 `InteractionFailed`에 세지 않아 `kw-discord-buttons-failed` 알람도 울리지 않습니다.

서버 설정 → 개요 → 기본 알림 설정은 '@멘션만'으로 둡니다. '모든 메시지'면 멤버가 공지와 수정 답글마다 알림을 받아 구독 역할이 의미가 없습니다. 학사·장학·긴급을 기본으로 켜 두려면 봇이 붙이지 말고 Discord 온보딩 질문에서 미리 골라 둡니다.

@everyone 역할에 `@everyone, @here, 모든 역할 멘션하기`가 켜져 있으면 멤버 누구나 구독 역할을 멘션해 구독자 전원을 부를 수 있습니다. 끄는 것을 권합니다. 봇은 공지 채널에서 따로 허용받아 영향이 없습니다. 끄는 건 `--apply` 뒤에 합니다. 봇은 자기에게 있는 권한만 채널에 줄 수 있어서, 먼저 끄면 공지 채널의 봇 멘션 허용을 설정하지 못합니다.

## 운영

운영 분류 안의 비공개 채널 #운영에 두 가지가 올라옵니다. `discord_setup.py --apply`가 이 채널을 이름으로 찾아 봇에 보기·보내기·링크 첨부·기록 보기를 주고, @everyone에게는 숨긴 채로 두고, ID를 `/kw/discord/channels`의 `ops`에 씁니다. 채널을 만들지는 않습니다. 두 함수 모두 Discord 큐에 `ops` 작업을 넣고 sender가 #운영에만 올립니다(멘션 없음).

- **아침 점검** (`kw-opsdigest`, 평일 08:40): 전날(월요일에는 금~일)을 숫자로만 보여 줍니다. 공지 제목·멤버·초대 코드는 넣지 않고, 이름은 게시판·서버·알람 이름만 씁니다.
  - 괜찮으면 한 줄: `✅ 어제 이상 없음 · 새 공지 12 · 초대 3`
  - 아니면 `⚠️ 어제 확인할 게 있어요` 아래에 게시판(꺼짐·잠시 멈춤·한동안 새 글 없음·새 공지), 공지 읽기(완료·실패·한도로 미룸), 디스코드(대기·실패·재시도), 초대(그 기간·전체), 켜진 `kw-*` 알람. 못 읽은 항목은 `확인 못 함`.
  - '한동안 새 글 없음'은 메인 게시판 3일, 취업게시판 7일, 나머지 21일 기준이고 FAQ는 보지 않습니다.
- **규정 변경 감시** (`kw-rulewatch`, 월요일 09:00): 학칙·학칙 시행세칙·다전공 내규 2개·조기졸업 규정은 rule.kw.ac.kr의 최신 `SEQ_HISTORY`로, 학사안내·장학 페이지 9개(bachelor_info01·03·07·08·11·12, summary, scholarship01·02)는 본문 해시로 봅니다. 바뀌면 `📘 학칙이 바뀌었어요 (2026.10.28 → 2026.11.20) · <링크> · 지식베이스 다시 만들기 필요`가 올라옵니다.
  - 처음 본 페이지는 기록만 합니다(kw-public `RULEWATCH#<id>`). 못 읽은 페이지는 그대로 두고 `kw-collector-needs-human`이 울립니다.
  - 수집기와 같은 서버별 게이트, robots 기록, `/kw/collector/enabled` 스위치를 씁니다. www는 그날 목록 수집에 쓰고 남는 몫만 써서, 못 본 페이지는 다음 주에 먼저 봅니다.
- **운영자 명령**: 서버 관리 권한이 있는 사람에게만 보이고, 처리할 때도 권한을 다시 확인합니다. 어느 채널에서나 쓸 수 있고 답은 쓴 사람에게만 보입니다.
  - `/초대회수 member:@멤버`: 그 멤버가 디스코드에서 받은 초대 코드를 모두 회수합니다. 그 코드로 연결한 PC와 커넥터는 1분 안에 끊깁니다. 멤버의 한도(3개)는 쓴 채로 남아 새 코드를 받지 못합니다.
  - `/초대현황`: 오늘·전체 초대 수와 한도(한 사람당 3개, 하루 50개).

### 초대를 DynamoDB 한 곳으로 옮기기

손으로 만든 초대(SSM `/kw/mcp/tokens`)와 디스코드에서 받은 초대(kw-bridge `INVITE#`)를 kw-bridge 하나로 모읍니다.
1. `uv run python scripts/invites_admin.py migrate`로 계정·테이블 ARN과 숫자를 보고 `--apply --expect-account <계정ID>`로 옮깁니다. 목록의 해시마다 `INVITE#<hash>`(issued_to·source `manual`)가 생기고, 그 초대의 PC 수(`devices`)는 그대로입니다. 다시 돌려도 바뀌는 것이 없습니다. 전체 초대 수(`DINVITES#total`)도 이때 맞춥니다. SSM 목록이 비었거나 없으면 아무것도 쓰지 않고 멈춥니다.
2. `cdk deploy -c invitesSsm=0`으로 배포합니다. Bridge·PublicMcp에 `KW_INVITES_SSM=0`이 붙어 SSM 목록을 읽지 않고, `/kw/mcp/tokens` 읽기 권한도 빠집니다. 되돌리려면 플래그 없이 다시 배포합니다. 이 배포 전까지는 옮긴 초대도 SSM 목록에 있어야 살아 있어서, 목록에서 해시를 빼면 예전처럼 회수됩니다.
3. 일주일쯤 문제가 없으면 소유자 확인을 받고 `aws ssm delete-parameter --region ap-northeast-2 --name /kw/mcp/tokens`로 지운 뒤, `infra/cdk.json`의 context에 `"invitesSsm": "0"`을 넣어 둡니다. **목록을 지운 뒤 플래그 없이 배포하면 손으로 만든 초대가 모두 끊깁니다.**

그 뒤 손으로 만든 초대 하나를 회수할 때는 `scripts/invites_admin.py revoke --hash <sha256>`(계획만) → `--apply --expect-account <계정ID>`를 씁니다. 2번 배포 전이라면 SSM 목록에서도 빼야 끊깁니다(명령이 알려 줌).

## 스위치와 비밀값 (SSM, CDK가 만들지 않음)

| 파라미터 | 의미 |
|---|---|
| `/kw/collector/enabled` | `true`일 때만 수집. 없으면 꺼짐 |
| `/kw/collector/notify` | `true`일 때만 Discord 전송. 없으면 꺼짐 |
| `/kw/discord/bot-token` | 봇 토큰 (SecureString). 본인이 직접 넣음 |
| `/kw/discord/channels` | `{"notice": "<채널ID>", "academic": ..., "scholarship": ..., "career": ..., "international": ..., "department": ..., "ops": ...}` (`department`는 선택, `ops`는 `discord_setup.py`가 #운영을 찾아 씀) |
| `/kw/discord/public-key` | Discord 앱의 공개키(Ed25519, 16진수). 버튼 요청 서명 확인용. 본인이 직접 넣음 |
| `/kw/discord/roles` | `scripts/discord_setup.py`가 씀: `{"guild": ..., "roles": {주제: 역할ID}, "menu": {"channel", "message"}}` |
| `/kw/mcp/tokens` | 초대·공개 MCP 토큰의 SHA-256 해시 목록 (JSON 배열). `invitesSsm=0`으로 배포한 뒤로는 읽지 않음(위 '초대를 DynamoDB 한 곳으로 옮기기') |
| `/kw/bridge/jwt-secret` | 액세스 토큰 서명 키 (SecureString) |

bridge 비밀값은 값이 화면에 나오지 않게 한 번만 만듭니다.

```bash
aws ssm put-parameter --name /kw/bridge/jwt-secret --type SecureString --value "$(openssl rand -base64 48)"
```

봇 토큰은 Discord 개발자 포털에서 [Reset Token]을 눌러 복사한 뒤, 아래 명령을 터미널에서 직접 실행해 넣습니다. 토큰이 화면이나 셸 기록에 남지 않습니다.

```bash
aws ssm put-parameter --region ap-northeast-2 --name /kw/discord/bot-token --type SecureString --overwrite --value "$(pbpaste)" && pbcopy < /dev/null
```

## 개발

```bash
uv sync
uv run pytest -q
./scripts/build_lambda.sh          # build/lambda (arm64, Python 3.13). ../kw-mcp/dist/release(서명된 릴리스)가 있어야 함
cd infra && npm install && ./node_modules/.bin/cdk synth
```

## 배포

bridge Lambda에는 kw-mcp의 서명된 릴리스가 그대로 들어갑니다(KLAS 로그인 코드를 PC 에이전트와 같이 씀). 서명은 여기서 하지 않습니다.

1. kw-mcp에서 `scripts/release.sh`를 돌립니다. 작업 트리가 깨끗하고 HEAD에 `v<version>` 태그가 있어야 합니다. 테스트와 설치 확인을 거친 wheel, 의존성 고정 파일, Claude 데스크톱 확장(`kw-campus.mcpb`)을 이 Mac 키체인의 릴리스 키로 서명해 `latest.json`과 함께 `dist/release/`에 둡니다. `--dry-run`은 버리는 키로 서명한 연습본(`dist/release-dry-run/`)이라 배포할 수 없습니다.
2. 여기서 `./scripts/build_lambda.sh`를 돌립니다. `../kw-mcp/dist/release`가 없거나 `kw_mcp.release check`(서명·해시 확인)를 통과하지 못하면 멈춥니다. 통과하면 그 wheel을 Lambda에 깔고, 같은 파일들(확장, wheel, 의존성 고정 파일, `latest.json`)을 `/dist/...`로 내려 주도록 복사합니다. kw-mcp가 다른 곳에 있으면 `KW_MCP_DIR`로 알려 줍니다.
3. 아래처럼 `cdk deploy`로 배포합니다.

PC 에이전트는 `latest.json`을 보고 스스로 업데이트하므로, 에이전트를 바꿨다면 kw-mcp의 version을 꼭 올려 새로 릴리스하세요(같은 버전이면 아무도 받지 않음).

**데이터만 바꿀 때** (학사 안내 지식베이스, 졸업·다전공 규칙): kw-mcp에서 `scripts/release.sh data` → 여기서 `./scripts/build_lambda.sh` → `cdk deploy`. `build_lambda.sh`는 `../kw-mcp/dist/data`가 있으면 `kw_mcp.release check-data`(서명·해시)를 통과해야 번들에 넣고, 없으면 전처럼 빌드합니다. 연결 서버는 `/dist/data/`에서 `data.json`과 거기 적힌 파일만 내려 주고, 자기 `school_guide`·`department_curriculum`도 이 자료를 씁니다(`run.sh`가 `KW_DATA_DIR` 설정).

```bash
cd infra
./node_modules/.bin/cdk bootstrap aws://<계정ID>/ap-northeast-2   # 처음 한 번
./node_modules/.bin/cdk deploy        # 알람 메일은 ~/.config/kw-campus/alarm-email 첫 줄(또는 --context alarmEmail=...)
```

알람 메일을 빼고 배포하면 SNS 구독이 지워집니다. 처음 구독하면 AWS가 확인 메일을 보내고, 그 링크를 눌러야 알람이 옵니다.

### 1.0.0 배포 순서와 사전 점검

1.0.0을 처음 올릴 때 한 번 합니다. 1.0 에이전트와 확장은 bridge의 `?source` 필터와 `department` 요청을 쓰고, bridge는 kw-mcp 릴리스를 그대로 싣기 때문에 순서가 중요합니다.

사전 점검:
1. **AWS 계정**: root로 배포한다면 root에 MFA가 켜져 있는지 확인합니다(콘솔 오른쪽 위 계정 이름 → 보안 자격 증명). 더 나은 방법은 아래 '배포 권한'의 배포용 역할로 옮기는 것입니다. 계정을 빼앗기면 `/kw/bridge/jwt-secret`으로 친구들의 PC 에이전트를 거쳐 KLAS 데이터를 읽을 수 있습니다.
2. **Lambda 동시 실행 한도**: 예약 동시 실행 합계(`infra/kw_campus_stack.py`의 `BRIDGE_CONCURRENCY` + `BRIDGE_RESULT_CONCURRENCY` + 수집기 1, 지금 20 + 5 + 1 = 26)를 빼고도 계정에 100이 남아야 배포됩니다. 아래 값이 126 이상인지 봅니다. 모자라면 Service Quotas → AWS Lambda → Concurrent executions를 늘려 승인된 뒤 배포합니다.
   ```bash
   aws lambda get-account-settings --region ap-northeast-2 --query AccountLimit.ConcurrentExecutions
   ```
3. **1.0 전에 등록한 PC**: `invite_hash`가 생기기 전에 등록한 기기(운영자 PC 등)는 초대를 회수해도 끊기지 않고, 그 기기의 예전 갱신 토큰(`REFRESH#`)은 `kw-mcp connections`에 보이지 않습니다. 아래 명령으로 찾아 각 PC의 `kw-mcp status`에 나오는 기기 ID와 맞춰 봅니다. 배포가 끝난 뒤 그 PC에 1.0을 설치하고 `kw-mcp unlink` → `kw-mcp link <초대코드>`로 다시 연결합니다(커넥터는 새 연결 코드로 다시 승인). 더 쓰지 않는 PC면 `kw-mcp unlink`만 합니다. unlink하면 서버의 기기 기록이 지워지므로 남은 `REFRESH#` 토큰도 더는 쓰이지 않습니다.
   ```bash
   aws dynamodb scan --region ap-northeast-2 --table-name kw-bridge \
     --filter-expression 'SK = :m AND begins_with(PK, :d) AND attribute_not_exists(invite_hash)' \
     --expression-attribute-values '{":m": {"S": "META"}, ":d": {"S": "DEV#"}}' --projection-expression 'PK, created_at, last_seen'
   ```
4. **릴리스 키 백업**: 서명 키는 이 Mac 키체인(서비스 `kw-campus-release`, 계정 `signing-key`)에만 있습니다. 잃으면 이미 설치된 에이전트에 새 버전을 보낼 수 없습니다(키를 바꾸면 모든 PC에서 다시 설치해야 함). 암호화한 사본을 오프라인 저장소(USB 등)에 둡니다. 키는 화면에 나오지 않고 바로 암호화되며, openssl이 터미널에서 암호를 묻습니다. 그 암호는 키와 따로(비밀번호 관리자 등) 보관합니다.
   ```bash
   security find-generic-password -s kw-campus-release -a signing-key -w \
     | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -out /Volumes/<USB>/kw-release-key.enc
   # 되살리기 (kw-mcp 폴더에서)
   openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -in /Volumes/<USB>/kw-release-key.enc \
     | uv run python -c "import keyring, sys; keyring.set_password('kw-campus-release', 'signing-key', sys.stdin.read().strip())"
   ```

배포 순서:
1. kw-mcp: `pyproject.toml`과 `manifest.json`의 version을 `1.0.0`으로 올리고 `uv lock`을 돌립니다.
2. kw-mcp: 커밋하고 `git tag v1.0.0`.
3. kw-mcp: `scripts/release.sh` (위 1번). `dist/release/`에 서명된 1.0.0이 생깁니다.
4. kw-campus: `uv lock`으로 kw-mcp 1.0.0을 다시 읽고 커밋합니다. 안 하면 `build_lambda.sh`의 `uv export --locked`가 멈춥니다. Lambda는 kw-mcp의 의존성을 이 잠금 파일에서 받으므로 `git diff uv.lock`으로 다른 버전이 바뀌었는지도 봅니다.
5. kw-campus: `./scripts/build_lambda.sh`. 릴리스 버전이 kw-mcp 폴더의 version과 다르거나, wheel의 `kw_mcp/*.py`가 `src/kw_mcp/*.py`와 한 바이트라도 다르면 멈춥니다(테스트는 kw-mcp 폴더의 코드로 돌았으니 Lambda에도 같은 코드가 들어가야 함).
6. `cdk deploy`.
7. 배포가 끝난 뒤에 초대 링크를 보냅니다. 이미 설치된 에이전트는 `latest.json`을 보고 스스로 1.0.0으로 올라갑니다.

### 배포 권한 (root 대신)

지금은 root 로그인으로 배포하고 있습니다. 바꾸는 순서는 이렇습니다(계정 보안 설정이라 직접 합니다).
1. AWS 콘솔 → IAM Identity Center → 사용 설정(서울 리전) → 사용자 하나 만들기.
2. 권한 세트를 만들고 인라인 정책으로 `sts:AssumeRole`을 `arn:aws:iam::<계정ID>:role/cdk-hnb659fds-*`에만 허용, 여기에 `ReadOnlyAccess`를 붙임. CDK는 bootstrap 때 만든 이 역할들로 배포하므로 사용자 자체에는 넓은 권한이 필요 없습니다.
3. SSM 비밀값을 넣을 때만 쓰는 두 번째 권한 세트: `ssm:PutParameter`를 `arn:aws:ssm:ap-northeast-2:<계정ID>:parameter/kw/*`에만 허용.
4. 계정에 할당 → `aws configure sso`로 프로필 등록 → `aws sso login --profile kw` → `AWS_PROFILE=kw ./node_modules/.bin/cdk deploy`.
5. 잘 되면 root 액세스 키가 있으면 지우고, root에는 MFA를 켜 둡니다.

배포만 해서는 아무 일도 일어나지 않습니다. 수집을 켜는 순서는 다음과 같습니다.
1. `/kw/collector/enabled=true`로 켜고, 첫 수집(기준점 저장)이 끝났는지 확인합니다.
2. 집 PC의 kw-notice-mcp launchd 수집기를 끕니다. 두 곳이 같은 서버를 동시에 긁지 않게 하려는 것입니다.
3. Discord 채널 ID와 봇 토큰을 넣은 뒤 `/kw/collector/notify=true`로 알림을 켭니다.

## 알람 대응: 스트림 DLQ (`kw-stream-dlq`)

dispatcher가 재시도(3번, 배치 나누기)로도 처리하지 못한 kw-public 스트림 배치는 건너뛰고, `kw-stream-dlq`에는 그 배치를 가리키는 메시지만 남습니다(스트림 ARN, 샤드 ID, 시퀀스 번호 범위. 공지 내용은 없음). 레코드 자체는 DynamoDB 스트림에 24시간만 남습니다. 공지는 kw-public에 그대로 있고 알림만 빠진 상태입니다.

1. 원인(`kw-dispatcher` 로그의 오류, SQS 권한 등)을 먼저 고칩니다. `/kw/collector/notify`가 꺼져 있으면 dispatcher는 Discord로 가는 작업을 만들지 않고(전달 기록의 ttl만 옮기는 keep 작업만 보냄) 성공으로 끝나므로, 다시 보내기 전에 켜져 있는지 봅니다.
2. 24시간 안이면 스트림에서 그 범위를 읽어 dispatcher에 다시 넣습니다. dispatcher가 받아 준 메시지만 큐에서 지웁니다(실패하면 그대로 남음). 이미 올라간 공지는 sender의 전달 기록 때문에 다시 올라가지 않습니다. 한 번에 메시지 10개까지 처리하니, 큐가 빌 때까지 되풀이합니다.
   ```bash
   uv run python - <<'EOF'
   import json
   import boto3

   sqs, streams, lam = (boto3.client(s, region_name="ap-northeast-2") for s in ("sqs", "dynamodbstreams", "lambda"))
   url = sqs.get_queue_url(QueueName="kw-stream-dlq")["QueueUrl"]
   for msg in sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=600).get("Messages", []):
       info = json.loads(msg["Body"])["DDBStreamBatchInfo"]
       end = int(info["endSequenceNumber"])
       it = streams.get_shard_iterator(StreamArn=info["streamArn"], ShardId=info["shardId"],
                                       ShardIteratorType="AT_SEQUENCE_NUMBER",
                                       SequenceNumber=info["startSequenceNumber"])["ShardIterator"]
       records = []
       for _ in range(100):
           page = streams.get_records(ShardIterator=it, Limit=100)
           records += [r for r in page["Records"] if int(r["dynamodb"]["SequenceNumber"]) <= end]
           it = page.get("NextShardIterator")
           if not it or any(int(r["dynamodb"]["SequenceNumber"]) >= end for r in page["Records"]):
               break
       out = lam.invoke(FunctionName="kw-dispatcher", Payload=json.dumps({"Records": records}, default=str))
       if "FunctionError" in out:
           print(info["shardId"], "dispatcher failed again; the message stays in the queue")
           continue
       sqs.delete_message(QueueUrl=url, ReceiptHandle=msg["ReceiptHandle"])
       print(info["shardId"], len(records), "records dispatched again")
   EOF
   ```
3. 24시간이 지났으면(`get-shard-iterator`가 `TrimmedDataAccessException`) 스트림 레코드는 없습니다. 알람 시각 즈음 새로 저장된 공지의 글 번호(`kw-public`의 `first_seen`으로 찾음, `collector_admin.py takedown`과 같은 형식)를 골라 새 글로 다시 보냅니다. dispatcher의 규칙(첫 수집분·오래된 고정 글·FAQ는 빼기, 채널 고르기)을 그대로 쓰고, 이미 올라간 공지는 sender가 건너뜁니다. 놓친 수정 알림은 다시 만들 수 없고, 학교가 그 글을 다시 고치면 그때 반영됩니다. 다 보냈으면 `aws sqs purge-queue --region ap-northeast-2 --queue-url <kw-stream-dlq URL>`로 큐를 비웁니다.
   ```bash
   uv run python - <글 번호> <글 번호> ... <<'EOF'
   import json
   import sys

   import boto3
   from boto3.dynamodb.types import TypeSerializer

   sys.path.insert(0, "scripts")
   from collector_admin import notice_key

   table = boto3.resource("dynamodb", region_name="ap-northeast-2").Table("kw-public")
   records = []
   for post_id in sys.argv[1:]:
       item = table.get_item(Key=notice_key(post_id), ConsistentRead=True).get("Item")
       if item and not item.get("removed"):
           image = {k: TypeSerializer().serialize(v) for k, v in item.items()}
           records.append({"eventName": "INSERT", "dynamodb": {"NewImage": image}})
   out = boto3.client("lambda", region_name="ap-northeast-2").invoke(
       FunctionName="kw-dispatcher", Payload=json.dumps({"Records": records}))
   print(len(records), "notices", "failed" if "FunctionError" in out else "dispatched again")
   EOF
   ```
