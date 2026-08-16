# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "faster-whisper",
#     "sounddevice",
#     "numpy"
# ]
# ///

"""
Whisper Voice Bridge
====================
Listens to the PC microphone using sounddevice, transcribes audio chunks
with faster-whisper (tiny model by default), maps keywords to numeric
command codes, and pushes them out on a dedicated LSL stream:

    Stream name : Voice_Commands
    Type        : Markers
    Channels    : 1  (float32 command code)
    Sample rate : irregular (event-driven)

Command codes (same as the old phone-side convention):
    left   →  99.0
    right  → -99.0
    fire   →  55.0
    stop   →  11.0
    <unknown keyword> → 1.0

Usage:
    uv run whisper_voice_bridge.py
    uv run whisper_voice_bridge.py --model base --device cpu --chunk 2.0
    uv run whisper_voice_bridge.py --list-devices
"""

import argparse
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from pylsl import StreamInfo, StreamOutlet, cf_float32

# ==========================================
# CONNECTION LOGGER  (connections.log)
# ==========================================
_log_dir = os.path.dirname(os.path.abspath(__file__))
_log_path = os.path.join(_log_dir, "connections.log")

conn_logger = logging.getLogger("connections")
if not conn_logger.handlers:
    conn_logger.setLevel(logging.DEBUG)
    _handler = logging.handlers.RotatingFileHandler(
        _log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    conn_logger.addHandler(_handler)

# Mirror to console too
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("whisper_bridge")

# ==========================================
# KEYWORD → COMMAND CODE MAP
# ==========================================
KEYWORD_MAP: dict[str, float] = {
    "left":      99.0,
    "right":    -99.0,
    "fire":      55.0,   # Supernova / Cleansing Blast
    "nova":      55.0,
    "supernova": 55.0,
    "bomb":      55.0,
    "missile":   66.0,   # Guided Homing Missiles
    "rocket":    66.0,
    "laser":     44.0,   # Primary Cannon
    "shoot":     44.0,
    "spread":    33.0,   # Secondary Double Spread
    "stop":      11.0,
    "pause":     11.0,
}

SAMPLE_RATE = 16_000   # Whisper expects 16 kHz mono


def list_audio_devices() -> None:
    print("\nAvailable audio input devices:")
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            print(f"  [{i:2d}] {d['name']}  (inputs: {d['max_input_channels']})")
    print()


def match_keywords(transcript: str) -> list[tuple[str, float]]:
    """Return all (keyword, code) pairs found in the transcript."""
    lower = transcript.lower()
    hits = []
    for word, code in KEYWORD_MAP.items():
        if word in lower:
            hits.append((word, code))
    return hits


def record_chunk(duration: float, device: int | None) -> np.ndarray:
    """Record `duration` seconds of mono 16 kHz audio and return as float32 array."""
    frames = int(SAMPLE_RATE * duration)
    audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=device)
    sd.wait()
    return audio.flatten()


def main() -> None:
    parser = argparse.ArgumentParser(description="PC-side Whisper voice bridge → LSL")
    parser.add_argument("--model",   default="tiny",  help="Whisper model size: tiny, base, small, medium (default: tiny)")
    parser.add_argument("--device",  default="cpu",   help="Compute device: cpu or cuda (default: cpu)")
    parser.add_argument("--chunk",   type=float, default=2.0, help="Audio chunk duration in seconds (default: 2.0)")
    parser.add_argument("--input-device", type=int, default=None, dest="input_device",
                        help="Sounddevice input device index (default: system default)")
    parser.add_argument("--list-devices", action="store_true", help="List available audio input devices and exit")
    parser.add_argument("--beam-size", type=int, default=1, help="Whisper beam size (1 = fastest greedy, default: 1)")
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        sys.exit(0)

    # --- Load Whisper model ---
    log.info(f"Loading faster-whisper model '{args.model}' on {args.device}...")
    try:
        model = WhisperModel(args.model, device=args.device, compute_type="int8")
    except Exception as e:
        log.error(f"Failed to load Whisper model: {e}")
        sys.exit(1)
    log.info("✅ Whisper model loaded.")
    conn_logger.info(f"WHISPER_READY  model={args.model}  device={args.device}")

    # --- Setup LSL outlet ---
    info = StreamInfo(
        name="Voice_Commands",
        type="Markers",
        channel_count=1,
        nominal_srate=0,          # irregular (event-driven)
        channel_format=cf_float32,
        source_id="whisper_voice_bridge_uid888",
    )
    ch_desc = info.desc().append_child("channels").append_child("channel")
    ch_desc.append_child_value("label", "VoiceCmd")
    ch_desc.append_child_value("type", "Marker")
    ch_desc.append_child_value("unit", "code")
    outlet = StreamOutlet(info)

    log.info("📡 LSL stream 'Voice_Commands' is live.")
    conn_logger.info("LSL_OUTLET  stream=Voice_Commands  source_id=whisper_voice_bridge_uid888")

    # --- Print keyword map ---
    print("\n" + "=" * 52)
    print("  🎙️  Whisper Voice Bridge  |  PC Microphone")
    print("=" * 52)
    print(f"  Model        : {args.model}")
    print(f"  Compute      : {args.device}")
    print(f"  Chunk size   : {args.chunk}s")
    print(f"  Input device : {args.input_device or 'system default'}")
    print("  Keywords     :")
    for kw, code in KEYWORD_MAP.items():
        print(f"    '{kw}' → {code}")
    print("=" * 52)
    print("  Listening... (Ctrl+C to stop)\n")

    total_commands = 0

    try:
        while True:
            # 1. Record a chunk
            try:
                audio = record_chunk(args.chunk, args.input_device)
            except Exception as e:
                log.warning(f"Audio capture error: {e}")
                time.sleep(0.5)
                continue

            # Skip near-silent chunks to avoid wasting Whisper cycles
            rms = float(np.sqrt(np.mean(audio ** 2)))
            if rms < 0.002:
                continue

            # 2. Transcribe
            try:
                segments, _ = model.transcribe(
                    audio,
                    beam_size=args.beam_size,
                    language="en",
                    vad_filter=True,          # skip silent segments
                )
                transcript = " ".join(s.text for s in segments).strip()
            except Exception as e:
                log.warning(f"Transcription error: {e}")
                conn_logger.error(f"WHISPER_ERROR  error={e}")
                continue

            if not transcript:
                continue

            log.info(f'🗣️  Heard: "{transcript}"')

            # 3. Match keywords and push to LSL
            hits = match_keywords(transcript)
            for keyword, code in hits:
                outlet.push_sample([code])
                total_commands += 1
                log.info(f'✅ Command: "{keyword}" → code={code}  (total sent: {total_commands})')
                conn_logger.info(f'VOICE_CMD  keyword="{keyword}"  code={code}  transcript="{transcript}"')

    except KeyboardInterrupt:
        conn_logger.info("SHUTDOWN  stream=Voice_Commands  reason=KeyboardInterrupt  total_commands_sent={total_commands}")
        print(f"\n🛑 Stopped. Total commands sent: {total_commands}")


if __name__ == "__main__":
    main()
