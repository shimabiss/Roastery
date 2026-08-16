# Roastery — コーヒー豆EC サンプルシステム

個人の技術学習用に作っているサンプルシステムです。コーヒー豆の EC を題材に、
**1つのシステムをテーマごとに拡張していく**形で、設計から実装・テスト・監視までを
実際に動くコードで扱います。

アプリはローカルの Docker で動かし、必要に応じて Azure のマネージドサービスに接続します。

---

## 1. このリポジトリの位置づけ

扱うテーマはアプリからインフラまで縦に広く、工程も設計・実装・テスト・監視と横に広いため、
テーマごとに別のサンプルを用意するのではなく、**Roastery というひとつのシステムを育てていく**
形式を取っています。積み上げるほど、前のテーマの実装が次のテーマの前提として効いてきます。

| テーマ | Roastery での扱い | 状態 |
|---|---|---|
| OpenTelemetry | 4サービスの計装 / Collector / Application Insights | **実装済み ← 現在ここ** |
| CI/CD | GitHub Actions で lint / test / build → ACA へのデプロイまで段階的に育てる | 未着手 |
| Kubernetes | compose 定義を Helm / Kustomize 化し、AKS に載せる | 未着手 |
| 要件定義・上流工程 | 架空の EC として機能要件を定義し、実装とのギャップを洗い出す | **初版あり** |
| 設計 (デザインパターン) | 同期3ステップの注文処理を Saga / Outbox / Circuit Breaker に作り替える | 未着手 |
| ネットワーク | サービス間通信、名前解決、L4 / L7 の切り分け | 未着手 |
| ドメイン・DNS・証明書 | Ingress と TLS 終端、証明書の自動更新 | 未着手 |
| Go 言語 | payment-api を同じ API 契約のまま Go に置き換える | 未着手 |
| Rust 言語 | 同上、または高スループットな新規サービスを追加する | 未着手 |
| Python | order / inventory / payment が Python。テスト・型・パッケージング | 未着手 |

> 「Roastery での扱い」の列は現時点の案です。着手時に変わります。

システムとして「何を作るか」は [docs/requirements/](docs/requirements/) に、
構成・ブランチ戦略・デプロイ戦略の決定と、その際に何を天秤にかけたかは
[docs/adr/](docs/adr/) に残してあります。
公開リポジトリとして運用するうえでの確認事項は
[docs/security-checklist.md](docs/security-checklist.md) にまとめています。

## 2. システム構成

```
                     ┌──────────────┐
  ブラウザ ──────────▶│   frontend   │ Node.js 22 / Express
                     │   :3000      │
                     └──────┬───────┘
                            │ HTTP (traceparent 伝播)
                     ┌──────▼───────┐
                     │  order-api   │ Python 3.12 / FastAPI
                     │   :8001      │
                     └──┬────────┬──┘
                 ┌──────▼──┐  ┌──▼───────────┐        ┌────────────┐
                 │inventory│  │ payment-api  │        │ PostgreSQL │
                 │  -api   │  │   :8003      │        │  (注文)    │
                 │  :8002  │  │ ★障害注入    │        └────────────┘
                 └────┬────┘  └──────────────┘
                 ┌────▼────┐
                 │  Redis  │ (在庫)
                 └─────────┘

  全サービス ── OTLP/gRPC ──▶ OpenTelemetry Collector ─┬─▶ Application Insights
                                                       ├─▶ Jaeger  (localhost:16686)
                                                       └─▶ Prometheus (localhost:9090)
```

**言語を2つ (Node.js / Python) 混ぜてあるのは意図的**です。「ベンダー中立・言語中立」という
OpenTelemetry の主張が、waterfall が1本に繋がる形で目に見えます。
以降 Go / Rust のサービスを足しても、この構造はそのまま使えます。

frontend の画面はパスで2つに分かれています。

| パス | 内容 | 実装 | 配信元 |
|---|---|---|---|
| `/` | 公開サイト。架空のコーヒー豆 EC | **Vue 3 + TypeScript + Vite** | `client/` をビルドした `dist/` |
| `/ops` | 運用画面。障害注入・動作確認 | 素の HTML / CSS / JS | `server/ops.html` |

**公開サイトだけ Vue にしています。** 運用画面は非公開かつ状態をほとんど持たないため、
フレームワークを入れる利点がありません。加えて、SPA のルーティングに `/ops` を載せると
**1つのバンドルに同居してサーバー側でパスを分けられなくなる**ため、意図的に分けています。
将来 ingress やリバースプロキシで**パス単位のアクセス制御**を掛けられる状態を保つためです。
**現時点ではパスが違うだけで認証は掛かっていません**（[docs/security-checklist.md](docs/security-checklist.md)）。

フロントの開発時は Vite の開発サーバーを使えます（`/api` は Express に転送されます）。

```bash
cd apps/frontend
npm install
npm run dev        # http://localhost:5173
npm run typecheck  # vue-tsc による型チェック
```

## 3. ローカルで動かす（Azure 不要）

### 前提環境

**Windows 11 + WSL2 (Ubuntu) + Docker** を前提にしています。
初回セットアップは [docs/wsl2-setup.md](docs/wsl2-setup.md) を参照してください。

- 必要なのは **Docker と Compose v2 だけ**（`docker compose version` が `v2` 以上）
- Python / Node.js / PostgreSQL / Redis をホストに入れる必要はありません。すべてコンテナ内です
- コマンドは **WSL2 の Ubuntu ターミナル**で実行します（PowerShell では動きません）
- リポジトリは **`~/` 配下（Linux ファイルシステム）** に置いてください。
  `/mnt/c/...` に置くとビルドが極端に遅くなります
- 画面は **Windows 側のブラウザ**から `http://localhost:3000` で見られます
  （WSL2 が localhost を自動転送します）
- 使用リソースの目安: イメージ約 2.5GB / メモリ約 2〜3GB（コンテナ 9 個）

> macOS / Linux でもそのまま動きます（スクリプトは両対応にしてあります）。

```bash
./scripts/local-start.sh
```

`.env` の作成 → Collector をローカル送信に切り替え → ビルド → 起動 →
**動作確認までを一気に**やります。初回は 3〜5 分かかります。

最後に走る `./scripts/verify.sh` が以下を自動でチェックします。

1. 全サービスと Collector / Jaeger / Prometheus が応答するか
2. 注文が通るか（DB・Redis・サービス間通信）
3. 在庫不足が 409 になるか
4. 遅延注入・エラー注入が効くか
5. **4サービスすべてのトレースが Jaeger に届いているか**（計装と Collector が繋がっている証拠）
6. カスタムメトリクスが Collector から公開されているか

`すべて正常です` と出れば準備完了です。あとは以下を開いてください。

| URL | 内容 |
|---|---|
| http://localhost:3000 | 公開サイト（架空のコーヒー豆EC） |
| http://localhost:3000/ops | 運用画面（障害注入 / 動作確認） |
| http://localhost:16686 | Jaeger — トレースの waterfall |
| http://localhost:9090 | Prometheus — メトリクス |
| http://localhost:55679/debug/tracez | Collector の中身 |

```bash
./scripts/load.sh                # 裏で注文を流し続ける (Ctrl-C で停止)
./scripts/chaos.sh slow 1500     # 遅延を注入して Jaeger で waterfall を見る
./scripts/chaos.sh clear
docker compose down -v           # 片付け
```

### 画面モックは削除しました（2026-08-16）

以前 `docs/mock/` に、バックエンド無しで動く HTML のモックを2つ置いていました。
**実装が追い越したため削除しています。**

モックを作った目的は「実装前に見せ方を決める」ことでした。P1〜P5 で
ログイン・注文手続き・出荷処理・焙煎についてのセクションまで実装した結果、
モック側には無い画面のほうが多くなり、**更新しない限り必ず誤った情報を見せる**
状態になりました。

> これは [06-format-comparison.md](docs/requirements/06-format-comparison.md) の
> 5節に書いたことと同じです。
>
> > 2形式の並行維持は、通常の開発では推奨しません。二重管理になり、**片方が必ず腐ります。**
>
> ユースケース記述とユーザーストーリーについて書いた話が、
> **モックと実装の間でそのまま起きた**形です。

画面を見たいときは実物を起動してください。`./scripts/local-start.sh` で
`http://localhost:3000`（公開サイト）と `http://localhost:3000/ops`（運用画面）が開きます。

## 4. OpenTelemetry で扱っている範囲

| テーマ | どこで見えるか |
|---|---|
| 分散トレース (サービスを跨ぐ1本の trace) | App Insights のトランザクション検索 / Jaeger |
| W3C Trace Context による伝播 (言語をまたいでも1本) | Node.js → Python の waterfall が繋がる |
| 自動計装 (ゼロコード) で取れる範囲 | HTTP / PostgreSQL / Redis の span が勝手に出る |
| 手動計装 (span / attribute / event / exception) | `checkout` `authorize` などの業務単位の span |
| カスタムメトリクス | `orders.created` `payment.authorize.duration` |
| ログとトレースの相互参照 | ログに `trace_id` が入り、trace から辿れる |
| Collector の役割 (中継・加工・多重送信) | 同じデータが App Insights と Jaeger の両方に届く |
| 障害の切り分け | 遅延・エラーを注入して waterfall から原因を特定 |

App Insights で使う KQL は [docs/kql-cheatsheet.md](docs/kql-cheatsheet.md) にまとめてあります。

障害注入は以下で操作します。

```bash
./scripts/chaos.sh slow 1500     # payment-api に 1.5 秒の遅延を注入
./scripts/chaos.sh error 0.3     # payment-api を 30% の確率で失敗させる
./scripts/chaos.sh inv-slow 800  # inventory-api の在庫参照を遅くする
./scripts/chaos.sh clear         # 解除 + 在庫リセット
./scripts/chaos.sh status        # 現在の設定
```

> `slow` と `error` は排他です (一方を設定すると他方は 0 に戻ります)。
> 両方同時に効かせたい場合は画面の Chaos パネルを使ってください。

代表的な確認の流れ:

- **遅延の切り分け** — `chaos.sh slow 1500` で注文し、waterfall の `authorize-payment` の
  帯だけが太ることを見る。span 属性に `chaos.latency_ms=1500` が載っている
- **エラーの伝播** — `chaos.sh error 1.0` で 502 を発生させ、ERROR が payment-api から
  order-api、frontend まで伝わることを見る
- **業務上の拒否** — 在庫5個の `DRIP-KETTLE` を6個以上注文し、`inventory.insufficient` の
  span event と `orders.rejected{reason="out_of_stock"}` が記録されることを見る。
  システム障害と区別できる設計になっている

## 5. Azure（Application Insights）に繋ぐ

ローカルで動いたら、送信先を追加します。**アプリ側の変更はありません。**

### 5-1. Azure リソースを作る

```bash
az login
./scripts/azure-setup.sh                    # 既定: dev / japaneast
# 作られるリソース: rg-roastery-dev-je-001 / log-... / appi-...
# 環境とリージョンを変える場合: ./scripts/azure-setup.sh stg japanwest
```

Log Analytics ワークスペースと Application Insights を作成し、接続文字列を `.env` に書き込みます。
1 日あたりの取り込み上限を 1GB に設定してあるので、想定外の課金にはなりません。

このスクリプトの実体は `infra/terraform` に対する `terraform apply` です。
Terraform を直接叩く場合は以下になります。

```bash
export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)
cd infra/terraform
terraform init
terraform plan
terraform apply
terraform output -raw connection_string
```

出力された接続文字列を `.env` の `APPLICATIONINSIGHTS_CONNECTION_STRING` に設定します
（スクリプト経由なら自動で書き込まれます）。詳細は
[infra/terraform/README.md](infra/terraform/README.md) を参照してください。

### 5-2. Collector の送信先を切り替えて再起動する

```bash
sed -i 's|^COLLECTOR_CONFIG=.*|COLLECTOR_CONFIG=config.yaml|' .env
docker compose up -d otel-collector     # Collector だけ入れ替え。アプリは再起動しない
./scripts/load.sh 1 30                  # 30 件ほど流す
```

`config.yaml` は Application Insights と Jaeger と Prometheus の **3つに同時送信**します。
Jaeger 側でも引き続き見えるので、両方を並べて比較できます。

**アプリのコードも設定も変えずに Collector の設定ファイルだけを差し替えている**という点が、
Collector を挟む価値そのものです。

### 5-3. Azure Portal で確認する

作成した Application Insights を開き、

- **アプリケーション マップ** — 4つのサービスと依存関係が図になる（初回表示まで 3〜5 分）
- **トランザクション検索** — 個別のリクエストの waterfall
- **ログ** — KQL でクエリ（[docs/kql-cheatsheet.md](docs/kql-cheatsheet.md)）

データが出るまで 1〜3 分のラグがあります（Collector のバッチ + Azure 側の取り込み）。

### 5-4. 片付け

```bash
docker compose down -v
terraform -chdir=infra/terraform destroy
```

## 6. ディレクトリ

```
.
├── docker-compose.yml          # 全部入り。OTEL_* 環境変数の見本でもある
├── .env.example                # 接続文字列などの設定
├── apps/                       # アプリケーション
│   ├── frontend/               # Node.js 22
│   │   ├── client/             #   公開サイト: Vue 3 + TypeScript + Vite
│   │   └── server/             #   Express (BFF) + 運用画面の素の HTML
│   ├── order-api/              # Python 3.12 / FastAPI  ← 手動計装の見本
│   ├── inventory-api/          # Python 3.12 / FastAPI + Redis
│   └── payment-api/            # Python 3.12 / FastAPI  ← 障害注入の主役
├── platform/                   # アプリ以外の実行時設定
│   ├── otel-collector/
│   │   ├── config.yaml         # Azure Monitor + Jaeger + Prometheus に多重送信
│   │   └── config.local.yaml   # ローカルのみ
│   ├── prometheus/prometheus.yml
│   └── postgres/init.sql
├── infra/                      # IaC
│   ├── terraform/              # Log Analytics + Application Insights（azurerm）
│   └── k8s/                    # Kubernetes のテーマで追加
├── scripts/
│   ├── check-docker.sh         # Docker 環境の事前チェック（いちばん最初）
│   ├── local-start.sh          # ローカルだけで起動 + 動作確認
│   ├── verify.sh               # 起動後の自動チェック
│   ├── azure-setup.sh          # terraform apply + .env 更新
│   ├── load.sh                 # 負荷生成
│   └── chaos.sh                # 障害注入
└── docs/
    ├── requirements/           # 機能要件・ユースケース・ドメインモデル
    ├── adr/                    # アーキテクチャ意思決定記録
    ├── security-checklist.md   # 公開リポジトリ / 公開エンドポイントの確認事項
    ├── wsl2-setup.md           # Windows 11 + WSL2 の初回セットアップ
    └── kql-cheatsheet.md       # App Insights の KQL
```

## 7. 実装上のポイント (コードを読むときの手がかり)

### 計装は環境変数で設定する

アプリのコードに「どこに送るか」は一切書いていません。`docker-compose.yml` の
`OTEL_EXPORTER_OTLP_ENDPOINT` などがすべてです。設定と実装が分離されているので、
送り先を変えるのに再ビルドが要りません。

### 自動計装はまず全部入れて、ノイズだけ個別に絞る

- Python は `opentelemetry-instrument` でランチャー起動 (Dockerfile の CMD)
- Node.js は `--require @opentelemetry/auto-instrumentations-node/register`

ただし既定のままだと不要な span が大量に出ます。以下を絞っています。

| 何を絞ったか | どこで |
|---|---|
| ASGI の `http send` / `http receive` span | 各 Python サービスで `FastAPIInstrumentor.instrument_app(..., exclude_spans=[...])` |
| Express/router のミドルウェア span | `OTEL_NODE_DISABLED_INSTRUMENTATIONS=express,router,connect,fs,dns,net` |
| ヘルスチェックの trace | `OTEL_PYTHON_EXCLUDED_URLS=healthz` |

### メトリクスは delta temporality にする

`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=DELTA` を設定しています。
Application Insights のメトリクス体験は delta を前提としており、
cumulative のままだとグラフが期待どおりになりません。

### Collector で運用ポリシーを一元化する

`platform/otel-collector/config.yaml` では以下をアプリの外で行っています。

- `resource` プロセッサで `deployment.environment` と `service.namespace` を全テレメトリに付与
- `attributes/redact` プロセッサで `enduser.id` をハッシュ化 (PII マスクの例)
- 3つのバックエンドへ同時送信

「アプリを再デプロイせずにポリシーを変えられる」のが Collector を挟む最大の理由です。

## 8. トラブルシューティング

まず `./scripts/verify.sh` を実行してください。どこで失敗しているかが切り分けられます。

| 症状 | 確認すること |
|---|---|
| Collector が起動しない | `docker compose logs otel-collector`。設定ファイルの構文エラーか、`COLLECTOR_VERSION` が古すぎて未対応の設定項目がある可能性。`.env` で `COLLECTOR_VERSION` を上げるか `latest` にする |
| Jaeger にサービスが出ない | Collector が受信できていない。`docker compose logs otel-collector` に `TracesExporter` のログが出ているか。`docker compose logs order-api` に OTLP の接続エラーが出ていないか |
| Azure にデータが出ない | `docker compose logs otel-collector` に認証エラーが出ていないか。`.env` の接続文字列が空でないか。`COLLECTOR_CONFIG=config.yaml` になっているか |
| データが出るまで遅い | 正常です。Collector のバッチ (5秒) + Azure 側の取り込み (1〜3分) |
| Prometheus からカスタムメトリクスが消えた | 正常です。`orders.created` などは注文が発生したときしかデータ点が出ず、delta temporality のため止まると送信自体がなくなります。Collector の `metric_expiration` (既定 5分) を過ぎるとエンドポイントから消えます。`./scripts/load.sh` で流し続けるか、注文を1件入れて 15 秒待てば再び現れます |
| Jaeger には出るが Azure には出ない | 接続文字列の問題。`COLLECTOR_CONFIG=config.yaml` になっているか |
| ビルドが遅い | 初回のみ |
| ポートが衝突する | 3000 / 5432 / 6379 / 9090 / 16686 を使用。`docker-compose.yml` の `ports` を変更 |

## 9. 動作確認済みの環境

- 検証したバージョン: OpenTelemetry Python `1.44.0` / instrumentation `0.65b0`、
  Node.js SDK `0.221.0` / auto-instrumentations `0.79.0`
- 4サービスを跨ぐ 18 span の trace、カスタムメトリクス、trace_id 付きログの出力を確認済み
- Collector のイメージタグは `.env` の `COLLECTOR_VERSION` で固定できます。
  再現性が必要な場面では `latest` ではなく具体的なバージョンに固定してください

## 10. ライセンス

**コード・ドキュメントは [MIT License](LICENSE)** です。自由に読んで、真似して、
持ち帰ってください。

**ただし MIT が及ぶのはこのリポジトリで書いたものだけです。**
同梱している写真は撮影者に権利があり、別のライセンスに従います。

| 対象 | ライセンス | 備考 |
|---|---|---|
| `apps/` `infra/` `platform/` `scripts/` `tests/` のコード | MIT | |
| `docs/` の文書 | MIT | 要件定義・ADR を含む |
| `apps/frontend/client/assets/` の画像 | [Unsplash License](https://unsplash.com/license) | **MIT ではありません。**出典は [CREDITS.md](apps/frontend/client/assets/CREDITS.md) |

画像を再利用する場合は Unsplash License の条件（**他のストック写真・壁紙サイトへの
再配布は不可**）を直接確認してください。このリポジトリを fork してサイトの素材として
使う分には範囲内ですが、画像だけ抜き出して別途配布するのは想定されていません。

依存ライブラリはそれぞれのライセンスに従います
（`apps/*/requirements.txt`・`apps/frontend/package.json`）。

> このリポジトリは業務ではなく個人の学習用に作っています。
