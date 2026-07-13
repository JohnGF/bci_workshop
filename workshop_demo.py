# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "brainflow",
#     "mne",
#     "numpy",
#     "scipy",
#     "setuptools<82",
# ]
# ///

import argparse
import time
import numpy as np
import mne
from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds


def main():
    # 1. Parse CLI arguments to toggle between Synthetic data and real Muse hardware
    parser = argparse.ArgumentParser(
        description="Neurotech Workshop Prototyping Script"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Attempt to connect to a real Muse device via BLE instead of Synthetic mode",
    )
    args = parser.parse_args()

    # 2. Select the Board ID
    if args.live:
        # BoardIds.MUSE_2_BOARD handles both Muse 2 and standard Muse S
        board_id = BoardIds.MUSE_2_BOARD.value
        print(
            "⚡ Mode: Live Muse Headset via BLE. Ensure Bluetooth is on and device is pairing."
        )
    else:
        board_id = BoardIds.SYNTHETIC_BOARD.value
        print("🤖 Mode: Synthetic Data Generator (No physical hardware required).")

    # 3. Configure streaming parameters
    params = BrainFlowInputParams()
    board = BoardShim(board_id, params)

    try:
        # 4. Spin up the hardware abstraction layer session
        print("🔄 Preparing session and opening stream...")
        board.prepare_session()
        board.start_stream()

        # --- ROBUST BUFFER CHECK FOR LIVE HARDWARE ---
        print("⏳ Waiting for data stream to initialize and fill buffer...")
        max_attempts = 10
        raw_data = np.empty((0, 0))

        for attempt in range(max_attempts):
            time.sleep(1)  # Accumulate packets second by second
            raw_data = board.get_current_board_data(1000)
            if raw_data.size > 0 and raw_data.shape[1] > 0:
                print(f"📥 Successfully captured buffer history after {attempt + 1}s!")
                break
            print(
                f"   [Attempt {attempt + 1}/{max_attempts}] Buffer still empty, waiting..."
            )

        if raw_data.size == 0 or raw_data.shape[1] == 0:
            raise RuntimeError(
                "Timeout: Hardware streaming started but no data packets were received."
            )
        # ---------------------------------------------

        # 5. Extract metadata details using specific Board IDs
        eeg_channels = BoardShim.get_eeg_channels(board_id)
        eeg_names = BoardShim.get_eeg_names(board_id)
        sfreq = BoardShim.get_sampling_rate(board_id)

        print(f"📊 Sampling Rate: {sfreq} Hz")
        print(f"🔌 Active EEG Channels indices: {eeg_channels} ({eeg_names})")
        print(f"📐 Data matrix shape: {raw_data.shape}")

        # 6. Isolate the EEG data rows and scale to Volts (BrainFlow returns uV; MNE expects Volts)
        eeg_data = raw_data[eeg_channels, :] / 1_000_000.0

        # 7. Package the matrix into an MNE Raw object for signal analysis pipelines
        print("\n🧠 Injecting matrix into MNE RawArray...")
        info = mne.create_info(
            ch_names=eeg_names, sfreq=sfreq, ch_types=["eeg"] * len(eeg_channels)
        )
        raw_mne = mne.io.RawArray(eeg_data, info, verbose=False)

        # 8. Compute Power Spectral Density (PSD) as an explicit sanity check
        print("📈 Computing Welch PSD for Alpha Band (8-12 Hz)...")
        psd_spectrum = raw_mne.compute_psd(
            method="welch", fmin=8, fmax=12, verbose=False
        )
        psds, freqs = psd_spectrum.get_data(return_freqs=True)

        print(
            f"✅ Success! Mean Alpha Power across channels: {np.mean(psds):.4e} V²/Hz"
        )

    except Exception as e:
        print(f"❌ Error encountered: {e}")

    finally:
        # 9. Ensure cleanly tear down low-level handles even if execution fails
        if board.is_prepared():
            print("\n🛑 Closing stream and releasing session cleanly.")
            board.stop_stream()
            board.release_session()


if __name__ == "__main__":
    main()
