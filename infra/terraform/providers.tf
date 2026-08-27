provider "azurerm" {
  features {
    # ---------------------------------------------------------------------
    # **リソースグループを消すとき、中に残っているものごと消す。**
    #
    # 既定 (true) では「Terraform が知らないリソースがグループ内に残っていたら
    # 削除を中止する」という安全弁が働く。ARM テンプレートなど別の手段で
    # 作られたものを巻き添えにしないための配慮で、考え方としては正しい。
    #
    # ただしこの構成では **Azure 自身が勝手に作るリソース**に引っかかる。
    # Application Insights を作ると、Azure が
    #   microsoft.insights/actiongroups/Application Insights Smart Detection
    # を同じリソースグループに自動生成する。これは Terraform の管理外なので、
    # **destroy がリソースグループの手前で必ず止まる。**
    #
    # このリソースグループは Terraform が作り、Terraform 以外は何も置かない
    # 前提なので、false にして中身ごと消す。
    # **共用のリソースグループでは絶対に false にしないこと。**
    # 安全弁を外す判断は「そのグループを誰が所有しているか」で決まる。
    # ---------------------------------------------------------------------
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }

  # azurerm 4.x では subscription_id が必須。
  # 変数で渡さなければ環境変数 ARM_SUBSCRIPTION_ID にフォールバックする。
  #   export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)
  subscription_id = var.subscription_id
}
