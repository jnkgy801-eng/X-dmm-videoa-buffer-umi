# -*- coding: utf-8 -*-
"""
📝 FANZA同人 → はてなブログ 記事自動生成・下書き投稿ツール

DMMアフィリエイトAPI(v3)で同人作品情報を取得し、作品ごとに
はてなブログ記事（1作品=1記事）を生成して、はてなブログAtomPub APIへ
「下書き（draft）」として投稿します。

⚠️ 安全設計についての重要な説明
--------------------------------------------------------------
- このスクリプトは記事を必ず「下書き」として投稿します。
  自動で「公開」まで行うモードは用意していません。
  最終的な公開判断は人間が行ってください。
- age_safety_filter.py により、タイトル・ジャンル・サークル名に
  未成年を想起させる語を含む作品は自動的に除外し、記事化しません。
  除外された件数と理由はログと outputs/skipped_*.txt に出力します。
- はてなブログ側の設定で「アダルトカテゴリブログ」に設定すること、
  年齢確認・注意書きの掲載など、プラットフォームの利用規約・
  法令（景品表示法のPR表記等含む）を遵守する責任は運用者にあります。
--------------------------------------------------------------
"""

import os
import sys
import base64
import hashlib
import random
import datetime
import requests
from pathlib import Path
from xml.sax.saxutils import escape

from age_safety_filter import is_safe, find_matched_keywords

# ================================================================
# ⚙️ 設定（環境変数から読み込み）
# ================================================================

DMM_API_ID       = os.environ.get('DMM_API_ID', '')
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '')

HATENA_ID        = os.environ.get('HATENA_ID', '')        # はてなID
HATENA_BLOG_ID   = os.environ.get('HATENA_BLOG_ID', '')   # 例: example.hatenablog.com
HATENA_API_KEY   = os.environ.get('HATENA_API_KEY', '')   # ブログの詳細設定＞AtomPub＞APIキー

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ 環境変数 DMM_API_ID / DMM_AFFILIATE_ID が設定されていません。')
    sys.exit(1)

if not HATENA_ID or not HATENA_BLOG_ID or not HATENA_API_KEY:
    print('❌ 環境変数 HATENA_ID / HATENA_BLOG_ID / HATENA_API_KEY が設定されていません。')
    sys.exit(1)

print('✅ 認証情報を読み込みました。')

DMM_API_BASE = 'https://api.dmm.com/affiliate/v3'

# 同人フロア固定（このツールはFANZA同人紹介専用）
SERVICE = 'doujin'
FLOOR   = 'digital_doujin'

DMM_SORT_MODE = os.environ.get('DMM_SORT_MODE', 'date').lower()
SORT_TARGETS = {'date': '-date', 'rank': '-rank'}
SORT_KEY = SORT_TARGETS.get(DMM_SORT_MODE, '-date')

_raw_start = os.environ.get('POST_START_INDEX', '')
if _raw_start.strip().isdigit():
    POST_START_INDEX = int(_raw_start.strip())
else:
    POST_START_INDEX = random.randint(1, 480)
print(f'📌 取得開始番号: {POST_START_INDEX}')

FETCH_COUNT = int(os.environ.get('FETCH_COUNT', '20'))
MAX_ARTICLES = int(os.environ.get('MAX_ARTICLES', '5'))  # 1回の実行で投稿する記事数の上限

# ================================================================
# 🔧 DMM API 関連（既存スクリプトと同じロジックを踏襲）
# ================================================================

def fetch_dmm_products():
    params = {
        'api_id':       DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      SERVICE,
        'floor':        FLOOR,
        'hits':         FETCH_COUNT,
        'offset':       POST_START_INDEX,
        'sort':         SORT_KEY,
        'output':       'json',
    }
    try:
        resp = requests.get(f'{DMM_API_BASE}/ItemList', params=params, timeout=15)
        data = resp.json()
        items = data.get('result', {}).get('items', [])
        if isinstance(items, dict):
            items = items.get('item', [])
        print(f'✅ DMM APIから {len(items)} 件取得しました。')
        return items
    except Exception as e:
        print(f'❌ DMM APIエラー: {e}')
        return []


def parse_product(item):
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

    genres = [g.get('name', '') for g in (item.get('iteminfo', {}).get('genre') or [])]
    maker  = ((item.get('iteminfo', {}).get('maker') or [{}])[0]).get('name', '')

    review_info = item.get('review', {}) or {}
    try:
        review_avg   = float(review_info.get('average', 0) or 0)
        review_count = int(review_info.get('count', 0) or 0)
    except (ValueError, TypeError):
        review_avg, review_count = 0.0, 0
    review_avg   = round(review_avg, 2) if review_avg else None
    review_count = review_count if review_count else None

    package_image = ''
    img = item.get('imageURL', {}) or {}
    package_image = img.get('large') or img.get('small') or ''

    # サンプル画像（作品紹介用に複数枚）
    sample_images = []
    sample_url_block = item.get('sampleImageURL', {}) or {}
    for key in ('sample_l', 'sample_s'):
        block = sample_url_block.get(key) or {}
        images = block.get('image') or []
        if isinstance(images, str):
            images = [images]
        if images:
            sample_images = [u for u in images if u]
            break

    return {
        'title':         title,
        'affiliate_url': affiliate_url,
        'price':         price_str,
        'price_num':     price_num,
        'genres':        genres,
        'maker':         maker,
        'review_avg':    review_avg,
        'review_count':  review_count,
        'package_image': package_image,
        'sample_images': sample_images,
    }


# ================================================================
# 📝 記事本文生成
# ================================================================

def get_article_body_ai(product: dict) -> dict:
    """
    Claude APIが使える場合はAI生成、使えない場合はテンプレートに
    フォールバックする。誇張表現や扇情的な煽り文句は避け、
    作品情報（ジャンル・価格・サークル・レビュー）に基づいた
    落ち着いた紹介文にする。
    戻り値: {'overview': str, 'points': [str, ...]}
    """
    if ANTHROPIC_API_KEY:
        try:
            return _get_article_body_from_api(product)
        except Exception as e:
            print(f'    ⚠️ AI記事生成エラー（テンプレート使用）: {e}')
    return _get_article_body_template(product)


def _get_article_body_from_api(product: dict) -> dict:
    genre_str = '・'.join(product['genres'][:5]) if product['genres'] else '不明'
    review_str = (
        f"平均{product['review_avg']}点（{product['review_count']}件のレビュー）"
        if product.get('review_avg') and product.get('review_count')
        else '不明'
    )
    prompt = (
        "FANZA同人（成人向け）作品を紹介するはてなブログ記事の本文材料を作成してください。\n\n"
        f"作品名: {product['title']}\n"
        f"ジャンル: {genre_str}\n"
        f"サークル: {product.get('maker') or '不明'}\n"
        f"価格: {product.get('price') or '不明'}\n"
        f"レビュー: {review_str}\n\n"
        "条件:\n"
        "- 誇張・扇情的な煽り文句（『必見』『話題沸騰』等）は使わない\n"
        "- 作品タイトルとジャンル・価格から素直に読み取れる範囲の紹介にとどめる\n"
        "- 未成年を想起させる表現は一切使わない（成人向け作品であることを前提にする）\n"
        "- OVERVIEWは作品タイトルを本文中で繰り返さない（タイトルは見出しに既に表示されている）。\n"
        "  代わりに、ジャンルから読み取れる「どんな魅力があるか」「どういうシチュエーション・\n"
        "  関係性の話か」を、素直に想像できる範囲で自然な文章として書く\n"
        "- POINTSに価格やレビュー点数など数値情報は含めない（別欄に表示済みのため）\n"
        "- POINTSは『○○要素が中心のストーリー』のような機械的な言い回しを使わない。\n"
        "  各ジャンル・特徴が読者にとってどう楽しめる要素なのかを、項目ごとに違う言い回しで自然な文にする\n"
        "- 出力は必ず次のプレーンテキスト形式のみ。前置きや説明・Markdown記法は禁止。\n\n"
        "===OVERVIEW===\n"
        "(150〜250文字程度で作品の魅力・シチュエーションを1〜2段落。段落間は空行で区切る)\n"
        "===POINTS===\n"
        "(「ここがポイント」として3〜4個、1行1項目、先頭に「- 」を付ける)\n"
    )
    resp = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        json={
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 600,
            'messages': [{'role': 'user', 'content': prompt}],
        },
        timeout=15,
    )
    data = resp.json()
    text = data.get('content', [{}])[0].get('text', '').strip()
    if not text or '===OVERVIEW===' not in text or '===POINTS===' not in text:
        raise ValueError('unexpected AI response format')

    overview_part = text.split('===OVERVIEW===', 1)[1].split('===POINTS===', 1)[0].strip()
    points_part = text.split('===POINTS===', 1)[1].strip()
    points = [
        line.strip().lstrip('-').strip()
        for line in points_part.splitlines()
        if line.strip().lstrip('-').strip()
    ]
    if not overview_part or not points:
        raise ValueError('empty overview or points')
    return {'overview': overview_part, 'points': points[:4]}


_GENRE_POINT_TEMPLATES = [
    '{g}が好きな方に楽しめる内容です',
    '{g}をテーマにした場面が描かれています',
    '{g}の要素が味わえる一作です',
    '{g}好きにおすすめできる仕上がりです',
]


def _get_article_body_template(product: dict) -> dict:
    genre_str = '、'.join(product['genres'][:5]) if product['genres'] else '不明'
    overview = f"{genre_str}系の同人作品です。"
    if product.get('maker'):
        overview += f" サークルは{product['maker']}。"
    overview += "\n\n気になる方は下記の作品ページで詳細をご確認ください。"

    points = []
    for i, g in enumerate((product.get('genres') or [])[:3]):
        tmpl = _GENRE_POINT_TEMPLATES[i % len(_GENRE_POINT_TEMPLATES)]
        points.append(tmpl.format(g=g))
    if product.get('review_avg') and product.get('review_count'):
        points.append(f"レビュー平均{product['review_avg']}点（{product['review_count']}件）")
    if not points:
        points = ['作品ページで詳細をチェック']

    return {'overview': overview, 'points': points[:4]}


def _paragraphs_to_html(text: str) -> str:
    """空行区切りの段落テキストを <p> タグ単位のHTMLに変換する。"""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    html_parts = []
    for p in paragraphs:
        # 段落内の単一改行は <br> に変換
        p_html = escape(p).replace('\n', '<br>')
        html_parts.append(
            f'<p style="line-height:1.8;color:#333;margin:0 0 12px;">{p_html}</p>'
        )
    return '\n'.join(html_parts)


_BADGE_COLORS = ['#ff6f91', '#ff9671', '#845ec2', '#4b93ff', '#00c2a8']


def _genre_badges_html(genres: list) -> str:
    if not genres:
        return ''
    badges = []
    for i, g in enumerate(genres[:5]):
        color = _BADGE_COLORS[i % len(_BADGE_COLORS)]
        badges.append(
            f'<span style="display:inline-block;background:{color};color:#fff;'
            'padding:4px 12px;border-radius:999px;font-size:12px;font-weight:bold;'
            f'margin:2px 4px 2px 0;">{escape(g)}</span>'
        )
    return f'<div style="margin:8px 0;">{"".join(badges)}</div>'


def _star_rating_html(avg, count) -> str:
    if not avg or not count:
        return ''
    filled = max(0, min(5, round(avg)))
    stars = '★' * filled + '☆' * (5 - filled)
    return (
        '<div style="margin:6px 0;">'
        f'<span style="color:#f5a623;font-size:18px;letter-spacing:1px;">{stars}</span> '
        f'<span style="color:#888;font-size:13px;">{avg}点（{count}件のレビュー）</span>'
        '</div>'
    )


def _points_list_html(points: list) -> str:
    if not points:
        return ''
    items = ''.join(
        f'<li style="margin:6px 0;line-height:1.6;color:#333;">{escape(pt)}</li>'
        for pt in points
    )
    return (
        '<div style="background:#fef6f8;border-left:4px solid #e0507a;border-radius:8px;'
        'padding:12px 16px;margin:14px 0;">'
        '<div style="font-weight:bold;color:#e0507a;margin-bottom:6px;">✓ ここがポイント</div>'
        f'<ul style="margin:0;padding-left:20px;">{items}</ul>'
        '</div>'
    )


def _sample_gallery_html(affiliate_url: str, sample_images: list, title: str) -> str:
    imgs = [u for u in (sample_images or []) if u][:4]
    if not imgs:
        return ''
    cells = []
    for url in imgs:
        cells.append(
            f'<a href="{escape(affiliate_url)}" target="_blank" rel="nofollow" '
            'style="flex:1 1 45%;min-width:120px;">'
            f'<img src="{escape(url)}" alt="{escape(title)} サンプル画像" loading="lazy" '
            'style="width:100%;height:auto;border-radius:8px;display:block;'
            'box-shadow:0 1px 6px rgba(0,0,0,0.1);"></a>'
        )
    return (
        '<div style="margin:16px 0;">'
        '<div style="font-weight:bold;color:#555;margin-bottom:8px;font-size:14px;">作品サンプル</div>'
        '<div style="display:flex;flex-wrap:wrap;gap:8px;">' + ''.join(cells) + '</div>'
        '</div>'
    )


def build_article(product: dict) -> dict:
    body_content = get_article_body_ai(product)
    overview_html = _paragraphs_to_html(body_content['overview'])
    points_html = _points_list_html(body_content['points'])
    genre_badges_html = _genre_badges_html(product.get('genres', []))
    star_html = _star_rating_html(product.get('review_avg'), product.get('review_count'))
    gallery_html = _sample_gallery_html(
        product.get('affiliate_url', ''), product.get('sample_images', []), product.get('title', '')
    )

    meta_line_parts = []
    if product.get('maker'):
        meta_line_parts.append(f'サークル: {escape(product["maker"])}')
    meta_line_html = ''
    if meta_line_parts:
        meta_line_html = (
            '<div style="color:#666;font-size:13px;margin:4px 0 10px;">'
            + ' ／ '.join(meta_line_parts) + '</div>'
        )

    image_html = ''
    if product.get('package_image'):
        img_url = escape(product['package_image'])
        image_html = (
            f'<a href="{escape(product["affiliate_url"])}" target="_blank" rel="nofollow">'
            f'<img src="{img_url}" alt="{escape(product["title"])}" loading="lazy" '
            'style="width:100%;max-width:560px;height:auto;border-radius:10px;'
            'display:block;margin:0 auto 16px;box-shadow:0 2px 10px rgba(0,0,0,0.12);">'
            '</a>'
        )

    price_badge_html = ''
    if product.get('price'):
        price_badge_html = (
            '<div style="display:inline-block;background:#fff0f5;color:#e0507a;'
            'border:1px solid #ffc2d6;border-radius:8px;padding:6px 14px;'
            f'font-size:15px;font-weight:bold;margin:10px 0;">価格 {escape(product["price"])}</div>'
        )

    overview_section_html = (
        '<div style="margin-top:14px;">'
        '<div style="font-weight:bold;color:#555;margin-bottom:6px;font-size:14px;">作品概要</div>'
        f'{overview_html}</div>'
    )

    cta_html = (
        f'<div style="text-align:center;margin:20px 0 8px;">'
        f'<a href="{escape(product["affiliate_url"])}" target="_blank" rel="nofollow" '
        'style="display:inline-block;padding:14px 36px;background:linear-gradient(135deg,#ff6f91,#e0507a);'
        'color:#fff;text-decoration:none;border-radius:999px;font-size:16px;font-weight:bold;'
        'box-shadow:0 4px 12px rgba(224,80,122,0.35);">'
        '▶ 作品ページを見る</a></div>'
    )

    disclaimer_html = (
        '<p style="color:#999;font-size:12px;line-height:1.6;margin-top:16px;">'
        '※本記事はアフィリエイトプログラムによる収益を得ています。#PR<br>'
        '※成人向けコンテンツを含みます。18歳未満の方はご利用いただけません。</p>'
    )

    card_inner = '\n'.join(
        part for part in [
            image_html,
            f'<h3 style="margin:0 0 6px;font-size:20px;line-height:1.4;">{escape(product["title"])}</h3>',
            meta_line_html,
            genre_badges_html,
            star_html,
            price_badge_html,
            overview_section_html,
            points_html,
            gallery_html,
            cta_html,
            disclaimer_html,
        ] if part
    )

    body_html = (
        '<div style="max-width:600px;margin:0 auto;padding:20px;border:1px solid #eee;'
        'border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,0.06);font-family:'
        '-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',sans-serif;">'
        f'{card_inner}</div>'
    )

    genre_categories = product['genres'][:5] if product['genres'] else []

    return {
        'title':      product['title'],
        'body':       body_html,
        'categories': genre_categories + ['FANZA同人', 'PR'],
    }


# ================================================================
# 🔐 はてなブログ AtomPub 投稿（WSSE認証・下書き固定）
# ================================================================

def _wsse_header(username: str, api_key: str) -> str:
    now = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    nonce_raw = os.urandom(16)
    digest = hashlib.sha1(nonce_raw + now.encode('utf-8') + api_key.encode('utf-8')).digest()
    return (
        f'UsernameToken Username="{username}", '
        f'PasswordDigest="{base64.b64encode(digest).decode()}", '
        f'Nonce="{base64.b64encode(nonce_raw).decode()}", '
        f'Created="{now}"'
    )


def post_draft_to_hatena(article: dict) -> bool:
    endpoint = f'https://blog.hatena.ne.jp/{HATENA_ID}/{HATENA_BLOG_ID}/atom/entry'

    categories_xml = ''.join(
        f'<category term="{escape(c)}" />' for c in article['categories'] if c
    )

    entry_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="http://www.w3.org/2005/Atom"
       xmlns:app="http://www.w3.org/2007/app">
  <title>{escape(article['title'])}</title>
  <author><name>{escape(HATENA_ID)}</name></author>
  <content type="text/html">{escape(article['body'])}</content>
  {categories_xml}
  <app:control>
    <app:draft>yes</app:draft>
  </app:control>
</entry>'''

    headers = {
        'Content-Type': 'application/atom+xml;type=entry',
        'X-WSSE': _wsse_header(HATENA_ID, HATENA_API_KEY),
    }

    try:
        resp = requests.post(endpoint, data=entry_xml.encode('utf-8'), headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            print(f"    ✅ 下書き投稿成功: {article['title'][:40]}")
            return True
        else:
            print(f"    ❌ 投稿失敗 status={resp.status_code}: {resp.text[:300]}")
            return False
    except Exception as e:
        print(f"    ❌ 投稿エラー: {e}")
        return False


# ================================================================
# 🚀 メイン実行
# ================================================================

def main():
    raw_items = fetch_dmm_products()
    if not raw_items:
        print('❌ 商品が取得できませんでした。')
        sys.exit(1)

    products = [parse_product(item) for item in raw_items]

    skipped = []
    safe_products = []
    for p in products:
        ok, matched = is_safe(p)
        if ok:
            safe_products.append(p)
        else:
            skipped.append((p, matched))

    print(f'🛡️ 安全フィルター適用: {len(products)}件 → 対象 {len(safe_products)}件 '
          f'(除外 {len(skipped)}件)')

    if skipped:
        Path('outputs').mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        skip_path = Path('outputs') / f'skipped_{ts}.txt'
        with open(skip_path, 'w', encoding='utf-8') as f:
            f.write('# 年少者連想ワードにより除外された作品一覧\n\n')
            for p, matched in skipped:
                f.write(f"- {p['title']}\n  マッチ語: {', '.join(matched)}\n")
        print(f'📄 除外ログ: {skip_path}')

    safe_products = safe_products[:MAX_ARTICLES]

    if not safe_products:
        print('⚠️ 投稿対象の作品がありませんでした（フィルターで全件除外、または取得0件）。')
        sys.exit(0)

    posted = 0
    for p in safe_products:
        print(f"\n📝 記事生成中: {p['title'][:40]}")
        article = build_article(p)
        if post_draft_to_hatena(article):
            posted += 1

    print(f'\n✅ 完了！{posted}/{len(safe_products)} 件をはてなブログに下書き投稿しました。')
    print('   ※ 公開前に必ず内容をご確認ください。')


if __name__ == '__main__':
    main()
