"""マスター以上のソロキューから、OTP のサブレーン統計を集めて JSON にする。

対象リージョンは --region（JP / KR / NA、既定 JP）。API キーは環境変数 RIOT_API_KEY。
出力は data/<region を小文字>/。試合キャッシュは全リージョン共通（試合 ID にリージョンが入る）。

試合 API の上限はルーティング単位で数えられる。JP と KR はどちらも ASIA なので、
同時に動かさず順番に実行する。NA（AMERICAS）は JP / KR と並行してよい。

判定（直近のランクソロから）:
- メインレーンは試合数が一番多いレーン。同数なら対象外
- そのレーンで 15 試合以上、かつ同一チャンピオンの使用率がしきい値以上で、同率 1 位がいない
- それ以外のレーンで使ったチャンピオンを、サブレーン / オートフィルとして集計する
  （マッチ API にオートフィルフラグはない）

しきい値は 2 種類あり、それぞれ別の集計を作る。
- main: メイン（--main-rate、既定 40%）→ data/<region>/main/
- otp:  OTP（--otp-rate、既定 80%）→ data/<region>/otp/

全員の試合を取ると時間がかかるので、先にチャンピオン熟練度（1 リクエスト / 人）で
候補を絞る。熟練度 1 位が 2 位の --mastery-ratio 倍以上の人だけ試合を取る。0 で無効。

プレイヤーごとの試合（レーンとチャンピオン）は data/<region>/players に保存し、判定は
JSON を作るたびにやり直す。しきい値を変えたら --rebuild-only で API なしに作り直せる。

開発用キーの制限（2 分 100 リクエスト）に合わせて、リクエスト間隔は既定 1.26 秒。
中断してもプレイヤー単位で再開する。全員の更新は --refresh（試合は --max-age-hours、
熟練度は --mastery-max-age-hours より古いものを取り直す）。

例:
  python collect_otp.py --limit 20
  python collect_otp.py --region KR --tiers CHALLENGER GRANDMASTER
  python collect_otp.py --screen-only
  python collect_otp.py --rebuild-only --otp-rate 0.7
  python collect_otp.py --combine
  python collect_otp.py
  python collect_otp.py --refresh
"""

import argparse
import collections
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

Region = collections.namedtuple("Region", "code platform platform_host match_host")
REGIONS = {
    "JP": Region("JP", "JP1", "https://jp1.api.riotgames.com", "https://asia.api.riotgames.com"),
    "KR": Region("KR", "KR", "https://kr.api.riotgames.com", "https://asia.api.riotgames.com"),
    "NA": Region("NA", "NA1", "https://na1.api.riotgames.com", "https://americas.api.riotgames.com"),
}
# 全リージョンの合計（--combine）。API は叩かないので問い合わせ先はない
COMBINED = Region("ALL", "ALL", "", "")
QUEUE = "RANKED_SOLO_5x5"
QUEUE_ID = 420
TIERS = ("CHALLENGER", "GRANDMASTER", "MASTER")
TIER_PATHS = {
    "CHALLENGER": "/lol/league/v4/challengerleagues/by-queue/" + QUEUE,
    "GRANDMASTER": "/lol/league/v4/grandmasterleagues/by-queue/" + QUEUE,
    "MASTER": "/lol/league/v4/masterleagues/by-queue/" + QUEUE,
}
RIOT_ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
ROLE_TO_UI = {
    "TOP": "TOP",
    "JUNGLE": "JUNGLE",
    "MIDDLE": "MID",
    "BOTTOM": "BOTTOM",
    "UTILITY": "UTILITY",
}
UI_LANES = ("TOP", "JUNGLE", "MID", "BOTTOM", "UTILITY")
MIN_DURATION_SECONDS = 300
USER_AGENT = "otp-off-role-insights/0.1"
SCREEN_RATIOS = (1.0, 1.25, 1.5, 2.0, 3.0)


class RiotError(Exception):
    def __init__(self, code, body):
        super().__init__("HTTP {}: {}".format(code, body))
        self.code = code


class RateLimiter:
    def __init__(self, min_interval):
        self.min_interval = min_interval
        self.next_at = 0.0

    def wait(self):
        now = time.monotonic()
        if now < self.next_at:
            time.sleep(self.next_at - now)
        self.next_at = time.monotonic() + self.min_interval


def riot_get(url, api_key, limiter):
    request = urllib.request.Request(
        url,
        headers={
            "X-Riot-Token": api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    last_error = None
    for attempt in range(6):
        limiter.wait()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if error.code == 429:
                retry_after = error.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(120, 2 ** attempt)
                print("429 のため {} 秒待ちます".format(delay), flush=True)
                time.sleep(delay)
                continue
            if error.code in (500, 502, 503, 504):
                last_error = RiotError(error.code, body)
                time.sleep(min(60, 2 ** attempt))
                continue
            raise RiotError(error.code, body)
        except OSError as error:
            # 読み取り中のタイムアウトや切断は URLError に包まれずに届く
            last_error = RiotError(0, str(getattr(error, "reason", error)))
            time.sleep(min(60, 2 ** attempt))
    raise last_error or RiotError(0, "request failed")


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def champion_filename(name):
    chars = []
    for char in name:
        if char.isalnum() or char in "._-":
            chars.append(char)
    # 検索側の入力（Kai'Sa / kaisa）と試合データの表記（Kaisa）をそろえる
    return "".join(chars).lower() or "unknown"


def other_lanes(main_lane):
    return tuple(lane for lane in UI_LANES if lane != main_lane)


def classify_games(games, min_main_games, min_rate):
    by_role = {role: [] for role in RIOT_ROLES}
    for game in games:
        role = game.get("role")
        champion = game.get("champion")
        if role in by_role and champion:
            by_role[role].append(champion)

    ranked = sorted(
        ((role, champs) for role, champs in by_role.items() if champs),
        key=lambda item: (-len(item[1]), item[0]),
    )
    if not ranked:
        return {"qualified": False, "reason": "no-games"}
    if len(ranked) >= 2 and len(ranked[0][1]) == len(ranked[1][1]):
        return {"qualified": False, "reason": "tied-main-lane"}

    main_role, main_champions = ranked[0]
    main_lane = ROLE_TO_UI[main_role]
    main_games = len(main_champions)
    if main_games < min_main_games:
        return {
            "qualified": False,
            "reason": "below-min-games",
            "mainLane": main_lane,
            "mainLaneGames": main_games,
        }

    counts = {}
    for champion in main_champions:
        counts[champion] = counts.get(champion, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    champion, champion_games = ordered[0]
    if len(ordered) >= 2 and ordered[1][1] == champion_games:
        return {
            "qualified": False,
            "reason": "tied-champion",
            "mainLane": main_lane,
            "mainLaneGames": main_games,
        }
    rate = champion_games / main_games
    if rate < min_rate:
        return {
            "qualified": False,
            "reason": "below-rate",
            "mainLane": main_lane,
            "mainLaneGames": main_games,
            "champion": champion,
            "mainLaneRate": round(rate, 4),
        }

    off_lanes = {lane: {} for lane in other_lanes(main_lane)}
    for role, champs in by_role.items():
        lane = ROLE_TO_UI[role]
        if lane == main_lane:
            continue
        for name in champs:
            off_lanes[lane][name] = off_lanes[lane].get(name, 0) + 1

    return {
        "qualified": True,
        "reason": "ok",
        "champion": champion,
        "mainLane": main_lane,
        "mainLaneGames": main_games,
        "mainLaneChampionGames": champion_games,
        "mainLaneRate": round(rate, 4),
        "mainLaneOther": {
            name: games for name, games in counts.items() if name != champion
        },
        "offLanes": off_lanes,
    }


def game_duration_seconds(info):
    duration = info.get("gameDuration") or 0
    if duration > 100000:
        duration = duration / 1000
    if not duration:
        start = info.get("gameStartTimestamp")
        end = info.get("gameEndTimestamp")
        if start and end and end > start:
            duration = (end - start) / 1000
    return duration


def games_from_match(match, puuid):
    info = match.get("info", match)
    if info.get("queueId") not in (None, QUEUE_ID):
        return None
    if game_duration_seconds(info) < MIN_DURATION_SECONDS:
        return None
    for participant in info.get("participants") or []:
        if participant.get("puuid") != puuid:
            continue
        role = participant.get("teamPosition") or ""
        champion = participant.get("championName") or ""
        if role not in ROLE_TO_UI or not champion:
            return None
        return {"role": role, "champion": champion}
    return None


def slim_match(match):
    info = match.get("info", match)
    return {
        "queueId": info.get("queueId"),
        "gameDuration": info.get("gameDuration"),
        "gameStartTimestamp": info.get("gameStartTimestamp"),
        "gameEndTimestamp": info.get("gameEndTimestamp"),
        "participants": [
            {
                "puuid": participant.get("puuid"),
                "championName": participant.get("championName"),
                "teamPosition": participant.get("teamPosition"),
            }
            for participant in info.get("participants") or []
        ],
    }


def picks_list(counts, total):
    rows = []
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    for champion, games in ordered:
        rate = round(games / total * 100, 1) if total else 0.0
        rows.append({"champion": champion, "games": games, "rate": rate})
    return rows


def empty_bucket(main_lane):
    return {
        "players": 0,
        "mainLaneGames": 0,
        "mainLaneOther": {},
        "offLanes": {lane: {} for lane in other_lanes(main_lane)},
    }


def add_player(buckets, player):
    key = (player["champion"], player["mainLane"])
    bucket = buckets.get(key)
    if bucket is None:
        bucket = empty_bucket(player["mainLane"])
        buckets[key] = bucket
    bucket["players"] += 1
    bucket["mainLaneGames"] += player["mainLaneGames"]
    for champion, games in player.get("mainLaneOther", {}).items():
        others = bucket["mainLaneOther"]
        others[champion] = others.get(champion, 0) + games
    for lane, picks in player.get("offLanes", {}).items():
        dest = bucket["offLanes"].setdefault(lane, {})
        for champion, games in picks.items():
            dest[champion] = dest.get(champion, 0) + games


def lane_block(bucket):
    off_lanes = {}
    for lane in other_lanes_from_bucket(bucket):
        picks = bucket["offLanes"].get(lane, {})
        total = sum(picks.values())
        off_lanes[lane] = {"games": total, "picks": picks_list(picks, total)}
    return {
        "players": bucket["players"],
        "mainLaneGames": bucket["mainLaneGames"],
        "mainLaneNext": picks_list(bucket["mainLaneOther"], bucket["mainLaneGames"]),
        "offLanes": off_lanes,
    }


def other_lanes_from_bucket(bucket):
    return tuple(bucket["offLanes"].keys())


def aggregate_players(players):
    buckets = {}
    for player in players:
        if player.get("qualified"):
            add_player(buckets, player)
    return buckets


def champion_payload(region, champion, blocks, updated_at):
    ordered = sorted(
        blocks.items(),
        key=lambda item: (-item[1]["players"], -item[1]["mainLaneGames"], item[0]),
    )
    main_lane, dominant = ordered[0]
    by_main_lane = {}
    for lane, bucket in ordered:
        by_main_lane[lane] = lane_block(bucket)
    dominant_block = by_main_lane[main_lane]
    return {
        "region": region.code,
        "platform": region.platform,
        "queue": QUEUE,
        "champion": champion,
        "updatedAt": updated_at,
        "mainLane": main_lane,
        "players": dominant["players"],
        "mainLaneGames": dominant_block["mainLaneGames"],
        "mainLaneNext": dominant_block["mainLaneNext"],
        "offLanes": dominant_block["offLanes"],
        "byMainLane": by_main_lane,
    }


def load_player_record(path):
    if not path.exists():
        return None
    try:
        record = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    # games を持たない旧形式のファイルは取り直す
    if not isinstance(record.get("games"), list):
        return None
    return record


def classify_player(record, min_main_games, min_rate):
    result = classify_games(record["games"], min_main_games, min_rate)
    result["puuid"] = record.get("puuid")
    result["tier"] = record.get("tier")
    return result


def load_player_records(player_dir):
    records = []
    if player_dir.exists():
        for path in sorted(player_dir.glob("*.json")):
            record = load_player_record(path)
            if record:
                records.append(record)
    return records


def rebuild_champion_files(region, player_dir, output_dir, matches, min_main_games, rates):
    """rates の profile（main / otp）ごとに集計を作り、profile → チャンピオン数を返す。"""
    return rebuild_from_records(
        region, load_player_records(player_dir), output_dir, matches, min_main_games, rates
    )


def rebuild_combined(data_dir, matches, min_main_games, rates):
    """全リージョンのプレイヤーをまとめた集計を data/all/ に作る。API は叩かない。"""
    records = []
    for code in sorted(REGIONS):
        records.extend(load_player_records(data_dir / code.lower() / "players"))
    return rebuild_from_records(
        COMBINED, records, data_dir / COMBINED.code.lower(), matches, min_main_games, rates
    )


def rebuild_from_records(region, records, output_dir, matches, min_main_games, rates):
    updated_at = datetime.now(timezone.utc).isoformat()
    return {
        profile: rebuild_profile(
            region,
            profile,
            min_rate,
            records,
            output_dir / profile,
            matches,
            min_main_games,
            updated_at,
        )
        for profile, min_rate in rates.items()
    }


def rebuild_profile(region, profile, min_rate, records, output_dir, matches, min_main_games, updated_at):
    players = [classify_player(record, min_main_games, min_rate) for record in records]

    buckets = aggregate_players(players)
    grouped = {}
    for (champion, lane), bucket in buckets.items():
        grouped.setdefault(champion, {})[lane] = bucket

    champion_dir = output_dir / "champions"
    champion_dir.mkdir(parents=True, exist_ok=True)
    written = set()
    index_rows = []
    for champion in sorted(grouped):
        payload = champion_payload(region, champion, grouped[champion], updated_at)
        payload["profile"] = profile
        payload["minRate"] = min_rate
        filename = champion_filename(champion) + ".json"
        write_json(champion_dir / filename, payload)
        written.add(filename)
        index_rows.append(
            {
                "champion": champion,
                "mainLane": payload["mainLane"],
                "players": payload["players"],
                "file": "champions/" + filename,
            }
        )

    if champion_dir.exists():
        for path in champion_dir.glob("*.json"):
            if path.name not in written:
                path.unlink()

    index_rows.sort(key=lambda row: (-row["players"], row["champion"]))
    write_json(
        output_dir / "index.json",
        {
            "region": region.code,
            "platform": region.platform,
            "profile": profile,
            "queue": QUEUE,
            "updatedAt": updated_at,
            "matchesPerPlayer": matches,
            "minMainGames": min_main_games,
            "minRate": min_rate,
            "qualifiedPlayers": sum(1 for player in players if player.get("qualified")),
            "champions": index_rows,
        },
    )
    return len(index_rows)


def fetch_ladder(region, tiers, api_key, limiter):
    players = []
    seen = set()
    for tier in tiers:
        payload = riot_get(region.platform_host + TIER_PATHS[tier], api_key, limiter)
        entries = payload.get("entries") or []
        added = 0
        for entry in entries:
            puuid = entry.get("puuid")
            if not puuid:
                summoner_id = entry.get("summonerId")
                if not summoner_id:
                    continue
                summoner = riot_get(
                    region.platform_host
                    + "/lol/summoner/v4/summoners/"
                    + urllib.parse.quote(summoner_id),
                    api_key,
                    limiter,
                )
                puuid = summoner.get("puuid")
            if not puuid or puuid in seen:
                continue
            seen.add(puuid)
            players.append({"puuid": puuid, "tier": tier})
            added += 1
        print("{}: {} 人".format(tier, added), flush=True)
    if not players:
        raise RiotError(0, "リーグ一覧から PUUID を取得できませんでした")
    return players


def fetch_match_ids(region, puuid, count, api_key, limiter):
    ids = []
    start = 0
    quoted = urllib.parse.quote(puuid)
    while len(ids) < count:
        take = min(100, count - len(ids))
        query = urllib.parse.urlencode(
            {"queue": QUEUE_ID, "start": start, "count": take}
        )
        url = "{}/lol/match/v5/matches/by-puuid/{}/ids?{}".format(
            region.match_host, quoted, query
        )
        batch = riot_get(url, api_key, limiter)
        if not isinstance(batch, list):
            break
        ids.extend(batch)
        if len(batch) < take:
            break
        start += len(batch)
    return ids


def load_match(region, match_id, cache_dir, api_key, limiter):
    path = cache_dir / (champion_filename(match_id) + ".json")
    if path.exists():
        try:
            return read_json(path)
        except (OSError, json.JSONDecodeError):
            pass
    try:
        payload = riot_get(
            region.match_host + "/lol/match/v5/matches/" + urllib.parse.quote(match_id),
            api_key,
            limiter,
        )
    except RiotError as error:
        if error.code == 404:
            return None
        raise
    slim = slim_match(payload)
    write_json(path, slim)
    return slim


def is_fresh(fetched_at, refresh, max_age_hours):
    if not refresh:
        return True
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - fetched < timedelta(hours=max_age_hours)


def should_skip(path, refresh, max_age_hours, matches):
    record = load_player_record(path)
    if record is None or record.get("matchesRequested") != matches:
        return False
    return is_fresh(record.get("fetchedAt"), refresh, max_age_hours)


class MasteryStore:
    """PUUID ごとの熟練度上位 2 体を mastery.json にまとめて保存する。"""

    def __init__(self, path):
        self.path = path
        self.entries = {}
        self.unsaved = 0
        if path.exists():
            try:
                self.entries = read_json(path)
            except (OSError, json.JSONDecodeError):
                print("mastery.json を読めないので作り直します", flush=True)

    def get(self, puuid, refresh, max_age_hours):
        entry = self.entries.get(puuid)
        if entry and is_fresh(entry.get("fetchedAt"), refresh, max_age_hours):
            return entry
        return None

    def put(self, puuid, top):
        entry = {"fetchedAt": datetime.now(timezone.utc).isoformat(), "top": top}
        self.entries[puuid] = entry
        self.unsaved += 1
        if self.unsaved >= 50:
            self.save()
        return entry

    def save(self):
        if self.unsaved:
            write_json(self.path, self.entries)
            self.unsaved = 0


def fetch_mastery(region, puuid, api_key, limiter):
    url = "{}/lol/champion-mastery/v4/champion-masteries/by-puuid/{}/top?count=2".format(
        region.platform_host, urllib.parse.quote(puuid)
    )
    try:
        rows = riot_get(url, api_key, limiter)
    except RiotError as error:
        if error.code == 404:
            return []
        raise
    return [
        {"championId": row.get("championId"), "points": row.get("championPoints") or 0}
        for row in rows or []
    ]


def ensure_mastery(region, store, puuid, refresh, max_age_hours, api_key, limiter):
    entry = store.get(puuid, refresh, max_age_hours)
    if entry is None:
        entry = store.put(puuid, fetch_mastery(region, puuid, api_key, limiter))
    return entry


def mastery_ratio(top):
    if not top:
        return 0.0
    if len(top) == 1:
        return float("inf")
    return top[0]["points"] / max(top[1]["points"], 1)


def screen_ladder(region, ladder, store, args, api_key, limiter):
    missing = [
        player
        for player in ladder
        if store.get(player["puuid"], args.refresh, args.mastery_max_age_hours) is None
    ]
    print(
        "熟練度の取得: {} 人（約 {:.1f} 時間）".format(
            len(missing), len(missing) * args.min_interval / 3600
        ),
        flush=True,
    )
    for index, player in enumerate(missing, start=1):
        store.put(player["puuid"], fetch_mastery(region, player["puuid"], api_key, limiter))
        if index % 100 == 0:
            print("{}/{}".format(index, len(missing)), flush=True)
    store.save()


def profile_rates(args):
    return {"main": args.main_rate, "otp": args.otp_rate}


def keep_awake():
    """実行中に Windows がスリープしないようにする。プロセスが終われば自動で元に戻る。"""
    if sys.platform != "win32":
        return
    import ctypes

    es_continuous = 0x80000000
    es_system_required = 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)


def print_counts(counts, output_dir):
    print(
        "チャンピオン JSON: メイン {} 件 / OTP {} 件 ({})".format(
            counts["main"], counts["otp"], output_dir
        ),
        flush=True,
    )


def report_screening(ladder, store, player_dir, args):
    ratios = {}
    for player in ladder:
        entry = store.entries.get(player["puuid"])
        if entry is not None:
            ratios[player["puuid"]] = mastery_ratio(entry["top"])

    rates = profile_rates(args)
    qualified = {profile: [] for profile in rates}
    for puuid, ratio in ratios.items():
        record = load_player_record(player_dir / (puuid + ".json"))
        if not record:
            continue
        for profile, min_rate in rates.items():
            if classify_player(record, args.min_main_games, min_rate)["qualified"]:
                qualified[profile].append(ratio)

    print("", flush=True)
    print(
        "熟練度 1位/2位 の比ごとの候補人数。収集済みで条件を満たした人"
        "（メイン {} 人 / OTP {} 人）のうち、ふるいに残る人数".format(
            len(qualified["main"]), len(qualified["otp"])
        ),
        flush=True,
    )
    for threshold in SCREEN_RATIOS:
        candidates = sum(1 for ratio in ratios.values() if ratio >= threshold)
        hours = candidates * (1 + args.matches) * args.min_interval / 3600
        kept = {
            profile: sum(1 for ratio in values if ratio >= threshold)
            for profile, values in qualified.items()
        }
        print(
            "  {:>4} 倍以上: {:>5} 人  試合取得 最大 {:>5.1f} 時間  残るメイン {}/{}  残る OTP {}/{}".format(
                threshold,
                candidates,
                hours,
                kept["main"],
                len(qualified["main"]),
                kept["otp"],
                len(qualified["otp"]),
            ),
            flush=True,
        )


def collect_player(region, player, matches, cache_dir, api_key, limiter):
    puuid = player["puuid"]
    try:
        match_ids = fetch_match_ids(region, puuid, matches, api_key, limiter)
    except RiotError as error:
        if error.code != 404:
            raise
        match_ids = []

    games = []
    for match_id in match_ids:
        match = load_match(region, match_id, cache_dir, api_key, limiter)
        if not match:
            continue
        game = games_from_match(match, puuid)
        if game:
            games.append(game)

    return {
        "puuid": puuid,
        "tier": player["tier"],
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "matchesRequested": matches,
        "matchesFound": len(match_ids),
        "games": games,
    }


def run_self_test():
    games = (
        [{"role": "MIDDLE", "champion": "Yasuo"}] * 20
        + [{"role": "MIDDLE", "champion": "Yone"}] * 4
        + [{"role": "TOP", "champion": "Sett"}] * 3
        + [{"role": "TOP", "champion": "Garen"}]
        + [{"role": "UTILITY", "champion": "Thresh"}] * 2
    )
    result = classify_games(games, 15, 0.5)
    assert result["qualified"] is True
    assert result["champion"] == "Yasuo"
    assert result["mainLane"] == "MID"
    assert result["mainLaneOther"]["Yone"] == 4
    assert result["offLanes"]["TOP"]["Sett"] == 3
    assert result["offLanes"]["UTILITY"]["Thresh"] == 2

    tied = classify_games(
        [{"role": "MIDDLE", "champion": "Ahri"}] * 10
        + [{"role": "TOP", "champion": "Sett"}] * 10,
        8,
        0.5,
    )
    assert tied["qualified"] is False
    assert tied["reason"] == "tied-main-lane"

    low = classify_games([{"role": "MIDDLE", "champion": "Yasuo"}] * 10, 15, 0.5)
    assert low["reason"] == "below-min-games"

    short = slim_match(
        {
            "info": {
                "queueId": 420,
                "gameDuration": 200,
                "participants": [
                    {
                        "puuid": "p1",
                        "championName": "Yasuo",
                        "teamPosition": "MIDDLE",
                    }
                ],
            }
        }
    )
    assert games_from_match(short, "p1") is None

    long_match = dict(short)
    long_match["gameDuration"] = 1200
    played = games_from_match(long_match, "p1")
    assert played == {"role": "MIDDLE", "champion": "Yasuo"}

    first = dict(result)
    second = dict(result)
    second["mainLaneOther"] = dict(result["mainLaneOther"])
    second["offLanes"] = {
        lane: dict(picks) for lane, picks in result["offLanes"].items()
    }
    blocks = aggregate_players([first, second])
    payload = champion_payload(REGIONS["KR"], "Yasuo", {"MID": blocks[("Yasuo", "MID")]}, "t")
    assert payload["region"] == "KR"
    assert payload["platform"] == "KR"
    assert payload["players"] == 2
    assert payload["mainLane"] == "MID"
    assert payload["mainLaneNext"][0]["champion"] == "Yone"
    assert payload["mainLaneNext"][0]["rate"] == 16.7
    assert payload["offLanes"]["TOP"]["picks"][0] == {
        "champion": "Sett",
        "games": 6,
        "rate": 75.0,
    }
    assert champion_filename("Kai'Sa") == "kaisa"
    assert champion_filename("Kaisa") == "kaisa"
    assert champion_filename("Nunu & Willump") == "nunuwillump"

    assert mastery_ratio([]) == 0.0
    assert mastery_ratio([{"points": 100}]) == float("inf")
    assert mastery_ratio([{"points": 300}, {"points": 100}]) == 3.0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        legacy = tmp_dir / "legacy.json"
        write_json(legacy, {"qualified": True, "matchesRequested": 40})
        assert load_player_record(legacy) is None
        assert should_skip(legacy, False, 24, 40) is False

        current = tmp_dir / "current.json"
        write_json(
            current,
            {
                "puuid": "p1",
                "tier": "MASTER",
                "fetchedAt": datetime.now(timezone.utc).isoformat(),
                "matchesRequested": 40,
                "games": games,
            },
        )
        assert should_skip(current, False, 24, 40) is True
        assert should_skip(current, False, 24, 20) is False
        record = load_player_record(current)
        assert classify_player(record, 15, 0.5)["qualified"] is True
        assert classify_player(record, 15, 0.9)["qualified"] is False

        players_dir = tmp_dir / "players"
        mid = lambda champion, n: [{"role": "MIDDLE", "champion": champion}] * n
        write_json(players_dir / "otp.json", {
            "puuid": "otp", "tier": "MASTER", "games": mid("Yasuo", 17) + mid("Yone", 3),
        })
        write_json(players_dir / "main.json", {
            "puuid": "main", "tier": "MASTER",
            "games": mid("Yasuo", 10) + mid("Ahri", 6) + mid("Zed", 4),
        })
        out = tmp_dir / "jp"
        counts = rebuild_champion_files(
            REGIONS["JP"], players_dir, out, 40, 15, {"main": 0.4, "otp": 0.8}
        )
        assert counts == {"main": 1, "otp": 1}
        main_yasuo = read_json(out / "main" / "champions" / "yasuo.json")
        otp_yasuo = read_json(out / "otp" / "champions" / "yasuo.json")
        assert (main_yasuo["profile"], main_yasuo["players"]) == ("main", 2)
        assert (otp_yasuo["profile"], otp_yasuo["players"], otp_yasuo["minRate"]) == ("otp", 1, 0.8)
        assert read_json(out / "otp" / "index.json")["profile"] == "otp"

        data_dir = tmp_dir / "data"
        write_json(data_dir / "jp" / "players" / "a.json", {"puuid": "a", "tier": "MASTER", "games": mid("Yasuo", 20)})
        write_json(data_dir / "na" / "players" / "b.json", {"puuid": "b", "tier": "MASTER", "games": mid("Yasuo", 18) + mid("Yone", 2)})
        counts = rebuild_combined(data_dir, 40, 15, {"main": 0.4, "otp": 0.8})
        assert counts == {"main": 1, "otp": 1}
        combined = read_json(data_dir / "all" / "main" / "champions" / "yasuo.json")
        assert (combined["region"], combined["players"]) == ("ALL", 2)
    print("self-test ok")


def main():
    parser = argparse.ArgumentParser(description="マスター以上の OTP サブレーン統計を収集する")
    parser.add_argument("--region", choices=sorted(REGIONS), default="JP", help="対象リージョン")
    parser.add_argument("--matches", type=int, default=40, help="プレイヤーあたりの直近ランクソロ試合数（最大 200）")
    parser.add_argument("--limit", type=int, default=0, help="今回試合を取る人数。0 は未処理の全員")
    parser.add_argument(
        "--mastery-ratio",
        type=float,
        default=1.5,
        help="熟練度 1 位が 2 位の何倍以上なら試合を取るか。0 で絞り込みなし",
    )
    parser.add_argument(
        "--screen-only",
        action="store_true",
        help="熟練度だけ取り、しきい値ごとの候補人数を表示する",
    )
    parser.add_argument(
        "--tiers",
        nargs="+",
        choices=TIERS,
        default=list(TIERS),
        help="対象のランク。例: --tiers CHALLENGER GRANDMASTER",
    )
    parser.add_argument("--min-main-games", type=int, default=15, help="メインレーンの最低試合数")
    parser.add_argument("--main-rate", type=float, default=0.4, help="メイン集計のしきい値（メインレーンでの使用率）")
    parser.add_argument("--otp-rate", type=float, default=0.8, help="OTP 集計のしきい値（メインレーンでの使用率）")
    parser.add_argument("--max-age-hours", type=float, default=24, help="--refresh 時に試合を取り直すまでの時間")
    parser.add_argument(
        "--mastery-max-age-hours",
        type=float,
        default=168,
        help="--refresh 時に熟練度を取り直すまでの時間（熟練度はゆっくり変わるので既定 7 日）",
    )
    parser.add_argument("--min-interval", type=float, default=1.26, help="API リクエストの最短間隔（秒）")
    parser.add_argument("--refresh", action="store_true", help="既存のプレイヤーも期限が切れていれば取り直す")
    parser.add_argument("--rebuild-only", action="store_true", help="API を叩かず JSON を作り直す")
    parser.add_argument(
        "--combine",
        action="store_true",
        help="API を叩かず、全リージョンの合計を data/all/ に作る（--region は無視）",
    )
    parser.add_argument("--self-test", action="store_true", help="API を叩かず集計ロジックを確認する")
    parser.add_argument("--data-dir", default="", help="出力先。未指定ならスクリプト横の data/")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return
    if args.matches < 1 or args.matches > 200:
        print("--matches は 1 から 200 です。", file=sys.stderr)
        sys.exit(1)
    for option, rate in (("--main-rate", args.main_rate), ("--otp-rate", args.otp_rate)):
        if not 0 < rate <= 1:
            print("{} は 0 より大きく 1 以下です。".format(option), file=sys.stderr)
            sys.exit(1)
    rates = profile_rates(args)
    if args.mastery_ratio < 0:
        print("--mastery-ratio は 0 以上です。", file=sys.stderr)
        sys.exit(1)

    region = REGIONS[args.region]
    root = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir) if args.data_dir else root / "data"
    output_dir = data_dir / region.code.lower()
    player_dir = output_dir / "players"
    cache_dir = data_dir / "cache" / "matches"
    player_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.combine:
        counts = rebuild_combined(data_dir, args.matches, args.min_main_games, rates)
        print_counts(counts, data_dir / COMBINED.code.lower())
        return
    if args.rebuild_only:
        counts = rebuild_champion_files(
            region, player_dir, output_dir, args.matches, args.min_main_games, rates
        )
        print_counts(counts, output_dir)
        return

    api_key = os.environ.get("RIOT_API_KEY")
    if not api_key:
        print("環境変数 RIOT_API_KEY を設定してください。", file=sys.stderr)
        sys.exit(1)

    keep_awake()
    limiter = RateLimiter(args.min_interval)
    mastery = MasteryStore(output_dir / "mastery.json")
    exit_code = 0
    try:
        print("リージョン: {}".format(region.code), flush=True)
        ladder = fetch_ladder(
            region, [tier for tier in TIERS if tier in args.tiers], api_key, limiter
        )

        if args.screen_only:
            screen_ladder(region, ladder, mastery, args, api_key, limiter)
            report_screening(ladder, mastery, player_dir, args)
            return

        pending = [
            player
            for player in ladder
            if not should_skip(
                player_dir / (player["puuid"] + ".json"),
                args.refresh,
                args.max_age_hours,
                args.matches,
            )
        ]
        print(
            "対象 {} 人 / 収集済み {} 人 / 未収集 {} 人".format(
                len(ladder), len(ladder) - len(pending), len(pending)
            ),
            flush=True,
        )
        if args.mastery_ratio > 0:
            print(
                "熟練度 1 位が 2 位の {} 倍以上の人だけ試合を取ります".format(args.mastery_ratio),
                flush=True,
            )

        collected = 0
        screened_out = 0
        for index, player in enumerate(pending, start=1):
            if args.limit and collected >= args.limit:
                break
            if args.mastery_ratio > 0:
                entry = ensure_mastery(
                    region,
                    mastery,
                    player["puuid"],
                    args.refresh,
                    args.mastery_max_age_hours,
                    api_key,
                    limiter,
                )
                if mastery_ratio(entry["top"]) < args.mastery_ratio:
                    screened_out += 1
                    if screened_out % 100 == 0:
                        print(
                            "{}/{} 熟練度で除外 {} 人".format(index, len(pending), screened_out),
                            flush=True,
                        )
                    continue

            record = collect_player(region, player, args.matches, cache_dir, api_key, limiter)
            write_json(player_dir / (player["puuid"] + ".json"), record)
            collected += 1
            result = classify_player(record, args.min_main_games, args.main_rate)
            if result["qualified"]:
                detail = "{} {} {:.0%}{}".format(
                    result["champion"],
                    result["mainLane"],
                    result["mainLaneRate"],
                    " OTP" if result["mainLaneRate"] >= args.otp_rate else "",
                )
            else:
                detail = result["reason"]
            print(
                "{}/{} {} {}".format(index, len(pending), record["tier"], detail),
                flush=True,
            )
            if collected % 25 == 0:
                rebuild_champion_files(
                    region,
                    player_dir,
                    output_dir,
                    args.matches,
                    args.min_main_games,
                    rates,
                )
        print(
            "試合を取った人 {} 人 / 熟練度で除外 {} 人".format(collected, screened_out),
            flush=True,
        )
    except KeyboardInterrupt:
        print("中断しました。ここまでの JSON を書き出します。", flush=True)
    except RiotError as error:
        print(str(error), file=sys.stderr)
        exit_code = 1
    finally:
        mastery.save()
        if not args.screen_only:
            counts = rebuild_champion_files(
                region, player_dir, output_dir, args.matches, args.min_main_games, rates
            )
            print_counts(counts, output_dir)
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
