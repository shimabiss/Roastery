provider "azurerm" {
  features {}

  # azurerm 4.x では subscription_id が必須。
  # 変数で渡さなければ環境変数 ARM_SUBSCRIPTION_ID にフォールバックする。
  #   export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)
  subscription_id = var.subscription_id
}
