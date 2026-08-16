# Kubernetes マニフェスト

Kubernetes のテーマで使います。**現時点では空です。**

## 予定している構成

```
k8s/
├── base/                    # 環境非依存のマニフェスト
│   ├── apps/                # 4サービスの Deployment / Service
│   ├── platform/            # OTel Collector (DaemonSet + Gateway)
│   └── kustomization.yaml
└── overlays/
    ├── dev/
    └── prod/
```

## 設計上の前提

- **コンテナイメージは Azure Container Apps と共通**にします。
  「同じイメージが実行基盤を選ばない」ことを示すのが Kubernetes のテーマの狙いの1つです
- **Collector の設定の実体は `platform/otel-collector/` に置き**、ここからは
  ConfigMap として参照します。compose とマニフェストで設定が二重管理にならないようにします
- Collector の配置は DaemonSet（ノード単位の収集）+ Gateway（集約と送信）の2段構成を予定しています

詳細は [../../docs/adr/0003-deployment-strategy.md](../../docs/adr/0003-deployment-strategy.md) を参照してください。
