# DelayCam — スマホカメラ → PC ディレイ再生

バスケ練習用。Android スマホで撮った映像を、Windows PC につないだ大型ディスプレイに
**10〜15秒（調整可）遅らせて**映す。選手はシュートを打ったあとディスプレイを見れば、
自分のフォームをそのまま確認できる。

```
 Androidスマホ (Chrome)                 Windows PC                      大型ディスプレイ
┌──────────────────────┐   Wi-Fi     ┌──────────────────────────────┐
│ /cam ページ           │  (PCのモバイル │ server.py (Python, aiohttp)   │
│  カメラ → WebCodecs   │  ホットスポット)│  静的ページ配信 + WebSocket 中継│
│  H.264 → WebSocket    │ ──────────▶ │              │                │
└──────────────────────┘             │ /view ページ (Chrome kiosk)   │──HDMI──▶ 全画面
                                     │  エンコード済みリングバッファ  │
                                     │  → 遅延後に復号 → canvas       │
                                     └──────────────────────────────┘
```

- スマホ側は **アプリ不要**、Chrome で QR を読むだけ
- 遅延バッファは PC 側のブラウザ内。遅延の増減・一時停止・コマ送り・スロー・巻き戻しが PC 側で完結
- 伝送は WebSocket(TCP)。どうせ10秒以上遅らせるので低遅延は不要、その分シンプルで欠落しない

## セットアップ（新しい PC でも 3 分）

1. このフォルダ (`delaycam/`) をコピーする
2. `start.bat` をダブルクリック
   - [uv](https://docs.astral.sh/uv/) が無ければ自動でインストールし、依存パッケージも自動で入る
   - サーバが起動し、表示ページ (`/view`) が Chrome/Edge の全画面で開く
3. 初回は Windows ファイアウォールの許可ダイアログが出るので **「プライベート」「パブリック」両方を許可**

手動で起動する場合:

```bash
uv run server.py --kiosk
```

`uv` を使わず venv でも動く: `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt && .venv\Scripts\python server.py --kiosk`

### オプション

| オプション | 意味 |
|---|---|
| `--kiosk` | 表示ページを全画面の専用ウィンドウで開く（終了は画面の「終了」ボタンか Esc） |
| `--open` | 表示ページを普通のブラウザで開く |
| `--ip 192.168.137.1` | QR に使う IP を固定（複数のネットワークがある PC で） |
| `--no-tls` | https を使わない（下記「証明書の警告が嫌な場合」） |
| `--http-port` / `--https-port` | ポート変更（既定 8080 / 8443） |
| `--rec-dir <dir>` | 録画の保存先（既定 `delaycam/recordings`。外付けドライブ等に変更可） |
| `--preroll <sec>` | REC 開始時に遡って保存する秒数（既定 30） |

録画を自動で始めるオプションは意図的に用意していない（下記「録画」参照）。

## コートでの使い方

1. **PC のモバイルホットスポットを ON** にする（設定 → ネットワーク → モバイルホットスポット、5GHz 推奨）
   - 体育館に Wi-Fi があればそれでも良い。PC とスマホが同じネットワークにいれば OK
2. スマホをホットスポットに接続し、`start.bat` で PC を起動
3. ディスプレイに出た **QR をスマホで読む**
   - 「この接続ではプライバシーが保護されません」と出たら **詳細設定 → …にアクセスする**（自己署名証明書のため。初回のみ）
4. スマホページで解像度・ビットレートを選び **「開始」**。三脚に固定、充電しながら使う
5. 15 秒後からディスプレイに映像が出る

### PC 側の操作（キーボード / マウス）

| キー | 動作 |
|---|---|
| ↑ / ↓ | 遅延 +1 秒 / −1 秒 |
| Space | 一時停止 / 再生 |
| ← / → | 1 コマ戻す / 進める |
| PageUp / PageDown | 5 秒戻す / 進める |
| S | スロー再生 0.5× |
| L | 設定した遅延に戻して通常再生 |
| M | 左右反転（鏡） |
| R | 表示を 90° 回転（縦置きディスプレイ用） |
| F | 全画面 |
| Q | QR コードを表示 |
| H | ヘルプ |
| Esc | 開いているカードを閉じる。何も開いていなければ終了確認 |
| 終了ボタン | 表示ウィンドウを閉じてサーバも停止する（kiosk は Esc/終了ボタンでしか閉じられない） |

マウスを動かすと同じ操作のボタンが下に出る。プレゼン用リモコン（PageUp/PageDown が送れるもの）があるとコート脇から操作できる。
初期遅延は URL で変えられる: `http://localhost:8080/view?delay=10`

## 録画とログ

**録画は PC 画面の「● REC」ボタンからのみ開始/停止する**（起動オプションや他端末からは不可）。
起動時は常に OFF。録画中は大画面の左上に赤い「● REC 00:12:34」が点滅し、スマホ側にも「PC側で録画中」と出る。

1. 操作バーの **● REC** → 撮影条件（視点 / カメラの高さ / 距離 / 練習内容 / メモ）を入力して **録画開始**。前回の入力は記憶される
2. REC を押した時点から **30 秒前**（プリロール）の映像も含めて保存が始まる
3. **■ REC 停止**、または「終了」で自動的に確定する。空き容量が 1GB を切ると自動停止

映像は再エンコードせず受信したまま書くので PC 負荷はほぼ増えない。4Mbps なら約 1.8GB/時。

画面操作（一時停止・コマ送り・遅延変更など）は、そのとき表示していたフレームのタイムスタンプ付きで記録される。
録画中は録画フォルダの `events.jsonl` に、録画していない時も `logs/events-YYYYMMDD.jsonl` に残る（映像は含まない）。
サーバのコンソール出力は `logs/delaycam-YYYYMMDD.log` に同じ内容が残る。

### 録画データ形式（format_version 1）

```
recordings/2026-09-13_17-40-12/
  meta.json      録画メタ（開始/終了時刻、撮影条件、PC・スマホ情報、セグメント一覧）
  seg01.h264     生の H.264 Annex B。ffmpeg / VLC / PyAV でそのまま読める
                 （VP8/VP9 のときは seg01.ivf。カメラが再接続すると seg02… と分かれる）
  frames.csv     seg, n, ts_us, key, offset, size, recv_unix_ms
  events.jsonl   1 行 1 イベント（後述）
```

- `ts_us` はスマホ側のフレームタイムスタンプ（µs、セグメント内で単調増加）。`recv_unix_ms` は PC 到着時刻
- `offset`/`size` でセグメントファイルから 1 フレームを切り出せる（IVF は 12 バイトのフレームヘッダの後ろを指す）
- `events.jsonl` の各行: `unix_ms`, `iso`, `type` + 内容
  - `rec_start` / `rec_stop` / `segment_start` / `segment_end`
  - `cam_connect` / `cam_disconnect` / `cam_config`
  - `stats`（5 秒ごと: 受信 fps, Mbps, 破棄数）
  - `ui`（画面操作: `action`, `display_ts_us`＝表示中フレームの ts, `seg`, `delay_target`, `delay_effective`, `rate`, `source`）
- `meta.json` の `segments[].phone` にスマホの UA・向き（縦/横）・カメラ名・実際の解像度/fps が入る

Python で読む例:

```python
import av, csv, json
meta = json.load(open("meta.json"))
rows = list(csv.DictReader(open("frames.csv")))
for frame in av.open("seg01.h264").decode(video=0):   # ts は rows[i]["ts_us"]
    img = frame.to_ndarray(format="bgr24")
```

MP4 が欲しいときは開発 PC で `ffmpeg -r 30 -i seg01.h264 -c copy seg01.mp4`。

### ツール（開発 PC 用）

```bash
uv run tools/rec_info.py recordings                   # 録画の一覧・要約（fps、欠落、イベント数）
uv run tools/rec_replay.py recordings/2026-09-13_17-40-12 --loop   # 録画を偽スマホとしてサーバに流す
```

`rec_replay.py` を使うとコートに行かずに表示側・解析側の開発ができる（サーバは本物のスマホと区別しない）。

## スマホ側の推奨設定

| 用途 | 解像度 | ビットレート |
|---|---|---|
| まず動かす / 電波が不安 | 1280×720 30fps | 4 Mbps |
| 大画面で細かく見たい | 1920×1080 30fps | 8〜12 Mbps |

- 「テストパターン」にチェックを入れるとカメラを使わず経過秒数の映像を送る。配線確認や遅延の実測に使う
- **縦持ち・横持ちどちらでも可**。「開始」を押した時の向きで画面を固定する（向きを変えたい時は停止→開始）。
  PC 側は映像の縦横に合わせて自動で収まる。ディスプレイ自体を縦置きにしている場合は R キーで回転
- 表示される遅延秒数は「スマホ→PC の最短伝送時間」を除いた値。実際の撮影→表示は +0.1〜0.3 秒程度

## 証明書の警告が嫌な場合

`--no-tls` で起動すると http になる。その代わりスマホの Chrome で
`chrome://flags/#unsafely-treat-insecure-origin-as-secure` に `http://<PCのIP>:8080` を登録して再起動する必要がある
（http のままだとブラウザがカメラを許可しない）。

## トラブルシュート

- **スマホから開けない** → PC のファイアウォールで Python を許可しているか。`--ip` で QR の IP を明示してみる
- **映像が出ない/止まる** → PC 側 `/view` の左下に受信 fps が出る。0 なら回線、30 なのに出ないなら復号。F12 でコンソールを見る
- **カクつく** → ビットレートを下げる、720p にする、スマホを PC に近づける（10〜15m 以内、5GHz）
- **「別のカメラに置き換えられました」** → 他のスマホ/タブで /cam を開いている。1 台だけにする
- サーバのログに 5 秒ごとに受信 fps / Mbps が出る

## ファイル

| ファイル | 役割 |
|---|---|
| `server.py` | aiohttp サーバ。静的配信、WebSocket 中継、QR 生成、自己署名証明書生成 |
| `static/cam.html` / `cam.js` | スマホ側。getUserMedia → VideoEncoder → WebSocket |
| `static/view.html` / `view.js` | PC 側。受信 → バッファ → VideoDecoder → canvas。操作はすべてここ |
| `start.bat` | uv の自動インストール + `--kiosk` 起動 |
| `tools/` | 録画の要約・再送ツール |
| `certs/` | 初回起動時に自動生成される自己署名証明書（git 管理外） |
| `recordings/`, `logs/` | 録画とログ（git 管理外） |

### 通信形式

- テキスト: `{"type":"config", session, codec, width, height, fps, bitrate, ua, orientation, camera, settings}`（エンコーダ設定時に送信）
- 表示側 → サーバ: `{"type":"rec", action:"start"|"stop", setup, delay}`（localhost のみ）、`{"type":"event", action, display_ts_us, …}`
- サーバ → 表示側/スマホ: `{"type":"status", cam, cam_url, recording:{active, id, bytes, frames, free_gb, …}}`
- バイナリ: 16 バイトヘッダ + エンコード済みチャンク
  `u8 version(1) | u8 flags(bit0=key) | u16 予約 | f64 timestamp(µs) | u32 duration(µs)`
- H.264 は Annex B 形式（SPS/PPS がキーフレームに含まれるので、復号側に別途設定不要）

## 今後の拡張候補

- 「直前の N 秒をループ再生」ボタン
- スマホ 2 台（正面・横）の同時表示
- ネイティブ Android アプリ化（4K/60fps や高ビットレートが必要になったら。通信形式はそのまま使える）
