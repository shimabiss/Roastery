output "connection_string" {
  description = "Collector に渡す接続文字列。.env の APPLICATIONINSIGHTS_CONNECTION_STRING に設定する"
  value       = azurerm_application_insights.this.connection_string
  sensitive   = true
}

output "instrumentation_key" {
  description = "インストルメンテーションキー (接続文字列にも含まれる)"
  value       = azurerm_application_insights.this.instrumentation_key
  sensitive   = true
}

output "resource_group_name" {
  description = "作成したリソースグループ名"
  value       = azurerm_resource_group.this.name
}

output "app_insights_name" {
  description = "作成した Application Insights 名"
  value       = azurerm_application_insights.this.name
}

output "workspace_name" {
  description = "作成した Log Analytics ワークスペース名"
  value       = azurerm_log_analytics_workspace.this.name
}

output "environment" {
  description = "この state が管理している環境"
  value       = var.environment
}

output "portal_url" {
  description = "Application Insights のポータル URL"
  value       = "https://portal.azure.com/#@/resource${azurerm_application_insights.this.id}/overview"
}

# ---------------------------------------------------------------------------
# Container Apps
# ---------------------------------------------------------------------------

output "site_url" {
  description = "公開サイトの URL。deploy_container_apps = false のときは null"
  value       = try("https://${azurerm_container_app.frontend[0].ingress[0].fqdn}", null)
}

output "ops_url" {
  description = <<-EOT
    運用画面の URL。**認証が掛かっていない。**
    現状は公開サイトと同じ Container App に同居しているため、
    URL を知っていれば誰でも障害注入と出荷操作ができる。
    docs/security-checklist.md の未対応項目。
  EOT
  value = try("https://${azurerm_container_app.frontend[0].ingress[0].fqdn}/ops", null)
}

output "container_app_environment_name" {
  description = "Container Apps 環境名"
  value       = try(azurerm_container_app_environment.this[0].name, null)
}

output "container_images" {
  description = "各サービスがデプロイされているイメージ。切り戻し時の確認に使う"
  value       = var.deploy_container_apps ? local.container_image : {}
}

output "internal_endpoints" {
  description = "サービス間の内部エンドポイント。障害切り分けのときに参照する"
  value = var.deploy_container_apps ? {
    order_api     = local.order_api_url
    inventory_api = local.inventory_api_url
    payment_api   = local.payment_api_url
    member_api    = local.member_api_url
    external_stub = local.external_stub_url
    postgres      = "postgres:5432"
    redis         = "redis:6379"
    otlp          = local.otlp_endpoint
  } : {}
}
