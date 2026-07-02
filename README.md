# はてなブログ 自動下書き投稿ツール セットアップ

## 追加されたファイル

- `age_safety_filter.py` … 未成年連想ワードによる除外フィルター
- `hatena_blog_poster.py` … DMM APIから同人作品を取得し、記事を生成してはてなブログへ**下書き**投稿するスクリプト
- `.github/workflows/hatena_blog_post.yml` … 上記を実行するGitHub Actionsワークフロー

## 動作の要点

1. DMM APIで同人作品を取得
2. `age_safety_filter.py` で未成年を想起させるキーワードを含む作品を除外（除外リストは `outputs/skipped_*.txt` に記録）
3. 残った作品ごとに記事本文を生成（1作品＝1記事）
4. はてなブログAtomPub APIへ **下書き（非公開）** として投稿
5. 公開は必ず人間が内容を確認してから、はてなブログの管理画面で行ってください

このワークフローは自動公開を行いません。意図的にそう設計してあります。

## 必要なGitHub Secrets

リポジトリの Settings → Secrets and variables → Actions で以下を登録してください。

| Secret名 | 内容 |
|---|---|
| `DMM_API_ID` | DMMアフィリエイトAPIのID（既存のものを流用可） |
| `DMM_AFFILIATE_ID` | DMMアフィリエイトID（既存のものを流用可） |
| `HATENA_ID` | はてなID（ログインID） |
| `HATENA_BLOG_ID` | ブログのドメイン（例: `example.hatenablog.com`） |
| `HATENA_API_KEY` | はてなブログ「詳細設定」ページ内の「AtomPub」欄に表示されるAPIキー（ログインパスワードとは別物） |
| `ANTHROPIC_API_KEY` | （任意）記事本文をAI生成したい場合。未設定でもテンプレートで動作します |

`HATENA_API_KEY` の確認場所: はてなブログ管理画面 → 設定 → 詳細設定 → AtomPub → APIキー

## はてな側で確認・設定してほしいこと

- 対象ブログを「アダルトカテゴリ」設定にしているか（はてなブログの規約上、成人向けアフィリエイトコンテンツを扱う場合は必須です）
- 年齢確認の導線（訪問者への注意喚起）を用意しているか
- アフィリエイト表記（PR表記）が景品表示法・ステルスマーケティング規制に沿っているか

これらはツールの範囲外（運用者の責任）です。

## 実行方法

GitHub Actions タブ → 「FANZA同人 → はてなブログ 下書き生成」→ Run workflow

## キーワードフィルターの調整

`age_safety_filter.py` の `EXCLUDE_KEYWORDS` に語を追加すると検知が強化されます。
削除・弱体化する変更は安全性を下げるため、慎重に検討してください。
