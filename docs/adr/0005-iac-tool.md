# ADR-0005: IaC ツールに Terraform / azurerm を使う

**Status:** Accepted
**Date:** 2026-08-15
**Deciders:** 本人

## Context

Azure リソース（Log Analytics ワークスペース + Application Insights）は当初 Bicep で
記述していました。ADR-0003 でも「PR で Bicep の what-if を実行する」ことを前提に
デプロイ戦略を組んでいます。

その前提を見直します。理由は以下です。

- ADR-0003 の方針では、最終的に **ACA と AKS の両方**を IaC で扱う
- Roastery は Azure だけを対象にしているが、**IaC ツールの選定は Azure 単独では
  決まらない**。実務では GitHub リソース、Entra ID のアプリ登録、
  外部 SaaS の設定などが同じパイプラインに乗ってくる
- CI/CD のテーマで扱いたい「plan を PR にコメントする」「state をどう共有するか」
  「drift をどう検知するか」といった論点は、**Terraform の方が題材として厚い**

制約は2つです。

1. 現時点で管理しているのは Log Analytics と Application Insights の2つだけ。
   移行コストは小さいので、**やるなら今**
2. GitHub / Azure ともに個人のもので、コストは抑えたい（ADR-0004）

## Decision

**IaC を Terraform に一本化し、プロバイダは `hashicorp/azurerm` を使います。**
`infra/bicep/` は廃止し、`infra/terraform/` に置き換えます。

あわせて以下を決めます。

### state はローカルから始め、CI/CD のテーマで移行する

当面 state はローカルの `terraform.tfstate` に置きます。GitHub Actions から
apply するようになった時点で、Azure Storage の backend に移行します
（`versions.tf` にコメントアウト済みのブロックを用意してあります）。

最初からリモート state にすると、**state 置き場の Storage アカウント自体を誰が
作るのか**という鶏と卵の問題を先に説明することになり、本筋がぼやけます。
「1人で手元から apply している限りローカルで足りる」「CI から apply した瞬間に
足りなくなる」という順序で体験する方が、リモート state の必要性が腹落ちします。

### リソース作成の経路を1つにする

`scripts/azure-setup.sh` は az CLI で直接リソースを作っていましたが、
**`terraform apply` のラッパーに置き換えます**。同じリソースを作る経路が2つあると、
片方が必ず腐り、「どちらが正か」の判断が毎回発生します。

スクリプトは引き続き、接続文字列を `.env` に書き込むところまで面倒を見ます。

### バージョンは範囲で制約し、実際の版はロックファイルで固定する

`versions.tf` にメジャーを跨がない制約（`~> 4.77`）を書き、
`.terraform.lock.hcl` をコミットします。プロバイダの更新は
`terraform init -upgrade` で意図的に行います。

## Options Considered

### Option A: Terraform / azurerm（採用）

| Dimension | Assessment |
|---|---|
| 学習価値 | High |
| Azure リソースの網羅性 | High（ただし新機能への追随は ARM/Bicep より遅れることがある） |
| Azure 以外への適用 | High |
| state 管理 | 必要（利点でも負債でもある） |

**Pros:** plan / state / drift 検知といった、**IaC を運用する上での論点が全部出てくる**。
Azure 以外（GitHub、Entra ID、SaaS）も同じ書き方で扱えるため、CI/CD のテーマを
Azure の外まで広げられる。実務での採用例が多く、情報量も多い。
**Cons:** state という管理対象が増える。プロバイダが Azure の新機能に追随するまで
タイムラグがあり、プレビュー機能は `azapi` プロバイダの併用が必要になることがある。
ライセンスが BSL（後述）。

### Option B: Bicep のまま続ける

| Dimension | Assessment |
|---|---|
| 学習価値 | Medium |
| Azure リソースの網羅性 | 最高（ARM と同時に新機能へ追随する） |
| Azure 以外への適用 | なし |
| state 管理 | 不要（Azure 側が実体を持つ） |

**Pros:** state が要らない。Azure の新機能に即日追随する。`az deployment what-if` が
標準で使える。Microsoft 純正なのでサポート経路が明確。移行コストゼロ。
**Cons:** **Azure 専用**。IaC の運用論点のうち「state をどう共有するか」「drift を
どう扱うか」が、Azure のデプロイ履歴に隠れて表に出てこない。学習題材としては
物足りない面がある。

### Option C: OpenTofu

**Pros:** Terraform のフォークで、HCL と azurerm プロバイダはそのまま使える。
ライセンスが MPL 2.0 のままなので、**BSL の制約を気にしなくてよい**。
**Cons:** 実務での採用がまだ Terraform ほど多くない。学習の目的が「実務で使うものを
身につける」ことであれば、まず Terraform を触る方が直接的。

> **BSL について** — Terraform は 2023 年に MPL 2.0 から BSL に変更されました。
> 対象は「Terraform と競合する製品を提供する場合」であり、**自社のインフラを管理する
> 通常の利用は制限されません**。個人の学習も同様です。ただし、SIer として顧客に
> Terraform ベースのマネージドサービスを提供するといった形態では、
> ライセンスの確認が必要になる場合があります。この ADR ではその判断は扱いません。

### Option D: 両方維持して比較する

**Pros:** 同じリソースを2通りで書き、差異を学習材料にできる。
**Cons:** 変更のたびに両方直す必要があり、**片方が必ず腐ります**。腐った方を見た人が
混乱するので、比較したいなら「一度書いて比較した記録を ADR に残す」方が安上がりです。
本 ADR の Options がまさにその記録です。

## Trade-off Analysis

**A と B の差は「Azure 専用でよいか」に集約されます。** Roastery が扱う対象は
今のところ Azure だけなので、この観点だけなら B の方が素直です。実際、
Azure の新機能への追随速度と state 不要という点は、Bicep の明確な優位です。

判断を分けたのは**学習題材としての厚み**です。ADR-0003 で CI/CD をテーマに据えた以上、
「plan を PR にコメントする」「state を誰がどこで持つか」「手で変えられた設定を
どう検知するか」は扱いたい論点です。Bicep だとこのうち state と drift の話が
Azure 側に吸収されてしまい、表に出てきません。**扱いたい論点が出てくる方**を選びました。

**移行コストは今が最小**です。管理対象は2リソースだけで、Git 管理も始まっていません。
ACA と AKS を書いた後に迷うより、ここで決め切る方が安上がりです。

**C は将来の逃げ道として残しておけば十分**と判断しました。HCL も azurerm プロバイダも
共通なので、必要になった時点で切り替えられます。「切り替えられる」こと自体が
Terraform 系を選ぶ副次的な利点です。

## Consequences

**楽になること**

- plan の差分を PR にコメントする流れを作れる（ADR-0003 の Stage 3）
- 将来 GitHub リソースや Entra ID のアプリ登録も同じ書き方で管理できる
- リソース作成の経路が `terraform apply` の1つに集約される

**難しくなること**

- **state という管理対象が増える**。ローカルにある間は、失うと既存リソースを
  認識できなくなる
- `terraform.tfstate` には接続文字列が平文で入る。`.gitignore` の管理が
  これまで以上に重要になる（公開リポジトリなので特に / ADR-0004）
- プロバイダのバージョン追随という作業が定常的に発生する
- Azure のプレビュー機能を触るときは `azapi` プロバイダの併用を検討する必要がある

**あとで見直すこと**

- CI/CD のテーマで Azure Storage backend に移行する（`versions.tf` に準備済み）
- ACA / AKS を書く段階で、モジュール分割の粒度を決める（ADR-0003 の Action Item）
- 環境ごとの差分を workspace で分けるか、ディレクトリで分けるかを決める

## Action Items

1. [x] `infra/terraform/` を作成し、Bicep が管理していた範囲を移植する
2. [x] `infra/bicep/` を廃止する
3. [x] `scripts/azure-setup.sh` を `terraform apply` のラッパーに置き換える
4. [x] `.gitignore` に state と tfvars を追加する
5. [ ] 初回 `terraform apply` を実行し、`.terraform.lock.hcl` をコミットする
6. [ ] CI/CD のテーマで Azure Storage backend に移行する
