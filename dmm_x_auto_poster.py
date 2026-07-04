# -*- coding: utf-8 -*-
"""
🐦 FANZA同人 → X（Twitter）自動投稿ツール

DMMアフィリエイトAPI(v3)を人気順（rank降順）で検索し、
・過去に投稿済みでない
・年少者連想ワードを含まない
作品を上位から5件集めて、X API (v2 /2/tweets) へ実際に投稿します。

GitHub Actionsの `schedule` で1時間に1回自動実行される想定です。

⚠️ 安全設計についての重要な説明
--------------------------------------------------------------
- age_safety_filter.py により、タイトル・ジャンル・サークル名に
  未成年を想起させる語を含む作品は自動的に除外し、投稿しません。
  除外された件数と理由はログと outputs/skipped_*.txt に出力します。
- outputs/posted_history.json に投稿済み作品のIDを記録し、
  同じ作品を二度投稿しないようにします（重複防止）。
- 1回の実行で5件に満たない場合は、rank順で取得ページ（offset）を
  進めながら重複・除外分を差し引いても5件集まるまで検索を続けます
  （ただし無限ループを避けるため取得ページ数に上限があります）。
- Xの利用規約・アダルトコンテンツに関するポリシー、および
  景品表示法のPR表記等を遵守する責任は運用者にあります。
--------------------------------------------------------------
"""

import os
import sys
import json
import time
import random
import datetime
import requests
from pathlib import Path
from requests_oauthlib import OAuth1

from age_safety_filter import is_safe

# ================================================================
# ⚙️ 設定（環境変数から読み込み）
# ================================================================

DMM_API_ID       = os.environ.get('DMM_API_ID', '')
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '')

X_API_KEY             = os.environ.get('X_API_KEY', '')
X_API_SECRET          = os.environ.get('X_API_SECRET', '')
X_ACCESS_TOKEN        = os.environ.get('X_ACCESS_TOKEN', '')
X_ACCESS_TOKEN_SECRET = os.environ.get('X_ACCESS_TOKEN_SECRET', '')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ 環境変数 DMM_API_ID / DMM_AFFILIATE_ID が設定されていません。')
    sys.exit(1)

if not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
    print('❌ 環境変数 X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET が設定されていません。')
    sys.exit(1)

print('✅ 認証情報を読み込みました。')

DMM_API_BASE = 'https://api.dmm.com/affiliate/v3'
X_API_BASE   = 'https://api.twitter.com/2/tweets'

DMM_FLOOR = os.environ.get('DMM_FLOOR', 'doujin')

FLOOR_SERVICE_MAP = {
    'videoa':  ('digital', 'videoa'),
    'videoc':  ('digital', 'videoc'),
    'anime':   ('digital', 'anime'),
    'doujin':  ('doujin',  'digital_doujin'),
    'comic':   ('ebook',   'comic'),
    'goods':   ('mono',    'goods'),
}
SERVICE, FLOOR_NAME = FLOOR_SERVICE_MAP.get(DMM_FLOOR, ('doujin', 'digital_doujin'))

HASHTAG_MAP = {
    'videoa': '#FANZA #DMM #アダルト #PR',
    'videoc': '#FANZA #DMM #素人 #PR',
    'anime':  '#FANZA #DMM #アニメ #PR',
    'doujin': '#FANZA #DMM #同人 #PR',
    'comic':  '#DMM #電子書籍 #漫画 #PR',
    'goods':  '#DMM #グッズ #PR',
    'default': '#DMM #PR',
}

# 1回のAPIリクエストで取得する件数（rank順ページング用）
FETCH_HITS = int(os.environ.get('FETCH_HITS', '100'))
# 何ページ（何回のAPIリクエスト）まで検索を続けるかの上限
# （5件揃わないまま延々とAPIを叩き続けないためのセーフティ）
MAX_FETCH_PAGES = int(os.environ.get('MAX_FETCH_PAGES', '10'))
# 1回の実行で投稿する件数
POSTS_PER_RUN = int(os.environ.get('POSTS_PER_RUN', '5'))

# DMM APIのoffsetは1始まり。ここから rank降順（人気順）で走査していく。
START_OFFSET = int(os.environ.get('POST_START_INDEX', '1'))

# 投稿済み作品の重複防止用履歴ファイル
# はてなブログ投稿(hatena_blog_poster.py)とは別の履歴ファイルにする
# （同じファイルを共有すると、はてなに投稿済みの作品がXに投稿できなくなるため）
POSTED_HISTORY_FILE = Path(os.environ.get('POSTED_HISTORY_FILE', 'outputs/x_posted_history.json'))

# 連続投稿の間隔（秒）。X側のスパム判定・レート制限対策として空ける。
POST_INTERVAL_SEC = int(os.environ.get('POST_INTERVAL_SEC', '15'))


# ================================================================
# 🗂️ 投稿履歴管理（重複投稿防止）
# ================================================================

def load_posted_history() -> set:
    if not POSTED_HISTORY_FILE.exists():
        return set()
    try:
        with open(POSTED_HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data.get('posted', []))
    except Exception as e:
        print(f'⚠️ 投稿履歴の読み込みに失敗しました（新規履歴として扱います）: {e}')
        return set()


def save_posted_history(history: set) -> None:
    POSTED_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(POSTED_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump({'posted': sorted(history)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ 投稿履歴の保存に失敗しました: {e}')


def product_history_key(product: dict) -> str:
    return product.get('content_id') or f"title:{product.get('title', '')}"


# ================================================================
# 🔧 DMM API（rank降順ページング）
# ================================================================

def fetch_dmm_page(offset: int):
    """指定したoffsetからrank降順(人気順)でFETCH_HITS件取得する。"""
    params = {
        'api_id':       DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      SERVICE,
        'floor':        FLOOR_NAME,
        'hits':         FETCH_HITS,
        'offset':       offset,
        'sort':         '-rank',
        'output':       'json',
    }
    try:
        resp = requests.get(f'{DMM_API_BASE}/ItemList', params=params, timeout=15)
        data = resp.json()
        items = data.get('result', {}).get('items', [])
        if isinstance(items, dict):
            items = items.get('item', [])
        return items
    except Exception as e:
        print(f'  ❌ DMM APIエラー（offset={offset}）: {e}')
        return []


def parse_product(item):
    content_id    = item.get('content_id', '') or item.get('product_id', '')
    title         = item.get('title', '')
    affiliate_url = item.get('affiliateURL', '') or item.get('URL', '')
    prices        = item.get('prices', {})
    price_str, price_num = '', None
    if prices:
        price_val = prices.get('price') or prices.get('list_price') or ''
        if price_val:
            digits = ''.join(c for c in str(price_val) if c.isdigit())
            if digits:
                price_num = int(digits)
                price_str = f'¥{price_num:,}'

    genres = [g.get('name', '') for g in (item.get('iteminfo', {}).get('genre') or [])][:3]
    maker  = ((item.get('iteminfo', {}).get('maker') or [{}])[0]).get('name', '')

    review_info = item.get('review', {}) or {}
    try:
        review_avg   = float(review_info.get('average', 0) or 0)
        review_count = int(review_info.get('count', 0) or 0)
    except (ValueError, TypeError):
        review_avg, review_count = 0.0, 0
    review_avg   = round(review_avg, 2) if review_avg else None
    review_count = review_count if review_count else None

    return {
        'content_id':    content_id,
        'title':         title,
        'affiliate_url': affiliate_url,
        'price':         price_str,
        'price_num':     price_num,
        'genres':        genres,
        'maker':         maker,
        'review_avg':    review_avg,
        'review_count':  review_count,
    }


def clean_url(url):
    if not url:
        return ''
    url = url.strip().replace('\n', '').replace('\r', '').replace('　', '')
    if not url.startswith('http'):
        return ''
    return url


def shorten_url(url):
    if not url:
        return url
    try:
        resp = requests.get('https://tinyurl.com/api-create.php', params={'url': url}, timeout=8)
        if resp.status_code == 200 and resp.text.startswith('http'):
            return resp.text.strip()
    except Exception as e:
        print(f'    ⚠️ URL短縮に失敗（元のURLを使用）: {e}')
    return url


def genre_tags(genres):
    return '　'.join('#' + g.replace(' ', '').replace('　', '') for g in genres if g)


# ================================================================
# ✂️ 文字数カウント（Xの仕様: URLは23文字固定扱い）
# ================================================================

import re
URL_CHAR_COUNT = 23
X_LIMIT = 280


def x_len(text: str) -> int:
    url_pattern = re.compile(r'https?://\S+')
    count = 0
    last_end = 0
    for m in url_pattern.finditer(text):
        count += len(text[last_end:m.start()])
        count += URL_CHAR_COUNT
        last_end = m.end()
    count += len(text[last_end:])
    return count


# ================================================================
# 📝 投稿文生成（1作品=1投稿）
# ================================================================

CV_TEMPLATES_FALLBACK = [
    "このジャンル好きな人には刺さると思う\nレビュー評価も高く選んで損はない",
    "深夜のお供にちょうどいい密度感🌙\n価格と内容のバランスが良い作品です",
    "タイトルの雰囲気通りの内容で期待は裏切らない\n気になるなら詳細だけでも覗いてみて",
    "人気ランキング上位に入るのも納得の完成度\n気になった人はチェックしてみて",
]


def get_copy_ai(title, genres, price, review_avg, review_count) -> str:
    if not ANTHROPIC_API_KEY:
        return random.choice(CV_TEMPLATES_FALLBACK)

    genre_str = '・'.join(genres) if genres else 'なし'
    review_str = f'平均{review_avg}点（{review_count}件）' if review_avg and review_count else 'なし'

    prompt = (
        "FANZA同人（成人向け）ランキング上位作品を紹介するX投稿のコメント文を作成してください。\n\n"
        f"作品タイトル：{title}\nジャンル：{genre_str}\n価格：{price if price else '不明'}\n"
        f"レビュー：{review_str}\n\n"
        "【条件】\n"
        "- 必ず2行構成、合計40〜70文字（改行込み）\n"
        "- 「ランキング上位」「人気」であることを自然に触れる\n"
        "- 誇張表現やAIっぽい宣伝文句（「話題沸騰中」「必見」等）は使わない\n"
        "- 絵文字は0〜1個まで\n"
        "- 出力は本文テキストのみ。前置きや説明は不要。"
    )
    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': 'claude-haiku-4-5-20251001',
                'max_tokens': 150,
                'messages': [{'role': 'user', 'content': prompt}],
            },
            timeout=10,
        )
        data = resp.json()
        text = data.get('content', [{}])[0].get('text', '').strip()
        if text and len(text) <= 100:
            return text
    except Exception as e:
        print(f'    ⚠️ AIコメント生成エラー（フォールバック使用）: {e}')
    return random.choice(CV_TEMPLATES_FALLBACK)


def build_tweet_text(product: dict, rank_no: int) -> str:
    hashtags  = HASHTAG_MAP.get(DMM_FLOOR, HASHTAG_MAP['default'])
    raw_url   = clean_url(product['affiliate_url'])
    short_url = shorten_url(raw_url)
    copy_text = get_copy_ai(
        product['title'], product['genres'], product['price'],
        product.get('review_avg'), product.get('review_count'),
    )

    def build(title_text, include_genre):
        lines = [f'📖【人気{rank_no}位】{title_text}', '', copy_text]
        if product['price']:
            lines.append(f'💰 {product["price"]}')
        if include_genre and product['genres']:
            lines.append(f'🏷 {genre_tags(product["genres"])}')
        lines.append(short_url)
        lines.append(hashtags)
        return '\n'.join(lines)

    text = build(product['title'], include_genre=True)
    if x_len(text) > X_LIMIT:
        text = build(product['title'], include_genre=False)
    if x_len(text) > X_LIMIT:
        truncated = product['title'][:30] + '…' if len(product['title']) > 30 else product['title']
        text = build(truncated, include_genre=False)
    if x_len(text) > X_LIMIT:
        # 最終手段：コメントを削って必須情報だけ残す
        text = f'📖【人気{rank_no}位】{truncated}\n{short_url}\n{hashtags}'
    return text


# ================================================================
# 🔐 X (Twitter) API v2 への投稿
# ================================================================

def post_to_x(text: str) -> bool:
    auth = OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
    try:
        resp = requests.post(X_API_BASE, auth=auth, json={'text': text}, timeout=15)
        if resp.status_code in (200, 201):
            tweet_id = resp.json().get('data', {}).get('id', '')
            print(f'    ✅ 投稿成功 (tweet_id={tweet_id})')
            return True
        else:
            print(f'    ❌ 投稿失敗 status={resp.status_code}: {resp.text[:300]}')
            return False
    except Exception as e:
        print(f'    ❌ 投稿エラー: {e}')
        return False


# ================================================================
# 🚀 メイン実行
# ================================================================

def main():
    posted_history = load_posted_history()
    print(f'📚 投稿済み履歴: {len(posted_history)}件')

    selected = []          # 投稿対象（rank順を維持）
    seen_in_run = set()    # 今回の実行内での重複防止
    all_skipped_age = []
    offset = START_OFFSET

    for page in range(1, MAX_FETCH_PAGES + 1):
        if len(selected) >= POSTS_PER_RUN:
            break

        print(f'\n🔎 [{page}/{MAX_FETCH_PAGES}ページ目] rank降順で取得中 '
              f'(offset={offset}, hits={FETCH_HITS})...')
        raw_items = fetch_dmm_page(offset)
        if not raw_items:
            print('  ⚠️ これ以上取得できませんでした。検索を打ち切ります。')
            break

        for item in raw_items:
            if len(selected) >= POSTS_PER_RUN:
                break

            product = parse_product(item)
            key = product_history_key(product)

            # 1. 過去投稿済みならスキップ（重複防止）
            if key in posted_history or key in seen_in_run:
                continue

            # 2. 年少者連想ワードを含む場合はスキップ（安全フィルター）
            ok, matched = is_safe(product)
            if not ok:
                all_skipped_age.append((product, matched))
                continue

            seen_in_run.add(key)
            selected.append(product)

        offset += FETCH_HITS

    print(f'\n📊 検索結果: {len(selected)}/{POSTS_PER_RUN}件 集まりました '
          f'（安全フィルター除外 {len(all_skipped_age)}件）')

    if all_skipped_age:
        Path('outputs').mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        skip_path = Path('outputs') / f'skipped_{ts}.txt'
        with open(skip_path, 'w', encoding='utf-8') as f:
            f.write('# 年少者連想ワードにより除外された作品一覧\n\n')
            for p, matched in all_skipped_age:
                f.write(f"- {p['title']}\n  マッチ語: {', '.join(matched)}\n")
        print(f'📄 除外ログ: {skip_path}')

    if not selected:
        print('⚠️ 投稿できる新規作品がありませんでした（重複/除外/取得上限）。')
        sys.exit(0)

    if len(selected) < POSTS_PER_RUN:
        print(f'⚠️ {MAX_FETCH_PAGES}ページ検索しましたが{POSTS_PER_RUN}件に届きませんでした。'
              f'集まった{len(selected)}件のみ投稿します。')

    posted = 0
    for i, product in enumerate(selected, 1):
        print(f"\n🐦 投稿 {i}/{len(selected)}: {product['title'][:40]}")
        text = build_tweet_text(product, rank_no=i)
        print(f'    文字数(X換算): {x_len(text)}字')
        if post_to_x(text):
            posted += 1
            # 投稿成功のたびに履歴へ追加して即保存（途中で中断しても重複を防ぐ）
            posted_history.add(product_history_key(product))
            save_posted_history(posted_history)

        if i < len(selected):
            time.sleep(POST_INTERVAL_SEC)

    print(f'\n✅ 完了！{posted}/{len(selected)}件をXに投稿しました。')
    print(f'   📚 累計投稿履歴: {len(posted_history)}件（{POSTED_HISTORY_FILE}）')


if __name__ == '__main__':
    main()
