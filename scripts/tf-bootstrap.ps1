#Requires -Version 5.1
# ---------------------------------------------------------------------------
# Terraform の state 置き場と、GitHub Actions 用の OIDC 設定を一度だけ作る。
# scripts/tf-bootstrap.sh の PowerShell 版で、**作られるものは完全に同じ**。
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
# 使い方（PowerShell）
#   az login
#   az account set --subscription "<サブスクリプション名またはID>"
#   .\scripts\tf-bootstrap.ps1 -Repo shimabiss/Roastery
#
#   -Environment dev|stg|prd  （既定 dev）
#   -Location    japaneast    （既定 japaneast）
#
# **何度実行しても同じ結果になる**ように書いてある（既にあるものは作り直さない）。
#
# 注意: このファイルは **UTF-8 BOM 付き** で保存してある。
#   Windows PowerShell 5.1 は BOM が無い .ps1 を ANSI として読むため、
#   BOM を外すと日本語のコメントと出力が文字化けする。
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[^/]+/[^/]+$')]
  [string]$Repo,

  [ValidateSet('dev', 'stg', 'prd')]
  [string]$Environment = 'dev',

  [string]$Location = 'japaneast'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Workload = 'roastery'

# ---------------------------------------------------------------------------
# az CLI は失敗しても PowerShell の例外にならない（終了コードを返すだけ）。
# **ここを見ないと、途中で失敗しても最後まで走って「完了しました」と出る。**
# ---------------------------------------------------------------------------
function Invoke-Az {
  param(
    [Parameter(Mandatory = $true)][string[]]$Arguments,
    [switch]$AllowFailure
  )
  $output = & az @Arguments 2>&1
  if ($LASTEXITCODE -ne 0) {
    if ($AllowFailure) { return $null }
    Write-Host ''
    Write-Host "az $($Arguments -join ' ') が失敗しました。" -ForegroundColor Red
    Write-Host ($output | Out-String)
    exit 1
  }
  return ($output | Out-String).Trim()
}

# BOM 無しの UTF-8 で書く。
#   - backend.hcl に BOM が付くと terraform が読めないことがある
#   - az に渡す JSON に BOM が付くとパースエラーになる
# **Set-Content -Encoding utf8 は PowerShell 5.1 では BOM を付ける。** だから使わない。
function Write-Utf8NoBom {
  param([string]$Path, [string]$Content)
  [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

function Write-Section { param([string]$Text) Write-Host ''; Write-Host "--- $Text ---" -ForegroundColor Cyan }

# ---------------------------------------------------------------------------
# 前提の確認
# ---------------------------------------------------------------------------
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
  Write-Host 'az CLI が見つかりません。' -ForegroundColor Red
  Write-Host '  winget install -e --id Microsoft.AzureCLI'
  exit 1
}
if ($null -eq (Invoke-Az @('account', 'show', '-o', 'none') -AllowFailure)) {
  Write-Host 'az login を実行してください。' -ForegroundColor Red
  exit 1
}

$SubscriptionId = Invoke-Az @('account', 'show', '--query', 'id', '-o', 'tsv')
$TenantId = Invoke-Az @('account', 'show', '--query', 'tenantId', '-o', 'tsv')
$SubName = Invoke-Az @('account', 'show', '--query', 'name', '-o', 'tsv')

$StateRg = "rg-$Workload-tfstate"
$Container = 'tfstate'
$AppName = "gh-$Workload-$Environment"

Write-Host "サブスクリプション : $SubName ($SubscriptionId)"
Write-Host "テナント           : $TenantId"
Write-Host "リポジトリ         : $Repo"
Write-Host "環境               : $Environment"
Write-Host "リージョン         : $Location"

# **課金されるサブスクリプションを取り違えると気づきにくい。** 一度止める。
$answer = Read-Host 'このサブスクリプションに作成します。よろしいですか (y/N)'
if ($answer -notmatch '^[yY]') { Write-Host '中止しました。'; exit 0 }

# ---------------------------------------------------------------------------
# 1. state 置き場
# ---------------------------------------------------------------------------
Write-Section 'state 置き場を用意します'
Invoke-Az @('group', 'create', '-n', $StateRg, '-l', $Location, '-o', 'none') | Out-Null

# ストレージアカウント名は **グローバルで一意** かつ 3〜24 文字の英小文字と数字のみ。
# 既に作ってあればそれを使い、無ければ乱数付きで作る。
$SaName = Invoke-Az @(
  'storage', 'account', 'list', '-g', $StateRg,
  '--query', "[?starts_with(name,'st${Workload}tfstate')].name | [0]", '-o', 'tsv'
) -AllowFailure

if ([string]::IsNullOrWhiteSpace($SaName) -or $SaName -eq 'null') {
  $suffix = -join (1..4 | ForEach-Object { '{0:x2}' -f (Get-Random -Minimum 0 -Maximum 256) })
  $SaName = "st${Workload}tfstate${suffix}"
  Write-Host "ストレージアカウントを作成します: $SaName"
  Invoke-Az @(
    'storage', 'account', 'create',
    '-n', $SaName, '-g', $StateRg, '-l', $Location,
    '--sku', 'Standard_LRS', '--kind', 'StorageV2',
    '--min-tls-version', 'TLS1_2',
    '--allow-blob-public-access', 'false',
    '--https-only', 'true',
    '-o', 'none'
  ) | Out-Null

  # 誤って消したときに戻せるようにする。**state は消えると復旧手段が無い。**
  Invoke-Az @(
    'storage', 'account', 'blob-service-properties', 'update',
    '--account-name', $SaName, '-g', $StateRg,
    '--enable-versioning', 'true',
    '--enable-delete-retention', 'true', '--delete-retention-days', '30',
    '-o', 'none'
  ) | Out-Null
}
else {
  Write-Host "既存のストレージアカウントを使います: $SaName"
}

$SaId = Invoke-Az @('storage', 'account', 'show', '-n', $SaName, '-g', $StateRg, '--query', 'id', '-o', 'tsv')

# 自分自身に Blob のデータ権限を付ける。
# **「所有者だから読める」わけではない。** データ平面の権限は制御平面と別。
$CurrentUserId = Invoke-Az @('ad', 'signed-in-user', 'show', '--query', 'id', '-o', 'tsv')
Invoke-Az @(
  'role', 'assignment', 'create',
  '--assignee-object-id', $CurrentUserId, '--assignee-principal-type', 'User',
  '--role', 'Storage Blob Data Contributor', '--scope', $SaId, '-o', 'none'
) -AllowFailure | Out-Null

# ロール割り当ての反映には数十秒かかることがある。
# **付けた直後の container create は権限エラーになりやすい**ので、リトライする。
Write-Host 'コンテナを作成します（権限の反映を待ちながら最大 5 回試します）'
$created = $false
foreach ($i in 1..5) {
  $r = Invoke-Az @(
    'storage', 'container', 'create',
    '--name', $Container, '--account-name', $SaName,
    '--auth-mode', 'login', '-o', 'none'
  ) -AllowFailure
  if ($null -ne $r) { $created = $true; break }
  Write-Host "  $i 回目は失敗。20 秒待ちます"
  Start-Sleep -Seconds 20
}
if (-not $created) {
  Write-Host 'コンテナを作成できませんでした。数分おいてもう一度実行してください。' -ForegroundColor Red
  Write-Host '（Storage Blob Data Contributor の反映待ちであることがほとんどです）'
  exit 1
}

# ---------------------------------------------------------------------------
# 2. GitHub Actions 用の OIDC (長期シークレットを作らない)
# ---------------------------------------------------------------------------
Write-Section 'GitHub Actions 用の OIDC を設定します'
$AppId = Invoke-Az @('ad', 'app', 'list', '--display-name', $AppName, '--query', '[0].appId', '-o', 'tsv') -AllowFailure

if ([string]::IsNullOrWhiteSpace($AppId) -or $AppId -eq 'null') {
  $AppId = Invoke-Az @('ad', 'app', 'create', '--display-name', $AppName, '--query', 'appId', '-o', 'tsv')
  Write-Host "アプリ登録を作成しました: $AppName ($AppId)"
}
else {
  Write-Host "既存のアプリ登録を使います: $AppName ($AppId)"
}

Invoke-Az @('ad', 'sp', 'create', '--id', $AppId, '-o', 'none') -AllowFailure | Out-Null
$SpObjectId = Invoke-Az @('ad', 'sp', 'show', '--id', $AppId, '--query', 'id', '-o', 'tsv')

# ---------------------------------------------------------------------------
# フェデレーション資格情報。**subject を環境スコープで固定する。**
#
#   repo:<owner>/<repo>:environment:<env>
#
# ここを repo:owner/repo:* のようなワイルドカードにすると、
# **任意のブランチ・任意の PR から Azure に入れてしまう。**
# Public リポジトリでは、それは「誰でも入れる」と同義になる。
# ---------------------------------------------------------------------------
$Subject = "repo:${Repo}:environment:${Environment}"
$existing = Invoke-Az @(
  'ad', 'app', 'federated-credential', 'list', '--id', $AppId,
  '--query', "[?subject=='$Subject'] | [0].name", '-o', 'tsv'
) -AllowFailure

if ([string]::IsNullOrWhiteSpace($existing) -or $existing -eq 'null') {
  # JSON を引数に直接埋めると PowerShell のクォート処理で壊れる。
  # **一時ファイルに書いて @file で渡す**のが確実。
  $credFile = (New-TemporaryFile).FullName
  $credJson = @{
    name      = "$AppName-env"
    issuer    = 'https://token.actions.githubusercontent.com'
    subject   = $Subject
    audiences = @('api://AzureADTokenExchange')
  } | ConvertTo-Json
  Write-Utf8NoBom -Path $credFile -Content $credJson

  Invoke-Az @('ad', 'app', 'federated-credential', 'create', '--id', $AppId, '--parameters', "@$credFile", '-o', 'none') | Out-Null
  Remove-Item $credFile -Force
  Write-Host "フェデレーション資格情報を作成しました: $Subject"
}
else {
  Write-Host "フェデレーション資格情報は設定済みです: $Subject"
}

# ---------------------------------------------------------------------------
# 3. ロール割り当て
#
# 本来は必要なリソースグループだけに絞りたいが、Terraform が
# リソースグループ自体を作るため、サブスクリプション スコープが要る。
# **範囲を絞れないことを分かったうえで付ける**のが大事で、
# 「とりあえず Owner」にはしない (ロール割り当て権限まで渡してしまう)。
# ---------------------------------------------------------------------------
Write-Section 'ロールを割り当てます'
Invoke-Az @(
  'role', 'assignment', 'create',
  '--assignee-object-id', $SpObjectId, '--assignee-principal-type', 'ServicePrincipal',
  '--role', 'Contributor', '--scope', "/subscriptions/$SubscriptionId", '-o', 'none'
) -AllowFailure | Out-Null
Invoke-Az @(
  'role', 'assignment', 'create',
  '--assignee-object-id', $SpObjectId, '--assignee-principal-type', 'ServicePrincipal',
  '--role', 'Storage Blob Data Contributor', '--scope', $SaId, '-o', 'none'
) -AllowFailure | Out-Null

# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
$TerraformDir = Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) 'infra') 'terraform'
$BackendFile = Join-Path $TerraformDir 'backend.hcl'
Write-Utf8NoBom -Path $BackendFile -Content @"
resource_group_name  = "$StateRg"
storage_account_name = "$SaName"
container_name       = "$Container"
key                  = "$Environment.terraform.tfstate"

"@

$quotedEnv = '"' + $Environment + '"'

Write-Host ''
Write-Host '===========================================================================' -ForegroundColor Green
Write-Host '完了しました。' -ForegroundColor Green
Write-Host ''
Write-Host "$BackendFile を書き出しました（.gitignore 対象）。"
Write-Host '手元から terraform を叩く場合はこれを使います:'
Write-Host ''
Write-Host '  cd infra\terraform'
Write-Host '  terraform init -backend-config=backend.hcl'
Write-Host ''
Write-Host "GitHub 側の設定 (Settings > Environments > $Environment > Variables):"
Write-Host ''
Write-Host "  AZURE_CLIENT_ID          $AppId"
Write-Host "  AZURE_TENANT_ID          $TenantId"
Write-Host "  AZURE_SUBSCRIPTION_ID    $SubscriptionId"
Write-Host "  TFSTATE_RESOURCE_GROUP   $StateRg"
Write-Host "  TFSTATE_STORAGE_ACCOUNT  $SaName"
Write-Host ''
Write-Host 'いずれも秘密情報ではないので secrets ではなく variables で構いません。'
Write-Host 'OIDC を使う限り、GitHub に置く長期シークレットはゼロになります。'
Write-Host ''
Write-Host '注意:'
Write-Host "  - Environment 名 $quotedEnv は変えないでください。"
Write-Host '    フェデレーション資格情報の subject が'
Write-Host "      $Subject"
Write-Host '    で固定されているため、名前が違うと認証が通りません。'
Write-Host '  - 別環境 (stg / prd) を足すときは、このスクリプトを環境ごとに実行します。'
Write-Host '==========================================================================='  -ForegroundColor Green

# gh CLI があるなら、そのまま変数を入れられる形も出しておく
if (Get-Command gh -ErrorAction SilentlyContinue) {
  Write-Host ''
  Write-Host 'gh CLI が入っているので、次のコマンドでも設定できます:' -ForegroundColor Yellow
  Write-Host ''
  Write-Host "  gh api -X PUT repos/$Repo/environments/$Environment"
  Write-Host "  gh variable set AZURE_CLIENT_ID         -R $Repo -e $Environment -b '$AppId'"
  Write-Host "  gh variable set AZURE_TENANT_ID         -R $Repo -e $Environment -b '$TenantId'"
  Write-Host "  gh variable set AZURE_SUBSCRIPTION_ID   -R $Repo -e $Environment -b '$SubscriptionId'"
  Write-Host "  gh variable set TFSTATE_RESOURCE_GROUP  -R $Repo -e $Environment -b '$StateRg'"
  Write-Host "  gh variable set TFSTATE_STORAGE_ACCOUNT -R $Repo -e $Environment -b '$SaName'"
}
