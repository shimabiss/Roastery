"""
テストから各サービスのモジュールを読み込むためのヘルパ。

なぜ ``sys.path.insert`` ではなく importlib なのか
--------------------------------------------------
サービスごとに ``app/`` というディレクトリ名が同じなので、
素朴に sys.path へ追加すると **``app`` という名前が衝突する**。
名前空間パッケージの挙動でたまたま動くこともあるが、
読み込み順に依存するため、テストの追加順で壊れる類のバグになる。

ファイルパスを指定して一意な名前で読み込めば、衝突しない。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(service: str, module: str):
    """``apps/<service>/app/<module>.py`` を一意な名前で読み込む。"""
    path = ROOT / "apps" / service / "app" / f"{module}.py"
    name = f"_roastery_{service.replace('-', '_')}_{module}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
