#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Application Insights を作り、.env に接続文字列を書き込むまでを行う。
#
#   az login
#   ./scripts/azure-setup.sh [環境] [リージョン]
#
#   環境      : prd / stg / dev   (既定 dev)
#   リージョン: japaneast / japanwest   (既定 japaneast)
#
# 作られるリソース名は命名規則から決まる (docs/adr/0006-resource-naming.md)。
#   rg-roastery-dev-je-001 / log-roastery-dev-je-001 / appi-roastery-dev-je-001
#
# 実体は infra/terraform に対する terraform apply。
# リソースを作る経路をここ1つに集約している (az CLI で直接作る版は廃止した)。
# 変数は infra/terraform/terraform.tfvars でも指定できる。
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

TF_DIR="infra/terraform"
ENVIRONMENT="${1:-dev}"
LOCATION="${2:-japaneast}"

case "$ENVIRONMENT" in
  prd|stg|dev) ;;
  *) echo "環境は prd / stg / dev のいずれかを指定してください (指定値: $ENVIRONMENT)" >&2; exit 1 ;;
esac

command -v terraform >/dev/null 2>&1 || {
  echo "terraform コマンドが見つかりません。docs/wsl2-setup.md の「6. Azure を使う場合のツール」を参照してください" >&2
  exit 1
}
command -v az >/dev/null 2>&1 || { echo "az コマンドが見つかりません" >&2; exit 1; }

# --- サブスクリプションの確認 ------------------------------------------------
SUB_ID=$(az account show --query id -o tsv 2>/dev/null) || {
  echo "Azure にログインしていません。先に 'az login' を実行してください" >&2
  exit 1
}
SUB_NAME=$(az account show --query name -o tsv 2>/dev/null)
export ARM_SUBSCRIPTION_ID="$SUB_ID"

echo "==> 対象サブスクリプション: ${SUB_NAME} (${SUB_ID})"
echo "==> 環境: ${ENVIRONMENT} / リージョン: ${LOCATION}"
echo

# --- terraform ---------------------------------------------------------------
echo "==> terraform init"
terraform -chdir="$TF_DIR" init -input=false || exit 1

echo
echo "==> terraform apply"
echo "    変更内容が表示されます。確認して 'yes' を入力してください。"
echo
terraform -chdir="$TF_DIR" apply \
  -var="environment=${ENVIRONMENT}" \
  -var="location=${LOCATION}" || exit 1

# --- .env へ接続文字列を書き込む ---------------------------------------------
CONN=$(terraform -chdir="$TF_DIR" output -raw connection_string 2>/dev/null)
if [ -z "$CONN" ]; then
  echo "接続文字列を取得できませんでした。terraform output を確認してください" >&2
  exit 1
fi

echo
echo "==> .env を更新"
[ -f .env ] || cp .env.example .env
# 接続文字列には ; = / が含まれる。sed のエスケープを避けるため、
# 該当行を除いてから追記する方式にしている。
TMP=$(mktemp)
grep -v '^APPLICATIONINSIGHTS_CONNECTION_STRING=' .env > "$TMP"
printf 'APPLICATIONINSIGHTS_CONNECTION_STRING=%s\n' "$CONN" >> "$TMP"
mv "$TMP" .env

RG=$(terraform -chdir="$TF_DIR" output -raw resource_group_name 2>/dev/null)
APPI=$(terraform -chdir="$TF_DIR" output -raw app_insights_name 2>/dev/null)
LAW=$(terraform -chdir="$TF_DIR" output -raw workspace_name 2>/dev/null)

cat <<EOF

完了しました。

  リソースグループ     : ${RG}
  Log Analytics        : ${LAW}
  Application Insights : ${APPI}
  接続文字列は .env に書き込み済みです。

Collector の送信先を Azure に切り替えて起動します:

  sed -i 's|^COLLECTOR_CONFIG=.*|COLLECTOR_CONFIG=config.yaml|' .env
  docker compose up -d --build
  ./scripts/load.sh

片付け (課金を止める):

  terraform -chdir=${TF_DIR} destroy

EOF
