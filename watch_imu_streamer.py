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
        rlist, _, _ = select.select([sys.stdin], [], [], 0.0)
        if rlist:
            char = sys.stdin.read(1)
            return char
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

def main():
    sfreq = 100 # 100 Hz sampling rate
    channel_count = 6 # Acc X, Y, Z, Gyro X, Y, Z
    
    print("📡 Setting up LSL Smartwatch IMU Stream...")
    info = StreamInfo(
        "Smartwatch_IMU",
        "IMU",
        channel_count,
        sfreq,
        cf_float32,
        "smartwatch_imu_sim_uid888"
    )
    
    # Label channels
    channels = info.desc().append_child("channels")
    labels = ["Acc_X", "Acc_Y", "Acc_Z", "Gyro_X", "Gyro_Y", "Gyro_Z"]
    units = ["m/s^2", "m/s^2", "m/s^2", "rad/s", "rad/s", "rad/s"]
    for lbl, unit in zip(labels, units):
        ch = channels.append_child("channel")
        ch.append_child_value("label", lbl)
        ch.append_child_value("type", "IMU")
        ch.append_child_value("unit", unit)
        
    outlet = StreamOutlet(info)
    print("✅ LSL Stream 'Smartwatch_IMU' is now active.")
    print("\nMotion Patterns:")
    print("  [1] Stationary (Static + Noise)")
    print("  [2] Circle Movement")
    print("  [3] Figure-8 (Lissajous Trajectory)")
    print("  [4] Violent Shake / Tremor")
    print("  [q] / [Ctrl+C] Exit")
    print("\nPress keys to switch active movement patterns...")

    # Simulation state
    pattern = "1"
    start_time = time.time()
    next_sample_time = start_time
    sample_interval = 1.0 / sfreq
    
    # Base gravity
    g = 9.81
    
    # Save original terminal settings to restore on exit if running in a TTY
    if sys.stdin.isatty():
        fd = sys.stdin.fileno()
        original_terminal_settings = termios.tcgetattr(fd)
    else:
        original_terminal_settings = None
    
    try:
        while True:
            # Check key presses
            key = get_key_nonblocking()
            if key:
                key_lower = key.lower()
                if key_lower == 'q' or ord(key) == 3:
                    print("\n🛑 Stopping IMU simulator...")
                    break
                elif key in ["1", "2", "3", "4"]:
                    pattern = key
                    pattern_names = {
                        "1": "Stationary",
                        "2": "Circular",
                        "3": "Figure-8",
                        "4": "Shaking"
                    }
                    print(f"\r🔄 Active Pattern: {pattern_names[pattern]}       ", end="", flush=True)

            current_time = time.time()
            if current_time >= next_sample_time:
                t = current_time - start_time
                
                # Default resting state
                acc_x, acc_y, acc_z = 0.0, 0.0, g
                gyro_x, gyro_y, gyro_z = 0.0, 0.0, 0.0
                
                # Add motion based on patterns
                if pattern == "1":
                    # Stationary: minor micro-tremor noise only
                    pass
                elif pattern == "2":
                    # Circular translation in XY-plane
                    # Position: x = cos(2t), y = sin(2t)
                    # Velocity: vx = -2sin(2t), vy = 2cos(2t)
                    # Acceleration: ax = -4cos(2t), ay = -4sin(2t)
                    omega = 2.5
                    acc_x = -3.0 * np.cos(omega * t)
                    acc_y = -3.0 * np.sin(omega * t)
                    acc_z = g + np.sin(t * 4) * 0.5 # minor vertical bounce
                    
                    # Gyroscope rates: rotation around Z
                    gyro_z = omega
                    gyro_x = np.sin(t) * 0.1
                elif pattern == "3":
                    # Figure-8 (Lissajous) trajectory
                    # Position: x = sin(t), y = sin(2t)
                    # Accel: ax = -sin(t), ay = -4sin(2t)
                    omega = 2.0
                    acc_x = -2.0 * np.sin(omega * t)
                    acc_y = -6.0 * np.sin(2 * omega * t)
                    acc_z = g + np.cos(omega * t) * 1.5
                    
                    gyro_x = np.cos(omega * t) * 0.4
                    gyro_y = np.sin(2 * omega * t) * 0.8
                elif pattern == "4":
                    # Shake / Tremor (High frequency random vibration)
                    acc_x = (np.random.randn() * 15.0)
                    acc_y = (np.random.randn() * 15.0)
                    acc_z = g + (np.random.randn() * 15.0)
                    
                    gyro_x = (np.random.randn() * 6.0)
                    gyro_y = (np.random.randn() * 6.0)
                    gyro_z = (np.random.randn() * 6.0)
                
                # Add gaussian sensor noise
                acc_x += np.random.randn() * 0.15
                acc_y += np.random.randn() * 0.15
                acc_z += np.random.randn() * 0.15
                gyro_x += np.random.randn() * 0.05
                gyro_y += np.random.randn() * 0.05
                gyro_z += np.random.randn() * 0.05
                
                # Push values
                outlet.push_sample([
                    float(acc_x), float(acc_y), float(acc_z),
                    float(gyro_x), float(gyro_y), float(gyro_z)
                ])
                
                next_sample_time += sample_interval
                
            time.sleep(0.001)

    except KeyboardInterrupt:
        print("\n🛑 Stopping IMU simulator...")
    finally:
        if original_terminal_settings is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, original_terminal_settings)

if __name__ == "__main__":
    main()
