import json
import os
import re
from datetime import datetime, timezone
import urllib.request
import urllib.error

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
DATA_FILE = "dorm_data.json"
API_BASE = "https://discord.com/api/v10"


def load_data():
    if not os.path.exists(DATA_FILE):
        return {"expenses": [], "settlement_rule": "equal", "last_processed_message_id": None}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("expenses", [])
    data.setdefault("settlement_rule", "equal")
    data.setdefault("last_processed_message_id", None)
    return data


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def discord_request(method, path, payload=None):
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bot {TOKEN}",
        "User-Agent": "DormExpenseBot (https://github.com, 1.0)",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def fetch_messages(after_id=None):
    query = f"/channels/{CHANNEL_ID}/messages?limit=100"
    if after_id:
        query += f"&after={after_id}"
    msgs = discord_request("GET", query) or []
    msgs.sort(key=lambda m: int(m["id"]))
    return msgs


def send_message(content):
    if not content:
        return
    discord_request("POST", f"/channels/{CHANNEL_ID}/messages", {"content": content[:1900]})


def format_report(data):
    expenses = data["expenses"]
    if not expenses:
        return "📊 **目前沒有未結清的記帳紀錄**\n直接輸入金額（例如 `280` 或 `280 晚餐`）即可新增一筆支出。"

    totals = {}
    grand_total = 0.0
    for item in expenses:
        payer = item["payer"]
        amount = float(item["amount"])
        totals[payer] = totals.get(payer, 0.0) + amount
        grand_total += amount

    rule = data.get("settlement_rule", "equal")
    lines = [
        "📊 **宿舍分帳目前報表**",
        f"• 未結清筆數：{len(expenses)} 筆",
        f"• 總支出金額：{grand_total:g} 元",
        f"• 分攤模式：`{rule}`",
        "• 各人已墊付：",
    ]
    for person, paid in totals.items():
        lines.append(f"  - {person}：{paid:g} 元")

    if len(totals) >= 2 and rule == "equal":
        avg = grand_total / len(totals)
        lines.append(f"• 平均每人應付：{avg:.1f} 元")
        for person, paid in totals.items():
            diff = paid - avg
            if diff > 0.01:
                lines.append(f"  - {person} 應收回 {diff:.1f} 元")
            elif diff < -0.01:
                lines.append(f"  - {person} 應付 {abs(diff):.1f} 元")
            else:
                lines.append(f"  - {person} 已打平")
    return "\n".join(lines)


def handle_command(content, author, data):
    cmd = content.strip()
    if cmd == "!說明":
        return (
            "📖 **宿舍分帳小管家指令說明**\n"
            "• 直接打數字或 `金額 項目`（例如 `280`、`150 衛生紙`）：幫你記一筆支出\n"
            "• `!報表`：查看目前支出統計與分攤試算\n"
            "• `!設定結算 equal`：設定平均分攤\n"
            "• `!結清`：清空目前帳本並重新開始下一輪記帳"
        )
    if cmd == "!報表":
        return format_report(data)
    if cmd.startswith("!設定結算"):
        parts = cmd.split(maxsplit=1)
        rule = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "equal"
        data["settlement_rule"] = rule
        return f"⚙️ 已更新結算模式為：`{rule}`（由 {author} 設定）"
    if cmd == "!結清":
        summary = format_report(data)
        data["expenses"] = []
        return f"✅ **已完成本輪結清，帳本已歸零**\n\n{summary}"
    return None


def main():
    if not TOKEN or not CHANNEL_ID:
        raise RuntimeError("缺少 DISCORD_TOKEN 或 DISCORD_CHANNEL_ID 環境變數")

    data = load_data()
    messages = fetch_messages(data.get("last_processed_message_id"))
    if not messages:
        return

    replies = []
    amount_pattern = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(.*)$")

    for msg in messages:
        msg_id = msg["id"]
        data["last_processed_message_id"] = msg_id

        author_info = msg.get("author", {})
        if author_info.get("bot"):
            continue

        author = author_info.get("global_name") or author_info.get("username") or "成員"
        content = (msg.get("content") or "").strip()
        if not content:
            continue

        if content.startswith("!"):
            res = handle_command(content, author, data)
            if res:
                replies.append(res)
            continue

        match = amount_pattern.match(content)
        if match:
            amount = float(match.group(1))
            note = match.group(2).strip() or "一般支出"
            if amount > 0:
                data["expenses"].append({
                    "payer": author,
                    "amount": amount,
                    "note": note,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                replies.append(f"🧾 已記錄 **{author}** 墊付 **{amount:g} 元**（{note}）")

    save_data(data)
    if replies:
        send_message("\n\n".join(replies))


if __name__ == "__main__":
    main()
