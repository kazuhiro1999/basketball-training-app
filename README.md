# basketball-training-app
Analyze and master your shooting form with delayed playback. This app offers real-time delayed playback and visual analysis to help you break down and improve your basketball shooting form.

バスケットボール練習用の「遅延ミラー」。スマホで撮った自分の動きを、コート脇の大型ディスプレイに
10〜15 秒遅れで映し、シュートフォームをその場で確認する。将来的に骨格推定オーバーレイと
補助的なフォーム解析・可視化を足していく。

| ディレクトリ | 内容 | 状態 |
|---|---|---|
| [`delaycam/`](delaycam/) | 遅延ビューワー本体（Python サーバ + スマホ用/PC用 Web ページ）+ 録画・操作ログ | **v1.1** — 使い方は [delaycam/README.md](delaycam/README.md) |
| [`analyzer/`](analyzer/) | 録画に対する骨格推定（RTMO / RTMPose / YOLO-pose を差し替え可）・複数人トラッキング・ボール検出。ONNX Runtime CPU | **0.1** — 使い方は [analyzer/README.md](analyzer/README.md) |

## 撮影の前提（v1）

- スマホ 1 台。三脚で高さ約 1m、**真横**または**斜め 45°** から（正面はボールが当たるので避ける）
- 静止した位置で打つ練習（フリースロー等）は**縦持ち**の方が体に使える画素が多い。横移動があるドリルは横持ち
- 画面には複数の生徒が写る前提（順番待ち・オクルージョンあり）。骨格推定は複数人トラッキングが必要
- まず撮りっぱなしで録画し、アノテーションと解析は後から録画に対して行う。ライブ表示に載せる処理は軽量モデルに限る

## 起動

`delaycam/start.bat` をダブルクリック（uv が無ければ自動インストール）。

## License

MIT — see [LICENSE](LICENSE).
