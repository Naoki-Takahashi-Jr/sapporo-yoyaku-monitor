# -*- coding: utf-8 -*-
"""札幌市公共施設予約情報システム 空き状況監視・Discord通知

仕様: docs/要件定義書.md (v0.4) / API詳細: docs/調査報告_API構造と監視対象施設.md

実行例:
    python src/monitor.py                  # 通常実行（tier自動判定・通知あり・状態保存あり）
    python src/monitor.py --dry-run        # 通知・状態保存なしで動作確認
    python src/monitor.py --tier 1         # Tier 1 のみ
    python src/monitor.py --only 0025      # 特定施設のみ（カンマ区切り可）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

JST = timezone(timedelta(hours=9))
BASE_URL = "https://yoyaku.harp.lg.jp"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.yaml"
STATE_PATH = ROOT / "state" / "snapshot.json"

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_JP = "月火水木金土日"

STATUS_LABELS = {
    "A01": "予約申込可",
    "L01": "抽選申込可",
    "L02": "抽選申込可",
    "A03": "電話受付",
    "U10": "窓口受付",
    "A02": "空き表示のみ",
}

ERROR_NOTIFY_THRESHOLD = 3   # 連続何回の実行失敗でエラー通知するか
TIER2_INTERVAL_MIN = 55      # 前回のTier2実行からこの分数が経過していたらTier2も実行


def log(msg):
    print(f"[{datetime.now(JST).strftime('%H:%M:%S')}] {msg}", flush=True)


def now_jst():
    return datetime.now(JST)


def parse_hhmm(s):
    """'HH:MM' を0時からの分に変換（'24:00'=1440に対応）"""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def parse_range(s):
    """'HH:MM-HH:MM' → (開始分, 終了分)"""
    a, b = s.split("-")
    return parse_hhmm(a), parse_hhmm(b)


def in_active_hours(cfg, now):
    start, end = parse_range(cfg["active_hours"])
    cur = now.hour * 60 + now.minute
    return start <= cur < end


def slot_in_windows(windows, date_str, start_hhmmss, end_hhmmss):
    """枠(date, start-end)が曜日別の通知時間帯と一部でも重なるか"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    ranges = windows.get(WEEKDAY_KEYS[d.weekday()]) or []
    s = parse_hhmm(start_hhmmss[:5])
    e = parse_hhmm(end_hhmmss[:5])
    if e <= s:  # 日跨ぎ・終了0:00表記は24:00扱い
        e += 24 * 60
    for r in ranges:
        ws, we = parse_range(r)
        if s < we and e > ws:
            return True
    return False


def build_payload(purpose_ids, purposes_cfg, start_date, end_date):
    ups = []
    for pid in purpose_ids:
        p = purposes_cfg[pid]
        ups.append({
            "groupId": "utilizationPurpose",
            "key": str(pid),
            "utilizationPurposeId": pid,
            "utilizationPurposeCategoryName": p["category"],
            "utilizationPurposeName": p["name"],
            "groupName": "利用目的",
            "itemId": f"utilizationPurpose{pid}",
        })
    return {
        "startDate": start_date.strftime("%Y-%m-%dT00:00:00+09:00"),
        "endDate": end_date.strftime("%Y-%m-%dT00:00:00+09:00"),
        "roomCode": None,
        "courtSize": None,
        "utilizationPurpose": ups,
        "usePeople": None,
        "room": None,
        "toggleTimeType": False,
        "usageTimes": None,
        "usagePeriodOfTime": None,
        "allowInternetRequest": None,
        "requestId": None,
    }


def request_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/sapporo/FacilitySearch/Index",
    }


def fetch_day(lgc, fc, payload):
    """GetDay APIを呼ぶ。失敗時は1回だけ間隔を空けてリトライ（要件5.2）。

    対象システムは高頻度アクセス時に一時的な500を返すことがあり、
    セッションを作り直すと通ることが多いため、試行ごとに新規セッションを使う。
    """
    url = f"{BASE_URL}/sapporo/FacilityAvailability/GetDay/{lgc}/{fc}"
    last_err = None
    for attempt in range(2):
        if attempt > 0:
            time.sleep(20)
        try:
            with requests.Session() as session:
                session.headers.update(request_headers())
                r = session.post(url, json=payload, timeout=30)
                if r.status_code != 200:
                    last_err = RuntimeError(f"HTTP {r.status_code}")
                    continue
                ct = r.headers.get("Content-Type", "")
                if "json" not in ct:
                    # HTMLが返る＝WAFチャレンジ or 混雑ページの可能性
                    raise RuntimeError(f"非JSON応答 (Content-Type: {ct}) — WAF/混雑の可能性")
                data = r.json()
                if "rooms" not in data or "timeFrames" not in data:
                    raise RuntimeError("想定外のレスポンス構造（API仕様変更の可能性）")
                return data
        except (requests.RequestException, ValueError) as e:
            last_err = e
    raise last_err


def extract_slots(fc, data):
    """GetDayレスポンス → {キー: 枠情報} に展開

    キー: fc|室場コード|コートコード|日付|時間枠ID

    時間枠には通常枠（usageTimeFrames）のほかに、複数の時間帯をまとめて
    1件として予約する結合枠（joinUsageTimeFrames。例: 午前+午後通し）がある。
    結合枠の時刻は構成枠の min(開始)〜max(終了) で解決し、is_join=True を付ける。
    どこにも定義がない枠は resolved=False とし、通知対象から除外する
    （00:00-00:00 のまま通知・終日扱いでフィルタを素通りするのを防ぐ）。
    """
    tf = {}
    joins = {}
    for ts in data.get("timeFrames") or []:
        for f in ts.get("usageTimeFrames") or []:
            tf[f["usageTimeFrameId"]] = (
                (f.get("usageStartTime") or "00:00:00"),
                (f.get("usageEndTime") or "00:00:00"),
            )
        for f in ts.get("joinUsageTimeFrames") or []:
            joins[f["usageTimeFrameId"]] = f.get("joinUsageTimeFrameIds") or []
    for fid, parts in joins.items():
        times = [tf[p] for p in parts if p in tf]
        if times and fid not in tf:
            # HH:MM:SS の固定長文字列なので辞書順比較で時刻比較になる
            tf[fid] = (min(t[0] for t in times), max(t[1] for t in times))

    slots = {}
    for room in data.get("rooms") or []:
        for court in room.get("courts") or []:
            for day in court.get("dayBooks") or []:
                date = (day.get("usageDate") or "")[:10]
                for ut in day.get("usageTimes") or []:
                    fid = ut.get("usageTimeFrameId")
                    resolved = fid in tf
                    start, end = tf.get(fid, ("00:00:00", "00:00:00"))
                    key = f"{fc}|{room['roomCode']}|{court['courtCode']}|{date}|{fid}"
                    slots[key] = {
                        "status": ut.get("statusType"),
                        "date": date,
                        "start": start,
                        "end": end,
                        "room": room.get("roomName") or "",
                        "court": court.get("courtName") or "",
                        "is_join": fid in joins,
                        "resolved": resolved,
                    }
    return slots


def is_notifiable_slot(info):
    """通知候補にしてよい枠か。

    - 時刻未解決の枠は除外（誤った終日扱いを防ぐ）
    - 結合枠は除外。結合枠が空く時は構成枠（通常枠）も同時に空くため通知が
      重複するだけで、通常枠側で過不足なく通知できる
    """
    return info.get("resolved", True) and not info.get("is_join", False)


def diff_new_availability(prev_slots, curr_slots, notify_statuses):
    """前回スナップショットと比較し「申込不可→申込可」に変化した枠を返す。

    前回に存在しないキー（初回・新規日付・時間枠ID変更）は通知しない（要件F-2）。
    """
    out = []
    for key, info in curr_slots.items():
        prev_status = prev_slots.get(key)
        if prev_status is None:
            continue
        if prev_status not in notify_statuses and info["status"] in notify_statuses:
            out.append((key, info))
    return out


def annotate_consecutive(curr_slots, hits, notify_statuses, min_minutes):
    """連続予約可能時間が min_minutes 以上の塊に属する枠だけ残す（要件F-8）。

    同一コート・同一日の空きステータス枠を開始時刻順に連結し（前枠の終了＝次枠の開始）、
    新たに空いた枠（hits）がその塊に含まれ、塊の合計が min_minutes 以上なら通知対象。
    塊の範囲は info["block"] に付与する（通知文言用）。
    """
    if min_minutes <= 0 or not hits:
        return hits

    # コート×日付ごとに空き枠を集める（キー: fc|室場|コート|日付）
    # 結合枠・未解決枠は連結計算を壊すため除外（通常枠のみで連続性を判定）
    groups = {}
    for key, info in curr_slots.items():
        if info["status"] in notify_statuses and is_notifiable_slot(info):
            g = "|".join(key.split("|")[:4])
            groups.setdefault(g, []).append(info)

    # 各グループ内で時間的に連続する塊（run）を構築
    runs_by_group = {}
    for g, infos in groups.items():
        runs, cur = [], None
        for info in sorted(infos, key=lambda x: parse_hhmm(x["start"][:5])):
            s = parse_hhmm(info["start"][:5])
            e = parse_hhmm(info["end"][:5])
            if e <= s:  # 日跨ぎ枠（深夜枠等）
                e += 24 * 60
            if cur and s == cur["end_min"]:
                cur["end_min"] = e
                cur["end"] = info["end"]
            else:
                if cur:
                    runs.append(cur)
                cur = {"start_min": s, "end_min": e,
                       "start": info["start"], "end": info["end"]}
        if cur:
            runs.append(cur)
        runs_by_group[g] = runs

    out = []
    for key, info in hits:
        g = "|".join(key.split("|")[:4])
        s = parse_hhmm(info["start"][:5])
        run = next((r for r in runs_by_group.get(g, [])
                    if r["start_min"] <= s < r["end_min"]), None)
        if run and (run["end_min"] - run["start_min"]) >= min_minutes:
            info["block"] = (run["start"], run["end"])
            out.append((key, info))
    return out


def format_item_line(info):
    d = datetime.strptime(info["date"], "%Y-%m-%d")
    wd = WEEKDAY_JP[d.weekday()]
    label = STATUS_LABELS.get(info["status"], info["status"])
    place = " ".join(x for x in [info["room"], info["court"]] if x)
    block = info.get("block")
    if block and (block[0] != info["start"] or block[1] != info["end"]):
        label += f"／連続枠 {block[0][:5]}–{block[1][:5]}"
    return f"{d.month}/{d.day}（{wd}） {info['start'][:5]}–{info['end'][:5]} {place}（{label}）"


def build_notification_embeds(groups, lgc):
    """施設ごとにfieldを作る。Discordの制限: field値1024字/25field/embed

    groups: [(facility設定dict, [(key, 枠情報), ...]), ...]
    """
    fields = []
    for fac, items in groups:
        items.sort(key=lambda x: (x[1]["date"], x[1]["start"], x[1]["room"]))
        url = f"{BASE_URL}/sapporo/FacilityAvailability/Index/{lgc}/{fac['fc']}?u={fac['purposes'][0]}"
        lines = [format_item_line(info) for _, info in items]
        lines.append(f"[→ 空き状況を開く]({url})")
        value, count = "", 0
        name = f"{fac['name']}（{fac['area']}）"
        for line in lines:
            if len(value) + len(line) + 1 > 1000:
                fields.append({"name": name if count == 0 else name + "（続き）", "value": value, "inline": False})
                value, count = "", count + 1
            value += line + "\n"
        if value:
            fields.append({"name": name if count == 0 else name + "（続き）", "value": value, "inline": False})

    embeds = []
    for i in range(0, len(fields), 25):
        embeds.append({
            "title": "🟢 空きが出ました！" if i == 0 else "（続き）",
            "color": 0x17AE9A,
            "fields": fields[i:i + 25],
            "footer": {"text": "札幌市公共施設予約 空き監視"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    return embeds


def send_discord(webhook_url, embeds):
    """embeds（最大10/メッセージ）を分割送信"""
    for i in range(0, len(embeds), 10):
        body = {"embeds": embeds[i:i + 10]}
        for attempt in range(2):
            r = requests.post(webhook_url, json=body, timeout=15)
            if r.status_code == 429 and attempt == 0:
                wait = float(r.headers.get("Retry-After", "2"))
                time.sleep(min(wait, 10))
                continue
            r.raise_for_status()
            break
        time.sleep(0.5)


def send_error_notification(webhook_url, consecutive, detail):
    repo = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    actions_link = f"\n[→ Actionsログを確認]({server}/{repo}/actions)" if repo else ""
    embeds = [{
        "title": "🔴 監視エラー",
        "color": 0xCA4949,
        "description": (
            f"空き状況の取得に **{consecutive}回連続** で失敗しています。\n"
            f"サイトメンテナンス・WAFブロック・API仕様変更の可能性があります。\n"
            f"直近のエラー: {detail}{actions_link}"
        ),
        "footer": {"text": "札幌市公共施設予約 空き監視"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]
    send_discord(webhook_url, embeds)


def send_recovery_notification(webhook_url):
    embeds = [{
        "title": "✅ 監視が復旧しました",
        "color": 0x17AE9A,
        "description": "空き状況の取得が再び成功しています。",
        "footer": {"text": "札幌市公共施設予約 空き監視"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]
    send_discord(webhook_url, embeds)


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log("WARN: スナップショットが壊れているため初期化します（この回は通知なし）")
    return {"meta": {}, "slots": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def select_facilities(cfg, state, args, now):
    """実行対象の施設リストと、Tier2を実行したかどうかを返す"""
    facilities = cfg["facilities"]
    if args.only:
        only = {x.strip() for x in args.only.split(",")}
        return [f for f in facilities if f["fc"] in only], False
    if args.tier == "1":
        return [f for f in facilities if f["tier"] == 1], False
    if args.tier == "2":
        return [f for f in facilities if f["tier"] == 2], True
    if args.tier == "all":
        return list(facilities), True

    # auto: Tier1は毎回。Tier2は前回実行からTIER2_INTERVAL_MIN分以上経過していたら実行
    last = state["meta"].get("last_tier2_run")
    run_tier2 = True
    if last:
        try:
            elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
            run_tier2 = elapsed >= TIER2_INTERVAL_MIN
        except ValueError:
            pass
    if run_tier2:
        return list(facilities), True
    return [f for f in facilities if f["tier"] == 1], False


def main():
    ap = argparse.ArgumentParser(description="札幌市公共施設予約 空き状況監視")
    ap.add_argument("--dry-run", action="store_true", help="通知送信・状態保存をしない")
    ap.add_argument("--tier", choices=["auto", "1", "2", "all"], default="auto")
    ap.add_argument("--only", help="施設コード指定（カンマ区切り）")
    ap.add_argument("--limit", type=int, help="先頭N施設のみ（動作確認用）")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    now = now_jst()

    if not in_active_hours(cfg, now):
        log(f"稼働時間外（{cfg['active_hours']} JST）のため終了")
        return 0

    state = load_state()
    prev_slots = state.get("slots") or {}
    first_run = not prev_slots

    targets, ran_tier2 = select_facilities(cfg, state, args, now)
    if args.limit:
        targets = targets[: args.limit]

    lgc = cfg["local_government_code"]
    purposes_cfg = cfg["purposes"]
    notify_statuses = set(cfg["notify_statuses"])
    windows = cfg["notify_windows"]
    min_consec = int(cfg.get("min_consecutive_minutes", 0))
    interval = float(cfg.get("request_interval_seconds", 1.5))
    start_date = now.date()
    end_date = start_date + timedelta(days=int(cfg["date_range_days"]))
    today_str = start_date.isoformat()

    log(f"対象 {len(targets)}施設（tier2込み: {ran_tier2}） / 期間 {start_date}〜{end_date}"
        + (" / DRY-RUN" if args.dry_run else ""))

    ok, failed, notify_items = 0, [], {}
    fetched_slots = {}
    fetched_fcs = set()
    last_error = ""

    for i, fac in enumerate(targets):
        if ok == 0 and len(failed) >= 8:
            log("先頭から連続で失敗しているため中断（メンテナンス・障害の可能性）")
            break
        if i > 0:
            time.sleep(interval)
        payload = build_payload(fac["purposes"], purposes_cfg, now, end_date)
        try:
            data = fetch_day(lgc, fac["fc"], payload)
            slots = extract_slots(fac["fc"], data)
            unresolved = sum(1 for v in slots.values() if not v.get("resolved", True))
            if unresolved:
                log(f"WARN {fac['name']}: 時刻を解決できない枠が{unresolved}件（通知対象外として扱う）")
            fetched_slots.update(slots)
            fetched_fcs.add(fac["fc"])
            ok += 1
        except Exception as e:
            last_error = f"{fac['name']}: {e}"
            log(f"NG {fac['name']}: {e}")
            failed.append(fac["name"])
            continue

        new_av = diff_new_availability(prev_slots, slots, notify_statuses)
        hit = [(k, v) for k, v in new_av
               if is_notifiable_slot(v)
               and slot_in_windows(windows, v["date"], v["start"], v["end"])]
        hit = annotate_consecutive(slots, hit, notify_statuses, min_consec)
        if hit:
            key = (fac["fc"], fac["name"], fac["area"])
            notify_items.setdefault(key, []).extend(hit)
            log(f"OK {fac['name']}: {len(slots)}枠 / 新規空き {len(new_av)}件（通知対象 {len(hit)}件）")
        else:
            log(f"OK {fac['name']}: {len(slots)}枠 / 新規空き {len(new_av)}件")

    # --- 通知 ---
    if notify_items and not first_run:
        fac_by_fc = {f["fc"]: f for f in cfg["facilities"]}
        groups = [
            ({"fc": fc, "name": name, "area": area, "purposes": fac_by_fc[fc]["purposes"]}, items)
            for (fc, name, area), items in notify_items.items()
        ]
        embeds = build_notification_embeds(groups, lgc)
        total = sum(len(v) for v in notify_items.values())
        if args.dry_run or not webhook:
            log(f"[dry-run] 通知対象 {total}件（送信せず）")
            for (fc, name, area), items in notify_items.items():
                for _, info in items:
                    log(f"  -> {name}: {format_item_line(info)}")
        else:
            send_discord(webhook, embeds)
            log(f"Discord通知を送信: {total}件")
    elif notify_items and first_run:
        log(f"初回実行のため通知をスキップ（{sum(len(v) for v in notify_items.values())}件検知）")

    # --- 実行結果の判定とエラー通知 ---
    run_failed = ok == 0 or (len(targets) > 0 and len(failed) >= max(1, len(targets) // 2))
    meta = state["meta"]
    if run_failed:
        meta["consecutive_failures"] = int(meta.get("consecutive_failures", 0)) + 1
        log(f"実行失敗扱い（成功{ok}/{len(targets)}） 連続失敗: {meta['consecutive_failures']}")
        if (meta["consecutive_failures"] >= ERROR_NOTIFY_THRESHOLD
                and not meta.get("error_notified")
                and webhook and not args.dry_run):
            send_error_notification(webhook, meta["consecutive_failures"], last_error)
            meta["error_notified"] = True
            log("エラー通知を送信")
    else:
        if meta.get("error_notified") and webhook and not args.dry_run:
            send_recovery_notification(webhook)
            log("復旧通知を送信")
        meta["consecutive_failures"] = 0
        meta["error_notified"] = False

    # --- スナップショット更新 ---
    # 取得成功した施設のキーは総入れ替え、失敗・対象外施設の前回値は保持、過去日付は削除
    new_slots = {}
    for key, status in prev_slots.items():
        fc, _, _, date, _ = key.split("|", 4)
        if fc in fetched_fcs:
            continue
        if date < today_str:
            continue
        new_slots[key] = status
    for key, info in fetched_slots.items():
        new_slots[key] = info["status"]
    state["slots"] = new_slots
    if ran_tier2 and not run_failed:
        meta["last_tier2_run"] = now.isoformat()

    if args.dry_run:
        log("[dry-run] 状態保存をスキップ")
    else:
        save_state(state)
        log(f"スナップショット保存: {len(new_slots)}枠")

    log(f"完了 成功{ok} 失敗{len(failed)}" + (f" ({', '.join(failed[:5])})" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
