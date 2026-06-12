# 札幌市公共施設予約 空き状況監視

[札幌市公共施設予約情報システム](https://yoyaku.harp.lg.jp/sapporo/) の空き状況を定期監視し、
サッカー・フットサル施設に新たな空き枠（キャンセル・受付開始・抽選受付）が出たら Discord に通知する個人用ツール。

- 📄 [要件定義書](docs/要件定義書.md) / [機能設計書](docs/機能設計書.md) / [環境定義書](docs/環境定義書.md) / [API調査報告書](docs/調査報告_API構造と監視対象施設.md)
- 実行基盤: GitHub Actions（15分間隔、JST 6:00–24:00）
- 監視対象: Tier 1（優先・15分間隔）18施設 ＋ Tier 2（60分間隔）43施設 → [config/config.yaml](config/config.yaml)

## 仕組み

```
GitHub Actions (cron 15分)
  → GetDay API で施設ごとの空き状況を取得（14日先まで・時間枠単位）
  → 前回スナップショット (state/snapshot.json) と比較
  → 「申込不可 → 申込可」に変化した枠を抽出
  → 曜日・時間帯フィルタ適用 → Discord Webhook へ通知
  → スナップショットをコミットして次回へ引き継ぎ
```

予約の自動実行は行わない（通知のみ）。

## セットアップ

### 1. Discord Webhook を用意

1. 通知を受けたい Discord サーバーでチャンネルを作成（例: `#施設空き通知`）
2. チャンネル設定 → 連携サービス → ウェブフック → 新しいウェブフック → URL をコピー
3. スマホに Discord アプリを入れ、そのチャンネルの通知を ON にする

### 2. GitHub リポジトリを作成して push

```bash
# GitHub で public リポジトリを作成してから
git remote add origin https://github.com/<あなたのユーザー名>/sapporo-yoyaku-monitor.git
git push -u origin main
```

### 3. Webhook URL を Secrets に登録

リポジトリの Settings → Secrets and variables → Actions → New repository secret

- Name: `DISCORD_WEBHOOK_URL`
- Secret: 手順1でコピーした URL

### 4. 動作確認

Actions タブ → 「空き状況監視」 → Run workflow で手動実行。

- 初回実行はスナップショット作成のみで通知されない（仕様）
- 2回目以降、空きの変化があれば通知される

## ローカルでの動作確認

```bash
pip install -r requirements.txt

python src/monitor.py --dry-run --limit 3   # 先頭3施設だけ取得して動作確認（通知・保存なし）
python src/monitor.py --dry-run --only 0025 # 施設コード指定
python src/monitor.py --tier 1 --dry-run    # Tier 1 全件
```

## 設定変更（config/config.yaml）

コードを触らずに以下を変更できる：

| 設定 | 内容 |
|---|---|
| `facilities` | 監視施設の追加・削除、`tier: 1`（15分間隔）/`tier: 2`（60分間隔）の入れ替え |
| `notify_windows` | 曜日ごとの通知対象時間帯（複数区間可）。初期値: 平日18–22時、土日終日 |
| `notify_statuses` | 「空き」とみなすステータス。`A02`（空き表示のみ）を足すことも可能 |
| `date_range_days` | 何日先まで監視するか（初期値14） |

ステータスコードの意味は[調査報告書 3章](docs/調査報告_API構造と監視対象施設.md)を参照。

## 運用上の注意

- 15分間隔の起動は **cron-job.org（外部cron）からの workflow_dispatch** が主経路。GitHub の schedule はバックアップ（GitHub 側の都合で大幅に間引かれることがあるため）。起動用トークンの期限切れ等で失敗が続くと cron-job.org からメール通知が届く（詳細は [環境定義書](docs/環境定義書.md)）

- 対象は公共システムのため、リクエスト間隔（`request_interval_seconds`）を詰めない・施設を無闇に増やさないこと
- 深夜帯（JST 0:00–6:00）は監視しない設計（cron と `active_hours` の両方で制御）
- 取得が3回連続で失敗すると 🔴 エラー通知が届く。サイトのメンテナンス・仕様変更・WAF ブロックの可能性があるので Actions ログを確認する
- 本ツールは非公式。サイト仕様の変更により動かなくなる可能性がある
