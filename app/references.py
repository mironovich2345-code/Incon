"""Библиотека видео-референсов.

Импортируются только файлы, которые вы загрузили сами (свои ролики или видео,
на хранение которых у вас есть основание). Приложение ничего не скачивает
из Instagram.

Пайплайн:
  1. ffprobe — длительность и разрешение;
  2. ffmpeg scene detection — моменты склеек и ключевые кадры;
  3. извлечение аудио (16 кГц mono WAV);
  4. транскрипция: faster-whisper (если установлен) или загруженные .srt/.vtt/.txt;
  5. автоматический разбор структуры (хук, темп монтажа, плотность речи);
  6. по желанию — разбор Claude по кадрам и транскрипту.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

MAX_KEYFRAMES = 24
SCENE_THRESHOLD = 0.3


class MediaToolError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    except FileNotFoundError as exc:
        raise MediaToolError(f"Не найден {cmd[0]}. Установите ffmpeg.") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(f"{cmd[0]} завершился с ошибкой: {exc.stderr[-500:]}") from exc


def probe(video: Path) -> dict:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height:format=duration", "-of", "json", str(video)])
    data = json.loads(out.stdout)
    stream = (data.get("streams") or [{}])[0]
    return {
        "duration_sec": float(data.get("format", {}).get("duration") or 0) or None,
        "width": stream.get("width"),
        "height": stream.get("height"),
    }


def detect_cuts(video: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Таймкоды смены сцены (склейки) по детектору ffmpeg."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video), "-filter:v", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise MediaToolError(f"ffmpeg scene detection: {proc.stderr[-500:]}")
    return [round(float(t), 2) for t in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]


def extract_frame(video: Path, t: float, out: Path) -> None:
    _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
          "-vf", "scale=480:-2", "-q:v", "4", str(out)])


def choose_keyframe_times(duration: float | None, cuts: list[float]) -> list[tuple[float, str]]:
    """Кадры: старт, 1 и 3 секунды (хук), каждая склейка, финал. Не более MAX_KEYFRAMES."""
    times: list[tuple[float, str]] = [(0.0, "start"), (1.0, "hook"), (3.0, "hook")]
    times += [(c + 0.05, "cut") for c in cuts]
    if duration:
        times.append((max(duration - 0.5, 0), "end"))
        times = [(t, k) for t, k in times if t < duration]
    times.sort()
    dedup: list[tuple[float, str]] = []
    for t, k in times:
        if not dedup or t - dedup[-1][0] >= 0.4:
            dedup.append((t, k))
    if len(dedup) > MAX_KEYFRAMES:
        step = len(dedup) / MAX_KEYFRAMES
        dedup = [dedup[int(i * step)] for i in range(MAX_KEYFRAMES)]
    return dedup


def extract_audio(video: Path, out: Path) -> Path:
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(out)])
    return out


def transcribe_whisper(audio: Path, model_size: str = "small") -> tuple[str, list[dict]]:
    from faster_whisper import WhisperModel  # импорт по требованию: тяжёлая зависимость

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _info = model.transcribe(str(audio), language="ru", vad_filter=True)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    return " ".join(s["text"] for s in segs), segs


_TS = r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"


def _ts(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(text: str) -> tuple[str, list[dict]]:
    """SRT / VTT -> (полный текст, сегменты). Обычный .txt -> текст без таймкодов."""
    segs: list[dict] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", ""))
    for b in blocks:
        m = re.search(_TS + r"\s*-->\s*" + _TS, b)
        if not m:
            continue
        g = m.groups()
        body = b[m.end():].strip()
        body = re.sub(r"<[^>]+>", "", body).replace("\n", " ").strip()
        if body:
            segs.append({"start": round(_ts(*g[:4]), 2), "end": round(_ts(*g[4:]), 2), "text": body})
    if segs:
        return " ".join(s["text"] for s in segs), segs
    clean = text.replace("WEBVTT", "").strip()
    return clean, []


def analyze_structure(duration: float | None, cuts: list[float], transcript: str | None, segments: list[dict]) -> dict:
    """Автоматический разбор. Только то, что можно измерить; остальное — None."""
    words = len(re.findall(r"\w+", transcript or ""))
    first3 = " ".join(s["text"] for s in segments if s["start"] < 3.0) if segments else None
    shots = [0.0] + cuts + ([duration] if duration else [])
    shot_lengths = [round(b - a, 2) for a, b in zip(shots, shots[1:]) if b > a]
    res = {
        "duration_sec": duration,
        "cuts_count": len(cuts),
        "cuts_per_10s": round(len(cuts) / duration * 10, 2) if duration else None,
        "first_cut_sec": cuts[0] if cuts else None,
        "avg_shot_sec": round(sum(shot_lengths) / len(shot_lengths), 2) if shot_lengths else None,
        "words": words or None,
        "words_per_min": round(words / duration * 60) if duration and words else None,
        "hook_text_first_3s": first3 or None,
        "speech_starts_sec": segments[0]["start"] if segments else None,
        "has_timed_transcript": bool(segments),
        # Не измеряем автоматически: текст на экране (нужен OCR), музыка, B-roll.
        "not_measured": ["текст на экране", "музыка/звуковые эффекты", "тип планов (крупный/средний)", "B-roll"],
    }
    if duration and segments:
        third = duration / 3
        parts = {"начало": [], "середина": [], "финал": []}
        for s in segments:
            key = "начало" if s["start"] < third else "середина" if s["start"] < 2 * third else "финал"
            parts[key].append(s["text"])
        res["beats"] = {k: " ".join(v)[:400] for k, v in parts.items()}
    return res


def process_video(video: Path, workdir: Path, transcript_upload: str | None = None, use_whisper: bool = True) -> dict:
    """Полный пайплайн. Возвращает поля для записи в references_lib."""
    workdir.mkdir(parents=True, exist_ok=True)
    result: dict = {"warnings": []}
    meta = probe(video)
    result.update(meta)
    try:
        cuts = detect_cuts(video)
    except MediaToolError as exc:
        cuts = []
        result["warnings"].append(str(exc))
    result["cuts"] = cuts

    frames_dir = workdir / "frames"
    frames_dir.mkdir(exist_ok=True)
    keyframes = []
    for idx, (t, kind) in enumerate(choose_keyframe_times(meta["duration_sec"], cuts)):
        out = frames_dir / f"{idx:02d}_{t:06.2f}.jpg"
        try:
            extract_frame(video, t, out)
            if out.exists():
                keyframes.append({"t": round(t, 2), "kind": kind, "path": str(out)})
        except MediaToolError as exc:
            result["warnings"].append(str(exc))
    result["keyframes"] = keyframes

    transcript, segments, source = None, [], "none"
    if transcript_upload:
        transcript, segments = parse_subtitles(transcript_upload)
        source = "upload"
    elif use_whisper and whisper_available():
        audio = extract_audio(video, workdir / "audio.wav")
        transcript, segments = transcribe_whisper(audio)
        source = "whisper"
    else:
        result["warnings"].append("Транскрипт не создан: faster-whisper не установлен и субтитры не загружены.")
    result.update({"transcript": transcript, "transcript_segments": segments, "transcript_source": source})
    result["structure"] = analyze_structure(meta["duration_sec"], cuts, transcript, segments)
    return result
