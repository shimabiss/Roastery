# ---------------------------------------------------------------------------
# Terraform 本体とプロバイダのバージョン制約。
#
# バージョンは「再現したいものは固定し、判定するものは範囲で書く」。
# ここは前者なので、メジャーを跨がない範囲に閉じておく。
# 実際に使われた版は .terraform.lock.hcl に記録され、これはコミットする。
# ---------------------------------------------------------------------------
terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.77"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # -------------------------------------------------------------------------
  # state は Azure Storage に置く (ADR-0003 Stage 2)。
  #
  # **CI から apply するなら、これは必須である。**
  # ローカル state のままだと、GitHub Actions は毎回まっさらな state で始まり、
  # 既に存在するリソースを「新規作成」しようとして失敗する。
  # 「手元では動くのに CI では壊れる」の典型例。
  #
  # 値をここに書かず **partial configuration** にしているのは、
  # ストレージアカウント名が環境ごとに違い、かつ Public リポジトリに
  # 置く情報を減らしたいため。init 時に外から与える。
  #
  #   ローカル : terraform init -backend-config=backend.hcl
  #   CI       : terraform init -backend-config="storage_account_name=..." ...
  #
  # ストレージアカウントは Terraform では作らない。**鶏と卵になる**ため、
  # scripts/tf-bootstrap.sh で一度だけ az CLI から作る。
  # -------------------------------------------------------------------------
  backend "azurerm" {
    # アクセスキーではなく Entra ID 認証を使う。
    # アカウントキーは長期シークレットなので、置かない・配らない・使わない。
    use_azuread_auth = true
  }
}
