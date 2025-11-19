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
# NEW: Path to the Pirata One font file
PIRATE_FONT_PATH = "Pirata_One/PirataOne-Regular.ttf"

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
            scale_factor = 0.80
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
        {"full": "Ships/ship (1).png", "half": "Ships/ship (13).png", "destroyed": "Ships/ship (19).png"}),
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
# Note: The draw_text_with_sheer_box function was removed as its logic 
#       was integrated directly into the main loop for precision.

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
# --- PYGAME SPRITE CLASSES ---
# ----------------------------------------------------------------------

class Cannon(pygame.sprite.Sprite):
    """Represents the player's fortress/cannon, now centered."""
    def __init__(self):
        super().__init__()
        # Load cannon image
        try:
            original_image = pygame.image.load(os.path.join(ASSET_PATH, "Ships/ship (2).png")).convert_alpha()
            # Use scale() for compatibility
            scale_factor = 1
            new_size = (int(original_image.get_width() * scale_factor), int(original_image.get_height() * scale_factor))
            self.image = pygame.transform.scale(original_image, new_size)
        except pygame.error:
            self.image = pygame.Surface((50, 30))
            self.image.fill(WHITE)
            
        self.rect = self.image.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))

    def draw_health_bar(self, screen):
        """
        Draws the Player's Fortress Health Bar at the top left.
        The HP text is now drawn below the bar.
        """
        MAX_WIDTH = 250
        BAR_HEIGHT = 20
        x, y = 10, 10 
        
        fill_ratio = max(0, PLAYER_FORTRESS['health'] / PLAYER_FORTRESS['max_health'])
        fill_width = MAX_WIDTH * fill_ratio
        
        # 1. Draw Health Bar
        border_rect = pygame.Rect(x, y, MAX_WIDTH, BAR_HEIGHT)
        pygame.draw.rect(screen, BLACK, border_rect, 2)
        
        color = GREEN
        if fill_ratio < 0.5: color = (255, 165, 0)
        if fill_ratio < 0.2: color = RED
            
        fill_rect = pygame.Rect(x, y, fill_width, BAR_HEIGHT)
        pygame.draw.rect(screen, color, fill_rect)

        # 2. Draw Text BELOW the bar
        try:
            bar_font = pygame.font.Font(PIRATE_FONT_PATH, 24)
        except:
            bar_font = pygame.font.Font(None, 24)

        text_content = f"FORTRESS HP: {PLAYER_FORTRESS['health']:.1f}"
        
        # Calculate Y position: Below the bar (y + BAR_HEIGHT + padding)
        text_y_start = y + BAR_HEIGHT + 5 
        
        text = bar_font.render(text_content, True, WHITE) 
        text_rect = text.get_rect(topleft=(x + 5, text_y_start))

        # Draw sheer background for the text
        sheer_text_surface = pygame.Surface(text_rect.size, pygame.SRCALPHA)
        # 100 is a slight transparency
        sheer_text_surface.fill((0, 0, 0, 100)) 
        screen.blit(sheer_text_surface, text_rect.topleft)
        screen.blit(text, text_rect.topleft)
        
        # --- REMOVED: Block that drew "FORTRESS LOST!" ---
        # if PLAYER_FORTRESS['health'] <= 0:
        #     # Draw LOST text below the HP text
        #     text_lost = bar_font.render("FORTRESS LOST!", True, RED)
        #     # Position below the first line of text
        #     screen.blit(text_lost, (x, text_y_start + text.get_height() + 5))
            
    def update(self):
        pass


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
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            
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
# --- PYGAME MAIN THREAD ---
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
    
    player_cannon = Cannon()
    cannon_sprites.add(player_cannon)
    
    hardware_thread = HardwareThread(event_queue)
    hardware_thread.start()

    # --- FONT INITIALIZATION (USING PIRATA ONE) ---
    # Attempt to load the pirate font, fall back to default if not found
    try:
        # Load the Pirata One font for different sizes
        font_score = pygame.font.Font(PIRATE_FONT_PATH, 48)
        font_large = pygame.font.Font(PIRATE_FONT_PATH, 96)
        font_medium = pygame.font.Font(PIRATE_FONT_PATH, 64)
        font_small = pygame.font.Font(PIRATE_FONT_PATH, 48)
        print(f"INFO: Successfully loaded '{PIRATE_FONT_PATH}'.")
    except Exception as e:
        print(f"WARNING: Could not load '{PIRATE_FONT_PATH}' ({e}). Falling back to default font.")
        font_score = pygame.font.Font(None, 36)
        font_large = pygame.font.Font(None, 74)
        font_medium = pygame.font.Font(None, 48)
        font_small = pygame.font.Font(None, 36)
    # ----------------------------------------------

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
                            cannonball = Effect(
                                player_cannon.rect.center, 
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
                            cannonball = Effect(
                                current_ship.rect.center, 
                                player_cannon.rect.center,
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
            player_cannon.draw_health_bar(screen)
            
            all_sprites.draw(screen)

            # --- TEXT DRAWING WITH SHEER BOXES ---
            CENTER_X = SCREEN_WIDTH // 2
            CENTER_Y = SCREEN_HEIGHT // 2
            BOX_PADDING = 30 # Overall padding for the big box
            
            if game_running and not game_over:
                # SCORING DISPLAY (TOP RIGHT) - FIXED CRASH LOCATION
                score_display_text = f"SCORE: {int(hardware_thread.score)}"
                
                # 1. Render text and get rect
                score_surface = font_score.render(score_display_text, True, WHITE)
                # Position the score text's top-right corner near (SCREEN_WIDTH - 10, 10)
                score_rect = score_surface.get_rect(topright=(SCREEN_WIDTH - 10, 10))
                
                # 2. Define the background box (centered around the text rect)
                TEXT_BOX_PADDING = 10 
                box_rect = score_rect.inflate(TEXT_BOX_PADDING * 2, TEXT_BOX_PADDING * 2)
                
                # 3. Draw the sheer box
                sheer_surface = pygame.Surface(box_rect.size, pygame.SRCALPHA)
                sheer_surface.fill((0, 0, 0, 150)) # Black with 150 alpha
                screen.blit(sheer_surface, box_rect.topleft)

                # 4. Draw the text
                screen.blit(score_surface, score_rect)

            elif game_over:
                # GAME OVER SCREEN (SINGLE CENTRAL SHEER BOX)
                # 1. Determine messages and colors
                if PLAYER_FORTRESS['health'] <= 0:
                    message = "DEFEAT! FORTRESS DESTROYED!"
                    color = RED
                elif get_current_target_ship() is None:
                    message = "VICTORY! ALL SHIPS SUNK!"
                    color = (255, 215, 0) # Gold
                else:
                    message = "TIME'S UP!"
                    color = WHITE
                
                score_text = f"FINAL SCORE: {last_game_score}"
                prompt_text = "PRESS ANY BUTTON TWICE TO CONTINUE"

                # 2. Render surfaces to get dimensions
                text_surface_large = font_large.render(message, True, color)
                text_surface_medium = font_medium.render(score_text, True, WHITE)
                text_surface_small = font_small.render(prompt_text, True, WHITE)

                # 3. Calculate Bounding Box dimensions (based on desired center points)
                # Desired center Y positions are: CenterY - 80, CenterY + 10, CenterY + 90
                top_line_top = (CENTER_Y - 80) - (text_surface_large.get_height() / 2)
                bottom_line_bottom = (CENTER_Y + 90) + (text_surface_small.get_height() / 2)
                
                box_top_y = int(top_line_top - BOX_PADDING)
                box_height = int(bottom_line_bottom - top_line_top + 2 * BOX_PADDING)

                # Calculate max width for X dimension
                max_width = max(
                    text_surface_large.get_width(),
                    text_surface_medium.get_width(),
                    text_surface_small.get_width()
                )
                box_width = max_width + (2 * BOX_PADDING)
                box_left_x = CENTER_X - (box_width // 2)

                # 4. Draw the single sheer box
                sheer_box_rect = pygame.Rect(box_left_x, box_top_y, box_width, box_height)
                sheer_surface = pygame.Surface(sheer_box_rect.size, pygame.SRCALPHA)
                sheer_surface.fill((0, 0, 0, 150)) # Black with 150 alpha
                screen.blit(sheer_surface, sheer_box_rect.topleft)

                # 5. Draw the text lines centered inside the box
                
                # Line 1: message
                text_rect_large = text_surface_large.get_rect(center=(CENTER_X, CENTER_Y - 80))
                screen.blit(text_surface_large, text_rect_large)
                
                # Line 2: score_text
                text_rect_medium = text_surface_medium.get_rect(center=(CENTER_X, CENTER_Y + 10))
                screen.blit(text_surface_medium, text_rect_medium)

                # Line 3: prompt_text
                text_rect_small = text_surface_small.get_rect(center=(CENTER_X, CENTER_Y + 90))
                screen.blit(text_surface_small, text_rect_small)
                
            elif not game_running:
                # START SCREEN (SINGLE CENTRAL SHEER BOX)
                text = "PRESS '5' TO START BATTLE"
                
                # 1. Render text and get rect
                text_surface = font_medium.render(text, True, WHITE)
                text_rect = text_surface.get_rect(center=(CENTER_X, CENTER_Y))
                
                # 2. Define the background box
                TEXT_BOX_PADDING = 15
                box_rect = text_rect.inflate(TEXT_BOX_PADDING * 2, TEXT_BOX_PADDING * 2)
                
                # 3. Draw the sheer box
                sheer_surface = pygame.Surface(box_rect.size, pygame.SRCALPHA)
                sheer_surface.fill((0, 0, 0, 150)) # Black with 150 alpha
                screen.blit(sheer_surface, box_rect.topleft)

                # 4. Draw the text
                screen.blit(text_surface, text_rect)

            # ---------------------------------------------
                
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