# kw-campus

광운대 공개 정보를 AWS(서울)에서 모아 Discord 채널로 알리고, Claude·ChatGPT에서 쓰는 MCP로 제공합니다. 연결 서버(bridge)를 거치면 각자 자기 KLAS 과제·퀴즈·온라인강의·시간표·과목 공지도 함께 물어볼 수 있습니다. 전체 계획은 `../kw-mcp/docs/plan.md`에 있습니다.

## 4단계: 연결 서버 (kw-bridge)

- 커넥터 URL: `https://<bridge-api>.execute-api.ap-northeast-2.amazonaws.com/mcp` (OAuth 2.1 + PKCE, 동적 클라이언트 등록)
- 안내 페이지: `https://<bridge-api>/setup#<초대코드>` — 연결 방법 세 가지를 쉬운 순서로 보여 줍니다.
- 연결 방법
  - **PC 에이전트**: `kw-mcp link`가 등록한 PC가 WebSocket으로 요청을 받아 KLAS에서 읽고, 요청마다 다른 키로 암호화해 올립니다(`../kw-mcp/src/kw_mcp/agent.py`). 승인 페이지에는 에이전트가 낸 1회용 연결 코드를 넣습니다.
  - **간편 연결** (SSM `/kw/bridge/password-mode`가 `true`일 때만, 지금은 꺼 둠): 승인 페이지에서 학번·비밀번호를 받아 봉인한 토큰으로 서버가 KLAS에 로그인합니다(`src/kwcampus/bridge/password.py`). KLAS가 AWS IP 로그인에 2차 인증을 요구해서 쓸 수 없습니다. 끄면 기존 간편 연결도 다음 호출부터 끊깁니다.
- 도구: `this_week`, `today_briefing`, `my_notices`, `my_courses`, `my_assignments`, `my_quizzes`, `my_online_lectures`, `my_team_projects`, `my_course_notices`, `my_deadlines`, `my_assignment_detail`, `my_syllabus`, `my_course_board`, `my_course_post`, `my_attachment`, `my_grades`, `my_attendance`, `my_exams`, `my_class_changes`, `my_graduation` + 공개 도구 5개. 상세 도구 셋은 에이전트에 하나씩 묻는 `call` 요청이라 에이전트 프로토콜 2 이상이 필요합니다.
- `kw-bridge` 테이블에는 기기 공개키, OAuth 클라이언트, 코드·토큰 해시, 연결 ID, 2분짜리 암호문, 비밀번호 거부 표시만 둡니다. 학번·비밀번호·개인 데이터는 없습니다.
- redirect는 Claude·ChatGPT의 콜백 URL 3개(정확히 일치)와 localhost만 받습니다. 다른 사이트로 돌아가는 클라이언트로 승인을 낚는 것을 막기 위해서입니다.
- 남용 방어: 승인 요청 하나에 실패 5번이면 닫습니다. `/device/result`는 서버가 연 요청 자리에만 한 번 쓸 수 있고 300KB까지입니다. 기기 서명은 한 번만 쓰입니다. 에이전트는 설치한 서버와 초대 코드의 서버가 다르면 연결하지 않습니다. 새 앱이 연결 코드로 붙으면 에이전트 로그에 남습니다. API는 전체 초당 10건(순간 20건)으로 제한합니다. IP별 제한이 아니어서 누가 계속 두드리면 모두 느려질 수 있지만, HTTP API에는 WAF를 붙일 수 없어 비용 상한 쪽을 택했습니다.
- 설치 파일도 bridge가 직접 줍니다: `/install.sh`, `/install.ps1`, `/dist/kw_mcp-*.whl`, `/dist/kw-klas.mcpb`. 공개 JSON API(`/c/<토큰>/api/...`)도 같은 주소에서 받습니다.

**초대**: 초대 코드는 `kwc_`로 시작하는 문자열 하나(서버 주소 + 토큰)입니다. 토큰을 새로 만들어 해시를 `/kw/mcp/tokens`에 더한 뒤 코드를 만듭니다.

```bash
uv run python -c "from kw_mcp.agent import make_invite; import sys; print(make_invite(sys.argv[1], sys.argv[2]))" <bridge-url> <토큰>
```

친구에게는 `https://<bridge-api>/setup#<초대코드>` 링크 하나만 보내면 됩니다. 회수하려면 그 토큰의 해시를 목록에서 지웁니다. 이미 연결된 간편 연결은 토큰이 만료될 때까지 남으므로, 바로 끊으려면 `/kw/bridge/seal-key`를 바꿉니다(모든 간편 연결이 끊겨 다시 연결해야 함).

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
- 수집: 메인 게시판과 같은 예약 실행(평일 09:00~18:40 20분마다와 21:00)에서 E3 학과 게시판과 번갈아 요청합니다. 차례는 시각이 아니라 이 서버에 마지막으로 요청한 쪽(`last_by`)으로 정하므로 21:00 실행도 번갈아 갑니다. 게시판이 차례를 못 쓰면 위젯은 80분 뒤에 다시 갑니다. ce/cs/ic/cni는 이 호스트의 별칭이고 kwlife·kwlaw·kwgame·kwsports·ingenium도 같은 서버(E3, 211.233.72.248)에 있어서, 요청 간격·하루 상한(32회)·쿨다운은 이 서버 하나 기준이고 학과 게시판과 같이 씁니다. 예전 `ORIGIN#aicon.kw.ac.kr` 상태(쿨다운·차단 횟수·오늘 횟수)는 첫 실행 때 `ORIGIN#211.233.72.248`로 옮기고 지웁니다.
- 실패: 서버 신호(403·429·보안문자 화면·연결 실패·5xx)만 E3에 기록합니다. 위젯 주소의 404·리디렉트·바뀐 화면은 위젯만 40분 → 80분 → … 최대 24시간 쉬고(SourceFailed 지표), 학과 게시판은 그대로 돕니다. 빈 목록은 E3의 빈 응답 횟수에 더해, 위젯과 게시판 한 바퀴(6번) 내내 비면 서버를 쉬게 합니다. 첫 수집은 기준점만 저장합니다(알림 없음). 이 사이트가 실패해도 메인 게시판 수집은 그대로 끝납니다.
- 링크: 위젯의 href는 쓰지 않고, 이 사이트의 공지 링크에서 잡은 글 번호로 `https://aicon.kw.ac.kr/administration/notice.php?admin_mode=read&no=<번호>`를 만듭니다. 다른 호스트로 가는 링크는 버립니다.
- 학부 게시판 2쪽(`department_office/lecture.php?site_type=N&page=2`)은 읽지 않습니다. E3 요청 1회를 위젯과 게시판 5개가 이미 나눠 쓰고 있어, 4개를 더하면 E3 게시판마다 3시간 20분이던 간격이 6시간으로 늘어납니다. 2쪽 글은 대부분 위젯에 먼저 나오고, 따로 읽으면 같은 글이 다른 글 번호(`aicon-<번호>`와 학부 게시판 번호)로 두 번 저장됩니다.
- 저장: `kw-public`의 `SRC#aicon`. 공개 데이터에서 글 번호는 `aicon-<번호>`, 분류는 "인공지능융합대학"(별칭 "인융대"; "학과"는 모든 학과·단과대). 학교 공지 검색·최신 공지·`my_notices`의 `department`에 같이 나옵니다.
- 알림: 분류 `dept-aicon` → 채널 키 `department`. `/kw/discord/channels`에 `department`가 없으면 학교-공지(`notice`) 채널로 보냅니다.

## 5단계: 학과·단과대 게시판

- 출처: 학과 사이트들이 같이 쓰는 PHP 게시판 두 종류입니다. A형은 `BoardMode=list&BOARD_CODE=...`(글 `BoardMode=view&UID=`), B형은 aicon과 같은 엔진(글 `admin_mode=read&no=`)입니다. 게시판 목록과 학과 이름은 `src/kwcampus/deptboards.py`의 `BOARDS` 한 곳에만 있고, 게시판을 늘릴 때는 한 줄을 더합니다. 전자정보공과대학 공지는 학과 사이트마다 똑같이 걸려 있어 ei에서만 읽습니다.
- 학교 사이트는 외부 IDC 서버 네 대에 있고, 호스트 이름이 아니라 서버마다 요청 간격·하루 상한(32회: 평일 낮 30회 + 21시 1회 + 여유 1회)·쿨다운을 둡니다. 실행마다 서버마다 1회만 요청하고, 그 서버에서 가장 오래 안 읽은 게시판을 돌아가며 읽습니다. 한 서버에서 예외가 나도 다른 서버는 그 실행에서 그대로 읽습니다.

| 서버 | 게시판 (키: BOARD_CODE) | 게시판마다 읽는 간격 |
|---|---|---|
| E1 61.111.8.4 | 전자정보공과대학 ei:notice, 전자공학과 ee:bachelor, 전자통신공학과 elcomm:officenotice, 전자융합공학과 radiowave:notice, 전기공학과 electric:notice, 전자재료공학과 snme:college, 반도체시스템공학부 semicon:notice, 법학부 law:notice | 2시간 40분 |
| E2 61.111.6.200 | 건축공학과 archi:note, 인문사회과학대학 chss:notice, 국어국문학과 korean:notice, 영어산업학과 english:notice(EUC-KR), 산업심리학과 psy:notice, 동북아문화산업학부 dnaci:notice, 행정학과 kwpa:faculty, 경영대학 biz:note | 2시간 40분 |
| E3 211.233.72.248 | 인공지능융합대학 위젯(위 절), 참빛인재대학 kwlife:notice, 금융부동산법무학과 kwlaw:notice, 게임콘텐츠학과 kwgame:notice, 스포츠상담재활학과 kwsports:notice, 인제니움학부대학 ingenium:notice | 위젯 40분, 게시판 3시간 20분 |
| E4 114.203.87.122 | 화학공학과 chemng, 스포츠융합과학과 sports (B형) | 40분 |

- 1페이지의 고정 글도 읽고(경영대학은 85행 중 65행이 고정), 새 글은 위치가 아니라 글 번호(UID·no)와 등록일로 판단합니다. 처음 보는 글이 지난번 1페이지의 가장 큰 번호보다 크거나, 등록일이 최근 14일 안이면 새 글입니다. 번호가 작고 오래된 글(1페이지로 밀려 올라온 글, 120일이 지나 다시 보이는 글)은 알림 없이 저장합니다. 그래서 게시판 번호가 다시 매겨지거나 한 번 파싱에 실패한 글도 놓치지 않습니다. 1페이지의 가장 큰 번호가 내려가면(최신 글 삭제, 번호 재설정) `dept_uid_regressed` 로그와 UidRegressed 지표를 남기고 그 페이지를 새 기준으로 씁니다. 같은 번호의 글이 제목과 등록일만 바꿔 다시 올라오면(새 학기 [필독]) 수정으로 저장하고 등록일과 ttl도 새로 씁니다. 경영대학처럼 번호에 쉼표가 있는 행("2,157")도 일반 글로 읽습니다.
- 한 게시판이 빈 목록을 주면 그 게시판만 건너뛰고, 한 바퀴(그 서버의 게시판 수만큼, E3는 위젯까지) 내내 빈 목록이면 서버를 쉬게 합니다. 게시판 하나의 404·리디렉트·바뀐 화면은 서버를 쉬게 하지 않고 그 게시판만 2시간 → 4시간 → … 최대 24시간 쉽니다.
- kwsports는 2024년 글 3개뿐이고 kwgame도 글이 5개라 거의 바뀌지 않습니다. 자율전공학부(liberal)와 job.kw.ac.kr 게시판은 아직 넣지 않았습니다.
- 저장: 게시판마다 `SRC#dept-<키>`(예: `SRC#dept-ee`). 글 번호는 `dept-ee-<UID>`, 분류와 부서는 학과·단과대 이름입니다(별칭 "전정대", "인사대", "경영대", "참빛", "인제니움"). 첫 수집은 기준점만 저장합니다(알림 없음).
- 알림: 분류 `dept-<키>` → 채널 키 `department`(없으면 학교-공지). 구독 역할은 단과대 단위입니다(`routing.COLLEGE_OF`, 예: `경영대학 알림`).

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
  - 한 번 실행할 때 서버마다 요청은 1회입니다. 예외로 www만 2시간에 한 번쯤 61초 간격으로 1회 더 요청합니다(취업게시판·FAQ).
  - 같은 서버(origin)에는 60초 간격을 두고(요청하는 그 시각 기준), 하루 44회(학과 게시판 서버는 32회, Wix 피드 호스트는 7회)로 제한합니다. 수집 Lambda는 동시에 하나만 돌아서(reserved concurrency 1) 손으로 돌린 실행이 예약 실행과 겹치지 않습니다.
  - 수집 시각: 평일 09:00~18:40 20분마다와 평일 21:00(KST). 휴강·통제 공지가 전날 저녁에 올라오는 경우가 많아 저녁에 한 번 더 봅니다. 주말은 공지가 없어(첫 309건 중 0건) 보지 않습니다.
  - 강한 차단(403·429·보안문자/챌린지 화면): 그 서버가 30분 → 60분 → 2시간 … 최대 24시간 쉽니다. 429·503의 Retry-After가 더 길면 그 값(최대 7일)을 따릅니다. 3번 연속이면 그 origin을 끄고, 끈 origin을 건너뛸 때마다 OriginDisabled 지표를 남겨 `kw-collector-origin-disabled` 알람이 계속 울립니다. 다시 켜는 건 사람이 합니다: `uv run python scripts/collector_admin.py reenable --origin <서버>`(기본은 보기만, `--apply`로 차단 횟수·쿨다운·꺼짐을 한꺼번에 되돌림).
  - 일시 장애(연결 실패·시간 초과·5xx): 첫 번째는 다음 실행까지만 기다리고, 그다음부터 30분 → 60분 … 최대 6시간 쉽니다. 끄지는 않고, 6번 연속이면 NeedsHuman 알람을 보냅니다. 빈 목록(모르는 차단 화면일 수 있음)은 30분부터 쉽니다.
  - 게시판 하나의 문제(404·리디렉트·예상과 다른 화면·크기 초과)는 서버를 쉬게 하지 않고 그 게시판만 쉽니다(SourceFailed).
  - 보안문자 판정은 목록을 못 읽은 화면에서만 합니다. 정상 목록 화면에 Cloudflare 분석 스크립트나 CDN 주소가 있어도 차단으로 보지 않습니다.
  - 요청 하나는 소켓 단계마다 15초, 본문 전체 30초를 넘기면 끊고(`slow`), Lambda 남은 시간이 요청 하나의 최악 시간보다 적으면 그 서버는 건너뜁니다.
  - robots.txt: 수집하는 호스트마다 2026-11-06에 손으로 한 번씩 확인하고 결과를 `politeness.ROBOTS`에 적었습니다. 기록이 없는 호스트와 Disallow 경로는 요청하지 않습니다(NeedsHuman). 지금 수집하는 경로는 모두 허용입니다. 같은 E1 서버의 job.kw.ac.kr은 `/recommend/`, `/internship/`, `/job/`, `/notice/news.php` 등을 막아서 수집하지 않습니다.
  - User-Agent: `kw-campus/1.0 (+https://github.com/kyowon1108/kw-campus; contact <bridge>/privacy; …)`. AWS 공용 IP에서 나가므로 학교 관리자가 이 프로젝트와 연락처를 찾을 수 있게 했습니다.
- **저장하는 것**: 제목, 분류, 부서, 날짜, 링크뿐입니다. 제목과 부서는 저장 전에 마스킹합니다(redaction v2, 항목의 `norm_v`=2): 연락처(점·괄호로 쓴 전화번호, `[at]` 메일 포함), 학번·계좌 같은 번호, 사람 이름(님·씨·학생·교수·선생님 앞의 이름, 합격자·명단·수상·학위논문 같은 명단 제목과 부고 제목의 이름, `홍*동` 같은 일부 가린 이름). 본문과 첨부는 가져오지 않습니다. 120일이 지나면 자동으로 지웁니다.
- **내리기**: 학교가 글을 지웠거나 삭제 요청이 오면 `uv run python scripts/collector_admin.py takedown <글 번호>`로 확인하고 `--apply`로 Discord 메시지(수정됨 답글 포함), 전달 기록, kw-public 항목을 지웁니다. 항목은 빈 표시(removed)로 남아 다음 수집이 되살리지 않습니다. 전달 기록은 150일 보관해 공지가 남아 있는 동안은 메시지를 찾을 수 있습니다.
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
- **이름이 들어갈 수 있는 제목**: 제목의 연락처·학번은 가리지만 사람 이름은 아직 가리지 않습니다(가림 규칙 v2 예정). 그때까지 학과 게시판·피드의 수상·명단·합격·부고·조의·당선·선발 결과·축하 글과, 어느 게시판이든 `홍*동`·`김○○`처럼 가린 이름이 보이는 제목은 Discord에 `[학과] 새 글 (제목은 링크에서 확인)`과 링크만, 멘션 없이 올립니다(`routing.name_risk`).
- **버튼 처리**: `kw-discordinteractions` Lambda(`POST /discord/interactions`, API `kw-discord`). Discord 서명(Ed25519)을 확인하고, 버튼 ID에 맞는 역할을 `/kw/discord/roles`에서만 찾아 그 역할 하나만 붙이거나 뗍니다. 요청에 든 역할 ID는 쓰지 않습니다. 서명 시각이 60초보다 오래되었거나 숫자 12자리를 넘는 요청은 거절합니다. 이 함수는 SSM 세 값(`bot-token`, `public-key`, `roles`)을 한 번에 읽고 테이블 권한은 없습니다. 아직 역할이 없는 주제 버튼은 역할 목록을 한 번 더 읽어 보고, 그래도 없으면 "준비 중"이라고 답합니다. 역할을 못 바꾼 클릭이 한 시간에 3번이면 `kw-discord-buttons-failed` 알람이 옵니다(봇 역할이 구독 역할 아래로 내려갔을 때, setup 순서가 틀렸을 때 등). 공개키가 없다는 결과는 캐시하지 않아서, 넣은 즉시 포털 저장이 됩니다.
- **읽기 전용 채널**: 공지 채널, #알림-설정, #학과-공지는 @everyone에게 기록 보기를 허용하고 메시지·스레드 만들기·스레드 메시지를 막습니다. 보기 권한은 건드리지 않아서 숨긴 채널·분류는 숨긴 채로 남습니다. 봇에는 그 채널에서 보기·보내기·링크 첨부·기록 보기와 역할 멘션만 줍니다.

### 서버 설정 (`scripts/discord_setup.py`)

인자 없이 돌리면 계획만 보여 주고 아무것도 바꾸지 않습니다. `--apply`를 붙여야 실제로 만듭니다. 몇 번을 돌려도 없는 것만 만들고 다른 것만 고칩니다.
- 채널에 원래 있던 다른 권한 설정은 그대로 둡니다. 새로 만드는 채널은 #학교-공지가 있는 분류의 권한을 물려받고, 같은 이름의 채널은 그 분류 안에 있을 때만 씁니다.
- 손으로 넣은 `department` 채널도 플래그 없이 늘 읽기 전용·봇 멘션 허용을 맞춥니다. 서버에 없는 ID면 경고합니다.
- 멈추는 경우(아무것도 바꾸지 않음): 봇에 관리자 권한이 있을 때(exit 2), 같은 이름의 기존 역할에 권한이 있거나 연동 역할이거나 봇 역할보다 위에 있을 때(exit 1).
- 고치는 경우: 기존 구독 역할이 '누구나 멘션 가능'이면 끄고, `topics.TOPICS`에서 이름이 바뀌었으면(2028년 단과대 개편 등) 이름만 바꿉니다. #알림-설정·#학과-공지의 채널 주제 문구가 다르면 고칩니다.
- 알려 주는 것: 봇 역할의 위험한 권한(추방·차단·서버 관리·메시지 관리·웹후크 등, 이름 없는 비트는 16진수), @everyone의 '모든 역할 멘션', 서버 기본 알림이 '모든 메시지'인지.
- 역할 목록(`/kw/discord/roles`)은 버튼 메시지를 고치기 전에 씁니다. SSM 쓰기가 실패하면 그대로 실행할 `aws ssm put-parameter` 명령을 보여 주고 exit 1로 끝납니다(값에 비밀은 없음).
- 실행하는 AWS 자격 증명에는 `/kw/discord/bot-token` 읽기(GetParameter, 복호화)와 `/kw/discord/roles`·`/kw/discord/channels` 쓰기(PutParameter)가 둘 다 있어야 합니다. 아래 '배포 권한'의 두 권한 세트 중 하나만으로는 끝까지 못 돕니다. 루트나 두 권한을 다 가진 프로필로 돌리거나, 쓰기 단계에서 출력된 명령을 쓰기 권한 프로필로 실행합니다.

```bash
uv run --with 'botocore[crt]' python scripts/discord_setup.py --department-channel           # 계획만
uv run --with 'botocore[crt]' python scripts/discord_setup.py --department-channel --apply   # 적용
```

`--apply`가 하는 일: 구독 역할 만들기(권한 없음, 멤버는 멘션 불가) → #알림-설정 만들기(`--department-channel`이면 #학과-공지도) → 채널 권한 설정 → SSM `/kw/discord/roles` 쓰기 → 버튼 메시지 올리기/고치기(새 메시지면 그 ID를 다시 씀) → `--department-channel`이면 `/kw/discord/channels`에 `department` 추가. `--region`의 기본값은 `ap-northeast-2`입니다.

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

서버 설정 → 개요 → 기본 알림 설정은 '@멘션만'으로 둡니다. '모든 메시지'면 멤버가 공지와 수정 답글마다 알림을 받아 구독 역할이 의미가 없습니다. 학사·장학·긴급을 기본으로 켜 두려면 봇이 붙이지 말고 Discord 온보딩 질문에서 미리 골라 둡니다.

@everyone 역할에 `@everyone, @here, 모든 역할 멘션하기`가 켜져 있으면 멤버 누구나 구독 역할을 멘션해 구독자 전원을 부를 수 있습니다. 끄는 것을 권합니다. 봇은 공지 채널에서 따로 허용받아 영향이 없습니다. 끄는 건 `--apply` 뒤에 합니다. 봇은 자기에게 있는 권한만 채널에 줄 수 있어서, 먼저 끄면 공지 채널의 봇 멘션 허용을 설정하지 못합니다.

## 스위치와 비밀값 (SSM, CDK가 만들지 않음)

| 파라미터 | 의미 |
|---|---|
| `/kw/collector/enabled` | `true`일 때만 수집. 없으면 꺼짐 |
| `/kw/collector/notify` | `true`일 때만 Discord 전송. 없으면 꺼짐 |
| `/kw/discord/bot-token` | 봇 토큰 (SecureString). 본인이 직접 넣음 |
| `/kw/discord/channels` | `{"notice": "<채널ID>", "academic": ..., "scholarship": ..., "career": ..., "international": ..., "department": ...}` (`department`는 선택) |
| `/kw/discord/public-key` | Discord 앱의 공개키(Ed25519, 16진수). 버튼 요청 서명 확인용. 본인이 직접 넣음 |
| `/kw/discord/roles` | `scripts/discord_setup.py`가 씀: `{"guild": ..., "roles": {주제: 역할ID}, "menu": {"channel", "message"}}` |
| `/kw/mcp/tokens` | 초대·공개 MCP 토큰의 SHA-256 해시 목록 (JSON 배열) |
| `/kw/bridge/jwt-secret` | 액세스 토큰 서명 키 (SecureString) |
| `/kw/bridge/seal-key` | 간편 연결 봉인 키, 32바이트 base64url (SecureString). 바꾸면 모든 간편 연결이 끊김 |

bridge 비밀값은 값이 화면에 나오지 않게 한 번만 만듭니다.

```bash
aws ssm put-parameter --name /kw/bridge/jwt-secret --type SecureString --value "$(openssl rand -base64 48)"
aws ssm put-parameter --name /kw/bridge/seal-key --type SecureString --value "$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode())')"
```

봇 토큰은 Discord 개발자 포털에서 [Reset Token]을 눌러 복사한 뒤, 아래 명령을 터미널에서 직접 실행해 넣습니다. 토큰이 화면이나 셸 기록에 남지 않습니다.

```bash
aws ssm put-parameter --region ap-northeast-2 --name /kw/discord/bot-token --type SecureString --overwrite --value "$(pbpaste)" && pbcopy < /dev/null
```

## 개발

```bash
uv sync
uv run pytest -q
./scripts/build_lambda.sh          # build/lambda (arm64, Python 3.13). ../kw-mcp가 옆에 있어야 함
cd infra && npm install && ./node_modules/.bin/cdk synth
```

## 배포

`build_lambda.sh`는 `../kw-mcp`의 wheel을 만들어 함께 넣습니다(KLAS 로그인 코드를 PC 에이전트와 같이 씀). 같은 wheel에 이 Mac 키체인의 릴리스 키로 서명한 `latest.json`도 만들어 넣습니다. PC 에이전트는 이걸 보고 스스로 업데이트하므로, 에이전트를 바꿨다면 kw-mcp의 version을 꼭 올리세요(같은 버전이면 아무도 받지 않음). Claude 데스크톱 확장도 내려 주려면 먼저 kw-mcp에서 `mcpb pack . dist/kw-klas.mcpb`를 실행해 둡니다.

```bash
cd infra
./node_modules/.bin/cdk bootstrap aws://<계정ID>/ap-northeast-2   # 처음 한 번
./node_modules/.bin/cdk deploy        # 알람 메일은 ~/.config/kw-campus/alarm-email 첫 줄(또는 --context alarmEmail=...)
```

알람 메일을 빼고 배포하면 SNS 구독이 지워집니다. 처음 구독하면 AWS가 확인 메일을 보내고, 그 링크를 눌러야 알람이 옵니다.

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
