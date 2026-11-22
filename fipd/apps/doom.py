"""
doom.py — DOOM pour FIP : raycasting maison, 320x240, zero dependance.

Un niveau = la carte MAP ci-dessous. Le joueur avance, tourne, tire au
fusil a pompe sur des imps (boules de feu) et des demons (charge au
contact). LEDs 1-6 = jauge de sante.

Controles :
  molette gauche  = tourner (capturee par le jeu sur cette page)
  molette droite = avancer / reculer d'un pas
  S1 = tirer (ou demarrer / rejouer)
  S2 / S3 = pas de cote gauche / droit
  S6 = marche auto on/off (courir sans molette)

Rendu : 160 rayons DDA (colonnes de 2 px), murs ombrages par distance +
face + joints de briques, sprites billboards clippe contre le z-buffer,
flash de blesse, HUD visage / sante / kills / munitions.
"""
import math
import random
import threading
import time

import fipui
from fipui import App
from PIL import Image, ImageDraw, ImageEnhance

# ---------------------------------------------------------------- carte --

MAP = [
    "########################",
    "#......#........#......#",
    "#.P....#..i.....#..m...#",
    "#......#........#......#",
    "#......#........#......#",
    "####.###........###.####",
    "#......#........#......#",
    "#..a...#........#..i...#",
    "#......#..=..=..#......#",
    "###.####..=..=..####.###",
    "#......#........#......#",
    "#..i...#....i...#..d...#",
    "#......#..=..=..#......#",
    "####.###..=..=..####.###",
    "#......#........#......#",
    "#......#...i....#..a...#",
    "#..d...#........#......#",
    "#......#........#......#",
    "####.####.####.###.#####",
    "#~......~....~.....i...#",
    "#~..i...~..m..~........#",
    "#~......~....~....a....#",
    "#~~~~~~~~~~~~~~~~~~~~~~#",
    "########################",
]
MW = MH = len(MAP)                       # 24 x 24, assert au parse

WALL_COLORS = {                          # teinte de base par type de mur
    '#': (150, 82, 56),                  # brique
    '=': (108, 116, 130),                # tech
    '%': (96, 122, 92),                  # metal
    '~': (146, 52, 52),                  # chair (hell)
}

FOV = 0.66                               # demi-largeur du plan camera (~66 deg)
NCOLS = 160                              # rayons (colonnes de 2 px)
TURN = 0.21                              # rad par cran de molette gauche
STEP = 0.45                             # unites par cran de molette droite
STRAFE = 0.33
RADIUS = 0.22                            # rayon de collision joueur/ennemis
FIRE_CD = 0.55                           # delai entre deux tirs
PLAYER_SPEED = 2.3                       # vitesse de la marche auto

# ------------------------------------------------------------ pixel art --
# '.' = transparent. Imp ~0.78 u, demon ~0.62 u (rapport via SPRITE_H).

IMP_ART = [
    "..#....#..",
    "..#.##.#..",
    "...#oo#...",
    "...####...",
    "..######..",
    ".#+#####+.",
    ".#+#####+.",
    "..##xx##..",
    "...####...",
    "...#..#...",
    "...#..#...",
    "..##..##..",
]
IMP_ART2 = IMP_ART[:7] + [
    "..##xx##..",
    "...####...",
    "..#....#..",
    "..#....#..",
    ".##....##.",
]
DEMON_ART = [
    "..######..",
    ".#oooooo#.",
    "#o##oo##o#",
    "#oooooooo#",
    "#o+oooo+o#",
    "#oooooooo#",
    ".#xxxxxx#.",
    ".#.####.#.",
    "..#.##.#..",
    "..##..##..",
]
DEMON_ART2 = DEMON_ART[:6] + [
    ".#xxxxxx#.",
    ".#.####.#.",
    ".#..##..#.",
    ".#......#.",
    "##......##",
]
FIRE_ART = [
    "..ff..",
    ".fFFf.",
    "fFWWFf",
    "fFWWFf",
    ".fFFf.",
    "..ff..",
]
MED_ART = [
    ".wwwwwww.",
    ".wwrrrww.",
    ".wwrrrww.",
    ".wwwrwww.",
    ".wwrrrww.",
    ".wwrrrww.",
    ".wwwwwww.",
]
AMMO_ART = [
    ".ggggg.",
    ".gygyg.",
    ".ggggg.",
    ".gygyg.",
    ".ggggg.",
]

SPRITE_PAL = {
    '#': (122, 72, 42), '+': (172, 112, 62), 'o': (250, 216, 60),
    'x': (210, 60, 44), 'f': (232, 120, 30), 'F': (252, 190, 60),
    'W': (255, 245, 200), 'w': (238, 238, 238), 'r': (208, 36, 36),
    'g': (74, 108, 66), 'y': (208, 180, 70),
}

# visage HUD 10x10 : peau, cheveux, yeux, bouche, sang
FACE_OK = [
    "..bbbbbb..",
    ".b%%%%%%b.",
    "b%%%%%%%%b",
    "b%e%%%%%eb",
    "b%e%%%%%eb",
    "b%%%%%%%%b",
    "b%%p%%p%%b",
    "b%%%%%%%%b",
    ".b%mmmm%b.",
    "..b%%%%b..",
]
FACE_HURT = [
    "..bbbbbb..",
    ".b%%%%%%b.",
    "b%%%r%%%%b",
    "b%e%%%%%eb",
    "bhe%%%%%eb",
    "b%%%%r%%%b",
    "b%%p%%p%%b",
    "b%%%mm%%%b",
    ".b%%%%%%b.",
    "..b%%%%b..",
]
FACE_CRIT = [
    "..bbbbbb..",
    ".b%%rr%%b.",
    "b%%r%%r%%b",
    "b%he%%eh%b",
    "b%%%%%%%%b",
    "b%%rrrr%%b",
    "b%r%pp%r%b",
    "b%%r%%r%%b",
    ".b%rrrr%b.",
    "..b%%%%b..",
]
FACE_PAL = {
    'b': (74, 48, 30), '%': (198, 152, 118), 'e': (240, 240, 240),
    'p': (30, 30, 34), 'm': (150, 90, 80), 'r': (196, 28, 28), 'h': (40, 26, 20),
}

SPRITE_H = {                             # hauteur monde (unites) par sprite
    'imp': 0.78, 'demon': 0.62, 'fire': 0.30, 'med': 0.34, 'ammo': 0.24,
}


def _art_to_img(art, pal):
    """Matrice de caracteres -> PIL RGBA."""
    h, w = len(art), len(art[0])
    im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    for y, row in enumerate(art):
        for x, ch in enumerate(row):
            c = pal.get(ch)
            if c:
                im.putpixel((x, y), c + (255,))
    return im


def _flash(img):
    """Version eblouie (touché par un tir) — reste RGBA (alpha preserve)."""
    return ImageEnhance.Brightness(img).enhance(2.6)


class Demon:
    __slots__ = ('kind', 'x', 'y', 'hp', 'pain', 'die_t', 'atk_cd', 'los_ok',
                 'los_t', 'phase', 'dead')

    def __init__(self, kind, x, y, hp):
        self.kind = kind
        self.x = x
        self.y = y
        self.hp = hp
        self.pain = 0.0
        self.die_t = 0.0
        self.atk_cd = random.uniform(0.5, 1.5)
        self.los_ok = False
        self.los_t = 0.0
        self.phase = random.random() * 7.0
        self.dead = False


class DoomApp(App):
    """DOOM — raycasting sur FIP. Voir docstring module pour les controles."""
    name = 'doom'
    title = 'DOOM'
    show_footer = False
    capture_left_knob = True

    def __init__(self, daemon):
        super().__init__(daemon)
        self.state = 'idle'              # idle | play | won | dead
        self.level = 1
        self.score = 0
        self.best = 0
        self.hp = 100
        self.ammo = 24
        self.kills = 0
        self.total = 0
        self.px = self.py = 2.5
        self.ang = 0.0
        self.enemies = []
        self.fires = []                  # boules de feu [{x, y, dx, dy, t}]
        self.items = []                  # [{kind, x, y}]
        self.grid = []
        self.recoil = 0.0                # recul du fusil (0..1)
        self.muzzle = 0.0                # flash du tir
        self.hurt = 0.0                  # flash rouge de blesse
        self.won_t = 0.0
        self.note = None                 # (texte, t0) banniere info
        self.auto = False                # marche auto (S6)
        self.fire_cd = 0.0
        self._glock = threading.Lock()
        self._zb = [1e9] * NCOLS
        self._stop = threading.Event()
        self._thread = None
        self._mk_sprites()
        self._new_level(1)

    # ------------------------------------------------ sprites & fonts --

    def _mk_sprites(self):
        self.spr = {}
        for key, frames in (('imp', (IMP_ART, IMP_ART2)),
                            ('demon', (DEMON_ART, DEMON_ART2)),
                            ('fire', (FIRE_ART, FIRE_ART)),
                            ('med', (MED_ART,)),
                            ('ammo', (AMMO_ART,))):
            base = [_art_to_img(a, SPRITE_PAL) for a in frames]
            self.spr[key] = (base, [_flash(b) for b in base])
        faces = {}
        for k, art in (('ok', FACE_OK), ('hurt', FACE_HURT),
                       ('crit', FACE_CRIT)):
            im = _art_to_img(art, FACE_PAL)
            faces[k] = im.resize((30, 30), Image.NEAREST)
        self.faces = faces
        self.F_BIG = fipui.font(44)
        self.F_MED = fipui.font(20)
        self.F_SML = fipui.font(14)

    # -------------------------------------------------- niveau / etat --

    def _new_level(self, level):
        self.grid = []
        spawn = (2.5, 2.5)
        self.enemies = []
        self.items = []
        extra = 2 * (level - 1)          # imps supplementaires par niveau
        floors = []
        for y, row in enumerate(MAP):
            assert len(row) == MW, f'carte ligne {y} : {len(row)} != {MW}'
            cells = []
            for x, ch in enumerate(row):
                if ch in WALL_COLORS:
                    cells.append(ch)
                else:
                    cells.append(0)
                    pos = (x + 0.5, y + 0.5)
                    if ch == 'P':
                        spawn = pos
                    elif ch == 'i':
                        self.enemies.append(Demon('imp', *pos, 20))
                    elif ch == 'd':
                        self.enemies.append(Demon('demon', *pos, 30))
                    elif ch == 'm':
                        self.items.append({'kind': 'med', 'x': pos[0], 'y': pos[1]})
                    elif ch == 'a':
                        self.items.append({'kind': 'ammo', 'x': pos[0], 'y': pos[1]})
                    if ch in '.idaPm':
                        floors.append(pos)
            self.grid.append(cells)
        for _ in range(extra):           # renforts places au hasard au sol
            x, y = random.choice(floors)
            self.enemies.append(Demon('imp', x, y, 20))
        self.px, self.py = spawn
        self.ang = 0.0
        self.total = len(self.enemies)
        self.kills = 0
        self.fires = []
        self.auto = False
        for e in self.enemies:
            e.atk_cd *= 1.0 + 0.12 * (level - 1)

    def _reset(self):
        self.level = 1
        self.score = 0
        self.hp = 100
        self.ammo = 24
        self._new_level(1)
        self.state = 'play'
        self._sync_leds()

    # ---------------------------------------------- cycle de vie FIP --

    def on_enter(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._sync_leds()

    def on_leave(self):
        self._stop.set()
        for i in range(1, 7):
            self.d.set_led(i, False)

    def _loop(self):
        """Boucle de jeu : physique ~50 Hz, redraw ~12 Hz max."""
        last = time.time()
        last_draw = 0.0
        while not self._stop.is_set():
            now = time.time()
            dt = min(0.1, now - last)
            last = now
            with self._glock:
                if self.state == 'play':
                    self._update(dt)
                elif self.state == 'won' and now - self.won_t > 2.2:
                    self.level += 1
                    self.score += 50
                    self.hp = min(100, self.hp + 25)
                    self.ammo = min(50, self.ammo + 10)
                    self._new_level(self.level)
                    self.state = 'play'
                    self.note = (f'NIVEAU {self.level}', now)
            if now - last_draw >= 0.08 and self.state in ('play', 'won'):
                last_draw = now
                self.d._redraw_now()
            self._stop.wait(0.02)

    # ---------------------------------------------------- evenements --

    def on_event(self, name, pressed):
        if not pressed:
            return
        if self.state != 'play':
            if name.startswith('S'):     # idle / dead : un bouton relance
                self._reset()
                self.d._redraw_now()
            return
        with self._glock:
            if name == 'left_cw':
                self.ang = (self.ang + TURN) % (2 * math.pi)
            elif name == 'left_ccw':
                self.ang = (self.ang - TURN) % (2 * math.pi)
            elif name == 'right_cw':
                self._move(math.cos(self.ang), math.sin(self.ang), STEP)
            elif name == 'right_ccw':
                self._move(math.cos(self.ang), math.sin(self.ang), -STEP * 0.7)
            elif name == 'S2':
                self._move(math.sin(self.ang), -math.cos(self.ang), -STRAFE)
            elif name == 'S3':
                self._move(math.sin(self.ang), -math.cos(self.ang), STRAFE)
            elif name == 'S6':
                self.auto = not self.auto
                self.note = ('MARCHE AUTO' if self.auto else 'ARRET', time.time())
            elif name == 'S1':
                self._fire()
            self._pickup()
        self.d._redraw_now()

    # ------------------------------------------------------ physique --

    def _blocked(self, x, y):
        for dx in (-RADIUS, 0.0, RADIUS):
            for dy in (-RADIUS, 0.0, RADIUS):
                cx, cy = int(x + dx), int(y + dy)
                if self.grid[cy][cx]:
                    return True
        return False

    def _move(self, dx, dy, dist):
        nx, ny = self.px + dx * dist, self.py + dy * dist
        if not self._blocked(nx, self.py):
            self.px = nx
        if not self._blocked(self.px, ny):
            self.py = ny

    def _los(self, x0, y0, x1, y1):
        """Ligne de vue : echantillonne le segment, mur = coupe."""
        d = math.hypot(x1 - x0, y1 - y0)
        if d < 0.01:
            return True
        steps = int(d / 0.15) + 1
        for i in range(1, steps):
            t = i / steps
            if self.grid[int(y0 + (y1 - y0) * t)][int(x0 + (x1 - x0) * t)]:
                return False
        return True

    def _fire(self):
        if self.fire_cd > 0:
            return
        if self.ammo <= 0:
            self.note = ('PLUS DE MUNITIONS', time.time())
            return
        self.ammo -= 1
        self.fire_cd = FIRE_CD
        self.recoil = 1.0
        self.muzzle = 0.09
        # hitscan : ennemi le plus proche dans un cone etroit devant
        best, best_d = None, 1e9
        for e in self.enemies:
            if e.dead:
                continue
            rx, ry = e.x - self.px, e.y - self.py
            d = math.hypot(rx, ry)
            if d > 16 or d < 0.05:
                continue
            da = (math.atan2(ry, rx) - self.ang + math.pi) % (2 * math.pi) - math.pi
            if abs(da) > math.atan2(0.35, d):
                continue
            if not self._los(self.px, self.py, e.x, e.y):
                continue
            if d < best_d:
                best, best_d = e, d
        if best:
            dmg = max(8, int(26 - best_d))
            best.hp -= dmg
            best.pain = 0.14
            self._move_push(best, math.cos(self.ang), math.sin(self.ang), 0.25)
            if best.hp <= 0:
                best.dead = True
                best.die_t = 0.8
                self.kills += 1
                self.score += 10
                self.set_led(7, True)
                threading.Timer(0.12, lambda: self.set_led(7, False)).start()
                if self.kills >= self.total:
                    self.state = 'won'
                    self.won_t = time.time()
                    self.score += 20
        self.set_led(8, True)
        threading.Timer(0.1, lambda: self.set_led(8, False)).start()

    def _move_push(self, e, dx, dy, dist):
        nx, ny = e.x + dx * dist, e.y + dy * dist
        if not self.grid[int(e.y)][int(nx)]:
            e.x = nx
        if not self.grid[int(ny)][int(e.x)]:
            e.y = ny

    def _hurt_player(self, dmg):
        self.hp -= dmg
        self.hurt = 0.3
        self._sync_leds()
        if self.hp <= 0:
            self.hp = 0
            self.state = 'dead'
            self.best = max(self.best, self.score)
            self.auto = False
            self.alert(f'Score {self.score}')

    def _pickup(self):
        for it in list(self.items):
            if math.hypot(it['x'] - self.px, it['y'] - self.py) < 0.55:
                self.items.remove(it)
                if it['kind'] == 'med':
                    if self.hp >= 100:
                        continue          # laisse le kit si pleine sante
                    self.hp = min(100, self.hp + 25)
                    self.note = ('MEDKIT +25', time.time())
                else:
                    self.ammo = min(50, self.ammo + 8)
                    self.note = ('MUNITIONS +8', time.time())
                self._sync_leds()

    def _update(self, dt):
        now = time.time()
        self.fire_cd = max(0.0, self.fire_cd - dt)
        self.recoil = max(0.0, self.recoil - dt * 5)
        self.muzzle = max(0.0, self.muzzle - dt)
        self.hurt = max(0.0, self.hurt - dt)
        if self.auto:
            self._move(math.cos(self.ang), math.sin(self.ang),
                       PLAYER_SPEED * dt)
            self._pickup()
        # --- ennemis ---
        for e in self.enemies:
            if e.dead:
                e.die_t -= dt
                continue
            e.pain = max(0.0, e.pain - dt)
            e.atk_cd -= dt
            e.los_t -= dt
            if e.los_t <= 0:
                e.los_t = 0.25
                e.los_ok = self._los(e.x, e.y, self.px, self.py)
            if not e.los_ok:
                continue
            dx, dy = self.px - e.x, self.py - e.y
            d = math.hypot(dx, dy)
            if d < 0.01:
                continue
            speed = (1.15 if e.kind == 'demon' else 0.75) * (1 + 0.08 * (self.level - 1))
            if e.kind == 'imp' and d < 3.5:
                pass                       # trop pres : tire au lieu d'avancer
            elif d > (0.6 if e.kind == 'demon' else 0.9):
                self._move_push(e, dx / d, dy / d, speed * dt)
            if e.atk_cd <= 0:
                if e.kind == 'demon' and d < 0.8:
                    e.atk_cd = 0.9
                    self._hurt_player(12)
                elif e.kind == 'imp' and d < 9:
                    e.atk_cd = max(1.2, 2.6 - 0.15 * self.level)
                    ang = math.atan2(dy, dx) + random.uniform(-0.05, 0.05)
                    self.fires.append({'x': e.x, 'y': e.y,
                                       'dx': math.cos(ang) * 3.2,
                                       'dy': math.sin(ang) * 3.2, 't': 4.0})
        self.enemies = [e for e in self.enemies if not (e.dead and e.die_t <= 0)]
        # --- boules de feu ---
        for f in list(self.fires):
            f['x'] += f['dx'] * dt
            f['y'] += f['dy'] * dt
            f['t'] -= dt
            if f['t'] <= 0 or self.grid[int(f['y'])][int(f['x'])]:
                self.fires.remove(f)
            elif math.hypot(f['x'] - self.px, f['y'] - self.py) < 0.4:
                self.fires.remove(f)
                self._hurt_player(11)

    # -------------------------------------------------------- LEDs --

    def _sync_leds(self):
        """LEDs 1-6 = jauge de sante (6 segments)."""
        n = min(6, math.ceil(self.hp / 16.7))
        for i in range(1, 7):
            self.set_led(i, i <= n)

    # ------------------------------------------------------ rendu 3D --

    def render_full(self, img):
        with self._glock:
            self._draw_scene(img)
        d = ImageDraw.Draw(img)
        if self.hurt > 0:
            img.paste(Image.blend(img, Image.new('RGB', img.size, (200, 20, 16)),
                                  0.10 + 0.25 * self.hurt), (0, 0))
        if self.state == 'idle':
            self._overlay_title(d)
        elif self.state == 'dead':
            self._overlay_dead(d)
        elif self.state == 'won':
            d.rectangle((0, 0, 319, 24), fill=(20, 60, 24))
            d.text((160, 12), f'NIVEAU {self.level} TERMINE  +50',
                   font=self.F_MED, fill=(140, 255, 150), anchor='mm')

    def _draw_scene(self, img):
        d = ImageDraw.Draw(img)
        px, py, ang = self.px, self.py, self.ang
        dirx, diry = math.cos(ang), math.sin(ang)
        planex, planey = -diry * FOV, dirx * FOV
        # --- ciel (enfer) et sol : degrades par bandes ---
        for i in range(10):               # y = 0 -> 100
            t = i / 9.0
            c = (int(26 + 60 * t), int(6 + 12 * t), int(6 + 10 * t))
            d.rectangle((0, i * 10, 319, i * 10 + 10), fill=c)
        for i in range(14):               # y = 100 -> 240
            t = i / 13.0
            c = (int(48 + 66 * t), int(38 + 50 * t), int(30 + 36 * t))
            d.rectangle((0, 100 + i * 10, 319, 110 + i * 10), fill=c)
        # --- murs : DDA, 160 colonnes de 2 px ---
        zb = self._zb
        for col in range(NCOLS):
            camx = 2.0 * col / (NCOLS - 1) - 1.0
            rdx = dirx + planex * camx
            rdy = diry + planey * camx
            mx, my = int(px), int(py)
            ddx = abs(1.0 / rdx) if rdx else 1e30
            ddy = abs(1.0 / rdy) if rdy else 1e30
            if rdx < 0:
                sx, sdx = -1, (px - mx) * ddx
            else:
                sx, sdx = 1, (mx + 1.0 - px) * ddx
            if rdy < 0:
                sy, sdy = -1, (py - my) * ddy
            else:
                sy, sdy = 1, (my + 1.0 - py) * ddy
            side, hit = 0, 0
            for _ in range(64):
                if sdx < sdy:
                    sdx += ddx
                    mx += sx
                    side = 0
                else:
                    sdy += ddy
                    my += sy
                    side = 1
                if mx < 0 or my < 0 or mx >= MW or my >= MH:
                    break
                hit = self.grid[my][mx]
                if hit:
                    break
            dist = max(0.05, (sdx - ddx) if side == 0 else (sdy - ddy))
            zb[col] = dist
            if not hit:
                continue
            h = int(240 / dist)
            y0 = 120 - h // 2
            y1 = y0 + h
            wall_x = (py + dist * rdy) if side == 0 else (px + dist * rdx)
            frac = wall_x - math.floor(wall_x)
            fog = max(0.16, 1.0 - dist / 13.0)
            if side:
                fog *= 0.72
            if frac < 0.06 or frac > 0.94:          # joint de brique
                fog *= 0.78
            r, g, b = WALL_COLORS[hit]
            x = col * 2
            d.rectangle((x, y0, x + 1, y1),
                        fill=(int(r * fog), int(g * fog), int(b * fog)))
            if h > 14:                              # liseres haut/bas
                rf, gf, bf = int(r * fog * 0.6), int(g * fog * 0.6), int(b * fog * 0.6)
                d.rectangle((x, y0, x + 1, y0 + 2), fill=(rf, gf, bf))
                d.rectangle((x, y1 - 2, x + 1, y1), fill=(rf, gf, bf))
        # --- sprites (tri par distance decroissante, clip z-buffer) ---
        sprites = []
        for e in self.enemies:
            dx, dy = e.x - px, e.y - py
            if e.dead:
                sprites.append((dx * dx + dy * dy, 'corpse', e, None))
            else:
                sprites.append((dx * dx + dy * dy, 'mob', e, None))
        for f in self.fires:
            dx, dy = f['x'] - px, f['y'] - py
            sprites.append((dx * dx + dy * dy, 'fire', f, None))
        for it in self.items:
            dx, dy = it['x'] - px, it['y'] - py
            sprites.append((dx * dx + dy * dy, 'item', it, None))
        sprites.sort(key=lambda s: -s[0])
        inv = 1.0 / (planex * diry - dirx * planey)
        for _, kind, obj, _ in sprites:
            if kind in ('mob', 'corpse'):
                ox, oy = obj.x, obj.y
            else:
                ox, oy = obj['x'], obj['y']
            relx, rely = ox - px, oy - py
            depth = inv * (-planey * relx + planex * rely)   # profondeur camera
            if depth <= 0.12:
                continue
            lat = inv * (diry * relx - dirx * rely)          # ecart lateral
            scx = int(160 * (1.0 + lat / depth))
            if scx < -80 or scx > 400:
                continue
            if kind == 'corpse':
                sh = max(2, int(30 / depth))
                sy = 120 + int(120 / depth) - sh
                d.rectangle((scx - sh, sy, scx + sh, sy + sh), fill=(96, 20, 16))
                continue
            if kind == 'mob':
                key = obj.kind
                frame = int(time.time() * 3 + obj.phase) % 2
                base, flash = self.spr[key]
                src = flash[frame] if obj.pain > 0 else base[frame]
            elif kind == 'fire':
                src = self.spr['fire'][0][0]
                frame = 0
            else:
                src = self.spr[obj['kind']][0][0]
            if kind == 'mob':
                world_h = SPRITE_H[obj.kind]
            elif kind == 'fire':
                world_h = SPRITE_H['fire']
            else:
                world_h = SPRITE_H[obj['kind']]
            sh = max(1, int(240 * world_h / depth))
            sw = max(1, int(sh * src.width / src.height))
            feet = 120 + int(120 / depth)
            top = feet - sh
            spr = src.resize((sw, sh), Image.NEAREST)
            mask = spr.split()[3]                      # alpha (la source est RGBA)
            if kind == 'mob':                          # fog sur les ennemis
                fog = max(0.25, 1.0 - depth / 13.0)
                if fog < 0.95:
                    spr = ImageEnhance.Brightness(spr).enhance(fog)
            # segments de colonnes visibles (z-buffer etendu en 320 px)
            x0 = max(0, scx - sw // 2)
            x1 = min(319, scx + sw // 2)
            seg_a = None
            for c in range(x0, x1 + 2):
                vis = c <= x1 and zb[min(NCOLS - 1, max(0, c // 2))] > depth + 0.05
                if vis and seg_a is None:
                    seg_a = c
                elif not vis and seg_a is not None:
                    box = (seg_a - x0, 0, c - x0, sh)
                    img.paste(spr.crop(box), (seg_a, top), mask.crop(box))
                    seg_a = None
        # --- flash du tir : eclaircit la scene entiere ---
        if self.muzzle > 0:
            img.paste(Image.blend(img, Image.new('RGB', img.size, (255, 232, 170)),
                                  0.14), (0, 0))
        # --- fusil a pompe ---
        self._draw_gun(d)
        # --- HUD ---
        self._draw_hud(d, img)
        if self.note:
            msg, t0 = self.note
            if time.time() - t0 > 1.2:
                self.note = None
            else:
                d.rectangle((60, 2, 259, 22), fill=(10, 12, 20))
                d.text((160, 12), msg, font=self.F_SML, fill=fipui.ACCENT,
                       anchor='mm')

    def _draw_gun(self, d):
        cx = 196
        kick = int(self.recoil * 16)
        y = 240 - 64 + kick
        d.rectangle((cx - 22, y + 40, cx - 4, y + 64), fill=(96, 62, 36))   # crosse
        d.rectangle((cx - 14, y + 18, cx - 8, y + 44), fill=(70, 74, 84))   # garde
        d.rectangle((cx - 8, y + 18, cx + 8, y + 40), fill=(52, 56, 64))    # bloc
        d.rectangle((cx - 7, y, cx - 2, y + 20), fill=(88, 92, 102))       # canon 1
        d.rectangle((cx + 2, y, cx + 7, y + 20), fill=(88, 92, 102))       # canon 2
        if self.muzzle > 0:
            d.ellipse((cx - 16, y - 18, cx + 16, y + 6), fill=(255, 236, 150))
            d.ellipse((cx - 8, y - 12, cx + 8, y + 2), fill=(255, 255, 220))

    def _draw_hud(self, d, img):
        d.rectangle((0, 212, 319, 239), fill=(12, 12, 16))
        d.line((0, 212, 319, 212), fill=(60, 60, 70))
        face = 'crit' if (self.state == 'dead' or self.hp <= 33) else \
            'hurt' if self.hp <= 66 else 'ok'
        img.paste(self.faces[face], (5, 210), self.faces[face])
        d.text((56, 226), f'{self.hp:3d}%', font=self.F_MED,
               fill=fipui.GREEN if self.hp > 33 else fipui.RED, anchor='lm')
        d.text((160, 226), f'KILLS {self.kills}/{self.total}', font=self.F_SML,
               fill=fipui.FG, anchor='mm')
        d.text((272, 224), f'{self.ammo}', font=self.F_MED,
               fill=fipui.ACCENT, anchor='rm')
        d.text((272, 236), 'MUN', font=self.F_SML, fill=fipui.DIM, anchor='rm')

    # ------------------------------------------------------ overlays --

    def _overlay_title(self, d):
        d.rectangle((14, 46, 305, 194), fill=(10, 10, 14))
        d.rectangle((14, 46, 305, 194), outline=(150, 40, 30), width=2)
        d.text((163, 88), 'DOOM', font=self.F_BIG, fill=(232, 44, 30), anchor='mm')
        d.text((160, 121), '— FIP EDITION —', font=self.F_SML, fill=fipui.ACCENT,
               anchor='mm')
        d.text((160, 148), 'un bouton = jouer', font=self.F_MED, fill=fipui.FG,
               anchor='mm')
        d.text((160, 172), 'molette G tourner · D avancer', font=self.F_SML,
               fill=fipui.DIM, anchor='mm')
        d.text((160, 186), 'S1 tir · S2/S3 cote · S6 courir', font=self.F_SML,
               fill=fipui.DIM, anchor='mm')

    def _overlay_dead(self, d):
        d.rectangle((24, 70, 295, 180), fill=(16, 6, 6))
        d.rectangle((24, 70, 295, 180), outline=(160, 30, 24), width=2)
        d.text((160, 98), 'MORT', font=self.F_BIG, fill=(220, 40, 30), anchor='mm')
        d.text((160, 132), f'Score {self.score} · Niveau {self.level}',
               font=self.F_MED, fill=fipui.FG, anchor='mm')
        d.text((160, 158), f'RECORD {self.best}', font=self.F_SML, fill=fipui.DIM,
               anchor='mm')
        d.text((160, 172), 'un bouton = recommencer', font=self.F_SML,
               fill=fipui.DIM, anchor='mm')
