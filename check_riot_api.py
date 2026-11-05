"""Riot API が使えるかの確認。Riot ID から PUUID を1件取って表示する。

日本のアカウント検索は Account API の ASIA ルーティングを使う。
API キーは環境変数 RIOT_API_KEY。
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ACCOUNT_HOST = "https://asia.api.riotgames.com"


def main():
    if len(sys.argv) != 3:
        print('使い方: python check_riot_api.py "ユーザー名" "タグライン"', file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("RIOT_API_KEY")
    if not api_key:
        print("環境変数 RIOT_API_KEY を設定してください。", file=sys.stderr)
        sys.exit(1)

    game_name, tag_line = sys.argv[1], sys.argv[2]
    if "#" in game_name or "#" in tag_line:
        print(
            "ユーザー名とタグラインは分けて渡してください。例: tikamanpo#1181 なら",
            file=sys.stderr,
        )
        print(
            '  python check_riot_api.py "tikamanpo" "1181"',
            file=sys.stderr,
        )
        print("JP1 はサーバー名なので、引数には入れません。", file=sys.stderr)
        sys.exit(1)
    path = "/riot/account/v1/accounts/by-riot-id/{}/{}".format(
        urllib.parse.quote(game_name),
        urllib.parse.quote(tag_line),
    )
    request = urllib.request.Request(
        ACCOUNT_HOST + path,
        headers={
            "X-Riot-Token": api_key,
            # urllib の既定 User-Agent は Cloudflare に 403 / 1010 で拒否される
            "User-Agent": "otp-off-role-insights/0.1",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request) as response:
            account = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        print("HTTP {}: {}".format(error.code, body), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as error:
        print("接続失敗: {}".format(error.reason), file=sys.stderr)
        sys.exit(1)

    print("gameName: {}".format(account["gameName"]))
    print("tagLine: {}".format(account["tagLine"]))
    print("puuid: {}".format(account["puuid"]))


if __name__ == "__main__":
    main()
