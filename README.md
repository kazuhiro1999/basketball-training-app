# basketball-training-app
Analyze and master your shooting form with delayed playback. This app offers real-time delayed playback and visual analysis to help you break down and improve your basketball shooting form.

バスケットボール練習用の「遅延ミラー」。スマホで撮った自分の動きを、コート脇の大型ディスプレイに
10〜15 秒遅れで映し、シュートフォームをその場で確認する。将来的に骨格推定オーバーレイと
補助的なフォーム解析・可視化を足していく。

| ディレクトリ | 内容 | 状態 |
|---|---|---|
| [`delaycam/`](delaycam/) | 遅延ビューワー本体（Python サーバ + スマホ用/PC用 Web ページ）+ 録画・操作ログ + 骨格オーバーレイ表示 + インスタントリプレイ | **v1.4** — 使い方は [delaycam/README.md](delaycam/README.md) |
| [`analyzer/`](analyzer/) | 骨格推定（RTMPose / RTMO / YOLO-pose / MediaPipe を差し替え可）・複数人トラッキング・ボール検出。録画のオフライン解析と、遅延再生へのライブ骨格供給（`uv run live`）。ONNX Runtime CPU | **0.2** — 使い方は [analyzer/README.md](analyzer/README.md) |

## 撮影の前提（v1）

- スマホ 1 台。三脚で高さ約 1m、**真横**または**斜め 45°** から（正面はボールが当たるので避ける）
- 静止した位置で打つ練習（フリースロー等）は**縦持ち**の方が体に使える画素が多い。横移動があるドリルは横持ち
- 画面には複数の生徒が写る前提（順番待ち・オクルージョンあり）。骨格推定は複数人トラッキングが必要
- まず撮りっぱなしで録画し、アノテーションと解析は後から録画に対して行う。ライブ表示に載せる処理は軽量モデルに限る

## 起動

Windows 10/11 + Chrome か Edge。初回だけインターネット接続が必要（uv・Python・依存パッケージ、骨格推定モデル約 50MB を自動で取得）。
体育館にネットが無いなら、**事前にネットのある場所で一度起動**しておく。2 回目以降はオフラインで動く。

| ファイル | 内容 |
|---|---|
| `delaycam/start.bat` | 遅延再生だけ起動（全画面） |
| `delaycam/start_with_pose.bat` | 遅延再生 + 骨格表示を同時に起動 |

`start.bat` で起動した場合も、画面の「⚙ 設定」→「骨格の設定・起動…」から骨格推定を後で起動できる。
スマホは画面に出る QR を Chrome で読み取るだけ。詳しい手順・操作は [delaycam/README.md](delaycam/README.md)。

初回は Windows ファイアウォールの許可（プライベート・パブリック両方）が出る。
zip をブラウザでダウンロードした場合は bat の実行時に「Windows によって PC が保護されました」→「詳細情報」→「実行」。

## License

MIT — see [LICENSE](LICENSE).
