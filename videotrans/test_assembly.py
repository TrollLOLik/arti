import requests
import json
import time
from pathlib import Path
from dotenv import load_dotenv
import os

# Load API key from .env
load_dotenv(Path(__file__).parent / ".env")
API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
if not API_KEY:
    raise RuntimeError("ASSEMBLYAI_API_KEY not found in .env")

HEADERS = {"authorization": API_KEY}
AUDIO_PATH = Path(__file__).parent / "test.mp3"


def upload_audio() -> str:
    url = "https://api.assemblyai.com/v2/upload"
    with open(AUDIO_PATH, "rb") as f:
        response = requests.post(url, headers=HEADERS, data=f)
    response.raise_for_status()
    upload_url = response.json()["upload_url"]
    print(f"[1/3] Uploaded {AUDIO_PATH.name} -> {upload_url[:60]}...")
    return upload_url


def submit_transcript(upload_url: str) -> str:
    url = "https://api.assemblyai.com/v2/transcript"
    payload = {
        "audio_url": upload_url,
        "language_detection": True,
        "speech_models": ["universal-3-pro"],
        "speaker_labels": True,
        "temperature": 0,
    }
    response = requests.post(url, json=payload, headers=HEADERS)
    response.raise_for_status()
    transcript_id = response.json()["id"]
    print(f"[2/3] Submitted transcript request -> id={transcript_id}")
    return transcript_id


def poll_transcript(transcript_id: str) -> dict:
    url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    print("[3/3] Polling for completion...")
    while True:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        result = response.json()
        status = result.get("status")
        if status == "completed":
            print("[OK] Transcription completed!")
            return result
        elif status == "error":
            raise RuntimeError(f"Transcription failed: {result.get('error')}")
        else:
            print(f"    ...status={status}, waiting 3s")
            time.sleep(3)


def print_results(result: dict) -> None:
    print("\n" + "=" * 60)
    print("FULL JSON RESPONSE")
    print("=" * 60)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("\n" + "=" * 60)
    print("TEXT")
    print("=" * 60)
    print(result.get("text", "<no text>"))
    print("\n" + "=" * 60)
    print("WORDS / UTTERANCES")
    print("=" * 60)

    words = result.get("words", [])
    if words:
        for w in words:
            print(f"  [{w.get('start', 0)/1000:.2f}s - {w.get('end', 0)/1000:.2f}s] speaker={w.get('speaker', '?')} text='{w.get('text', '')}'")
    else:
        utterances = result.get("utterances", [])
        for u in utterances:
            print(f"  [{u.get('start', 0)/1000:.2f}s - {u.get('end', 0)/1000:.2f}s] speaker={u.get('speaker', '?')} text='{u.get('text', '')}'")

    print("\n" + "=" * 60)
    print("SPEAKERS COUNT")
    print("=" * 60)
    speakers = set()
    for w in words:
        speakers.add(w.get("speaker", "?"))
    for u in result.get("utterances", []):
        speakers.add(u.get("speaker", "?"))
    print(f"Detected speakers: {sorted(speakers)}")


if __name__ == "__main__":
    upload_url = upload_audio()
    transcript_id = submit_transcript(upload_url)
    result = poll_transcript(transcript_id)
    print_results(result)
