# ---------------------------------------------------------------------------
# OpenTelemetry Collector のイメージ (Azure Container Apps 用)。
#
# なぜイメージを作るのか
# ----------------------
# docker compose では設定ファイルをボリュームマウントしていた。
# **Azure Container Apps にはバインドマウントが無い**ため、そのままでは動かない。
#
# 選択肢は3つあった。
#   1. 設定を焼き込んだイメージを作る   ← これを採用
#   2. Azure Files をマウントする       ← ストレージアカウントが1つ増える
#   3. 設定を環境変数で丸ごと渡す       ← 読めない・差分が追えない
#
# 1 の欠点は「設定を変えるとイメージの再ビルドが要る」こと。
# 利点は **動いている設定がイメージのタグで一意に特定できる**こと。
# 可観測性の設定は頻繁には変えないので、後者を取った。
#
# 設定の実体は platform/otel-collector/ の1箇所のままで、
#   compose      … ボリュームマウント
#   ACA          … この COPY
#   Kubernetes   … ConfigMap (Kubernetes のテーマで追加)
# と **参照のしかたが実行基盤ごとに違うだけ**という形を保っている (ADR-0001)。
# ---------------------------------------------------------------------------

# タグを固定する。compose 側は COLLECTOR_VERSION=latest でも動くが、
# デプロイするものは「いつビルドしても同じ」でなければ切り戻しの意味が無い。
ARG COLLECTOR_VERSION=0.137.0
FROM otel/opentelemetry-collector-contrib:${COLLECTOR_VERSION}

COPY config.yaml config.local.yaml config.aca.yaml /etc/otelcol/

# ACA では Jaeger も Prometheus も立てていないため、
# **Azure Monitor だけに送る config.aca.yaml を既定にする。**
# config.yaml のままだと jaeger:4317 への接続失敗がログを埋める。
CMD ["--config=/etc/otelcol/config.aca.yaml"]
