# ===========================================================================
# Roastery 用の Azure リソース。
#   - Log Analytics ワークスペース
#   - Application Insights (ワークスペースベース)
#
# アプリそのものはローカルの docker compose で動かし、テレメトリだけを
# Azure に送る構成を前提にしている。「アプリの実行場所と可観測性バックエンドは
# 分離できる」という OpenTelemetry の性質をそのまま利用した形。
# ===========================================================================

locals {
  # リージョンの略号。値の妥当性は var.location の validation で担保しているため、
  # ここに来る時点で必ず引ける。
  location_abbr = lookup(var.location_abbreviations, var.location, "")

  # <種別>-<ワークロード>-<環境>-<リージョン>-<連番>   (Azure CAF 準拠)
  #   rg-roastery-dev-je-001
  #   log-roastery-dev-je-001
  #   appi-roastery-dev-je-001
  name_suffix = "${var.workload}-${var.environment}-${local.location_abbr}-${var.instance}"

  resource_group_name = coalesce(var.resource_group_name, "rg-${local.name_suffix}")

  tags = merge({
    project     = var.workload
    environment = var.environment
    managed_by  = "terraform"
  }, var.tags)
}

resource "azurerm_resource_group" "this" {
  name     = local.resource_group_name
  location = var.location
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${local.name_suffix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name

  sku               = "PerGB2018"
  retention_in_days = var.retention_in_days
  daily_quota_gb    = var.daily_quota_gb

  tags = local.tags

  # Log Analytics は削除後 14 日間、同名での再作成ができない (論理削除)。
  # 作り直しが重なって衝突したときは var.instance を 002 に上げる。
  lifecycle {
    precondition {
      condition     = local.location_abbr != ""
      error_message = "location '${var.location}' の略号が location_abbreviations に定義されていません。"
    }
  }
}

resource "azurerm_application_insights" "this" {
  name                = "appi-${local.name_suffix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name

  application_type = "web"
  workspace_id     = azurerm_log_analytics_workspace.this.id

  # 接続文字列だけで取り込めるようにしている (手順を単純にするため)。
  # 本番では Entra ID 認証 (local_authentication_disabled = true) を検討する。
  local_authentication_disabled = false
  internet_ingestion_enabled    = true
  internet_query_enabled        = true

  tags = local.tags
}
