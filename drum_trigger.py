# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "numpy",
#     "scipy",
#     "pygame"
# ]
# ///

import sys
import time
import numpy as np
import pygame
from scipy.signal import butter, sosfilt
from pylsl import StreamInlet, resolve_byprop


def main():
    # 1. Initialize Audio Engine
    pygame.mixer.pre_init(44100, -16, 2, 512)  # Low latency audio config
    pygame.init()

    try:
        # REPLACE THIS with a path to a real drum sample (.wav) on your machine
        drum_sound = pygame.mixer.Sound("kick_drum.wav")
    except FileNotFoundError:
        print(
            "⚠️ Warning: 'kick_drum.wav' not found. Running in visual-only terminal mode."
        )
        drum_sound = None

    # 2. Hook the LSL Network
    print("🔍 Hooking LSL network stream (Muse_Athena)...")
    streams = resolve_byprop("name", "Muse_Athena", timeout=5)
    if not streams:
        print("❌ Stream not found. Ensure athena_streamer.py is running.")
        sys.exit(1)

    inlet = StreamInlet(streams[0])
    sfreq = int(inlet.info().nominal_srate())

    # 3. DSP Configuration
    # Highpass filter at 70Hz to isolate EMG (muscle) from EEG (brain)
    sos = butter(4, 70.0, btype="highpass", fs=sfreq, output="sos")

    win_samples = int(sfreq * 0.25)  # 250ms rolling window
    buffer = np.zeros(win_samples)

    # Thresholding parameters (Adjust these based on the user's baseline)
    TRIGGER_THRESHOLD = 150.0
    COOLDOWN_SEC = 0.3
    last_trigger_time = 0

    print("🥁 Neuromuscular Drum Kit Live. Clench your jaw to play!")

    try:
        while True:
            samples, _ = inlet.pull_chunk(max_samples=50)
            if not samples:
                time.sleep(0.01)
                continue

            data = np.array(samples)
            num_new = len(data)

            # We use TP9 (Index 0) as it sits right over the temporalis muscle (jaw)
            buffer[:-num_new] = buffer[num_new:]
            buffer[-num_new:] = data[:, 0]

            # A. High-pass filter to isolate muscle noise
            emg_noise = sosfilt(sos, buffer)

            # B. Calculate RMS energy envelope of the most recent 100ms
            recent_emg = emg_noise[-int(sfreq * 0.1) :]
            rms_energy = np.sqrt(np.mean(recent_emg**2))

            # C. Trigger Logic
            current_time = time.time()
            if (
                rms_energy > TRIGGER_THRESHOLD
                and (current_time - last_trigger_time) > COOLDOWN_SEC
            ):
                print(f"[{current_time:.2f}] 💥 DRUM HIT! (Energy: {rms_energy:.1f})")
                if drum_sound:
                    drum_sound.play()
                last_trigger_time = current_time

    except KeyboardInterrupt:
        print("\n🛑 Shutting down drum trigger...")
        pygame.quit()


if __name__ == "__main__":
    main()
