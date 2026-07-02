"""
💰🐦 DMMアフィリエイト → X（Twitter）投稿文ジェネレーター
DMMから商品情報を取得し、X投稿用テキストをデスクトップまたは指定フォルダに保存します。
（X APIは使わないので完全無料で動作します）
"""

import os
import sys
import datetime
import requests
import random
from pathlib import Path

# ================================================================
# ⚙️  設定（環境変数から読み込み）
# ================================================================

DMM_API_ID       = os.environ.get('DMM_API_ID', '')
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '')

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ 環境変数 DMM_API_ID / DMM_AFFILIATE_ID が設定されていません。')
    sys.exit(1)

print('✅ 認証情報を読み込みました。')

DMM_FLOOR = os.environ.get('DMM_FLOOR', 'videoa')

# ----------------------------------------------------------------
# 📌 ソートモード設定
# ----------------------------------------------------------------
DMM_SORT_MODE = os.environ.get('DMM_SORT_MODE', 'both').lower()

SORT_TARGETS = {
    'both': [('-date', '新着順'), ('-rank', '人気順')],
    'date': [('-date', '新着順')],
    'rank': [('-rank', '人気順')],
}
SORT_LIST = SORT_TARGETS.get(DMM_SORT_MODE, SORT_TARGETS['both'])

# ----------------------------------------------------------------
# 🎲 取得開始位置（環境変数未設定時はランダム: 1〜480）
# ----------------------------------------------------------------
_raw_start = os.environ.get('POST_START_INDEX', '')
if _raw_start.strip().isdigit():
    POST_START_INDEX = int(_raw_start.strip())
    print(f'📌 指定された取得開始番号: {POST_START_INDEX}')
else:
    POST_START_INDEX = random.randint(1, 480)
    print(f'🎲 ランダム取得開始番号: {POST_START_INDEX}')

FETCH_COUNT = 100
DMM_OFFSET  = POST_START_INDEX
DMM_HITS    = FETCH_COUNT

# ----------------------------------------------------------------
# 💰 価格フィルター設定
# ----------------------------------------------------------------
DMM_PRICE_RANGE = os.environ.get('DMM_PRICE_RANGE', 'all').strip().lower()

def parse_price_range(range_str):
    if not range_str or range_str == 'all':
        return None
    range_str = range_str.replace('円', '').replace(',', '').strip()
    if '-' not in range_str:
        return None
    min_part, max_part = range_str.split('-', 1)
    try:
        price_min = int(min_part.strip()) if min_part.strip() else 0
    except ValueError:
        price_min = 0
    if max_part.strip():
        try:
            price_max = int(max_part.strip())
        except ValueError:
            price_max = None
    else:
        price_max = None
    return (price_min, price_max)

PRICE_RANGE_BOUNDS = parse_price_range(DMM_PRICE_RANGE)
if PRICE_RANGE_BOUNDS:
    _pmin, _pmax = PRICE_RANGE_BOUNDS
    _pmax_label = f'{_pmax:,}円' if _pmax is not None else '上限なし'
    print(f'💰 価格フィルター: {_pmin:,}円 〜 {_pmax_label}')
else:
    print('💰 価格フィルター: なし（すべての価格を対象）')

DMM_API_BASE = 'https://api.dmm.com/affiliate/v3'

FLOOR_SERVICE_MAP = {
    'videoa':  ('digital', 'videoa'),
    'videoc':  ('digital', 'videoc'),
    'anime':   ('digital', 'anime'),
    'doujin':  ('doujin',  'digital_doujin'),
    'comic':   ('ebook',   'comic'),
    'goods':   ('mono',    'goods'),
    'digital': ('digital', 'videoa'),
}

HASHTAG_MAP = {
    'videoa': '#FANZA #DMM #アダルト #PR',
    'videoc': '#FANZA #DMM #素人 #PR',
    'anime':  '#FANZA #DMM #アニメ #PR',
    'doujin': '#FANZA #DMM #同人 #PR',
    'comic':  '#DMM #電子書籍 #漫画 #PR',
    'goods':  '#DMM #グッズ #PR',
    'default': '#DMM #PR',
}

# ================================================================
# 🤖 AI投稿文生成
#
# 【戦略】
#   競合分析から判明した2つの重要な施策をコードに組み込む。
#
#   1. 「サービス新規紹介料」を狙う
#      FANZAを初めて使う人が購入すると、通常報酬に加えて
#      1,050〜5,240円の固定ボーナスが発生する。
#      → 「FANZAを使ったことがない人」に響く文章を優先する。
#
#   2. 「伸ばす投稿（hook投稿）」と「売る投稿（cv投稿）」を分ける
#      hook投稿: アフィリリンクなし。ギャップ・意外性で自然にバズらせ
#               フォロワー獲得・インプレッションを稼ぐ。
#      cv投稿:   アフィリリンクあり。hook投稿で温まったフォロワーに
#               購買意欲の高いCTAで購入を促す。
#      → 生成比率: hook 1本 + cv 1本 を1セットとして出力する。
# ================================================================

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ----------------------------------------------------------------
# フォールバックテンプレート（hook / cv 両用）
# ----------------------------------------------------------------
HOOK_TEMPLATES_BY_GENRE = {
    'ラブコメ': [
        "FANZAって使ったことない人に聞きたいんだけど\nラブコメ系の電子書籍が普通に揃ってるの知ってる？\nアダルトのイメージ強いと思うけど普通に読める作品も多い",
        "なんとなく敬遠してたFANZA、試しに使ってみたら\nラブコメ系でこのクオリティは予想外だった\nアカウント作るだけなら無料なのも地味に助かった",
    ],
    '巨乳': [
        "FANZAを使わない理由って何？\n・登録がめんどくさい\n・課金が不安\n・そもそも知らない\nどれかあてはまるなら損してるかも",
        "FANZA歴3年の自分が一番後悔してること\nもっと早く使い始めればよかったってやつ\n最初のハードルが全然大したことなかった",
    ],
    '人妻・主婦': [
        "FANZAに登録するのって実は5分かからないの知ってる？\nクレカ不要で始められるし\n最初の壁が思ったよりずっと低かった",
        "FANZA未経験の人に一番多い誤解\n「会員登録したら高額請求される」\nこれ完全にデマです。無料会員のまま使える機能も多い",
    ],
    '制服': [
        "FANZAが初めての人に伝えたいこと\n無料で見られるサンプルがめちゃくちゃ充実してる\n本購入前に確認できるから失敗しない",
        "意外と知られてないんだけどFANZAって\n登録→閲覧→購入がぜんぶスマホで完結する\nアプリもあるしUI思ったより全然いい",
    ],
    '熟女': [
        "FANZAって名前は聞いたことあるけど\n実際どんなサービスか知ってる人、意外と少ない\n使い方まとめてみたら想像より全然シンプルだった",
    ],
    'VR': [
        "VRコンテンツに興味あるけど何から始めればいい？\nって聞かれることが増えてきたので\nFANZAのVRが一番選択肢が多い理由を説明する",
        "VRのアダルトコンテンツって\nどこで配信されてるか知ってる？\n実はFANZAが圧倒的に品揃えがいい",
    ],
    '4時間以上作品': [
        "コスパで選ぶなら長尺作品が圧倒的にお得\nFANZAの4時間以上作品って\n1時間あたりの単価を計算するとかなり安い",
    ],
}

HOOK_TEMPLATES_FALLBACK = [
    "FANZAを使ったことない人に聞きたいんだけど\n使わない理由ってなんですか？\nほとんどの不安って実は誤解から来てると思う",
    "FANZA歴5年の自分が最初につまずいたこと\n実はそんな大したことじゃなかった\n登録から最初の購入まで正直15分かからなかった",
    "FANZAって実際どんなサービス？\nって聞かれることが増えてきた\n一言で言うと動画も本もグッズも全部あるやつ",
    "FANZA未経験の人が思ってる不安、3つ答えます\n①登録が大変そう → 5分以内\n②高そう → 100円からある\n③バレそう → 通帳には「DMM」とだけ表示",
    "アダルトコンテンツって\nどこで買うのが一番安全か知ってる？\n大手サービスを使うべき理由がちゃんとある",
]

CV_TEMPLATES_BY_GENRE = {
    'ラブコメ': [
        "このジャンル好きな人には刺さる設定だった💕\nレビュー評価も高く、価格も手が出しやすい範囲",
        "ラブコメ好きなら後悔しない一作\n初回購入なら新規報酬も出るので試してみて",
    ],
    '巨乳': [
        "ボリューム感とキャラの可愛さが両立してる\nこのジャンル好きなら試す価値あり",
        "絵柄がツボすぎた。これ系好きな人に素直におすすめ\nレビュー数も多くて安定してる",
    ],
    '人妻・主婦': [
        "人妻もので外れ引いたことほぼない、これも安定してた\n禁断感の演出がうまい作品です",
        "背徳感のある設定が好きな人には刺さる構成\nレビュー評価も高く選んで損はない",
    ],
    '制服': [
        "学園ものは設定の作り込みで全然変わるけど\nこれはちゃんとしてる。制服好きには刺さる✏️",
        "このジャンルの安定感ってあるよね\n今回も期待を裏切らなかった、詳細で確認してみて",
    ],
    '熟女': [
        "大人の色気をちゃんと表現できてる作品\n熟女もので完成度高いのは貴重です",
        "このジャンル好きなら間違いなく刺さる\n高評価レビューが多い信頼できる一本",
    ],
    '近親相姦': [
        "タブー設定が好きな人には刺さる構成\n後味がクセになる系で詳細で確認してみて",
    ],
    '寝取り・寝取られ・NTR': [
        "NTR好きにはわかるこの感覚\nNTR耐性ある人なら最後まで引き込まれると思う",
    ],
    '調教': [
        "展開の流れが読んでてテンション上がる🔥\n進行の緩急がうまい。このジャンル好きなら損しない",
    ],
    '中出し': [
        "内容の密度がしっかりある作品、薄いやつとは違う\nこのジャンルでこの完成度は当たりだと思う",
    ],
    '異種姦': [
        "ファンタジー設定好きは世界観から楽しめると思う🐉\n設定の独自性があって飽きなかった",
    ],
    'VR': [
        "VR専用に作られてる感がちゃんとあって臨場感が違う\n8K対応なら画質の差を体感できる",
    ],
    '4時間以上作品': [
        "ボリューム系が好きな人には満足度が高い一本\nコスパ重視で探してる人に刺さる作品",
    ],
}

CV_TEMPLATES_FALLBACK = [
    "このジャンル好きな人には刺さると思う\nレビュー評価も高く選んで損はない",
    "深夜のお供にちょうどいい密度感🌙\n価格と内容のバランスが良い作品です",
    "タイトルの雰囲気通りの内容で期待は裏切らない\n気になるなら詳細だけでも覗いてみて",
    "こういうの待ってた人いるんじゃないかな\n今すぐ詳細ページで確認してみて",
]


def get_hook_ai(title: str, genres: list, maker: str) -> str:
    """【hook投稿用】FANZAを使ったことがない人の誤解を解き、
    興味を持たせる自然な投稿文を生成する。リンクなし・売り込みなし。"""
    if not ANTHROPIC_API_KEY:
        return _get_hook_fallback(genres)

    genre_str = '・'.join(genres) if genres else 'なし'

    prompt = (
        "FANZAアフィリエイトのX投稿で、リンクなし・売り込みなしの「フック投稿」を作ってください。\n\n"
        "【目的】\n"
        "FANZAをまだ使ったことがない人の誤解（登録が難しい・怪しい・高い等）を解いて、\n"
        "「意外と使えそう」と思わせる自然なつぶやき。バズ・いいね・フォロワー獲得が目的。\n\n"
        f"関連ジャンル：{genre_str}\n\n"
        "【条件】\n"
        "- 必ず3行構成、合計50〜90文字（改行込み）\n"
        "- 1行目：FANZAに関する疑問・意外な事実・ギャップを提示。「え、そうなの？」と思わせる導入\n"
        "- 2行目：その裏付けや具体的な理由（登録5分・クレカ不要・通帳にDMMとだけ表示など実際の情報を使う）\n"
        "- 3行目：「使わないのはもったいない」と思わせる締め。疑問形や断言でもOK\n"
        "- アフィリリンクや商品紹介は絶対に入れない（純粋な情報発信として自然に見せる）\n"
        "- AIっぽい宣伝文句（「話題沸騰中」「ファン必見」等）は絶対使わない\n"
        "- 絵文字は全体で0〜2個まで\n"
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
        if text and len(text) <= 120:
            return text
    except Exception as e:
        print(f'    ⚠️  AI hook生成エラー（フォールバック使用）: {e}')

    return _get_hook_fallback(genres)


def get_cv_ai(title: str, genres: list, maker: str, price: str, review_avg, review_count) -> str:
    """【cv投稿用】商品を紹介し購買意欲を高める2行コメントを生成する。
    特に「FANZAを初めて使う人」に刺さる文章を意識する（新規報酬狙い）。"""
    if not ANTHROPIC_API_KEY:
        return _get_cv_fallback(genres)

    genre_str = '・'.join(genres) if genres else 'なし'
    maker_str = maker if maker else '不明'

    review_info_str = ''
    if review_avg and review_count and review_count >= 5:
        review_info_str = f'平均{review_avg}点（{review_count}件）'

    if DMM_FLOOR == 'doujin':
        floor_label = '同人誌・同人CG集'
        no_sample_note = '- 「サンプルを見て」「試し読み」などサンプル動画・試読を促す表現は使わない（同人作品のため）\n'
    else:
        floor_label = 'AV・動画'
        no_sample_note = ''

    prompt = (
        f"{floor_label}のX投稿で「売る投稿（CV投稿）」の紹介本文を作ってください。\n\n"
        f"作品タイトル：{title}\n"
        f"ジャンル：{genre_str}\n"
        f"サークル/メーカー：{maker_str}\n"
        f"価格：{price if price else '不明'}\n"
        f"レビュー：{review_info_str if review_info_str else 'なし'}\n\n"
        "【目的】\n"
        "FANZAを初めて使う人も含め「この作品、今すぐ見たい/買いたい」と思わせる文章。\n"
        "初回購入者はFANZAの新規報酬（1,050〜5,240円）の対象になるので、\n"
        "FANZAを使ったことがない人の心理的ハードルも下げる表現を意識する。\n\n"
        "【条件】\n"
        "- 必ず2行構成、合計50〜80文字（改行込み）\n"
        "- 1行目（25〜40文字）：ジャンルや設定への共感・反応。そのジャンルが好きな人の気持ちに刺さる言葉\n"
        "- 2行目（25〜40文字）：今すぐ見たい/買いたいと思わせる具体的な理由。\n"
        "  レビュー件数・評価点があれば積極的に使う。\n"
        "  「FANZAが初めてでも登録5分でOK」「初回購入なら〜」等、初心者の背中を押す一言もあれば入れる。\n"
        f"- AIっぽい宣伝文句（「話題沸騰中」「クオリティに驚く」「ファン必見」等）は絶対使わない\n"
        "- そのジャンルが好きな人が普通につぶやくような自然な口語体\n"
        "- 絵文字は全体で0〜2個まで\n"
        f"{no_sample_note}"
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
        print(f'    ⚠️  AI cv生成エラー（フォールバック使用）: {e}')

    return _get_cv_fallback(genres)


def _get_hook_fallback(genres: list) -> str:
    for genre in genres:
        for key, templates in HOOK_TEMPLATES_BY_GENRE.items():
            if key in genre:
                return random.choice(templates)
    return random.choice(HOOK_TEMPLATES_FALLBACK)


def _get_cv_fallback(genres: list) -> str:
    for genre in genres:
        for key, templates in CV_TEMPLATES_BY_GENRE.items():
            if key in genre:
                return random.choice(templates)
    return random.choice(CV_TEMPLATES_FALLBACK)


# ================================================================
# 🔧 DMM API 関数
# ================================================================

def fetch_dmm_products(sort_key, sort_label):
    service, floor_name = FLOOR_SERVICE_MAP.get(DMM_FLOOR, ('digital', 'videoa'))
    params = {
        'api_id':       DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      service,
        'floor':        floor_name,
        'hits':         DMM_HITS,
        'offset':       DMM_OFFSET,
        'sort':         sort_key,
        'output':       'json',
    }
    print(f'\n  [{sort_label}] 取得範囲: {DMM_OFFSET}件目〜{DMM_OFFSET + DMM_HITS - 1}件目')
    try:
        resp = requests.get(f'{DMM_API_BASE}/ItemList', params=params, timeout=15)
        data = resp.json()
        items = data.get('result', {}).get('items', [])
        if isinstance(items, dict):
            items = items.get('item', [])
        if items:
            url_str = items[0].get('affiliateURL', '')
            print(f"  URLの総文字数: {len(url_str)} / 末尾10文字: {url_str[-10:]}")
        print(f'  ✅ {len(items)} 件取得しました。')
        return items
    except Exception as e:
        print(f'  ❌ DMM APIエラー: {e}')
        return []


def parse_product(item):
    title         = item.get('title', '')
    affiliate_url = item.get('affiliateURL', '') or item.get('URL', '')
    prices        = item.get('prices', {})
    price_str     = ''
    price_num     = None
    if prices:
        price_val = prices.get('price') or prices.get('list_price') or ''
        if price_val:
            digits = ''.join(c for c in str(price_val) if c.isdigit())
            if digits:
                price_num = int(digits)
                price_str = f'\u00a5{price_num:,}'
    actors = [a.get('name', '') for a in (item.get('iteminfo', {}).get('actress') or [])][:3]
    genres = [g.get('name', '') for g in (item.get('iteminfo', {}).get('genre') or [])][:3]
    maker  = ((item.get('iteminfo', {}).get('maker') or [{}])[0]).get('name', '')

    review_info = item.get('review', {}) or {}
    try:
        review_avg   = float(review_info.get('average', 0) or 0)
        review_count = int(review_info.get('count', 0) or 0)
    except (ValueError, TypeError):
        review_avg   = 0.0
        review_count = 0
    review_avg   = round(review_avg, 2) if review_avg else None
    review_count = review_count if review_count else None

    sample_movie_url = ''
    smv = item.get('sampleMovieURL', {})
    if smv:
        for key in ['size_720_480', 'size_644_414', 'size_560_360', 'size_476_306']:
            val = smv.get(key, '')
            if val:
                sample_movie_url = val.strip()
                break

    return {
        'title':            title,
        'affiliate_url':    affiliate_url,
        'price':            price_str,
        'price_num':        price_num,
        'actors':           actors,
        'genres':           genres,
        'maker':            maker,
        'sample_movie_url': sample_movie_url,
        'review_avg':       review_avg,
        'review_count':     review_count,
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
        resp = requests.get(
            'https://tinyurl.com/api-create.php',
            params={'url': url},
            timeout=8,
        )
        if resp.status_code == 200 and resp.text.startswith('http'):
            return resp.text.strip()
    except Exception as e:
        print(f'    ⚠️  URL短縮に失敗（元のURLを使用）: {e}')
    return url


def actor_tags(actors):
    return '　'.join('#' + a.replace(' ', '').replace('　', '') for a in actors if a)


def genre_tags(genres):
    return '　'.join('#' + g.replace(' ', '').replace('　', '') for g in genres if g)


def price_in_range(product):
    if not PRICE_RANGE_BOUNDS:
        return True
    price_num = product.get('price_num')
    if price_num is None:
        return False
    price_min, price_max = PRICE_RANGE_BOUNDS
    if price_num < price_min:
        return False
    if price_max is not None and price_num > price_max:
        return False
    return True


# ================================================================
# ✂️  文字数カウント（Xの仕様: URLは23文字固定扱い）
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
# 📝 投稿文ビルダー（hook + cv の2本セット）
# ================================================================

def build_x_posts(product):
    """
    1商品あたり2本の投稿を生成して返す。

    hook_post: アフィリリンクなし。FANZAの誤解を解いてフォロワー・インプレ獲得を狙う。
               朝〜昼の投稿に向く。
    cv_post:   アフィリリンクあり。温まったフォロワーに購買を促す。
               夜の投稿に向く。新規ユーザー獲得（新規報酬）も意識した文章。
    """
    hashtags  = HASHTAG_MAP.get(DMM_FLOOR, HASHTAG_MAP['default'])
    raw_url   = clean_url(product['affiliate_url'])
    short_url = shorten_url(raw_url)
    act_tags  = actor_tags(product['actors'])

    hook_copy = get_hook_ai(product['title'], product['genres'], product.get('maker', ''))
    cv_copy   = get_cv_ai(
        product['title'],
        product['genres'],
        product.get('maker', ''),
        product['price'],
        product.get('review_avg'),
        product.get('review_count'),
    )

    title        = product['title']
    review_avg   = product.get('review_avg')
    review_count = product.get('review_count')

    # ── レビュー・メーカー情報 ────────────────────────────────────
    reason_parts = []
    if review_avg and review_count and review_count >= 5:
        reason_parts.append(f'レビュー平均{review_avg}（{review_count}件）')
    if product.get('maker'):
        reason_parts.append(f"{product['maker']}制作")

    # CTAを状況で切り替え（新規ユーザーへの訴求を強調）
    if DMM_FLOOR == 'doujin':
        cta = '作品ページを確認してみて'
        newbie_nudge = ''
    else:
        if review_count and review_count >= 20:
            cta = 'サンプルで確認してから決めて'
        elif product.get('price_num') and product['price_num'] <= 1000:
            cta = 'この価格なら気軽に試せる'
        else:
            cta = 'サンプルだけでも見てみて'
        # FANZAの新規ユーザー向けの補足（新規報酬狙い）
        newbie_nudge = 'FANZA初めてでも登録5分、すぐ見られます'

    # ── hook投稿（アフィリリンクなし）───────────────────────────
    def build_hook():
        # hookにはURLを入れない（売り込み感ゼロ）
        return hook_copy

    hook_post = build_hook()
    # hookはURLなしなので文字数オーバーはほぼないが念のためチェック
    if x_len(hook_post) > X_LIMIT:
        hook_post = hook_copy.split('\n')[0] + '\n' + hook_copy.split('\n')[1] if '\n' in hook_copy else hook_copy[:100]

    # ── cv投稿ポスト1（タイトル＋コピー＋URL）──────────────────
    def truncate_title(max_len):
        return title[:max_len] + '…' if len(title) > max_len else title

    def build_cv1(display_title, include_actor):
        lines = [f'📖 {display_title}', '', cv_copy, short_url]
        if include_actor and act_tags:
            lines.insert(-1, f'👤 {act_tags}')
        return '\n'.join(lines)

    cv1 = build_cv1(title, include_actor=True)
    if x_len(cv1) > X_LIMIT:
        cv1 = build_cv1(title, include_actor=False)
    if x_len(cv1) > X_LIMIT:
        cv1 = build_cv1(truncate_title(40), include_actor=False)
    if x_len(cv1) > X_LIMIT:
        cv1 = build_cv1(truncate_title(20), include_actor=False)
    if x_len(cv1) > X_LIMIT:
        cv_line1 = cv_copy.split('\n')[0]
        cv1 = f'📖 {truncate_title(20)}\n\n{cv_line1}\n{short_url}'

    # ── cv投稿ポスト2（詳細情報＋URL）───────────────────────────
    def build_cv2(genre_limit, include_nudge):
        lines = ['📌 気に入ったら本編はこちら👇']
        if reason_parts:
            lines.append('、'.join(reason_parts) + 'の作品。' + cta)
        if include_nudge and newbie_nudge:
            lines.append(f'💡 {newbie_nudge}')
        if product['price']:
            lines.append(f'💰 {product["price"]}')
        if product['genres'] and genre_limit > 0:
            lines.append(f'🏷 {genre_tags(product["genres"][:genre_limit])}')
        lines.append(short_url)
        lines.append(hashtags)
        return '\n'.join(lines)

    cv2 = build_cv2(genre_limit=3, include_nudge=True)
    if x_len(cv2) > X_LIMIT:
        cv2 = build_cv2(genre_limit=3, include_nudge=False)
    if x_len(cv2) > X_LIMIT:
        cv2 = build_cv2(genre_limit=1, include_nudge=False)
    if x_len(cv2) > X_LIMIT:
        cv2 = build_cv2(genre_limit=0, include_nudge=False)

    return hook_post, cv1, cv2


# ================================================================
# 💾 保存先を決定
# ================================================================

def get_save_dir():
    explicit = os.environ.get('SAVE_DIR', '').strip()
    if explicit:
        Path(explicit).mkdir(parents=True, exist_ok=True)
        return explicit

    if os.environ.get('SAVE_TO_REPO', '').lower() == 'true':
        out = Path('outputs')
        out.mkdir(exist_ok=True)
        return str(out)

    try:
        home = Path.home()
        for path in [
            home / "Desktop",
            home / "OneDrive" / "Desktop",
            home / "OneDrive" / "デスクトップ",
            home / "デスクトップ",
        ]:
            if path.exists():
                return str(path)
    except Exception:
        pass

    return '.'


def save_posts(all_sections):
    save_dir  = get_save_dir()
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename  = f'dmm_x_posts_{timestamp}.txt'
    filepath  = os.path.join(save_dir, filename)

    total = sum(len(posts) for _, posts in all_sections)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"# DMMアフィリエイト X投稿文\n")
        f.write(f"# 生成日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# フロア: {DMM_FLOOR} / モード: {DMM_SORT_MODE}\n")
        f.write(f"# 価格フィルター: {DMM_PRICE_RANGE}\n")
        f.write(f"# 取得開始: {DMM_OFFSET}件目 / 各ソート{FETCH_COUNT}件\n")
        f.write(f"# 総商品数: {total}件（1商品=hook1本+cv2本の計3投稿）\n")
        f.write("=" * 60 + "\n")
        f.write("【投稿戦略】\n")
        f.write("  hook投稿  → URLなし。朝〜昼に投稿。バズ・フォロワー獲得目的。\n")
        f.write("  cv投稿1,2 → URLあり。夜（21〜24時）に投稿。購買促進目的。\n")
        f.write("              FANZA初回購入者には新規紹介料（最大5,240円）も発生。\n")
        f.write("=" * 60 + "\n\n")

        for sort_label, posts in all_sections:
            f.write(f"{'=' * 60}\n")
            f.write(f"【{sort_label}】{len(posts)}件\n")
            f.write(f"{'=' * 60}\n\n")

            for i, (product, triplet) in enumerate(posts, 1):
                hook_post, cv1, cv2 = triplet
                h_len  = x_len(hook_post)
                c1_len = x_len(cv1)
                c2_len = x_len(cv2)
                over_h  = ' ⚠️ 超過' if h_len > X_LIMIT else ''
                over_c1 = ' ⚠️ 超過' if c1_len > X_LIMIT else ''
                over_c2 = ' ⚠️ 超過' if c2_len > X_LIMIT else ''

                f.write(f"--- {sort_label} {i}/{len(posts)} ---\n")
                f.write(f"商品名: {product['title']}\n")
                f.write(f"文字数(X換算): hook={h_len}字{over_h} / cv1={c1_len}字{over_c1} / cv2={c2_len}字{over_c2}\n")
                f.write(f"URL確認: {product['affiliate_url']}\n")
                if product.get('sample_movie_url'):
                    f.write(f"サンプル動画: {product['sample_movie_url']}\n")
                f.write("-" * 40 + "\n")
                f.write("【hook投稿（朝〜昼・URLなし・バズ狙い）】\n")
                f.write(hook_post)
                f.write("\n\n【cv投稿1（夜・タイトル＋コピー）】\n")
                f.write(cv1)
                f.write("\n\n【cv投稿2（cv1のスレッド続き・詳細＋URL）】\n")
                f.write(cv2)
                f.write("\n\n")

    print(f'\n💾 保存完了！')
    print(f'📄 ファイル: {filepath}')
    return filepath


# ================================================================
# 🚀 メイン実行
# ================================================================

print(f'🛍️  DMMから商品情報を取得中（フロア: {DMM_FLOOR} / モード: {DMM_SORT_MODE}）...')

all_sections = []

for sort_key, sort_label in SORT_LIST:
    raw_items = fetch_dmm_products(sort_key, sort_label)
    if not raw_items:
        print(f'  ⚠️  [{sort_label}] 商品が取得できませんでした。スキップします。')
        continue

    products = [parse_product(item) for item in raw_items]

    if PRICE_RANGE_BOUNDS:
        before_count = len(products)
        products = [p for p in products if price_in_range(p)]
        print(f'  💰 価格フィルター適用: {before_count}件 → {len(products)}件')

    if not products:
        print(f'  ⚠️  [{sort_label}] 価格条件に合う商品がありませんでした。スキップします。')
        continue

    print(f'  📝 [{sort_label}] 投稿文を生成中（hook + cv 各1本ずつ）...')

    posts = []
    for p in products:
        hook_post, cv1, cv2 = build_x_posts(p)
        h_len  = x_len(hook_post)
        c1_len = x_len(cv1)
        c2_len = x_len(cv2)
        warn = ' ⚠️ 超過あり' if any(l > X_LIMIT for l in [h_len, c1_len, c2_len]) else ''
        posts.append((p, (hook_post, cv1, cv2)))
        print(f"    ✅ [hook:{h_len}字 / cv1:{c1_len}字 / cv2:{c2_len}字{warn}] {p['title'][:25]}...")

    all_sections.append((sort_label, posts))

if not all_sections:
    print('❌ 商品が1件も取得できませんでした。')
    sys.exit(1)

first_label, first_posts = all_sections[0]
print('\n' + '=' * 60)
print(f'📋 投稿文プレビュー（{first_label} 1件目）')
print('=' * 60)
hook_post, cv1, cv2 = first_posts[0][1]
print('【hook投稿（朝〜昼・URLなし）】')
print(hook_post)
print('\n【cv投稿1（夜・タイトル＋コピー）】')
print(cv1)
print('\n【cv投稿2（cv1のスレッド続き）】')
print(cv2)
print('=' * 60)

save_posts(all_sections)

total = sum(len(p) for _, p in all_sections)
print(f'\n✅ 完了！合計 {total} 件の商品の投稿文を保存しました（各商品3投稿セット）。')
print('hook投稿は朝〜昼、cv投稿は夜（21〜24時）に投稿するのがおすすめです。')
