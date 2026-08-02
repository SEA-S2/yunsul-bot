import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

THREAD_ID = "1506549734859472896"
KST = ZoneInfo("Asia/Seoul")
API_BASE = "https://discord.com/api/v10"


def api_request(method: str, path: str, token: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        API_BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "YunsulBot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord API 오류 {error.code}: {detail}") from error


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        sys.exit("GitHub Secret 'DISCORD_TOKEN'이 설정되지 않았습니다.")

    now = dt.datetime.now(KST)
    content = f"{now.month}/{now.day} 완료"

    bot_user = api_request("GET", "/users/@me", token)
    bot_id = str(bot_user["id"])

    # 같은 날짜 문구가 이미 올라갔다면 중복 작성하지 않습니다.
    recent = api_request("GET", f"/channels/{THREAD_ID}/messages?limit=50", token)
    for message in recent:
        if str(message.get("author", {}).get("id")) == bot_id and message.get("content", "").strip() == content:
            print(f"이미 작성되어 있어 건너뜁니다: {content}")
            return

    api_request("POST", f"/channels/{THREAD_ID}/messages", token, {"content": content})
    print(f"작성 완료: {content}")


if __name__ == "__main__":
    main()
