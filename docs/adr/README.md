# ADR — アーキテクチャ意思決定記録

このディレクトリには、Roastery の構成に関する意思決定とその理由を残します。

## なぜ ADR を書くか

テーマを積み上げていくリポジトリでは、数か月後の自分が「なぜこうなっているのか」を
忘れます。決定そのものより、**そのとき何を天秤にかけたか**が残っていないと、
判断を再現できません。

ADR を書くこと自体が「設計 (デザインパターン)」のテーマの一部でもあります。

## 一覧

| # | タイトル | 状態 | 概要 |
|---|---|---|---|
| [0001](0001-repository-layout.md) | リポジトリ構成 | Accepted | モノレポ1つ。`apps` / `platform` / `infra` の役割分担 |
| [0002](0002-branching-and-tagging.md) | ブランチ戦略 | Accepted | GitHub Flow。長期ブランチは `main` のみ、必須承認数は 0 |
| [0003](0003-deployment-strategy.md) | デプロイ戦略 | Accepted | ACA から始めて AKS へ。OIDC 認証、レジストリは GHCR から |
| [0004](0004-repository-visibility.md) | リポジトリの公開範囲 | Accepted | 個人アカウントの Public。Free プランのまま全機能が使える |
| [0005](0005-iac-tool.md) | IaC ツール | Accepted | Bicep から Terraform / azurerm へ。state は当面ローカル |
| [0006](0006-resource-naming.md) | リソース命名規則 | Accepted | CAF 準拠。`<種別>-<ワークロード>-<環境>-<リージョン>-<連番>` |

## 書き方

- 1 ファイル 1 決定。番号は連番で、欠番を作らない
- **却下した案とその理由を必ず書く**。ADR の価値の大半はここにあります
- 決定を覆すときは既存 ADR を書き換えず、新しい ADR を追加して
  古い方の状態を `Superseded by ADR-NNNN` にする
- テンプレートは既存の ADR をコピーして使ってください
