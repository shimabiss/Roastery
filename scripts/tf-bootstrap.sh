#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Terraform の state 置き場と、GitHub Actions 用の OIDC 設定を一度だけ作る。
#
# なぜ Terraform で作らないのか
# ----------------------------
# **鶏と卵になるため。** state を置く場所を Terraform で作ると、
# その Terraform 自身の state をどこに置くのか、という問題が残る。
# 「一度だけ手で作り、以後は触らない」ものは Terraform の外に出すのが定石。
#
# 実行するもの
#   1. state 用のリソースグループ / ストレージアカウント / コンテナ
#   2. GitHub Actions 用の Entra ID アプリと **環境スコープのフェデレーション資格情報**
#   3. 必要なロール割り当て
#
# 使い方
#   ./scripts/tf-bootstrap.sh <GitHubユーザー名>/<リポジトリ名> [環境] [リージョン]
#     例: ./scripts/tf-bootstrap.sh your-name/Roastery dev japaneast
# ---------------------------------------------------------------------------
set -euo pipefail

REPO="${1:-}"
ENVIRONMENT="${2:-dev}"
LOCATION="${3:-japaneast}"
WORKLOAD="roastery"

if [ -z "$REPO" ]; then
  echo "使い方: $0 <owner>/<repo> [dev|stg|prd] [region]" >&2
  exit 1
fi
case "$ENVIRONMENT" in
  dev|stg|prd) ;;
  *) echo "環境は dev / stg / prd のいずれかで指定してください: $ENVIRONMENT" >&2; exit 1 ;;
esac

command -v az > /dev/null || { echo "az CLI が見つかりません" >&2; exit 1; }
az account show > /dev/null 2>&1 || { echo "az login を実行してください" >&2; exit 1; }

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"
STATE_RG="rg-${WORKLOAD}-tfstate"
CONTAINER="tfstate"
APP_NAME="gh-${WORKLOAD}-${ENVIRONMENT}"

echo "サブスクリプション : ${SUBSCRIPTION_ID}"
echo "リポジトリ         : ${REPO}"
echo "環境               : ${ENVIRONMENT}"
echo

# ---------------------------------------------------------------------------
# 1. state 置き場
# ---------------------------------------------------------------------------
echo "--- state 置き場を用意します ---"
az group create -n "$STATE_RG" -l "$LOCATION" -o none

# ---------------------------------------------------------------------------
# ストレージアカウント名は **グローバルで一意** かつ 3〜24 文字の英小文字と数字のみ。
# ハイフンも大文字も使えず、**24 文字という上限が思ったより近い。**
#
#   "st" + "roastery" + "tfstate" = 17 文字 → 乱数に使えるのは 7 文字しかない。
#
# そこで乱数部の長さを固定せず、**残り文字数から決める。**
# WORKLOAD を長くしても壊れないようにするため。
# ---------------------------------------------------------------------------
SA_PREFIX="$(printf 'st%stfstate' "$WORKLOAD" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9' | cut -c1-20)"
SA_RANDOM_LEN=$((24 - ${#SA_PREFIX}))
[ "$SA_RANDOM_LEN" -gt 8 ] && SA_RANDOM_LEN=8

# 既に作ってあればそれを使い、無ければ乱数付きで作る。
SA_NAME="$(az storage account list -g "$STATE_RG" \
  --query "[?starts_with(name,'${SA_PREFIX}')].name | [0]" -o tsv 2>/dev/null || true)"

if [ -z "$SA_NAME" ] || [ "$SA_NAME" = "null" ]; then
  SA_NAME="${SA_PREFIX}$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-"${SA_RANDOM_LEN}")"

  # **作る前に自分で検証する。** az のエラーは
  # 「なぜその名前になったか」がメッセージから読み取れない。
  if ! printf '%s' "$SA_NAME" | grep -Eq '^[a-z0-9]{3,24}$'; then
    echo "ストレージアカウント名が規則に合いません: ${SA_NAME} (${#SA_NAME} 文字)" >&2
    echo "3〜24 文字の英小文字と数字のみです。WORKLOAD に短い名前を指定してください。" >&2
    exit 1
  fi

  echo "ストレージアカウントを作成します: ${SA_NAME} (${#SA_NAME} 文字)"
  az storage account create \
    -n "$SA_NAME" -g "$STATE_RG" -l "$LOCATION" \
    --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --allow-blob-public-access false \
    --https-only true \
    -o none
  # 誤って消したときに戻せるようにする。state は消えると復旧手段が無い
  az storage account blob-service-properties update \
    --account-name "$SA_NAME" -g "$STATE_RG" \
    --enable-versioning true \
    --enable-delete-retention true --delete-retention-days 30 \
    -o none
else
  echo "既存のストレージアカウントを使います: ${SA_NAME}"
fi

# アカウントキーではなく Entra ID でコンテナを作る (--auth-mode login)
az storage container create \
  --name "$CONTAINER" --account-name "$SA_NAME" --auth-mode login -o none 2>/dev/null || true

# 自分自身に Blob のデータ権限を付ける。
# **「所有者だから読める」わけではない。** データ平面の権限は制御平面と別。
CURRENT_USER_ID="$(az ad signed-in-user show --query id -o tsv)"
SA_ID="$(az storage account show -n "$SA_NAME" -g "$STATE_RG" --query id -o tsv)"
az role assignment create \
  --assignee-object-id "$CURRENT_USER_ID" --assignee-principal-type User \
  --role "Storage Blob Data Contributor" --scope "$SA_ID" -o none 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. GitHub Actions 用の OIDC (長期シークレットを作らない)
# ---------------------------------------------------------------------------
echo
echo "--- GitHub Actions 用の OIDC を設定します ---"
APP_ID="$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [ -z "$APP_ID" ] || [ "$APP_ID" = "null" ]; then
  APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
  echo "アプリ登録を作成しました: ${APP_NAME} (${APP_ID})"
else
  echo "既存のアプリ登録を使います: ${APP_NAME} (${APP_ID})"
fi

az ad sp create --id "$APP_ID" -o none 2>/dev/null || true
SP_OBJECT_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"

# ---------------------------------------------------------------------------
# フェデレーション資格情報。**subject を環境スコープで固定する。**
#
#   repo:<owner>/<repo>:environment:<env>
#
# ここを repo:owner/repo:* のようなワイルドカードにすると、
# **任意のブランチ・任意の PR から Azure に入れてしまう。**
# Public リポジトリでは、それは「誰でも入れる」と同義になる。
# ---------------------------------------------------------------------------
# 資格情報を1件登録する（既にあれば何もしない）
add_federated_credential() {
  local subject="$1" name="$2"
  if az ad app federated-credential list --id "$APP_ID" \
       --query "[?subject=='${subject}'] | [0].name" -o tsv 2> /dev/null | grep -q .; then
    echo "  設定済み : ${subject}"
    return
  fi
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"${name}\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"${subject}\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }" -o none
  echo "  作成しました : ${subject}"
}

REGISTERED_SUBJECTS=()

# --- 名前ベースの subject（従来の形式）-------------------------------------
NAME_SUBJECT="repo:${REPO}:environment:${ENVIRONMENT}"
add_federated_credential "$NAME_SUBJECT" "${APP_NAME}-env"
REGISTERED_SUBJECTS+=("$NAME_SUBJECT")

# ---------------------------------------------------------------------------
# --- ID ベースの subject（immutable subject）--------------------------------
#
# **GitHub は 2026-07-15 以降に作成・改名・移管されたリポジトリについて、
# subject を数値 ID 入りの形式で発行する。**
#
#   repo:<owner>@<owner_id>/<repo>@<repo_id>:environment:<env>
#
# 名前ベースの subject しか登録していないと、認証が
#   AADSTS700213: No matching federated identity record found
# で落ちる。ID はリポジトリを改名しても変わらないため、**名前ベースより安全**
# （名前を手放した後に第三者が同名を取る、という経路を塞げる）。
#
# 両方登録しておけば、どちらの形式で来ても通る。
#
# ID は公開 API から引く。取れない環境では、エラー文に出ている値を
# 環境変数で渡してから再実行する:
#   OWNER_ID=34046622 REPO_ID=1336013213 ./scripts/tf-bootstrap.sh <owner>/<repo> dev
# ---------------------------------------------------------------------------
OWNER="${REPO%%/*}"
REPO_NAME="${REPO##*/}"
OWNER_ID="${OWNER_ID:-}"
REPO_ID="${REPO_ID:-}"

if [ -z "$OWNER_ID" ] || [ -z "$REPO_ID" ]; then
  REPO_JSON="$(curl -fsSL --max-time 20 \
    -H 'Accept: application/vnd.github+json' \
    -H 'User-Agent: roastery-bootstrap' \
    "https://api.github.com/repos/${REPO}" 2> /dev/null || true)"

  if [ -n "$REPO_JSON" ]; then
    if command -v python3 > /dev/null 2>&1; then
      OWNER_ID="${OWNER_ID:-$(printf '%s' "$REPO_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["owner"]["id"])' 2> /dev/null || true)}"
      REPO_ID="${REPO_ID:-$(printf '%s' "$REPO_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2> /dev/null || true)}"
    elif command -v jq > /dev/null 2>&1; then
      OWNER_ID="${OWNER_ID:-$(printf '%s' "$REPO_JSON" | jq -r '.owner.id')}"
      REPO_ID="${REPO_ID:-$(printf '%s' "$REPO_JSON" | jq -r '.id')}"
    fi
  fi
fi

if [ -n "$OWNER_ID" ] && [ -n "$REPO_ID" ]; then
  IMMUTABLE_SUBJECT="repo:${OWNER}@${OWNER_ID}/${REPO_NAME}@${REPO_ID}:environment:${ENVIRONMENT}"
  add_federated_credential "$IMMUTABLE_SUBJECT" "${APP_NAME}-env-immutable"
  REGISTERED_SUBJECTS+=("$IMMUTABLE_SUBJECT")
else
  echo "  ID ベースの資格情報は作成していません（GitHub API からリポジトリ ID を取得できませんでした）。"
  echo "  AADSTS700213 が出た場合は、エラー文の ID を OWNER_ID / REPO_ID で渡して再実行してください。"
fi

# ---------------------------------------------------------------------------
# 3. ロール割り当て
#
# 本来は必要なリソースグループだけに絞りたいが、Terraform が
# リソースグループ自体を作るため、サブスクリプション スコープが要る。
# **範囲を絞れないことを分かったうえで付ける**のが大事で、
# 「とりあえず Owner」にはしない (ロール割り当て権限まで渡してしまう)。
# ---------------------------------------------------------------------------
echo
echo "--- ロールを割り当てます ---"
az role assignment create \
  --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Contributor" --scope "/subscriptions/${SUBSCRIPTION_ID}" -o none 2>/dev/null || true
az role assignment create \
  --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope "$SA_ID" -o none 2>/dev/null || true

# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
BACKEND_FILE="infra/terraform/backend.hcl"
cat > "$BACKEND_FILE" <<EOF
resource_group_name  = "${STATE_RG}"
storage_account_name = "${SA_NAME}"
container_name       = "${CONTAINER}"
key                  = "${ENVIRONMENT}.terraform.tfstate"
EOF

cat <<EOF

===========================================================================
完了しました。

${BACKEND_FILE} を書き出しました。次を実行してください:

  cd infra/terraform
  terraform init -backend-config=backend.hcl

GitHub 側の設定 (Settings > Environments > ${ENVIRONMENT} > Variables):

  AZURE_CLIENT_ID        ${APP_ID}
  AZURE_TENANT_ID        ${TENANT_ID}
  AZURE_SUBSCRIPTION_ID  ${SUBSCRIPTION_ID}

いずれも秘密情報ではないので secrets ではなく variables で構いません。
**OIDC を使う限り、GitHub に置く長期シークレットはゼロになります。**

注意:
  - Environment 名 "${ENVIRONMENT}" は変えないでください。
    フェデレーション資格情報の subject を次で固定しているため、
    名前が違うと認証が通りません。
$(printf '      %s\n' "${REGISTERED_SUBJECTS[@]}")
  - 別環境 (stg / prd) を足すときは、このスクリプトを環境ごとに実行します。
===========================================================================
EOF
