# Irodori-TTS-Serverless

[Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server) を RunPod Serverless で動かすための薄いラッパー。

- `handler.py` が Starlette `TestClient` 経由で上流の FastAPI アプリを in-process 呼び出し
- `Dockerfile` で Irodori-TTS 500M v3 モデルと DACVAE codec を image に焼き込み
- 呼び出し側は OpenAI 互換のリクエストボディを `event["input"]` に入れて RunPod `/runsync` に送信、レスポンスは base64 オーディオ

## 構成

```
.
├── Dockerfile                 # GPU base, モデル焼き込み済
├── handler.py                 # RunPod handler (TestClient で upstream を叩く)
├── pyproject.toml             # uv 用 — upstream を git+ 依存で取得
├── voices/                    # image に焼くプリセット voice (任意)
├── test_endpoint.py           # /runsync をローカルから叩くスモークテスト
└── .github/workflows/build.yml  # ghcr.io への自動ビルド & push
```

## デプロイ手順

### 1. GitHub リポジトリを作成して push

```powershell
cd E:\ai\Irodori-TTS-Serverless
git init
git add .
git commit -m "Initial commit"
gh repo create Riti0208/irodori-tts-serverless --public --source=. --push
```

`--public` にすると ghcr.io のイメージ pull で認証不要になる (RunPod 側の image pull が楽)。プライベートにする場合は別途 RunPod に GitHub の PAT を Container Registry Credential として登録してください。

### 2. GitHub Actions でイメージビルド

push 後、`.github/workflows/build.yml` が自動実行されます (約 20〜30 分)。Actions タブで進捗確認。完了すると `ghcr.io/riti0208/irodori-tts-serverless:latest` ができます。

Actions 完了後、GitHub の **Packages** タブから image の visibility を `Public` に変更すると RunPod が認証なしで pull できます。

### 3. RunPod Serverless エンドポイント作成

```powershell
runpodctl serverless endpoint create `
  --name irodori-tts `
  --image ghcr.io/riti0208/irodori-tts-serverless:latest `
  --gpuTypes "NVIDIA GeForce RTX 4090" `
  --containerDiskInGb 25 `
  --workersMin 0 `
  --workersMax 2 `
  --idleTimeout 30 `
  --executionTimeoutMs 600000
```

> ※ `runpodctl serverless` のサブコマンド名は CLI バージョンで変わることがあります。`runpodctl serverless --help` で確認してください。Web ダッシュボード (https://runpod.io/console/serverless) でも同じことができます。

エンドポイント作成後、出力された **Endpoint ID** を控えます。

### 4. テスト

```powershell
$env:RUNPOD_API_KEY = "rpa_xxxxxxxx"
$env:RUNPOD_ENDPOINT_ID = "xxxxxxxx"

python test_endpoint.py "こんにちは、これはRunPod経由のテストです。" out.wav

# 参照音声でクローン
python test_endpoint.py "声色テスト" cloned.wav --ref-wav E:\ai\Irodori-TTS-500M-v3\refs\AizawaEma_ref10s.wav
```

## API

`POST https://api.runpod.ai/v2/<endpoint-id>/runsync`

リクエスト:
```json
{
  "input": {
    "input": "合成するテキスト",
    "voice": "none",
    "response_format": "wav",
    "speed": 1.0,
    "irodori": {
      "num_steps": 16,
      "seed": 42,
      "cfg_scale_text": 3.0,
      "cfg_scale_speaker": 5.0,
      "t_schedule_mode": "linear"
    },
    "ref_wav_b64": "<base64 encoded WAV>"
  }
}
```

レスポンス (success):
```json
{
  "output": {
    "audio_b64": "<base64 encoded audio>",
    "format": "wav",
    "seed": "42",
    "total_to_decode": "1.234"
  }
}
```

`event["input"]` の中身は上流 `/v1/audio/speech` とほぼ同じ。追加項目:
- `ref_wav_b64`: WAV ファイルを base64 で渡す (handler が `/tmp` に書き出して `ref_wav` に注入)
- `ref_wav_url`: HTTP(S) URL から取得 (同上)

## ローカルでハンドラ単体テスト

GPU マシンで pip 環境を用意してから:

```powershell
pip install -e .
$env:IRODORI_MODEL_DEVICE = "cuda"
$env:IRODORI_MODEL_PRECISION = "bf16"
python handler.py --local-test
```

`runpod` パッケージのローカル API モードでも検証可能:

```powershell
python handler.py
# → runpod runtime mode (本番と同じ runpod.serverless.start)
```

## 環境変数 (Dockerfile 既定)

`IRODORI_*` は全て上流 [Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server#configuration) の挙動を制御します。代表的なもの:

| 変数 | 既定 (Dockerfile) | メモ |
| :--- | :--- | :--- |
| `IRODORI_CHECKPOINT` | `/app/weights/model.safetensors` | image 内に焼き込まれた重み |
| `IRODORI_MODEL_DEVICE` | `cuda` | |
| `IRODORI_MODEL_PRECISION` | `bf16` | |
| `IRODORI_CODEC_PRECISION` | `bf16` | |
| `IRODORI_PRELOAD` | `true` | worker 起動時にモデルロード (cold start で 10s 程度) |
| `IRODORI_ALLOW_NO_REF_VOICE` | `true` | `voice="none"` を許可 |
| `IRODORI_API_KEY` | unset | 上流の bearer token (handler 直叩きなら不要) |

RunPod ダッシュボードの "Environment Variables" で上書き可能。

## トラブルシューティング

- **401 on `runpodctl`**: `runpodctl config --apiKey rpa_xxx` でキー再設定
- **`/runsync` がタイムアウト**: `executionTimeoutMs` を 600000 (10 分) 以上に。長文合成なら 1200000 (20 分) も検討
- **ghcr.io pull 失敗**: image を Public にするか、RunPod に GitHub Container Registry credential を登録
- **VRAM 不足**: 4090 24GB なら余裕。3090 でも OK。8GB 級は厳しい (max_seconds を小さくすれば可)
- **生成音声が短い/長い**: `irodori.duration_scale` (>1で長く、<1で短く) または `speed` (>1で速く)
