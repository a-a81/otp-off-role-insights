"""夜間処理をまとめて実行する。タスクスケジューラから毎日 4 時に起動する。

1. update_champion_names.py
2. collect_otp.py をリージョンごとに実行する。試合 API の上限はルーティング単位なので、
   同じルーティング（JP と KR は asia）は順番に、違うルーティング（NA は americas）は並行して動かす
3. collect_otp.py --combine で全リージョンの合計（data/all/）を作る
4. 集計ファイルを GitHub に commit / push する（gitPush が true のとき）
5. Healthchecks.io に開始・成功・失敗を送る。失敗と、時間内に終わらなかった場合はメールが届く

設定は nightly.local.json（Git に入れない）。nightly.example.json をコピーして作る。
ログは logs/ に残し、古いものは 30 回分を超えたら消す。

ターミナルから動かしたときは Ctrl+C で止められる。各リージョンはここまでの集計を
書き出してから止まり、次のリージョンには進まない。もう一度実行すれば続きから再開する。

例:
  python run_nightly.py            タスクスケジューラと同じ実行
  python run_nightly.py --no-ping  初回の長い収集を手で回すとき（Healthchecks に送らない）
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

from collect_otp import USER_AGENT, keep_awake

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
KEEP_RUNS = 30
ROUTING = {"JP": "asia", "KR": "asia", "NA": "americas"}


def has_console():
    if os.name != "nt":
        return True
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleWindow())


# タスクスケジューラ（pythonw）から動くときは子プロセスの窓を出さない。
# ターミナルから動かしたときは同じ窓を共有させ、Ctrl+C が収集側にも届くようにする。
NO_WINDOW = 0 if has_console() else getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Run:
    def __init__(self, stamp):
        self.stamp = stamp
        self.log_path = LOG_DIR / "{}-nightly.log".format(stamp)
        self.problems = []
        self.notes = []
        self.stopping = False
        self.lock = threading.Lock()

    def log(self, message):
        line = "{} {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
        with self.lock:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        # pythonw では標準出力がないので print は何もしない
        print(line, flush=True)

    def problem(self, message):
        with self.lock:
            self.problems.append(message)
        self.log("問題: " + message)

    def note(self, message):
        with self.lock:
            self.notes.append(message)
        self.log(message)

    def step_log(self, name):
        return LOG_DIR / "{}-{}.log".format(self.stamp, name)


def acquire_lock(path):
    """二重起動を防ぐ。ロックはプロセスが終われば自動で外れる。"""
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    regions = [entry for entry in config.get("regions", []) if entry.get("enabled", True)]
    for entry in regions:
        if entry.get("region") not in ROUTING:
            raise ValueError("未対応のリージョン: {}".format(entry.get("region")))
    config["regions"] = regions
    return config


def api_key_from(config):
    key = (config.get("riotApiKey") or "").strip()
    if key.startswith("RGAPI-") and "xxxx" not in key:
        return key
    return os.environ.get("RIOT_API_KEY", "")


def run_command(run, command, log_path, env):
    with open(log_path, "a", encoding="utf-8") as out:
        out.write("\n===== {} {} =====\n".format(datetime.now().isoformat(timespec="seconds"), " ".join(command)))
        out.flush()
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        ).returncode


def run_step(run, name, command, env):
    run.log("開始: {}".format(name))
    started = time.monotonic()
    code = run_command(run, command, run.step_log(name), env)
    minutes = (time.monotonic() - started) / 60
    if code == 0:
        run.log("完了: {}（{:.0f} 分）".format(name, minutes))
    else:
        tail = "\n".join("    " + line for line in last_lines(run.step_log(name), 3))
        run.problem("{} が終了コード {} で止まりました（{:.0f} 分）。ログ: {}\n{}".format(
            name, code, minutes, run.step_log(name).name, tail))
    return code


def last_lines(path, count):
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    return lines[-count:]


def last_line_with(path, marker):
    if not path.exists():
        return ""
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if marker in line]
    return lines[-1].strip() if lines else ""


def collect_chain(run, entries, config, env):
    for entry in entries:
        if run.stopping:
            break
        code = entry["region"]
        name = "collect-" + code.lower()
        command = [
            sys.executable,
            str(ROOT / "collect_otp.py"),
            "--region",
            code,
            "--tiers",
            *entry.get("tiers", ["CHALLENGER", "GRANDMASTER", "MASTER"]),
            *config.get("collectArgs", []),
        ]
        run_step(run, name, command, env)
        summary = last_line_with(run.step_log(name), "チャンピオン JSON")
        if summary:
            run.note("{}: {}".format(code, summary))


def collect_all(run, config, env):
    groups = {}
    for entry in config["regions"]:
        groups.setdefault(ROUTING[entry["region"]], []).append(entry)
    threads = [
        threading.Thread(target=collect_chain, args=(run, entries, config, env), name=routing)
        for routing, entries in groups.items()
    ]
    for thread in threads:
        thread.start()
    # 待ち時間を区切らないと Windows では Ctrl+C を受け取れない
    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(0.5)
    except KeyboardInterrupt:
        run.stopping = True
        run.log("中断を受け付けました。各リージョンがここまでの集計を書き出すのを待っています")
        for thread in threads:
            while thread.is_alive():
                try:
                    thread.join(0.5)
                except KeyboardInterrupt:
                    pass
        raise


def publish(run, env):
    git = shutil.which("git")
    if not git:
        run.problem("git が見つからないので push できませんでした")
        return
    if not (ROOT / ".git").exists():
        run.problem("このフォルダーが Git リポジトリではないので push できませんでした")
        return

    paths = []
    if (ROOT / "data" / "champion_names.json").exists():
        paths.append("data/champion_names.json")
    for region_dir in sorted((ROOT / "data").iterdir()):
        for profile in ("main", "otp"):
            if (region_dir / profile).is_dir():
                paths.append("data/{}/{}".format(region_dir.name, profile))
    if not paths:
        run.note("push する集計ファイルがありません")
        return

    log_path = run.step_log("git")
    if run_command(run, [git, "add", "--all", "--", *paths], log_path, env) != 0:
        run.problem("git add に失敗しました。ログ: " + log_path.name)
        return
    if run_command(run, [git, "diff", "--cached", "--quiet"], log_path, env) == 0:
        run.note("集計ファイルに変更がないので push しませんでした")
        return
    message = "Nightly data update {}".format(datetime.now().strftime("%Y-%m-%d"))
    if run_command(run, [git, "commit", "-m", message], log_path, env) != 0:
        run.problem("git commit に失敗しました。ログ: " + log_path.name)
        return
    if run_command(run, [git, "push"], log_path, env) != 0:
        run.problem("git push に失敗しました。ログ: " + log_path.name)
        return
    run.note("GitHub に push しました")


def ping(run, url, suffix, body=""):
    if not url:
        return
    request = urllib.request.Request(
        url.rstrip("/") + suffix,
        data=body.encode("utf-8")[:90000],
        headers={"User-Agent": USER_AGENT},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15):
                return
        except OSError as error:
            run.log("Healthchecks への送信に失敗（{} 回目）: {}".format(attempt + 1, error))
            time.sleep(5)


def remove_old_logs():
    stamps = sorted({path.name[:13] for path in LOG_DIR.glob("*.log")})
    for stamp in stamps[:-KEEP_RUNS]:
        for path in LOG_DIR.glob(stamp + "-*.log"):
            path.unlink()


def summary_text(run, started):
    minutes = (time.monotonic() - started) / 60
    lines = ["夜間処理 {}（{:.0f} 分）".format(run.stamp, minutes)]
    if run.problems:
        lines.append("")
        lines.append("問題:")
        lines.extend("- " + message for message in run.problems)
    if run.notes:
        lines.append("")
        lines.extend("- " + message for message in run.notes)
    lines.append("")
    lines.append("ログ: " + str(run.log_path))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="夜間処理をまとめて実行する")
    parser.add_argument("--config", default=str(ROOT / "nightly.local.json"), help="設定ファイル")
    parser.add_argument("--no-ping", action="store_true", help="Healthchecks.io に送らない")
    parser.add_argument("--no-push", action="store_true", help="GitHub に push しない")
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    run = Run(datetime.now().strftime("%Y%m%d-%H%M"))
    started = time.monotonic()
    url = ""
    try:
        config = load_config(Path(args.config))
        url = "" if args.no_ping else (config.get("healthcheckUrl") or "").strip()

        lock = acquire_lock(LOG_DIR / "nightly.lock")
        if lock is None:
            run.problem("前回の夜間処理がまだ動いているので、今回は始めませんでした")
            ping(run, url, "/fail", summary_text(run, started))
            sys.exit(1)

        keep_awake()
        ping(run, url, "/start")
        run.log("リージョン: " + ", ".join(entry["region"] for entry in config["regions"]))

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        api_key = api_key_from(config)
        if not api_key:
            run.problem("Riot の API キーが設定されていません（nightly.local.json の riotApiKey）")
        else:
            env["RIOT_API_KEY"] = api_key
            run_step(run, "champion-names", [sys.executable, str(ROOT / "update_champion_names.py")], env)
            collect_all(run, config, env)
            combine_command = [sys.executable, str(ROOT / "collect_otp.py"), "--combine", *config.get("collectArgs", [])]
            if run_step(run, "combine", combine_command, env) == 0:
                summary = last_line_with(run.step_log("combine"), "チャンピオン JSON")
                if summary:
                    run.note("ALL: " + summary)
            if config.get("gitPush") and not args.no_push:
                publish(run, env)
        remove_old_logs()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        run.problem("手動で中断しました。もう一度実行すれば続きから再開します")
    except Exception:
        run.problem("想定外のエラー:\n" + traceback.format_exc())

    text = summary_text(run, started)
    run.log("終了\n" + text)
    if run.problems:
        ping(run, url, "/fail", text)
        sys.exit(1)
    ping(run, url, "", text)


if __name__ == "__main__":
    main()
