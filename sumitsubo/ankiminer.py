"""Calls to the pinned Anki Miner's `--api` commands, as a separate process.

Used for the heavy ingest work: downloading a video, transcribing speech, and reading books
(EPUB, PDF, text, web articles) into sentence-sized units. None of these commands talk to Anki.
Anki Miner keeps its own settings and downloaded models under <data>/anki-miner.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config


class AnkiMinerError(RuntimeError):
    pass


def _exe() -> list[str]:
    beside = Path(sys.executable).with_name("anki-miner")
    found = str(beside) if beside.exists() else shutil.which("anki-miner")
    if not found:
        raise AnkiMinerError("Anki Miner's command (anki-miner) was not found. Reinstall Sumitsubo's dependencies.")
    return [found]


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["ANKI_MINER_HOME"] = str(config.data_dir() / "anki-miner")
    Path(env["ANKI_MINER_HOME"]).mkdir(parents=True, exist_ok=True)
    return env


def call(command: str, *args: str, timeout: int | None = None) -> dict:
    """Run one --api command and return its verdict (the single JSON line it prints)."""
    proc = subprocess.run(_exe() + ["--api", command, *args], capture_output=True, text=True, env=_env(), timeout=timeout)
    line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith("{")), None)
    if not line:
        raise AnkiMinerError(f"Anki Miner gave no answer to '{command}' (exit {proc.returncode}): {proc.stderr[-300:]}")
    return json.loads(line)


def _run_file(command: str, work: Path, body: dict, run_id: str, timeout: int | None = None) -> dict:
    """Write a request file, run the command, and return the run's result file contents."""
    work.mkdir(parents=True, exist_ok=True)
    req = work / f"{command}-request.json"
    req.write_text(json.dumps(dict(schema=1, language="ja", run_dir=str(work), **body), ensure_ascii=False), encoding="utf-8")
    verdict = call(command, str(req), timeout=timeout)
    if not verdict.get("ok"):
        runs = verdict.get("runs") or [{}]
        raise AnkiMinerError(f"{command}: {verdict.get('error') or runs[0].get('error')}: "
                             f"{verdict.get('message') or runs[0].get('message')}")
    results = sorted((work / run_id).glob(f"{command}-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    return json.loads(results[-1].read_text(encoding="utf-8")) if results else verdict


def version() -> str:
    return call("version", timeout=120)["result"]["app"]


def units(path: str | Path, work: Path) -> tuple[list[dict], dict]:
    """Sentence-sized units of a book, text file, PDF, EPUB or web article: [{text, location}], info."""
    work.mkdir(parents=True, exist_ok=True)
    req = work / "inspect-request.json"
    req.write_text(json.dumps(dict(schema=1, language="ja", run_dir=str(work),
                                   sources=[dict(path=str(path), units=True, run_id="src")]), ensure_ascii=False), encoding="utf-8")
    verdict = call("inspect", str(req), timeout=1800)
    src = (verdict.get("result") or {}).get("sources", [{}])[0]
    if not verdict.get("ok") or src.get("error") or not src.get("file"):
        raise AnkiMinerError(f"Could not read the source: {src.get('error') or verdict.get('error')}: "
                             f"{src.get('message') or verdict.get('message')}")
    data = json.loads((work / "src" / src["file"]).read_text(encoding="utf-8"))
    return [dict(text=u["text"], location=u.get("location", "")) for u in data["units"]], src


def fetch(url: str, work: Path, subtitle_source: str = "auto") -> dict:
    """Download one video with a subtitle (its captions, or a transcription). Returns the fetch result:
    video_file, subtitle_file, sub_source, title, duration."""
    return _run_file("fetch", work, dict(episodes=[dict(run_id="src", youtube_url=url,
                                                        youtube_subtitle_source=subtitle_source)]), "src")


def transcribe(media: str | Path, work: Path) -> str | None:
    """Transcribe a local audio or video file. Returns the subtitle file path, or None when there is no speech."""
    key = "audio_file" if Path(media).suffix.lower() in {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"} else "video_file"
    res = _run_file("transcribe", work, dict(episodes=[{"run_id": "src", key: str(media)}]), "src")
    return res.get("subtitle_file")


def setup_speech(model: str, progress: Path) -> dict:
    """Install the speech engine and model (about 3 GB for large-v3). Long-running."""
    progress.parent.mkdir(parents=True, exist_ok=True)
    return call("setup", "--language", "ja", "--progress", str(progress), "--speech-model", "--model", model)
