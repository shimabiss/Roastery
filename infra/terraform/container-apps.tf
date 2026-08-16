# ===========================================================================
# Azure Container Apps へのデプロイ (ADR-0003 Stage 2)
#
# 構成の考え方
# ------------
# docker compose の構成をそのまま ACA に写している。サービス境界を変えていないので、
# **「どこで動かすか」を変えてもアプリのコードは1行も変わらない**ことが示せる。
# 接続先はすべて環境変数で外から与える設計にしてあったことが、ここで効いている。
#
# データストアの扱い (コスト優先の判断)
# ------------------------------------
# postgres / redis を **マネージドサービスではなく Container App** で動かしている。
#
#   マネージド (Flexible Server + Azure Cache for Redis) … 月 ¥4,000〜5,000
#   コンテナアプリ                                        … 月 数百円
#
# ADR-0003 の「Stage 3 までは月額ほぼゼロ」を守るための選択で、
# **設計として正しいからではない。** 引き換えに以下を受け入れている。
#
#   - リビジョンが入れ替わるとデータが消える (永続ボリュームを付けていない)
#   - レプリカを増やせない (max_replicas = 1 で固定)
#   - バックアップ・PITR・自動フェイルオーバーが無い
#
# **本番を名乗るなら、ここは必ずマネージドに置き換える。**
# 置き換えの判断材料として、この注記ごと教材にする。
#
# なぜ init.sql が要らないか
# --------------------------
# postgres の `docker-entrypoint-initdb.d` は compose のボリュームマウント前提で、
# ACA には持ち込めない。ところが各サービスが起動時に冪等な DDL を流す
# (`app/schema.py`) ようにしてあるため、**そのまま動く。**
# 「新規構築」と「既存環境の更新」を分けておいた判断が、実行基盤を変えたときに効いた。
# ===========================================================================

resource "azurerm_container_app_environment" "this" {
  count = var.deploy_container_apps ? 1 : 0

  name                = "cae-${local.name_suffix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name

  # コンテナの stdout/stderr が Log Analytics の ContainerAppConsoleLogs_CL に入る。
  # OpenTelemetry で送っているトレース・メトリクスとは **別系統** である点に注意。
  # 「アプリが自分で送るテレメトリ」と「基盤が拾うログ」は取得経路が違う。
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  tags = local.tags
}

# ---------------------------------------------------------------------------
# postgres の資格情報。**tfvars にも state 外にも書かない。**
# state には入るため、backend を Azure Storage に移す際は必ず暗号化と
# アクセス制御を掛ける (versions.tf の backend ブロック参照)。
# ---------------------------------------------------------------------------
resource "random_password" "postgres" {
  count = var.deploy_container_apps ? 1 : 0

  length  = 32
  special = false # 接続文字列に URL エンコードが要る文字を避ける
}

locals {
  # 内部 HTTP ingress の宛先。
  #   ACA は同一環境内ならアプリ名だけでも解決するが、FQDN のほうが曖昧さが無い。
  #   ポートは ingress の 80 になるため、compose の :8000 は付けない。
  internal_host = try("internal.${azurerm_container_app_environment.this[0].default_domain}", "")

  order_api_url     = "http://order-api.${local.internal_host}"
  inventory_api_url = "http://inventory-api.${local.internal_host}"
  payment_api_url   = "http://payment-api.${local.internal_host}"
  member_api_url    = "http://member-api.${local.internal_host}"
  external_stub_url = "http://external-stub.${local.internal_host}"

  # 内部 TCP ingress は「アプリ名:公開ポート」で解決する。
  # **compose と同じ文字列になる**ので、DATABASE_URL などは書き換え不要。
  database_url  = "postgresql://demo:${try(random_password.postgres[0].result, "")}@postgres:5432/demo"
  otlp_endpoint = "http://otel-collector:4317"
  # GHCR に push するイメージ。名前はサービス名と一致させてある
  all_services = [
    "frontend", "order-api", "inventory-api", "payment-api",
    "member-api", "external-stub", "otel-collector",
  ]
  container_image = {
    for k in local.all_services :
    k => "${var.image_registry}/${var.image_repository}/${k}:${var.image_tag}"
  }

  # 全サービス共通の OpenTelemetry 設定。compose の x-otel-env と同じ内容。
  # **設定をアプリのコードではなく環境変数に置いた**ので、そのまま移せている。
  otel_env = {
    OTEL_EXPORTER_OTLP_ENDPOINT                       = local.otlp_endpoint
    OTEL_EXPORTER_OTLP_PROTOCOL                       = "grpc"
    OTEL_TRACES_EXPORTER                              = "otlp"
    OTEL_METRICS_EXPORTER                             = "otlp"
    OTEL_LOGS_EXPORTER                                = "otlp"
    OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE = "DELTA"
    OTEL_METRIC_EXPORT_INTERVAL                       = "15000"
    OTEL_TRACES_SAMPLER                               = "parentbased_always_on"
    OTEL_RESOURCE_ATTRIBUTES                          = "service.version=${var.image_tag},deployment.environment=${var.environment}"
  }

  python_otel_env = {
    OTEL_PYTHON_LOG_CORRELATION           = "true"
    OTEL_PYTHON_LOG_LEVEL                 = "info"
    OTEL_PYTHON_EXCLUDED_URLS             = "healthz"
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS = "fastapi"
  }
}

# ===========================================================================
# データストア (TCP ingress / 単一レプリカ)
# ===========================================================================
resource "azurerm_container_app" "postgres" {
  count = var.deploy_container_apps ? 1 : 0

  name                         = "postgres"
  container_app_environment_id = azurerm_container_app_environment.this[0].id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"
  tags                         = local.tags

  secret {
    name  = "postgres-password"
    value = random_password.postgres[0].result
  }

  ingress {
    external_enabled = false # **外部 TCP ingress は VNET が要る。内部なら不要**
    transport        = "tcp"
    target_port      = 5432
    exposed_port     = 5432 # 呼び出し側は postgres:5432 で解決する

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    # **単一レプリカで固定する。** 複数に増えると各レプリカが別のデータを持ち、
    # 「注文が見えたり見えなかったりする」という最悪の壊れ方をする。
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "postgres"
      image  = "postgres:17-alpine"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "POSTGRES_USER"
        value = "demo"
      }
      env {
        name        = "POSTGRES_PASSWORD"
        secret_name = "postgres-password"
      }
      env {
        name  = "POSTGRES_DB"
        value = "demo"
      }
      # 初期化 SQL は流さない。各サービスが起動時に冪等な DDL を適用する
      # (apps/*/app/schema.py)。ボリュームマウントに依存しない構成にしてある。
    }
  }
}

resource "azurerm_container_app" "redis" {
  count = var.deploy_container_apps ? 1 : 0

  name                         = "redis"
  container_app_environment_id = azurerm_container_app_environment.this[0].id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"
  tags                         = local.tags

  ingress {
    external_enabled = false
    transport        = "tcp"
    target_port      = 6379
    exposed_port     = 6379

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "redis"
      image  = "redis:7-alpine"
      cpu    = 0.25
      memory = "0.5Gi"
    }
  }
}

# ===========================================================================
# OpenTelemetry Collector
#
# 設定ファイルをイメージに焼き込んでいる (platform/otel-collector/Dockerfile)。
# compose ではボリュームマウントだったが、ACA にはバインドマウントが無い。
# **設定の実体は platform/otel-collector/ の1箇所のまま**で、
# 参照のしかたが実行基盤ごとに違うだけ、という形を保っている (ADR-0001)。
# ===========================================================================
resource "azurerm_container_app" "otel_collector" {
  count = var.deploy_container_apps ? 1 : 0

  name                         = "otel-collector"
  container_app_environment_id = azurerm_container_app_environment.this[0].id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type = "SystemAssigned"
  }

  secret {
    name  = "appinsights-connection-string"
    value = azurerm_application_insights.this.connection_string
  }

  ingress {
    external_enabled = false
    # OTLP gRPC をそのまま通したいので TCP passthrough にする。
    # http2 ingress でも動くが、TCP のほうが「gRPC が素通しである」ことが明確。
    transport    = "tcp"
    target_port  = 4317
    exposed_port = 4317

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    # **0 にしない。** テレメトリの送り先が落ちていると、
    # アプリ側のエクスポータがエラーを吐き続けてノイズになる。
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "otel-collector"
      image  = local.container_image["otel-collector"]
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        secret_name = "appinsights-connection-string"
      }
      env {
        name  = "DEPLOY_ENV"
        value = var.environment
      }
    }
  }
}

# ===========================================================================
# アプリケーション (HTTP ingress)
#
# 内部向けは min_replicas = 0 でスケールインさせる (var.min_replicas)。
# アイドル時のコストがほぼゼロになる代わりに、**最初の1リクエストが遅い**。
# 講義でデモする直前に1回叩いて温めておくとよい。
# ===========================================================================
locals {
  # 各アプリ固有の環境変数。共通分は locals.otel_env / python_otel_env で足す。
  app_env = {
    inventory-api = {
      REDIS_HOST    = "redis"
      CHAOS_SLOW_MS = "0"
    }
    external-stub = {
      CHAOS_LATENCY_MS       = "0"
      CHAOS_ERROR_RATE       = "0"
      CHAOS_NO_RESPONSE_RATE = "0"
    }
    payment-api = {
      EXTERNAL_STUB_URL = local.external_stub_url
      GATEWAY_TIMEOUT_S = "5"
    }
    member-api = {
      REDIS_HOST        = "redis"
      EXTERNAL_STUB_URL = local.external_stub_url
    }
    order-api = {
      INVENTORY_API_URL       = local.inventory_api_url
      PAYMENT_API_URL         = local.payment_api_url
      MEMBER_API_URL          = local.member_api_url
      EXTERNAL_STUB_URL       = local.external_stub_url
      FREE_SHIPPING_THRESHOLD = "5000"
    }
  }

  # DATABASE_URL を必要とするサービス。シークレット参照にするため別扱いにする。
  # **set ではなく list にしてある。** contains() は list を取る関数で、
  # set を渡すと型エラーになる。for_each 用 (internal_apps) は逆に set が要る。
  needs_database = ["order-api", "payment-api", "member-api"]

  internal_apps = toset([
    "inventory-api", "external-stub", "payment-api", "member-api", "order-api",
  ])
}

resource "azurerm_container_app" "internal" {
  for_each = var.deploy_container_apps ? local.internal_apps : toset([])

  name                         = each.key
  container_app_environment_id = azurerm_container_app_environment.this[0].id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type = "SystemAssigned"
  }

  dynamic "secret" {
    for_each = contains(local.needs_database, each.key) ? [1] : []
    content {
      name  = "database-url"
      value = local.database_url
    }
  }

  ingress {
    external_enabled = false
    transport        = "http"
    target_port      = 8000
    # 内部通信なので TLS を要求しない。証明書の管理を持ち込まないための割り切り。
    # **外部公開する frontend では false にしてある。**
    allow_insecure_connections = true

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name   = each.key
      image  = local.container_image[each.key]
      cpu    = var.container_cpu
      memory = var.container_memory

      dynamic "env" {
        for_each = merge(
          local.otel_env,
          local.python_otel_env,
          { OTEL_SERVICE_NAME = each.key },
          lookup(local.app_env, each.key, {}),
        )
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = contains(local.needs_database, each.key) ? [1] : []
        content {
          name        = "DATABASE_URL"
          secret_name = "database-url"
        }
      }

      dynamic "env" {
        for_each = each.key == "member-api" ? [1] : []
        content {
          name  = "SITE_URL"
          value = "https://${azurerm_container_app.frontend[0].ingress[0].fqdn}"
        }
      }

      # /healthz が 200 を返すまでトラフィックを流さない。
      # **これが無いと、DB 接続待ちの間のリクエストが 502 になる。**
      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = 8000
        path                    = "/healthz"
        initial_delay           = 20
        failure_count_threshold = 5
      }
    }
  }

  depends_on = [
    azurerm_container_app.postgres,
    azurerm_container_app.redis,
    azurerm_container_app.otel_collector,
  ]
}

# ---------------------------------------------------------------------------
# 公開サイト。**唯一の外部公開エンドポイント。**
#
# /ops (運用画面) は認証が無いため、本来はここで分離するか認証を掛ける必要がある。
# docs/security-checklist.md に「外部公開しない」と書いてあるのはこの点で、
# **現状は同じ Container App に同居しているので守れていない。**
# 既知の未対応として明記しておく。
# ---------------------------------------------------------------------------
resource "azurerm_container_app" "frontend" {
  count = var.deploy_container_apps ? 1 : 0

  name                         = "frontend"
  container_app_environment_id = azurerm_container_app_environment.this[0].id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type = "SystemAssigned"
  }

  ingress {
    external_enabled = true
    transport        = "auto"
    target_port      = 3000
    # 外部公開なので平文を許さない。ACA が自動で TLS を終端する
    allow_insecure_connections = false

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name   = "frontend"
      image  = local.container_image["frontend"]
      cpu    = var.container_cpu
      memory = var.container_memory

      dynamic "env" {
        for_each = merge(local.otel_env, {
          OTEL_SERVICE_NAME                   = "frontend"
          OTEL_NODE_RESOURCE_DETECTORS        = "env,host,os,container"
          OTEL_NODE_DISABLED_INSTRUMENTATIONS = "express,router,connect,fs,dns,net"
          ORDER_API_URL                       = local.order_api_url
          INVENTORY_API_URL                   = local.inventory_api_url
          PAYMENT_API_URL                     = local.payment_api_url
          MEMBER_API_URL                      = local.member_api_url
          EXTERNAL_STUB_URL                   = local.external_stub_url
          PORT                                = "3000"
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      readiness_probe {
        transport = "HTTP"
        port      = 3000
        path      = "/healthz"
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = 3000
        path                    = "/healthz"
        initial_delay           = 15
        failure_count_threshold = 5
      }
    }
  }
}
