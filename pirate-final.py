import pygame
import os
import sys
import time
import random
import threading
import queue
import fcntl
import math 

# --- HARDWARE LIBRARIES (Must be installed on Raspberry Pi OS) ---
try:
    import evdev
    from plasma import auto
    from evdev import InputDevice, ecodes
except ImportError:
    print("WARNING: Hardware libraries (evdev, plasma) not found.")
    print("         The game will run but the HardwareThread will fail to initialize.")
    # Define placeholder classes/functions for testing on non-Pi systems
    class InputDevice:
        def __init__(self, path): raise FileNotFoundError
    class auto:
        def __init__(self, **kwargs): pass
        def set_all(self, r, g, b, brightness=None): pass
        def set_pixel(self, i, r, g, b, brightness=None): pass
        def show(self): pass
    class ecodes:
        KEY_1 = 1
        KEY_2 = 2
        KEY_3 = 3
        KEY_4 = 4
        KEY_5 = 5
        KEY_6 = 6
        KEY_7 = 7
        KEY_8 = 8
        KEY_9 = 9
        EV_KEY = 1
        
# --- CONFIGURATION & CONSTANTS ---
# These are initial values, they will be updated by main() for fullscreen resolution
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600 

ASSET_PATH = "kenney_pirate-pack (1)/PNG/Retina"
# Recommending FPS 90 as a stable high rate for the Pi
FPS = 90
BLUE = (30, 144, 255) 
GREEN = (0, 200, 0)
RED = (200, 0, 0)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
MOLE_COLOR = (0, 255, 0)

# Game/Positioning Constants (Will be recalculated in main())
MIN_SHIP_DISTANCE = min(SCREEN_WIDTH, SCREEN_HEIGHT) // 3 
SHIP_SPAWN_PADDING = 80 

# Hardware/Game Constants (from mole6_writer.py)
NUM_LIGHTS = 9
PIXELS_PER_BUTTON = 4
NUM_PIXELS = NUM_LIGHTS * PIXELS_PER_BUTTON
GAME_DURATION = 30.0
MOLE_DURATION = 1.0
PENALTY_FLASH_DURATION = 0.2
COUNTDOWN_FLASH_DURATION = 0.5

KEY_TO_LIGHT_INDEX = {
    ecodes.KEY_1: 0, ecodes.KEY_2: 1, ecodes.KEY_3: 2, ecodes.KEY_4: 3,
    ecodes.KEY_5: 4, ecodes.KEY_6: 5, ecodes.KEY_7: 6, ecodes.KEY_8: 7,
    ecodes.KEY_9: 8,
}
device_path = '/dev/input/event0'

# --- GLOBAL THREAD-SAFE QUEUE ---
event_queue = queue.Queue()


# ----------------------------------------------------------------------
# --- BATTLE LOGIC (The State Machine for Pygame) ---
# ----------------------------------------------------------------------

class EnemyShip(pygame.sprite.Sprite):
    """Represents a single enemy ship with its health and visual properties."""
    def __init__(self, name, max_health, sprite_paths):
        super().__init__()
        self.name = name
        self.max_health = max_health
        self.current_health = max_health
        self.is_destroyed = False
        
        self.battle_pos = None 
        
        # Pygame Image Handling (LOADED ONCE IN MAIN THREAD)
        self.images = {
            "full": self._load_and_scale(sprite_paths["full"]),
            "half": self._load_and_scale(sprite_paths["half"]),
            "destroyed": self._load_and_scale(sprite_paths["destroyed"]),
        }
        
        self.image = self.images["full"]
        self.rect = self.image.get_rect()

    def _load_and_scale(self, sprite_path):
        """Loads and scales a single image for compatibility."""
        try:
            original_image = pygame.image.load(os.path.join(ASSET_PATH, sprite_path)).convert_alpha()
            # Use scale() for compatibility
            scale_factor = 0.5
            new_size = (int(original_image.get_width() * scale_factor), int(original_image.get_height() * scale_factor))
            return pygame.transform.scale(original_image, new_size) 
        except pygame.error as e:
            print(f"Error loading image {sprite_path}: {e}")
            img = pygame.Surface((100, 100)) 
            img.fill((100, 100, 100))
            return img

    def get_current_sprite(self):
        """Returns the appropriate image based on current health status."""
        if self.is_destroyed:
            return self.images["destroyed"]
        
        health_ratio = self.current_health / self.max_health
        
        if health_ratio <= 0.5:
            return self.images["half"]
        
        return self.images["full"]

    def take_damage(self):
        """Reduces the ship's current health."""
        if not self.is_destroyed:
            self.current_health -= 1
            self.image = self.get_current_sprite() 
            
            if self.current_health <= 0:
                self.is_destroyed = True
                print(f"BATTLE: The {self.name} has been sunk!")
                return "SHIP_DESTROYED"
            else:
                print(f"BATTLE: Hit! The {self.name} has {self.current_health} HP remaining.")
                return "SHIP_HIT"
        return None


# Fleet and Player Initialization (Global state for the Pygame loop)
SHIP_DATA = [
    # (Name, Max_HP, {"full": Path, "half": Path, "destroyed": Path})
    ("Sloop", 5, 
        {"full": "Ships/ship (5).png", "half": "Ships/ship (17).png", "destroyed": "Ships/ship (23).png"}),
    ("Brigantine", 10, 
        {"full": "Ships/ship (4).png", "half": "Ships/ship (16).png", "destroyed": "Ships/ship (22).png"}),
    ("Frigate", 15, 
        {"full": "Ships/ship (3).png", "half": "Ships/ship (15).png", "destroyed": "Ships/ship (23).png"}),
    ("Man-of-War", 15, 
        {"full": "Ships/ship (7).png", "half": "Ships/ship (13).png", "destroyed": "Ships/ship (19).png"}),
    ("Dreadnought (Boss)", 5, 
        {"full": "Ships/ship (6).png", "half": "Ships/ship (18).png", "destroyed": "Ships/ship (24).png"}),
]

# Create global variables to hold mutable game state for the Pygame thread
ENEMY_FLEET = []
PLAYER_FORTRESS = {'health': 10, 'max_health': 10}

def initialize_fleet_structure():
    """
    **ONLY CALL IN MAIN THREAD.** Creates the ship objects, loads images, 
    and assigns fixed, non-overlapping positions.
    """
    global ENEMY_FLEET, PLAYER_FORTRESS, MIN_SHIP_DISTANCE
    
    MIN_SHIP_DISTANCE = min(SCREEN_WIDTH, SCREEN_HEIGHT) // 3
    
    ENEMY_FLEET = [
        EnemyShip(name, health, paths) for name, health, paths in SHIP_DATA
    ]
    
    placed_positions = []
    for ship in ENEMY_FLEET:
        ship.battle_pos = generate_non_overlapping_position(
            ship.image.get_size(), 
            MIN_SHIP_DISTANCE, 
            placed_positions, 
            SHIP_SPAWN_PADDING
        )
        placed_positions.append(ship.battle_pos)

def reset_game_for_new_round():
    """
    **CALLED BY HARDWARETHREAD.** Safely resets the state of existing objects 
    without reloading any images.
    """
    global ENEMY_FLEET, PLAYER_FORTRESS
    
    if not ENEMY_FLEET:
        return

    for ship in ENEMY_FLEET:
        ship.current_health = ship.max_health
        ship.is_destroyed = False
        ship.image = ship.images["full"] 

    PLAYER_FORTRESS['health'] = PLAYER_FORTRESS['max_health']
    
def get_current_target_ship():
    """Returns the first ship in the fleet that is NOT yet destroyed."""
    for ship in ENEMY_FLEET:
        if not ship.is_destroyed:
            return ship
    return None

# --- GLOBAL HELPER FUNCTIONS ---

def draw_ship_health(screen, ship):
    """Draws the health bar for the current target ship."""
    BAR_WIDTH = ship.image.get_width()
    BAR_HEIGHT = 10
    
    x = ship.rect.left
    y = ship.rect.top - BAR_HEIGHT - 5 
    
    fill = (ship.current_health / ship.max_health) * BAR_WIDTH
    
    # Draw background (empty health)
    background_rect = pygame.Rect(x, y, BAR_WIDTH, BAR_HEIGHT)
    pygame.draw.rect(screen, RED, background_rect) 
    
    # Draw green health fill
    fill_rect = pygame.Rect(x, y, fill, BAR_HEIGHT)
    pygame.draw.rect(screen, GREEN, fill_rect) 
    
    # Draw black border
    pygame.draw.rect(screen, BLACK, background_rect, 1)

def generate_non_overlapping_position(ship_size, min_distance, existing_positions, padding):
    """
    Generates a random (x, y) coordinate that is far from the center and 
    does not overlap with existing_positions.
    """
    CENTER_X = SCREEN_WIDTH // 2
    CENTER_Y = SCREEN_HEIGHT // 2
    
    MAX_DISTANCE = min(CENTER_X, CENTER_Y) - 50 
    
    i = 0
    while i < 1000: 
        angle = random.uniform(0, 2 * math.pi)
        distance = random.uniform(min_distance, MAX_DISTANCE)

        x = CENTER_X + distance * math.cos(angle)
        y = CENTER_Y + distance * math.sin(angle)
        
        new_pos = pygame.Vector2(x, y)
        is_overlapping = False

        for existing_pos in existing_positions:
            dist_to_other = new_pos.distance_to(pygame.Vector2(existing_pos))
            if dist_to_other < (ship_size[0] + ship_size[1]) / 2 + padding:
                is_overlapping = True
                break
        
        if not is_overlapping and 50 < x < SCREEN_WIDTH - 50 and 50 < y < SCREEN_HEIGHT - 50:
            return (int(x), int(y))

        i += 1
    
    return (SCREEN_WIDTH - 100, 100) 


# ----------------------------------------------------------------------
# --- PYGAME SPRITE CLASSES (UPDATED) ---
# ----------------------------------------------------------------------

# --- START: PlayerShip REPLACES Cannon ---
class PlayerShip(pygame.sprite.Sprite):
    """
    Represents the player's ship, now using HP and three visual states, 
    similar to EnemyShip. It replaces the old Cannon class.
    """
    def __init__(self):
        super().__init__()
        
        # Initialize internal state using global health settings
        self.max_health = PLAYER_FORTRESS['max_health']
        self.current_health = self.max_health
        self.is_destroyed = False
        
        # Define the sprite paths for the player ship
        sprite_paths = {
            "full": "Ships/ship (2).png", # <--- NEW SPRITE PATH
            "half": "Ships/ship (14).png", 
            "destroyed": "Ships/ship (20).png", 
        }

        # Pygame Image Handling (Copied from EnemyShip logic)
        self.images = {
            "full": self._load_and_scale(sprite_paths["full"]),
            "half": self._load_and_scale(sprite_paths["half"]),
            "destroyed": self._load_and_scale(sprite_paths["destroyed"]),
        }
        
        self.image = self.images["full"]
        # Center the player ship in the screen
        self.rect = self.image.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))

    def _load_and_scale(self, sprite_path):
        """Loads and scales a single image for compatibility."""
        try:
            original_image = pygame.image.load(os.path.join(ASSET_PATH, sprite_path)).convert_alpha()
            # Use scale() for compatibility - slightly larger for the player's base
            scale_factor = 2.0 
            new_size = (int(original_image.get_width() * scale_factor), int(original_image.get_height() * scale_factor))
            return pygame.transform.scale(original_image, new_size) 
        except pygame.error as e:
            print(f"Error loading image {sprite_path}: {e}")
            img = pygame.Surface((150, 150)) 
            img.fill(BLUE)
            return img

    def get_current_sprite(self):
        """Returns the appropriate image based on current health status."""
        if self.is_destroyed:
            return self.images["destroyed"]
        
        health_ratio = self.current_health / self.max_health
        
        if health_ratio <= 0.5:
            return self.images["half"]
        
        return self.images["full"]

    def draw_health_bar(self, screen):
        """Draws the Player's Ship Health Bar at the top left."""
        
        MAX_WIDTH = 250
        BAR_HEIGHT = 20
        x, y = 10, 10 
        
        # We now use the global state's health value
        current_health = PLAYER_FORTRESS['health']
        max_health = PLAYER_FORTRESS['max_health']
        
        fill_ratio = max(0, current_health / max_health)
        fill_width = MAX_WIDTH * fill_ratio
        
        border_rect = pygame.Rect(x, y, MAX_WIDTH, BAR_HEIGHT)
        pygame.draw.rect(screen, BLACK, border_rect, 2)
        
        color = GREEN
        if fill_ratio < 0.5: color = (255, 165, 0) # Orange
        if fill_ratio < 0.2: color = RED
            
        fill_rect = pygame.Rect(x, y, fill_width, BAR_HEIGHT)
        pygame.draw.rect(screen, color, fill_rect)

        font = pygame.font.Font(None, 24)
        text = font.render(f"PLAYER HP: {current_health:.1f}", True, WHITE) 
        screen.blit(text, (x + 5, y + 2))
        
        if current_health <= 0:
            text_lost = font.render("SHIP SUNK!", True, RED)
            screen.blit(text_lost, (x, y + BAR_HEIGHT + 5))
            
    def update(self):
        # Update the visual sprite based on the global health state
        self.current_health = PLAYER_FORTRESS['health']
        self.is_destroyed = (self.current_health <= 0)
        self.image = self.get_current_sprite()
# --- END: PlayerShip REPLACES Cannon ---


class Effect(pygame.sprite.Sprite):
    """
    Represents a cannonball in motion or a temporary explosion.
    This is now purely a visual effect.
    """
    def __init__(self, start_pos, end_pos, effect_type, duration=None):
        super().__init__()
        global all_sprites
        self.effect_type = effect_type
        self.start_pos = pygame.Vector2(start_pos)
        self.end_pos = pygame.Vector2(end_pos)
        self.position = self.start_pos
        self.speed = 10 
        self.distance = self.end_pos - self.start_pos
        self.total_distance = self.distance.length()
        self.progress = 0.0
        self.is_moving = True
        
        if effect_type in ["HIT", "MISS"]:
            self.load_image("Ship parts/cannonBall.png", scale=1.0)
            if self.total_distance > 0:
                self.direction = self.distance.normalize()
        elif effect_type == "EXPLOSION":
            self.load_image("Effects/explosion1.png", scale=1.0)
            self.lifetime = duration if duration else 15  
            self.is_moving = False
            self.rect = self.image.get_rect(center=self.end_pos)
            
    def load_image(self, path, scale):
        try:
            original_image = pygame.image.load(os.path.join(ASSET_PATH, path)).convert_alpha()
            new_width = int(original_image.get_width() * scale)
            new_height = int(original_image.get_height() * scale)
            self.image = pygame.transform.scale(original_image, (new_width, new_height))
        except pygame.error:
            self.image = pygame.Surface((20, 20))
            self.image.fill(BLACK)
            
        self.rect = self.image.get_rect(center=self.position)

    def update(self):
        if self.effect_type == "EXPLOSION":
            self.lifetime -= 1
            if self.lifetime <= 0:
                self.kill()
        elif self.is_moving:
            self.progress += self.speed
            if self.progress >= self.total_distance:
                if self.effect_type == "HIT":
                    # Generate explosion on landing
                    global all_sprites
                    all_sprites.add(Effect(self.end_pos, self.end_pos, "EXPLOSION"))
                
                # IMPORTANT: Damage/Healing logic has been removed from here.
                # It is now handled INSTANTLY in the main loop event processor.
                self.kill()
            else:
                t = self.progress / self.total_distance
                self.position = self.start_pos + self.distance * t
                self.rect.center = (int(self.position.x), int(self.position.y))


# ----------------------------------------------------------------------
# --- HARDWARE CONTROLLER THREAD ---
# ----------------------------------------------------------------------

class HardwareThread(threading.Thread):
    def __init__(self, event_queue):
        super().__init__()
        self.event_queue = event_queue
        self.running = True
        self.is_available = False
        
        # Hardware setup
        try:
            self.plasma = auto(default=f"GPIO:14:15:pixel_count={NUM_PIXELS}")
            self.plasma.set_all(0, 0, 0)
            self.plasma.show()
            self.dev = InputDevice(device_path)
            
            # Set to non-blocking mode
            fd = self.dev.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O.NONBLOCK)
            
            print(f"HARDWARE: Initialized and listening on {device_path}...")
            self.is_available = True
        except FileNotFoundError:
            print(f"HARDWARE: Error: Device not found at {device_path}. Running in simulation mode.")
        except Exception as e:
            print(f"HARDWARE: An error occurred during device setup: {e}. Running in simulation mode.")
        
        self.score = 0
        self.active_mole_light_index = None
        self.last_mole_time = 0
        self.game_start_time = 0
        
    def get_pixel_indices_for_light(self, light_index):
        start = light_index * PIXELS_PER_BUTTON
        end = start + PIXELS_PER_BUTTON
        return start, end

    def light_up_mole(self, light_index):
        if self.is_available:
            start_pixel, end_pixel = self.get_pixel_indices_for_light(light_index)
            for i in range(start_pixel, end_pixel):
                self.plasma.set_pixel(i, MOLE_COLOR[0], MOLE_COLOR[1], MOLE_COLOR[2], brightness=0.25)
            self.plasma.show()

    def turn_off_mole(self, light_index):
        if self.is_available and light_index is not None:
            start_pixel, end_pixel = self.get_pixel_indices_for_light(light_index)
            for i in range(start_pixel, end_pixel):
                self.plasma.set_pixel(i, 0, 0, 0, brightness=0.25)
            self.plasma.show()

    def light_up_all_red(self):
        if self.is_available:
            self.plasma.set_all(255, 0, 0, brightness=0.25)
            self.plasma.show()
            time.sleep(PENALTY_FLASH_DURATION)
            self.plasma.set_all(0, 0, 0, brightness=0.25)
            self.plasma.show()

    def countdown_sequence(self):
        if self.is_available:
            self.plasma.set_all(0, 0, 255, brightness=0.25) 
            self.plasma.show()
            time.sleep(COUNTDOWN_FLASH_DURATION * 2)
            self.plasma.set_all(0, 0, 0, brightness=0.25)
            time.sleep(COUNTDOWN_FLASH_DURATION)

            for i in range(3, 0, -1):
                self.light_up_mole(i - 1)
                time.sleep(COUNTDOWN_FLASH_DURATION)
                self.turn_off_mole(i - 1)
                time.sleep(COUNTDOWN_FLASH_DURATION)
        
        self.event_queue.put({"type": "COUNTDOWN_FINISHED"})

    def spawn_next_mole(self):
        """Helper to spawn the next mole immediately after a hit/miss."""
        self.turn_off_mole(self.active_mole_light_index)
        
        new_mole_index = random.randint(0, NUM_LIGHTS - 1)
        self.active_mole_light_index = new_mole_index
        self.light_up_mole(self.active_mole_light_index)
        self.last_mole_time = time.time()
        self.event_queue.put({"type": "MOLE_SPAWN", "index": new_mole_index})


    def run(self):
        while self.running:
            # Game Setup/Reset: Use the thread-safe reset function
            reset_game_for_new_round() 
            self.score = 0
            self.active_mole_light_index = None
            
            # --- START SCREEN ---
            self.event_queue.put({"type": "START_SCREEN"})
            
            self.turn_off_mole(self.active_mole_light_index)
            self.light_up_mole(KEY_TO_LIGHT_INDEX[ecodes.KEY_5])

            # Wait for '5' button press to start
            start_pressed = False
            while not start_pressed and self.running:
                try:
                    for event in self.dev.read():
                        if event.type == ecodes.EV_KEY and event.value == 1 and event.code == ecodes.KEY_5:
                            start_pressed = True
                            break
                except (IOError, BlockingIOError, AttributeError):
                    pass # Run tight loop, no sleep

            if not self.running: break

            self.countdown_sequence()

            self.game_start_time = time.time()
            
            # --- INNER GAME LOOP ---
            while self.running:
                current_time = time.time()
                time_elapsed = current_time - self.game_start_time
                
                # Check for Game End Condition (Time's Up OR Visual Layer Win/Loss)
                if time_elapsed >= GAME_DURATION or PLAYER_FORTRESS['health'] <= 0 or get_current_target_ship() is None:
                    break 

                # 1. Mole timer/spawning logic
                if self.active_mole_light_index is None or (current_time - self.last_mole_time) > MOLE_DURATION:
                    if self.active_mole_light_index is not None:
                        self.event_queue.put({"type": "MOLE_ESCAPED"})
                        
                    self.spawn_next_mole()

                # 2. Read input
                if self.is_available:
                    try:
                        for event in self.dev.read():
                            if event.type == ecodes.EV_KEY and event.value == 1:
                                if self.active_mole_light_index is not None and event.code in KEY_TO_LIGHT_INDEX:
                                    pressed_light_index = KEY_TO_LIGHT_INDEX[event.code]

                                    if pressed_light_index == self.active_mole_light_index:
                                        self.score += 1
                                        self.event_queue.put({"type": "PLAYER_HIT", "score": self.score})
                                        
                                        self.spawn_next_mole() 
                                        
                                    else:
                                        self.score = max(0, self.score - 0.5)
                                        self.light_up_all_red()
                                        self.event_queue.put({"type": "PLAYER_MISS", "score": self.score})
                                        
                                        self.spawn_next_mole() 

                    except (IOError, BlockingIOError, AttributeError):
                        pass
                
                # Removed time.sleep(0.001) to run tight loop for maximum speed 
                pass 

            # --- GAME OVER CLEANUP ---
            self.turn_off_mole(self.active_mole_light_index)
            if self.is_available:
                self.plasma.set_all(255, 255, 255, brightness=0.25)
                self.plasma.show()
                
            self.event_queue.put({"type": "GAME_OVER", "score": self.score})
            
            keys_needed = 2
            keys_pressed = 0
            
            while keys_pressed < keys_needed and self.running:
                try:
                    for event in self.dev.read():
                        if event.type == ecodes.EV_KEY and event.value == 1:
                            keys_pressed += 1
                            print(f"HARDWARE: Key press detected. {keys_pressed}/{keys_needed} to continue.")
                            break 
                except (IOError, BlockingIOError, AttributeError):
                    time.sleep(0.05) 

            if not self.running: break
            
            if self.is_available:
                self.plasma.set_all(0, 0, 0, brightness=0.25)
                self.plasma.show()

    def stop(self):
        self.running = False
        if self.is_available:
            self.plasma.set_all(0, 0, 0)
            self.plasma.show()
        print("HARDWARE: Thread gracefully stopped.")


# ----------------------------------------------------------------------
# --- PYGAME MAIN THREAD (UPDATED) ---
# ----------------------------------------------------------------------

def main():
    global all_sprites
    
    pygame.init()
    
    try:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        
        global SCREEN_WIDTH, SCREEN_HEIGHT 
        SCREEN_WIDTH = screen.get_width()
        SCREEN_HEIGHT = screen.get_height()
        
    except pygame.error:
        screen = pygame.display.set_mode((800, 600))
        
    global MIN_SHIP_DISTANCE
    MIN_SHIP_DISTANCE = min(SCREEN_WIDTH, SCREEN_HEIGHT) // 3
        
    pygame.display.set_caption("Whack-A-Pirate Battle")
    clock = pygame.time.Clock() 
    
    ocean_tile = None
    try:
        ocean_tile = pygame.image.load(os.path.join(ASSET_PATH, "Tiles/tile_73.png")).convert()
    except pygame.error:
        print("Error loading tile_73.png. Using solid blue color.")
    
    initialize_fleet_structure()
    
    game_running = False
    game_over = False
    last_game_score = 0
    
    all_sprites = pygame.sprite.Group()
    cannon_sprites = pygame.sprite.Group()
    
    # --- CHANGE 1: Instantiate PlayerShip instead of Cannon ---
    player_ship = PlayerShip()
    cannon_sprites.add(player_ship)
    
    hardware_thread = HardwareThread(event_queue)
    hardware_thread.start()
    
    running = True
    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                
            # 1. Process Hardware Events (Read Queue)
            while not event_queue.empty():
                try:
                    event = event_queue.get_nowait()
                    
                    if event['type'] == "START_SCREEN":
                        all_sprites.empty()
                        game_running = False
                        game_over = False
                        last_game_score = 0 
                        
                    elif event['type'] == "COUNTDOWN_FINISHED":
                        game_running = True
                        
                    elif event['type'] == "PLAYER_HIT":
                        current_ship = get_current_target_ship()
                        if current_ship:
                            # 1. MECHANICS: INSTANT DAMAGE AND HEAL
                            current_ship.take_damage() 
                            PLAYER_FORTRESS['health'] = min(
                                PLAYER_FORTRESS['health'] + 0.5, 
                                PLAYER_FORTRESS['max_health']
                            )
                            
                            # 2. VISUALS: CREATE CANNONBALL
                            # --- CHANGE 2: Use player_ship.rect.center for start pos ---
                            cannonball = Effect(
                                player_ship.rect.center, 
                                current_ship.rect.center,
                                "HIT"
                            )
                            all_sprites.add(cannonball)
                            
                    elif event['type'] == "PLAYER_MISS" or event['type'] == "MOLE_ESCAPED":
                        current_ship = get_current_target_ship()
                        if current_ship and PLAYER_FORTRESS['health'] > 0:
                            # 1. MECHANICS: ENEMY INSTANT DAMAGE
                            PLAYER_FORTRESS['health'] = max(0, PLAYER_FORTRESS['health'] - 1) 
                            
                            # 2. VISUALS: CREATE RETURN CANNONBALL
                            # --- CHANGE 3: Use player_ship.rect.center for end pos ---
                            cannonball = Effect(
                                current_ship.rect.center, 
                                player_ship.rect.center,
                                "MISS"
                            )
                            all_sprites.add(cannonball)

                    elif event['type'] == "GAME_OVER":
                        game_over = True
                        last_game_score = int(event.get('score', 0)) 

                except queue.Empty:
                    break
            
            # 2. Check for Win/Loss state change caused by object updates
            if game_running and (PLAYER_FORTRESS['health'] <= 0 or get_current_target_ship() is None):
                game_over = True
            
            # 3. Update Graphics
            all_sprites.update()
            cannon_sprites.update()
            
            # 4. Drawing
            
            if ocean_tile:
                tile_width = ocean_tile.get_width()
                tile_height = ocean_tile.get_height()
                for x in range(0, SCREEN_WIDTH, tile_width):
                    for y in range(0, SCREEN_HEIGHT, tile_height):
                        screen.blit(ocean_tile, (x, y))
            else:
                screen.fill(BLUE)

            for ship in ENEMY_FLEET:
                if ship.battle_pos is not None:
                    ship.rect.center = ship.battle_pos
                    ship.image = ship.get_current_sprite()
                    screen.blit(ship.image, ship.rect)
                    
                    if ship == get_current_target_ship() and not ship.is_destroyed:
                        draw_ship_health(screen, ship)

            cannon_sprites.draw(screen)
            # --- CHANGE 4: Call draw_health_bar on player_ship ---
            player_ship.draw_health_bar(screen)
            
            all_sprites.draw(screen)

            font_score = pygame.font.Font(None, 36)
            
            if game_running and not game_over:
                score_display_text = f"SCORE: {int(hardware_thread.score)}"
                score_surface = font_score.render(score_display_text, True, WHITE)
                score_rect = score_surface.get_rect(topright=(SCREEN_WIDTH - 10, 10))
                screen.blit(score_surface, score_rect)

            font_large = pygame.font.Font(None, 74)
            font_medium = pygame.font.Font(None, 48)
            font_small = pygame.font.Font(None, 36)
            
            if game_over:
                if PLAYER_FORTRESS['health'] <= 0:
                    message = "DEFEAT! SHIP SUNK!" # <--- UPDATED MESSAGE
                    color = RED
                elif get_current_target_ship() is None:
                    message = "VICTORY! ALL SHIPS SUNK!"
                    color = (255, 215, 0) 
                else:
                    message = "TIME'S UP!"
                    color = WHITE

                text = font_large.render(message, True, color)
                text_rect = text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 80))
                screen.blit(text, text_rect)
                
                score_text = font_medium.render(f"FINAL SCORE: {last_game_score}", True, WHITE)
                score_rect = score_text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 10))
                screen.blit(score_text, score_rect)

                prompt_text = font_small.render("PRESS ANY BUTTON TWICE TO CONTINUE", True, WHITE)
                prompt_rect = prompt_text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 90))
                screen.blit(prompt_text, prompt_rect)
                
            elif not game_running:
                text = font_small.render("PRESS '5' TO START BATTLE", True, WHITE)
                text_rect = text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))
                screen.blit(text, text_rect)
                
            # Cap drawing speed at FPS (90)
            pygame.display.flip()
            clock.tick(FPS)
    
    except KeyboardInterrupt:
        print("\nKeyboard interrupt detected. Initiating graceful shutdown...")
        running = False

    finally:
        print("Cleaning up threads and Pygame resources.")
        hardware_thread.stop()
        hardware_thread.join()
        pygame.quit()
        sys.exit()

if __name__ == "__main__":
    if not os.path.isdir(os.path.join(".", ASSET_PATH)):
         print(f"Error: Asset directory not found. Please ensure the folder '{ASSET_PATH}' is correct and relative to the script file.")
    else:
        main()