# kw-campus

광운대 공개 공지를 AWS(서울)에서 모아 Discord 채널로 알려 주는 서비스입니다. 지금은 1단계로, 메인 공지 게시판만 다룹니다. 전체 계획은 `../kw-mcp/docs/plan.md`에 있습니다.

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

봇 토큰은 Discord 개발자 포털에서 [Reset Token]을 눌러 복사한 뒤, 아래 명령을 터미널에서 직접 실행해 넣습니다. 토큰이 화면이나 셸 기록에 남지 않습니다.

```bash
aws ssm put-parameter --region ap-northeast-2 --name /kw/discord/bot-token --type SecureString --overwrite --value "$(pbpaste)" && pbcopy < /dev/null
```

## 개발

```bash
uv sync
uv run pytest -q
./scripts/build_lambda.sh          # build/lambda (arm64, Python 3.13)
cd infra && npm install && ./node_modules/.bin/cdk synth
```

## 배포

```bash
cd infra
./node_modules/.bin/cdk bootstrap aws://<계정ID>/ap-northeast-2   # 처음 한 번
./node_modules/.bin/cdk deploy --context alarmEmail=<알람 받을 메일>
```

배포만 해서는 아무 일도 일어나지 않습니다. 수집을 켜는 순서는 다음과 같습니다.
1. `/kw/collector/enabled=true`로 켜고, 첫 수집(기준점 저장)이 끝났는지 확인합니다.
2. 집 PC의 kw-notice-mcp launchd 수집기를 끕니다. 두 곳이 같은 서버를 동시에 긁지 않게 하려는 것입니다.
3. Discord 채널 ID와 봇 토큰을 넣은 뒤 `/kw/collector/notify=true`로 알림을 켭니다.
