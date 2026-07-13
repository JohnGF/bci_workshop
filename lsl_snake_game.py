# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pygame",
#     "pylsl",
#     "numpy",
# ]
# ///

import sys
import random
import threading
import time
import numpy as np
import pygame
from pylsl import StreamInlet, resolve_byprop
from game_input import GameInputManager

# --- CONSTANTS ---
WINDOW_WIDTH = 800
WINDOW_HEIGHT = 500
GRID_SIZE = 20
TILE_SIZE = 22
GRID_MARGIN = 20

# Colors (FCUL Presentation visual language)
COLOR_BG = (15, 23, 42)        # slate-900
COLOR_PANEL_BG = (30, 41, 59)  # slate-800
COLOR_GRID_LINE = (30, 41, 59)
COLOR_SNAKE_HEAD = (30, 190, 130) # fculgreen
COLOR_SNAKE_BODY = (16, 185, 129)
COLOR_FOOD = (239, 68, 68)     # red-500
COLOR_TEXT = (241, 245, 249)
COLOR_TEXT_MUTED = (148, 163, 184)
COLOR_CYAN = (56, 189, 248)    # Ch C3 / blink
COLOR_PINK = (236, 72, 153)    # Ch C4 / clench

# Thresholds (microvolts)
BLINK_THRESHOLD = 80.0
CLENCH_THRESHOLD = 140.0
REFRACTORY_MS = 800  # Lockout between triggers

# --- BCI STATE CONTROLLER ---
# Standalone BCIController class removed in favor of unified game_input.py module

def synthesize_food_sound():
    sample_rate = 44100
    duration = 0.08
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    freq = np.geomspace(600, 1200, len(t))
    val = np.sin(2 * np.pi * freq * t) * 0.2
    sound_arr = (val * 32767).astype(np.int16)
    sound_arr = np.repeat(sound_arr[:, np.newaxis], 2, axis=1)
    return pygame.sndarray.make_sound(sound_arr)

def synthesize_shake_sound():
    sample_rate = 44100
    duration = 0.2
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    val = (np.sin(2 * np.pi * 523.25 * t) + np.sin(2 * np.pi * 659.25 * t)) * np.exp(-8 * t) * 0.25
    sound_arr = (val * 32767).astype(np.int16)
    sound_arr = np.repeat(sound_arr[:, np.newaxis], 2, axis=1)
    return pygame.sndarray.make_sound(sound_arr)

def synthesize_crash_sound():
    sample_rate = 44100
    duration = 0.4
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    freq = np.geomspace(400, 80, len(t))
    val = np.sin(2 * np.pi * freq * t) * np.exp(-6 * t) * 0.3
    noise = (np.random.rand(len(t)) * 2 - 1) * np.exp(-12 * t) * 0.15
    val = (val + noise) * 0.7
    sound_arr = (val * 32767).astype(np.int16)
    sound_arr = np.repeat(sound_arr[:, np.newaxis], 2, axis=1)
    return pygame.sndarray.make_sound(sound_arr)

# --- SNAKE GAME PARADIGM ---
class SnakeGame:
    def __init__(self):
        pygame.init()
        self.audio_enabled = False
        try:
            pygame.mixer.init()
            self.audio_enabled = True
        except pygame.error as e:
            print(f"Warning: Audio device not found ({e}). Running without audio.")
        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.RESIZABLE)
        pygame.display.set_caption("LSL Physiological BCI Snake Game")
        self.clock = pygame.time.Clock()
        self.font_title = pygame.font.SysFont("Arial", 14, bold=True)
        self.font_ui = pygame.font.SysFont("Arial", 12, bold=False)
        self.font_ui_bold = pygame.font.SysFont("Arial", 12, bold=True)
        
        self.snd_food = None
        self.snd_shake = None
        self.snd_crash = None
        if self.audio_enabled:
            try:
                self.snd_food = synthesize_food_sound()
                self.snd_shake = synthesize_shake_sound()
                self.snd_crash = synthesize_crash_sound()
            except Exception as e:
                print(f"Audio Synthesis Warning: {e}. Running mute.")
                self.snd_food = None
                self.snd_shake = None
                self.snd_crash = None
            
        self.bci = GameInputManager()
        self.reset_game()
        
    def reset_game(self):
        # Starts in the middle, heading Right
        self.snake = [(5, 10), (4, 10), (3, 10)]
        self.direction = (1, 0)
        self.score = 0
        self.game_over = False
        self.game_started = False
        self.spawn_food()
        
    def spawn_food(self):
        while True:
            self.food = (random.randint(0, GRID_SIZE - 1), random.randint(0, GRID_SIZE - 1))
            if self.food not in self.snake:
                break
                
    def turn_relative(self, side):
        # side: "left" or "right"
        # Since standard BCI controls map blinks to Left turns and clenches to Right turns:
        dx, dy = self.direction
        if side == "left":
            # Turn 90 degrees counter-clockwise
            self.direction = (dy, -dx)
        elif side == "right":
            # Turn 90 degrees clockwise
            self.direction = (-dy, dx)
            
    def update(self):
        # We parse the BCI / keyboard commands together
        self.bci.update()
        
        # 1. Action/Start check
        if self.bci.is_action():
            if self.game_over:
                self.reset_game()
                self.game_started = True
            else:
                self.game_started = not self.game_started
            if self.snd_shake:
                self.snd_shake.play()
                
        # 2. Secondary check
        elif self.bci.is_sec_action():
            self.spawn_food()
            if self.snd_food:
                self.snd_food.play()
            print("🎮 Snake Game: Secondary Action triggered! Spawning extra food!")
            
        # 3. Tertiary check
        elif self.bci.is_tert_action():
            global COLOR_SNAKE_BODY, COLOR_SNAKE_HEAD
            import random
            COLOR_SNAKE_BODY = (random.randint(50, 255), random.randint(50, 255), random.randint(50, 255))
            COLOR_SNAKE_HEAD = (random.randint(150, 255), random.randint(150, 255), random.randint(150, 255))
            if self.snd_shake:
                self.snd_shake.play()
            print("🎮 Snake Game: Tertiary Action triggered! Cycling snake colors!")
            
        # 4. Movement checks (Keyboard + BCI merged!)
        elif not self.game_over:
            moved = False
            if self.bci.is_left():
                if self.direction != (1, 0):
                    self.direction = (-1, 0)
                moved = True
            elif self.bci.is_right():
                if self.direction != (-1, 0):
                    self.direction = (1, 0)
                moved = True
            elif self.bci.is_up():
                if self.direction != (0, 1):
                    self.direction = (0, -1)
                moved = True
            elif self.bci.is_down():
                if self.direction != (0, -1):
                    self.direction = (0, 1)
                moved = True
                
            if moved and not self.game_started:
                self.game_started = True
            
        if not self.game_started or self.game_over:
            return

        # Calculate new head location
        dx, dy = self.direction
        head_x = self.snake[0][0] + dx
        head_y = self.snake[0][1] + dy
        
        # Teleport wrap-around boundaries
        if head_x < 0: head_x = GRID_SIZE - 1
        if head_x >= GRID_SIZE: head_x = 0
        if head_y < 0: head_y = GRID_SIZE - 1
        if head_y >= GRID_SIZE: head_y = 0
        
        new_head = (head_x, head_y)
        
        # Self-collision check
        if new_head in self.snake:
            self.game_over = True
            if self.snd_crash:
                self.snd_crash.play()
            return
            
        self.snake.insert(0, new_head)
        
        # Food consumption
        if new_head == self.food:
            self.score += 10
            self.spawn_food()
            if self.snd_food:
                self.snd_food.play()
        else:
            self.snake.pop()
            
    def draw(self):
        self.screen.fill(COLOR_BG)
        
        # --- 1. DRAW SNAKE PLAYFIELD ---
        play_size = GRID_SIZE * TILE_SIZE
        pygame.draw.rect(self.screen, COLOR_BG, (GRID_MARGIN, GRID_MARGIN, play_size, play_size))
        
        # Grid lines
        for x in range(GRID_SIZE + 1):
            pygame.draw.line(self.screen, COLOR_GRID_LINE, 
                             (GRID_MARGIN + x * TILE_SIZE, GRID_MARGIN), 
                             (GRID_MARGIN + x * TILE_SIZE, GRID_MARGIN + play_size), 1)
        for y in range(GRID_SIZE + 1):
            pygame.draw.line(self.screen, COLOR_GRID_LINE, 
                             (GRID_MARGIN, GRID_MARGIN + y * TILE_SIZE), 
                             (GRID_MARGIN + play_size, GRID_MARGIN + y * TILE_SIZE), 1)
            
        # Draw Food
        food_rect = (GRID_MARGIN + self.food[0] * TILE_SIZE + 2, 
                     GRID_MARGIN + self.food[1] * TILE_SIZE + 2, 
                     TILE_SIZE - 4, TILE_SIZE - 4)
        pygame.draw.ellipse(self.screen, COLOR_FOOD, food_rect)
        
        # Draw Snake
        for idx, seg in enumerate(self.snake):
            color = COLOR_SNAKE_HEAD if idx == 0 else COLOR_SNAKE_BODY
            seg_rect = (GRID_MARGIN + seg[0] * TILE_SIZE + 2, 
                        GRID_MARGIN + seg[1] * TILE_SIZE + 2, 
                        TILE_SIZE - 4, TILE_SIZE - 4)
            pygame.draw.rect(self.screen, color, seg_rect, border_radius=4)
            
        # --- 2. DRAW RIGHT CONTROL PANEL ---
        panel_x = GRID_MARGIN + play_size + 20
        panel_w = WINDOW_WIDTH - panel_x - GRID_MARGIN
        panel_h = play_size
        pygame.draw.rect(self.screen, COLOR_PANEL_BG, (panel_x, GRID_MARGIN, panel_w, panel_h), border_radius=12)
        
        # Content Margin
        px = panel_x + 15
        py = GRID_MARGIN + 15
        
        # Title
        title_surf = self.font_title.render("BCI PHY WORKSHOP CONTROLLER", True, COLOR_TEXT)
        self.screen.blit(title_surf, (px, py))
        py += 25
        
        # Connection Status for both nodes
        eeg_connected = self.bci.eeg_connected
        imu_connected = self.bci.imu_connected
        dashboard_controlled = self.bci.dashboard_controlled
        
        # 1. EEG Stream Status
        if dashboard_controlled:
            eeg_color = COLOR_SNAKE_HEAD
            eeg_text = "REDIRECTED (main.py)"
        else:
            eeg_color = COLOR_SNAKE_HEAD if eeg_connected else COLOR_FOOD
            eeg_text = self.bci.eeg_stream_name if eeg_connected else "DISCONNECTED (Muse)"
        self.screen.blit(self.font_ui.render("EEG NODE:", True, COLOR_TEXT_MUTED), (px, py))
        self.screen.blit(self.font_ui_bold.render(eeg_text, True, eeg_color), (px + 90, py))
        py += 18
        
        # 2. IMU Stream Status
        if dashboard_controlled:
            imu_color = COLOR_SNAKE_HEAD
            imu_text = "REDIRECTED (main.py)"
        else:
            imu_color = COLOR_SNAKE_HEAD if imu_connected else COLOR_FOOD
            imu_text = self.bci.imu_stream_name if imu_connected else "DISCONNECTED (Phone)"
        self.screen.blit(self.font_ui.render("IMU NODE:", True, COLOR_TEXT_MUTED), (px, py))
        self.screen.blit(self.font_ui_bold.render(imu_text, True, imu_color), (px + 90, py))
        py += 28
        
        # Raw Waves Visualizer (Mini-Scope)
        scope_h = 110
        scope_w = panel_w - 30
        pygame.draw.rect(self.screen, COLOR_BG, (px, py, scope_w, scope_h), border_radius=8)
        
        with self.bci.lock:
            if eeg_connected and imu_connected:
                # BOTH connected: Draw split scope
                mid_y = py + scope_h // 2
                pygame.draw.line(self.screen, COLOR_GRID_LINE, (px, mid_y), (px + scope_w, mid_y), 1)
                
                # EEG Waves (top half)
                c3_clean = self.bci.eeg_buffer_c3 - np.mean(self.bci.eeg_buffer_c3)
                tp9_clean = self.bci.eeg_buffer_tp9 - np.mean(self.bci.eeg_buffer_tp9)
                max_plot_eeg = 100.0
                
                pts_c3 = []
                for i in range(min(len(c3_clean), scope_w)):
                    x = px + i
                    val = c3_clean[-scope_w + i] if len(c3_clean) >= scope_w else c3_clean[i]
                    y = py + 25 - int((val / max_plot_eeg) * 20)
                    y = max(py + 2, min(mid_y - 2, y))
                    pts_c3.append((x, y))
                if len(pts_c3) > 1:
                    pygame.draw.lines(self.screen, COLOR_CYAN, False, pts_c3, 1)
                    
                pts_tp9 = []
                for i in range(min(len(tp9_clean), scope_w)):
                    x = px + i
                    val = tp9_clean[-scope_w + i] if len(tp9_clean) >= scope_w else tp9_clean[i]
                    y = py + 25 - int((val / max_plot_eeg) * 20)
                    y = max(py + 2, min(mid_y - 2, y))
                    pts_tp9.append((x, y))
                if len(pts_tp9) > 1:
                    pygame.draw.lines(self.screen, COLOR_PINK, False, pts_tp9, 1)
                    
                # IMU Waves (bottom half)
                ax_clean = self.bci.acc_buffer_x - np.mean(self.bci.acc_buffer_x)
                ay_clean = self.bci.acc_buffer_y - np.mean(self.bci.acc_buffer_y)
                max_plot_imu = 15.0
                
                pts_ax = []
                for i in range(min(len(ax_clean), scope_w)):
                    x = px + i
                    val = ax_clean[-scope_w + i] if len(ax_clean) >= scope_w else ax_clean[i]
                    y = mid_y + 25 - int((val / max_plot_imu) * 20)
                    y = max(mid_y + 2, min(py + scope_h - 2, y))
                    pts_ax.append((x, y))
                if len(pts_ax) > 1:
                    pygame.draw.lines(self.screen, COLOR_CYAN, False, pts_ax, 1)
                    
                pts_ay = []
                for i in range(min(len(ay_clean), scope_w)):
                    x = px + i
                    val = ay_clean[-scope_w + i] if len(ay_clean) >= scope_w else ay_clean[i]
                    y = mid_y + 25 - int((val / max_plot_imu) * 20)
                    y = max(mid_y + 2, min(py + scope_h - 2, y))
                    pts_ay.append((x, y))
                if len(pts_ay) > 1:
                    pygame.draw.lines(self.screen, COLOR_PINK, False, pts_ay, 1)
                    
                self.screen.blit(self.font_ui.render("Muse EEG", True, COLOR_TEXT_MUTED), (px + 5, py + 3))
                self.screen.blit(self.font_ui.render("Phone IMU / Voice", True, COLOR_TEXT_MUTED), (px + 5, mid_y + 3))
            
            elif imu_connected:
                # Only IMU connected
                pygame.draw.line(self.screen, COLOR_GRID_LINE, (px, py + scope_h//2), (px + scope_w, py + scope_h//2), 1)
                ax_clean = self.bci.acc_buffer_x - np.mean(self.bci.acc_buffer_x)
                ay_clean = self.bci.acc_buffer_y - np.mean(self.bci.acc_buffer_y)
                max_plot = 15.0
                
                # Draw Accel X
                pts_ax = []
                for i in range(min(len(ax_clean), scope_w)):
                    x = px + i
                    val = ax_clean[-scope_w + i] if len(ax_clean) >= scope_w else ax_clean[i]
                    y = py + scope_h // 2 - int((val / max_plot) * (scope_h // 2))
                    y = max(py + 2, min(py + scope_h - 2, y))
                    pts_ax.append((x, y))
                if len(pts_ax) > 1:
                    pygame.draw.lines(self.screen, COLOR_CYAN, False, pts_ax, 1)
                    
                # Draw Accel Y
                pts_ay = []
                for i in range(min(len(ay_clean), scope_w)):
                    x = px + i
                    val = ay_clean[-scope_w + i] if len(ay_clean) >= scope_w else ay_clean[i]
                    y = py + scope_h // 2 - int((val / max_plot) * (scope_h // 2))
                    y = max(py + 2, min(py + scope_h - 2, y))
                    pts_ay.append((x, y))
                if len(pts_ay) > 1:
                    pygame.draw.lines(self.screen, COLOR_PINK, False, pts_ay, 1)
                    
                c3_tag = self.font_ui.render("Acc. X (Cyan)", True, COLOR_CYAN)
                tp9_tag = self.font_ui.render("Acc. Y (Pink)", True, COLOR_PINK)
                self.screen.blit(c3_tag, (px + 5, py + 5))
                self.screen.blit(tp9_tag, (px + scope_w - 95, py + 5))
                
            else:
                # EEG or nothing
                pygame.draw.line(self.screen, COLOR_GRID_LINE, (px, py + scope_h//2), (px + scope_w, py + scope_h//2), 1)
                c3_clean = self.bci.eeg_buffer_c3 - np.mean(self.bci.eeg_buffer_c3)
                tp9_clean = self.bci.eeg_buffer_tp9 - np.mean(self.bci.eeg_buffer_tp9)
                max_plot = 100.0
                
                # Draw C3
                pts_c3 = []
                for i in range(min(len(c3_clean), scope_w)):
                    x = px + i
                    val = c3_clean[-scope_w + i] if len(c3_clean) >= scope_w else c3_clean[i]
                    y = py + scope_h // 2 - int((val / max_plot) * (scope_h // 2))
                    y = max(py + 2, min(py + scope_h - 2, y))
                    pts_c3.append((x, y))
                if len(pts_c3) > 1:
                    pygame.draw.lines(self.screen, COLOR_CYAN, False, pts_c3, 1)
                    
                # Draw TP9
                pts_tp9 = []
                for i in range(min(len(tp9_clean), scope_w)):
                    x = px + i
                    val = tp9_clean[-scope_w + i] if len(tp9_clean) >= scope_w else tp9_clean[i]
                    y = py + scope_h // 2 - int((val / max_plot) * (scope_h // 2))
                    y = max(py + 2, min(py + scope_h - 2, y))
                    pts_tp9.append((x, y))
                if len(pts_tp9) > 1:
                    pygame.draw.lines(self.screen, COLOR_PINK, False, pts_tp9, 1)
                    
                c3_tag = self.font_ui.render("Ch. C3 / AF7", True, COLOR_CYAN)
                tp9_tag = self.font_ui.render("Ch. C4 / TP9", True, COLOR_PINK)
                self.screen.blit(c3_tag, (px + 5, py + 5))
                self.screen.blit(tp9_tag, (px + scope_w - 95, py + 5))
                
        py += scope_h + 15
        
        # --- Threshold Bar Gauges ---
        bar_w = scope_w
        bar_h = 10
        
        if eeg_connected:
            b_perc = min(1.0, self.bci.blink_val / BLINK_THRESHOLD)
            b_color = COLOR_CYAN if self.bci.blink_val < BLINK_THRESHOLD else COLOR_SNAKE_HEAD
            
            self.screen.blit(self.font_ui.render(f"BLINK VALUE: {self.bci.blink_val:.1f} uV", True, COLOR_TEXT), (px, py))
            self.screen.blit(self.font_ui.render(f"LIMIT: {BLINK_THRESHOLD} uV", True, COLOR_TEXT_MUTED), (px + bar_w - 95, py))
            py += 15
            pygame.draw.rect(self.screen, COLOR_BG, (px, py, bar_w, bar_h), border_radius=4)
            if b_perc > 0:
                pygame.draw.rect(self.screen, b_color, (px, py, int(bar_w * b_perc), bar_h), border_radius=4)
            py += bar_h + 10
            
            c_perc = min(1.0, self.bci.clench_val / CLENCH_THRESHOLD)
            c_color = COLOR_PINK if self.bci.clench_val < CLENCH_THRESHOLD else COLOR_SNAKE_HEAD
            
            self.screen.blit(self.font_ui.render(f"CLENCH VARIANCE: {self.bci.clench_val:.1f} uV", True, COLOR_TEXT), (px, py))
            self.screen.blit(self.font_ui.render(f"LIMIT: {CLENCH_THRESHOLD} uV", True, COLOR_TEXT_MUTED), (px + bar_w - 95, py))
            py += 15
            pygame.draw.rect(self.screen, COLOR_BG, (px, py, bar_w, bar_h), border_radius=4)
            if c_perc > 0:
                pygame.draw.rect(self.screen, c_color, (px, py, int(bar_w * c_perc), bar_h), border_radius=4)
            py += bar_h + 12
            
        if imu_connected:
            roll_val = self.bci.latest_roll
            shake_val = self.bci.latest_shake
            
            r_perc = min(1.0, np.abs(roll_val) / 45.0)
            r_color = COLOR_CYAN if np.abs(roll_val) < 25.0 else COLOR_SNAKE_HEAD
            
            self.screen.blit(self.font_ui.render(f"PHONE ROLL/TILT: {roll_val:.1f}°", True, COLOR_TEXT), (px, py))
            self.screen.blit(self.font_ui.render("TRIGGER: ±25°", True, COLOR_TEXT_MUTED), (px + bar_w - 95, py))
            py += 15
            pygame.draw.rect(self.screen, COLOR_BG, (px, py, bar_w, bar_h), border_radius=4)
            if r_perc > 0:
                pygame.draw.rect(self.screen, r_color, (px, py, int(bar_w * r_perc), bar_h), border_radius=4)
            py += bar_h + 10
            
            s_perc = min(1.0, shake_val / 12.0)
            s_color = COLOR_PINK if shake_val < 8.0 else COLOR_SNAKE_HEAD
            
            self.screen.blit(self.font_ui.render(f"PHONE SHAKE FORCE: {shake_val:.1f} m/s²", True, COLOR_TEXT), (px, py))
            self.screen.blit(self.font_ui.render("TRIGGER: 8.0", True, COLOR_TEXT_MUTED), (px + bar_w - 95, py))
            py += 15
            pygame.draw.rect(self.screen, COLOR_BG, (px, py, bar_w, bar_h), border_radius=4)
            if s_perc > 0:
                pygame.draw.rect(self.screen, s_color, (px, py, int(bar_w * s_perc), bar_h), border_radius=4)
            py += bar_h + 15
            
        if not eeg_connected and not imu_connected:
            self.screen.blit(self.font_ui_bold.render("KEYBOARD / DEMO PLAY MODE", True, COLOR_CYAN), (px, py))
            py += 20
        
        # Score & Game Info Text Overlay
        score_surf = self.font_title.render(f"SCORE: {self.score}", True, COLOR_TEXT)
        self.screen.blit(score_surf, (px, py))
        
        # Controls info
        shortcut_y = py + 30
        self.screen.blit(self.font_ui.render("[Arrow Keys / WASD] - Manual controls", True, COLOR_TEXT_MUTED), (px, shortcut_y))
        self.screen.blit(self.font_ui.render("[B] - Simulate Eye Blink  |  [C] - Simulate Clench", True, COLOR_TEXT_MUTED), (px, shortcut_y + 15))
        
        # Overlay States (Start / Game Over)
        if self.game_over:
            # Translucent mask
            overlay = pygame.Surface((play_size, play_size))
            overlay.set_alpha(190)
            overlay.fill((15, 23, 42))
            self.screen.blit(overlay, (GRID_MARGIN, GRID_MARGIN))
            
            go_f = pygame.font.SysFont("Arial", 28, bold=True)
            sub_f = pygame.font.SysFont("Arial", 14, bold=False)
            
            go_s = go_f.render("GAME OVER", True, COLOR_FOOD)
            sub_s = sub_f.render("Press SPACEBAR to restart", True, COLOR_TEXT)
            
            self.screen.blit(go_s, (GRID_MARGIN + play_size//2 - go_s.get_width()//2, GRID_MARGIN + play_size//2 - 25))
            self.screen.blit(sub_s, (GRID_MARGIN + play_size//2 - sub_s.get_width()//2, GRID_MARGIN + play_size//2 + 15))
            
        elif not self.game_started:
            overlay = pygame.Surface((play_size, play_size))
            overlay.set_alpha(190)
            overlay.fill((15, 23, 42))
            self.screen.blit(overlay, (GRID_MARGIN, GRID_MARGIN))
            
            start_f = pygame.font.SysFont("Arial", 20, bold=True)
            sub_f = pygame.font.SysFont("Arial", 14, bold=False)
            
            start_s = start_f.render("BCI SNAKE PLAYFIELD", True, COLOR_SNAKE_HEAD)
            sub_s = sub_f.render("Press Arrow Key or SPACE to Start", True, COLOR_TEXT)
            
            self.screen.blit(start_s, (GRID_MARGIN + play_size//2 - start_s.get_width()//2, GRID_MARGIN + play_size//2 - 25))
            self.screen.blit(sub_s, (GRID_MARGIN + play_size//2 - sub_s.get_width()//2, GRID_MARGIN + play_size//2 + 10))

        pygame.display.flip()
        
    def loop(self):
        while True:
            # Snake speed: 6 ticks per second (adjustable for challenge)
            self.clock.tick(6)
            
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.bci.running = False
                    pygame.quit()
                    sys.exit()
                elif event.type == pygame.VIDEORESIZE:
                    global WINDOW_WIDTH, WINDOW_HEIGHT
                    WINDOW_WIDTH, WINDOW_HEIGHT = event.w, event.h
                    self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.RESIZABLE)
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_SPACE:
                        if self.game_over:
                            self.reset_game()
                        self.game_started = True
                        
                    # Simulator Hotkeys
                    elif event.key == pygame.K_b:
                        self.bci.simulate_blink()
                        self.game_started = True
                    elif event.key == pygame.K_c:
                        self.bci.simulate_clench()
                        self.game_started = True
                        
            self.update()
            self.draw()


if __name__ == "__main__":
    game = SnakeGame()
    game.loop()
