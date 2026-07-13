import os
import sys
import time
import math
import threading
import numpy as np
import pygame

try:
    from pylsl import StreamInlet, resolve_byprop
    lsl_available = True
except ImportError:
    lsl_available = False

class GameInputManager:
    def __init__(self):
        self.pending_commands = []
        self.lock = threading.Lock()
        self.running = True
        
        self.cmds = []
        self.keys = []
        self.mouse = []
        self.mods = 0
        
        # State tracking for standalone LSL fallback
        self.dashboard_controlled = False
        self.imu_connected = False
        self.eeg_connected = False
        self.latest_roll = 0.0
        self.latest_shake = 0.0
        self.blink_val = 0.0
        self.clench_val = 0.0
        self.last_trigger_time = 0
        
        # Buffers for visualizers (scrolling HUD trends)
        self.eeg_buffer_c3 = np.zeros(200)
        self.eeg_buffer_tp9 = np.zeros(200)
        self.acc_buffer_x = np.zeros(200)
        self.acc_buffer_y = np.zeros(200)
        
        self.eeg_stream_name = "None"
        self.imu_stream_name = "None"
        
        # Start background threads
        self.thread_stdin = threading.Thread(target=self._stdin_worker, daemon=True)
        self.thread_stdin.start()
        
        if lsl_available:
            self.thread_imu = threading.Thread(target=self._imu_worker, daemon=True)
            self.thread_imu.start()
            self.thread_eeg = threading.Thread(target=self._eeg_worker, daemon=True)
            self.thread_eeg.start()

    def add_command(self, cmd):
        with self.lock:
            self.pending_commands.append(cmd)

    def get_commands(self):
        with self.lock:
            cmds = list(self.pending_commands)
            self.pending_commands.clear()
            return cmds

    def _stdin_worker(self):
        while self.running:
            line = sys.stdin.readline()
            if not line:
                break
            cmd = line.strip().lower()
            if cmd:
                with self.lock:
                    self.dashboard_controlled = True
                self.add_command(cmd)

    def _imu_worker(self):
        print("📡 InputMgr Standalone: Searching for Smartwatch_IMU LSL stream...")
        while self.running:
            if self.dashboard_controlled:
                time.sleep(2.0)
                continue
            try:
                streams = resolve_byprop("name", "Smartwatch_IMU", timeout=1.0)
                if streams:
                    inlet = StreamInlet(streams[0])
                    with self.lock:
                        self.imu_connected = True
                        self.imu_stream_name = streams[0].name()
                    print("✅ InputMgr Standalone: IMU Stream Connected successfully!")
                    
                    while self.running and not self.dashboard_controlled:
                        samples, _ = inlet.pull_chunk(max_samples=20)
                        if samples:
                            samples_np = np.array(samples)
                            ax = samples_np[:, 0]
                            ay = samples_np[:, 1]
                            
                            with self.lock:
                                self.acc_buffer_x = np.append(self.acc_buffer_x[len(ax):], ax)
                                self.acc_buffer_y = np.append(self.acc_buffer_y[len(ay):], ay)
                                
                                last_sample = samples[-1]
                                last_ax, last_ay, last_az = last_sample[0], last_sample[1], last_sample[2]
                                roll = math.atan2(last_ay, last_az) * 180.0 / math.pi if abs(last_az) > 1e-3 else 0.0
                                self.latest_roll = roll
                                
                                acc_mag = np.sqrt(last_ax**2 + last_ay**2 + last_az**2)
                                self.latest_shake = np.abs(acc_mag - 9.81)
                                
                            if roll < -15.0:
                                self.add_command("left")
                            elif roll > 15.0:
                                self.add_command("right")
                                
                            # Voice command injection spike fallback
                            if len(last_sample) >= 6:
                                gz = last_sample[5]
                                if gz > 90.0:
                                    self.add_command("left")
                                elif gz < -90.0:
                                    self.add_command("right")
                        time.sleep(0.016)
            except Exception:
                with self.lock:
                    self.imu_connected = False
            time.sleep(2.0)

    def _eeg_worker(self):
        print("📡 InputMgr Standalone: Searching for Muse_Athena LSL stream...")
        while self.running:
            if self.dashboard_controlled:
                time.sleep(2.0)
                continue
            try:
                streams = resolve_byprop("type", "EEG", timeout=1.0)
                if streams:
                    inlet = StreamInlet(streams[0])
                    with self.lock:
                        self.eeg_connected = True
                        self.eeg_stream_name = streams[0].name()
                    print("✅ InputMgr Standalone: EEG Stream Connected successfully!")
                    
                    last_trigger = 0.0
                    while self.running and not self.dashboard_controlled:
                        samples, _ = inlet.pull_chunk(max_samples=50)
                        if samples:
                            samples_np = np.array(samples)
                            c3_new = samples_np[:, 1] if samples_np.shape[1] > 1 else samples_np[:, 0]
                            tp9_new = samples_np[:, 0]
                            
                            with self.lock:
                                self.eeg_buffer_c3 = np.append(self.eeg_buffer_c3[len(c3_new):], c3_new)
                                self.eeg_buffer_tp9 = np.append(self.eeg_buffer_tp9[len(tp9_new):], tp9_new)
                                
                                c3_clean = self.eeg_buffer_c3 - np.mean(self.eeg_buffer_c3)
                                tp9_clean = self.eeg_buffer_tp9 - np.mean(self.eeg_buffer_tp9)
                                
                                window_blink = 80
                                self.blink_val = float(np.max(c3_clean[-window_blink:]) - np.min(c3_clean[-window_blink:]))
                                self.clench_val = float(np.std(tp9_clean))
                                
                                now_ms = pygame.time.get_ticks()
                                if now_ms - self.last_trigger_time > 800:
                                    if self.clench_val > 35.0:
                                        self.add_command("right")
                                        self.last_trigger_time = now_ms
                                    elif self.blink_val > 45.0:
                                        self.add_command("left")
                                        self.last_trigger_time = now_ms
                        time.sleep(0.016)
            except Exception:
                with self.lock:
                    self.eeg_connected = False
            time.sleep(2.0)

    def simulate_blink(self):
        now_ms = pygame.time.get_ticks()
        if now_ms - self.last_trigger_time > 800:
            self.add_command("left")
            self.last_trigger_time = now_ms
            self.blink_val = 95.0
                
    def simulate_clench(self):
        now_ms = pygame.time.get_ticks()
        if now_ms - self.last_trigger_time > 800:
            self.add_command("right")
            self.last_trigger_time = now_ms
            self.clench_val = 160.0

    def update(self):
        """Call at the beginning of each frame. Combines Pygame keyboard/mouse and BCI states."""
        self.cmds = self.get_commands()
        self.keys = pygame.key.get_pressed()
        self.mouse = pygame.mouse.get_pressed()
        self.mods = pygame.key.get_mods()

    def is_left(self):
        if len(self.keys) == 0:
            return "left" in self.cmds
        return self.keys[pygame.K_LEFT] or self.keys[pygame.K_a] or ("left" in self.cmds)

    def is_right(self):
        if len(self.keys) == 0:
            return "right" in self.cmds
        return self.keys[pygame.K_RIGHT] or self.keys[pygame.K_d] or ("right" in self.cmds)

    def is_up(self):
        if len(self.keys) == 0:
            return "up" in self.cmds
        return self.keys[pygame.K_UP] or self.keys[pygame.K_w] or ("up" in self.cmds)

    def is_down(self):
        if len(self.keys) == 0:
            return "down" in self.cmds
        return self.keys[pygame.K_DOWN] or self.keys[pygame.K_s] or ("down" in self.cmds)

    def is_action(self):
        if len(self.keys) == 0:
            return ("action" in self.cmds) or ("toggle_start" in self.cmds)
        return self.keys[pygame.K_SPACE] or ("action" in self.cmds) or ("toggle_start" in self.cmds)

    def is_sec_action(self):
        if len(self.mouse) == 0:
            return "sec_action" in self.cmds
        # Left click OR shift key OR sec_action command
        return self.mouse[0] or (self.mods & pygame.KMOD_SHIFT) or ("sec_action" in self.cmds)

    def is_tert_action(self):
        if len(self.mouse) == 0:
            return "tert_action" in self.cmds
        # Right click OR ctrl key OR tert_action command
        return self.mouse[2] or (self.mods & pygame.KMOD_CTRL) or ("tert_action" in self.cmds)
