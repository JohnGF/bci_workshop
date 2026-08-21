import os
import sys
import time
import math
import threading
import numpy as np
import pygame

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from pylsl import StreamInlet, resolve_byprop
    lsl_available = True
except ImportError:
    lsl_available = False

class GameInputManager:
    def __init__(self):
        self.pending_commands = []
        self.pending_player_commands = {1: [], 2: []}
        self.lock = threading.Lock()
        self.running = True
        
        self.cmds = []
        self.player_cmds = {1: [], 2: []}
        self.keys = []
        self.mouse = []
        self.mods = 0
        
        # State tracking for standalone LSL fallback
        self.dashboard_controlled = False
        self.imu_connected = {1: False, 2: False}
        self.eeg_connected = {1: False, 2: False}
        
        self.latest_roll = {1: 0.0, 2: 0.0}
        self.latest_shake = {1: 0.0, 2: 0.0}
        self.latest_touch_x = {1: -1.0, 2: -1.0}
        self.latest_touch_y = {1: -1.0, 2: -1.0}
        self.latest_touch_active = {1: False, 2: False}
        
        self.blink_val = {1: 0.0, 2: 0.0}
        self.clench_val = {1: 0.0, 2: 0.0}
        self.last_trigger_time = {1: 0, 2: 0}
        
        # Buffers for visualizers (scrolling HUD trends)
        self.eeg_buffer_c3 = {1: np.zeros(200), 2: np.zeros(200)}
        self.eeg_buffer_tp9 = {1: np.zeros(200), 2: np.zeros(200)}
        self.acc_buffer_x = {1: np.zeros(200), 2: np.zeros(200)}
        self.acc_buffer_y = {1: np.zeros(200), 2: np.zeros(200)}
        
        self.eeg_stream_name = {1: "None", 2: "None"}
        self.imu_stream_name = {1: "None", 2: "None"}

        self.imu_inlet = {1: None, 2: None}
        self.eeg_inlet = {1: None, 2: None}
        self.voice_inlet = None
        
        # Start background threads
        self.thread_stdin = threading.Thread(target=self._stdin_worker, daemon=True)
        self.thread_stdin.start()
        
        if lsl_available:
            self.thread_discovery = threading.Thread(target=self._unified_discovery_worker, daemon=True)
            self.thread_discovery.start()

            self.thread_imu_p1 = threading.Thread(target=lambda: self._imu_pull_loop(1), daemon=True)
            self.thread_imu_p1.start()
            self.thread_imu_p2 = threading.Thread(target=lambda: self._imu_pull_loop(2), daemon=True)
            self.thread_imu_p2.start()

            self.thread_eeg_p1 = threading.Thread(target=lambda: self._eeg_pull_loop(1), daemon=True)
            self.thread_eeg_p1.start()
            self.thread_eeg_p2 = threading.Thread(target=lambda: self._eeg_pull_loop(2), daemon=True)
            self.thread_eeg_p2.start()

            self.thread_voice = threading.Thread(target=self._voice_pull_loop, daemon=True)
            self.thread_voice.start()

    def add_command(self, cmd: str, player: int = 1):
        with self.lock:
            # Check for player prefixes like p1:left or p2:action
            raw = cmd.strip().lower()
            if raw.startswith("p1:"):
                p = 1
                action = raw[3:].strip()
            elif raw.startswith("p2:"):
                p = 2
                action = raw[3:].strip()
            else:
                p = player
                action = raw
            
            if p not in self.pending_player_commands:
                self.pending_player_commands[p] = []
            self.pending_player_commands[p].append(action)
            self.pending_commands.append(raw)

    def get_commands(self):
        with self.lock:
            cmds = list(self.pending_commands)
            self.pending_commands.clear()
            p_cmds = {1: list(self.pending_player_commands.get(1, [])), 2: list(self.pending_player_commands.get(2, []))}
            self.pending_player_commands[1].clear()
            self.pending_player_commands[2].clear()
            return cmds, p_cmds

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

    def _unified_discovery_worker(self):
        """Single non-blocking background discovery loop that matches all streams in one quick pass."""
        while self.running:
            if self.dashboard_controlled:
                time.sleep(2.0)
                continue

            need_scan = (
                self.imu_inlet[1] is None or
                self.imu_inlet[2] is None or
                self.eeg_inlet[1] is None or
                self.eeg_inlet[2] is None or
                self.voice_inlet is None
            )

            if need_scan:
                try:
                    streams = resolve_streams(0.1)
                except Exception:
                    streams = []

                for s in streams:
                    try:
                        s_name = s.name()
                        s_type = s.type().upper()

                        # Match Player 1 IMU
                        if self.imu_inlet[1] is None and s_name in ("Smartwatch_IMU_P1", "Smartwatch_IMU", "Watch_IMU"):
                            inlet = StreamInlet(s)
                            self.imu_inlet[1] = inlet
                            with self.lock:
                                self.imu_connected[1] = True
                                self.imu_stream_name[1] = s_name
                            print(f"✅ InputMgr: Player 1 IMU Connected ('{s_name}')")

                        # Match Player 2 IMU
                        elif self.imu_inlet[2] is None and s_name in ("Smartwatch_IMU_P2", "Watch_IMU_P2", "Watch_IMU_2"):
                            inlet = StreamInlet(s)
                            self.imu_inlet[2] = inlet
                            with self.lock:
                                self.imu_connected[2] = True
                                self.imu_stream_name[2] = s_name
                            print(f"✅ InputMgr: Player 2 IMU Connected ('{s_name}')")

                        # Match Player 1 EEG
                        if self.eeg_inlet[1] is None and (s_name in ("Muse-EEG-P1", "Muse_Athena", "Muse-EEG", "Petal_Stream") or s_type == "EEG"):
                            inlet = StreamInlet(s)
                            self.eeg_inlet[1] = inlet
                            with self.lock:
                                self.eeg_connected[1] = True
                                self.eeg_stream_name[1] = s_name
                            print(f"✅ InputMgr: Player 1 EEG Connected ('{s_name}')")

                        # Match Player 2 EEG
                        elif self.eeg_inlet[2] is None and s_name in ("Muse-EEG-P2", "Muse_Athena_2", "Muse-2"):
                            inlet = StreamInlet(s)
                            self.eeg_inlet[2] = inlet
                            with self.lock:
                                self.eeg_connected[2] = True
                                self.eeg_stream_name[2] = s_name
                            print(f"✅ InputMgr: Player 2 EEG Connected ('{s_name}')")

                        # Match Voice Commands
                        if self.voice_inlet is None and s_name == "Voice_Commands":
                            self.voice_inlet = StreamInlet(s)
                            print("✅ InputMgr: Voice Commands Stream Connected!")
                    except Exception:
                        pass

            time.sleep(1.5)

    def _imu_pull_loop(self, player_id: int):
        """Zero-latency polling worker for player IMU streaming."""
        while self.running:
            inlet = self.imu_inlet[player_id]
            if inlet is None or self.dashboard_controlled:
                time.sleep(0.05)
                continue

            try:
                samples, _ = inlet.pull_chunk(max_samples=25, timeout=0.0)
                if not samples:
                    time.sleep(0.002)
                    continue

                samples_np = np.array(samples)
                ax = samples_np[:, 0]
                ay = samples_np[:, 1]
                
                with self.lock:
                    buf_x = self.acc_buffer_x[player_id]
                    buf_y = self.acc_buffer_y[player_id]
                    self.acc_buffer_x[player_id] = np.append(buf_x[len(ax):], ax)
                    self.acc_buffer_y[player_id] = np.append(buf_y[len(ay):], ay)
                    
                    last_sample = samples[-1]
                    last_ax, last_ay, last_az = last_sample[0], last_sample[1], last_sample[2]
                    roll = math.atan2(last_ay, last_az) * 180.0 / math.pi if abs(last_az) > 1e-3 else 0.0
                    self.latest_roll[player_id] = roll
                    
                    acc_mag = np.sqrt(last_ax**2 + last_ay**2 + last_az**2)
                    self.latest_shake[player_id] = np.abs(acc_mag - 9.81)
                    
                if roll < -15.0:
                    self.add_command("left", player=player_id)
                elif roll > 15.0:
                    self.add_command("right", player=player_id)
                    
                # Check screen touch/tap events & X/Y coordinates across ALL samples in this pull chunk
                touch_detected = False
                for s in samples:
                    if len(s) >= 10:
                        if s[9] >= 0.5:
                            touch_detected = True
                            with self.lock:
                                self.latest_touch_x[player_id] = s[7]
                                self.latest_touch_y[player_id] = s[8]
                                self.latest_touch_active[player_id] = True
                    elif len(s) >= 7:
                        if s[6] >= 0.5:
                            touch_detected = True
                            with self.lock:
                                self.latest_touch_active[player_id] = True

                if touch_detected:
                    self.add_command("action", player=player_id)
                    self.add_command("fire", player=player_id)

                # Gyroscope turn injection fallback
                if len(last_sample) >= 6:
                    gz = last_sample[5]
                    if gz > 90.0:
                        self.add_command("left", player=player_id)
                    elif gz < -90.0:
                        self.add_command("right", player=player_id)

            except Exception:
                self.imu_inlet[player_id] = None
                with self.lock:
                    self.imu_connected[player_id] = False
                time.sleep(0.5)

    def _eeg_pull_loop(self, player_id: int):
        """Zero-latency polling worker for player EEG streaming."""
        while self.running:
            inlet = self.eeg_inlet[player_id]
            if inlet is None or self.dashboard_controlled:
                time.sleep(0.05)
                continue

            try:
                samples, _ = inlet.pull_chunk(max_samples=50, timeout=0.0)
                if not samples:
                    time.sleep(0.004)
                    continue

                samples_np = np.array(samples)
                c3_new = samples_np[:, 1] if samples_np.shape[1] > 1 else samples_np[:, 0]
                tp9_new = samples_np[:, 0]

                eeg_cmd = None
                with self.lock:
                    buf_c3 = self.eeg_buffer_c3[player_id]
                    buf_tp9 = self.eeg_buffer_tp9[player_id]
                    self.eeg_buffer_c3[player_id] = np.append(buf_c3[len(c3_new):], c3_new)
                    self.eeg_buffer_tp9[player_id] = np.append(buf_tp9[len(tp9_new):], tp9_new)

                    c3_clean = self.eeg_buffer_c3[player_id] - np.mean(self.eeg_buffer_c3[player_id])
                    tp9_clean = self.eeg_buffer_tp9[player_id] - np.mean(self.eeg_buffer_tp9[player_id])

                    window_blink = 80
                    self.blink_val[player_id] = float(np.max(c3_clean[-window_blink:]) - np.min(c3_clean[-window_blink:]))
                    self.clench_val[player_id] = float(np.std(tp9_clean))

                    now_ms = pygame.time.get_ticks()
                    if now_ms - self.last_trigger_time[player_id] > 800:
                        if self.clench_val[player_id] > 35.0:
                            eeg_cmd = "right"
                            self.last_trigger_time[player_id] = now_ms
                        elif self.blink_val[player_id] > 45.0:
                            eeg_cmd = "left"
                            self.last_trigger_time[player_id] = now_ms

                if eeg_cmd:
                    self.add_command(eeg_cmd, player=player_id)

            except Exception:
                self.eeg_inlet[player_id] = None
                with self.lock:
                    self.eeg_connected[player_id] = False
                time.sleep(0.5)

    def _voice_pull_loop(self):
        """Zero-latency polling worker for Voice Commands stream."""
        code_to_cmd = {
            99.0: "left",
            -99.0: "right",
            55.0: "supernova",
            66.0: "missile",
            44.0: "action",
            33.0: "sec_action",
            11.0: "toggle_start",
        }
        while self.running:
            inlet = self.voice_inlet
            if inlet is None or self.dashboard_controlled:
                time.sleep(0.1)
                continue

            try:
                samples, _ = inlet.pull_chunk(max_samples=10, timeout=0.0)
                if samples:
                    for s in samples:
                        code = float(s[0])
                        cmd = code_to_cmd.get(code)
                        if cmd:
                            self.add_command(cmd, player=1)
                else:
                    time.sleep(0.02)
            except Exception:
                self.voice_inlet = None
                time.sleep(1.0)

    def simulate_blink(self, player_id: int = 1):
        now_ms = pygame.time.get_ticks()
        if now_ms - self.last_trigger_time[player_id] > 800:
            self.add_command("left", player=player_id)
            self.last_trigger_time[player_id] = now_ms
            self.blink_val[player_id] = 95.0
                
    def simulate_clench(self, player_id: int = 1):
        now_ms = pygame.time.get_ticks()
        if now_ms - self.last_trigger_time[player_id] > 800:
            self.add_command("right", player=player_id)
            self.last_trigger_time[player_id] = now_ms
            self.clench_val[player_id] = 160.0

    def update(self):
        """Call at the beginning of each frame. Combines Pygame keyboard/mouse and BCI states."""
        self.cmds, self.player_cmds = self.get_commands()
        if pygame.get_init() and pygame.display.get_init():
            self.keys = pygame.key.get_pressed()
            self.mouse = pygame.mouse.get_pressed()
            self.mods = pygame.key.get_mods()
        else:
            self.keys = []
            self.mouse = []
            self.mods = 0

    def get_player_cmds(self, player: int = 1) -> list[str]:
        return self.player_cmds.get(player, [])

    def is_left(self, player: int = 1, two_player_mode: bool = False):
        p_cmds = self.player_cmds.get(player, [])
        if len(self.keys) == 0:
            return ("left" in p_cmds) or (player == 1 and "left" in self.cmds)
        if player == 1:
            k = self.keys[pygame.K_a] or (not two_player_mode and self.keys[pygame.K_LEFT])
            return k or ("left" in p_cmds) or ("left" in self.cmds)
        elif player == 2:
            return self.keys[pygame.K_LEFT] or ("left" in p_cmds)
        return False

    def is_right(self, player: int = 1, two_player_mode: bool = False):
        p_cmds = self.player_cmds.get(player, [])
        if len(self.keys) == 0:
            return ("right" in p_cmds) or (player == 1 and "right" in self.cmds)
        if player == 1:
            k = self.keys[pygame.K_d] or (not two_player_mode and self.keys[pygame.K_RIGHT])
            return k or ("right" in p_cmds) or ("right" in self.cmds)
        elif player == 2:
            return self.keys[pygame.K_RIGHT] or ("right" in p_cmds)
        return False

    def is_up(self, player: int = 1, two_player_mode: bool = False):
        p_cmds = self.player_cmds.get(player, [])
        if len(self.keys) == 0:
            return ("up" in p_cmds) or (player == 1 and "up" in self.cmds)
        if player == 1:
            k = self.keys[pygame.K_w] or (not two_player_mode and self.keys[pygame.K_UP])
            return k or ("up" in p_cmds) or ("up" in self.cmds)
        elif player == 2:
            return self.keys[pygame.K_UP] or ("up" in p_cmds)
        return False

    def is_down(self, player: int = 1, two_player_mode: bool = False):
        p_cmds = self.player_cmds.get(player, [])
        if len(self.keys) == 0:
            return ("down" in p_cmds) or (player == 1 and "down" in self.cmds)
        if player == 1:
            k = self.keys[pygame.K_s] or (not two_player_mode and self.keys[pygame.K_DOWN])
            return k or ("down" in p_cmds) or ("down" in self.cmds)
        elif player == 2:
            return self.keys[pygame.K_DOWN] or ("down" in p_cmds)
        return False

    def is_action(self, player: int = 1):
        p_cmds = self.player_cmds.get(player, [])
        fire_aliases = {"action", "fire", "laser", "shoot", "attack", "toggle_start"}
        has_cmd = any(c in fire_aliases for c in p_cmds)
        mouse_left = self.mouse[0] if len(self.mouse) > 0 else False
        if len(self.keys) == 0:
            return has_cmd or mouse_left or (player == 1 and any(c in fire_aliases for c in self.cmds))
        if player == 1:
            return self.keys[pygame.K_SPACE] or mouse_left or has_cmd or any(c in fire_aliases for c in self.cmds)
        elif player == 2:
            return self.keys[pygame.K_RETURN] or self.keys[pygame.K_KP_ENTER] or has_cmd
        return False

    def is_sec_action(self, player: int = 1):
        p_cmds = self.player_cmds.get(player, [])
        if player == 1:
            mouse_right = self.mouse[2] if len(self.mouse) > 2 else False
            k = self.keys[pygame.K_f] if len(self.keys) > 0 else False
            shift = (self.mods & pygame.KMOD_LSHIFT) if len(self.keys) > 0 else False
            return mouse_right or k or shift or ("sec_action" in p_cmds) or ("sec_action" in self.cmds)
        elif player == 2:
            k = self.keys[pygame.K_SLASH] if len(self.keys) > 0 else False
            shift = (self.mods & pygame.KMOD_RSHIFT) if len(self.keys) > 0 else False
            return k or shift or ("sec_action" in p_cmds)
        return False

    def is_tert_action(self, player: int = 1):
        p_cmds = self.player_cmds.get(player, [])
        if player == 1:
            mouse_mid = self.mouse[1] if len(self.mouse) > 1 else False
            k = self.keys[pygame.K_c] if len(self.keys) > 0 else False
            ctrl = (self.mods & pygame.KMOD_LCTRL) if len(self.keys) > 0 else False
            return mouse_mid or k or ctrl or ("tert_action" in p_cmds) or ("tert_action" in self.cmds)
        elif player == 2:
            k = self.keys[pygame.K_PERIOD] if len(self.keys) > 0 else False
            ctrl = (self.mods & pygame.KMOD_RCTRL) if len(self.keys) > 0 else False
            return k or ctrl or ("tert_action" in p_cmds)
        return False
