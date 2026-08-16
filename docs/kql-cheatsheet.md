# Application Insights KQL チートシート

Azure Portal → Application Insights → **ログ** で実行します。

## OpenTelemetry のデータがどのテーブルに入るか

| OTel の概念 | App Insights のテーブル |
|---|---|
| SERVER / CONSUMER kind の span | `requests` |
| CLIENT / INTERNAL / PRODUCER kind の span | `dependencies` |
| ログレコード、span event | `traces` |
| `record_exception()` した例外 | `exceptions` |
| メトリクス | `customMetrics` |

span attribute は各テーブルの `customDimensions` に入ります。
`trace_id` は `operation_Id`、`span_id` は `id`、親 span は `operation_ParentId` に対応します。

---

## 基本

### 直近の注文リクエストを見る

```kusto
requests
| where timestamp > ago(30m)
| where cloud_RoleName == "order-api" and name contains "orders"
| project timestamp, name, duration, resultCode, success, operation_Id
| order by timestamp desc
| take 50
```

### trace_id を指定して1本の trace をすべて取り出す

```kusto
let opId = "＜operation_Id を貼る＞";
union requests, dependencies, traces, exceptions
| where operation_Id == opId
| project timestamp, itemType, name = coalesce(name, message, type),
          duration, cloud_RoleName, id, operation_ParentId
| order by timestamp asc
```

デモの「1本の trace を全部見る」はこれ1つで足ります。

---

## 遅延の切り分け

### サービス別の応答時間パーセンタイル

```kusto
requests
| where timestamp > ago(1h)
| summarize p50 = percentile(duration, 50),
            p95 = percentile(duration, 95),
            p99 = percentile(duration, 99),
            count()
      by cloud_RoleName
| order by p95 desc
```

### 遅い注文の内訳を、依存先ごとに分解する

「フロントが遅い」を「どの依存先が遅いか」に落とす、実務で一番使うクエリです。

```kusto
let slowOps =
    requests
    | where timestamp > ago(1h) and cloud_RoleName == "frontend" and duration > 1000
    | project operation_Id;
dependencies
| where timestamp > ago(1h)
| where operation_Id in (slowOps)
| summarize total = sum(duration), calls = count() by name, cloud_RoleName
| order by total desc
```

### 障害注入の設定値ごとに応答時間を見る

`chaos.latency_ms` を span attribute に載せてあるので、原因まで一気に説明できます。

```kusto
dependencies
| where timestamp > ago(1h) and cloud_RoleName == "payment-api"
| extend injected = toint(customDimensions["chaos.latency_ms"])
| summarize p95 = percentile(duration, 95), count() by injected
| order by injected asc
```

---

## エラーの分析

### 失敗の内訳

```kusto
requests
| where timestamp > ago(1h) and success == false
| summarize count() by cloud_RoleName, resultCode
| order by count_ desc
```

### 例外のトップ

```kusto
exceptions
| where timestamp > ago(1h)
| summarize count() by cloud_RoleName, type, outerMessage
| order by count_ desc
```

### 在庫不足だけを抜き出す（span event を使う）

システム障害ではなく業務上の拒否を区別する例です。

```kusto
traces
| where timestamp > ago(1h) and message == "inventory.insufficient"
| extend sku = tostring(customDimensions["order.sku"]),
         qty = toint(customDimensions["order.quantity"])
| summarize count() by sku
| order by count_ desc
```

---

## メトリクス

### カスタムメトリクスの一覧

```kusto
customMetrics
| where timestamp > ago(1h)
| distinct name, cloud_RoleName
| order by cloud_RoleName asc
```

### 注文件数の推移

```kusto
customMetrics
| where timestamp > ago(1h) and name == "orders.created"
| summarize orders = sum(valueSum) by bin(timestamp, 1m)
| render timechart
```

### 拒否理由の内訳

```kusto
customMetrics
| where timestamp > ago(1h) and name == "orders.rejected"
| extend reason = tostring(customDimensions["reason"])
| summarize sum(valueSum) by reason, bin(timestamp, 5m)
| render columnchart
```

### 決済の所要時間分布

```kusto
customMetrics
| where timestamp > ago(1h) and name == "payment.authorize.duration"
| summarize avg = sum(valueSum) / sum(valueCount),
            max = max(valueMax)
      by bin(timestamp, 1m)
| render timechart
```

---

## PII マスクの確認

Collector の `attributes/redact` プロセッサで `enduser.id` をハッシュ化しています。
生の `demo-user` が残っていないことを確認します。

```kusto
dependencies
| where timestamp > ago(1h)
| where isnotempty(customDimensions["enduser.id"])
| project timestamp, name, user = tostring(customDimensions["enduser.id"])
| take 20
```

---

## 取り込み量とコストの確認

リソースを片付ける前に、いくらぶん取り込んだかを確認しておくとコスト感がつかめます。

```kusto
union withsource = tbl *
| where timestamp > ago(1d)
| summarize GB = sum(_BilledSize) / 1024.0 / 1024.0 / 1024.0 by tbl
| order by GB desc
```

> Log Analytics の課金は取り込み量に比例します。
> サンプリングの話をするときに、この数字を実物として見せると理解が早いです。
