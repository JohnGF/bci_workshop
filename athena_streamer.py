# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "brainflow",
#     "pylsl",
#     "numpy"
# ]
# ///

import time
from pylsl import StreamInfo, StreamOutlet, cf_float32
from brainflow.board_shim import (
    BoardShim,
    BrainFlowInputParams,
    BoardIds,
    BrainFlowPresets,
    BrainFlowError,
)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Muse Athena LSL Streamer")
    parser.add_argument("--sim", action="store_true", help="Simulate Muse S using BrainFlow's Synthetic Board")
    args, _ = parser.parse_known_args()

    if args.sim:
        board_id = BoardIds.SYNTHETIC_BOARD.value
        print("🤖 Mode: Synthetic Data Simulation (No Muse hardware required)")
    else:
        board_id = BoardIds.MUSE_S_ATHENA_BOARD.value
        print("⚡ Mode: Live Muse Headset via BLE")

    params = BrainFlowInputParams()
    board = BoardShim(board_id, params)

    preset_eeg = BrainFlowPresets.DEFAULT_PRESET.value
    preset_opt = BrainFlowPresets.ANCILLARY_PRESET.value

    # --- 1. Extract Hardware Metadata ---
    eeg_channels = BoardShim.get_eeg_channels(board_id, preset_eeg)
    sfreq_eeg = BoardShim.get_sampling_rate(board_id, preset_eeg)

    # Ancillary presets (Optics & Battery) might not exist on Synthetic boards
    try:
        sfreq_opt = BoardShim.get_sampling_rate(board_id, preset_opt)
        opt_channel_count = 9
        opt_slice_indices = list(range(1, 10))
        battery_channel = BoardShim.get_battery_channel(board_id, preset_opt)
        print(f"🔋 Battery channel index found: {battery_channel}")
    except BrainFlowError as e:
        print(f"⚠️ Ancillary preset (Optics/Battery) not fully supported on this board: {e}")
        sfreq_opt = sfreq_eeg
        opt_channel_count = 9
        opt_slice_indices = list(range(0, 9))  # mock slice indices
        battery_channel = None

    # --- 2. Initialize Persistent LSL Outlets ---
    print("📡 Initializing LSL Network Nodes...")

    info_eeg = StreamInfo(
        "Muse_Athena",
        "EEG",
        len(eeg_channels),
        sfreq_eeg,
        cf_float32,
        "muse_athena_uid123",
    )
    outlet_eeg = StreamOutlet(info_eeg)

    info_opt = StreamInfo(
        "Muse_Optics",
        "fNIRS",
        opt_channel_count,
        sfreq_opt,
        cf_float32,
        "muse_athena_opt_uid123",
    )
    outlet_opt = StreamOutlet(info_opt)

    # --- 3. The Fault-Tolerant State Machine ---
    timeout_threshold = 5.0

    while True:
        try:
            print("\n🔄 Connecting to BLE Hardware...")
            board.prepare_session()
            board.start_stream()
            print("✅ Hardware connected! Streaming live.")

            last_data_time = time.time()
            last_battery_val = None
            last_battery_print_time = 0
            eeg_started = False
            opt_started = False

            # --- Inner Loop: Data Pump ---
            while True:
                data_pulled = False

                # 1. Poll EEG
                try:
                    if board.get_board_data_count(preset_eeg) > 0:
                        eeg_matrix = board.get_board_data(preset=preset_eeg)
                        if not eeg_started:
                            print(f"📥 EEG stream active. Packet shape: {eeg_matrix.shape}")
                            eeg_started = True
                        outlet_eeg.push_chunk(eeg_matrix[eeg_channels, :].T.tolist())
                        data_pulled = True
                except BrainFlowError:
                    pass

                # 2. Poll Optics
                try:
                    if board.get_board_data_count(preset_opt) > 0:
                        opt_data = board.get_board_data(preset=preset_opt)
                        if not opt_started:
                            print(f"📥 Optics & Battery stream active. Packet shape: {opt_data.shape}")
                            opt_started = True
                        outlet_opt.push_chunk(opt_data[opt_slice_indices, :].T.tolist())
                        
                        if battery_channel is not None and opt_data.shape[1] > 0:
                            current_time = time.time()
                            latest_battery = int(opt_data[battery_channel, -1])
                            if (latest_battery != last_battery_val) or (current_time - last_battery_print_time > 30):
                                print(f"🔋 Headset Battery Level: {latest_battery}%")
                                last_battery_val = latest_battery
                                last_battery_print_time = current_time
                                
                        data_pulled = True
                except BrainFlowError:
                    pass

                # 3. Connection Watchdog
                current_time = time.time()
                if data_pulled:
                    last_data_time = current_time
                elif (current_time - last_data_time) > timeout_threshold:
                    print("❌ BLE Timeout: No data received for 5 seconds.")
                    break

                time.sleep(0.02)

        except BrainFlowError as e:
            print(f"⚠️ Connection failure: {e}")

        except KeyboardInterrupt:
            print("\n🛑 Terminating server manually...")
            break

        finally:
            if board.is_prepared():
                print("🧹 Cleaning up broken hardware session...")
                try:
                    board.stop_stream()
                except BrainFlowError:
                    pass
                board.release_session()

            print("⏳ Retrying connection in 3 seconds...")
            time.sleep(3)


if __name__ == "__main__":
    main()
