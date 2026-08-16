# ADR-0003: デプロイ戦略

**Status:** Accepted
**Date:** 2026-08-15
**Deciders:** 本人

> **改訂記録 (2026-08-16):** 環境識別子を dev / prod から **prd / stg / dev** に改め、
> リソースグループ名を ADR-0006 の命名規則に合わせました。IaC ツールは ADR-0005 で
> Bicep から Terraform に変更しています。
>
> **改訂記録 (2026-08-15):** 初版では Azure リソースの所属と公開範囲を未決としていました。
> **個人アカウント / 個人サブスクリプション / コスト最小化**という前提が確定したため、
> コンテナレジストリと環境構成を見直しています（ACR → GHCR、prod は後回し）。

## Context

現在の Roastery はローカルの Docker Compose で動き、Azure には Application Insights
だけを使っています。ここに CI/CD を入れるにあたっての前提は以下です。

- **Kubernetes は学習テーマに含まれる**ため、最終的には AKS に載せたい
- ただし AKS を最初から立てると、クラスタの常時起動コストと運用負荷が
  CI/CD パイプライン自体の学習を圧迫する
- **CI/CD 自体も学習テーマとして扱う**ため、パイプラインは完成形を置くのではなく
  段階的に育てられる形にしたい
- **GitHub / Azure ともに個人のもの**を使い、**コストは可能な限り抑える**
- リポジトリは個人アカウントの Public（ADR-0004）
- 業務で扱うのは Azure なので、学習内容も Azure のプラクティスに寄せる価値がある

## Decision

**Azure Container Apps から始め、Kubernetes のテーマで AKS を追加します。**
両者は排他ではなく、**同じコンテナイメージを2つの実行基盤にデプロイできる状態**を
目指します。これ自体が「コンテナ化しておくと実行基盤を選べる」という学習題材になります。

### コンテナレジストリ: GHCR から始める

イメージは **GitHub Container Registry (ghcr.io)** に push します。

- リポジトリが Public なので、**公開パッケージは無料かつ認証なしで pull できます**
- ACA からの pull にレジストリ資格情報が不要になります
- ACR Basic（約 $5/月）を当面省けます

**ACR は Kubernetes のテーマで導入します。** ACR と AKS のマネージド ID 連携
（`az aks update --attach-acr`）はそれ自体が実務で必ず通る論点なので、
「なぜ GHCR から ACR に移すのか」を含めて学習対象にできます。

### 環境: dev から始める

| 環境 | RG | いつ作るか |
|---|---|---|
| dev | `rg-roastery-dev-je-001` | Stage 2（CI/CD のテーマ） |
| stg | `rg-roastery-stg-je-001` | 必要になった時点 |
| prd | `rg-roastery-prd-je-001` | Stage 3（同じテーマの後半） |

環境識別子とリソース名の付け方は ADR-0006 に従います。

初版では共有 RG に ACR を置く構成でしたが、**GHCR を使うため共有 RG は不要**に
なりました。コンピュートと可観測性を環境ごとの RG にまとめ、**まるごと消して
作り直せる単位**にします。この RG の切り方自体が学習対象です。

prd を最初から作らないのは、**環境プロモーションを CI/CD のテーマの題材として
残しておく**ためです。dev だけが存在する状態から「本番を足す」という流れの方が、
なぜ環境を分けるのかを実感しやすくなります。

### 認証: OIDC のみ。長期シークレットを置かない

GitHub Actions から Azure へは **Entra ID の Workload Identity Federation (OIDC)**
で認証します。サービスプリンシパルの秘密鍵はリポジトリに登録しません。

federated credential は用途ごとに分けます。

| サブジェクト | 用途 | 権限 |
|---|---|---|
| `repo:<user>/Roastery:environment:dev` | dev へのデプロイ | `rg-roastery-dev-je-001` に Contributor |
| `repo:<user>/Roastery:environment:prd` | prd へのデプロイ | `rg-roastery-prd-je-001` に Contributor |
| `repo:<user>/Roastery:pull_request` | `terraform plan` | 読み取り権限のみ |

**PR からは読み取り権限しか使えない**という分離が重要です。Public リポジトリなので
fork からの PR が来る可能性があり、インフラを壊せない構造を権限設計で担保します
（ADR-0004 の対策と対応）。

### デプロイのトリガー

| 契機 | 対象 | 承認 |
|---|---|---|
| PR 作成・更新 | なし（lint / test / build / `terraform plan` のみ） | — |
| `main` への push | dev | 自動 |
| `vX.Y.Z` タグ | prd | GitHub Environments の required reviewers で承認 |

1人運用でも**デプロイ承認は自分で行えます**（PR 承認と違い、Environments の
required reviewers は自分自身を指定でき、自分のデプロイを承認できます）。
ADR-0002 で必須承認数を 0 にしたのとは対照的な挙動で、この違い自体が
CI/CD のテーマで扱える論点です。

### パイプラインの段階的な育て方

CI/CD をテーマとして扱うため、最初から完成形は置きません。

| Stage | 内容 | いつ |
|---|---|---|
| 1 | lint / test / build。イメージは作るが push しない | Git 管理開始と同時 |
| 2 | GHCR への push、dev への自動デプロイ、OIDC 認証 | CI/CD のテーマ |
| 3 | PR への `terraform plan` コメント、prd の承認デプロイ | CI/CD のテーマ |
| 4 | ACR への移行、AKS へのデプロイ、GitOps | Kubernetes のテーマ |

Stage 1 を先に入れておくのは、**ブランチ保護の必須チェックとして機能させる**ためです
（ADR-0002）。デプロイがなくても CI があれば `main` の品質は守れます。

### 可観測性の配置

| 実行基盤 | Collector の置き方 |
|---|---|
| ローカル (compose) | 単独コンテナ（現状のまま） |
| ACA | 単独の Container App として常駐。アプリからは内部 FQDN で OTLP 送信 |
| AKS | DaemonSet + Gateway の2段 |

App Insights は環境ごとに分けます。設定の実体は `platform/otel-collector/` の1箇所に
置き、compose のボリュームマウントと Kubernetes の ConfigMap の両方から参照します
（ADR-0001）。

### コストの見積もり

| 項目 | 月額の目安 |
|---|---|
| GitHub Actions（Public リポジトリ） | **$0**（標準ランナー無料・無制限） |
| GHCR（公開パッケージ） | **$0** |
| ACA（min replicas = 0） | アイドル時ほぼ **$0** |
| App Insights / Log Analytics | 日次1GB 上限。この規模なら数十〜数百円 |
| ACR Basic（Kubernetes のテーマから） | 約 $5 |
| AKS（Kubernetes のテーマのみ） | クラスタ稼働中のみ発生。着手の前後で作成・削除する |

**Stage 3 までは月額ほぼゼロ**に収まる想定です。費用が発生するのは
Kubernetes のテーマで ACR と AKS を足してからで、AKS は使うときだけ立てます。

## Options Considered

### Option A: 段階的に ACA → AKS（採用）

| Dimension | Assessment |
|---|---|
| 複雑さ | Medium |
| コスト | Low（AKS のテーマのみ増加） |
| 学習価値 | High |
| 実務との近さ | High |

**Pros:** CI/CD パイプラインを AKS の準備を待たずに立てられる。「同じイメージを
違う基盤に載せる」という比較が最終的に成立し、ACA と AKS の選定基準を実物で語れる。
コストが段階的に増える。
**Cons:** Terraform が ACA 用と AKS 用の2系統になり、モジュール設計を最初から
考えておく必要がある。

### Option B: 最初から AKS 一本

| Dimension | Assessment |
|---|---|
| 複雑さ | High |
| コスト | Medium〜High（常時起動） |
| 学習価値 | High |
| 実務との近さ | High |

**Pros:** 業務での実務に最も近い。Helm / Kustomize / GitOps まで一気通貫で扱える。
遠回りがない。
**Cons:** CI/CD のテーマで「パイプラインを作る」ことに集中したいのに、関心が
Kubernetes の概念に持っていかれます。**2つのテーマが同時に来ると、どちらも
消化不良になる**のが最大の懸念です。個人サブスクリプションでクラスタを常時起動すると
月額も無視できません。

### Option C: ACA のみ

| Dimension | Assessment |
|---|---|
| 複雑さ | Low |
| コスト | Low |
| 学習価値 | Medium |
| 実務との近さ | Medium |

**Pros:** 最も単純で安い。Compose からの移行が素直で、CI/CD の学習に集中できる。
**Cons:** Kubernetes がテーマに入っている以上、いずれ AKS が必要になります。
その時点で「ACA 用に作った Terraform とパイプラインをどうするか」を考えることになり、
結局 A と同じ作業を後ろ倒しにしただけになります。

### Option D: CI のみ、デプロイなし

**Pros:** Azure コストが完全にゼロ。最速で始められる。
**Cons:** CD の学習価値が丸ごと失われます。OIDC 認証、環境プロモーション、
承認ゲート、what-if による事前確認といった、**実務で最も相談を受ける部分**が
扱えなくなります。ACA が実質無料である以上、コストを理由にここを捨てる必然性は
ありません。

## Trade-off Analysis

**A と B の分岐点は「2つの新概念を同時に扱うか」**です。
CI/CD と Kubernetes はどちらも重いテーマで、同時に手を付けると両方が中途半端になります。
A なら CI/CD のテーマでは「パイプラインの作り方」だけに集中でき、Kubernetes のテーマでは
「既に動いているパイプラインのデプロイ先を差し替える」という形で、
それまでの資産が効いてくる構図になります。これは Roastery 全体の方針とも一致します。

**C は「後ろ倒しにしただけ」**という評価に尽きます。ACA 用の Terraform とワークフローは
どのみち書くことになるので、AKS を後から足す A との差は、最初から AKS を
見込んだモジュール設計にしておくかどうかだけです。それなら最初から見込んでおく方が安い。

**GHCR と ACR の選択**は、この段階ではコストだけで決めました。Public リポジトリの
公開パッケージなら無料で認証も不要という条件が強すぎます。ただし ACR を捨てたわけでは
なく、**AKS のテーマで移行する題材として温存**しています。「無料で始めて、必要になったら
移す」という判断過程自体が、実務でよくある意思決定の練習になります。

## Consequences

**楽になること**

- CI/CD のテーマを Kubernetes の進捗と独立に進められる
- シークレットがパイプラインにもランタイムにも存在しない構成を一貫して保てる
- `rg-roastery-dev-je-001` をまるごと削除して作り直せる
- Stage 3 まで月額ほぼゼロで進められる

**難しくなること**

- Terraform を最初からモジュール分割しておく必要がある（ACA と AKS で共通部分を括る）
- Collector の配置が実行基盤ごとに3通りになり、設定の実体を1箇所に保つ工夫が要る
- レジストリを GHCR から ACR に移す作業が Kubernetes のテーマで発生する
- ACA と AKS の両方を維持するので、片方が腐らないよう CI で両方ビルドし続ける必要がある

**あとで見直すこと**

- prd / stg 環境を本当に作るか。Stage 3 の時点で必要性を再評価する
- GitOps ツール（Argo CD / Flux）の選定は Kubernetes のテーマで別 ADR にする
- ACA を AKS 移行後も残すか、役目を終えたとして畳むか

## Action Items

1. [ ] Stage 1 のワークフローを作成する（lint / test / build、paths フィルタ付き）
2. [ ] `main` の必須ステータスチェックに Stage 1 の CI を登録する（ADR-0002）
3. [ ] Entra ID にアプリ登録と federated credential を作成する（`environment:dev` / `pull_request`）
4. [ ] GitHub Environments に `dev` を作成する
5. [ ] `infra/terraform/` を ACA と AKS で共通化できるモジュール構成に分割する
6. [x] README のテーマ一覧に「CI/CD」を追加する
