# ADR-0001: リポジトリ構成

**Status:** Accepted
**Date:** 2026-08-15
**Deciders:** 本人

## Context

Roastery は「1つのシステムをテーマごとに拡張していく」方針の個人学習用サンプルです。現時点では
OpenTelemetry のテーマまで実装されており、今後以下が加わります。

- Kubernetes マニフェスト（Helm / Kustomize）
- 環境別の Terraform 変数（IaC ツールの選定は ADR-0005）
- GitHub Actions のワークフロー
- Go / Rust で書き直したサービス

つまり **今の構成は「一番小さい状態」であり、置き場が足りなくなることが確定している**
という前提で決める必要があります。

制約は2つです。

1. clone して `./scripts/local-start.sh` 一発で動くこと。この手軽さは崩さない
2. GitHub Actions で「変更されたサービスだけビルドする」ことができる粒度になっていること

現状の構成は以下です。

```
Roastery/
├── docker-compose.yml
├── collector/        # OTel Collector と Prometheus の設定
├── db/               # init.sql
├── services/         # 4サービス
├── infra/            # main.bicep 1枚（当時。ADR-0005 で Terraform に移行）
├── scripts/
└── docs/
```

## Decision

**モノレポ1つ**を維持したうえで、以下のレイアウトに再編します。

```
Roastery/
├── .github/
│   ├── workflows/               # CI/CD
│   ├── CODEOWNERS
│   ├── dependabot.yml
│   └── pull_request_template.md
├── apps/                        # アプリケーション（旧 services/）
│   ├── frontend/                # Node.js 22 / Express
│   ├── order-api/               # Python 3.12 / FastAPI
│   ├── inventory-api/           # Python 3.12 / FastAPI
│   └── payment-api/             # Python 3.12 / FastAPI
├── platform/                    # アプリ以外の実行時設定（旧 collector/, db/）
│   ├── otel-collector/          # config.yaml / config.local.yaml
│   ├── prometheus/              # prometheus.yml
│   └── postgres/                # init.sql
├── infra/                       # IaC
│   ├── terraform/               # azurerm プロバイダ（ADR-0005）
│   └── k8s/                     # Kubernetes のテーマで追加（今は README のみ）
├── scripts/
├── docs/
│   ├── adr/
│   ├── mock/
│   └── （既存の md 群）
├── docker-compose.yml           # ルート据え置き
├── .env.example
└── README.md
```

### 決定に含まれる3つの判断

**1. `services/` を `apps/` に改名する**

`service` は OpenTelemetry の `service.name` と Kubernetes の Service リソースの
両方で使われる語です。この2つを扱うリポジトリで、ディレクトリ名にも同じ語を使うと
「サービスを直して」が3通りに読めてしまいます。`apps/` なら曖昧さがありません。

**2. `platform/` を新設する**

`collector/` と `db/` はアプリではありませんが、ローカル実行にもクラウド実行にも
必要な設定です。今後 Grafana のプロビジョニング設定や Collector のマニフェストが
増えるとルート直下が散らかるため、先に箱を作ります。

同じ Collector 設定を compose のボリュームマウントと Kubernetes の ConfigMap の
両方から参照することになるので、**設定の実体は1箇所**という原則も同時に立てます。

**3. `docker-compose.yml` はルートに残す**

`deploy/compose/` に移す案もありましたが、採りませんでした。最初に打つコマンドは
`docker compose up` であり、`docker compose -f deploy/compose/compose.yaml up` は
入口の摩擦として割に合いません。ルートの compose ファイルは「ローカル開発環境の定義」
であると同時に「OTEL_* 環境変数の見本」でもあり、README から真っ先に参照されます。

## Options Considered

### Option A: 現状構成を維持し `.github/` だけ追加する

| Dimension | Assessment |
|---|---|
| 複雑さ | Low |
| 移行コスト | ほぼゼロ |
| 拡張性 | Low |
| 学習用としての明快さ | Medium |

**Pros:** 今すぐ Git 管理を始められる。既存ドキュメントのパス記述を直す必要がない。
**Cons:** K8s マニフェストと環境別パラメータの置き場が `infra/` 1階層に集中して破綻する。
`services/` の語の衝突が、OTel と K8s を扱うたびに読み違いの元になる。

### Option B: モノレポで再編する（採用）

| Dimension | Assessment |
|---|---|
| 複雑さ | Medium |
| 移行コスト | 中（30ファイル程度の移動 + パス参照の修正） |
| 拡張性 | High |
| 学習用としての明快さ | High |

**Pros:** paths フィルタで CI をサービス単位に出し分けられる。Terraform と K8s マニフェストが
`infra/` の下に並び、クラウドリソースとクラスタ内リソースの境界が視覚的に分かる。
サービスや環境が増えたときの置き場が自明。
**Cons:** 既存ドキュメント内のパス記述を全部直す必要がある。
Git 管理開始前にやらないと、履歴の最初に大きなリネームコミットが載る。

### Option C: app と infra を別リポジトリに分離する

| Dimension | Assessment |
|---|---|
| 複雑さ | High |
| 移行コスト | 高 |
| 拡張性 | High |
| 学習用としての明快さ | Low |

**Pros:** アプリ開発者とインフラ担当で権限を分けられる。リリースサイクルを独立させられる。
実務でよく見る構成なので、その意味では現実的。
**Cons:** 学習用としては筋が悪い。「アプリのこの変更にはインフラのこの変更が要る」という
対応関係が、2リポジトリのバージョン整合という別の難問にすり替わります。
2つ clone して2つ切り替える手間も毎回かかります。

## Trade-off Analysis

**A と B の分岐点は「いつ払うか」だけ**です。再編コストは今なら30ファイルの移動で
済みますが、Git 管理を始めてからやると、履歴の先頭に大きなリネームコミットが載り、
その後のすべての差分が読みにくくなります。**今が最も安い**という一点で B を採ります。

**C は実務では正しいことが多いが、学習用では逆**という典型例です。実務のリポジトリ分離は
組織の権限境界に合わせるためのもので、Roastery にはその境界がありません。分離の
メリットだけが消えて、デメリットだけが残ります。

なお C の考え方自体は捨てません。「設計 (デザインパターン)」のテーマで、モノレポと
ポリレポの境界をどう引くかという題材として扱えます。

## Consequences

**楽になること**

- `paths: apps/order-api/**` のようなフィルタで、変更されたサービスだけビルドできる
- サービスを追加するとき（Go 版 payment-api など）の置き場を議論しなくてよい
- Terraform と K8s マニフェストが並ぶので、クラウド側とクラスタ内の責務の切れ目が見える

**難しくなること**

- README / wsl2-setup のパス記述を一斉に直す必要がある
- `docker-compose.yml` の build context とボリュームマウントのパスが全部変わる
- 作成済みの図やスクリーンショットとディレクトリ名が食い違う

**あとで見直すこと**

- `apps/` が6つを超えたら、言語別のサブディレクトリを切るか検討する
- `platform/` に Grafana / Tempo / Loki が加わったら、`platform/observability/` に
  一段掘るか検討する
- Go / Rust サービスが入ったとき、依存管理ファイル（go.mod / Cargo.toml）を
  ルートに置くかサービス配下に置くかを別 ADR で決める

## Action Items

1. [ ] `services/` → `apps/`、`collector/` → `platform/otel-collector/`、
       `db/` → `platform/postgres/`、`collector/prometheus.yml` → `platform/prometheus/` へ移動
2. [x] `infra/` を `terraform/` と `k8s/` に分ける（IaC ツールの選定は ADR-0005）
3. [ ] `docker-compose.yml` の build context / volumes のパスを修正
4. [ ] `scripts/*.sh` 内のパス参照を修正
5. [ ] README / wsl2-setup のパス記述を修正
6. [ ] `./scripts/local-start.sh` が通ることを確認してから Git 管理を開始する
