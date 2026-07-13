# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "numpy"
# ]
# ///

import time
import sys
import select
import tty
import termios
import numpy as np
from pylsl import StreamInfo, StreamOutlet, cf_float32

def get_key_nonblocking():
    """Reads a single keypress from standard input without blocking."""
    if not sys.stdin.isatty():
        # Running as a subprocess (stdin is a pipe)
        rlist, _, _ = select.select([sys.stdin], [], [], 0.0)
        if rlist:
            char = sys.stdin.read(1)
            return char
        return None

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # Check if input is available
        rlist, _, _ = select.select([sys.stdin], [], [], 0.0)
        if rlist:
            char = sys.stdin.read(1)
            return char
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

def ppg_pulse_model(t, bpm):
    """
    Simulates a single PPG cardiac cycle using two Gaussian curves:
    one for the primary systolic peak, and one for the diastolic/dicrotic notch.
    """
    cardiac_period = 60.0 / bpm
    phase = (t % cardiac_period) / cardiac_period
    
    # Systolic peak (primary wave)
    systolic_center = 0.2
    systolic_width = 0.08
    systolic_amp = 1.0
    systolic = systolic_amp * np.exp(-((phase - systolic_center) ** 2) / (2 * systolic_width ** 2))
    
    # Diastolic peak (dicrotic notch/wave)
    diastolic_center = 0.45
    diastolic_width = 0.06
    diastolic_amp = 0.35
    diastolic = diastolic_amp * np.exp(-((phase - diastolic_center) ** 2) / (2 * diastolic_width ** 2))
    
    return systolic + diastolic

def main():
    # Stream details
    sfreq = 100 # 100 Hz sampling rate typical for raw smartwatch PPG
    channel_count = 2 # Channel 0: Green (HR), Channel 1: Red (SpO2/Reference)
    
    print("📡 Setting up LSL Smartwatch PPG Stream...")
    info = StreamInfo(
        "Smartwatch_PPG",
        "PPG",
        channel_count,
        sfreq,
        cf_float32,
        "smartwatch_ppg_sim_uid999"
    )
    
    # Add channel descriptions
    channels = info.desc().append_child("channels")
    ch_green = channels.append_child("channel")
    ch_green.append_child_value("label", "Green_LED")
    ch_green.append_child_value("type", "PPG")
    ch_red = channels.append_child("channel")
    ch_red.append_child_value("label", "Red_LED")
    ch_red.append_child_value("type", "PPG")
    
    outlet = StreamOutlet(info)
    print("✅ LSL Stream 'Smartwatch_PPG' is now active on the local network.")
    print("\nControls:")
    print("  [w] / [Arrow Up]   : Increase Heart Rate (+5 BPM)")
    print("  [s] / [Arrow Down] : Decrease Heart Rate (-5 BPM)")
    print("  [Space]            : Randomize Heart Rate")
    print("  [q] / [Ctrl+C]     : Exit")
    print("\nPress keys to control simulation...")

    # Simulation parameters
    base_bpm = 70.0
    current_bpm = 70.0
    respiration_rate = 15.0 # breaths per minute
    
    start_time = time.time()
    next_sample_time = start_time
    sample_interval = 1.0 / sfreq

    # Save original terminal settings to restore on exit if running in a TTY
    if sys.stdin.isatty():
        fd = sys.stdin.fileno()
        original_terminal_settings = termios.tcgetattr(fd)
    else:
        original_terminal_settings = None
    
    try:
        while True:
            # Check key presses (non-blocking)
            key = get_key_nonblocking()
            if key:
                key_lower = key.lower()
                if key_lower == 'q' or ord(key) == 3: # 'q' or Ctrl+C
                    print("\n🛑 Stopping simulator...")
                    break
                elif key_lower == 'w' or key == '\x1b': # 'w' or escape sequence (arrow keys)
                    # Check for arrow keys (which start with \x1b[)
                    if key == '\x1b':
                        # Read the remaining escape characters
                        r, _, _ = select.select([sys.stdin], [], [], 0.05)
                        if r:
                            seq = sys.stdin.read(2)
                            if seq == '[A': # Up Arrow
                                current_bpm = min(180.0, current_bpm + 5.0)
                            elif seq == '[B': # Down Arrow
                                current_bpm = max(40.0, current_bpm - 5.0)
                    else:
                        current_bpm = min(180.0, current_bpm + 5.0)
                    print(f"\r💓 Heart Rate Adjusted: {current_bpm:.1f} BPM   ", end="", flush=True)
                elif key_lower == 's':
                    current_bpm = max(40.0, current_bpm - 5.0)
                    print(f"\r💓 Heart Rate Adjusted: {current_bpm:.1f} BPM   ", end="", flush=True)
                elif key == ' ':
                    current_bpm = float(np.random.randint(55, 130))
                    print(f"\r🎲 Heart Rate Randomized: {current_bpm:.1f} BPM   ", end="", flush=True)

            # Keep execution synced to sample rate
            current_time = time.time()
            if current_time >= next_sample_time:
                t = current_time - start_time
                
                # 1. Simulate HRV (Heart Rate Variability) - high frequency fluctuations
                hrv_noise = np.sin(t * 1.5) * 1.5 + (np.random.randn() * 0.2)
                effective_bpm = current_bpm + hrv_noise
                
                # 2. Simulate Respiratory Sinus Arrhythmia (BPM goes up/down with breathing)
                resp_frequency = respiration_rate / 60.0
                resp_phase = 2 * np.pi * resp_frequency * t
                effective_bpm += np.sin(resp_phase) * 3.0
                
                # 3. Generate primary PPG pulse wave (Green Channel)
                green_pulse = ppg_pulse_model(t, effective_bpm)
                
                # Add baseline breathing drift
                baseline_drift = np.sin(resp_phase) * 0.15
                
                # Add sensor noise
                green_noise = np.random.randn() * 0.02
                green_val = green_pulse + baseline_drift + green_noise
                
                # 4. Generate secondary pulse wave (Red Channel - slightly shifted phase & lower amplitude)
                red_pulse = ppg_pulse_model(t - 0.05, effective_bpm) * 0.85
                red_noise = np.random.randn() * 0.03
                red_val = red_pulse + baseline_drift * 1.1 + red_noise
                
                # Push sample
                outlet.push_sample([float(green_val), float(red_val)])
                
                # Update next sample time
                next_sample_time += sample_interval
                
            # Sleep very briefly to reduce CPU load
            time.sleep(0.001)

    except KeyboardInterrupt:
        print("\n🛑 Stopping simulator...")
    finally:
        # Restore terminal settings
        if original_terminal_settings is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, original_terminal_settings)

if __name__ == "__main__":
    main()
