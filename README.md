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
- 학식·학사일정: `kw-campus` Lambda가 평일 07:30에 돕니다. 이번 주 식단이 없거나 월요일일 때만 학식 페이지를 읽고, 학사일정은 7일마다 한 번 읽습니다. 두 요청 사이에는 실제로 61초를 쉽니다.
- 남용 방어: API Gateway가 초당 2건, 순간 최대 5건으로 제한합니다. GET/DELETE는 405를 돌려주어 SSE 연결이 Lambda를 붙잡지 않게 합니다.

친구에게 토큰을 줄 때는 새 토큰을 만들어 해시를 목록에 더합니다. 회수할 때는 그 해시를 지웁니다. 반영까지 최대 5분 걸립니다.

## 5단계: 학과 공지 (인공지능융합대학)

- 출처: 인공지능융합대학 홈의 "대학 공지" 위젯(`https://aicon.kw.ac.kr/main/main.php`). 요청 한 번으로 4개 학부(컴퓨터정보공학부·소프트웨어학부·정보융합학부·로봇학부)의 최신 공지 5개를 받습니다. 학부 게시판 1페이지는 고정 글뿐이라 새 글은 위젯에서만 보입니다.
- 수집: 메인 게시판과 같은 예약 실행(평일 낮 20분마다)에서 한 번 더 요청합니다. ce/cs/ic/cni는 이 호스트의 별칭이라 요청 간격·하루 상한·쿨다운은 `aicon.kw.ac.kr` 하나 기준입니다. 첫 수집은 기준점만 저장합니다(알림 없음). 이 사이트가 실패해도 메인 게시판 수집은 그대로 끝납니다.
- 저장: `kw-public`의 `SRC#aicon`. 공개 데이터에서 글 번호는 `aicon-<번호>`, 분류는 "인공지능융합대학"(별칭 "인융대", "학과"). 학교 공지 검색·최신 공지·`my_notices`의 `department`에 같이 나옵니다.
- 알림: 분류 `dept-aicon` → 채널 키 `department`. `/kw/discord/channels`에 `department`가 없으면 학교-공지(`notice`) 채널로 보냅니다.

## 5단계: 학과 게시판 (전자정보공과대학)

- 출처: 학과 사이트들이 같이 쓰는 PHP 게시판(`BoardMode=list&BOARD_CODE=...`). 전자정보공과대학(ei)·전자공학과·전자통신공학과·전자융합공학과·전기공학과·전자재료공학과·반도체시스템공학부. 게시판 목록과 학과 이름은 `src/kwcampus/deptboards.py`의 `BOARDS` 한 곳에만 있고, 게시판을 늘릴 때는 한 줄을 더합니다. 단과대 공지는 학과 사이트마다 똑같이 걸려 있어 ei에서만 읽습니다.
- 수집: 이 호스트들은 모두 서버 하나(61.111.8.4)의 별칭이라 요청 간격·하루 상한(30회)·쿨다운은 서버 기준입니다. 실행마다 이 서버에는 1회만 요청하고, 가장 오래 안 읽은 게시판을 돌아가며 읽습니다(게시판마다 약 2시간 20분에 한 번). 1페이지의 고정 글도 읽고, 새 글은 위치가 아니라 UID로 판단합니다. 전에 본 가장 큰 UID보다 크면 새 글이고, 작은데 처음 보이는 글은 알림 없이 저장합니다.
- 저장: 게시판마다 `SRC#dept-<키>`(예: `SRC#dept-ee`). 글 번호는 `dept-ee-<UID>`, 분류와 부서는 학과 이름입니다. 첫 수집은 기준점만 저장합니다(알림 없음).
- 알림: 분류 `dept-<키>` → 채널 키 `department`(없으면 학교-공지). 구독 역할은 단과대 단위입니다(`전자정보공과대학 알림`).

## 취업게시판·종합 FAQ (www.kw.ac.kr)

- 메인 공지와 같은 목록 형식이지만 분류 이름이 달라서(`[취업관련]`, 부서 이름) 자체 분류 "취업"(`job`), "FAQ"(`faq`)로 저장합니다. `SRC#kw-job`, `SRC#kw-faq`, 글 번호 `kw-job-<번호>`, `kw-faq-<번호>`.
- www의 요청 간격·하루 40회 상한을 메인 공지와 같이 씁니다. 그래서 취업게시판은 2시간에 한 번, FAQ는 하루에 한 번만 읽고, 한 실행에서 둘 중 하나만 읽습니다. 메인 공지 요청 뒤 실제로 61초를 쉬고 읽습니다(그래서 수집 Lambda 제한 시간은 150초입니다). 하루 www 요청: 메인 30 + 취업 5 + FAQ 1 + 학식·학사일정 3 이하.
- 알림: 취업 → 채널 키 `career`. FAQ는 검색만 되고 알림은 보내지 않습니다.

## 1단계 구성

```
EventBridge Scheduler (평일 09:00~18:59 KST, 20분마다)
  → kw-collector: 메인 공지 1페이지 GET 1회 → 파싱·마스킹 → kw-public (DynamoDB)
  → DynamoDB Streams → kw-dispatcher → SQS kw-discord → kw-discordsender → Discord 채널
```

- **학교 서버 예절**
  - 한 번 실행할 때 서버마다 요청은 1회입니다. www만 2시간에 한 번쯤 61초 간격으로 1회 더 요청합니다(취업게시판·FAQ).
  - 같은 서버(origin)에는 60초 간격을 두고, 하루 40회(학과 게시판 서버는 30회)로 제한합니다.
  - 403·429·보안문자·빈 목록이 오면 쿨다운에 들어갑니다. 쿨다운은 30분에서 시작해 두 배씩 늘고, 최대 24시간입니다.
  - 3번 연속으로 막히면 그 origin을 끕니다. 다시 켜는 건 사람이 합니다.
- **저장하는 것**: 제목(연락처는 가림), 분류, 부서, 날짜, 링크뿐입니다. 본문과 첨부는 가져오지 않습니다. 120일이 지나면 자동으로 지웁니다.
- **첫 수집**: 알림 없이 기준점으로만 저장합니다. 그 뒤 새 글만 알립니다. 오래된 고정 글은 알리지 않습니다.
- **채널 배정**

| 채널 | 분류 |
|---|---|
| 학교-공지 | 일반, 학생, 봉사, 입학, 시설, 병무 |
| 학사 | 학사 |
| 장학 | 등록/장학 |
| 취업 | 외부, 취업(취업게시판) |
| 학과-공지 | 인공지능융합대학, 전자정보공과대학과 그 학과들 (`department` 채널 ID가 없으면 학교-공지) |
| 국제 | 국제교류, 국제학생 |

## Discord 알림: 수정 공지·구독 역할·읽기 전용 채널

- **수정 공지**: 이미 알린 공지의 제목이나 수정일(메인 공지·취업게시판에만 있음, 학과 게시판·aicon은 제목만)이 바뀌면 원래 메시지를 고칩니다. 올린 지 30분이 넘었으면 그 메시지에 `✏️ 수정됨 · <수정일>` 답글을 링크와 함께 답니다. 수정일이 없는 게시판은 바뀐 걸 본 날짜를 씁니다. 답글은 공지 하나에 한국 시간 하루 한 번까지이고, `kw-delivery`의 `reply_day`로 셉니다. 누가 원래 메시지를 지웠으면 다시 만들지 않습니다.
- **구독 역할**: 주제마다 역할 하나(`학사 알림`, `장학 알림`, `취업 알림`, `국제 알림`, `일반 공지 알림`, `인공지능융합대학 알림`, `전자정보공과대학 알림`, 목록은 `routing.TOPICS`). #알림-설정의 버튼을 누르면 그 역할이 켜지거나 꺼지고, 누른 사람에게만 보이는 답이 옵니다. 새 공지는 자기 주제 역할만 멘션합니다(`allowed_mentions`에 그 역할 하나, @everyone·@here 없음). 22:00~08:00에 올라가는 공지는 멘션 없이 올립니다. 공지를 고쳐도 다시 울리지 않습니다.
- **버튼 처리**: `kw-discordinteractions` Lambda(`POST /discord/interactions`, API `kw-discord`). Discord 서명(Ed25519)을 확인하고, 버튼 ID에 맞는 역할을 `/kw/discord/roles`에서만 찾아 그 역할 하나만 붙이거나 뗍니다. 요청에 든 역할 ID는 쓰지 않습니다. 이 함수는 SSM 세 값(`bot-token`, `public-key`, `roles`)만 읽고 테이블 권한은 없습니다.
- **읽기 전용 채널**: 공지 채널과 #알림-설정은 @everyone에게 보기·기록 보기만 허용하고 메시지·스레드 만들기·스레드 메시지를 막습니다. 봇에는 그 채널에서 보기·보내기·링크 첨부·기록 보기와 역할 멘션만 줍니다.

### 서버 설정 (`scripts/discord_setup.py`)

인자 없이 돌리면 계획만 보여 주고 아무것도 바꾸지 않습니다. `--apply`를 붙여야 실제로 만듭니다. 몇 번을 돌려도 없는 것만 만들고 다른 것만 고칩니다. 채널에 원래 있던 다른 권한 설정은 그대로 둡니다.

```bash
uv run --with 'botocore[crt]' python scripts/discord_setup.py --department-channel           # 계획만
uv run --with 'botocore[crt]' python scripts/discord_setup.py --department-channel --apply   # 적용
```

`--apply`가 하는 일: 구독 역할 만들기(권한 없음, 멤버는 멘션 불가) → #알림-설정 만들기(`--department-channel`이면 #학과-공지도) → 채널 권한 설정 → 버튼 메시지 올리기/고치기 → SSM `/kw/discord/roles` 쓰기(`--department-channel`이면 `/kw/discord/channels`에 `department` 추가). `--region`의 기본값은 `ap-northeast-2`입니다.

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
