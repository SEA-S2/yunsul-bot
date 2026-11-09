# Railway 24시간 실행 가이드 (윤슬봇)

이 저장소는 두 개의 독립된 기능을 가지고 있습니다.

1. `post_daily.py`: 기존 GitHub Actions 예약 게시. **변경 없음**.
2. `contribution_bot.py`: 24시간 실행이 필요한 납품·기여도 명령어. Railway 서비스에서 실행.

## 예상 요금
Railway Hobby는 월 **최소 $5**이고 월 $5 사용량이 포함됩니다. 메모리/CPU/네트워크/디스크 사용량이 이를 초과하면 추가 청구될 수 있습니다. Railway의 Billing에서 사용량과 한도를 확인하세요. 무료 요금제를 선택할 수도 있지만 지속적인 무중단 운영은 보장하지 않습니다.

## 서비스 만들기
1. https://railway.com 에서 GitHub로 로그인.
2. **New Project → Deploy from GitHub repo** → `SEA-S2/yunsul-bot` 선택.
3. 저장소 루트 `Dockerfile`을 이용해 Python 서비스가 빌드됩니다. **실행 명령어는 `python contribution_bot.py`**입니다.
4. **Variables**에서 아래 환경변수를 추가하세요. 코드 저장소/채팅에 토큰을 올리지 마세요.
   - `DISCORD_TOKEN`: 기존 윤슬봇의 Bot Token
   - `DISCORD_GUILD_ID`: 윤 슬 Discord 서버 ID (개발자 모드로 서버 우클릭 → 서버 ID 복사)
   - `CONTRIBUTION_DB_PATH`: `/data/contributions.sqlite3`
5. **Volume(영구 저장 공간)**을 해당 서비스에 추가하고 **Mount path를 정확히 `/data`**로 설정하세요. 이 단계를 빠뜨리면 배포/재시작 때 납품 기록이 소실될 수 있습니다.
6. **Settings**에서 Replicas는 1개, 공개 도메인은 불필요, 서비스는 상시 실행하도록 설정하세요. Railway 서비스가 Discord 게이트웨이에 **외부 연결**하므로 웹 서버 포트/URL이 필요하지 않습니다.
7. 변수와 볼륨 설정이 모두 반영된 뒤 **Deploy/Redeploy**. 로그에서 `윤 슬 납품봇 로그인` 및 `동기화된 명령어: 7개` 확인.
8. Discord에서 `/재료설정`, `/목표설정`, `/납품등록`, `/기여도` 순으로 확인합니다.

## 실수 방지
- **Bot Token을 Reset하지 마세요.** 현재 토큰을 알 수 없거나 노출이 의심될 때만 재발급이 필요합니다. 재발급 시 기존 GitHub Actions Secret도 업데이트해야 합니다.
- 실제 디스코드 서버에 윤슬봇이 이미 들어 있다면 새로 초대할 필요 없습니다. 명령어가 나타나지 않으면 `applications.commands` 스코프 및 로그를 확인합니다.
- Discord 봇 토큰을 외부에 보여 주지 마세요.
- 봇이 재시작되어도 SQLite 데이터를 유지하려면 `CONTRIBUTION_DB_PATH=/data/contributions.sqlite3`와 Volume의 `/data` 마운트가 **둘 다 필요**합니다.
- 데이터베이스는 정기적으로 Railway Volume 백업을 설정하세요.
- 실행중인 봇 프로세스는 하나만 유지하고, 동일한 SQLite를 동시에 여러 프로세스가 쓰게 하지 마세요.
- Github Actions는 실시간 봇 서버가 아니므로 기존 워크플로 실행 여부와 별개입니다.

## 개발 메모
Dockerfile은 추가 패키지를 `requirements-contribution.txt`에서 설치하고 `contribution_bot.py`만 실행합니다. 기존 스케줄러는 Docker 컨테이너에서 실행하지 않습니다.
