# SupabaseのDB読み取りによる非活動停止対策

このワークフローは、既存の読み取り専用RPCを定期実行してDB利用を発生させ、その成功・失敗を記録します。**Freeプランの停止を防げると保証するものではありません。初期状態では実行OFFです。**

## 目的と変更範囲

- ワークフロー: `.github/workflows/supabase-keepalive.yml`
- 通信を模擬するローカルテスト: `.github/tests/test_supabase_keepalive.py`
- この運用手順書: `.github/SUPABASE_KEEPALIVE.md`

PWAの配信ファイル、認証処理、撮影、写真保存・復元、テンプレート、Service Worker、既存DBデータ・権限・関数は変更しません。DBの作成・更新SQLやマイグレーションを自動実行する仕組みもありません。

## 2026年11月23日に確認した前提

対象リポジトリは `hiroshitanaka010936/koujikokuban-camera`、基点はmainの `ab218d841467dd3e734c790bb016704ec66a35a4` です。基点には19個の配信用ファイルがあり、READMEとService Workerのバージョンは2.2.0、`.github/workflows` はありませんでした。

`auth.js` の接続先とSupabase側の読み取り結果を照合しました。対象プロジェクトは `wufbvfumsrwkgxsztuea` です。確認時はFreeプラン、`ACTIVE_HEALTHY` でした。この状態は確認時点のもので、将来も継続する保証ではありません。

既存の `public.kokuban_healthcheck()` は次の構成でした。

| 項目 | 確認結果 |
| --- | --- |
| 戻り値 | integer |
| SQL本体 | `SELECT 1` |
| 実行特性 | `STABLE` |
| 権限 | `SECURITY INVOKER`（`prosecdef = false`） |
| search_path | 空文字 |
| EXECUTEの明示的な付与先 | `postgres` と `anon` |
| `anon` の利用者・端末承認テーブルへの権限 | `kokuban_access` と `app_device_approvals` ともSELECT・INSERT・UPDATE・DELETE・TRUNCATEなし。列単位のSELECT・INSERT・UPDATE権限もなし |
| 他のpublic関数 | `kokuban_check_access` と `kokuban_check_device` の実行は `anon` に許可されていない |

この既存関数をそのまま利用するため、新しいDBロールやテーブルは作りません。関数がなくなった場合や権限が変わった場合、ワークフローは失敗を報告します。自動的な権限追加、関数作成、管理者キーへの切り替えは行いません。

## 実行内容と権限

固定した次のURLへ、GETを1回送ります。再試行が必要な場合だけ最大3回になります。

```text
https://wufbvfumsrwkgxsztuea.supabase.co/rest/v1/rpc/kokuban_healthcheck
```

SupabaseのPostgRESTではGETでのRPC呼び出しはREAD ONLYトランザクションになります。[PostgRESTの公式仕様](https://docs.postgrest.org/en/stable/references/transactions.html)に基づく動作です。この関数は1だけを返し、利用者・端末・写真のレコードを取得しません。

GitHub側は `permissions: {}` です。checkout、外部Action、パッケージのダウンロード、GitHub APIへの書き込みはありません。GitHubホストの `ubuntu-24.04` にあるPython 3の標準ライブラリだけで実行します。

認証には `sb_publishable_` から始まるPublishable keyを使用します。ユーザーのログイン・メール・パスワード・端末承認・セッショントークンは使いません。`sb_secret_`、legacy JWT形式の `service_role` / `anon`、DBパスワードはこのワークフローでは受け付けません。

**Publishable key自体が「この関数だけに使える鍵」なのではありません。** ユーザートークンを付けずに使うとDBでは `anon` の権限になります。今回の最小権限の判断は、上記の実DBのgrantと関数の確認に基づきます。将来、匿名ロールに別の権限を追加した場合は再確認が必要です。[SupabaseのAPIキー公式資料](https://supabase.com/docs/guides/getting-started/api-keys)を参照してください。

## 認証情報の登録

設定変更は別途承認を得てから行います。値をPR本文、ソースコード、チャット、実行ログへ貼り付けないでください。

GitHubリポジトリの **Settings → Secrets and variables → Actions** で、次を設定します。

| 種別 | 名前 | 内容 |
| --- | --- | --- |
| Repository secret | `SUPABASE_KEEPALIVE_PUBLISHABLE_KEY` | 対象プロジェクトのPublishable key。SupabaseのAPI Keys画面で確認する |
| Repository variable | `SUPABASE_KEEPALIVE_ENABLED` | 承認前は未作成または `false`。実行を承認した後にだけ、小文字の `true` にする |

Publishable keyはクライアント公開が可能な低権限キーですが、ここではローテーションとログ管理のためActions Secret経由で渡します。新しいファイルに値を埋め込みません。すでに配信中の `auth.js` のキー設定も変更しません。

Secretの値は環境変数としてPythonへ渡し、シェルコマンドへ展開しません。HTTPSの証明書検証を有効にし、送信先URLを固定し、リダイレクトを拒否します。ログには分類済みのエラーコードだけを出し、キー、HTTPヘッダー、応答本文、例外の詳細は出しません。GitHubの自動マスクだけに依存しない設計です。[GitHubのセキュリティ資料](https://docs.github.com/en/actions/reference/security/secure-use)も参照してください。

## 実行頻度

cronは `23 0,8,16 * * *`（UTC）です。日本時間では毎日 **01:23・09:23・17:23**、8時間ごと、通常1日3回・週21回の読み取りを予定します。毎時00分の混雑を避けています。

これは実装上の提案値であり、Supabaseが保証した停止回避条件ではありません。2026年11月23日確認の[公式説明](https://supabase.com/docs/guides/platform/free-project-pausing)は過去7日間の十分なユーザーDB利用が必要とし、通常は毎日数回が目安としています。[公式FAQ](https://supabase.com/kb/faqs/platform/free-project-pausing)は保証ではないことも明示しています。週1回のアクセスを十分とする根拠は確認できません。

`SELECT 1` に対する成功応答はその時点でのDB経路の正常性を確認できます。しかし、これが非活動判定にどの程度算入されるか、1日3回で停止を回避できるかは未実証です。ログインの成功、端末の承認、写真の保存・復元も検証していません。

## 承認と有効化

作業を次の3段階に分けます。

1. **PRの作成を承認:** 上記3ファイルを専用ブランチへコミットし、main向けのPRを作成します。この承認だけではmainへのマージ、Secret/Variable登録、実行開始を行いません。
2. **反映・設定・実行を承認:** mainへのマージを承認した後に反映し、Secretを登録します。設定値を確認して `SUPABASE_KEEPALIVE_ENABLED=true` に変更すると、次の定刻からDBへのアクセスが可能になります。この変更は定期実行の有効化です。
3. **初回確認:** 有効化を承認した範囲で、Actions → Supabase DB activity check → Run workflowからmainを指定して1回実行し、実際のDB読み取りが成功したことを確認します。続いて定期実行を確認します。

定期実行・手動実行は、元リポジトリのmainかつ有効化変数が `true` の場合だけジョブを起動します。push・pull_requestではDBアクセスを開始しません。PRだけの状態では定期実行は始まりません。手動実行機能もワークフローがdefault branchに存在する必要があります。

変数が未設定・`false`・誤記の場合、ジョブは **skipped（未有効化）** です。成功と解釈しないでください。有効化変数が `true` でSecretが未設定の場合は **failed** として検出します。

## 成功判定と失敗検出

成功には、HTTP 200、JSONのContent-Type、JSON値が整数の `1` であることのすべてが必要です。`true`、`1.0`、`"1"`、空配列、HTML、空のHTTP 204などは成功にしません。レスポンスは最大1,024バイトまでに制限します。

1回の通信には15秒のタイムアウトを設け、通信失敗またはHTTP 408・429・500・502・503・504だけ、5秒後・15秒後に再試行します。最大3回です。タイムアウトはソケットの待機単位なので、ジョブ全体の上限は別途 **3分** に設定しています。同時実行は抑止します。

| 表示する分類 | 意味と次の確認 |
| --- | --- |
| `DB_READ_SUCCEEDED / EXPECTED_INTEGER_1` | 今回の読み取り成功。停止回避効果やPWA全体の成功とは別 |
| `MISSING_PUBLISHABLE_KEY` / `INVALID_PUBLISHABLE_KEY` | Secretの設定・対象プロジェクト・キー種別を確認。通信前に失敗 |
| `AUTHENTICATION_FAILED` / `PERMISSION_DENIED` | キーの失効、匿名ロールの関数実行権限などを確認。管理者キーに置き換えない |
| `RPC_NOT_FOUND` / `RPC_NOT_READ_ONLY` | 関数の存在・引数・STABLE指定・API公開を管理者が読み取りで確認 |
| `PROJECT_PAUSED` | HTTP 540。停止済みのため所有者がDashboardで状態を確認。自動再開しない |
| `REDIRECT_REFUSED` / `TLS_FAILED` | 送信先・証明書・ネットワークを確認。検証を無効にしない |
| `HTTP_408` / `HTTP_429` / 対象の `HTTP_5xx` / `NETWORK_FAILED` | 最大3回でも解消しなければ失敗。サービス状況と通信を確認 |
| `INVALID_JSON` / `UNEXPECTED_CONTENT_TYPE` / `UNEXPECTED_RPC_RESULT` / `RESPONSE_TOO_LARGE` | 応答が契約と異なるため失敗。本文を公開ログへ貼り付けず調査 |
| `UNEXPECTED_CLIENT_ERROR` / `SUMMARY_WRITE_FAILED` | 実行環境・クライアントまたは結果記録のエラー。成功にしない |

処理が検出した最終失敗は終了コード1、Actionsのエラー表示、実行サマリーに残ります。中間の再試行も警告として記録します。Secretと実データを含まない確認時刻・試行回数を記録します。ジョブ全体のタイムアウト・強制キャンセル・runner障害では、Pythonのサマリーが残らない場合があります。その場合はGitHub側の実行状態とログで判断します。

GitHubアカウントの **Settings → Notifications → Actions** で通知を有効にし、必要に応じて失敗時のみ通知を選びます。定期実行の通知先は、作成者・cron変更者・再有効化した人に応じて変わるため、本人が受信できるか確認が必要です。このPRでは通知設定を変更せず、メールや外部Webhookを送るコードも追加しません。[公式の通知仕様](https://docs.github.com/en/actions/concepts/workflows-and-actions/notifications-for-workflow-runs)を参照してください。

## 自動実行そのものが止まる場合

[GitHubの公式仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)では、高負荷による遅延・欠落があり、public repositoryは60日間リポジトリ活動がないと定期ワークフローが無効化されます。Actionsが実行され続ければこの制限を無条件で免れる、とは扱いません。

**起動しなかったジョブは、そのジョブ自身では失敗通知できません。** 本ワークフローには外部からの未起動監視は含みません。運用ではActionsの直近の成功時刻を確認し、例えば24時間成功がない場合は次を調べます。24時間は運用上の提案値で、Supabaseの閾値ではありません。

- 有効化変数が `true` か、ジョブがskippedになっていないか。
- ワークフロー・リポジトリのActionsが無効化されていないか。
- 60日無活動による停止、default branch・実行元アカウントの変更、GitHubの障害がないか。
- 直近の失敗分類、Supabaseのプロジェクト状態、所有者に停止予告メールが届いていないか。

自動コミットでリポジトリ活動を作る処理は入れていません。未起動を自動通知する独立した監視が必要なら、別途対象・通知先・承認を決めて追加します。Pro以上の有料プランでは非活動による停止の対象外であることは公式に説明されていますが、このPRでは契約を変更しません。

## 効果の確認と中止

次の結果を混同せず記録してください。

1. ローカルテスト成功: 模擬応答に対する安全な処理と失敗検出を確認。
2. 実DBへの初回GET成功: その時点で公開APIから既存RPCが実行できたことを確認。
3. GitHubでの初回手動実行・定期実行成功: GitHubのSecret・スケジュールを含む経路を確認。
4. 継続観測: 少なくとも7日間の実行履歴、失敗・欠落、Supabaseの警告・停止状況を確認。週21回は予定回数であり、実績は履歴から数える。

7日間停止しなかったとしても、PWAの実利用や管理者のDashboard操作などが活動として加算され得るため、このワークフロー単独の効果を証明したことにはなりません。将来の停止回避も保証しません。

中止する場合は `SUPABASE_KEEPALIVE_ENABLED=false` にします。実行中のジョブはGitHub側で別途キャンセルします。必要ならワークフローを無効化し、Actions Secretを削除します。既存のPWAが使うキー自体を失効させるとPWAに影響するため、停止操作とキーの失効を混同しないでください。

## ローカルでの再検証

リポジトリルートで次を実行します。標準ライブラリのみを使い、実際のSupabaseやGitHubへ通信しません。

```sh
python3 .github/tests/test_supabase_keepalive.py
```

テストはworkflow内の実際のPythonブロックを読み込んで検証します。YAMLとGitHubの式は、利用できる場合に `actionlint .github/workflows/supabase-keepalive.yml` で別途確認します。これらはGitHub上での実行成功や、非活動停止対策の効果を証明するものではありません。

### PR準備時の検証結果（2026年11月23日）

| 検証 | 観測結果 |
| --- | --- |
| 模擬通信による20テスト | すべて成功。正常・異常JSON、HTTP失敗、再試行、TLS、リダイレクト、秘密情報の非出力などを確認 |
| actionlint 1.7.12 | エラーなし。配布バイナリのSHA-256を公式リリースのdigestと照合して使用 |
| 実DBへの読み取り専用GET | 日本時間21:08:30に1回実施し、HTTP 200・JSON整数1を確認。作成したworkflow内のPythonをローカルで実行 |
| 既存PWAへの変更 | 基点mainの既存19ファイルすべてとバイト単位で一致 |
| GitHub上でのコミット・PR・設定・実行 | 承認前のため未実施 |
| 定期実行の継続・停止回避効果 | 未検証 |

GitHubでの実行ではないため、Actions Secretの登録や定期実行経路が正常であることまでは確認できていません。実DBの権限確認も列権限まで含む読み取りだけで行い、DBのデータ・定義・権限は変更していません。
