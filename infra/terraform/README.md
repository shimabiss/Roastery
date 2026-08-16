# Terraform — Azure リソース

Roastery のテレメトリ送信先（Log Analytics ワークスペース + Application Insights）を
Terraform / azurerm プロバイダで管理します。

選定の経緯と、Bicep から移行した理由は
[../../docs/adr/0005-iac-tool.md](../../docs/adr/0005-iac-tool.md) を参照してください。

## 命名規則

```
<種別>-<ワークロード>-<環境>-<リージョン>-<連番>
```

| 要素 | 変数 | 値 |
|---|---|---|
| 種別 | （固定） | `rg` / `log` / `appi` — Azure CAF の推奨略号 |
| ワークロード | `workload` | `roastery` |
| 環境 | `environment` | `prd`（本番）/ `stg`（ステージング）/ `dev`（開発） |
| リージョン | `location` → 略号に変換 | `japaneast` → `je` / `japanwest` → `jw` |
| 連番 | `instance` | `001` |

生成される名前:

```
rg-roastery-dev-je-001
log-roastery-dev-je-001
appi-roastery-dev-je-001
```

**リージョンの略号は `var.location_abbreviations` が単一の情報源**です。
`var.location` の validation もこの変数を参照しているため、リージョンを増やすときに
直すのは1箇所だけです。未定義のリージョンを指定すると plan の時点で弾かれます。

```hcl
location_abbreviations = {
  japaneast = "je"
  japanwest = "jw"
  eastus    = "eus"   # 追加するとこのリージョンも指定できるようになる
}
```

> **Log Analytics は削除後 14 日間、同名で再作成できません**（論理削除）。
> 作り直しが重なって衝突したら `instance = "002"` に上げてください。

決定の経緯は [../../docs/adr/0006-resource-naming.md](../../docs/adr/0006-resource-naming.md) にあります。

## 作るもの

| リソース | 用途 |
|---|---|
| `azurerm_resource_group` | まるごと消して作り直せる単位 |
| `azurerm_log_analytics_workspace` | ログの実体。**日次取り込み上限 1GB** を設定して課金を止める |
| `azurerm_application_insights` | ワークスペースベース。接続文字列を Collector に渡す |
| `azurerm_container_app_environment` | Container Apps の実行環境。ログは Log Analytics に入る |
| `azurerm_container_app` × 9 | アプリ7つ + postgres + redis |

`deploy_container_apps = false` にすると Container Apps を作らず、
**ローカルの Docker で動かしてテレメトリだけ Azure に送る**当初の構成に戻せます。

### Container Apps の構成

| アプリ | ingress | 呼び出し方 | レプリカ |
|---|---|---|---|
| `frontend` | **外部**（HTTPS） | 唯一の公開エンドポイント | 0〜2 |
| `order-api` / `inventory-api` / `payment-api` / `member-api` / `external-stub` | 内部 HTTP | `http://<名前>.internal.<ドメイン>` | 0〜2 |
| `postgres` / `redis` | 内部 TCP | `postgres:5432` / `redis:6379` | 1 固定 |
| `otel-collector` | 内部 TCP | `http://otel-collector:4317` | 1 固定 |

**TCP ingress を内部に限っている**のは、外部 TCP ingress だけが VNET を要求するためです。
内部なら VNET なしで使えます。そして内部 TCP は「アプリ名:公開ポート」で解決するので、
**`DATABASE_URL` などの文字列が docker compose と完全に一致します。**

### データストアの扱い（重要）

`postgres` と `redis` を **マネージドサービスではなく Container App** で動かしています。

| 方式 | 月額の目安 |
|---|---|
| コンテナアプリ（採用） | 数百円 |
| PostgreSQL Flexible Server + Azure Cache for Redis | ¥4,000〜5,000 |

ADR-0003 の「Stage 3 までは月額ほぼゼロ」を守るための選択で、
**設計として正しいからではありません。** 引き換えに次を受け入れています。

- リビジョンが入れ替わると**データが消える**（永続ボリュームなし）
- レプリカを増やせない（`max_replicas = 1` 固定）
- バックアップ・PITR・自動フェイルオーバーが無い

**本番を名乗るならここは必ずマネージドに置き換えます。**

なお **`init.sql` は ACA に持ち込んでいません。** 各サービスが起動時に冪等な DDL を
流す（`app/schema.py`）ため、そのまま動きます。「新規構築」と「既存環境の更新」を
分けておいた判断が、実行基盤を変えたときに効いた形です。

## 使い方

いちばん簡単なのはリポジトリ直下のスクリプトです。`terraform apply` から
`.env` への接続文字列の書き込みまでをまとめて実行します。

```bash
az login
./scripts/azure-setup.sh              # 既定: dev / japaneast
./scripts/azure-setup.sh stg japanwest   # 環境とリージョンを指定する場合
```

Terraform を直接叩く場合:

```bash
export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)

cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # 必要なら値を編集
terraform init
terraform plan
terraform apply

# 接続文字列を取り出す（機微情報なので -raw で明示的に）
terraform output -raw connection_string
```

片付け:

```bash
terraform -chdir=infra/terraform destroy
```

## state について

**state は Azure Storage に置きます。**

CI から `terraform apply` する以上、これは必須です。ローカル state のままだと
GitHub Actions は毎回まっさらな state で始まり、既に存在するリソースを
「新規作成」しようとして失敗します。**「手元では動くのに CI では壊れる」の典型**です。

置き場は `scripts/tf-bootstrap.sh` が一度だけ作ります。**Terraform では作りません。**
state を置く場所を Terraform で作ると、その Terraform 自身の state をどこに置くのか
という鶏と卵になるためです。「一度だけ手で作り、以後は触らない」ものは
Terraform の外に出すのが定石です。

`versions.tf` の backend は **partial configuration** にしてあり、
値は `init` 時に外から与えます。

```bash
terraform init -backend-config=backend.hcl
```

注意点:

- **アカウントキーではなく Entra ID 認証**（`use_azuread_auth = true`）を使います。
  アカウントキーは長期シークレットなので、置かない・配らない・使わない
- `backend.hcl` は `.gitignore` 対象です。秘密情報は含みませんが、
  Public リポジトリにストレージアカウント名を置かないための措置です
- **環境ごとに `key` を分けます。** 同じキーを共有すると
  dev の apply が本番の state を書き換えます。環境の分離は state の分離から始まります
- state には **postgres のパスワードと App Insights の接続文字列が平文で入ります。**
  ブートストラップ時にバージョニングと論理削除を有効にしてあります

### 既にローカル state がある場合

```bash
terraform init -migrate-state -backend-config=backend.hcl
```

## バージョンの方針

`versions.tf` でメジャーを跨がない範囲に制約を書き、実際に使われた版は
`.terraform.lock.hcl` に記録します。**ロックファイルはコミットします。**

プロバイダを上げるときは意図的に行ってください。

```bash
terraform init -upgrade
```

## ファイル構成

```
infra/terraform/
├── versions.tf              # Terraform 本体とプロバイダのバージョン制約、backend
├── providers.tf             # azurerm プロバイダの設定
├── variables.tf             # 入力変数
├── main.tf                  # リソース定義
├── outputs.tf               # 接続文字列などの出力
├── container-apps.tf        # Container Apps 環境とアプリ 9 つ
├── terraform.tfvars.example # 変数のひな形（tfvars 本体は .gitignore 対象）
├── backend.hcl.example      # backend の設定（本体は .gitignore 対象）
└── README.md
```

---

## Azure へデプロイする

**2段階で進めます。** 1回目で認証・state・plan/apply の経路だけを通し、
2回目で Container Apps を載せます。一度に全部やると、失敗したときに
**「認証が通っていないのか」「ACA の定義が悪いのか」が切り分けられません。**

### 1. 一度だけ: state 置き場と OIDC を作る

bash（WSL2 / Cloud Shell / macOS）:

```bash
az login
./scripts/tf-bootstrap.sh <GitHubユーザー名>/Roastery dev
```

PowerShell（Windows）:

```powershell
az login
az account set --subscription "<サブスクリプション名またはID>"
.\scripts\tf-bootstrap.ps1 -Repo <GitHubユーザー名>/Roastery
```

**どちらも作られるものは同じです。** 何度実行しても結果は変わりません
（既にあるものは作り直しません）。

このスクリプトが作るもの:

- state 用のリソースグループ / ストレージアカウント / コンテナ
- GitHub Actions 用の Entra ID アプリ
- **環境スコープのフェデレーション資格情報**（`repo:<owner>/Roastery:environment:dev`）
- 必要なロール割り当て（Contributor / Storage Blob Data Contributor）

> subject をワイルドカードにすると、**フォークや任意ブランチからでも Azure に入れます。**
> Public リポジトリでは「誰でも入れる」と同義なので、必ず環境スコープで固定します。

終了時に `infra/terraform/backend.hcl` が書き出され、GitHub に設定する値が表示されます。

### 2. GitHub の Environment に変数を設定する

Settings → Environments → `dev` → Variables:

| 名前 | 値 |
|---|---|
| `AZURE_CLIENT_ID` | ブートストラップの出力 |
| `AZURE_TENANT_ID` | 同上 |
| `AZURE_SUBSCRIPTION_ID` | 同上 |
| `TFSTATE_RESOURCE_GROUP` | `rg-roastery-tfstate` |
| `TFSTATE_STORAGE_ACCOUNT` | ブートストラップの出力 |

いずれも秘密情報ではないので `secrets` ではなく `variables` で構いません。
**OIDC を使う限り、GitHub に置く長期シークレットはゼロになります。**

Environment 名は `dev` から変えないでください。フェデレーション資格情報の
subject が `repo:<owner>/Roastery:environment:dev` で固定されています。

### 3. 1回目: 監視基盤だけを apply する

Actions → deploy → Run workflow → **`deploy_container_apps` を `false`** にして実行。

- `build` … 7つのイメージをビルドして GHCR に push する（**ここは毎回走る**）
- `terraform` … Log Analytics と Application Insights だけを作る
- `smoke` … スキップされる（まだ検証対象が無いため）

**ここで確かめているのは OIDC 認証と state の経路です。** よくある失敗:

| 症状 | 原因と対処 |
|---|---|
| `AADSTS70021: No matching federated identity record found` | Environment 名が `dev` でないか、subject のリポジトリ名の大小文字が違う。`az ad app federated-credential list --id <AZURE_CLIENT_ID>` で subject を確認する |
| `AuthorizationFailed` | ロール割り当てがまだ反映されていない。数分待って再実行 |
| state の blob が 403 | サービスプリンシパルに `Storage Blob Data Contributor` が付いていない |
| `The subscription is not registered to use namespace 'Microsoft.App'` | `az provider register --namespace Microsoft.App`（`Microsoft.OperationalInsights` も同様） |

### 4. GHCR のパッケージを public にする ← **忘れやすい**

`build` が通ると `https://github.com/users/<owner>/packages` に7つのパッケージができます。
**個人アカウント配下のパッケージは既定で private です。**
一方 ACA には registry の資格情報を渡していない（`container-apps.tf` に
`registry` ブロックが無い）ため、private のままだと `ImagePullFailure` で
リビジョンが起動しません。

各パッケージ → Package settings → Danger Zone → Change visibility → **Public**

対象は `roastery/frontend` `roastery/order-api` `roastery/inventory-api`
`roastery/payment-api` `roastery/member-api` `roastery/external-stub`
`roastery/otel-collector` の7つです。

> private のまま使うなら PAT を ACA に渡すことになり、
> **「長期シークレットを置かない」という前提と衝突します。**
> リポジトリ自体が Public なので、イメージを public にして困ることはありません。
> ただし **public から private には戻せません。**

### 5. 2回目: Container Apps を載せる

Actions → deploy → Run workflow → `deploy_container_apps` は既定の `true` のまま実行。

`smoke` ジョブが `/healthz` と `/api/catalog` まで確認します。
**「apply が成功した」と「動いている」は別**なので、ここが緑になって初めて完了です。
公開 URL は Actions のサマリに出ます。

`min_replicas = 0` なので最初の1リクエストはコールドスタートで数秒かかります。
`smoke` はそれを見越して最大5分待ちます。

### 6. push で自動デプロイに切り替える（任意）

ここまで通ったら `.github/workflows/deploy.yml` の `on:` に push を戻します。

```yaml
on:
  push:
    branches: [main]
  workflow_dispatch:
    ...
```

**準備が済むまで戻さない**のは、`AZURE_CLIENT_ID` が無い状態で push のたびに
失敗すると、「赤い × が付いているのが普通」になってしまうためです。
そうなると本当の失敗に気づけなくなります。

### 7. 手元から apply する場合

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# image_repository = "<GitHubユーザー名を小文字で>/roastery" を設定する（必須）

terraform init -backend-config=backend.hcl
terraform plan
terraform apply

terraform output -raw site_url
```

**GHCR はリポジトリ名に大文字を許しません。** `Roastery` ではなく `roastery` と書きます
（`variables.tf` の validation でも弾かれます）。CI 側は `${GITHUB_REPOSITORY,,}` で
自動的に小文字化しているので、この注意が要るのは手元から流すときだけです。

### 8. コストを止める

アイドル時に課金が出るのは `postgres` / `redis` / `otel-collector` の**3つだけ**です
（`min_replicas = 1` のため）。アプリは 0 までスケールインします。

この3つで常時 **1.0 vCPU / 2.0 GiB** を占有します。30日で約 259万 vCPU 秒・518万 GiB 秒に
なり、Container Apps の無料枠（サブスクリプションあたり月 18万 vCPU 秒 / 36万 GiB 秒）を
**十数倍超えます。** 大半はアイドル料金が適用されますが、ゼロにはなりません。

- **デプロイ前に予算アラートを設定してください**（Cost Management → 予算）
- 実際の金額は1〜2日動かしてから Cost Management で確認するのが確実です

使わない期間は止められます。

```bash
terraform apply -var deploy_container_apps=false
```

GitHub Actions から止める場合は Run workflow で `deploy_container_apps` を `false` に
します。Log Analytics と Application Insights は残るので、ローカルの docker compose から
テレメトリを送る構成はそのまま使えます。

## 既知の未対応

| 内容 | 影響 |
|---|---|
| `/ops`（運用画面）に認証が無い | URL を知っていれば誰でも障害注入と出荷操作ができる。公開サイトと同じ Container App に同居しているため、ingress では分離できていない |
| データストアに永続ボリュームが無い | リビジョン入れ替えでデータが消える |
| ロール割り当てがサブスクリプション スコープ | Terraform がリソースグループ自体を作るため絞れない。ただし Owner ではなく Contributor にしてある |
