variable "subscription_id" {
  description = "対象の Azure サブスクリプション ID。未指定なら ARM_SUBSCRIPTION_ID を使う"
  type        = string
  default     = null
}

# ---------------------------------------------------------------------------
# 命名規則を構成する要素
#   <種別>-<ワークロード>-<環境>-<リージョン>-<連番>
#   例: log-roastery-dev-je-001
# 詳細は docs/adr/0006-resource-naming.md
# ---------------------------------------------------------------------------

variable "workload" {
  description = "ワークロード名。リソース名の第2要素になる"
  type        = string
  default     = "roastery"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.workload))
    error_message = "workload は英小文字で始まる 2〜21 文字の英小文字・数字・ハイフンで指定してください。"
  }
}

variable "environment" {
  description = "環境識別子。prd = 本番 / stg = ステージング / dev = 開発"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["prd", "stg", "dev"], var.environment)
    error_message = "environment は prd / stg / dev のいずれかで指定してください。"
  }
}

variable "location_abbreviations" {
  description = "Azure リージョン名 → 命名に使う略号。リージョンを増やすときはここに足す"
  type        = map(string)
  default = {
    japaneast = "je"
    japanwest = "jw"
  }
}

variable "location" {
  description = "リソースのリージョン。location_abbreviations に定義された値のみ指定できる"
  type        = string
  default     = "japaneast"

  validation {
    # Terraform 1.9 以降、validation から他の変数を参照できる。
    # 略号の定義とリージョンの許可リストを1箇所にまとめるためにこの書き方をしている。
    condition     = contains(keys(var.location_abbreviations), var.location)
    error_message = "location は location_abbreviations に定義されたリージョンのみ指定できます。増やす場合は location_abbreviations に略号を追加してください。"
  }
}

variable "instance" {
  description = "同一環境・同一リージョンに同種のリソースを複数作る場合の連番"
  type        = string
  default     = "001"

  validation {
    condition     = can(regex("^[0-9]{3}$", var.instance))
    error_message = "instance は 3 桁の数字（例: 001）で指定してください。"
  }
}

variable "resource_group_name" {
  description = "リソースグループ名を明示指定する場合に使う。null なら命名規則から生成する"
  type        = string
  default     = null
}

# ---------------------------------------------------------------------------
# リソースの設定
# ---------------------------------------------------------------------------

variable "retention_in_days" {
  description = "ログの保持日数。学習用途なので既定は最小の 30 日"
  type        = number
  default     = 30

  validation {
    condition     = var.retention_in_days >= 30 && var.retention_in_days <= 730
    error_message = "retention_in_days は 30〜730 の範囲で指定してください。"
  }
}

variable "daily_quota_gb" {
  description = "1日あたりの取り込み上限 (GB)。想定外の課金を防ぐストッパー。-1 で無制限"
  type        = number
  default     = 1
}

variable "tags" {
  description = "全リソースに追加で付与するタグ"
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# Azure Container Apps (ADR-0003 Stage 2)
# ---------------------------------------------------------------------------

variable "deploy_container_apps" {
  description = <<-EOT
    Container Apps 一式をデプロイするかどうか。

    false にすると Log Analytics と Application Insights だけが残る。
    「ローカルの docker compose で動かし、テレメトリだけ Azure に送る」
    という当初の構成に戻せるようにしてある。

    **アイドル時に課金が発生するのは postgres / redis / otel-collector の3つ**
    （min_replicas = 1 のため）。使わない期間はここを false にして
    `terraform apply` すると止められる。
  EOT
  type        = bool
  default     = true
}

variable "image_registry" {
  description = "コンテナレジストリのホスト。ADR-0003 により当面は GHCR を使う"
  type        = string
  default     = "ghcr.io"
}

variable "image_repository" {
  description = <<-EOT
    イメージのリポジトリ部分。GHCR なら "<GitHubユーザー名>/roastery"。
    最終的なイメージ名は <registry>/<repository>/<サービス名>:<tag> になる。
      例: ghcr.io/your-name/roastery/order-api:sha-abc1234
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._/-]*$", var.image_repository))
    error_message = "image_repository は英小文字・数字・ . _ - / のみで指定してください（GHCR の制約）。"
  }
}

variable "image_tag" {
  description = <<-EOT
    デプロイするイメージのタグ。

    **latest を既定にしていない。** latest だと「いま何が動いているか」が
    state からも Azure からも分からなくなり、切り戻しもできない。
    CI からはコミット SHA を渡す（例: sha-abc1234）。
  EOT
  type        = string
  default     = "main"
}

variable "min_replicas" {
  description = <<-EOT
    アプリの最小レプリカ数。0 にするとアイドル時のコストがほぼゼロになる代わりに、
    **最初の1リクエストがコールドスタートで数秒かかる。**
    デモの直前に一度叩いて温めておくとよい。
  EOT
  type        = number
  default     = 0

  validation {
    condition     = var.min_replicas >= 0 && var.min_replicas <= 5
    error_message = "min_replicas は 0〜5 で指定してください。"
  }
}

variable "max_replicas" {
  description = "アプリの最大レプリカ数。学習用途なので小さく抑える"
  type        = number
  default     = 2

  validation {
    condition     = var.max_replicas >= 1 && var.max_replicas <= 10
    error_message = "max_replicas は 1〜10 で指定してください。"
  }
}

variable "container_cpu" {
  description = <<-EOT
    コンテナの vCPU。**メモリとの組み合わせが決まっている**ので勝手な値は入らない。
      0.25 -> 0.5Gi / 0.5 -> 1Gi / 0.75 -> 1.5Gi / 1 -> 2Gi / 1.25 -> 2.5Gi ...
  EOT
  type        = number
  default     = 0.25
}

variable "container_memory" {
  description = "コンテナのメモリ。container_cpu と対応した値にする"
  type        = string
  default     = "0.5Gi"

  validation {
    condition     = can(regex("^[0-9.]+Gi$", var.container_memory))
    error_message = "container_memory は \"0.5Gi\" のような形式で指定してください。"
  }
}
