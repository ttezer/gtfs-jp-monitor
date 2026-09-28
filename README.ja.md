# gtfs-jp-monitor

🇹🇷 [Türkçe](README.tr.md) · 🇬🇧 [English](README.md) · 🇯🇵 **日本語**

[gtfs-data.jp](https://gtfs-data.jp) で公開されている GTFS フィードの監視パイプラインです。

**Web サイト：<https://ttezer.github.io/gtfs-jp-monitor/>**（毎日更新・トルコ語／英語／日本語）

各フィードの公開（恒久的な `gtfs_file_uid` で識別）ごとに [GTFS Analyzer](https://github.com/ttezer/gtfs-analyzer)
を実行し、言語に依存しない簡潔な要約を保存して、公開間の差分を作成します。差分は検証結果の差分と、
意味的な変更レポート（停留所、路線、経路、時刻表、運行日、運賃）です。

状況：毎日稼働中、開発継続中。パイプラインの仕様は [docs/data-model.md](docs/data-model.md)
にあります（英語）。

結果は毎日 <https://ttezer.github.io/gtfs-jp-monitor/> で公開されます。比較はリンクで共有でき
（data-model §10.1）、各変更レポートは
`reports/<org_id>/<feed_id>/<old_uid>__<new_uid>.json.gz` に JSON としても公開されます
（data-model §10.0）。`status.json` はサイトの最終更新日時を示します。

公開のペアはそれぞれ、意味のあるサービス変更・技術的な変更のみ・同等・不明のいずれかに分類されます。
レポートのないペアは、変更されたファイルから分類を推定します（data-model §12）。`metrics.json`
はフィードごと・全体の過去365日について、公開頻度と意味のある更新の頻度、変更の割合、検証結果の悪化、
カバー率を示します（§13）。`fields.json.gz` は車椅子情報などの任意項目がどの程度入力されているかを示し、
日本の範囲外にある停留所や緯度・経度が入れ替わっている停留所を示します（§15）。

## 動作の仕組み

GitHub Actions のワークフロー（`.github/workflows/gtfs-jp-monitor.yml`）が毎日 04:10 JST に実行されます
（GitHub の都合で遅れることがあります）。カタログを同期し、新しい公開を解析し、変更レポートを作成して、
結果を別の非公開データリポジトリにコミットし、サイトを GitHub Pages に公開します。
データリポジトリのウォッチドッグが毎晩、サイトが30時間以内に再構築されたことを確認し、
GitHub が非アクティブを理由に毎日の実行を無効にした場合は再度有効にします（data-model §10, §10.2）。

## 自分で実行する

Python 3.11 以上が必要です。パイプラインは標準ライブラリ以外を必要としません。コマンドは
データリポジトリのチェックアウトである `--data-dir` を対象に動作し（`install-analyzer` は `--dest`
を取ります）、それぞれ `--help` があります。

| コマンド | 内容 |
|---|---|
| `sync-catalog` | gtfs-data.jp からフィードと公開のカタログを取得します |
| `install-analyzer` | 固定された GTFS Analyzer のリリースを取得して検証します（`analyzer.lock.json`） |
| `analyze` | 解析器とプロファイルについて記録のない公開を解析します |
| `semantic-reports` | ページに表示される公開の、不足している変更レポートを作成します |
| `export-web` | データディレクトリからサイトを構築します（`--html`） |
| `semantic-report`, `rawdiff` | 2つの GTFS ZIP ファイルを直接比較します |

```bash
PYTHONPATH=src python3 -m gtfs_jp_monitor sync-catalog --data-dir data
PYTHONPATH=src python3 -m gtfs_jp_monitor install-analyzer --dest analyzer
PYTHONPATH=src python3 -m gtfs_jp_monitor analyze --data-dir data --analyzer analyzer/gtfs-analyzer --limit 20
PYTHONPATH=src python3 -m gtfs_jp_monitor semantic-reports --data-dir data
PYTHONPATH=src python3 -m gtfs_jp_monitor export-web --data-dir data --release-tag v0.14.0 --html site/index.html
```

ローカルでビルドした解析器の結果は、スクラッチとして指定されたディレクトリにのみ書き込まれます
（data-model §6.2）。公開データはワークフローが作成します。

## 構成

| パス | 内容 |
|---|---|
| `docs/` | パイプラインの仕様（`data-model.md`）と意味的変更エンジンの仕様 |
| `schemas/` | 生成されるデータの JSON Schema |
| `src/gtfs_jp_monitor/` | パイプラインのコード |
| `src/gtfs_jp_semantic/` | 意味的変更エンジン（`docs/semantic/`） |
| `web/prototype/` | `export-web` がデータを埋め込む Web ページ |
| `tests/` | 単体テスト |

テストの実行（スキーマのテストには `dev` の追加依存 `jsonschema` が必要です）：

```bash
PYTHONPATH=src python3 -m unittest discover -t . -s tests
```

生成データは別のデータリポジトリに書き込まれます。GTFS の ZIP ファイルそのものは保存せず、
それぞれの内容署名のみを保存します（data-model §4.2）。

API クライアントは Python の標準ライブラリのみを使い、TLS 証明書を常に検証します。
システムの CA 証明書を含まない Python（例：python.org の macOS インストーラー）では
CA バンドルが必要です（例：`SSL_CERT_FILE=$(python3 -m certifi)`）。

## データの出典とライセンス

フィードのデータは gtfs-data.jp と各交通事業者によるものです。各公開の記録には、そのフィードに
公開されているライセンスを保存しています。派生データを再配布する際は、そのライセンスに従ってください。

このリポジトリのコードは [MIT ライセンス](LICENSE) で公開しています。フィードのデータとそこから
派生したデータは対象外で、それぞれのライセンスに従います。
