# セキュリティチェックリスト

Roastery を **Public リポジトリ**（ADR-0004）で運用し、**インターネットから到達できる
Azure 環境**（ADR-0003）にデプロイするうえでの確認事項です。

リスクは性質の違う3層に分かれます。**1と2は設定で潰せますが、3はアプリの設計に
踏み込む必要があります。** そして3はリポジトリを private にしても消えません。

---

## 1. 第三者による CI/CD の実行

### 1-1. fork からの PR で何が起きるか

Public リポジトリでは誰でも fork して PR を出せ、CI が動きます。ただし
**`pull_request` トリガーであれば既定でも守りは効いています。**

| | fork からの PR (`pull_request`) |
|---|---|
| 実行コンテキスト | fork 側 |
| リポジトリの Secrets | **アクセス不可** |
| Environments の Secrets / OIDC | **アクセス不可** |
| `GITHUB_TOKEN` | **read-only** |

つまり「CI が勝手に走る」こと自体は起きますが、**それだけでは秘密は漏れず、
インフラにも到達できません。**

- [ ] Settings → Actions → General → Fork pull request workflows から
      **"Require approval for all external contributors"** を選ぶ
      （既定は「初回コントリビュータのみ承認必須」。全員に引き上げる）

これで暗号採掘目的の PR スパム（public リポジトリの定番)も無効化できます。

### 1-2. 本当に危険なのは `pull_request_target` と `workflow_run`

この2つは **base リポジトリの特権コンテキスト**で動きます。Secrets にアクセスでき、
`GITHUB_TOKEN` も write です。ここで**PR のコードを checkout して実行すると、
第三者による任意コード実行**が成立し、全 Secrets が抜かれます（通称 pwn request）。

- [ ] `pull_request_target` を**使わない**
- [ ] `workflow_run` を使う場合、PR のコードを checkout・実行しない
- [ ] やむを得ず使う場合は、PR のコードに触れない処理（ラベル付与など）に限定する

### 1-3. GITHUB_TOKEN の既定権限を絞る

- [ ] Settings → Actions → General → Workflow permissions を
      **"Read repository contents permission"** にする
- [ ] 書き込みが要る job にだけ `permissions:` ブロックで明示的に付与する

```yaml
permissions:
  contents: read          # ワークフロー冒頭で最小に固定
jobs:
  deploy:
    permissions:
      contents: read
      id-token: write     # OIDC に必要な分だけ足す
```

### 1-4. OIDC のサブジェクトを絞る（最重要）

federated credential のサブジェクトが緩いと、**PR のコンテキストから Azure に
到達できてしまいます。** ここだけは設定ミスが致命傷になります。

- [ ] サブジェクトにワイルドカード（`repo:<user>/Roastery:*`）を**使わない**
- [ ] デプロイ用は `repo:<user>/Roastery:environment:dev` のように
      **environment スコープ**に限定する
- [ ] GitHub Environments の **deployment branch policy** で `main` のみに絞る
- [ ] Environments に **required reviewers** を設定する
      → 承認されるまで job が起動しないため、fork PR から OIDC トークンに到達できない
- [ ] PR で `terraform plan` を回す場合、その credential には**読み取りロールのみ**を割り当てる

### 1-5. Action のサプライチェーン

`actions/checkout@v4` の `v4` は**可変タグ**です。タグを移動されると別のコードが動きます。

- [ ] すべての Action を**40桁のコミット SHA でピン留め**する
- [ ] `dependabot.yml` に `package-ecosystem: github-actions` を追加し、SHA を自動更新させる

```yaml
- uses: actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8  # v5.0.0
```

### 1-6. self-hosted runner を使わない

Public リポジトリで self-hosted runner を使うと、**誰でも PR 経由でその
マシン上で任意コードを実行できます。** 例外なく GitHub-hosted runner を使ってください。

- [ ] self-hosted runner を登録しない

---

## 2. 秘密情報の露出

### 2-1. 一度公開した秘密は取り消せない

force push しても、GitHub のキャッシュ・他人の fork・各種アーカイブに残ります。
**漏らした時点で、消すのではなく鍵をローテートする**しかありません。

だからこそ「コミットさせない」側の防御が要ります。**ここは Public のほうが有利**です。

- [ ] Settings → Code security から **Secret scanning** を有効にする（Public は無料）
- [ ] **Push protection** を有効にする → 秘密を含むコミットが push 時にブロックされる

> Private リポジトリの Free プランでは Secret scanning も Push protection も使えません。
> 「Public にすると危険」という直感に反して、**この一点では Public のほうが守られます。**

### 2-2. 公開される情報の棚卸し

- [ ] `.gitignore` に `.env` が入っていることを確認（確認済み）
- [ ] `.env.example` にはプレースホルダのみ。実値を書かない
- [ ] Azure サブスクリプション ID / テナント ID / クライアント ID は
      GitHub Environments の Secrets に置き、リポジトリには書かない
- [ ] `terraform.tfstate` と `*.tfvars` が `.gitignore` 対象であることを確認（設定済み）
      — **state には接続文字列が平文で入ります**
- [ ] Application Insights の接続文字列を README やスクリーンショットに写り込ませない
      （インストルメンテーションキーを含むため）

サブスクリプション ID 単体で侵入されることはありませんが、**攻撃者にとっては
偵察情報**です。無償で渡す理由はありません。

---

## 3. デプロイ後の Azure 側（見落としやすい）

**ここが実質的に最大のリスクです。** そしてリポジトリを private にしても消えません。

### 3-1. 認証なしの管理エンドポイントが公開される

Roastery には、動作確認用に意図的に**認証のない管理エンドポイント**があります。

| サービス | エンドポイント | できること |
|---|---|---|
| payment-api | `POST /admin/chaos` | 遅延・エラー率の注入 |
| inventory-api | `POST /admin/chaos` | 在庫参照の遅延注入 |
| inventory-api | `POST /admin/reset` | 在庫の初期化 |

ローカルの compose なら問題ありませんが、**ACA で外部 ingress にすると
インターネットから誰でも障害を注入できます。** しかもリポジトリが Public なので、
エンドポイントの存在自体が公開情報です。

- [ ] ACA では **frontend だけ external ingress**、order-api / inventory-api /
      payment-api は **internal ingress**（ACA 環境内からのみ到達可）にする
- [ ] frontend の `/ops` と `/api/chaos/*` プロキシを、環境変数で無効化できるようにする
      （**パスを分けただけではアクセス制御にならない**。`/ops` は誰でも開ける）
- [ ] 将来 `/admin/*` に共有シークレットによる簡易認証を足すことを検討する
      — 「公開するなら認証は要る」という当たり前を、実物で確認できます

### 3-2. コストの暴走

ACA は従量課金です。公開 URL を大量に叩かれると scale out して課金が伸びます。
**個人サブスクリプションには支払いの自動停止がありません。**

- [ ] Container App の **max replicas を小さく固定**する（例: 2）
- [ ] **Azure Budgets** でサブスクリプション単位の予算とアラートを設定する
- [ ] Log Analytics の日次取り込み上限 1GB を維持する（設定済み）
- [ ] 使わない期間は `terraform -chdir=infra/terraform destroy` で環境ごと消す
      （`az group delete` で消すと Terraform の state と実体がずれます）

### 3-3. 将来 AKS を立てるとき

Kubernetes のテーマで扱う論点として、以下を先に認識しておいてください。

- API サーバーの公開範囲（Public / Private クラスタ）
- Ingress の TLS 終端と証明書（「ドメイン・DNS・証明書」のテーマと接続）
- NetworkPolicy によるサービス間通信の制限
- クラスタを立てっぱなしにしないこと（コストが桁違い）

---

## 4. Public のほうが有利な点（整理）

| 機能 | Public (Free) | Private (Free) |
|---|---|---|
| Secret scanning / Push protection | **無料で使える** | 使えない |
| Dependabot alerts | 使える | 使える |
| CodeQL（コードスキャン） | **無料で使える** | 使えない |
| Actions 標準ランナー | 無制限 | 2,000 分/月 |

**セキュリティ機能に関しては、Public のほうが手厚い**というのが実態です。
「公開 = 危険」ではなく、「公開だから防御機能が無料で付いてくる」という側面があります。

---

## 5. 最初に一度だけやる設定（まとめ）

リポジトリ作成直後に、以下を順に実施してください。

1. [ ] Settings → Actions → General
       - Fork pull request workflows: **Require approval for all external contributors**
       - Workflow permissions: **Read repository contents permission**
2. [ ] Settings → Code security
       - **Secret scanning** を有効化
       - **Push protection** を有効化
       - **Dependabot alerts** を有効化
3. [ ] Settings → Branches（または Rules）
       - `main` の保護を設定（ADR-0002 の表のとおり）
4. [ ] Settings → Environments
       - `dev` を作成し、deployment branch policy を `main` のみに設定
5. [ ] Azure ポータル
       - Budgets で予算アラートを設定
6. [ ] ワークフローを書くとき
       - Action を SHA でピン留め
       - `pull_request_target` を使わない
       - OIDC のサブジェクトを environment スコープに限定
