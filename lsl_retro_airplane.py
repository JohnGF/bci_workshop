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
class RetroPlaneGame:
    def __init__(self, fullscreen=False):
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
        pygame.display.set_caption("🎮 OpenGL BCI Retro Fighter Jet")
        
        if not opengl_available:
            print("❌ PyOpenGL is required to run this game.")
            sys.exit(1)
            
        glViewport(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, WINDOW_WIDTH, WINDOW_HEIGHT, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("Courier New", 18, bold=True)
        self.font_title = pygame.font.SysFont("Courier New", 26, bold=True)
        
        self.input_mgr = GameInputManager()
        
        # Synthesize audio on launch
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
        self.player_x = WINDOW_WIDTH // 2
        self.player_y = 520
        self.player_target_x = WINDOW_WIDTH // 2
        self.player_target_y = 520
        self.score = 0
        self.health = 100
        self.game_over = False
        self.damage_flash_counter = 0
        self.nova_flash_counter = 0

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

        glViewport(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, WINDOW_WIDTH, WINDOW_HEIGHT, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        print(f"🖥️ Display Mode: {'FULLSCREEN' if self.is_fullscreen else 'WINDOWED'} ({WINDOW_WIDTH}x{WINDOW_HEIGHT})")
        
        # Scrolling Starfield
        self.stars = []
        for _ in range(120):
            self.stars.append({
                "x": random.randint(0, WINDOW_WIDTH),
                "y": random.randint(0, WINDOW_HEIGHT),
                "speed": random.uniform(1.5, 4.5),
                "brightness": random.uniform(0.3, 1.0)
            })
            
        self.lasers = []
        self.missiles = []
        self.shockwaves = []
        self.nova_events = []
        self.enemies = []
        self.particles = []
        
        self.enemy_spawn_timer = 0
        self.shoot_cooldown = 0
        self.missile_cooldown = 0
        self.touch_steering_enabled = True

        # ── BCI Cognitive Buff State ──────────────────────────────────────────
        # focus_shield_on / focus_shield_off commands from main.py
        self.shield_active = False        # High Focus → halves incoming damage
        self.spread_buff_active = False   # Laser Spread Buff → fires 3-way multi-lasers on primary attack
        # ─────────────────────────────────────────────────────────────────────

    def reset_game(self):
        self.player_x = WINDOW_WIDTH // 2
        self.player_y = 520
        self.player_target_x = WINDOW_WIDTH // 2
        self.player_target_y = 520
        self.score = 0
        self.health = 100
        self.game_over = False
        self.damage_flash_counter = 0
        self.lasers.clear()
        self.missiles.clear()
        self.shockwaves.clear()
        self.enemies.clear()
        self.particles.clear()
        self.nova_events.clear()
        self.shield_active = False       # clear buff state on game reset
        self.spread_buff_active = False  # clear spread buff state

    def handle_inputs(self):
        global WINDOW_WIDTH, WINDOW_HEIGHT
        
        # 1. Update GameInputManager states (polls keyboard/mouse and BCI queue)
        self.input_mgr.update()

        # 1b. Handle BCI cognitive buff and action commands
        for cmd in list(self.input_mgr.cmds):
            if cmd == "focus_shield_on":
                self.shield_active = True
                print("🧠 FOCUS SHIELD ACTIVATED — damage halved")
            elif cmd == "focus_shield_off":
                self.shield_active = False
                print("🧠 Focus shield deactivated")
            elif cmd == "spread_buff_on":
                self.spread_buff_active = True
                print("🔥 SPREAD CANNON BUFF ACTIVATED — triple multi-laser unlocked")
            elif cmd == "spread_buff_off":
                self.spread_buff_active = False
                print("🔥 Spread cannon buff deactivated")
            elif cmd in ("toggle_spread_buff", "spread_buff", "spread"):
                self.spread_buff_active = not self.spread_buff_active
                print(f"🔥 Spread Cannon Buff: {'ACTIVATED' if self.spread_buff_active else 'DEACTIVATED'}")
            elif cmd in ("fullscreen", "toggle_fullscreen"):
                self.toggle_fullscreen()
            elif cmd in ("missile", "calm_missile", "voice_missile"):
                self.fire_missile()
            elif cmd in ("supernova", "nova", "voice_fire", "fire", "tert_action"):
                self.trigger_supernova()

        # 2. Poll window events (quit, resize, key toggles)
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
                elif event.key == K_F11 or (event.key == K_RETURN and (event.mod & KMOD_ALT)):
                    self.toggle_fullscreen()
                elif event.key == K_t:
                    self.touch_steering_enabled = not self.touch_steering_enabled
                    print(f"🎮 Touch Screen X/Y Steering Mode: {'ENABLED' if self.touch_steering_enabled else 'DISABLED (Tilt Only)'}")
                elif event.key in (K_m, K_b):
                    self.fire_missile()
                elif event.key in (K_f, K_n):
                    self.trigger_supernova()
                elif event.key in (K_s, K_x):
                    self.spread_buff_active = not self.spread_buff_active
                    print(f"🔥 Spread Cannon Buff: {'ACTIVATED' if self.spread_buff_active else 'DEACTIVATED'}")
            elif event.type == VIDEORESIZE:
                WINDOW_WIDTH, WINDOW_HEIGHT = event.w, event.h
                self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), DOUBLEBUF | OPENGL | RESIZABLE)
                glViewport(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
                glMatrixMode(GL_PROJECTION)
                glLoadIdentity()
                glOrtho(0, WINDOW_WIDTH, WINDOW_HEIGHT, 0, -1, 1)
                glMatrixMode(GL_MODELVIEW)
                glLoadIdentity()

        # 3. Handle Actions (Steer, Fire, Spread, Bomb/Missile) using unified input manager
        if self.input_mgr.is_left():
            self.player_target_x = max(30, self.player_target_x - 12.0)
        if self.input_mgr.is_right():
            self.player_target_x = min(WINDOW_WIDTH - 30, self.player_target_x + 12.0)
        if self.input_mgr.is_up():
            self.player_target_y = max(40, self.player_target_y - 12.0)
        if self.input_mgr.is_down():
            self.player_target_y = min(WINDOW_HEIGHT - 40, self.player_target_y + 12.0)
            
        if self.input_mgr.is_action():
            if self.game_over:
                self.reset_game()
            else:
                self.fire_laser()
                
        if self.input_mgr.is_sec_action():
            if not self.game_over:
                # Secondary action toggles the persistent Spread Cannon Buff!
                self.spread_buff_active = not self.spread_buff_active
                        
        if self.input_mgr.is_tert_action():
            if not self.game_over:
                # Supernova Cleansing Blast!
                self.trigger_supernova()

        # 4. Continuous tilt steer or direct Touch Screen X/Y steering if connected standalone
        if self.input_mgr.imu_connected and not self.input_mgr.dashboard_controlled:
            if self.touch_steering_enabled and self.input_mgr.latest_touch_active:
                tx = self.input_mgr.latest_touch_x
                ty = self.input_mgr.latest_touch_y
                if tx >= 0.0 and ty >= 0.0:
                    target_x = (tx / 360.0) * WINDOW_WIDTH
                    target_y = (ty / 360.0) * WINDOW_HEIGHT
                    self.player_target_x = max(30, min(WINDOW_WIDTH - 30, target_x))
                    self.player_target_y = max(40, min(WINDOW_HEIGHT - 40, target_y))
            else:
                roll = self.input_mgr.latest_roll
                if abs(roll) > 12.0:
                    vel = np.clip(roll * 0.35, -9.0, 9.0)
                    self.player_target_x = max(30, min(WINDOW_WIDTH - 30, self.player_target_x + vel))

    def fire_laser(self):
        if self.shoot_cooldown <= 0:
            if self.spread_buff_active:
                # Triple Laser Spread Buff Pattern!
                self.lasers.append({"x": self.player_x, "y": self.player_y - 20, "vx": 0.0, "vy": -10.0, "color": (255, 110, 230)})
                self.lasers.append({"x": self.player_x - 16, "y": self.player_y - 12, "vx": -2.8, "vy": -9.2, "color": (255, 110, 230)})
                self.lasers.append({"x": self.player_x + 16, "y": self.player_y - 12, "vx": 2.8, "vy": -9.2, "color": (255, 110, 230)})
                self.shoot_cooldown = 14
            else:
                # Standard Single Forward Laser Cannon
                self.lasers.append({"x": self.player_x, "y": self.player_y - 20, "vx": 0.0, "vy": -9.5, "color": COLOR_NEON_GREEN})
                self.shoot_cooldown = 12
            if self.snd_laser:
                self.snd_laser.play()

    def fire_missile(self):
        """Launches visible guided homing missiles with particle exhaust trails."""
        if self.missile_cooldown <= 0:
            self.missile_cooldown = 24
            # Launch twin homing missiles from left and right wing hardpoints
            self.missiles.append({
                "x": self.player_x - 20,
                "y": self.player_y - 8,
                "vx": -3.2,
                "vy": -7.5,
                "speed": 11.5,
                "life": 120
            })
            self.missiles.append({
                "x": self.player_x + 20,
                "y": self.player_y - 8,
                "vx": 3.2,
                "vy": -7.5,
                "speed": 11.5,
                "life": 120
            })
            if self.snd_missile:
                self.snd_missile.play()
            self.damage_flash_counter = 4

    def trigger_supernova(self, origin_x=None, origin_y=None):
        """Triggers a brilliant full-screen chromatic Supernova blast cleansing all hostiles."""
        ox = self.player_x if origin_x is None else origin_x
        oy = (self.player_y - 20) if origin_y is None else origin_y

        # 1. Spawn Supernova entity
        self.nova_events.append({
            "x": ox,
            "y": oy,
            "radius": 12.0,
            "max_radius": 1100.0,
            "expand_speed": 26.0,
            "alpha": 1.0,
            "starburst_angles": [random.uniform(0, 2 * math.pi) for _ in range(48)],
            "banner_timer": 50
        })

        # 2. Spawn 70 sparkling prismatic particle shards
        for _ in range(70):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(3.5, 15.0)
            col = random.choice([
                (255, 255, 255),  # Pure White
                (255, 220, 30),   # Radiant Gold
                (0, 240, 255),    # Electric Cyan
                (255, 60, 220),   # Neon Magenta
                (180, 70, 255)    # Violet Core
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

        # 3. Cleanse all enemies on screen with bonus points
        if self.enemies:
            self.score += len(self.enemies) * 35
            for enemy in self.enemies:
                self.spawn_particles(enemy["x"], enemy["y"], (255, 215, 0), count=18, speed_max=8.0)
            self.enemies.clear()

        # 4. Audio synthesis & screen border flash
        if self.snd_nova:
            self.snd_nova.play()
        self.nova_flash_counter = 18
        self.damage_flash_counter = 6

    def spawn_enemy(self):
        self.enemy_spawn_timer += 1
        spawn_rate = max(24, 90 - int(self.score / 15))
        if self.enemy_spawn_timer >= spawn_rate:
            self.enemy_spawn_timer = 0
            self.enemies.append({
                "x": random.randint(50, WINDOW_WIDTH - 50),
                "y": -20,
                "speed": random.uniform(2.0, 4.5 + min(3.0, self.score / 200)),
                "w": 30,
                "h": 20
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

    def update(self):
        if self.game_over:
            return

        self.spawn_enemy()
        
        if self.shoot_cooldown > 0:
            self.shoot_cooldown -= 1
        if self.missile_cooldown > 0:
            self.missile_cooldown -= 1

        # Smooth player interpolation
        self.player_x += (self.player_target_x - self.player_x) * 0.16
        self.player_y += (self.player_target_y - self.player_y) * 0.16

        # Update Stars
        for star in self.stars:
            star["y"] += star["speed"]
            if star["y"] > WINDOW_HEIGHT:
                star["y"] = 0
                star["x"] = random.randint(0, WINDOW_WIDTH)

        # Update Guided Missiles
        for m in self.missiles[:]:
            m["life"] -= 1
            # 1. Homing physics: steer toward nearest enemy on screen
            if self.enemies:
                nearest = min(self.enemies, key=lambda e: (e["x"] - m["x"])**2 + (e["y"] - m["y"])**2)
                dx = nearest["x"] - m["x"]
                dy = nearest["y"] - m["y"]
                dist = math.hypot(dx, dy)
                if dist > 0:
                    target_vx = (dx / dist) * m["speed"]
                    target_vy = (dy / dist) * m["speed"]
                    m["vx"] += (target_vx - m["vx"]) * 0.20
                    m["vy"] += (target_vy - m["vy"]) * 0.20
            else:
                m["vy"] = -m["speed"]

            m["x"] += m["vx"]
            m["y"] += m["vy"]

            # 2. Spawn fiery rocket exhaust trail particles
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

            # 3. Check missile collision with enemies
            m_hit = False
            m_pos = (m["x"], m["y"])
            for enemy in self.enemies[:]:
                e_rect = pygame.Rect(enemy["x"] - enemy["w"]//2, enemy["y"] - enemy["h"]//2, enemy["w"], enemy["h"])
                if e_rect.collidepoint(m_pos) or math.hypot(enemy["x"] - m["x"], enemy["y"] - m["y"]) < 26:
                    self.enemies.remove(enemy)
                    self.spawn_particles(enemy["x"], enemy["y"], (252, 211, 77), count=25, speed_max=8.0)
                    self.score += 40
                    m_hit = True

            # 4. Trigger expanding shockwave on hit or detonation
            if m_hit or m["y"] < -20 or m["life"] <= 0:
                self.shockwaves.append({
                    "x": m["x"],
                    "y": m["y"],
                    "radius": 8.0,
                    "max_radius": 110.0,
                    "alpha": 1.0,
                    "color": (255, 215, 0)
                })
                if self.snd_explosion:
                    self.snd_explosion.play()
                self.damage_flash_counter = max(self.damage_flash_counter, 6)
                if m in self.missiles:
                    self.missiles.remove(m)

        # Update Expanding Shockwaves
        for sw in self.shockwaves[:]:
            sw["radius"] += 7.0
            sw["alpha"] -= 0.045
            # Shockwave wipes enemies in blast radius
            for enemy in self.enemies[:]:
                if math.hypot(enemy["x"] - sw["x"], enemy["y"] - sw["y"]) < sw["radius"]:
                    self.enemies.remove(enemy)
                    self.spawn_particles(enemy["x"], enemy["y"], (255, 220, 50), count=15)
                    self.score += 25
            if sw["alpha"] <= 0 or sw["radius"] >= sw["max_radius"]:
                self.shockwaves.remove(sw)

        # Update Supernova Events
        for nova in self.nova_events[:]:
            nova["radius"] += nova["expand_speed"]
            nova["alpha"] -= 0.024
            if "banner_timer" in nova and nova["banner_timer"] > 0:
                nova["banner_timer"] -= 1
            # Cleanse any enemies entering expanding wave
            for enemy in self.enemies[:]:
                if math.hypot(enemy["x"] - nova["x"], enemy["y"] - nova["y"]) < nova["radius"]:
                    self.enemies.remove(enemy)
                    self.spawn_particles(enemy["x"], enemy["y"], (255, 215, 0), count=15)
                    self.score += 25
            if nova["alpha"] <= 0 or nova["radius"] >= nova["max_radius"]:
                self.nova_events.remove(nova)

        if self.nova_flash_counter > 0:
            self.nova_flash_counter -= 1

        # Update Lasers
        for laser in self.lasers[:]:
            laser["x"] += laser.get("vx", 0.0)
            laser["y"] += laser.get("vy", -9.5)
            if laser["y"] < -20 or laser["x"] < -20 or laser["x"] > WINDOW_WIDTH + 20:
                self.lasers.remove(laser)

        # Update Enemies
        for enemy in self.enemies[:]:
            enemy["y"] += enemy["speed"]

            # Hit bottom bounds (leaked enemy)
            if enemy["y"] > WINDOW_HEIGHT:
                self.enemies.remove(enemy)
                dmg = 2 if self.shield_active else 5   # shield halves leak penalty
                self.health = max(0, self.health - dmg)
                self.damage_flash_counter = 12
                if self.health <= 0:
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
                    self.score += 20
                    break

            # Player collision check
            player_rect = pygame.Rect(self.player_x - 20, self.player_y - 15, 40, 30)
            if player_rect.colliderect(enemy_rect):
                self.enemies.remove(enemy)
                self.spawn_particles(enemy["x"], enemy["y"], COLOR_NEON_YELLOW)
                if self.snd_explosion:
                    self.snd_explosion.play()
                dmg = 10 if self.shield_active else 20  # shield halves collision damage
                self.health = max(0, self.health - dmg)
                self.damage_flash_counter = 15
                if self.health <= 0:
                    self.game_over = True

        # Update Particles
        for p in self.particles[:]:
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            p["alpha"] -= 0.025
            if p["alpha"] <= 0:
                self.particles.remove(p)

    def draw_player(self):
        # Draw player vector fighter jet in retro line style
        px, py = self.player_x, self.player_y
        
        # Glowing wings
        glLineWidth(2.5)
        glColor3f(*COLOR_NEON_BLUE)
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

        # Draw glowing engine flame
        if random.random() > 0.4:
            glColor3f(*COLOR_NEON_YELLOW)
            glBegin(GL_LINES)
            glVertex2f(px - 4, py + 10)
            glVertex2f(px, py + 22)
            glVertex2f(px + 4, py + 10)
            glVertex2f(px, py + 22)
            glEnd()

        # Focus Shield: draw a pulsing cyan hexagonal ring around the player
        if self.shield_active:
            alpha = 0.55 + 0.35 * math.sin(pygame.time.get_ticks() * 0.006)
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glColor4f(0.0, 0.85, 1.0, alpha)
            glLineWidth(2.0)
            r = 38  # shield ring radius
            n = 6   # hexagonal
            glBegin(GL_LINE_LOOP)
            for i in range(n):
                angle = 2.0 * math.pi * i / n - math.pi / 2
                glVertex2f(px + r * math.cos(angle), py + r * math.sin(angle))
            glEnd()
            glDisable(GL_BLEND)

        # Spread Cannon Buff: Wingtip auxiliary emitters & energy aura
        if self.spread_buff_active:
            pulse = 0.65 + 0.35 * math.sin(pygame.time.get_ticks() * 0.012)
            glColor3f(1.0 * pulse, 0.35 * pulse, 0.9 * pulse)
            glPointSize(6.0)
            glBegin(GL_POINTS)
            glVertex2f(px - 25, py + 10)
            glVertex2f(px + 25, py + 10)
            glEnd()
            # Emitter bracket lines
            glLineWidth(2.5)
            glBegin(GL_LINES)
            glVertex2f(px - 25, py + 10); glVertex2f(px - 25, py + 2)
            glVertex2f(px + 25, py + 10); glVertex2f(px + 25, py + 2)
            glEnd()

    def draw_enemy(self, enemy):
        ex, ey = enemy["x"], enemy["y"]
        w, h = enemy["w"], enemy["h"]
        
        glColor3f(*COLOR_NEON_PINK)
        glLineWidth(2.0)
        
        # Draw dynamic retro space invader mesh
        glBegin(GL_LINE_LOOP)
        glVertex2f(ex, ey - h//2)
        glVertex2f(ex - w//2, ey)
        glVertex2f(ex - w//3, ey + h//2)
        glVertex2f(ex + w//3, ey + h//2)
        glVertex2f(ex + w//2, ey)
        glEnd()
        
        # Center core indicator
        glPointSize(3.0)
        glBegin(GL_POINTS)
        glVertex2f(ex - 6, ey - 2)
        glVertex2f(ex + 6, ey - 2)
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
        """Draws a retro glowing vector rocket with nosecone, delta fins, and thruster."""
        mx, my = m["x"], m["y"]
        angle = math.atan2(m["vy"], m["vx"]) + math.pi / 2
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        def rot(dx, dy):
            return mx + (dx * cos_a - dy * sin_a), my + (dx * sin_a + dy * cos_a)

        # 1. Warhead Nosecone (Neon Gold)
        glColor3f(1.0, 0.84, 0.0)
        glLineWidth(2.5)
        glBegin(GL_LINE_LOOP)
        glVertex2f(*rot(0, -12))
        glVertex2f(*rot(-4, -2))
        glVertex2f(*rot(4, -2))
        glEnd()

        # 2. Missile Body (Cyan / White)
        glColor3f(0.2, 0.9, 1.0)
        glBegin(GL_LINES)
        glVertex2f(*rot(-4, -2)); glVertex2f(*rot(-4, 8))
        glVertex2f(*rot(4, -2)); glVertex2f(*rot(4, 8))
        glEnd()

        # 3. Delta Fins (Neon Orange)
        glColor3f(1.0, 0.5, 0.0)
        glBegin(GL_LINES)
        glVertex2f(*rot(-4, 4)); glVertex2f(*rot(-9, 10))
        glVertex2f(*rot(4, 4)); glVertex2f(*rot(9, 10))
        glEnd()

        # 4. Pulsing Thruster Core
        glColor3f(1.0, 0.9, 0.2)
        glPointSize(4.0)
        glBegin(GL_POINTS)
        glVertex2f(*rot(0, 10))
        glEnd()

    def draw_shockwave(self, s):
        """Draws an expanding neon shockwave blast ring."""
        sx, sy = s["x"], s["y"]
        r = s["radius"]
        alpha = max(0.0, s["alpha"])

        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glColor4f(1.0, 0.85, 0.1, alpha)
        glLineWidth(3.0)

        n = 24
        glBegin(GL_LINE_LOOP)
        for i in range(n):
            theta = 2.0 * math.pi * i / n
            glVertex2f(sx + r * math.cos(theta), sy + r * math.sin(theta))
        glEnd()
        glDisable(GL_BLEND)

    def draw_supernova(self, n):
        """Renders radiant multi-ring chromatic Supernova with radial starburst rays and banner."""
        nx, ny = n["x"], n["y"]
        r = n["radius"]
        alpha = max(0.0, min(1.0, n["alpha"]))

        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        # 1. Radiant Starburst Rays
        glLineWidth(2.0)
        glColor4f(1.0, 0.95, 0.5, alpha * 0.75)
        glBegin(GL_LINES)
        for angle in n.get("starburst_angles", []):
            ray_len = min(r * 1.35, 1200.0)
            glVertex2f(nx, ny)
            glVertex2f(nx + ray_len * math.cos(angle), ny + ray_len * math.sin(angle))
        glEnd()

        # 2. Concentric Chromatic Rings
        chroma_layers = [
            ((1.0, 1.0, 1.0), 4.5, 0.0),       # Core White Shockwave
            ((1.0, 0.85, 0.0), 3.5, -20.0),    # Radiant Gold
            ((0.0, 0.95, 1.0), 3.0, -45.0),    # Electric Cyan
            ((0.9, 0.2, 1.0), 2.5, -70.0)      # Cosmic Violet
        ]
        
        num_segments = 36
        for color, width, offset in chroma_layers:
            curr_r = r + offset
            if curr_r <= 0:
                continue
            glColor4f(color[0], color[1], color[2], alpha * 0.85)
            glLineWidth(width)
            glBegin(GL_LINE_LOOP)
            for i in range(num_segments):
                theta = 2.0 * math.pi * i / num_segments
                glVertex2f(nx + curr_r * math.cos(theta), ny + curr_r * math.sin(theta))
            glEnd()

        glDisable(GL_BLEND)

        # 3. Supernova Cleansing Banner Text
        if n.get("banner_timer", 0) > 0:
            self.draw_text("💥 🌟 SUPERNOVA CLEANSING! 🌟 💥", WINDOW_WIDTH // 2 - 195, 95, self.font_title, (255, 230, 0))

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
        # 1. Title bar
        self.draw_text("BCI ARCADE SHIELD: ", 20, 20, self.font, (148, 163, 184))
        
        # Health Bar Outline
        glColor3f(0.2, 0.2, 0.3)
        glBegin(GL_QUADS)
        glVertex2f(190, 22)
        glVertex2f(340, 22)
        glVertex2f(340, 36)
        glVertex2f(190, 36)
        glEnd()
        
        # Health Bar Fill
        if self.health > 0:
            h_pct = self.health / 100.0
            glColor3f(1.0 - h_pct, h_pct, 0.2)
            glBegin(GL_QUADS)
            glVertex2f(192, 24)
            glVertex2f(192 + int(144 * h_pct), 24)
            glVertex2f(192 + int(144 * h_pct), 34)
            glVertex2f(192, 34)
            glEnd()
            
        self.draw_text(f"SCORE: {self.score}", WINDOW_WIDTH - 150, 20, self.font, (22, 242, 179))
        
        # Missile Readiness HUD Badge
        if self.missile_cooldown <= 0:
            self.draw_text("🚀 MISSILES & NOVA: READY [VOICE 'FIRE' / M / F]", WINDOW_WIDTH - 440, 48, self.font, (250, 204, 21))
        else:
            self.draw_text("🚀 RELOADING...", WINDOW_WIDTH - 200, 48, self.font, (100, 116, 139))

        # 2. Connection HUD & Control Hotkeys
        bci_label = "CTRL MODE: "
        if self.input_mgr.dashboard_controlled:
            bci_label += "DASHBOARD REDIRECT (main.py)"
            b_color = (34, 197, 94) # Green
        else:
            nodes = []
            if self.input_mgr.eeg_connected: nodes.append("Muse EEG")
            if self.input_mgr.imu_connected:
                touch_mode_str = "Phone Touch X/Y" if self.touch_steering_enabled else "Phone IMU Tilt"
                nodes.append(touch_mode_str)
            bci_label += ", ".join(nodes) if nodes else "KEYBOARD CONTROL"
            b_color = (34, 197, 94) if nodes else (239, 68, 68)
            
        hud_bottom_y = WINDOW_HEIGHT - 35
        self.draw_text(bci_label, 20, hud_bottom_y, self.font, b_color)
        
        hints = "[F11]: Fullscreen  |  [T]: Touch/Tilt  |  [X]: Spread  |  [F]: Nova"
        self.draw_text(hints, WINDOW_WIDTH - 620, hud_bottom_y, self.font, (148, 163, 184))

        # 3. BCI Cognitive Buff Status
        buff_y = 55
        if self.shield_active:
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glColor4f(0.0, 0.8, 1.0, 0.12)
            glBegin(GL_QUADS)
            glVertex2f(14, buff_y - 2)
            glVertex2f(260, buff_y - 2)
            glVertex2f(260, buff_y + 18)
            glVertex2f(14, buff_y + 18)
            glEnd()
            glDisable(GL_BLEND)
            self.draw_text("🧠 FOCUS SHIELD ACTIVE  [-50% DMG]", 18, buff_y, self.font, (0, 220, 255))
            buff_y += 22

        if self.spread_buff_active:
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glColor4f(1.0, 0.35, 0.9, 0.12)
            glBegin(GL_QUADS)
            glVertex2f(14, buff_y - 2)
            glVertex2f(275, buff_y - 2)
            glVertex2f(275, buff_y + 18)
            glVertex2f(14, buff_y + 18)
            glEnd()
            glDisable(GL_BLEND)
            self.draw_text("🔥 SPREAD CANNON ACTIVE [3-WAY]", 18, buff_y, self.font, (255, 110, 230))
            buff_y += 22

    def draw(self):
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        
        # Draw Starfield
        glBegin(GL_POINTS)
        for star in self.stars:
            glColor4f(1.0, 1.0, 1.0, star["brightness"])
            glVertex2f(star["x"], star["y"])
        glEnd()
        
        # Draw Particles
        for p in self.particles:
            self.draw_particle(p)
            
        # Draw Lasers
        for laser in self.lasers:
            self.draw_laser(laser)

        # Draw Guided Missiles
        for missile in self.missiles:
            self.draw_missile(missile)

        # Draw Expanding Shockwaves
        for shockwave in self.shockwaves:
            self.draw_shockwave(shockwave)

        # Draw Supernova Events
        for nova in self.nova_events:
            self.draw_supernova(nova)
            
        # Draw Enemies
        for enemy in self.enemies:
            self.draw_enemy(enemy)

        # Draw Player
        if not self.game_over:
            self.draw_player()
        else:
            self.draw_text("GAME OVER", WINDOW_WIDTH//2 - 70, WINDOW_HEIGHT//2 - 40, self.font_title, (239, 68, 68))
            self.draw_text("Tilt / Blink / Space to Restart", WINDOW_WIDTH//2 - 165, WINDOW_HEIGHT//2 + 5, self.font, (148, 163, 184))

        self.draw_hud()

        # Render Supernova golden boundary flash
        if self.nova_flash_counter > 0:
            glLineWidth(8.0)
            glColor4f(1.0, 0.85, 0.0, 0.7)
            glBegin(GL_LINE_LOOP)
            glVertex2f(0, 0)
            glVertex2f(WINDOW_WIDTH, 0)
            glVertex2f(WINDOW_WIDTH, WINDOW_HEIGHT)
            glVertex2f(0, WINDOW_HEIGHT)
            glEnd()

        # Render damage flash boundary overlay
        if self.damage_flash_counter > 0:
            glLineWidth(6.0)
            glColor4f(1.0, 0.1, 0.1, 0.5)
            glBegin(GL_LINE_LOOP)
            glVertex2f(0, 0)
            glVertex2f(WINDOW_WIDTH, 0)
            glVertex2f(WINDOW_WIDTH, WINDOW_HEIGHT)
            glVertex2f(0, WINDOW_HEIGHT)
            glEnd()
            self.draw_text("⚠️ SHIELD DAMAGE! ⚠️", WINDOW_WIDTH // 2 - 90, 80, self.font, (239, 68, 68))

    def run(self):
        while True:
            self.handle_inputs()
            self.update()
            self.draw()
            
            pygame.display.flip()
            self.clock.tick(FPS)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retro Fighter Jet BCI Arcade Game")
    parser.add_argument("--fullscreen", "-f", action="store_true", help="Launch in fullscreen mode")
    args = parser.parse_args()
    game = RetroPlaneGame(fullscreen=args.fullscreen)
    game.run()
