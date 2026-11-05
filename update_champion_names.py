"""Data Dragon からチャンピオン名（日本語・英語・韓国語）を取り、data/champion_names.json に書く。

API キーは不要。新チャンピオンはパッチで増えるので、夜間バッチの前に実行する。
id は試合データの championName と同じ（例: Wukong は MonkeyKing）。
image はアイコン画像のファイル名。画面は {DDRAGON}/cdn/{version}/img/champion/{image} を表示する。

例:
  python update_champion_names.py
"""

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DDRAGON = "https://ddragon.leagueoflegends.com"
LOCALES = {"ja": "ja_JP", "en": "en_US", "ko": "ko_KR"}
USER_AGENT = "otp-off-role-insights/0.1"


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_champions():
    version = get_json(DDRAGON + "/api/versions.json")[0]
    champions = {}
    for lang, locale in LOCALES.items():
        payload = get_json("{}/cdn/{}/data/{}/champion.json".format(DDRAGON, version, locale))
        for champion_id, row in payload["data"].items():
            entry = champions.setdefault(
                champion_id,
                {"id": champion_id, "key": row["key"], "image": row["image"]["full"], "names": {}},
            )
            entry["names"][lang] = row["name"]
    return version, sorted(champions.values(), key=lambda row: row["id"])


def main():
    try:
        version, champions = fetch_champions()
    except (OSError, ValueError, KeyError) as error:
        print("Data Dragon から取得できませんでした: {}".format(error), file=sys.stderr)
        sys.exit(1)

    path = Path(__file__).resolve().parent / "data" / "champion_names.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": version,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "champions": champions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    print("パッチ {} / {} 体 ({})".format(version, len(champions), path))


if __name__ == "__main__":
    main()
