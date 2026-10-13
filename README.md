# X成長オートパイロット（Buffer連携）

自動化するのは「投稿の作成・画像カード生成・予約」。フォロー/いいね/リプの自動化は
Xの自動化ルールに触れやすくアカウント凍結リスクがあるため、意図的に入れていません。

## セットアップ（初回のみ・約15分）
1. GitHubで **公開(Public)リポジトリ** を作り、このフォルダの中身をpush
   （画像を `raw.githubusercontent.com` で公開するため。投稿文は元々公開されるものだけです）
2. `config.yaml` の `image_base_url` の `YOUR_NAME/YOUR_REPO` を自分のものに変更
3. BufferのAPIキーを取得: https://publish.buffer.com/settings/api
4. ローカルで `pip install -r requirements.txt` →
   `BUFFER_API_KEY=xxx python autopost.py channels` でチャンネルIDを確認
5. リポジトリの Settings > Secrets and variables > Actions に登録
   - `BUFFER_API_KEY` / `BUFFER_CHANNEL_ID`
   - `ANTHROPIC_API_KEY`（任意。入れると投稿が自動で補充される）
6. Actions タブで `autopost` を手動実行 → 以降は毎朝6時(JST)に自動

## 事前確認コマンド
    python autopost.py schedule --dry-run     # 予約内容の確認（API呼び出しなし）
    python autopost.py render --force         # 画像を作り直す
    python autopost.py export-csv --limit 10  # API不使用: Buffer手動CSV取り込み用

## 仕組み
- `queue.json` が投稿キュー（現在100件入り: 10/13〜11/6）
- 1日4枠: 07:30 M(気づき) / 12:15 N(プロンプト+画像) / 19:00 E(問いかけ) / 21:45 S(保存版+画像)
- 未予約が8件を下回ると Claude が1週間分を自動生成（ハッシュタグ/URL禁止・文字数検証つき）
- Buffer側に積む件数は `max_scheduled_in_buffer` で制限（無料プランの枠対策）

## 自動化できない、でも一番効く作業（毎日15分）
1. 19:00投稿の後1時間は、付いたリプに全部返信する
2. 同ジャンル（AI・時短）の伸びている投稿に、1日5〜10件、役に立つ一言リプを手動でつける
3. 週1回、インプレッション上位3投稿の型を見て、`config.yaml` の `theme` や投稿比率を調整する
