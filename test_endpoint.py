"""Smoke-test the RunPod Serverless endpoint.

Usage:
    set RUNPOD_API_KEY=rpa_xxx
    set RUNPOD_ENDPOINT_ID=xxxxxxxx
    python test_endpoint.py "こんにちは、これはRunPod経由のテストです。" out.wav

Optional 3rd arg: reference WAV path (will be base64-encoded for voice cloning).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    parser.add_argument("output", type=Path, default=Path("out.wav"), nargs="?")
    parser.add_argument("--ref-wav", type=Path, default=None, help="Local WAV to clone")
    parser.add_argument("--voice", default="none", help="voice id (when not using --ref-wav)")
    parser.add_argument("--num-steps", type=int, default=16)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--response-format", default="wav")
    parser.add_argument("--endpoint-id", default=os.environ.get("RUNPOD_ENDPOINT_ID"))
    parser.add_argument("--api-key", default=os.environ.get("RUNPOD_API_KEY"))
    parser.add_argument("--sync", action="store_true", default=True, help="Use /runsync")
    args = parser.parse_args()

    if not args.endpoint_id or not args.api_key:
        print("ERROR: set RUNPOD_ENDPOINT_ID and RUNPOD_API_KEY", file=sys.stderr)
        return 2

    payload: dict[str, object] = {
        "input": args.text,
        "voice": args.voice,
        "response_format": args.response_format,
        "irodori": {"num_steps": args.num_steps},
    }
    if args.seed is not None:
        payload["irodori"]["seed"] = args.seed  # type: ignore[index]
    if args.ref_wav is not None:
        if not args.ref_wav.is_file():
            print(f"ERROR: ref_wav not found: {args.ref_wav}", file=sys.stderr)
            return 2
        payload["ref_wav_b64"] = base64.b64encode(args.ref_wav.read_bytes()).decode("ascii")

    url = f"https://api.runpod.ai/v2/{args.endpoint_id}/{'runsync' if args.sync else 'run'}"
    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/json",
    }
    body = json.dumps({"input": payload})

    print(f"POST {url}")
    print(f"  payload keys: {sorted(payload.keys())} (text={len(args.text)} chars)")
    t0 = time.perf_counter()
    r = requests.post(url, headers=headers, data=body, timeout=600)
    elapsed = time.perf_counter() - t0
    print(f"  status={r.status_code} elapsed={elapsed:.2f}s")

    if r.status_code != 200:
        print("ERROR response:", r.text[:2000], file=sys.stderr)
        return 1

    js = r.json()
    # /runsync may return early with status=IN_QUEUE / IN_PROGRESS when the
    # worker is still spinning up. In that case fall back to polling /status
    # until the job finishes.
    if isinstance(js, dict) and js.get("status") in {"IN_QUEUE", "IN_PROGRESS"} and "id" in js:
        job_id = js["id"]
        status_url = f"https://api.runpod.ai/v2/{args.endpoint_id}/status/{job_id}"
        print(f"  job {js['status']} -> polling {status_url}")
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(5)
            sr = requests.get(status_url, headers=headers, timeout=60)
            sj = sr.json()
            status = sj.get("status")
            print(f"  status={status} ({time.time() - t0:.1f}s elapsed)")
            if status in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
                js = sj
                break
        else:
            print("polling timed out", file=sys.stderr)
            return 1

    if js.get("status") == "FAILED":
        print("job FAILED:", json.dumps(js, ensure_ascii=False)[:2000], file=sys.stderr)
        return 1

    output = js.get("output") or js
    if isinstance(output, dict) and output.get("error"):
        print("worker error:", output["error"], file=sys.stderr)
        return 1
    if not isinstance(output, dict) or "audio_b64" not in output:
        print("unexpected response shape:", json.dumps(js)[:2000], file=sys.stderr)
        return 1

    audio = base64.b64decode(output["audio_b64"])
    args.output.write_bytes(audio)
    print(f"saved: {args.output} ({len(audio)} bytes)")
    print(f"  seed={output.get('seed')} total_to_decode={output.get('total_to_decode')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
