# Roastery — コーヒー豆EC サンプルシステム

個人の技術学習用に作っているサンプルシステムです。コーヒー豆の EC を題材に、
**1つのシステムをテーマごとに拡張していく**形で、設計から実装・テスト・監視までを
実際に動くコードで扱います。

アプリはローカルの Docker で動かし、必要に応じて Azure のマネージドサービス
（Application Insights）に接続します。アプリごと Azure Container Apps に載せる経路も
GitHub Actions + Terraform で用意してあります。

---

## 1. このリポジトリの位置づけ

扱うテーマはアプリからインフラまで縦に広く、工程も設計・実装・テスト・監視と横に広いため、
テーマごとに別のサンプルを用意するのではなく、**Roastery というひとつのシステムを育てていく**
形式を取っています。積み上げるほど、前のテーマの実装が次のテーマの前提として効いてきます。

| テーマ | Roastery での扱い | 状態 |
|---|---|---|
| 要件定義・上流工程 | 架空の EC として機能要件を定義し、実装スコープ P1〜P5 を確定させて実装した | **実装済み**（[07-implementation-scope.md](docs/requirements/07-implementation-scope.md)） |
| OpenTelemetry | 6サービスの計装 / Collector / Application Insights | **実装済み** |
| 設計 (デザインパターン) | 注文は Saga (補償トランザクション)、出荷は前進復旧、決済は冪等キー。状態遷移は表で持つ | **一部実装済み**（`order-api/app/saga.py` `states.py`。Outbox / Circuit Breaker は未着手） |
| IaC | Terraform で Log Analytics + Application Insights + Container Apps 一式を作る | **実装済み**（[infra/terraform/](infra/terraform/)） |
| CI/CD | GitHub Actions で test / typecheck / build / fmt / 統合テスト、ACA へのデプロイと破棄 | **実装済み**（[.github/workflows/](.github/workflows/)。deploy と destroy は手動実行のみ） |
| Kubernetes | compose 定義を Helm / Kustomize 化し、AKS に載せる | 未着手（[infra/k8s/](infra/k8s/) は空） |
| ネットワーク | サービス間通信、名前解決、L4 / L7 の切り分け | 未着手 |
| ドメイン・DNS・証明書 | Ingress と TLS 終端、証明書の自動更新 | 未着手 |
| Go 言語 | payment-api を同じ API 契約のまま Go に置き換える | 未着手 |
| Rust 言語 | 同上、または高スループットな新規サービスを追加する | 未着手 |
| Python | order / inventory / payment / member / external-stub の5サービスが Python。テスト・型・パッケージング | 未着手 |

> 「Roastery での扱い」の列は現時点の案です。着手時に変わります。

システムとして「何を作るか」は [docs/requirements/](docs/requirements/) に、
構成・ブランチ戦略・デプロイ戦略の決定と、その際に何を天秤にかけたかは
[docs/adr/](docs/adr/) に残してあります。
公開リポジトリとして運用するうえでの確認事項は
[docs/security-checklist.md](docs/security-checklist.md) にまとめています。

## 2. システム構成

```
  ブラウザ
     │
     ▼
  ┌──────────────┐  Node.js 22 / Express (BFF) + Vue 3
  │   frontend   │  :3000
  └──┬────────┬──┘
     │        │  HTTP (traceparent 伝播)
     ▼        ▼
┌──────────┐ ┌──────────────┐  Python 3.12 / FastAPI
│member-api│ │  order-api   │  注文 / 出荷 / キャンセル
│  :8005   │ │   :8001      │
└──────────┘ └──┬────────┬──┘
 会員・セッション │        │
 住所帳・カート   ▼        ▼
        ┌──────────────┐ ┌──────────────┐
        │inventory-api │ │ payment-api  │ 冪等キー / 決済状態の永続化
        │    :8002     │ │   :8003      │
        └──────────────┘ └──────┬───────┘
                                ▼
                        ┌───────────────┐
                        │ external-stub │ 決済代行 / メール配信 / 配送業者の模擬
                        │    :8004      │ ★障害注入はここ
                        └───────────────┘

  データストア  PostgreSQL :5432 … 会員・住所・セッション・注文・決済・冪等キー
                Redis      :6379 … 実在庫 / 引当済数・カート・価格と公開状態の上書き

  ※ order-api / member-api も external-stub を直接呼びます (確認メール / 発送通知)。
     frontend も /api/mail/inbox と障害注入で external-stub を直接呼びます。

  全6サービス ── OTLP/gRPC ──▶ OpenTelemetry Collector ─┬─▶ Application Insights
                                                        ├─▶ Jaeger  (localhost:16686)
                                                        └─▶ Prometheus (localhost:9090)
```

**言語を2つ (Node.js / Python) 混ぜてあるのは意図的**です。「ベンダー中立・言語中立」という
OpenTelemetry の主張が、waterfall が1本に繋がる形で目に見えます。
以降 Go / Rust のサービスを足しても、この構造はそのまま使えます。

外部サービス (決済代行 / メール配信 / 配送業者) は `external-stub` 1つにまとめてあります。
スタブは学習対象ではないため分けても IaC とデプロイのコストが増えるだけ、という判断です
（[07-implementation-scope.md](docs/requirements/07-implementation-scope.md) 4節）。
**障害注入も external-stub 側にあります。** payment-api の `/admin/chaos` は
external-stub への中継で、`scripts/chaos.sh` がこちらを叩きます
（運用画面 `/ops` は frontend 経由で external-stub を直接叩きます）。

frontend の画面はパスで分かれています。

| パス | 内容 | 実装 | 配信元 |
|---|---|---|---|
| `/` | 公開サイト。架空のコーヒー豆 EC | **Vue 3 + TypeScript + Vite** | `client/` をビルドした `dist/` |
| `/ops` | 運用画面。障害注入・動作確認 | 素の HTML / CSS / JS | `server/ops.html` |
| `/verify` | 確認メールのリンク先。公開サイトの SPA に流し込むだけ | 同上（`/` と同じバンドル） | `dist/index.html` |

**公開サイトだけ Vue にしています。** 運用画面は非公開かつ状態をほとんど持たないため、
フレームワークを入れる利点がありません。加えて、SPA のルーティングに `/ops` を載せると
**1つのバンドルに同居してサーバー側でパスを分けられなくなる**ため、意図的に分けています。
将来 ingress やリバースプロキシで**パス単位のアクセス制御**を掛けられる状態を保つためです。
**現時点ではパスが違うだけで認証は掛かっていません**（[docs/security-checklist.md](docs/security-checklist.md)）。

フロントの開発時は Vite の開発サーバーを使えます（`/api` と `/ops` は Express に転送されます）。

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
- 起動するコンテナは **11 個**（アプリ6 / データストア2 / 可観測性3）。
  Grafana は `docker compose --profile grafana up -d` を付けたときだけ起動します

> macOS / Linux でもそのまま動きます（スクリプトは両対応にしてあります）。

```bash
./scripts/local-start.sh
```

`.env` の作成 → Collector をローカル送信に切り替え → ビルド → 起動 →
**動作確認までを一気に**やります。初回は 3〜5 分かかります。

最後に走る `./scripts/verify.sh` が以下を自動でチェックします。
**要件（FR / BR / UC）の番号がそのままチェック項目になっている**のが要点です。

1. 6サービスと Collector / Jaeger / Prometheus が応答するか
2. 会員登録・メール確認・ログイン。未確認では購入できないこと（FR-101/102/114 / BR-19）
3. 住所帳と、配送先で変わる送料・閾値以上の送料無料（FR-106 / FR-404 / UC-01 A2）
4. 未ログインで入れたカートがログイン後もマージされて残ること（FR-306 / BR-20）
5. 複数明細が1つの注文になること、注文確認メールが届くこと（FR-408 / FR-1001）
6. 出荷の状態遷移とキャンセルの禁則、重複遷移の排他（UC-06 / BR-05/06/26/31）
7. 売上確定に失敗しても「受付済」に戻さず、リトライで前進復旧できること（UC-06 E1 / BR-28）
8. キャンセル・決済失敗・決済代行の無応答で、引当が必ず解放されること（UC-02 / FR-603 / UC-01 E3）
9. 在庫不足が 409 になり、**他の明細も引き当てられない**こと（BR-03）
10. 遅延注入が効くこと
11. 商品の公開・非公開、入荷による実在庫の増加、注文履歴（FR-1102 / FR-605 / FR-406）
12. **6サービスすべてのトレースが Jaeger に届いているか**（計装と Collector が繋がっている証拠）
13. カスタムメトリクスが Collector から公開されているか

`すべて正常です` と出れば準備完了です。あとは以下を開いてください。

| URL | 内容 |
|---|---|
| http://localhost:3000 | 公開サイト（架空のコーヒー豆EC） |
| http://localhost:3000/ops | 運用画面（障害注入 / 動作確認） |
| http://localhost:16686 | Jaeger — トレースの waterfall |
| http://localhost:9090 | Prometheus — メトリクス |
| http://localhost:8889/metrics | Collector が公開するアプリのメトリクス（Prometheus 形式） |
| http://localhost:55679/debug/tracez | Collector の中身 |

```bash
./scripts/load.sh                # 裏で注文を流し続ける (Ctrl-C で停止)
./scripts/chaos.sh slow 1500     # 遅延を注入して Jaeger で waterfall を見る
./scripts/chaos.sh clear
docker compose down -v           # 片付け
```

> `load.sh` は起動時に **負荷生成用の会員を1つ作り、メール確認とログインまで済ませて**
> から注文を流します。P2 で会員必須にしたため、`/api/checkout` には
> ログイン済み・メール確認済みのセッションとサーバー側カートが要るからです。
> 会員を使い回したい場合は `LOAD_EMAIL` を固定してください。
> 在庫が尽きたら自動で在庫をリセットして続行します（`AUTO_RESTOCK=0` で無効化）。

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
| 分散トレース (6サービスを跨ぐ1本の trace) | App Insights のトランザクション検索 / Jaeger |
| W3C Trace Context による伝播 (言語をまたいでも1本) | Node.js → Python の waterfall が繋がる |
| 自動計装 (ゼロコード) で取れる範囲 | HTTP / PostgreSQL / Redis の span が勝手に出る |
| 手動計装 (span / attribute / event / exception) | `checkout` `reserve-inventory` `authorize-payment` `ship-order` `cancel-order` などの業務単位の span |
| カスタムメトリクス | `orders.created` `orders.rejected` `orders.compensated` `payment.authorize.duration` `inventory.releases` `carts.merges` など |
| ログとトレースの相互参照 | ログに `trace_id` が入り、trace から辿れる |
| Collector の役割 (中継・加工・多重送信) | 同じデータが App Insights と Jaeger の両方に届く |
| 障害の切り分け | 遅延・エラーを注入して waterfall から原因を特定 |

App Insights で使う KQL は [docs/kql-cheatsheet.md](docs/kql-cheatsheet.md) にまとめてあります。

障害注入は **external-stub（決済代行の模擬）** と inventory-api に対して行います。
`chaos.sh` は payment-api 経由で external-stub の `/admin/chaos` を叩きます。

```bash
./scripts/chaos.sh slow 1500     # 決済代行の応答に 1.5 秒の遅延を注入
./scripts/chaos.sh error 0.3     # 決済代行を 30% の確率で失敗させる
./scripts/chaos.sh inv-slow 800  # inventory-api の在庫参照を遅くする
./scripts/chaos.sh clear         # 解除 + 在庫リセット
./scripts/chaos.sh status        # 現在の設定
```

> `slow` と `error` は排他です。external-stub の `/admin/chaos` は
> **指定しなかったパラメータを 0 に戻す**ため、片方だけを送ると他方が解除されます。
> 画面の Chaos パネルも同じ挙動です。両方を同時に効かせたい場合は
> 1リクエストで両方を渡してください。
>
> ```bash
> curl -X POST 'http://localhost:8004/admin/chaos?latency_ms=1500&error_rate=0.3'
> ```

**無応答**（応答を返さずに呼び出し側をタイムアウトさせる）も注入できます。
`chaos.sh` には無いので直接叩きます。エラー応答と無応答は別の異常系で、
UC-01 E3 と冪等キー（BR-28）が必要になる理由そのものです。

```bash
curl -X POST 'http://localhost:8004/admin/chaos?no_response_rate=1.0&no_response_hold_s=30'
curl -X POST 'http://localhost:8004/admin/chaos'   # 解除
```

代表的な確認の流れ:

- **遅延の切り分け** — `chaos.sh slow 1500` で注文し、waterfall の `authorize-payment`
  配下（external-stub の span）だけが太ることを見る。external-stub の span 属性に
  `chaos.latency_ms=1500` が載り、`chaos.latency_injected` の span event が出る
- **エラーの伝播** — `chaos.sh error 1.0` で注文すると、external-stub が 500、
  payment-api が 502、order-api も 502 を返す。ERROR が external-stub から
  payment-api、order-api、frontend まで伝わることを見る
- **業務上の拒否** — 在庫5個の `DRIP-KETTLE` を6個以上注文し、`inventory.insufficient` の
  span event と `orders.rejected{reason="reserve-inventory"}` が記録されることを見る。
  システム障害と区別できる設計になっている
- **補償が走ったこと** — 決済を失敗させると `saga.failed` の span event と
  `orders.compensated` が記録される。**補償自体が失敗した場合は
  `orders.compensation_failures`**（人手の対応が要る状態）が上がる

## 5. Azure（Application Insights）に繋ぐ

ローカルで動いたら、送信先を追加します。**アプリ側の変更はありません。**

### 5-1. Azure リソースを作る

**先に `terraform.tfvars` を用意してください。** `image_repository` は既定値が無く、
未設定だと `terraform apply` が対話で聞いてきます。
また `deploy_container_apps` の既定は `true` で、**Container Apps 一式まで作られます。**
テレメトリだけ Azure に送り、アプリはローカルの Docker で動かす構成にしたい場合は
`deploy_container_apps = false` を入れてください。

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# image_repository      = "<GitHubユーザー名>/roastery"   ← 必須。すべて小文字
# deploy_container_apps = false                          ← 監視基盤だけでよい場合
cd ../..
```

```bash
az login
./scripts/azure-setup.sh                    # 既定: dev / japaneast
# 作られるリソース: rg-roastery-dev-je-001 / log-... / appi-...
#   (deploy_container_apps = true なら + Container Apps 環境と 9 個の Container App)
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

- **アプリケーション マップ** — 6つのサービスと依存関係が図になる（初回表示まで 3〜5 分）
- **トランザクション検索** — 個別のリクエストの waterfall
- **ログ** — KQL でクエリ（[docs/kql-cheatsheet.md](docs/kql-cheatsheet.md)）

データが出るまで 1〜3 分のラグがあります（Collector のバッチ + Azure 側の取り込み）。

### 5-4. Azure Container Apps にアプリごと載せる（任意）

テレメトリの送信先だけでなく、アプリ自体を Azure に載せる経路も用意してあります
（ADR-0003 Stage 2）。GitHub Actions から
**GHCR にイメージを push → OIDC で Azure 認証 → `terraform apply`** の順に流れます。

```bash
./scripts/tf-bootstrap.sh <owner>/Roastery dev   # 一度だけ。state 置き場と OIDC 設定を作る
# GitHub > Settings > Environments > dev に AZURE_CLIENT_ID などの variables を設定
# 以降は Actions から deploy ワークフローを手動実行
```

- **長期シークレットは1つも置きません。** Azure へは Workload Identity Federation (OIDC)
- `deploy` / `destroy` はいずれも `workflow_dispatch` のみ。push では走りません
- ACA 上の Collector は Jaeger も Prometheus も居ないため、
  **Azure Monitor だけに送る `config.aca.yaml`** を焼き込んだイメージを使います
- postgres / redis も Container App として動かしています（コスト優先の割り切り。
  永続ボリュームが無いのでリビジョン更新でデータは消えます）

詳細は [infra/terraform/README.md](infra/terraform/README.md) を参照してください。

### 5-5. 片付け

```bash
docker compose down -v
terraform -chdir=infra/terraform destroy
```

Azure に載せている場合は、Actions の `destroy` ワークフロー（環境名の入力が必要）でも
消せます。**state 置き場・Entra ID のアプリ登録・GHCR のパッケージは Terraform の管理外**
なので残ります。

## 6. ディレクトリ

```
.
├── docker-compose.yml          # 全部入り。OTEL_* 環境変数の見本でもある
├── .env.example                # 接続文字列などの設定
├── pytest.ini                  # ユニットテストの設定 (testpaths = tests)
├── apps/                       # アプリケーション
│   ├── frontend/               # Node.js 22
│   │   ├── client/             #   公開サイト: Vue 3 + TypeScript + Vite
│   │   └── server/             #   Express (BFF) + 運用画面の素の HTML
│   ├── order-api/              # Python 3.12 / FastAPI  ← Saga・状態遷移・手動計装の見本
│   ├── inventory-api/          # Python 3.12 / FastAPI + Redis (Lua で原子的に引当)
│   ├── payment-api/            # Python 3.12 / FastAPI  ← 冪等キーと決済状態の永続化
│   ├── member-api/             # Python 3.12 / FastAPI  ← 会員・セッション・住所帳・カート
│   └── external-stub/          # Python 3.12 / FastAPI  ← 決済代行/メール/配送の模擬・障害注入
├── platform/                   # アプリ以外の実行時設定
│   ├── otel-collector/
│   │   ├── config.yaml         # Azure Monitor + Jaeger + Prometheus に多重送信
│   │   ├── config.local.yaml   # ローカルのみ (Jaeger + Prometheus)
│   │   ├── config.aca.yaml     # Azure Container Apps 用 (Azure Monitor のみ)
│   │   └── Dockerfile          # ACA 用に設定を焼き込んだイメージ
│   ├── prometheus/prometheus.yml
│   └── postgres/init.sql       # ローカル用の初期化 (ACA では各サービスが冪等 DDL を流す)
├── infra/                      # IaC
│   ├── terraform/              # Log Analytics + App Insights + Container Apps（azurerm）
│   └── k8s/                    # Kubernetes のテーマで追加（現時点では空）
├── tests/                      # pytest。Redis も Postgres も起動せずに回る
│   ├── test_saga.py            #   補償の順序・前提条件つき補償
│   ├── test_states.py          #   状態遷移表
│   ├── test_stock.py           #   Lua による引当 (fakeredis)
│   ├── test_carts.py / test_member_security.py / test_shipping.py / test_external_stub.py
│   └── conftest.py
├── .github/workflows/
│   ├── ci.yml                  # test / typecheck / build / terraform fmt / 統合テスト
│   ├── deploy.yml              # GHCR へ push → OIDC → terraform apply（手動実行）
│   └── destroy.yml             # terraform destroy（手動実行・確認入力つき）
├── scripts/
│   ├── check-docker.sh         # Docker 環境の事前チェック（いちばん最初）
│   ├── local-start.sh          # ローカルだけで起動 + 動作確認
│   ├── verify.sh               # 起動後の自動チェック（要件番号つき）
│   ├── azure-setup.sh          # terraform apply + .env 更新
│   ├── tf-bootstrap.sh / .ps1  # state 置き場と GitHub Actions の OIDC 設定（一度だけ）
│   ├── optimize-images.sh      # 商品写真を WebP に変換して配信サイズを落とす
│   ├── load.sh                 # 負荷生成（会員登録 → 確認 → ログインまで自動で通す）
│   └── chaos.sh                # 障害注入
└── docs/
    ├── requirements/           # 01-scope 〜 07-implementation-scope
    ├── adr/                    # アーキテクチャ意思決定記録 (0001〜0006)
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
| FastAPI の自動計装そのもの | `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi`。**アプリ側で `instrument_app` を明示的に呼ぶ**ため二重計装を避ける |

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
| ポートが衝突する | 3000（frontend）/ 8001〜8005（order / inventory / payment / external-stub / member）/ 5432 / 6379 / 4317 / 4318 / 8888 / 8889 / 13133 / 55679（Collector）/ 16686（Jaeger）/ 9090（Prometheus）/ 3001（Grafana はプロファイル指定時のみ）を使用。`docker-compose.yml` の `ports` を変更 |

## 9. 動作確認済みの環境

- 検証したバージョン: OpenTelemetry Python `1.44.0` / instrumentation `0.65b0`、
  Node.js SDK `0.221.0` / auto-instrumentations `0.79.0`
- 6サービス（frontend / order / inventory / payment / member / external-stub）を跨ぐ trace、
  カスタムメトリクス、trace_id 付きログの出力を `scripts/verify.sh` で確認済み
- Collector のイメージタグは `.env` の `COLLECTOR_VERSION` で固定できます。
  再現性が必要な場面では `latest` ではなく具体的なバージョンに固定してください

## 10. ライセンス

**コード・ドキュメントは [MIT License](LICENSE)** です。自由に読んで、真似して、
持ち帰ってください。

**ただし MIT が及ぶのはこのリポジトリで書いたものだけです。**
同梱している写真は撮影者に権利があり、別のライセンスに従います。

| 対象 | ライセンス | 備考 |
|---|---|---|
| `apps/` `infra/` `platform/` `scripts/` `tests/` `.github/` のコード | MIT | |
| `docs/` の文書 | MIT | 要件定義・ADR を含む |
| `apps/frontend/client/assets/` の画像 | [Unsplash License](https://unsplash.com/license) | **MIT ではありません。**出典は [CREDITS.md](apps/frontend/client/assets/CREDITS.md) |

画像を再利用する場合は Unsplash License の条件（**他のストック写真・壁紙サイトへの
再配布は不可**）を直接確認してください。このリポジトリを fork してサイトの素材として
使う分には範囲内ですが、画像だけ抜き出して別途配布するのは想定されていません。

依存ライブラリはそれぞれのライセンスに従います
（`apps/*/requirements.txt`・`apps/frontend/package.json`）。

> このリポジトリは業務ではなく個人の学習用に作っています。
