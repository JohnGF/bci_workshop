#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pygame",
#     "pylsl",
#     "numpy",
#     "PyOpenGL",
# ]
# ///
import os
import sys
import time
import math
import random
import argparse
import threading
import numpy as np
import pygame
from pygame.locals import *

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from game_input import GameInputManager

try:
    from OpenGL.GL import *
    from OpenGL.GLU import *
    opengl_available = True
except ImportError:
    opengl_available = False

try:
    from pylsl import StreamInlet, resolve_byprop
    lsl_available = True
except ImportError:
    lsl_available = False

# ==========================================
# GAME CONSTANTS & PALETTE
# ==========================================
WINDOW_WIDTH = 800
WINDOW_HEIGHT = 600
FPS = 60

COLOR_NEON_BLUE = (0.2, 0.6, 1.0)
COLOR_NEON_PINK = (1.0, 0.2, 0.6)
COLOR_NEON_GREEN = (0.2, 1.0, 0.6)
COLOR_NEON_YELLOW = (1.0, 0.9, 0.2)

# ==========================================
# BCI INTERFACE CONTROLLER
# ==========================================
# Standalone BCIController class removed in favor of unified game_input.py module

# ==========================================
# 8-BIT SYNTHESIZED RETRO AUDIO GENERATOR
# ==========================================
def synthesize_laser_sound():
    sample_rate = 44100
    duration = 0.15
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    freq = np.geomspace(900, 200, len(t))
    val = np.sin(2 * np.pi * freq * t) * 0.25
    sound_arr = (val * 32767).astype(np.int16)
    sound_arr = np.repeat(sound_arr[:, np.newaxis], 2, axis=1) # Stereo
    return pygame.sndarray.make_sound(sound_arr)

def synthesize_explosion_sound():
    sample_rate = 44100
    duration = 0.3
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    val = (np.random.rand(len(t)) * 2 - 1) * np.exp(-15 * t) * 0.3
    sound_arr = (val * 32767).astype(np.int16)
    sound_arr = np.repeat(sound_arr[:, np.newaxis], 2, axis=1) # Stereo
    return pygame.sndarray.make_sound(sound_arr)

def synthesize_missile_sound():
    sample_rate = 44100
    duration = 0.35
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    freq = np.geomspace(200, 850, len(t))
    noise = (np.random.rand(len(t)) * 2 - 1) * 0.25
    val = (np.sin(2 * np.pi * freq * t) * 0.2 + noise) * np.exp(-3.5 * t)
    sound_arr = (val * 32767).astype(np.int16)
    sound_arr = np.repeat(sound_arr[:, np.newaxis], 2, axis=1)
    return pygame.sndarray.make_sound(sound_arr)

def synthesize_nova_sound():
    sample_rate = 44100
    duration = 0.85
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    sub = np.sin(2 * np.pi * np.geomspace(180, 30, len(t)) * t) * 0.45
    noise = (np.random.rand(len(t)) * 2 - 1) * np.exp(-4.5 * t) * 0.4
    shimmer = np.sin(2 * np.pi * np.geomspace(1200, 300, len(t)) * t) * np.exp(-6 * t) * 0.2
    val = np.clip((sub + noise + shimmer) * np.exp(-1.8 * t), -1.0, 1.0) * 0.35
    sound_arr = (val * 32767).astype(np.int16)
    sound_arr = np.repeat(sound_arr[:, np.newaxis], 2, axis=1)
    return pygame.sndarray.make_sound(sound_arr)

# ==========================================
# RETRO PLANE ARCADE GAME
# ==========================================
# ==========================================
# RETRO PLANE ARCADE GAME (1P / 2P MULTIPLAYER & BCI BUFFS)
# ==========================================
class PlayerJet:
    def __init__(self, player_id: int, start_x: float, start_y: float, color: tuple):
        self.id = player_id
        self.x = start_x
        self.y = start_y
        self.target_x = start_x
        self.target_y = start_y
        self.vx = 0.0
        self.vy = 0.0
        self.score = 0
        self.health = 100
        self.alive = True
        self.color = color
        
        # Cooldown timers
        self.shoot_cooldown = 0
        self.missile_cooldown = 0
        self.afterimage_history = []
        
        # ── Cognitive & Physiological Buff States ──────────────────────────────
        self.shield_active = False        # High Focus → 50% damage reduction & enemy deflection
        self.bullet_time_active = False   # Ultra Focus → Time dilation (enemies slowed by 45%)
        self.speed_boost_active = False   # Engine Overdrive → +40% speed boost
        self.zen_regen_active = False     # Deep Calm → Nanite repair (+1.5 HP/sec healing)
        self.spread_buff_active = False   # Spread Overdrive → 3-way triple multi-laser cannon
        self.missile_battery_active = False # Deep Calm → Auto-homing missile battery every 4s
        self.auto_missile_timer = 0
        # ──────────────────────────────────────────────────────────────────────

    def reset(self, start_x: float, start_y: float):
        self.x = start_x
        self.y = start_y
        self.target_x = start_x
        self.target_y = start_y
        self.vx = 0.0
        self.vy = 0.0
        self.health = 100
        self.alive = True
        self.shoot_cooldown = 0
        self.missile_cooldown = 0
        self.afterimage_history.clear()
        self.shield_active = False
        self.bullet_time_active = False
        self.speed_boost_active = False
        self.zen_regen_active = False
        self.spread_buff_active = False
        self.missile_battery_active = False
        self.auto_missile_timer = 0


class RetroPlaneGame:
    def __init__(self, fullscreen=False, two_player=False):
        global WINDOW_WIDTH, WINDOW_HEIGHT
        pygame.init()
        self.audio_enabled = False
        try:
            pygame.mixer.init()
            self.audio_enabled = True
        except pygame.error as e:
            print(f"Warning: Audio device not found ({e}). Running without audio.")
        
        self.is_fullscreen = False
        self.saved_window_size = (WINDOW_WIDTH, WINDOW_HEIGHT)
        
        flags = DOUBLEBUF | OPENGL | (FULLSCREEN if fullscreen else RESIZABLE)
        if fullscreen:
            info = pygame.display.Info()
            WINDOW_WIDTH, WINDOW_HEIGHT = info.current_w, info.current_h
            self.is_fullscreen = True

        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), flags)
        pygame.display.set_caption("🎮 OpenGL BCI Retro Fighter Jet (1P / 2P Co-Op & Cognitive Buffs)")
        
        if not opengl_available:
            print("❌ PyOpenGL is required to run this game.")
            pygame.quit()
            sys.exit()
            
        self.vp_x = 0
        self.vp_y = 0
        self.vp_w = WINDOW_WIDTH
        self.vp_h = WINDOW_HEIGHT
        self.update_screen_scaling()
        
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("Courier New", 16, bold=True)
        self.font_small = pygame.font.SysFont("Courier New", 13, bold=True)
        self.font_title = pygame.font.SysFont("Courier New", 26, bold=True)
        
        self.input_mgr = GameInputManager()
        
        # Audio Synthesis
        self.snd_laser = None
        self.snd_explosion = None
        self.snd_missile = None
        self.snd_nova = None
        if self.audio_enabled:
            try:
                self.snd_laser = synthesize_laser_sound()
                self.snd_explosion = synthesize_explosion_sound()
                self.snd_missile = synthesize_missile_sound()
                self.snd_nova = synthesize_nova_sound()
            except Exception as e:
                print(f"Audio Synthesis Warning: {e}. Running mute.")
                self.snd_laser = None
                self.snd_explosion = None
                self.snd_missile = None
                self.snd_nova = None

        # Game state variables
        self.two_player_mode = two_player
        self.game_over = False
        self.is_paused = False
        self.damage_flash_counter = 0
        self.nova_flash_counter = 0
        self.resonance_active = False
        self.resonance_damage_timer = 0
        
        # Initialize Players: P1 (Cyan) and P2 (Magenta) in normalized 800x600 space
        p1_x = 800 // 3 if self.two_player_mode else 400
        p2_x = 2 * 800 // 3
        self.p1 = PlayerJet(1, p1_x, 520, (0.0, 0.85, 1.0))
        self.p2 = PlayerJet(2, p2_x, 520, (1.0, 0.25, 0.75))
        
        # Scrolling Starfield
        self.stars = []
        for _ in range(120):
            self.stars.append({
                "x": random.randint(0, 800),
                "y": random.randint(0, 600),
                "speed": random.uniform(1.5, 4.2),
                "brightness": random.uniform(0.3, 1.0)
            })
            
        self.lasers = []
        self.missiles = []
        self.shockwaves = []
        self.nova_events = []
        self.enemies = []
        self.particles = []
        
        self.enemy_spawn_timer = 0
        self.touch_steering_enabled = True

    def update_screen_scaling(self):
        """Sets up hardware letterbox/pillarbox viewport preserving exact 800x600 logical coordinate space."""
        global WINDOW_WIDTH, WINDOW_HEIGHT
        target_aspect = 800.0 / 600.0
        win_w, win_h = max(1, WINDOW_WIDTH), max(1, WINDOW_HEIGHT)
        win_aspect = win_w / win_h

        if win_aspect > target_aspect:
            # Screen is wider than 4:3 -> Pillarbox (bars on left/right)
            self.vp_h = win_h
            self.vp_w = int(win_h * target_aspect)
            self.vp_x = (win_w - self.vp_w) // 2
            self.vp_y = 0
        else:
            # Screen is taller than 4:3 -> Letterbox (bars on top/bottom)
            self.vp_w = win_w
            self.vp_h = int(win_w / target_aspect)
            self.vp_x = 0
            self.vp_y = (win_h - self.vp_h) // 2

        glViewport(self.vp_x, self.vp_y, self.vp_w, self.vp_h)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, 800, 600, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()

    def toggle_fullscreen(self):
        """Toggles between resizable windowed mode and borderless/hardware fullscreen."""
        global WINDOW_WIDTH, WINDOW_HEIGHT
        self.is_fullscreen = not self.is_fullscreen
        if self.is_fullscreen:
            self.saved_window_size = (WINDOW_WIDTH, WINDOW_HEIGHT)
            info = pygame.display.Info()
            WINDOW_WIDTH, WINDOW_HEIGHT = info.current_w, info.current_h
            self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), DOUBLEBUF | OPENGL | FULLSCREEN)
        else:
            WINDOW_WIDTH, WINDOW_HEIGHT = self.saved_window_size
            self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), DOUBLEBUF | OPENGL | RESIZABLE)

        self.update_screen_scaling()
        print(f"🖥️ Display Mode: {'FULLSCREEN' if self.is_fullscreen else 'WINDOWED'} ({WINDOW_WIDTH}x{WINDOW_HEIGHT})")

    def toggle_2p_mode(self):
        self.two_player_mode = not self.two_player_mode
        self.reset_game()
        print(f"👥 Mode Switched: {'2-PLAYER CO-OP MODE' if self.two_player_mode else '1-PLAYER SOLO MODE'}")

    def reset_game(self):
        p1_x = WINDOW_WIDTH // 3 if self.two_player_mode else WINDOW_WIDTH // 2
        p2_x = 2 * WINDOW_WIDTH // 3
        self.p1.reset(p1_x, 520)
        self.p2.reset(p2_x, 520)
        self.game_over = False
        self.is_paused = False
        self.damage_flash_counter = 0
        self.nova_flash_counter = 0
        self.lasers.clear()
        self.missiles.clear()
        self.shockwaves.clear()
        self.enemies.clear()
        self.particles.clear()
        self.nova_events.clear()

    def handle_player_commands(self, player: PlayerJet, cmds: list[str]):
        """Executes player-scoped cognitive buff commands and action tokens."""
        for cmd in cmds:
            # 1. High Focus Buffs (Shield & Bullet Time)
            if cmd == "focus_shield_on":
                player.shield_active = True
                print(f"🧠 P{player.id} FOCUS SHIELD ACTIVATED — 50% damage reduction")
            elif cmd == "focus_shield_off":
                player.shield_active = False
                print(f"🧠 P{player.id} Focus shield deactivated")
            elif cmd in ("bullet_time_on", "chrono_on", "bullet_time"):
                player.bullet_time_active = True
                print(f"⏳ P{player.id} BULLET TIME / CHRONO SLOWDOWN ACTIVATED")
            elif cmd in ("bullet_time_off", "chrono_off"):
                player.bullet_time_active = False
                print(f"⏳ P{player.id} Bullet time deactivated")

            # 2. Deep Calm Buffs (Zen Regen & Missile Battery)
            elif cmd in ("zen_regen_on", "calm_on", "zen_on"):
                player.zen_regen_active = True
                print(f"🌿 P{player.id} ZEN REGENERATION ACTIVATED — continuous nanite repair")
            elif cmd in ("zen_regen_off", "calm_off", "zen_off"):
                player.zen_regen_active = False
                print(f"🌿 P{player.id} Zen regeneration deactivated")
            elif cmd in ("missile_battery_on", "auto_missile_on"):
                player.missile_battery_active = True
                print(f"🚀 P{player.id} MISSILE BATTERY ACTIVATED")
            elif cmd in ("missile_battery_off", "auto_missile_off"):
                player.missile_battery_active = False

            # 3. Spread Cannon Overdrive & Speed Boost
            elif cmd in ("boost_on", "speed_boost_on", "turbo_on"):
                player.speed_boost_active = True
                print(f"⚡ P{player.id} TURBO ENGINE OVERDRIVE (+40% speed)")
            elif cmd in ("boost_off", "speed_boost_off", "turbo_off"):
                player.speed_boost_active = False
                print(f"⚡ P{player.id} Turbo speed deactivated")
            elif cmd == "spread_buff_on":
                player.spread_buff_active = True
                print(f"🔥 P{player.id} SPREAD CANNON OVERDRIVE ACTIVATED — 3-way multi-laser")
            elif cmd == "spread_buff_off":
                player.spread_buff_active = False
                print(f"🔥 P{player.id} Spread cannon deactivated")
            elif cmd in ("toggle_spread_buff", "spread_buff", "spread"):
                player.spread_buff_active = not player.spread_buff_active
                print(f"🔥 P{player.id} Spread Cannon: {'ACTIVATED' if player.spread_buff_active else 'DEACTIVATED'}")

            # 4. Instant Attacks (Missile / Supernova)
            elif cmd in ("missile", "calm_missile", "voice_missile"):
                self.fire_missile(player)
            elif cmd in ("supernova", "nova", "voice_fire", "fire", "tert_action"):
                self.trigger_supernova(origin_x=player.x, origin_y=player.y)

    def handle_inputs(self, time_scale=1.0):
        global WINDOW_WIDTH, WINDOW_HEIGHT
        
        # 1. Update GameInputManager states
        self.input_mgr.update()

        # Handle global commands from main.py or stdin
        for cmd in list(self.input_mgr.cmds):
            if cmd in ("pause", "toggle_pause"):
                self.is_paused = not self.is_paused
            elif cmd in ("restart", "reset"):
                self.reset_game()
            elif cmd in ("fullscreen", "toggle_fullscreen"):
                self.toggle_fullscreen()
            elif cmd in ("enable_2p", "2p_on", "two_player"):
                if not self.two_player_mode:
                    self.toggle_2p_mode()
            elif cmd in ("disable_2p", "1p_on", "single_player"):
                if self.two_player_mode:
                    self.toggle_2p_mode()

        # Process per-player BCI cognitive buff commands
        self.handle_player_commands(self.p1, self.input_mgr.get_player_cmds(1))
        if self.two_player_mode:
            self.handle_player_commands(self.p2, self.input_mgr.get_player_cmds(2))
        else:
            # If P2 sends commands while in 1P mode, auto-switch to 2P mode!
            p2_cmds = self.input_mgr.get_player_cmds(2)
            if p2_cmds:
                self.toggle_2p_mode()
                self.handle_player_commands(self.p2, p2_cmds)

        # 2. Poll Pygame window events (quit, resize, key hotkeys)
        for event in pygame.event.get():
            if event.type == QUIT:
                self.input_mgr.running = False
                pygame.quit()
                sys.exit()
            elif event.type == KEYDOWN:
                if event.key == K_ESCAPE:
                    if self.is_fullscreen:
                        self.toggle_fullscreen()
                    else:
                        self.input_mgr.running = False
                        pygame.quit()
                        sys.exit()
                elif event.key == K_p:
                    self.is_paused = not self.is_paused
                elif event.key == K_r:
                    self.reset_game()
                elif event.key == K_2:
                    self.toggle_2p_mode()
                elif event.key == K_F11 or (event.key == K_RETURN and (event.mod & KMOD_ALT)):
                    self.toggle_fullscreen()
                elif event.key == K_t:
                    self.touch_steering_enabled = not self.touch_steering_enabled
                    print(f"🎮 Touch Steering: {'ENABLED' if self.touch_steering_enabled else 'DISABLED'}")
                
                # Hotkey Buff Toggles for Instant Testing:
                elif event.key == K_1:
                    self.p1.shield_active = not self.p1.shield_active
                    print(f"🧠 P1 Focus Shield: {'ACTIVE' if self.p1.shield_active else 'OFF'}")
                elif event.key == K_3:
                    self.p1.bullet_time_active = not self.p1.bullet_time_active
                    print(f"⏳ Bullet Time: {'ACTIVE' if self.p1.bullet_time_active else 'OFF'}")
                elif event.key == K_4:
                    self.p1.zen_regen_active = not self.p1.zen_regen_active
                    print(f"🌿 P1 Zen Regen: {'ACTIVE' if self.p1.zen_regen_active else 'OFF'}")
                elif event.key == K_5:
                    self.p1.spread_buff_active = not self.p1.spread_buff_active
                    print(f"🔥 P1 Spread Cannon: {'ACTIVE' if self.p1.spread_buff_active else 'OFF'}")
                elif event.key == K_6:
                    self.fire_missile(self.p1)
                elif event.key == K_b:
                    self.p1.speed_boost_active = not self.p1.speed_boost_active
                    print(f"⚡ P1 Turbo Boost: {'ACTIVE' if self.p1.speed_boost_active else 'OFF'}")
                elif event.key == K_m:
                    self.p1.missile_battery_active = not self.p1.missile_battery_active
                    print(f"🚀 P1 Missile Battery: {'ACTIVE' if self.p1.missile_battery_active else 'OFF'}")
                elif event.key == K_7 and self.two_player_mode:
                    self.p2.shield_active = not self.p2.shield_active
                    print(f"🧠 P2 Focus Shield: {'ACTIVE' if self.p2.shield_active else 'OFF'}")
                elif event.key == K_8 and self.two_player_mode:
                    self.p2.zen_regen_active = not self.p2.zen_regen_active
                    print(f"🌿 P2 Zen Regen: {'ACTIVE' if self.p2.zen_regen_active else 'OFF'}")
                elif event.key == K_9 and self.two_player_mode:
                    self.p2.spread_buff_active = not self.p2.spread_buff_active
                    print(f"🔥 P2 Spread Cannon: {'ACTIVE' if self.p2.spread_buff_active else 'OFF'}")
                elif event.key == K_0 and self.two_player_mode:
                    self.fire_missile(self.p2)
                elif event.key == K_o and self.two_player_mode:
                    self.p2.speed_boost_active = not self.p2.speed_boost_active
                    print(f"⚡ P2 Turbo Boost: {'ACTIVE' if self.p2.speed_boost_active else 'OFF'}")
                elif event.key == K_i and self.two_player_mode:
                    self.p2.missile_battery_active = not self.p2.missile_battery_active
                    print(f"🚀 P2 Missile Battery: {'ACTIVE' if self.p2.missile_battery_active else 'OFF'}")
                elif event.key in (K_f, K_n):
                    self.trigger_supernova(self.p1.x, self.p1.y)

            elif event.type == VIDEORESIZE:
                WINDOW_WIDTH, WINDOW_HEIGHT = event.w, event.h
                self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), DOUBLEBUF | OPENGL | RESIZABLE)
                self.update_screen_scaling()

        # Check restart on Game Over from any touch tap, touchpad action, or key
        if self.game_over:
            if (self.input_mgr.is_action(1) or 
                self.input_mgr.is_action(2) or 
                self.input_mgr.latest_touch_active.get(1, False) or 
                self.input_mgr.latest_touch_active.get(2, False) or
                "action" in self.input_mgr.cmds or
                "fire" in self.input_mgr.cmds or
                "restart" in self.input_mgr.cmds or
                "reset" in self.input_mgr.cmds):
                self.reset_game()
                return

        # Continuous silky-smooth velocity calculation in normalized 800x600 canvas
        p1_boost = 1.4 if self.p1.speed_boost_active else 1.0
        p1_h_speed = 9.5 * p1_boost
        p1_v_speed = 8.0 * p1_boost

        # 3. Handle Player 1 Movements & Actions
        if self.p1.alive:
            p1_steer_x = 0.0
            p1_steer_y = 0.0
            if self.input_mgr.is_left(1, self.two_player_mode):
                p1_steer_x -= p1_h_speed
            if self.input_mgr.is_right(1, self.two_player_mode):
                p1_steer_x += p1_h_speed
            if self.input_mgr.is_up(1, self.two_player_mode):
                p1_steer_y -= p1_v_speed
            if self.input_mgr.is_down(1, self.two_player_mode):
                p1_steer_y += p1_v_speed

            # Blend velocity
            if p1_steer_x != 0.0:
                self.p1.vx += (p1_steer_x - self.p1.vx) * (0.35 * time_scale)
            else:
                self.p1.vx *= (0.78 ** time_scale)

            if p1_steer_y != 0.0:
                self.p1.vy += (p1_steer_y - self.p1.vy) * (0.35 * time_scale)
            else:
                self.p1.vy *= (0.78 ** time_scale)
                
            if self.input_mgr.is_action(1):
                self.fire_laser(self.p1)
                    
            if self.input_mgr.is_sec_action(1):
                if not self.game_over:
                    self.p1.spread_buff_active = True
                    self.fire_laser(self.p1)
                            
            if self.input_mgr.is_tert_action(1):
                if not self.game_over:
                    self.trigger_supernova(self.p1.x, self.p1.y)

        # 4. Handle Player 2 Movements & Actions (if 2P mode active)
        if self.two_player_mode and self.p2.alive:
            p2_boost = 1.4 if self.p2.speed_boost_active else 1.0
            p2_h_speed = 9.5 * p2_boost
            p2_v_speed = 8.0 * p2_boost
            p2_steer_x = 0.0
            p2_steer_y = 0.0
            if self.input_mgr.is_left(2, True):
                p2_steer_x -= p2_h_speed
            if self.input_mgr.is_right(2, True):
                p2_steer_x += p2_h_speed
            if self.input_mgr.is_up(2, True):
                p2_steer_y -= p2_v_speed
            if self.input_mgr.is_down(2, True):
                p2_steer_y += p2_v_speed

            if p2_steer_x != 0.0:
                self.p2.vx += (p2_steer_x - self.p2.vx) * (0.35 * time_scale)
            else:
                self.p2.vx *= (0.78 ** time_scale)

            if p2_steer_y != 0.0:
                self.p2.vy += (p2_steer_y - self.p2.vy) * (0.35 * time_scale)
            else:
                self.p2.vy *= (0.78 ** time_scale)
                
            if self.input_mgr.is_action(2):
                self.fire_laser(self.p2)
                    
            if self.input_mgr.is_sec_action(2):
                if not self.game_over:
                    self.p2.spread_buff_active = True
                    self.fire_laser(self.p2)
                            
            if self.input_mgr.is_tert_action(2):
                if not self.game_over:
                    self.trigger_supernova(self.p2.x, self.p2.y)

        # 5. Continuous IMU tilt / touch steering from phone
        for p_id, p_jet in [(1, self.p1), (2, self.p2)]:
            if self.input_mgr.imu_connected.get(p_id, False) and not self.input_mgr.dashboard_controlled:
                p_boost = 1.4 if p_jet.speed_boost_active else 1.0
                if self.touch_steering_enabled and self.input_mgr.latest_touch_active.get(p_id, False):
                    tx = self.input_mgr.latest_touch_x.get(p_id, -1.0)
                    ty = self.input_mgr.latest_touch_y.get(p_id, -1.0)
                    if tx >= 0.0 and ty >= 0.0:
                        target_x = (tx / 360.0) * 800.0
                        target_y = (ty / 360.0) * 600.0
                        p_jet.x += (target_x - p_jet.x) * (0.25 * time_scale)
                        p_jet.y += (target_y - p_jet.y) * (0.25 * time_scale)
                else:
                    roll = self.input_mgr.latest_roll.get(p_id, 0.0)
                    if abs(roll) > 10.0:
                        target_vx = (np.clip(roll, -35.0, 35.0) / 35.0) * (11.5 * p_boost)
                        p_jet.vx += (target_vx - p_jet.vx) * (0.30 * time_scale)

        # 6. Apply Continuous Physics Integrator in normalized boundaries
        for p in [self.p1, self.p2] if self.two_player_mode else [self.p1]:
            if p.alive:
                p.x += p.vx * time_scale
                p.y += p.vy * time_scale
                p.x = max(28, min(772, p.x))
                p.y = max(35, min(565, p.y))

    def fire_laser(self, player: PlayerJet):
        if player.shoot_cooldown <= 0:
            if player.spread_buff_active:
                laser_col = (255, 110, 230) if player.id == 1 else (255, 180, 50)
                player.shoot_cooldown = 14
                self.lasers.append({"player_id": player.id, "x": player.x, "y": player.y - 20, "vx": 0.0, "vy": -11.0, "color": laser_col})
                self.lasers.append({"player_id": player.id, "x": player.x - 16, "y": player.y - 12, "vx": -3.2, "vy": -10.0, "color": laser_col})
                self.lasers.append({"player_id": player.id, "x": player.x + 16, "y": player.y - 12, "vx": 3.2, "vy": -10.0, "color": laser_col})
            else:
                laser_col = COLOR_NEON_GREEN if player.id == 1 else (0.2, 0.9, 1.0)
                player.shoot_cooldown = 12
                self.lasers.append({"player_id": player.id, "x": player.x, "y": player.y - 20, "vx": 0.0, "vy": -10.5, "color": laser_col})
            if self.snd_laser:
                self.snd_laser.play()

    def fire_missile(self, player: PlayerJet):
        """Launches twin guided homing missiles for the player."""
        if player.missile_cooldown <= 0:
            player.missile_cooldown = 24
            m_speed = 12.0
            self.missiles.append({
                "player_id": player.id,
                "x": player.x - 20,
                "y": player.y - 8,
                "vx": -3.2,
                "vy": -7.5,
                "speed": m_speed,
                "life": 130
            })
            self.missiles.append({
                "player_id": player.id,
                "x": player.x + 20,
                "y": player.y - 8,
                "vx": 3.2,
                "vy": -7.5,
                "speed": m_speed,
                "life": 130
            })
            if self.snd_missile:
                self.snd_missile.play()
            self.damage_flash_counter = 4

    def trigger_supernova(self, origin_x=None, origin_y=None):
        """Triggers a brilliant full-screen chromatic Supernova blast cleansing all hostiles."""
        ox = self.p1.x if origin_x is None else origin_x
        oy = (self.p1.y - 20) if origin_y is None else origin_y

        self.nova_events.append({
            "x": ox,
            "y": oy,
            "radius": 12.0,
            "max_radius": 1200.0,
            "expand_speed": 28.0,
            "alpha": 1.0,
            "starburst_angles": [random.uniform(0, 2 * math.pi) for _ in range(48)],
            "banner_timer": 50
        })

        for _ in range(70):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(4.0, 16.0)
            col = random.choice([
                (255, 255, 255), (255, 220, 30), (0, 240, 255), (255, 60, 220), (180, 70, 255)
            ])
            self.particles.append({
                "x": ox,
                "y": oy,
                "vx": math.cos(angle) * speed,
                "vy": math.sin(angle) * speed,
                "color": col,
                "alpha": 1.0,
                "size": random.uniform(3.0, 7.0)
            })

        if self.enemies:
            pts = len(self.enemies) * 35
            self.p1.score += pts
            if self.two_player_mode and self.p2.alive:
                self.p2.score += pts
            for enemy in self.enemies:
                self.spawn_particles(enemy["x"], enemy["y"], (255, 215, 0), count=18, speed_max=8.0)
            self.enemies.clear()

        if self.snd_nova:
            self.snd_nova.play()
        self.nova_flash_counter = 18
        self.damage_flash_counter = 6

    def spawn_enemy(self):
        self.enemy_spawn_timer += 1
        combined_score = self.p1.score + (self.p2.score if self.two_player_mode else 0)
        spawn_rate = max(20, 85 - int(combined_score / 20))
        if self.enemy_spawn_timer >= spawn_rate:
            self.enemy_spawn_timer = 0
            self.enemies.append({
                "x": random.randint(50, 750),
                "y": -20,
                "speed": random.uniform(2.2, 4.6 + min(3.2, combined_score / 250)),
                "w": 32,
                "h": 22
            })

    def spawn_particles(self, x, y, color, count=15, speed_max=6.0):
        for _ in range(count):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(1.5, speed_max)
            self.particles.append({
                "x": x,
                "y": y,
                "vx": math.cos(angle) * speed,
                "vy": math.sin(angle) * speed,
                "color": color,
                "alpha": 1.0,
                "size": random.uniform(2.0, 5.0)
            })

    def update(self, time_scale=1.0):
        if self.game_over or self.is_paused:
            return

        self.spawn_enemy()

        # Check Bullet Time active state
        bullet_time_on = self.p1.bullet_time_active or (self.two_player_mode and self.p2.bullet_time_active)
        game_speed_mod = 0.55 if bullet_time_on else 1.0

        # Update active players
        for p in [self.p1, self.p2] if self.two_player_mode else [self.p1]:
            if not p.alive:
                continue

            if p.shoot_cooldown > 0:
                p.shoot_cooldown -= 1
            if p.missile_cooldown > 0:
                p.missile_cooldown -= 1

            # Record position for bullet-time motion blur afterimages
            if p.bullet_time_active:
                p.afterimage_history.append((p.x, p.y, pygame.time.get_ticks()))
                p.afterimage_history = [item for item in p.afterimage_history if pygame.time.get_ticks() - item[2] < 250]
            else:
                p.afterimage_history.clear()

            # Zen HP Regeneration: +1.5 HP/sec
            if p.zen_regen_active and p.health < 100:
                p.health = min(100, p.health + 0.025 * time_scale)
                if random.random() > 0.7:
                    self.particles.append({
                        "x": p.x + random.uniform(-20, 20),
                        "y": p.y + random.uniform(-15, 15),
                        "vx": random.uniform(-0.5, 0.5),
                        "vy": random.uniform(-1.5, -0.5),
                        "color": (34, 197, 94), # Emerald green
                        "alpha": 0.85,
                        "size": random.uniform(2.5, 4.5)
                    })

            # Auto missile battery
            if p.missile_battery_active:
                p.auto_missile_timer += 1
                if p.auto_missile_timer >= 240: # every 4 seconds
                    p.auto_missile_timer = 0
                    self.fire_missile(p)

        # ── 2-Player Brainwave Resonance Lightning Tether ───────────────────────
        self.resonance_active = False
        if self.two_player_mode and self.p1.alive and self.p2.alive:
            if (self.p1.shield_active and self.p2.shield_active) or (self.p1.zen_regen_active and self.p2.zen_regen_active):
                self.resonance_active = True
                self.resonance_damage_timer += 1
                
                # Check line segment collision between P1 and P2 with enemies
                x1, y1 = self.p1.x, self.p1.y
                x2, y2 = self.p2.x, self.p2.y
                dx = x2 - x1
                dy = y2 - y1
                line_len_sq = dx*dx + dy*dy
                
                if line_len_sq > 0:
                    for enemy in self.enemies[:]:
                        ex, ey = enemy["x"], enemy["y"]
                        t = max(0.0, min(1.0, ((ex - x1)*dx + (ey - y1)*dy) / line_len_sq))
                        proj_x = x1 + t * dx
                        proj_y = y1 + t * dy
                        dist = math.hypot(ex - proj_x, ey - proj_y)
                        
                        if dist < 32: # Enemy touching resonance beam!
                            self.enemies.remove(enemy)
                            self.spawn_particles(enemy["x"], enemy["y"], (255, 240, 80), count=20, speed_max=9.0)
                            self.p1.score += 50
                            self.p2.score += 50
                            if self.snd_explosion:
                                self.snd_explosion.play()

        # Update Stars
        for star in self.stars:
            star["y"] += star["speed"] * game_speed_mod * time_scale
            if star["y"] > 600:
                star["y"] = 0
                star["x"] = random.randint(0, 800)

        # Update Guided Missiles
        for m in self.missiles[:]:
            m["life"] -= 1 * time_scale
            if self.enemies:
                nearest = min(self.enemies, key=lambda e: (e["x"] - m["x"])**2 + (e["y"] - m["y"])**2)
                dx = nearest["x"] - m["x"]
                dy = nearest["y"] - m["y"]
                dist = math.hypot(dx, dy)
                if dist > 0:
                    target_vx = (dx / dist) * m["speed"]
                    target_vy = (dy / dist) * m["speed"]
                    m["vx"] += (target_vx - m["vx"]) * (0.22 * time_scale)
                    m["vy"] += (target_vy - m["vy"]) * (0.22 * time_scale)
            else:
                m["vy"] = -m["speed"]

            m["x"] += m["vx"] * time_scale
            m["y"] += m["vy"] * time_scale

            if random.random() > 0.15:
                self.particles.append({
                    "x": m["x"] + random.uniform(-2, 2),
                    "y": m["y"] + 10,
                    "vx": random.uniform(-0.6, 0.6) - m["vx"] * 0.1,
                    "vy": random.uniform(1.5, 3.5),
                    "color": (255, random.randint(150, 240), 40),
                    "alpha": 0.9,
                    "size": random.uniform(2.5, 4.5)
                })

            m_hit = False
            m_pos = (m["x"], m["y"])
            for enemy in self.enemies[:]:
                e_rect = pygame.Rect(enemy["x"] - enemy["w"]//2, enemy["y"] - enemy["h"]//2, enemy["w"], enemy["h"])
                if e_rect.collidepoint(m_pos) or math.hypot(enemy["x"] - m["x"], enemy["y"] - m["y"]) < 28:
                    self.enemies.remove(enemy)
                    self.spawn_particles(enemy["x"], enemy["y"], (252, 211, 77), count=25, speed_max=8.0)
                    if m.get("player_id", 1) == 1:
                        self.p1.score += 40
                    else:
                        self.p2.score += 40
                    m_hit = True

            if m_hit or m["y"] < -20 or m["life"] <= 0:
                self.shockwaves.append({
                    "x": m["x"], "y": m["y"], "radius": 8.0, "max_radius": 115.0, "alpha": 1.0, "color": (255, 215, 0)
                })
                if self.snd_explosion:
                    self.snd_explosion.play()
                self.damage_flash_counter = max(self.damage_flash_counter, 6)
                if m in self.missiles:
                    self.missiles.remove(m)

        # Update Expanding Shockwaves
        for sw in self.shockwaves[:]:
            sw["radius"] += 7.5 * time_scale
            sw["alpha"] -= 0.045 * time_scale
            for enemy in self.enemies[:]:
                if math.hypot(enemy["x"] - sw["x"], enemy["y"] - sw["y"]) < sw["radius"]:
                    self.enemies.remove(enemy)
                    self.spawn_particles(enemy["x"], enemy["y"], (255, 220, 50), count=15)
                    self.p1.score += 25
            if sw["alpha"] <= 0 or sw["radius"] >= sw["max_radius"]:
                self.shockwaves.remove(sw)

        # Update Supernova Events
        for nova in self.nova_events[:]:
            nova["radius"] += nova["expand_speed"] * time_scale
            nova["alpha"] -= 0.024 * time_scale
            if "banner_timer" in nova and nova["banner_timer"] > 0:
                nova["banner_timer"] -= 1 * time_scale
            for enemy in self.enemies[:]:
                if math.hypot(enemy["x"] - nova["x"], enemy["y"] - nova["y"]) < nova["radius"]:
                    self.enemies.remove(enemy)
                    self.spawn_particles(enemy["x"], enemy["y"], (255, 215, 0), count=15)
                    self.p1.score += 25
            if nova["alpha"] <= 0 or nova["radius"] >= nova["max_radius"]:
                self.nova_events.remove(nova)

        if self.nova_flash_counter > 0:
            self.nova_flash_counter -= 1

        # Update Lasers
        for laser in self.lasers[:]:
            laser["x"] += laser.get("vx", 0.0) * time_scale
            laser["y"] += laser.get("vy", -10.5) * time_scale
            if laser["y"] < -20 or laser["x"] < -20 or laser["x"] > 820:
                self.lasers.remove(laser)

        # Update Enemies (Speed scaled by Bullet Time)
        for enemy in self.enemies[:]:
            enemy["y"] += enemy["speed"] * game_speed_mod * time_scale

            # Bottom bounds leak
            if enemy["y"] > 620:
                self.enemies.remove(enemy)
                shielded = self.p1.shield_active or (self.two_player_mode and self.p2.shield_active)
                dmg = 2 if shielded else 5
                if self.p1.alive:
                    self.p1.health = max(0, self.p1.health - dmg)
                    if self.p1.health <= 0: self.p1.alive = False
                if self.two_player_mode and self.p2.alive:
                    self.p2.health = max(0, self.p2.health - dmg)
                    if self.p2.health <= 0: self.p2.alive = False
                self.damage_flash_counter = 12
                if not self.p1.alive and (not self.two_player_mode or not self.p2.alive):
                    self.game_over = True
                continue

            # Laser collision check
            enemy_rect = pygame.Rect(enemy["x"] - enemy["w"]//2, enemy["y"] - enemy["h"]//2, enemy["w"], enemy["h"])
            for laser in self.lasers[:]:
                if enemy_rect.collidepoint(laser["x"], laser["y"]):
                    self.lasers.remove(laser)
                    self.enemies.remove(enemy)
                    self.spawn_particles(enemy["x"], enemy["y"], COLOR_NEON_PINK)
                    if self.snd_explosion:
                        self.snd_explosion.play()
                    if laser.get("player_id", 1) == 1:
                        self.p1.score += 20
                    else:
                        self.p2.score += 20
                    break

            # Player collision check
            for p in [self.p1, self.p2] if self.two_player_mode else [self.p1]:
                if not p.alive:
                    continue
                p_rect = pygame.Rect(p.x - 20, p.y - 15, 40, 30)
                if p_rect.colliderect(enemy_rect):
                    if enemy in self.enemies:
                        self.enemies.remove(enemy)
                    self.spawn_particles(enemy["x"], enemy["y"], COLOR_NEON_YELLOW)
                    if self.snd_explosion:
                        self.snd_explosion.play()
                    dmg = 10 if p.shield_active else 20
                    p.health = max(0, p.health - dmg)
                    if p.health <= 0:
                        p.alive = False
                    self.damage_flash_counter = 15
                    if not self.p1.alive and (not self.two_player_mode or not self.p2.alive):
                        self.game_over = True

        # Update Particles
        for p in self.particles[:]:
            p["x"] += p["vx"] * time_scale
            p["y"] += p["vy"] * time_scale
            p["alpha"] -= 0.025 * time_scale
            if p["alpha"] <= 0:
                self.particles.remove(p)

    def draw_player_jet(self, player: PlayerJet):
        px, py = player.x, player.y
        col = player.color
        now = pygame.time.get_ticks()

        # 1. Motion Blur Afterimages for Bullet Time
        if player.bullet_time_active and player.afterimage_history:
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            for ax, ay, t in player.afterimage_history:
                alpha = max(0.0, 1.0 - (now - t) / 250.0) * 0.40
                glColor4f(col[0], col[1], col[2], alpha)
                glLineWidth(1.5)
                glBegin(GL_LINE_LOOP)
                glVertex2f(ax, ay - 20)
                glVertex2f(ax - 15, ay - 5)
                glVertex2f(ax - 25, ay + 10)
                glVertex2f(ax - 8, ay + 8)
                glVertex2f(ax, ay + 15)
                glVertex2f(ax + 8, ay + 8)
                glVertex2f(ax + 25, ay + 10)
                glVertex2f(ax + 15, ay - 5)
                glEnd()
            glDisable(GL_BLEND)

        # 2. Main Jet Body
        glLineWidth(2.5)
        glColor3f(*col)
        glBegin(GL_LINE_LOOP)
        glVertex2f(px, py - 20)      # Nose
        glVertex2f(px - 15, py - 5)   # Left fuselage
        glVertex2f(px - 25, py + 10)  # Left wing tip
        glVertex2f(px - 8, py + 8)    # Left thruster
        glVertex2f(px, py + 15)       # Tail
        glVertex2f(px + 8, py + 8)    # Right thruster
        glVertex2f(px + 25, py + 10)  # Right wing tip
        glVertex2f(px + 15, py - 5)   # Right fuselage
        glEnd()

        # Cockpit canopy
        glColor3f(0.85, 0.95, 1.0)
        glLineWidth(1.5)
        glBegin(GL_LINE_LOOP)
        glVertex2f(px, py - 14)
        glVertex2f(px - 4, py - 2)
        glVertex2f(px, py + 4)
        glVertex2f(px + 4, py - 2)
        glEnd()

        # 3. Engine Exhaust / Turbo Afterburner Sprite
        if player.speed_boost_active:
            # Dual supercharged cyan/orange afterburner flames
            flame_len = 28 + random.uniform(-4, 6)
            glLineWidth(3.0)
            glColor3f(0.0, 0.9, 1.0)
            glBegin(GL_LINES)
            glVertex2f(px - 8, py + 8); glVertex2f(px - 8, py + 8 + flame_len)
            glVertex2f(px + 8, py + 8); glVertex2f(px + 8, py + 8 + flame_len)
            glEnd()
            glColor3f(1.0, 0.9, 0.3)
            glLineWidth(1.5)
            glBegin(GL_LINES)
            glVertex2f(px - 8, py + 8); glVertex2f(px - 8, py + 8 + flame_len * 0.6)
            glVertex2f(px + 8, py + 8); glVertex2f(px + 8, py + 8 + flame_len * 0.6)
            glEnd()
            # Speed streaks
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glColor4f(0.2, 0.8, 1.0, 0.5)
            glLineWidth(1.2)
            glBegin(GL_LINES)
            glVertex2f(px - 25, py + 10); glVertex2f(px - 25, py + 35)
            glVertex2f(px + 25, py + 10); glVertex2f(px + 25, py + 35)
            glEnd()
            glDisable(GL_BLEND)
        else:
            # Standard thruster flame
            if random.random() > 0.35:
                glColor3f(*COLOR_NEON_YELLOW)
                glLineWidth(2.0)
                glBegin(GL_LINES)
                glVertex2f(px - 4, py + 10); glVertex2f(px - 4, py + 22 + random.uniform(0, 4))
                glVertex2f(px + 4, py + 10); glVertex2f(px + 4, py + 22 + random.uniform(0, 4))
                glEnd()

        # 4. Missile Battery Pod Sprites
        if player.missile_battery_active:
            # Left & Right Wing Rocket Pods
            for sign in (-1, 1):
                pod_x = px + sign * 26
                pod_y = py + 4
                glColor3f(0.9, 0.7, 0.1) # Amber pod housing
                glLineWidth(2.0)
                glBegin(GL_LINE_LOOP)
                glVertex2f(pod_x - 3, pod_y - 8)
                glVertex2f(pod_x + 3, pod_y - 8)
                glVertex2f(pod_x + 3, pod_y + 8)
                glVertex2f(pod_x - 3, pod_y + 8)
                glEnd()
                # Armed missile warhead tip
                glColor3f(1.0, 0.3, 0.1)
                glPointSize(4.0)
                glBegin(GL_POINTS)
                glVertex2f(pod_x, pod_y - 10)
                glEnd()

        # 5. Spread Cannon Wingtip Capacitors
        if player.spread_buff_active:
            pulse = 0.65 + 0.35 * math.sin(now * 0.012)
            glColor3f(1.0 * pulse, 0.35 * pulse, 0.9 * pulse)
            glPointSize(6.0)
            glBegin(GL_POINTS)
            glVertex2f(px - 25, py + 10); glVertex2f(px + 25, py + 10)
            glVertex2f(px, py - 20) # Nose emitter
            glEnd()
            glLineWidth(2.5)
            glBegin(GL_LINES)
            glVertex2f(px - 25, py + 10); glVertex2f(px - 25, py + 2)
            glVertex2f(px + 25, py + 10); glVertex2f(px + 25, py + 2)
            glVertex2f(px, py - 20); glVertex2f(px, py - 26)
            glEnd()

        # 6. Focus Shield (Pulsing Hexagonal Energy Forcefield)
        if player.shield_active:
            alpha = 0.55 + 0.35 * math.sin(now * 0.006)
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            s_col = (0.0, 0.85, 1.0) if player.id == 1 else (1.0, 0.3, 0.8)
            glColor4f(s_col[0], s_col[1], s_col[2], alpha)
            glLineWidth(2.2)
            r = 38
            n = 6
            rot_offset = now * 0.0015
            glBegin(GL_LINE_LOOP)
            for i in range(n):
                angle = 2.0 * math.pi * i / n - math.pi / 2 + rot_offset
                glVertex2f(px + r * math.cos(angle), py + r * math.sin(angle))
            glEnd()
            # Inner resonant ring
            glLineWidth(1.2)
            glColor4f(1.0, 1.0, 1.0, alpha * 0.5)
            glBegin(GL_LINE_LOOP)
            for i in range(12):
                angle = 2.0 * math.pi * i / 12 - rot_offset
                glVertex2f(px + (r * 0.7) * math.cos(angle), py + (r * 0.7) * math.sin(angle))
            glEnd()
            glDisable(GL_BLEND)

        # 7. Zen Healing Orbital Nanite Aura
        if player.zen_regen_active:
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            z_alpha = 0.35 + 0.25 * math.sin(now * 0.008)
            glColor4f(0.1, 0.95, 0.4, z_alpha)
            glLineWidth(1.8)
            glBegin(GL_LINE_LOOP)
            for i in range(16):
                th = 2.0 * math.pi * i / 16
                glVertex2f(px + 32 * math.cos(th), py + 32 * math.sin(th))
            glEnd()
            # 4 Orbital Nanite Sparks circling the jet
            glPointSize(5.0)
            glColor4f(0.3, 1.0, 0.6, 0.9)
            glBegin(GL_POINTS)
            for k in range(4):
                orb_angle = now * 0.004 + (k * math.pi / 2)
                glVertex2f(px + 32 * math.cos(orb_angle), py + 32 * math.sin(orb_angle))
            glEnd()
            glDisable(GL_BLEND)

    def draw_resonance_tether(self):
        """Renders an animated high-voltage electric plasma tether between P1 and P2."""
        if not self.resonance_active or not self.p1.alive or not self.p2.alive:
            return

        x1, y1 = self.p1.x, self.p1.y
        x2, y2 = self.p2.x, self.p2.y

        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        
        # Outer electric glow
        glLineWidth(4.0)
        glColor4f(1.0, 0.85, 0.1, 0.75)
        glBegin(GL_LINE_STRIP)
        num_segments = 14
        for i in range(num_segments + 1):
            t = i / float(num_segments)
            seg_x = x1 + t * (x2 - x1)
            seg_y = y1 + t * (y2 - y1)
            if 0 < i < num_segments:
                seg_x += random.uniform(-6, 6)
                seg_y += random.uniform(-6, 6)
            glVertex2f(seg_x, seg_y)
        glEnd()

        # Inner hot white core beam
        glLineWidth(2.0)
        glColor4f(1.0, 1.0, 1.0, 0.95)
        glBegin(GL_LINE_STRIP)
        for i in range(num_segments + 1):
            t = i / float(num_segments)
            seg_x = x1 + t * (x2 - x1) + (random.uniform(-2, 2) if 0 < i < num_segments else 0)
            seg_y = y1 + t * (y2 - y1) + (random.uniform(-2, 2) if 0 < i < num_segments else 0)
            glVertex2f(seg_x, seg_y)
        glEnd()

        glDisable(GL_BLEND)

    def draw_enemy(self, enemy):
        ex, ey = enemy["x"], enemy["y"]
        w, h = enemy["w"], enemy["h"]
        glColor3f(*COLOR_NEON_PINK)
        glLineWidth(2.0)
        glBegin(GL_LINE_LOOP)
        glVertex2f(ex, ey - h//2)
        glVertex2f(ex - w//2, ey)
        glVertex2f(ex - w//3, ey + h//2)
        glVertex2f(ex + w//3, ey + h//2)
        glVertex2f(ex + w//2, ey)
        glEnd()
        glPointSize(3.0)
        glBegin(GL_POINTS)
        glVertex2f(ex - 6, ey - 2); glVertex2f(ex + 6, ey - 2)
        glEnd()

    def draw_laser(self, laser):
        col = laser.get("color", COLOR_NEON_GREEN)
        glColor3f(*col)
        glLineWidth(3.0)
        lx, ly = laser["x"], laser["y"]
        vx = laser.get("vx", 0.0)
        vy = laser.get("vy", -9.5)
        glBegin(GL_LINES)
        glVertex2f(lx, ly)
        glVertex2f(lx - vx * 1.5, ly - vy * 1.5)
        glEnd()

    def draw_missile(self, m):
        mx, my = m["x"], m["y"]
        angle = math.atan2(m["vy"], m["vx"]) + math.pi / 2
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        def rot(dx, dy): return mx + (dx * cos_a - dy * sin_a), my + (dx * sin_a + dy * cos_a)

        glColor3f(1.0, 0.84, 0.0)
        glLineWidth(2.5)
        glBegin(GL_LINE_LOOP)
        p1 = rot(0, -12); p2 = rot(-4, -4); p3 = rot(-3, 8); p4 = rot(3, 8); p5 = rot(4, -4)
        glVertex2f(*p1); glVertex2f(*p2); glVertex2f(*p3); glVertex2f(*p4); glVertex2f(*p5)
        glEnd()

        # Missile thruster flame
        if random.random() > 0.2:
            glColor3f(1.0, 0.4, 0.0)
            glLineWidth(2.0)
            glBegin(GL_LINES)
            f1 = rot(-2, 8); f2 = rot(0, 16 + random.uniform(0, 4)); f3 = rot(2, 8)
            glVertex2f(*f1); glVertex2f(*f2)
            glVertex2f(*f3); glVertex2f(*f2)
            glEnd()

    def draw_shockwave(self, sw):
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        col = sw.get("color", (255, 215, 0))
        glColor4f(col[0]/255.0, col[1]/255.0, col[2]/255.0, max(0.0, sw["alpha"]))
        glLineWidth(3.0)
        r = sw["radius"]
        num_segments = 32
        glBegin(GL_LINE_LOOP)
        for i in range(num_segments):
            theta = 2.0 * math.pi * i / num_segments
            glVertex2f(sw["x"] + r * math.cos(theta), sw["y"] + r * math.sin(theta))
        glEnd()
        glDisable(GL_BLEND)

    def draw_supernova(self, n):
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        nx, ny = n["x"], n["y"]
        r = n["radius"]
        alpha = max(0.0, n["alpha"])

        glLineWidth(2.0)
        glColor4f(1.0, 0.95, 0.5, alpha * 0.75)
        glBegin(GL_LINES)
        for angle in n.get("starburst_angles", []):
            ray_len = min(r * 1.35, 1200.0)
            glVertex2f(nx, ny)
            glVertex2f(nx + ray_len * math.cos(angle), ny + ray_len * math.sin(angle))
        glEnd()

        chroma_layers = [
            ((1.0, 1.0, 1.0), 4.5, 0.0), ((1.0, 0.85, 0.0), 3.5, -20.0),
            ((0.0, 0.95, 1.0), 3.0, -45.0), ((0.9, 0.2, 1.0), 2.5, -70.0)
        ]
        num_segments = 36
        for color, width, offset in chroma_layers:
            curr_r = r + offset
            if curr_r <= 0: continue
            glColor4f(color[0], color[1], color[2], alpha * 0.85)
            glLineWidth(width)
            glBegin(GL_LINE_LOOP)
            for i in range(num_segments):
                theta = 2.0 * math.pi * i / num_segments
                glVertex2f(nx + curr_r * math.cos(theta), ny + curr_r * math.sin(theta))
            glEnd()
        glDisable(GL_BLEND)

        if n.get("banner_timer", 0) > 0:
            self.draw_text("💥 🌟 SUPERNOVA CLEANSING! 🌟 💥", 205, 95, self.font_title, (255, 230, 0))

    def draw_particle(self, p):
        glColor4f(p["color"][0], p["color"][1], p["color"][2], p["alpha"])
        glPointSize(p["size"])
        glBegin(GL_POINTS)
        glVertex2f(p["x"], p["y"])
        glEnd()

    def draw_text(self, text, x, y, font, color=(255, 255, 255)):
        text_surface = font.render(text, True, color)
        text_data = pygame.image.tostring(text_surface, "RGBA", True)
        width, height = text_surface.get_size()
        
        glEnable(GL_TEXTURE_2D)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        
        tex_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, text_data)
        glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex2f(x, y + height)
        glTexCoord2f(1, 0); glVertex2f(x + width, y + height)
        glTexCoord2f(1, 1); glVertex2f(x + width, y)
        glTexCoord2f(0, 1); glVertex2f(x, y)
        glEnd()
        
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_BLEND)
        glDeleteTextures([tex_id])

    def draw_hud(self):
        # ── PLAYER 1 HUD (Top-Left) ──
        self.draw_text("P1 [CYAN]:", 20, 16, self.font, (56, 189, 248))
        
        # P1 Health Bar Outline & Fill
        glColor3f(0.2, 0.2, 0.3)
        glBegin(GL_QUADS)
        glVertex2f(110, 18); glVertex2f(230, 18); glVertex2f(230, 30); glVertex2f(110, 30)
        glEnd()
        if self.p1.health > 0:
            h_pct = self.p1.health / 100.0
            glColor3f(1.0 - h_pct, h_pct, 0.2)
            glBegin(GL_QUADS)
            glVertex2f(112, 20); glVertex2f(112 + int(116 * h_pct), 20)
            glVertex2f(112 + int(116 * h_pct), 28); glVertex2f(112, 28)
            glEnd()
            
        self.draw_text(f"SCORE: {self.p1.score}", 240, 16, self.font, (22, 242, 179))

        # P1 Buff Badges
        p1_buff_y = 38
        if self.p1.shield_active:
            self.draw_text("🛡️ FOCUS SHIELD ACTIVE [-50% DMG]", 20, p1_buff_y, self.font_small, (0, 220, 255))
            p1_buff_y += 18
        if self.p1.bullet_time_active:
            self.draw_text("⏳ BULLET TIME ACTIVE [TIME SLOW 45%]", 20, p1_buff_y, self.font_small, (147, 197, 253))
            p1_buff_y += 18
        if self.p1.speed_boost_active:
            self.draw_text("⚡ TURBO OVERDRIVE ACTIVE [+40% SPEED]", 20, p1_buff_y, self.font_small, (56, 189, 248))
            p1_buff_y += 18
        if self.p1.zen_regen_active:
            self.draw_text("🌿 ZEN REGEN ACTIVE [+1.5 HP/s]", 20, p1_buff_y, self.font_small, (52, 211, 153))
            p1_buff_y += 18
        if self.p1.spread_buff_active:
            self.draw_text("🔥 SPREAD CANNON ACTIVE [3-WAY]", 20, p1_buff_y, self.font_small, (244, 114, 182))
            p1_buff_y += 18
        if self.p1.missile_battery_active:
            self.draw_text("🚀 MISSILE BATTERY ARMED [AUTO-HOMING]", 20, p1_buff_y, self.font_small, (251, 191, 36))
            p1_buff_y += 18

        # ── PLAYER 2 HUD (Top-Right, if 2P Mode) ──
        if self.two_player_mode:
            p2_base_x = 440
            self.draw_text("P2 [PINK]:", p2_base_x, 16, self.font, (244, 114, 182))
            
            glColor3f(0.2, 0.2, 0.3)
            glBegin(GL_QUADS)
            glVertex2f(p2_base_x + 90, 18); glVertex2f(p2_base_x + 210, 18)
            glVertex2f(p2_base_x + 210, 30); glVertex2f(p2_base_x + 90, 30)
            glEnd()
            if self.p2.health > 0:
                h_pct2 = self.p2.health / 100.0
                glColor3f(1.0 - h_pct2, h_pct2, 0.2)
                glBegin(GL_QUADS)
                glVertex2f(p2_base_x + 92, 20); glVertex2f(p2_base_x + 92 + int(116 * h_pct2), 20)
                glVertex2f(p2_base_x + 92 + int(116 * h_pct2), 28); glVertex2f(p2_base_x + 92, 28)
                glEnd()
                
            self.draw_text(f"SCORE: {self.p2.score}", p2_base_x + 220, 16, self.font, (244, 114, 182))

            # P2 Buff Badges
            p2_buff_y = 38
            if self.p2.shield_active:
                self.draw_text("🛡️ FOCUS SHIELD ACTIVE [-50% DMG]", p2_base_x, p2_buff_y, self.font_small, (244, 114, 182))
                p2_buff_y += 18
            if self.p2.bullet_time_active:
                self.draw_text("⏳ BULLET TIME ACTIVE [TIME SLOW 45%]", p2_base_x, p2_buff_y, self.font_small, (147, 197, 253))
                p2_buff_y += 18
            if self.p2.speed_boost_active:
                self.draw_text("⚡ TURBO OVERDRIVE ACTIVE [+40% SPEED]", p2_base_x, p2_buff_y, self.font_small, (244, 114, 182))
                p2_buff_y += 18
            if self.p2.zen_regen_active:
                self.draw_text("🌿 ZEN REGEN ACTIVE [+1.5 HP/s]", p2_base_x, p2_buff_y, self.font_small, (52, 211, 153))
                p2_buff_y += 18
            if self.p2.spread_buff_active:
                self.draw_text("🔥 SPREAD CANNON ACTIVE [3-WAY]", p2_base_x, p2_buff_y, self.font_small, (251, 146, 60))
                p2_buff_y += 18
            if self.p2.missile_battery_active:
                self.draw_text("🚀 MISSILE BATTERY ARMED [AUTO-HOMING]", p2_base_x, p2_buff_y, self.font_small, (251, 191, 36))
                p2_buff_y += 18

        # ── CENTER HUD (Mode & Resonance Status) ──
        mode_text = "👥 2-PLAYER CO-OP MODE" if self.two_player_mode else "👤 1-PLAYER SOLO MODE [Press '2' for 2P]"
        self.draw_text(mode_text, 260, 16, self.font, (148, 163, 184))
        
        if self.resonance_active:
            self.draw_text("⚡ RESONANCE TETHER BURNING FOES! ⚡", 240, 42, self.font, (250, 204, 21))

        # Bottom Bar Status & Hotkeys
        hud_bottom_y = 572
        hotkeys_text = "[WASD/Space]: P1  |  [Arrows/Enter]: P2  |  [2]: Toggle 2P  |  [1-5]: P1 Buffs  |  [7-0]: P2 Buffs  |  [F]: Nova"
        self.draw_text(hotkeys_text, 20, hud_bottom_y, self.font_small, (100, 116, 139))

    def draw(self):
        glClearColor(0.02, 0.05, 0.10, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        
        # Starfield
        glBegin(GL_POINTS)
        for star in self.stars:
            glColor4f(1.0, 1.0, 1.0, star["brightness"])
            glVertex2f(star["x"], star["y"])
        glEnd()
        
        # Particles
        for p in self.particles:
            self.draw_particle(p)
            
        # Lasers
        for laser in self.lasers:
            self.draw_laser(laser)

        # Guided Missiles
        for missile in self.missiles:
            self.draw_missile(missile)

        # Shockwaves
        for shockwave in self.shockwaves:
            self.draw_shockwave(shockwave)

        # Supernova Events
        for nova in self.nova_events:
            self.draw_supernova(nova)

        # Resonance Tether between P1 and P2
        self.draw_resonance_tether()
            
        # Enemies
        for enemy in self.enemies:
            self.draw_enemy(enemy)

        # Players
        if not self.game_over:
            if self.p1.alive:
                self.draw_player_jet(self.p1)
            if self.two_player_mode and self.p2.alive:
                self.draw_player_jet(self.p2)
        else:
            self.draw_text("GAME OVER", 330, 260, self.font_title, (239, 68, 68))
            self.draw_text("Press 'R', Space, or Touch to Restart", 255, 305, self.font, (148, 163, 184))

        self.draw_hud()

        # Pause Overlay
        if self.is_paused:
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glColor4f(0.02, 0.05, 0.12, 0.75)
            glBegin(GL_QUADS)
            glVertex2f(0, 0); glVertex2f(800, 0); glVertex2f(800, 600); glVertex2f(0, 600)
            glEnd()
            glDisable(GL_BLEND)
            self.draw_text("⏸️ PAUSED", 345, 265, self.font_title, (56, 189, 248))
            self.draw_text("Press 'P' to Resume  |  Press 'R' to Restart", 200, 310, self.font, (226, 232, 240))

        # Supernova golden boundary flash
        if self.nova_flash_counter > 0:
            glLineWidth(8.0)
            glColor4f(1.0, 0.85, 0.0, 0.7)
            glBegin(GL_LINE_LOOP)
            glVertex2f(0, 0); glVertex2f(800, 0); glVertex2f(800, 600); glVertex2f(0, 600)
            glEnd()

        # Damage flash boundary overlay
        if self.damage_flash_counter > 0:
            glLineWidth(6.0)
            glColor4f(1.0, 0.1, 0.1, 0.5)
            glBegin(GL_LINE_LOOP)
            glVertex2f(0, 0); glVertex2f(800, 0); glVertex2f(800, 600); glVertex2f(0, 600)
            glEnd()

    def run(self):
        while True:
            raw_ms = self.clock.tick(FPS)
            dt = min(0.066, max(0.001, raw_ms / 1000.0))
            time_scale = dt * 60.0

            self.handle_inputs(time_scale)
            self.update(time_scale)
            self.draw()
            
            pygame.display.flip()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retro Fighter Jet BCI Arcade Game")
    parser.add_argument("--fullscreen", "-f", action="store_true", help="Launch in fullscreen mode")
    parser.add_argument("--players", "-p", type=int, default=1, choices=[1, 2], help="Number of players (1 or 2)")
    args = parser.parse_args()
    game = RetroPlaneGame(fullscreen=args.fullscreen, two_player=(args.players == 2))
    game.run()
