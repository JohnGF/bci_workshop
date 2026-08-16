# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "brainflow",
#     "pylsl",
#     "numpy"
# ]
# ///

import logging
import logging.handlers
import os
import time
import numpy as np
from pylsl import StreamInfo, StreamOutlet, cf_float32
from brainflow.board_shim import (
    BoardShim,
    BrainFlowInputParams,
    BoardIds,
    BrainFlowPresets,
    BrainFlowError,
)

# ==========================================
# CONNECTION LOGGER  (connections.log)
# ==========================================
_log_dir = os.path.dirname(os.path.abspath(__file__))
_log_path = os.path.join(_log_dir, "connections.log")

conn_logger = logging.getLogger("connections")
if not conn_logger.handlers:  # avoid duplicate handlers if imported elsewhere
    conn_logger.setLevel(logging.DEBUG)
    _handler = logging.handlers.RotatingFileHandler(
        _log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    conn_logger.addHandler(_handler)


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

    # --- 1. Extract Hardware Metadata ---
    if args.sim:
        preset_opt = None
        eeg_channels = BoardShim.get_eeg_channels(board_id, preset_eeg)[:4]
        sfreq_eeg = BoardShim.get_sampling_rate(board_id, preset_eeg)
        sfreq_opt = 64
        opt_channel_count = 9
        opt_slice_indices = list(range(9))
        battery_channel = None
    else:
        preset_opt = BrainFlowPresets.ANCILLARY_PRESET.value
        eeg_channels = BoardShim.get_eeg_channels(board_id, preset_eeg)
        sfreq_eeg = BoardShim.get_sampling_rate(board_id, preset_eeg)
        try:
            sfreq_opt = BoardShim.get_sampling_rate(board_id, preset_opt)
            battery_channel = BoardShim.get_battery_channel(board_id, preset_opt)
            print(f"🔋 Battery channel index found: {battery_channel}")
        except BrainFlowError as e:
            print(f"⚠️ Ancillary preset (Optics/Battery) rate error: {e}")
            sfreq_opt = 64
            battery_channel = None

        try:
            opt_slice_indices = BoardShim.get_optical_channels(board_id, preset_opt)
        except BrainFlowError:
            try:
                opt_slice_indices = BoardShim.get_ppg_channels(board_id, preset_opt)
            except BrainFlowError:
                opt_slice_indices = list(range(1, 10))
        if not opt_slice_indices:
            opt_slice_indices = list(range(1, 10))
        opt_channel_count = len(opt_slice_indices)

    import uuid
    session_uid = uuid.uuid4().hex[:12]

    # --- 2. Initialize Persistent LSL Outlets ---
    print("📡 Initializing LSL Network Nodes...")

    opt_uid = f"muse_optics_{session_uid}"
    info_opt = StreamInfo(
        "Muse_Optics",
        "fNIRS",
        opt_channel_count,
        sfreq_opt,
        cf_float32,
        opt_uid,
    )
    outlet_opt = StreamOutlet(info_opt)
    print(f"[{time.strftime('%H:%M:%S')}] ✅ Muse_Optics outlet registered   uid={opt_uid}  ch={opt_channel_count}  rate={sfreq_opt}Hz")

    eeg_uid = f"muse_athena_{session_uid}"
    info_eeg = StreamInfo(
        "Muse_Athena",
        "EEG",
        len(eeg_channels),
        sfreq_eeg,
        cf_float32,
        eeg_uid,
    )
    outlet_eeg = StreamOutlet(info_eeg)
    print(f"[{time.strftime('%H:%M:%S')}] ✅ Muse_Athena outlet registered  uid={eeg_uid}  ch={len(eeg_channels)}  rate={sfreq_eeg}Hz")

    # --- Self-verify: confirm both streams are resolvable on the LSL network ---
    from pylsl import resolve_byprop as _resolve
    for stream_name in ("Muse_Optics", "Muse_Athena"):
        for attempt in range(3):
            found = _resolve("name", stream_name, timeout=2.0)
            if found:
                print(f"[{time.strftime('%H:%M:%S')}] ✅ Network verify OK: {stream_name} visible to resolvers")
                break
            else:
                print(f"[{time.strftime('%H:%M:%S')}] ⚠️  Network verify attempt {attempt+1}/3: {stream_name} not yet visible, waiting...")
                time.sleep(1.0)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] ❌ WARNING: {stream_name} could not be verified on the LSL network after 3 attempts.")
            print("   Clients may fail to connect. Check firewall/multicast settings.")

    # --- 3. The Fault-Tolerant State Machine ---
    timeout_threshold = 5.0

    while True:
        try:
            print("\n🔄 Connecting to BLE Hardware...")
            board.prepare_session()
            board.start_stream()
            session_start = time.monotonic()
            conn_logger.info(f"CONNECT    device=Muse_BLE  board_id={board_id}")
            print("✅ Hardware connected! Streaming live.")

            last_data_time = time.time()
            last_battery_val = None
            last_battery_print_time = 0
            eeg_started = False
            opt_started = False

            # --- Inner Loop: Data Pump ---
            connection_grace_period = 15.0  # Give Muse S 15 seconds after BLE connection to start sending first packet
            while True:
                data_pulled = False

                # 1. Poll EEG
                try:
                    count_eeg = board.get_board_data_count(preset_eeg)
                    if count_eeg > 0:
                        eeg_matrix = board.get_board_data(count_eeg, preset_eeg)
                        if not eeg_started:
                            conn_logger.info(f"STREAM_START  device=Muse_BLE  stream=EEG  shape={eeg_matrix.shape}")
                            print(f"📥 EEG stream active. Packet shape: {eeg_matrix.shape}")
                            eeg_started = True
                        outlet_eeg.push_chunk(eeg_matrix[eeg_channels, :].T.tolist())
                        data_pulled = True
                except BrainFlowError as e:
                    conn_logger.error(f"EEG_POLL_ERROR device=Muse_BLE error={e}")

                # 2. Poll Optics
                if args.sim:
                    if data_pulled:
                        num_opt_samples = max(1, int(round(eeg_matrix.shape[1] * (sfreq_opt / sfreq_eeg))))
                        sim_t = time.time() + np.linspace(0, num_opt_samples / sfreq_opt, num_opt_samples, endpoint=False)
                        hr_freq = 1.2  # ~72 BPM
                        pulse = 500.0 + 150.0 * np.sin(2 * np.pi * hr_freq * sim_t) + 45.0 * np.sin(4 * np.pi * hr_freq * sim_t)
                        noise = np.random.normal(0, 5.0, (opt_channel_count, num_opt_samples))
                        sim_opt = np.tile(pulse, (opt_channel_count, 1)) + noise
                        outlet_opt.push_chunk(sim_opt.T.tolist())
                        if not opt_started:
                            conn_logger.info(f"STREAM_START  device=Muse_SIM  stream=Optics  shape={sim_opt.shape}")
                            print(f"📥 Simulated Optics stream active. Packet shape: {sim_opt.shape}")
                            opt_started = True
                elif preset_opt is not None:
                    try:
                        count_opt = board.get_board_data_count(preset_opt)
                        if count_opt > 0:
                            opt_data = board.get_board_data(count_opt, preset_opt)
                            if not opt_started:
                                conn_logger.info(f"STREAM_START  device=Muse_BLE  stream=Optics  shape={opt_data.shape}")
                                print(f"📥 Optics & Battery stream active. Packet shape: {opt_data.shape}")
                                opt_started = True
                            
                            valid_indices = [idx for idx in opt_slice_indices if idx < opt_data.shape[0]]
                            if valid_indices:
                                outlet_opt.push_chunk(opt_data[valid_indices, :].T.tolist())
                            
                            if battery_channel is not None and battery_channel < opt_data.shape[0] and opt_data.shape[1] > 0:
                                current_time = time.time()
                                latest_battery = int(opt_data[battery_channel, -1])
                                if (latest_battery != last_battery_val) or (current_time - last_battery_print_time > 30):
                                    print(f"🔋 Headset Battery Level: {latest_battery}%")
                                    last_battery_val = latest_battery
                                    last_battery_print_time = current_time
                                    
                            data_pulled = True
                    except BrainFlowError as e:
                        conn_logger.error(f"OPT_POLL_ERROR device=Muse_BLE error={e}")

                # 3. Connection Watchdog
                current_time = time.time()
                if data_pulled:
                    last_data_time = current_time
                else:
                    # Apply extended grace period right after BLE connection before first data packet arrives
                    effective_timeout = connection_grace_period if not (eeg_started or opt_started) else timeout_threshold
                    if (current_time - last_data_time) > effective_timeout:
                        duration = current_time - (session_start if 'session_start' in dir() else current_time)
                        conn_logger.warning(f"TIMEOUT    device=Muse_BLE  no_data_for={effective_timeout}s  session_duration={duration:.1f}s")
                        print(f"❌ BLE Timeout: No data received for {int(effective_timeout)} seconds.")
                        break

                time.sleep(0.02)

        except BrainFlowError as e:
            conn_logger.error(f"ERROR      device=Muse_BLE  error={e}")
            print(f"⚠️ Connection failure: {e}")

        except KeyboardInterrupt:
            conn_logger.info("SHUTDOWN   device=Muse_BLE  reason=KeyboardInterrupt")
            print("\n🛑 Terminating server manually...")
            break

        finally:
            if board.is_prepared():
                duration = time.monotonic() - session_start if 'session_start' in dir() else 0
                conn_logger.info(f"DISCONNECT device=Muse_BLE  reason=cleanup  session_duration={duration:.1f}s")
                print("🧹 Cleaning up broken hardware session...")
                try:
                    board.stop_stream()
                except BrainFlowError:
                    pass
                board.release_session()

            conn_logger.info("RETRY      device=Muse_BLE  retry_in=3s")
            print("⏳ Retrying connection in 3 seconds...")
            time.sleep(3)


if __name__ == "__main__":
    main()
