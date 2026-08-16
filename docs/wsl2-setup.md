# Windows 11 + WSL2 セットアップ手順

このデモは **Windows 11 + WSL2 (Ubuntu)** を前提環境としています。
以下は初回のみの作業です。所要 15〜30 分（大半がダウンロード待ち）。

---

## 0. 全体像

```
Windows 11
 ├─ ブラウザ (Edge / Chrome)  ──────▶ http://localhost:3000 で画面を見る
 └─ WSL2 (Ubuntu)
     └─ Docker  ──▶ コンテナ 9 個（アプリ・DB・Collector・Jaeger など）
```

WSL2 は localhost を Windows 側に自動転送するため、**Windows のブラウザから
そのまま `http://localhost:3000` で見られます**。特別な設定は不要です。

---

## 1. WSL2 を用意する

PowerShell（管理者）で確認します。

```powershell
wsl --status
wsl --list --verbose
```

`VERSION` が `2` の Ubuntu があれば OK です。無い場合:

```powershell
wsl --install -d Ubuntu-24.04
```

**バージョンを明示している**のは意図的です。`-d Ubuntu`（無指定）は「その時点の最新 LTS」を
指すため、時期によって入るものが変わり、この手順書とズレます。`.env` の
`COLLECTOR_VERSION` を固定するのと同じ理由です。

> Ubuntu 26.04 LTS でも動きますが、WSL 向けは Microsoft Store 配布がまだで
> tarball（`wsl --install --from-file`）での導入になります。ここは主題ではないので、
> 手順が枯れている 24.04 LTS（標準サポート 2029年まで）を使います。

インストール後、再起動して Ubuntu を起動し、ユーザー名とパスワードを設定します。

WSL2 のバージョンが古い場合は更新しておきます。

```powershell
wsl --update
```

---

## 2. Docker を入れる

**どちらか一方**を選んでください。

### 方式A: Docker Desktop（手軽）

1. Docker Desktop をインストール
2. 設定 → General → **Use the WSL 2 based engine** を有効化
3. 設定 → Resources → WSL Integration → **使う Ubuntu ディストリビューションを ON**

> Docker Desktop は、従業員 250 人以上かつ年間売上 1,000 万ドル以上の組織では
> 有償サブスクリプションが必要です。社内の契約状況を確認してください。

### 方式B: WSL2 内に Docker Engine を直接入れる（ライセンス上の制約なし）

Docker Engine 本体は Apache 2.0 で、Docker Desktop の有償化対象外です。
Ubuntu のターミナルで実行します。

```bash
# 1) リポジトリを追加
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

# 2) インストール
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 3) sudo なしで docker を使えるようにする
sudo usermod -aG docker $USER
```

**WSL2 で systemd を有効化**して、Docker が自動起動するようにします。

```bash
sudo tee /etc/wsl.conf > /dev/null <<'EOF'
[boot]
systemd=true
EOF
```

PowerShell から WSL を再起動します。

```powershell
wsl --shutdown
```

Ubuntu を開き直して確認します。

```bash
sudo systemctl enable --now docker
docker run --rm hello-world
```

> systemd を使わない場合は、Ubuntu を起動するたびに
> `sudo service docker start` を実行してください。

### 動作確認（方式A / B 共通）

リポジトリを配置済みなら、事前チェックスクリプトでまとめて確認できます。

```bash
./scripts/check-docker.sh
```

コマンドの有無・デーモンへの接続・sudo なし実行・イメージの取得・コンテナからの
名前解決と外部通信・バインドマウント・ポート公開に加えて、WSL2 固有の項目
（systemd の有効化、リポジトリの置き場所、メモリと空きディスク）まで確認します。
NG が出た項目には、そのまま打てる対処コマンドが表示されます。

リポジトリをまだ置いていない場合は、最低限これだけ通れば先に進めます。

```bash
docker compose version     # v2 以上であること
docker run --rm hello-world
```

---

## 3. メモリ割り当て（必要な場合のみ）

このデモはコンテナ 9 個でメモリ 2〜3GB 程度を使います。
WSL2 の既定はホストメモリの 50%（上限 8GB 程度）なので、通常はそのままで足ります。

不足する場合は、Windows 側の `C:\Users\<ユーザー名>\.wslconfig` を作成します。

```ini
[wsl2]
memory=8GB
processors=4
```

編集後は PowerShell で `wsl --shutdown` して反映します。

---

## 4. リポジトリを配置する

**必ず WSL2 の Linux ファイルシステム側（`~/`）に置いてください。**
`/mnt/c/...`（Windows 側のドライブ）に置くとファイル I/O が数倍遅くなり、
`docker compose build` が極端に時間がかかります。

```bash
cd ~
git clone <リポジトリURL> roastery
cd roastery
```

zip を受け取った場合は、Windows のダウンロードフォルダから WSL2 側にコピーします。

```bash
cd ~
cp /mnt/c/Users/<ユーザー名>/Downloads/roastery.zip .
unzip roastery.zip
cd roastery
```

### 改行コードの注意

Windows 側で `git clone` すると、Git の設定によってはシェルスクリプトが
CRLF に変換され、WSL2 で実行できなくなります。

```
./scripts/local-start.sh: /usr/bin/env: 'bash\r': No such file or directory
```

このエラーが出たら、以下で直せます。

```bash
sudo apt install -y dos2unix
dos2unix scripts/*.sh
```

リポジトリには `.gitattributes` を同梱してあるので、
**そこから clone し直せば再発しません**。

---

## 5. 起動する

```bash
./scripts/local-start.sh
```

ビルドから動作確認まで自動で走ります。初回は 3〜5 分かかります。
`すべて正常です` と出たら、**Windows 側のブラウザ**で以下を開いてください。

| URL | 内容 |
|---|---|
| http://localhost:3000 | 公開サイト（架空のコーヒー豆EC） |
| http://localhost:3000/ops | 運用画面（障害注入 / 動作確認） |
| http://localhost:16686 | Jaeger — トレースの waterfall |
| http://localhost:9090 | Prometheus — メトリクス |

---

## 6. Azure を使う場合のツール（任意）

ローカルで動かすだけなら不要です。Application Insights に接続する場合にだけ必要になります。

**いずれも WSL2（Ubuntu）側に入れてください。** Windows 側に入れても
`scripts/azure-setup.sh` からは呼べません。WSL2 のシェルから見えるのは
`terraform.exe` という名前になるうえ、Windows 版の Terraform に WSL2 のパスを
渡すことになり、噛み合いません。

### Terraform

```bash
wget -O - https://apt.releases.hashicorp.com/gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
https://apt.releases.hashicorp.com $(grep -oP '(?<=UBUNTU_CODENAME=).*' /etc/os-release || lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list

sudo apt update && sudo apt install -y terraform
```

### Azure CLI

```bash
curl -fsSL 'https://azurecliprod.blob.core.windows.net/$root/deb_install.sh' | sudo bash
```

### 確認

```bash
terraform version     # v1.9 以上であること
az version
```

### ログイン

```bash
az login
```

WSL2 からはブラウザが自動で開かないことがあります。その場合は、

```bash
az login --use-device-code
```

表示されたコードを Windows 側のブラウザで入力します。

> **Windows 側の Azure CLI とは認証が共有されません。** 資格情報は
> `~/.azure` に保存されるため、Windows と WSL2 でそれぞれログインが必要です。

### 実行

```bash
./scripts/azure-setup.sh
```

`terraform apply` の内容が表示されるので、確認して `yes` を入力してください。
詳細は [../infra/terraform/README.md](../infra/terraform/README.md) を参照してください。

---

## 7. WSL2 特有のトラブルシュート

| 症状 | 原因と対処 |
|---|---|
| `bash\r: No such file or directory` | 改行コードが CRLF。`dos2unix scripts/*.sh` |
| `Cannot connect to the Docker daemon` | Docker が起動していない。方式B なら `sudo service docker start`（または systemd 有効化）。方式A なら Docker Desktop の WSL Integration が ON か確認 |
| `permission denied ... docker.sock` | `sudo usermod -aG docker $USER` の後、一度 `exit` して Ubuntu を開き直す（または `wsl --shutdown`） |
| ビルドが異常に遅い | リポジトリを `/mnt/c/...` に置いている。`~/` 配下に移動する |
| ブラウザから localhost に繋がらない | PowerShell で `wsl --shutdown` して WSL を再起動。それでも駄目なら `wsl --update` |
| メモリ不足でコンテナが落ちる | `.wslconfig` で `memory=8GB` を設定して `wsl --shutdown` |
| ディスクを食っている | `docker system prune -a` で未使用イメージを削除。WSL2 の仮想ディスクは自動では縮まない |

---

## 8. 片付け

```bash
docker compose down -v      # コンテナとボリュームを削除
docker system prune -a      # イメージも消す場合（次回の再ビルドが長くなります）
```

Azure リソースを作った場合は、こちらも忘れずに。

```bash
terraform -chdir=infra/terraform destroy
```
