#!/usr/bin/env python3
"""X成長オートパイロット: 投稿生成 -> 画像カード生成 -> Buffer APIで予約投稿。

自動化するのは「投稿の作成と予約」だけ。フォロー・いいね・リプライの自動化は
Xの自動化ルール違反になりやすいため、意図的に含めていない。
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict
from zoneinfo import ZoneInfo

import requests
import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
QUEUE_PATH = ROOT / "queue.json"
IMAGE_DIR = ROOT / "images"
BUFFER_URL = "https://api.buffer.com"
MAX_WEIGHTED_LEN = 280  # Xの重み付き文字数(全角=2)で安全側に判定


class Card(TypedDict):
    title: str
    body: str


class Item(TypedDict, total=False):
    id: str
    kind: str                  # M / N / E / S
    text: str
    card: Optional[Card]       # Noneならテキストのみ
    due: str                   # ローカル時刻 "YYYY-MM-DDTHH:MM"
    status: str                # pending / scheduled / expired
    buffer_post_id: Optional[str]


class AutopostError(RuntimeError):
    pass


# ---------------------------------------------------------------- 共通
def load_config() -> dict[str, Any]:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_queue() -> list[Item]:
    if not QUEUE_PATH.exists():
        return []
    return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))


def save_queue(queue: list[Item]) -> None:
    # 途中で落ちても壊れないよう、一時ファイル経由で置き換える
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(QUEUE_PATH)


def weighted_len(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def parse_local(due: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(due).replace(tzinfo=tz)


def card_from_text(kind: str, text: str) -> Optional[Card]:
    """N(プロンプト)とS(保存版)だけ、本文の核を画像カードにする。"""
    if kind not in ("N", "S"):
        return None
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) < 2:
        return None
    return {"title": paras[0].replace("【保存版】", "").strip(), "body": paras[1]}


# ---------------------------------------------------------------- 画像カード
FONT_CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
]
NO_LINE_START = set("、。，．」』）】！？!?,.")


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size, index=0)
    raise AutopostError("日本語フォントが見つかりません。FONT_PATH を設定するか fonts-noto-cjk を入れてください。")


def wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for ch in para:
            # 行頭に句読点が来ないよう、はみ出しても前の行に付ける
            if draw.textlength(cur + ch, font=font) <= max_w or (ch in NO_LINE_START and cur):
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)
    return lines


def render_card(card: Card, out: Path, cfg: dict[str, Any]) -> None:
    W, H, PAD = 1200, 675, 64
    bg, panel, accent, fg, sub = "#0B1220", "#162033", "#38BDF8", "#FFFFFF", "#94A3B8"
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 10], fill=accent)

    # 上部ラベル
    label_font = load_font(26)
    d.text((PAD, 40), cfg["account_name"].split("|")[-1], font=label_font, fill=accent)

    # タイトル（最大2行に収まるサイズを探す）
    for size in (60, 54, 48, 44, 40):
        tf = load_font(size)
        t_lines = wrap(d, card["title"], tf, W - PAD * 2)
        if len(t_lines) <= 2:
            break
    y = 92
    for ln in t_lines:
        d.text((PAD, y), ln, font=tf, fill=fg)
        y += int(size * 1.3)

    # 本文パネル（収まるまで文字を縮める）
    top, bottom = y + 18, H - 78
    inner_w, inner_h = W - PAD * 2 - 56, bottom - top - 44
    for bsize in range(40, 21, -2):
        bf = load_font(bsize)
        b_lines = wrap(d, card["body"], bf, inner_w)
        if len(b_lines) * int(bsize * 1.55) <= inner_h:
            break
    else:
        raise AutopostError(f"本文が長すぎてカードに収まりません: {card['title']}")
    d.rounded_rectangle([PAD, top, W - PAD, bottom], radius=22, fill=panel)
    d.rectangle([PAD, top + 18, PAD + 8, bottom - 18], fill=accent)
    by = top + 22 + (inner_h - len(b_lines) * int(bsize * 1.55)) // 2
    for ln in b_lines:
        d.text((PAD + 36, by), ln, font=bf, fill="#E2E8F0")
        by += int(bsize * 1.55)

    # フッター
    ff = load_font(26)
    d.text((PAD, H - 56), cfg["handle"], font=ff, fill=sub)
    right = "保存して使ってね"
    d.text((W - PAD - d.textlength(right, font=ff), H - 56), right, font=ff, fill=accent)
    out.parent.mkdir(exist_ok=True)
    img.save(out, "PNG", optimize=True)


def image_name(item: Item) -> str:
    return f"{item['id']}.png"


def cmd_render(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    n = 0
    for it in load_queue():
        if it.get("status") == "pending" and it.get("card"):
            out = IMAGE_DIR / image_name(it)
            if out.exists() and not args.force:
                continue
            render_card(it["card"], out, cfg)
            n += 1
    print(f"rendered {n} image(s) -> {IMAGE_DIR}")


# ---------------------------------------------------------------- Buffer API
def gql(query: str, api_key: str, retries: int = 3) -> dict[str, Any]:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = requests.post(
                BUFFER_URL,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                json={"query": query},
                timeout=30,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                raise AutopostError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                raise AutopostError(json.dumps(data["errors"], ensure_ascii=False))
            return data["data"]
        except (requests.RequestException, AutopostError) as e:
            last = e
            time.sleep(2 ** attempt * 2)  # レート制限・一時障害に備えて指数バックオフ
    raise AutopostError(f"Buffer API failed: {last}")


def get_api_key() -> str:
    key = os.environ.get("BUFFER_API_KEY")
    if not key:
        raise AutopostError("環境変数 BUFFER_API_KEY が未設定です (https://publish.buffer.com/settings/api)")
    return key


def cmd_channels(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    key = get_api_key()
    orgs = gql("query { account { organizations { id name } } }", key)["account"]["organizations"]
    for org in orgs:
        print(f"[organization] {org['id']}  {org['name']}")
        q = f'query {{ channels(input: {{ organizationId: {json.dumps(org["id"])} }}) {{ id name displayName service }} }}'
        for ch in gql(q, key)["channels"]:
            print(f"   channel_id={ch['id']}  {ch['service']}  {ch.get('displayName') or ch['name']}")
    print("\n使うチャンネルIDを環境変数 BUFFER_CHANNEL_ID に設定してください。")


def create_post(key: str, channel_id: str, text: str, due_utc: datetime, image_url: Optional[str]) -> str:
    assets = f"assets: [{{ image: {{ url: {json.dumps(image_url)} }} }}]" if image_url else ""
    mutation = f"""
    mutation {{
      createPost(input: {{
        text: {json.dumps(text, ensure_ascii=False)}
        channelId: {json.dumps(channel_id)}
        schedulingType: automatic
        mode: customScheduled
        dueAt: {json.dumps(due_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'))}
        {assets}
      }}) {{
        ... on PostActionSuccess {{ post {{ id }} }}
        ... on MutationError {{ message }}
      }}
    }}"""
    res = gql(mutation, key)["createPost"]
    if "post" not in res or not res["post"]:
        raise AutopostError(res.get("message", "unknown mutation error"))
    return res["post"]["id"]


def cmd_schedule(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    queue = load_queue()
    key = "" if args.dry_run else get_api_key()
    channel_id = os.environ.get("BUFFER_CHANNEL_ID", "DRY_RUN" if args.dry_run else "")
    if not channel_id:
        raise AutopostError("環境変数 BUFFER_CHANNEL_ID が未設定です (python autopost.py channels で確認)")

    in_buffer = sum(1 for it in queue if it.get("status") == "scheduled" and parse_local(it["due"], tz) > now)
    room = max(0, int(cfg["max_scheduled_in_buffer"]) - in_buffer)
    pending = sorted((it for it in queue if it.get("status") == "pending"), key=lambda i: i["due"])
    done = 0
    for it in pending:
        due = parse_local(it["due"], tz)
        if due <= now + timedelta(minutes=10):
            it["status"] = "expired"  # 過去日時は予約できないので落とす
            continue
        if done >= room:
            break
        image_url = None
        if it.get("card"):
            image_url = f"{cfg['image_base_url'].rstrip('/')}/{image_name(it)}"
            if not args.dry_run and "YOUR_NAME" in image_url:
                raise AutopostError("config.yaml の image_base_url を自分のリポジトリに書き換えてください")
        if args.dry_run:
            print(f"[dry-run] {it['due']} {it['kind']} image={bool(image_url)}  {it['text'][:30]!r}")
        else:
            try:
                it["buffer_post_id"] = create_post(key, channel_id, it["text"], due.astimezone(timezone.utc), image_url)
                it["status"] = "scheduled"
            except AutopostError as e:
                print(f"[skip] {it['id']}: {e}", file=sys.stderr)
                continue  # 1件の失敗で全体を止めない
            save_queue(queue)
        done += 1
    if not args.dry_run:
        save_queue(queue)
    print(f"scheduled {done} post(s); buffer room was {room}")


# ---------------------------------------------------------------- 取り込み・生成
def next_due_slots(cfg: dict[str, Any], queue: list[Item], count: int) -> list[tuple[str, str]]:
    tz = ZoneInfo(cfg["timezone"])
    slots = cfg["slots"]
    if queue:
        last = max(parse_local(i["due"], tz) for i in queue)
        day, idx = last.date(), None
        for n, s in enumerate(slots):
            if s["time"] == last.strftime("%H:%M"):
                idx = n
        idx = 0 if idx is None else idx + 1
        if idx >= len(slots):
            day, idx = day + timedelta(days=1), 0
    else:
        day, idx = datetime.now(tz).date() + timedelta(days=1), 0
    out: list[tuple[str, str]] = []
    for _ in range(count):
        s = slots[idx]
        out.append((f"{day.isoformat()}T{s['time']}", s["kind"]))
        idx += 1
        if idx >= len(slots):
            day, idx = day + timedelta(days=1), 0
    return out


def cmd_import_csv(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    """既存のBuffer用CSV(text,posting_time)をキューに取り込む。"""
    slot_kind = {s["time"]: s["kind"] for s in cfg["slots"]}
    queue = load_queue()
    known = {i["id"] for i in queue}
    for path in sorted(glob.glob(args.pattern)):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                due = row["posting_time"].strip().replace(" ", "T")
                item_id = re.sub(r"\D", "", due)
                if item_id in known:
                    continue
                kind = slot_kind.get(due[-5:], "M")
                queue.append({"id": item_id, "kind": kind, "text": row["text"], "card": card_from_text(kind, row["text"]),
                              "due": due, "status": "pending", "buffer_post_id": None})
                known.add(item_id)
    queue.sort(key=lambda i: i["due"])
    save_queue(queue)
    print(f"queue: {len(queue)} item(s)")


def cmd_generate(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    import anthropic  # 任意機能のため遅延import

    queue = load_queue()
    pending = [i for i in queue if i.get("status") == "pending"]
    if len(pending) >= cfg["generate"]["min_pending"] and not args.force:
        print(f"pending={len(pending)} -> 補充不要")
        return
    count = len(cfg["slots"]) * int(cfg["generate"]["batch_days"])
    plan = next_due_slots(cfg, queue, count)
    kinds = [k for _, k in plan]
    recent = "\n".join(f"- {i['text'].splitlines()[0]}" for i in queue[-80:])
    prompt = f"""あなたはX(旧Twitter)運用のプロ編集者です。テーマ「{cfg['generate']['theme']}」の投稿を{count}件作ってください。
種別は次の順番どおりに並べます: {json.dumps(kinds)}
- M: 朝の短い気づき。2〜3行。
- N: コピペで使えるプロンプト。1行目=タイトル、空行、「」で囲んだプロンプト、空行、使い方の一言。
- E: 問いかけ。番号で答えられる選択肢か、体験を聞く質問で返信を誘う。
- S: 【保存版】で始まり、空行のあと①②③か・の箇条書き3〜4個、空行、締めの一言。
ルール: ハッシュタグ・URL禁止。1投稿は全角140字以内。誇大表現・断定的な数字の約束は禁止。具体的で今日から使える内容。
次の既存投稿と内容が重複しないこと:
{recent}
出力はJSON配列のみ。要素は{{"text": "..."}}。順番は種別の順と対応させる。"""
    client = anthropic.Anthropic()
    texts: list[str] = []
    for attempt in range(3):
        msg = client.messages.create(model=cfg["generate"]["model"], max_tokens=8000,
                                     messages=[{"role": "user", "content": prompt}])
        raw = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip(), flags=re.M).strip()
        try:
            texts = [x["text"].strip() for x in json.loads(raw)]
            if len(texts) == count and all(weighted_len(t) <= MAX_WEIGHTED_LEN and "http" not in t and "#" not in t for t in texts):
                break
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        texts = []
    if not texts:
        raise AutopostError("生成結果の検証に3回失敗しました。時間をおいて再実行してください。")
    for (due, kind), text in zip(plan, texts):
        queue.append({"id": re.sub(r"\D", "", due), "kind": kind, "text": text, "card": card_from_text(kind, text),
                      "due": due, "status": "pending", "buffer_post_id": None})
    save_queue(queue)
    print(f"generated {len(texts)} item(s)")


def cmd_export_csv(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    """APIを使わず、Bufferの手動CSV取り込みで使う場合のエクスポート。"""
    rows = [i for i in load_queue() if i.get("status") == "pending"][: args.limit]
    base = cfg["image_base_url"].rstrip("/")
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["text", "image_url", "tags", "posting_time"])
        w.writeheader()
        for i in rows:
            w.writerow({"text": i["text"], "image_url": f"{base}/{image_name(i)}" if i.get("card") else "",
                        "tags": "", "posting_time": i["due"].replace("T", " ")})
    print(f"wrote {len(rows)} row(s) -> {args.out}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("channels", help="BufferのチャンネルID一覧")
    s = sub.add_parser("import-csv", help="既存CSVをキューに取り込む")
    s.add_argument("pattern")
    s = sub.add_parser("generate", help="Claudeで投稿を補充")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("render", help="画像カードを生成")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("schedule", help="Buffer APIで予約")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("export-csv", help="Buffer手動取り込み用CSVを出力")
    s.add_argument("--out", default="buffer_next.csv")
    s.add_argument("--limit", type=int, default=10)
    args = p.parse_args()
    cfg = load_config()
    handlers = {"channels": cmd_channels, "import-csv": cmd_import_csv, "generate": cmd_generate,
                "render": cmd_render, "schedule": cmd_schedule, "export-csv": cmd_export_csv}
    try:
        handlers[args.cmd](cfg, args)
    except AutopostError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
