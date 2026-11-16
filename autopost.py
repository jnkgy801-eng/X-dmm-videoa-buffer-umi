#!/usr/bin/env python3
"""X成長オートパイロット: 投稿生成 -> 画像カード生成 -> Buffer APIで予約投稿。

自動化するのは「投稿の作成と予約」だけ。フォロー・いいね・リプライの自動化は
Xの自動化ルール違反になりやすいため、意図的に含めていない。
"""
from __future__ import annotations

import argparse
import csv
import difflib
import glob
import hashlib
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
    followup: Optional[str]    # 自分への続きリプ(スレッド2投稿目)。無ければ単発投稿


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


def jittered_due(it: Item, cfg: dict[str, Any], tz: ZoneInfo) -> datetime:
    """毎日ぴったり同じ分に投稿すると機械的に見えるため、予約時だけ±N分ずらす。
    queue上の時刻(スロット)は変えない(next_due_slotsがスロット時刻で次の枠を判定するため)。
    idから決定的に決めるので、再実行しても同じ時刻になる。"""
    base = parse_local(it["due"], tz)
    n = int(cfg.get("schedule", {}).get("jitter_minutes", 0))
    if n <= 0:
        return base
    h = int(hashlib.sha256(it["id"].encode()).hexdigest()[:8], 16)
    return base + timedelta(minutes=h % (2 * n + 1) - n)


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


def _assets_literal(image_url: Optional[str]) -> str:
    return f"assets: [{{ image: {{ url: {json.dumps(image_url)} }} }}]" if image_url else ""


def create_post(key: str, channel_id: str, text: str, due_utc: datetime, image_url: Optional[str],
                followup: Optional[str] = None) -> str:
    if followup:
        # Bufferのスレッド仕様: 1投稿目も含めて全部thread配列に入れ、トップのtextは1投稿目と同じにする。
        # 画像は1投稿目のエントリに付ける(トップのassetsとは二重にしない)。
        top_assets = ""
        metadata = (
            "metadata: { twitter: { thread: [ "
            f"{{ text: {json.dumps(text, ensure_ascii=False)} {_assets_literal(image_url)} }} "
            f"{{ text: {json.dumps(followup, ensure_ascii=False)} }} ] }} }}"
        )
    else:
        top_assets, metadata = _assets_literal(image_url), ""
    mutation = f"""
    mutation {{
      createPost(input: {{
        text: {json.dumps(text, ensure_ascii=False)}
        channelId: {json.dumps(channel_id)}
        schedulingType: automatic
        mode: customScheduled
        dueAt: {json.dumps(due_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'))}
        {top_assets}
        {metadata}
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
    failed = 0
    thread_cfg = cfg.get("thread", {})
    thread_kinds = set(thread_cfg.get("kinds", [])) if thread_cfg.get("enabled") else set()
    for it in pending:
        due = jittered_due(it, cfg, tz)
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
        followup = it.get("followup") if it["kind"] in thread_kinds else None
        if args.dry_run:
            print(f"[dry-run] {due:%Y-%m-%dT%H:%M} {it['kind']} image={bool(image_url)} "
                  f"thread={bool(followup)}  {it['text'][:30]!r}")
        else:
            try:
                try:
                    post_id = create_post(key, channel_id, it["text"], due.astimezone(timezone.utc), image_url, followup)
                except AutopostError as e:
                    if not followup:
                        raise
                    # スレッド指定がBuffer側で拒否されても、単発投稿として予約して止めない
                    print(f"::warning::[thread失敗->単発で再試行] {it['id']}: {e}", file=sys.stderr)
                    post_id = create_post(key, channel_id, it["text"], due.astimezone(timezone.utc), image_url, None)
                it["buffer_post_id"] = post_id
                it["status"] = "scheduled"
            except AutopostError as e:
                print(f"::error::[skip] {it['id']}: {e}", file=sys.stderr)
                failed += 1
                continue  # 1件の失敗で全体を止めない
            save_queue(queue)
        done += 1
    if not args.dry_run:
        save_queue(queue)
    print(f"scheduled {done} post(s); buffer room was {room}")
    future = in_buffer + done
    if not args.dry_run:
        if failed:
            raise AutopostError(f"{failed}件のBuffer予約に失敗しました")
        if future == 0:
            raise AutopostError("Buffer側に未来の予約投稿が1件もありません（投稿キュー切れ）。"
                                "ANTHROPIC_API_KEY の設定、または queue.json への追加が必要です")
        if future < 4:
            print(f"::warning::予約済みの投稿が残り{future}件です")


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
    earliest = datetime.now(tz) + timedelta(minutes=30)  # 過去・直近の枠は作らない
    while len(out) < count:
        s = slots[idx]
        due = f"{day.isoformat()}T{s['time']}"
        if parse_local(due, tz) > earliest:
            out.append((due, s["kind"]))
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


INSIGHTS_PATH = ROOT / "insights.json"


def load_insights() -> Optional[dict[str, Any]]:
    if not INSIGHTS_PATH.exists():
        return None
    try:
        return json.loads(INSIGHTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("::warning::insights.json が壊れています。無視して生成します", file=sys.stderr)
        return None


def validate_text(text: str, gen: dict[str, Any], seen: list[str]) -> Optional[str]:
    """NGなら理由を返す。X側のスパム判定・誇大表現・重複投稿を避けるための機械チェック。"""
    if weighted_len(text) > MAX_WEIGHTED_LEN:
        return "文字数超過"
    if "http" in text or "#" in text:
        return "URL/ハッシュタグ"
    for w in gen.get("banned_phrases", []):
        if w in text:
            return f"禁止表現: {w}"
    th = float(gen.get("similarity_threshold", 0.8))
    if any(difflib.SequenceMatcher(None, text, r).ratio() >= th for r in seen):
        return "既存投稿と類似"
    return None


def build_generate_prompt(cfg: dict[str, Any], count: int, kinds: list[str], recent: str,
                          insights: Optional[dict[str, Any]]) -> str:
    gen = cfg["generate"]
    persona = gen.get("persona", "").strip()
    experiences = "\n".join(f"- {e}" for e in gen.get("experiences", []))
    perf = ""
    if insights and insights.get("top_posts"):
        top = "\n".join(f"- {t['text'].splitlines()[0]}" for t in insights["top_posts"][:5])
        low = "\n".join(f"- {t['text'].splitlines()[0]}" for t in insights.get("bottom_posts", [])[:3])
        perf = f"\n【実績データ】反応が良かった投稿(切り口・型を参考に、中身は新しく):\n{top}\n反応が弱かった投稿(この型は避ける):\n{low}\n"
    return f"""あなたはX(旧Twitter)運用のプロ編集者です。テーマ「{gen['theme']}」の投稿を{count}件作ってください。
アカウントの人物像: {persona or '(未設定)'}
本人の実体験(使ってよいのはここにあるものだけ。体験談を捏造しない。無ければ一般的な観察として書く):
{experiences or '- (未設定)'}

種別は次の順番どおりに並べます: {json.dumps(kinds)}
- M: 朝の短い気づき。2〜3行。
- N: コピペで使えるプロンプト。1行目=タイトル、空行、「」で囲んだプロンプト、空行、使い方の一言。
- E: 問いかけ。番号で答えられる選択肢か、体験を聞く質問で返信を誘う。
- S: 【保存版】で始まり、空行のあと①②③か・の箇条書き3〜4個、空行、締めの一言。
各件に "followup"(自分の投稿へ続けて付ける2投稿目)も作る。種別ごとの役割:
- M: null
- N: 実際にそのプロンプトを使った入力例→出力の要点(1〜2行)
- E: 投稿者自身の答えと理由を一言(議論の呼び水。体験談は上記の実体験の範囲で)
- S: 「どれから試しますか？番号で教えてください」のように返信を促す一言
共通ルール:
- 1行目(冒頭)は「読者の具体的な困りごと」か「意外な事実」で始める。「〜のコツ」「〜術」などの総称タイトルで始めない。
- ハッシュタグ・URL禁止。1投稿(followup含む)は全角140字以内。
- 誇大表現・断定的な数字の約束・「絶対」「誰でも」は禁止。具体的で今日から使える内容。
- 同じ言い回し・同じ書き出しを繰り返さない。
{perf}
次の既存投稿と内容が重複しないこと:
{recent}
出力はJSON配列のみ。要素は{{"text": "...", "followup": "..." または null}}。順番は種別の順と対応させる。"""


def cmd_generate(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    import anthropic  # 任意機能のため遅延import

    gen = cfg["generate"]
    queue = load_queue()
    pending = [i for i in queue if i.get("status") == "pending"]
    if len(pending) >= gen["min_pending"] and not args.force:
        print(f"pending={len(pending)} -> 補充不要")
        return
    count = len(cfg["slots"]) * int(gen["batch_days"])
    plan = next_due_slots(cfg, queue, count)
    kinds = [k for _, k in plan]
    recent_items = [i["text"] for i in queue[-120:]]
    recent = "\n".join(f"- {t.splitlines()[0]}" for t in recent_items[-80:])
    prompt = build_generate_prompt(cfg, count, kinds, recent, load_insights())

    client = anthropic.Anthropic()
    results: list[dict[str, Any]] = []
    for attempt in range(3):
        msg = client.messages.create(model=gen["model"], max_tokens=8000,
                                     messages=[{"role": "user", "content": prompt}])
        raw = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip(), flags=re.M).strip()
        try:
            parsed = json.loads(raw)
            items = [{"text": x["text"].strip(), "followup": (x.get("followup") or "").strip() or None} for x in parsed]
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            print(f"[generate] attempt {attempt + 1}: JSONとして解釈できませんでした", file=sys.stderr)
            continue
        if len(items) != count:
            print(f"[generate] attempt {attempt + 1}: 件数不一致 {len(items)}/{count}", file=sys.stderr)
            continue
        seen = list(recent_items)
        errors: list[str] = []
        for n, it in enumerate(items):
            for label, t in (("text", it["text"]), ("followup", it["followup"])):
                if not t:
                    continue
                why = validate_text(t, gen, seen)
                if why:
                    errors.append(f"#{n} {label}: {why}")
            seen.append(it["text"])
        if not errors:
            results = items
            break
        print(f"[generate] attempt {attempt + 1}: 検証NG {errors[:5]}", file=sys.stderr)
    if not results:
        raise AutopostError("生成結果の検証に3回失敗しました。時間をおいて再実行してください。")
    for (due, kind), it in zip(plan, results):
        queue.append({"id": re.sub(r"\D", "", due), "kind": kind, "text": it["text"],
                      "card": card_from_text(kind, it["text"]), "followup": it["followup"] if kind != "M" else None,
                      "due": due, "status": "pending", "buffer_post_id": None})
    save_queue(queue)
    print(f"generated {len(results)} item(s)")


# ---------------------------------------------------------------- 反応データの分析(学習ループ)
CSV_ALIASES: dict[str, list[str]] = {
    "text": ["post text", "text", "tweet text", "ポストのテキスト", "ポスト本文", "投稿本文", "テキスト"],
    "date": ["date", "time", "日付", "投稿日時", "日時"],
    "impressions": ["impressions", "インプレッション"],
    "likes": ["likes", "いいね"],
    "replies": ["replies", "返信", "リプライ"],
    "reposts": ["reposts", "retweets", "リポスト"],
    "bookmarks": ["bookmarks", "ブックマーク"],
    "profile": ["profile visits", "user profile clicks", "プロフィールのクリック", "プロフィールへのアクセス"],
    "follows": ["new follows", "follows", "新しいフォロー"],
}


def _find_col(headers: list[str], aliases: list[str]) -> Optional[str]:
    low = {h: h.strip().lower() for h in headers}
    for a in aliases:  # 完全一致を優先
        for h, l in low.items():
            if l == a:
                return h
    for a in aliases:
        for h, l in low.items():
            if a in l:
                return h
    return None


def _num(v: Any) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def cmd_analyze(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    """Xアナリティクスからエクスポートしたコンテンツ別CSVを分析し、insights.jsonを書く。
    generateがこれを読んで「反応が良かった型」に寄せる。X APIを使わないので追加費用なし。"""
    w = {"likes": 1, "reposts": 3, "replies": 8, "bookmarks": 5, "profile": 4, "follows": 10}
    w.update(cfg.get("analyze", {}).get("weights", {}))
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        cols = {k: _find_col(headers, v) for k, v in CSV_ALIASES.items()}
        if not cols["text"] or not cols["impressions"]:
            raise AutopostError(f"CSVの列を認識できません(text/impressionsが必須)。ヘッダー: {headers}")
        rows = list(reader)

    by_prefix = {re.sub(r"\s+", "", i["text"])[:30]: i for i in load_queue()}
    scored: list[dict[str, Any]] = []
    for r in rows:
        text = (r[cols["text"]] or "").strip()
        imp = _num(r[cols["impressions"]])
        if not text or imp <= 0:
            continue
        raw = sum(w[k] * _num(r[cols[k]]) for k in w if cols.get(k))
        src = by_prefix.get(re.sub(r"\s+", "", text)[:30])
        scored.append({"text": text, "kind": src["kind"] if src else "?", "hour": src["due"][-5:] if src else "?",
                       "impressions": imp, "score": round(raw / max(imp, 100) * 100, 3)})
    if len(scored) < 10:
        raise AutopostError(f"分析対象が{len(scored)}件しかありません。最低10件は必要です")

    def agg(key: str) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for k in sorted({x[key] for x in scored}):
            g = [x for x in scored if x[key] == k]
            out[k] = {"n": len(g), "avg_impressions": round(sum(x["impressions"] for x in g) / len(g)),
                      "avg_score": round(sum(x["score"] for x in g) / len(g), 3)}
        return out

    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)
    insights = {"generated_at": datetime.now().isoformat(timespec="seconds"), "weights": w,
                "by_kind": agg("kind"), "by_slot": agg("hour"),
                "top_posts": ranked[:10], "bottom_posts": ranked[-5:]}
    INSIGHTS_PATH.write_text(json.dumps(insights, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"analyzed {len(scored)} post(s) -> {INSIGHTS_PATH.name}")
    for title, key in (("種別", "by_kind"), ("時間帯", "by_slot")):
        print(f"\n[{title}別] 平均インプ / 平均スコア(=重み付き反応/インプ×100)")
        for k, v in sorted(insights[key].items(), key=lambda kv: -kv[1]["avg_score"]):
            print(f"  {k:>6}: n={int(v['n']):3d}  imp={int(v['avg_impressions']):6d}  score={v['avg_score']}")
    print("\n-> 強い種別の比率を config.yaml の slots で増やし、弱い枠は種別を入れ替えてください。")


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
    s = sub.add_parser("analyze", help="Xアナリティクスのコンテンツ別CSVを分析してinsights.jsonを作る")
    s.add_argument("csv")
    s = sub.add_parser("export-csv", help="Buffer手動取り込み用CSVを出力")
    s.add_argument("--out", default="buffer_next.csv")
    s.add_argument("--limit", type=int, default=10)
    args = p.parse_args()
    cfg = load_config()
    handlers = {"channels": cmd_channels, "import-csv": cmd_import_csv, "generate": cmd_generate,
                "render": cmd_render, "schedule": cmd_schedule, "export-csv": cmd_export_csv,
                "analyze": cmd_analyze}
    try:
        handlers[args.cmd](cfg, args)
    except AutopostError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
