"""
gen_screens.py — Captures d'ecran du FIP pour la doc (docs/).
Les pages sont rendues exactement comme le daemon les envoie au device
(rendu PIL identique, avant le flip vertical propre au transport).
Genere : docs/pageN.png (x5), docs/pages.png (grille), docs/hero.png (mockup).
"""
import os

from PIL import Image, ImageDraw, ImageFont

import fipd3

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs')
os.makedirs(OUT, exist_ok=True)

D = fipd3.D


def font(sz):
    for p in (r'C:\Windows\Fonts\consola.ttf', r'C:\Windows\Fonts\arial.ttf'):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    return ImageFont.load_default()


F_LBL = font(17)
F_CAP = font(15)

# --- rendu des 5 pages dans des etats vivants ---
D.page = 0
p_clock = D.render_page()                     # horloge (heure reelle)
D.page = 1
p_sys = D.render_page()                       # systeme (valeurs reelles)
D.page = 2
p_net = D.render_page()                       # reseau
D.page = 3
D._prev_btn = 0x40 | 0x200 | 0x02             # S2 + S5 enfonces + molette droite
p_test = D.render_page()
D._prev_btn = 0
D.page = 4
D._scroll = 264                               # milieu du texte, barre visible
p_read = D.render_page()
D._scroll = 0
D.page = 0

pages = [p_clock, p_sys, p_net, p_test, p_read]
labels = ['1 - Clock', '2 - System', '3 - Network', '4 - Button tester',
          '5 - Reader (right knob)']

for i, img in enumerate(pages, 1):
    img.save(os.path.join(OUT, f'page{i}.png'))

# --- grille 3 + 2 ---
M, LBL = 18, 30
W = 3 * 320 + 4 * M
H = 2 * 240 + 2 * LBL + 3 * M
grid = Image.new('RGB', (W, H), (13, 17, 23))
d = ImageDraw.Draw(grid)
for i, (img, lab) in enumerate(zip(pages, labels)):
    r, c = divmod(i, 3)
    x = M + c * (320 + M)
    y = M + r * (240 + LBL + M)
    grid.paste(img, (x, y))
    d.rectangle((x - 1, y - 1, x + 320, y + 240), outline=(45, 52, 66))
    d.text((x + 160, y + 240 + LBL // 2), lab, font=F_LBL,
           fill=(160, 170, 190), anchor='mm')
grid.save(os.path.join(OUT, 'pages.png'))

# --- mockup device (hero) ---
hero = Image.new('RGB', (640, 400), (13, 16, 23))
d = ImageDraw.Draw(hero)
d.rounded_rectangle((88, 16, 552, 372), radius=20, fill=(22, 26, 34),
                    outline=(44, 49, 61), width=2)
d.rounded_rectangle((148, 46, 492, 314), radius=8, fill=(5, 7, 10),
                    outline=(35, 40, 51), width=2)
hero.paste(p_clock, (160, 60))
# 6 boutons a gauche de l'ecran (S3 allume)
for i in range(6):
    y = 70 + i * 38
    on = (i == 2)
    d.rounded_rectangle((110, y, 138, y + 28), radius=8,
                        fill=(70, 200, 120) if on else (34, 38, 47),
                        outline=(150, 240, 180) if on else (60, 66, 80))
# 2 molettes sous l'ecran
for cx in (176, 464):
    d.ellipse((cx - 20, 330, cx + 20, 370), fill=(28, 32, 40),
              outline=(58, 64, 79), width=2)
    d.line((cx, 336, cx, 348), fill=(120, 130, 150), width=3)
# boutons page haut/bas au centre-bas
d.rounded_rectangle((300, 332, 340, 344), radius=4, fill=(34, 38, 47))
d.rounded_rectangle((300, 356, 340, 368), radius=4, fill=(34, 38, 47))
d.text((320, 390), '320 x 240 - 6 buttons - 2 knobs - HTTP API :8199',
       font=F_CAP, fill=(120, 130, 150), anchor='mm')
hero.save(os.path.join(OUT, 'hero.png'))

print('OK ->', os.path.normpath(OUT))
for f in sorted(os.listdir(OUT)):
    print('  ', f)
