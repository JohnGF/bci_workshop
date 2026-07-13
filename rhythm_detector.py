# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "brainflow",
#     "numpy",
#     "scipy"
# ]
# ///

import time
import argparse
import numpy as np
from scipy import signal
from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds


def main():
    parser = argparse.ArgumentParser(description="Pure CPython ASSR Rhythm Detection")
    parser.add_argument(
        "--bpm", type=float, default=120.0, help="Target rhythm in Beats Per Minute"
    )
    args = parser.parse_args()

    # 1. Calculate target frequency from BPM
    target_hz = args.bpm / 60.0
    print(f"🎵 Target Rhythm: {args.bpm} BPM ({target_hz:.2f} Hz)")

    # 2. Initialize Muse S (Athena) backend
    board_id = BoardIds.MUSE_S_ATHENA_BOARD.value
    params = BrainFlowInputParams()
    board = BoardShim(board_id, params)

    try:
        print("🔄 Connecting to hardware and establishing BLE socket...")
        board.prepare_session()
        board.start_stream()

        # We require a long buffer for high-resolution low-frequency bins.
        # 10 seconds of data gives us a 0.1 Hz frequency resolution matrix.
        buffer_seconds = 10
        print(
            f"⏳ Collecting {buffer_seconds}s of baseline. PLAY SOUND NOW. DO NOT MOVE."
        )
        time.sleep(buffer_seconds)

        # 3. Pull telemetry matrix
        data = board.get_current_board_data(256 * buffer_seconds)

        # 4. Isolate Temporal Channels (TP9 and TP10)
        eeg_channels = BoardShim.get_eeg_channels(board_id)
        tp9_idx = eeg_channels[0]
        tp10_idx = eeg_channels[3]
        sfreq = BoardShim.get_sampling_rate(board_id)

        # Average the two temporal channels to boost the auditory signal-to-noise ratio
        temporal_data = (data[tp9_idx, :] + data[tp10_idx, :]) / 2.0

        # Detrend the signal to remove DC offset and slow baseline drift
        temporal_data = signal.detrend(temporal_data)

        # 5. Compute Welch's Power Spectral Density (PSD)
        # nperseg=sfreq*4 yields a sliding window with 0.25 Hz resolution
        freqs, psd = signal.welch(temporal_data, fs=sfreq, nperseg=sfreq * 4)

        # 6. Locate the target frequency bin
        target_idx = np.argmin(np.abs(freqs - target_hz))
        beat_power = psd[target_idx]

        # Calculate the surrounding noise floor (excluding the target peak)
        baseline_mask = (freqs >= 1.0) & (freqs <= 4.0)
        baseline_mask[target_idx] = False
        noise_floor = np.mean(psd[baseline_mask])

        snr = beat_power / noise_floor

        print("\n📊 --- CORTICAL TRACKING RESULTS ---")
        print(f"Target Frequency Power:  {beat_power:.4e} V²/Hz")
        print(f"Surrounding Noise Floor: {noise_floor:.4e} V²/Hz")
        print(f"Signal-to-Noise Ratio:   {snr:.2f}x")

        if snr > 2.0:
            print("✅ Positive Entrainment! The temporal lobe is tracking the rhythm.")
        else:
            print(
                "❌ No entrainment detected. The signal is buried in the noise floor."
            )

    except Exception as e:
        print(f"❌ Execution Error: {e}")

    finally:
        if board.is_prepared():
            board.stop_stream()
            board.release_session()


if __name__ == "__main__":
    main()
