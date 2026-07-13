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

# ==========================================
# RETRO PLANE ARCADE GAME
# ==========================================
class RetroPlaneGame:
    def __init__(self):
        pygame.init()
        try:
            pygame.mixer.init()
        except pygame.error as e:
            print(f"Warning: Audio device not found ({e}). Running without audio.")
        
        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), DOUBLEBUF | OPENGL | RESIZABLE)
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
        try:
            self.snd_laser = synthesize_laser_sound()
            self.snd_explosion = synthesize_explosion_sound()
        except Exception as e:
            print(f"Audio Synthesis Warning: {e}. Running mute.")
            self.snd_laser = None
            self.snd_explosion = None

        # Game state variables
        self.player_x = WINDOW_WIDTH // 2
        self.player_y = 520
        self.player_target_x = WINDOW_WIDTH // 2
        self.player_target_y = 520
        self.score = 0
        self.health = 100
        self.game_over = False
        self.damage_flash_counter = 0
        
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
        self.enemies = []
        self.particles = []
        
        self.enemy_spawn_timer = 0
        self.shoot_cooldown = 0
        
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
        self.enemies.clear()
        self.particles.clear()

    def handle_inputs(self):
        global WINDOW_WIDTH, WINDOW_HEIGHT
        
        # 1. Update GameInputManager states (polls keyboard/mouse and BCI queue)
        self.input_mgr.update()
        
        # 2. Poll window events (quit, resize)
        for event in pygame.event.get():
            if event.type == QUIT:
                self.input_mgr.running = False
                pygame.quit()
                sys.exit()
            elif event.type == KEYDOWN:
                if event.key == K_ESCAPE:
                    self.input_mgr.running = False
                    pygame.quit()
                    sys.exit()
            elif event.type == VIDEORESIZE:
                WINDOW_WIDTH, WINDOW_HEIGHT = event.w, event.h
                self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), DOUBLEBUF | OPENGL | RESIZABLE)
                glViewport(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
                glMatrixMode(GL_PROJECTION)
                glLoadIdentity()
                glOrtho(0, WINDOW_WIDTH, WINDOW_HEIGHT, 0, -1, 1)
                glMatrixMode(GL_MODELVIEW)
                glLoadIdentity()

        # 3. Handle Actions (Steer, Fire, Spread, Bomb) using unified input manager
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
                # Double laser spread!
                if self.shoot_cooldown <= 0:
                    self.lasers.append({"x": self.player_x - 15, "y": self.player_y - 20})
                    self.lasers.append({"x": self.player_x + 15, "y": self.player_y - 20})
                    self.shoot_cooldown = 15
                    if self.snd_laser:
                        self.snd_laser.play()
                        
        if self.input_mgr.is_tert_action():
            if not self.game_over:
                # Smart Bomb! Wipes all enemies!
                if self.enemies:
                    self.score += len(self.enemies) * 10
                    for enemy in self.enemies:
                        self.spawn_particles(enemy["x"], enemy["y"], (252, 211, 77))
                    self.enemies.clear()
                    if self.snd_explosion:
                        self.snd_explosion.play()
                    self.damage_flash_counter = 8  # grid flash

        # 4. Continuous tilt steer if connected standalone
        if self.input_mgr.imu_connected and not self.input_mgr.dashboard_controlled:
            roll = self.input_mgr.latest_roll
            if abs(roll) > 12.0:
                vel = np.clip(roll * 0.35, -9.0, 9.0)
                self.player_target_x = max(30, min(WINDOW_WIDTH - 30, self.player_target_x + vel))

    def fire_laser(self):
        if self.shoot_cooldown <= 0:
            self.lasers.append({"x": self.player_x, "y": self.player_y - 20})
            self.shoot_cooldown = 12
            if self.snd_laser:
                self.snd_laser.play()

    def spawn_enemy(self):
        self.enemy_spawn_timer += 1
        # Slower initial spawn rate (1.5s), increasing difficulty gradually
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

    def spawn_particles(self, x, y, color):
        for _ in range(15):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(2.0, 6.0)
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

        # Smooth player interpolation
        self.player_x += (self.player_target_x - self.player_x) * 0.16
        self.player_y += (self.player_target_y - self.player_y) * 0.16

        # Update Stars
        for star in self.stars:
            star["y"] += star["speed"]
            if star["y"] > WINDOW_HEIGHT:
                star["y"] = 0
                star["x"] = random.randint(0, WINDOW_WIDTH)

        # Update Lasers
        for laser in self.lasers[:]:
            laser["y"] -= 9.5
            if laser["y"] < 0:
                self.lasers.remove(laser)

        # Update Enemies
        for enemy in self.enemies[:]:
            enemy["y"] += enemy["speed"]
            
            # Hit bottom bounds (leaked enemy)
            if enemy["y"] > WINDOW_HEIGHT:
                self.enemies.remove(enemy)
                self.health = max(0, self.health - 5)  # reduced penalty
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
                self.health = max(0, self.health - 20)  # reduced penalty
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
        glColor3f(*COLOR_NEON_GREEN)
        glLineWidth(3.0)
        glBegin(GL_LINES)
        glVertex2f(laser["x"], laser["y"])
        glVertex2f(laser["x"], laser["y"] - 15)
        glEnd()

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
        
        # 2. Connection HUD
        bci_label = "CTRL MODE: "
        if self.input_mgr.dashboard_controlled:
            bci_label += "DASHBOARD REDIRECT (main.py)"
            b_color = (34, 197, 94) # Green
        else:
            nodes = []
            if self.input_mgr.eeg_connected: nodes.append("Muse EEG")
            if self.input_mgr.imu_connected: nodes.append("Phone IMU")
            bci_label += ", ".join(nodes) if nodes else "KEYBOARD CONTROL"
            b_color = (34, 197, 94) if nodes else (239, 68, 68)
            
        self.draw_text(bci_label, 20, 560, self.font, b_color)

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
    game = RetroPlaneGame()
    game.run()
