"""
gen_screens.py — Captures d'ecran du FIP pour la doc (docs/).
Rend les pages du daemon (framework fipui) exactement comme envoyees au device.
Genere : docs/pageN.png (x6), docs/pages.png (grille 3x2), docs/hero.png (mockup).
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

# --- etats vivants pour les captures ---
D.apps[3].state = 0x40 | 0x200 | 0x02           # tester : S2+S5 + molette droite
D.apps[4].scroll = 264                            # reader : milieu du texte
demo = D.apps[5]
demo.view = 1                                     # demo : liste defilante (items 2-7)
demo.states = {0: True, 1: False, 2: True, 3: False, 4: True, 6: True}

labels = ['1 - Clock', '2 - System', '3 - Network', '4 - Button tester',
          '5 - Reader (right knob)', '6 - fipui demo (menu + LEDs)']
pages = [D.render_page(i) for i in range(len(D.apps))]
for i, img in enumerate(pages, 1):
    img.save(os.path.join(OUT, f'page{i}.png'))

# --- grille 3x2 ---
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

# --- mockup device (hero) : ecran = horloge, S3 allume + LED 3 verte ---
hero = Image.new('RGB', (640, 400), (13, 16, 23))
d = ImageDraw.Draw(hero)
d.rounded_rectangle((88, 16, 552, 372), radius=20, fill=(22, 26, 34),
                    outline=(44, 49, 61), width=2)
d.rounded_rectangle((148, 46, 492, 314), radius=8, fill=(5, 7, 10),
                    outline=(35, 40, 51), width=2)
hero.paste(pages[0], (160, 60))
for i in range(6):
    y = 70 + i * 38
    on = (i == 2)
    d.rounded_rectangle((110, y, 138, y + 28), radius=8,
                        fill=(70, 200, 120) if on else (34, 38, 47),
                        outline=(150, 240, 180) if on else (60, 66, 80))
for cx in (176, 464):
    d.ellipse((cx - 20, 330, cx + 20, 370), fill=(28, 32, 40),
              outline=(58, 64, 79), width=2)
    d.line((cx, 336, cx, 348), fill=(120, 130, 150), width=3)
d.rounded_rectangle((300, 332, 340, 344), radius=4, fill=(34, 38, 47))
d.rounded_rectangle((300, 356, 340, 368), radius=4, fill=(34, 38, 47))
d.text((320, 390), '320x240 - 6 buttons + LEDs - 2 knobs - HTTP API :8199',
       font=font(15), fill=(120, 130, 150), anchor='mm')
hero.save(os.path.join(OUT, 'hero.png'))

print('OK ->', os.path.normpath(OUT))
for f in sorted(os.listdir(OUT)):
    print('  ', f)
