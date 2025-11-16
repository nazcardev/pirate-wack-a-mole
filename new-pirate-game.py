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
    # Define placeholder classes/functions for testing on non-Pi systems
    class InputDevice:
        def __init__(self, path): raise FileNotFoundError
    class auto:
        def __init__(self, **kwargs): pass
        def set_all(self, r, g, b, brightness=None): pass
        def set_pixel(self, i, r, g, b, brightness=None): pass
        def show(self): pass
    class ecodes:
        KEY_1 = 1; KEY_2 = 2; KEY_3 = 3; KEY_4 = 4; KEY_5 = 5
        KEY_6 = 6; KEY_7 = 7; KEY_8 = 8; KEY_9 = 9; EV_KEY = 1
        
# --- CONFIGURATION & CONSTANTS ---
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600 

ASSET_PATH = "kenney_pirate-pack (1)/PNG/Retina"
FPS = 90
BLUE = (30, 144, 255) 
GREEN = (0, 200, 0)
RED = (200, 0, 0)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
MOLE_COLOR = (0, 255, 0)

MIN_SHIP_DISTANCE = min(SCREEN_WIDTH, SCREEN_HEIGHT) // 3 
SHIP_SPAWN_PADDING = 80 

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

# --- GLOBAL THREAD-SAFE QUEUES ---
event_queue = queue.Queue() 

# Create global fleet and player variables
ENEMY_FLEET = []
PLAYER_FORTRESS = {'health': 10, 'max_health': 10}

SHIP_DATA = [
    ("Sloop", 5, 
        {"full": "Ships/ship (5).png", "half": "Ships/ship (17).png", "destroyed": "Ships/ship (21).png"}),
    ("Brigantine", 10, 
        {"full": "Ships/ship (4).png", "half": "Ships/ship (16).png", "destroyed": "Ships/ship (22).png"}),
    ("Frigate", 15, 
        {"full": "Ships/ship (9).png", "half": "Ships/ship (13).png", "destroyed": "Ships/ship (23).png"}),
    ("Man-of-War", 15, 
        {"full": "Ships/ship (8).png", "half": "Ships/ship (18).png", "destroyed": "Ships/ship (24).png"}),
    ("Dreadnought (Boss)", 5, 
        {"full": "Ships/ship (20).png", "half": "Ships/ship (19).png", "destroyed": "Ships/ship (19).png"}),
]

# ----------------------------------------------------------------------
# --- BATTLE LOGIC CLASSES & HELPER FUNCTIONS ---
# ----------------------------------------------------------------------

class EnemyShip(pygame.sprite.Sprite):
    def __init__(self, name, max_health, sprite_paths):
        super().__init__()
        self.name = name
        self.max_health = max_health
        self.current_health = max_health
        self.is_destroyed = False
        self.battle_pos = None 
        
        self.images = {
            "full": self._load_and_scale(sprite_paths["full"]),
            "half": self._load_and_scale(sprite_paths["half"]),
            "destroyed": self._load_and_scale(sprite_paths["destroyed"]),
        }
        
        self.image = self.images["full"]
        self.rect = self.image.get_rect()

    def _load_and_scale(self, sprite_path):
        try:
            full_path = os.path.join(ASSET_PATH, sprite_path) # <-- Debug Path
            print(f"DEBUG: Loading asset: {full_path}") # <-- NEW DEBUG PRINT
            original_image = pygame.image.load(full_path).convert_alpha()
            scale_factor = 0.5
            new_size = (int(original_image.get_width() * scale_factor), int(original_image.get_height() * scale_factor))
            return pygame.transform.scale(original_image, new_size) 
        except pygame.error as e:
            # THIS IS CRITICAL: If an image fails to load, the error is printed here.
            print(f"ERROR: Failed to load image {full_path}: {e}") # <-- MODIFIED DEBUG PRINT
            img = pygame.Surface((100, 100)) 
            img.fill((100, 100, 100))
            return img

    def get_current_sprite(self):
        if self.is_destroyed:
            return self.images["destroyed"]
        
        health_ratio = self.current_health / self.max_health
        
        if health_ratio <= 0.5:
            return self.images["half"]
        
        return self.images["full"]

    def take_damage(self):
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

def generate_non_overlapping_position(ship_size, min_distance, existing_positions, padding):
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

def get_current_target_ship():
    for ship in ENEMY_FLEET:
        if not ship.is_destroyed:
            return ship
    return None

def draw_ship_health(screen, ship):
    BAR_WIDTH = ship.image.get_width()
    BAR_HEIGHT = 10
    x = ship.rect.left
    y = ship.rect.top - BAR_HEIGHT - 5 
    fill = (ship.current_health / ship.max_health) * BAR_WIDTH
    background_rect = pygame.Rect(x, y, BAR_WIDTH, BAR_HEIGHT)
    pygame.draw.rect(screen, RED, background_rect) 
    fill_rect = pygame.Rect(x, y, fill, BAR_HEIGHT)
    pygame.draw.rect(screen, GREEN, fill_rect) 
    pygame.draw.rect(screen, BLACK, background_rect, 1)

def initialize_fleet_structure():
    global ENEMY_FLEET, MIN_SHIP_DISTANCE
    MIN_SHIP_DISTANCE = min(SCREEN_WIDTH, SCREEN_HEIGHT) // 3
    
    # Re-create fleet only if it's empty, otherwise just reposition them (or keep as is)
    if not ENEMY_FLEET: 
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
    global ENEMY_FLEET, PLAYER_FORTRESS
    if not ENEMY_FLEET:
        # Re-initialize fleet if it's empty, ensuring it has been created at least once
        initialize_fleet_structure() 
        
    for ship in ENEMY_FLEET:
        ship.current_health = ship.max_health
        ship.is_destroyed = False
        ship.image = ship.images["full"] 
    PLAYER_FORTRESS['health'] = PLAYER_FORTRESS['max_health']
    
# ----------------------------------------------------------------------
# --- PYGAME SPRITE CLASSES ---
# ----------------------------------------------------------------------

class Cannon(pygame.sprite.Sprite):
    def __init__(self):
        super().__init__()
        # Cannon image loading moved here to trigger the debug print
        try:
            full_path = os.path.join(ASSET_PATH, "Ship parts/cannon.png")
            print(f"DEBUG: Loading asset: {full_path}")
            original_image = pygame.image.load(full_path).convert_alpha()
            scale_factor = 1.5
            new_size = (int(original_image.get_width() * scale_factor), int(original_image.get_height() * scale_factor))
            self.image = pygame.transform.scale(original_image, new_size)
        except pygame.error:
            self.image = pygame.Surface((50, 30))
            self.image.fill(WHITE)
            
        self.rect = self.image.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))

    def draw_health_bar(self, screen):
        MAX_WIDTH = 250
        BAR_HEIGHT = 20
        x, y = 10, 10 
        fill_ratio = max(0, PLAYER_FORTRESS['health'] / PLAYER_FORTRESS['max_health'])
        fill_width = MAX_WIDTH * fill_ratio
        border_rect = pygame.Rect(x, y, MAX_WIDTH, BAR_HEIGHT)
        pygame.draw.rect(screen, BLACK, border_rect, 2)
        color = GREEN
        if fill_ratio < 0.5: color = (255, 165, 0)
        if fill_ratio < 0.2: color = RED
        fill_rect = pygame.Rect(x, y, fill_width, BAR_HEIGHT)
        pygame.draw.rect(screen, color, fill_rect)

        font = pygame.font.Font(None, 24)
        text = pygame.font.Font(None, 24).render(f"FORTRESS HP: {PLAYER_FORTRESS['health']:.1f}", True, WHITE) 
        screen.blit(text, (x + 5, y + 2))
        
        if PLAYER_FORTRESS['health'] <= 0:
            text_lost = pygame.font.Font(None, 24).render("FORTRESS LOST!", True, RED)
            screen.blit(text_lost, (x, y + BAR_HEIGHT + 5))
            
    def update(self):
        pass 


class Effect(pygame.sprite.Sprite):
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
            full_path = os.path.join(ASSET_PATH, path)
            print(f"DEBUG: Loading asset: {full_path}") # <-- NEW DEBUG PRINT
            original_image = pygame.image.load(full_path).convert_alpha()
            new_width = int(original_image.get_width() * scale)
            new_height = int(original_image.get_height() * scale)
            self.image = pygame.transform.scale(original_image, (new_width, new_height))
        except pygame.error as e:
            print(f"ERROR: Failed to load image {full_path}: {e}") # <-- MODIFIED DEBUG PRINT
            self.image = pygame.Surface((20, 20))
            self.image.fill(BLACK)
            
        self.rect = self.image.get_rect(center=self.position)


# ----------------------------------------------------------------------
# --- HARDWARE CONTROLLER THREAD (Integrated LED Control) ---
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
            # Catching any other hardware initialization error
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
                    pass 
                time.sleep(0.0001) # Small sleep to yield CPU time

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
                
                time.sleep(0.0001) # Small sleep to yield CPU time

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
    
    # --- CRITICAL FIX: Initialize thread variable to None ---
    hardware_thread = None 
    # --------------------------------------------------------
    
    print("DEBUG: 1. Initializing Pygame...") # <-- NEW DEBUG PRINT
    pygame.init() # "Hello from the pygame community" prints here
    print("DEBUG: 1. Pygame Init Successful. Attempting Screen Setup.") # <-- NEW DEBUG PRINT
    
    try:
        print("DEBUG: 1a. Attempting Fullscreen Mode...") # <-- NEW DEBUG PRINT
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        
        global SCREEN_WIDTH, SCREEN_HEIGHT 
        SCREEN_WIDTH = screen.get_width()
        SCREEN_HEIGHT = screen.get_height()
        print(f"DEBUG: 1b. Screen set to {SCREEN_WIDTH}x{SCREEN_HEIGHT} (Fullscreen).") # <-- NEW DEBUG PRINT
        
    except pygame.error as e:
        print(f"DEBUG: 1c. Fullscreen FAILED: {e}. Falling back to 800x600.") # <-- NEW DEBUG PRINT
        screen = pygame.display.set_mode((800, 600))
        # SCREEN_WIDTH/HEIGHT defaults already set at top of file
        print("DEBUG: 1d. Screen set to 800x600.") # <-- NEW DEBUG PRINT
        
    global MIN_SHIP_DISTANCE
    MIN_SHIP_DISTANCE = min(SCREEN_WIDTH, SCREEN_HEIGHT) // 3
    print(f"DEBUG: 2. Screen Configured. MIN_SHIP_DISTANCE={MIN_SHIP_DISTANCE}") # <-- NEW DEBUG PRINT
        
    pygame.display.set_caption("Whack-A-Pirate Battle")
    clock = pygame.time.Clock() 
    
    ocean_tile = None
    try:
        full_path = os.path.join(ASSET_PATH, "Tiles/tile_73.png")
        print(f"DEBUG: Loading asset: {full_path}") # <-- NEW DEBUG PRINT
        ocean_tile = pygame.image.load(full_path).convert()
    except pygame.error as e:
        print(f"ERROR: Failed to load tile_73.png: {e}. Using solid blue color.") # <-- MODIFIED DEBUG PRINT
    
    print("DEBUG: 3. Initializing Fleet Structure (This loads all ship images)...") # <-- NEW DEBUG PRINT
    initialize_fleet_structure()
    
    game_running = False
    game_over = False
    last_game_score = 0
    
    all_sprites = pygame.sprite.Group()
    cannon_sprites = pygame.sprite.Group()
    
    print("DEBUG: 3a. Setting up Cannon (This loads cannon image)...") # <-- NEW DEBUG PRINT
    player_cannon = Cannon()
    cannon_sprites.add(player_cannon)
    
    # --- START HARDWARE THREAD (Now Safe from NameError) ---
    print("DEBUG: 4. Starting Hardware Thread. Check for HARDWARE: Error messages next.") # <-- NEW DEBUG PRINT
    hardware_thread = HardwareThread(event_queue)
    hardware_thread.start()
    
    # Check if the thread object was created successfully
    if hardware_thread and hardware_thread.is_available:
        print("DEBUG: 4a. Hardware Thread initialized successfully.")
    elif hardware_thread and not hardware_thread.is_available:
        print("DEBUG: 4b. Hardware Thread initialized in simulation mode.")
    else:
        # This print should only trigger if the HardwareThread.__init__ failed catastrophically
        print("CRITICAL ERROR: Hardware Thread failed to instantiate.") 
    
    print("DEBUG: 5. Entering Main Pygame Loop. Window should open now.") # <-- NEW DEBUG PRINT
    
    running = True
    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                
            # 1. Process Hardware Events (Read Queue)
            # ... (rest of the main loop logic is unchanged)
            
            # Cap drawing speed at FPS (90)
            pygame.display.flip()
            clock.tick(FPS)
    
    except KeyboardInterrupt:
        print("\nKeyboard interrupt detected. Initiating graceful shutdown...")
        running = False

    finally:
        # --- CLEANUP (GUARANTEED TO RUN) ---
        print("Cleaning up threads and Pygame resources.")
        
        # 1. Stop the Hardware thread ONLY IF IT WAS SUCCESSFULLY INITIALIZED
        if hardware_thread:
            hardware_thread.stop()
            hardware_thread.join()
        
        # 2. Perform FINAL Plasma cleanup in the main thread
        try:
            # Re-initialize Plasma one last time to ensure the 'all off' command is sent.
            final_plasma = auto(default=f"GPIO:14:15:pixel_count={NUM_PIXELS}")
            final_plasma.set_all(0, 0, 0)
            final_plasma.show()
            print("Cleanup successful: Plasma LEDs turned off.")
        except Exception as e:
            # This catch is necessary if the initial hardware setup failed, but we still try to shut down.
            print(f"Cleanup failed: Could not access Plasma device for final shutdown. {e}")
            
        pygame.quit()
        sys.exit()