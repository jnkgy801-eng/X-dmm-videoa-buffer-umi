# -*- coding: utf-8 -*-
"""
🛡️ 年少者連想ワード フィルター

タイトル・ジャンルタグ・サークル名などに、未成年を想起させる語が
含まれる作品を自動的に除外するための安全装置です。

このフィルターは「投稿前に必ず通す」ことを前提にしています。
キーワードを緩める（削除する）場合は、除外対象がなぜ危険なのかを
理解した上で、慎重に判断してください。単純に検知漏れを増やす方向の
調整はおすすめしません。追加・強化（表記ゆれの追加など）を推奨します。
"""

import unicodedata

# ================================================================
# 除外キーワード一覧
# カテゴリ別に管理。表記ゆれ（全角/半角・大文字/小文字）にできるだけ
# 対応するため、正規化した上でマッチングする。
# ================================================================

EXCLUDE_KEYWORDS = [
    # --- 学年・学校区分を直接示す語 ---
    "JK", "女子高生", "女子校生", "女高生",
    "JC", "女子中学生", "女中生",
    "JS", "女子小学生",
    "小学生", "中学生", "高校生", "高校１年", "高校1年",
    "小学校", "中学校", "高等学校",
    "生徒", "教え子",
    "現役jk", "現役JK", "現役女子高生",

    # --- 年齢・未成年そのものを示す語 ---
    "未成年", "児童", "低年齢", "年少",

    # --- 幼さ・ロリ系ジャンル語 ---
    "ロリ", "ロリータ", "幼女", "幼児", "幼い",
    "あどけない", "あどけなさ", "幼顔", "童顔ロリ",

    # --- 学生を強く想起させる小道具・文脈語 ---
    "制服JK", "セーラー服", "学生服", "ブレザー制服",
    "ランドセル", "通学路", "下校中", "放課後デート",
    "学園祭", "修学旅行", "夏休み補講",

    # --- 家族内の年少者を想起させる語（近親+年少の組み合わせ回避）---
    "幼い妹", "幼い娘", "実の娘 小学", "実の娘 中学",
]

# 「制服」単体・「学園」単体は誤検知が多い（コスプレ・大人設定作品にも
# 頻出）ため意図的にリストへ入れていない。精度を上げたい場合は
# 個別に相談してから追加する。


def _normalize(text: str) -> str:
    """全角/半角・大文字/小文字を統一して比較しやすくする。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return text.lower()


_NORMALIZED_KEYWORDS = [_normalize(k) for k in EXCLUDE_KEYWORDS]


def find_matched_keywords(*fields) -> list:
    """
    渡された文字列群（タイトル・ジャンル・サークル名など）を走査し、
    マッチした除外キーワードのリストを返す。空リストなら該当なし。
    """
    matched = []
    for field in fields:
        if not field:
            continue
        if isinstance(field, (list, tuple)):
            joined = " ".join(str(f) for f in field)
        else:
            joined = str(field)
        norm = _normalize(joined)
        for raw_kw, norm_kw in zip(EXCLUDE_KEYWORDS, _NORMALIZED_KEYWORDS):
            if norm_kw in norm and raw_kw not in matched:
                matched.append(raw_kw)
    return matched


def is_safe(product: dict) -> tuple:
    """
    product（parse_productが返す辞書）を受け取り、
    (安全か: bool, マッチしたキーワード: list) を返す。
    """
    matched = find_matched_keywords(
        product.get("title", ""),
        product.get("genres", []),
        product.get("maker", ""),
    )
    return (len(matched) == 0, matched)
