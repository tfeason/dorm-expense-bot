import json
import os
import re
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
DEFAULT_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
DATA_FILE = "dorm_data.json"
API_BASE = "https://discord.com/api/v10"
TW_TZ = timezone(timedelta(hours=8))
CRON_INTERVAL_MINUTES = 5


def build_read_progress_bar(now_tw=None):
    if now_tw is None:
        now_tw = datetime.now(TW_TZ)

    interval = CRON_INTERVAL_MINUTES
    seconds_in_cycle = interval * 60
    elapsed_seconds = ((now_tw.minute % interval) * 60) + now_tw.second
    ratio = min(max(elapsed_seconds / seconds_in_cycle, 0.0), 1.0)
    pct = int(round(ratio * 100))

    bar_len = 12
    filled = min(bar_len, int(round(ratio * bar_len)))
    bar = "█" * filled + "░" * (bar_len - filled)

    next_min_total = ((now_tw.minute // interval) + 1) * interval
    next_run = now_tw.replace(second=0, microsecond=0) + timedelta(minutes=next_min_total - now_tw.minute)
    rem_minutes = max(0, int((next_run - now_tw).total_seconds() // 60))

    read_time_str = now_tw.strftime("%m/%d %H:%M:%S")
    next_time_str = next_run.strftime("%H:%M")
    return f"⏳ **讀取進度**：`[{bar}] {pct}%`（讀取於 `{read_time_str}`｜下次約 `{next_time_str}`，約 {rem_minutes} 分後）"


def load_data():
    if not os.path.exists(DATA_FILE):
        return {"channels": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if "channels" not in raw:
        channels = {}
        if DEFAULT_CHANNEL_ID:
            channels[DEFAULT_CHANNEL_ID] = {
                "expenses": raw.get("expenses", []),
                "settlement_rule": raw.get("settlement_rule", "equal"),
                "member_count": raw.get("member_count", 0),
                "last_processed_message_id": raw.get("last_processed_message_id"),
                "last_daily_noon_date": "",
            }
        return {"channels": channels}

    return raw


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_channel_ledger(data, channel_id):
    channels = data.setdefault("channels", {})
    ledger = channels.setdefault(
        channel_id,
        {
            "expenses": [],
            "settlement_rule": "equal",
            "member_count": 0,
            "last_processed_message_id": None,
            "last_daily_noon_date": "",
        },
    )
    ledger.setdefault("expenses", [])
    ledger.setdefault("settlement_rule", "equal")
    ledger.setdefault("member_count", 0)
    ledger.setdefault("last_processed_message_id", None)
    ledger.setdefault("last_daily_noon_date", "")
    return ledger


def discord_request(method, path, payload=None):
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bot {TOKEN}",
        "User-Agent": "DormExpenseBot (https://github.com, 2.2)",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"[HTTP {e.code}] {method} {path}: {err_body}")
        return None


def get_target_channels(data):
    target_ids = set(data.get("channels", {}).keys())
    if DEFAULT_CHANNEL_ID:
        target_ids.add(DEFAULT_CHANNEL_ID)

    guilds = discord_request("GET", "/users/@me/guilds") or []
    for g in guilds:
        gid = g.get("id")
        if not gid:
            continue
        ch_list = discord_request("GET", f"/guilds/{gid}/channels") or []
        for ch in ch_list:
            if ch.get("type") == 0:
                name = ch.get("name", "")
                if "記帳" in name or "分帳" in name:
                    target_ids.add(ch["id"])

    return sorted(target_ids)


def fetch_messages(channel_id, after_id=None):
    query = f"/channels/{channel_id}/messages?limit=100"
    if after_id:
        query += f"&after={after_id}"
    msgs = discord_request("GET", query)
    if not isinstance(msgs, list):
        return []
    msgs.sort(key=lambda m: int(m["id"]))
    return msgs


def send_message(channel_id, content):
    if not content:
        return
    chunks = []
    current = ""
    for line in content.split("\n"):
        if len(current) + len(line) + 1 > 1850:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        discord_request("POST", f"/channels/{channel_id}/messages", {"content": chunk})


def calculate_transfers(balances):
    debtors = [[p, -amt] for p, amt in balances.items() if amt < -0.05]
    creditors = [[p, amt] for p, amt in balances.items() if amt > 0.05]
    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)

    transfers = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        d_name, d_owe = debtors[i]
        c_name, c_get = creditors[j]
        pay = min(d_owe, c_get)
        if pay > 0.05:
            transfers.append(f"  - **{d_name}** 轉給 **{c_name}**：**{pay:.1f} 元**")
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] <= 0.05:
            i += 1
        if creditors[j][1] <= 0.05:
            j += 1
    return transfers


def build_daily_noon_report(ledger, now_tw):
    expenses = ledger["expenses"]
    if not expenses:
        return None

    totals = {}
    counts = {}
    grand_total = 0.0
    for item in expenses:
        payer = item["payer"]
        amount = float(item["amount"])
        totals[payer] = totals.get(payer, 0.0) + amount
        counts[payer] = counts.get(payer, 0) + 1
        grand_total += amount

    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    top_person, top_amount = ranked[0]

    lines = [
        f"🕛 **每日 12:00 吃土榜＆個人財務報表**（{now_tw.strftime('%Y/%m/%d')}）",
        f"• 本輪累積總支出：**{grand_total:g} 元**（共 {len(expenses)} 筆）",
        f"• 🫠 **今日最吃土（花最多錢）**：**{top_person}**（已支出 **{top_amount:g} 元**）",
        "• **個人財務報表**：",
    ]
    medals = ["🥇", "🥈", "🥉"]
    for idx, (person, paid) in enumerate(ranked):
        badge = medals[idx] if idx < len(medals) else "•"
        share = (paid / grand_total * 100) if grand_total > 0 else 0.0
        lines.append(f"  {badge} **{person}**：支出 **{paid:g} 元**（{counts[person]} 筆，佔 {share:.1f}%）")

    return "\n".join(lines)


def format_report(ledger):
    expenses = ledger["expenses"]
    if not expenses:
        return (
            "📊 **目前沒有未結清的記帳紀錄**\n"
            "直接輸入金額（例如 `280` 或 `280 晚餐`）即可新增一筆支出，輸入 `!說明` 查看所有指令。"
        )

    totals = {}
    grand_total = 0.0
    for item in expenses:
        payer = item["payer"]
        amount = float(item["amount"])
        totals[payer] = totals.get(payer, 0.0) + amount
        grand_total += amount

    configured_count = int(ledger.get("member_count") or 0)
    active_count = len(totals)
    headcount = configured_count if configured_count >= active_count else active_count

    lines = [
        "📊 **宿舍分帳結算報表**",
        f"• 未結清筆數：{len(expenses)} 筆",
        f"• 總支出金額：**{grand_total:g} 元**",
        f"• 分攤人數：{headcount} 人"
        + (f"（已設定固定 {configured_count} 人）" if configured_count > 0 else "（依目前墊付人數自動計算）"),
        "• 各人已墊付：",
    ]
    for person, paid in totals.items():
        lines.append(f"  - {person}：{paid:g} 元")

    if headcount >= 2:
        avg = grand_total / headcount
        lines.append(f"• 平均每人應付：**{avg:.1f} 元**")
        balances = {person: paid - avg for person, paid in totals.items()}
        for person, diff in balances.items():
            if diff > 0.05:
                lines.append(f"  - {person} 應收回 {diff:.1f} 元")
            elif diff < -0.05:
                lines.append(f"  - {person} 應付 {abs(diff):.1f} 元")
            else:
                lines.append(f"  - {person} 已打平")

        transfers = calculate_transfers(balances)
        if transfers:
            lines.append("• 💸 **建議轉帳方式**：")
            lines.extend(transfers)
    else:
        lines.append("• 目前僅 1 人有墊付紀錄；若有多位室友同住，可輸入 `!人數 4`（改成你們寢室人數）即可自動除以全寢人數！")

    return "\n".join(lines)


def format_details(ledger):
    expenses = ledger["expenses"]
    if not expenses:
        return "📋 **目前沒有任何記帳明細**"

    lines = [f"📋 **本輪記帳明細（共 {len(expenses)} 筆）**"]
    for idx, item in enumerate(expenses, start=1):
        ts = item.get("time_str", "")
        time_tag = f" `{ts}`" if ts else ""
        lines.append(f"{idx}. **{item['payer']}** 墊付 **{float(item['amount']):g} 元**（{item['note']}）{time_tag}")
    return "\n".join(lines)


def handle_command(normalized_cmd, author, ledger):
    compact = "".join(normalized_cmd.split())

    if compact in ("!說明", "!指令", "!help", "!功能"):
        return (
            "📖 **宿舍分帳小管家指令說明**\n"
            "• `金額` 或 `金額 項目`（例如 `280`、`150 衛生紙`）：單純記錄一筆支出\n"
            "• `!結清`：顯示最終結算報表並將本輪帳本歸零\n"
            "• `!報表`：查看目前分攤試算與建議轉帳\n"
            "• `!明細`：列出本輪逐筆支出明細\n"
            "• `!人數 4`：設定寢室總分攤人數（`!人數 0` 改回自動計算）\n"
            "• `!刪除`：刪除最新一筆記帳紀錄\n"
            "• 每天 **12:00** 自動發送「吃土榜＋個人財務報表」"
        )

    if compact in ("!報表", "!結算", "!統計"):
        return format_report(ledger)

    if compact in ("!明細", "!列表", "!紀錄"):
        return format_details(ledger)

    if compact in ("!刪除", "!撤銷", "!undo"):
        if not ledger["expenses"]:
            return "⚠️ 目前帳本是空的，沒有可以刪除的紀錄。"
        removed = ledger["expenses"].pop()
        return f"🗑️ 已刪除最新一筆紀錄：**{removed['payer']}** 墊付 **{float(removed['amount']):g} 元**（{removed['note']}）"

    if compact.startswith("!人數"):
        num_str = compact.replace("!人數", "").strip()
        if not num_str.isdigit():
            return "⚠️ 請輸入正確的人數格式，例如：`!人數 4`（輸入 `!人數 0` 改回自動計算）"
        count = int(num_str)
        ledger["member_count"] = count
        if count == 0:
            return "⚙️ 已改回「依有墊付紀錄的室友人数自動平分」模式。"
        return f"⚙️ 已設定寢室分攤人數為 **{count} 人**（由 {author} 設定）"

    if compact.startswith("!設定結算"):
        rule = normalized_cmd.replace("!設定結算", "", 1).strip() or "equal"
        ledger["settlement_rule"] = rule
        return f"⚙️ 已更新結算模式為：`{rule}`（由 {author} 設定）"

    if compact in ("!結清", "!歸零"):
        summary = format_report(ledger)
        ledger["expenses"] = []
        return f"✅ **已完成本輪結清，帳本已歸零！**\n\n{summary}"

    return None


def process_channel(channel_id, ledger, now_tw):
    messages = fetch_messages(channel_id, ledger.get("last_processed_message_id"))
    replies = []
    amount_pattern = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(.*)$")

    for msg in messages:
        ledger["last_processed_message_id"] = msg["id"]

        author_info = msg.get("author", {})
        if author_info.get("bot"):
            continue

        author = author_info.get("global_name") or author_info.get("username") or "成員"
        raw_content = (msg.get("content") or "").strip()
        if not raw_content:
            continue

        normalized = raw_content.replace("！", "!").strip()

        if normalized.startswith("!"):
            res = handle_command(normalized, author, ledger)
            if res:
                replies.append(res)
            continue

        match = amount_pattern.match(normalized)
        if match:
            amount = float(match.group(1))
            note = match.group(2).strip() or "一般支出"
            if amount > 0:
                ledger["expenses"].append(
                    {
                        "payer": author,
                        "amount": amount,
                        "note": note,
                        "time_str": now_tw.strftime("%m/%d %H:%M"),
                        "timestamp": now_tw.isoformat(),
                    }
                )
                # 單純記錄，不附加分攤算式
                replies.append(f"🧾 已記錄 **{author}** 墊付 **{amount:g} 元**（{note}）")

    # 每天 12:00（12:00～12:04 區間）自動發送一次吃土榜與個人財務報表
    today_str = now_tw.strftime("%Y-%m-%d")
    if now_tw.hour == 12 and now_tw.minute < 5 and ledger.get("last_daily_noon_date") != today_str:
        noon_report = build_daily_noon_report(ledger, now_tw)
        ledger["last_daily_noon_date"] = today_str
        if noon_report:
            replies.append(noon_report)

    if replies:
        replies.append(build_read_progress_bar(now_tw))
        send_message(channel_id, "\n\n".join(replies))


def main():
    if not TOKEN:
        raise RuntimeError("缺少 DISCORD_TOKEN 環境變數")

    now_tw = datetime.now(TW_TZ)
    data = load_data()
    channel_ids = get_target_channels(data)
    if not channel_ids:
        raise RuntimeError("找不到任何目標頻道，請確認 DISCORD_CHANNEL_ID 或建立名稱包含「記帳」的文字頻道")

    for cid in channel_ids:
        ledger = get_channel_ledger(data, cid)
        process_channel(cid, ledger, now_tw)

    save_data(data)


if __name__ == "__main__":
    main()
