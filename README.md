# kw-campus

광운대 공개 정보를 AWS(서울)에서 모아 Discord 채널로 알리고, Claude·ChatGPT에서 쓰는 MCP로 제공합니다. 연결 서버(bridge)를 거치면 각자 자기 KLAS 과제·퀴즈·온라인강의·시간표·과목 공지도 함께 물어볼 수 있습니다. 전체 계획은 `../kw-mcp/docs/plan.md`에 있습니다.

## 4단계: 연결 서버 (kw-bridge)

- 커넥터 URL: `https://<bridge-api>.execute-api.ap-northeast-2.amazonaws.com/mcp` (OAuth 2.1 + PKCE, 동적 클라이언트 등록)
- 안내 페이지: `https://<bridge-api>/setup#<초대코드>` — 연결 방법 세 가지를 쉬운 순서로 보여 줍니다.
- 연결 방법
  - **PC 에이전트**: `kw-mcp link`가 등록한 PC가 WebSocket으로 요청을 받아 KLAS에서 읽고, 요청마다 다른 키로 암호화해 올립니다(`../kw-mcp/src/kw_mcp/agent.py`). 승인 페이지에는 에이전트가 낸 1회용 연결 코드를 넣습니다.
  - **간편 연결** (SSM `/kw/bridge/password-mode`가 `true`일 때만, 지금은 꺼 둠): 승인 페이지에서 학번·비밀번호를 받아 봉인한 토큰으로 서버가 KLAS에 로그인합니다(`src/kwcampus/bridge/password.py`). KLAS가 AWS IP 로그인에 2차 인증을 요구해서 쓸 수 없습니다. 끄면 기존 간편 연결도 다음 호출부터 끊깁니다.
- 도구: `this_week`, `today_briefing`, `my_notices`, `my_courses`, `my_assignments`, `my_quizzes`, `my_online_lectures`, `my_team_projects`, `my_course_notices`, `my_deadlines` + 공개 도구 5개
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

## 1단계 구성

```
EventBridge Scheduler (평일 09:00~18:59 KST, 20분마다)
  → kw-collector: 메인 공지 1페이지 GET 1회 → 파싱·마스킹 → kw-public (DynamoDB)
  → DynamoDB Streams → kw-dispatcher → SQS kw-discord → kw-discordsender → Discord 채널
```

- **학교 서버 예절**
  - 한 번 실행할 때 요청은 1회입니다.
  - 같은 서버(origin)에는 60초 간격을 두고, 하루 40회로 제한합니다.
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
| 취업 | 외부 |
| 국제 | 국제교류, 국제학생 |

## 스위치와 비밀값 (SSM, CDK가 만들지 않음)

| 파라미터 | 의미 |
|---|---|
| `/kw/collector/enabled` | `true`일 때만 수집. 없으면 꺼짐 |
| `/kw/collector/notify` | `true`일 때만 Discord 전송. 없으면 꺼짐 |
| `/kw/discord/bot-token` | 봇 토큰 (SecureString). 본인이 직접 넣음 |
| `/kw/discord/channels` | `{"notice": "<채널ID>", "academic": ..., "scholarship": ..., "career": ..., "international": ...}` |
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

`build_lambda.sh`는 `../kw-mcp`의 wheel을 만들어 함께 넣습니다(KLAS 로그인 코드를 PC 에이전트와 같이 씀). Claude 데스크톱 확장도 내려 주려면 먼저 kw-mcp에서 `mcpb pack . dist/kw-klas.mcpb`를 실행해 둡니다.

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
