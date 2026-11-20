"""
charte.py — charte graphique partagée des apps fipd (thème sombre type
GitHub Dark). `import charte` depuis n'importe quelle app (repere du daemon).

Fournit : palette, cartes arrondies (panel), boutons "keycap" alignes sur
S1-S6, icones vectorielles PIL (ico_*), pastilles de statistiques (pill),
avatars lettres + utilitaires (ago, LANG_COLORS).
"""
import math

import fipui

# ---------- palette (GitHub Dark + accents) ----------
BG      = (13, 17, 23)     # fond general  #0d1117
CARD    = (22, 27, 34)     # carte        #161b22
CARD_HI = (33, 38, 45)     # carte active
BORDER  = (48, 54, 61)     # bordure      #30363d
FG      = (230, 237, 243)  # texte        #e6edf3
DIM     = (139, 148, 158)  # texte secondaire #8b949e
BLUE    = (88, 166, 255)   # accent       #58a6ff
GREEN   = (63, 185, 80)    # succes       #3fb950
RED     = (248, 81, 73)    # erreur       #f85149
PURPLE  = (188, 140, 255)  # pull requests #bc8cff
ORANGE  = (210, 153, 34)   # attention
TEAL    = (56, 178, 172)

LANG_COLORS = {            # couleurs officielles GitHub des langages
    'Python': (53, 114, 165), 'JavaScript': (241, 224, 90),
    'TypeScript': (49, 120, 198), 'Go': (0, 173, 216),
    'Rust': (222, 165, 132), 'C': (85, 85, 85), 'C++': (243, 75, 125),
    'Java': (176, 114, 25), 'HTML': (227, 76, 38), 'CSS': (86, 61, 124),
    'Shell': (137, 224, 81), 'C#': (178, 34, 52), 'Dockerfile': (56, 79, 125),
}


def lang_color(name):
    return LANG_COLORS.get(name or '', DIM)


def ago(iso):
    """'2026-11-19T23:56:07Z' -> '2 h' / '3 j' / '' (si illisible)."""
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat((iso or '').replace('Z', '+00:00'))
        s = (datetime.now(timezone.utc) - t).total_seconds()
        if s < 0:
            return ''
        if s < 3600:
            return '{} min'.format(max(1, int(s // 60)))
        if s < 86400:
            return '{} h'.format(int(s // 3600))
        return '{} j'.format(int(s // 86400))
    except Exception:
        return ''


# ---------- cartes & keycaps ----------

def panel(d, box, radius=8, fill=CARD, outline=BORDER, width=1):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline,
                        width=width)


def keycap(d, y, label, value='', icon=None, col=None, hot=False, h=17,
           x0=6, x1=300, tag='', fsize=13, r=5):
    """Rangee-bouton alignee sur un bouton physique (S1-S6).
    icon = callable(d, cx, cy, r, col). hot = etat actif (encadre colore).
    value = texte dim a droite du label ; tag = 'S1'.. en bout de rangee."""
    col = col or FG
    d.rounded_rectangle((x0, y, x1, y + h), radius=4,
                        fill=CARD_HI if hot else CARD,
                        outline=col if hot else BORDER, width=1)
    f = fipui.font(fsize)
    if icon:
        icon(d, x0 + 4 + r + 4, y + h // 2 + 1, r, col)
    tx = x0 + 2 * r + 14
    d.text((tx, y + h // 2 + 1), label, font=f, fill=col, anchor='lm')
    if value:
        d.text((tx + d.textlength(label, font=f) + 6, y + h // 2 + 1),
               value, font=f, fill=DIM, anchor='lm')
    if tag:
        d.text((x1 - 5, y + h // 2 + 1), tag, font=fipui.font(11),
               fill=(90, 95, 110), anchor='rm')


def pill(d, x, cy, icon, value, col=FG, h=17):
    """Pastille icone+valeur, retourne la largeur occuree."""
    f = fipui.font(13)
    tw = d.textlength(value, font=f)
    w = int(12 + tw + 10)
    d.rounded_rectangle((x, cy - h // 2, x + w, cy + h // 2), radius=h // 2,
                        fill=CARD, outline=BORDER)
    if icon:
        icon(d, x + 11, cy, 4, col)
    d.text((x + 21, cy), value, font=f, fill=col, anchor='lm')
    return w


# ---------- icones vectorielles (centrees, rayon r) ----------

def ico_star(d, cx, cy, r, col):
    pts = []
    for i in range(10):
        rr = r + 1 if i % 2 == 0 else (r + 1) * 0.45
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=col)


def ico_issue(d, cx, cy, r, col):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=col, width=2)
    d.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=col)


def ico_branch(d, cx, cy, r, col):
    d.line((cx - 2, cy - r, cx - 2, cy + r), fill=col, width=2)
    d.ellipse((cx - 5, cy - r - 3, cx + 1, cy + r - 3), fill=col)
    d.ellipse((cx - 5, cy + r - 3, cx + 1, cy + r + 3), fill=col)
    d.line((cx - 2, cy, cx + r - 1, cy), fill=col, width=2)
    d.ellipse((cx + r - 4, cy - 3, cx + r + 2, cy + 3), fill=col)


def ico_play(d, cx, cy, r, col):
    d.polygon([(cx - r + 1, cy - r), (cx + r, cy), (cx - r + 1, cy + r)],
              fill=col)


def ico_home(d, cx, cy, r, col):
    d.rounded_rectangle((cx - r + 1, cy - r, cx + r, cy + r), radius=2,
                        outline=col, width=2)
    d.line((cx - r + 4, cy + r - 3, cx + r - 3, cy + r - 3), fill=col, width=2)


def ico_link(d, cx, cy, r, col):
    d.rounded_rectangle((cx - r, cy - 3, cx + 1, cy + 3), radius=2,
                        outline=col, width=2)
    d.rounded_rectangle((cx - 1, cy - 3, cx + r, cy + 3), radius=2,
                        fill=CARD, outline=col, width=2)


def ico_terminal(d, cx, cy, r, col):
    d.rounded_rectangle((cx - r, cy - r + 1, cx + r, cy + r - 1), radius=2,
                        outline=col, width=2)
    d.line((cx - r + 3, cy - 3, cx - r + 6, cy), fill=col, width=1)
    d.line((cx - r + 3, cy + 1, cx - r + 6, cy - 3), fill=col, width=1)
    d.line((cx - r + 8, cy + 1, cx + r - 3, cy + 1), fill=col, width=1)


def ico_copy(d, cx, cy, r, col):
    d.rounded_rectangle((cx - r, cy - r + 2, cx + 1, cy + r), radius=2,
                        outline=col, width=2)
    d.rounded_rectangle((cx - 1, cy - r, cx + r, cy + r - 2), radius=2,
                        fill=CARD, outline=col, width=2)


def ico_x(d, cx, cy, r, col):
    d.line((cx - r, cy - r, cx + r, cy + r), fill=col, width=2)
    d.line((cx - r, cy + r, cx + r, cy - r), fill=col, width=2)


def ico_min(d, cx, cy, r, col):
    d.rounded_rectangle((cx - r, cy - r, cx + r, cy + r), radius=2,
                        outline=col, width=2)
    d.line((cx - r + 3, cy + 2, cx + r - 3, cy + 2), fill=col, width=2)


def ico_check(d, cx, cy, r, col):
    d.line((cx - r, cy, cx - 2, cy + r - 1), fill=col, width=2)
    d.line((cx - 2, cy + r - 1, cx + r, cy - r + 1), fill=col, width=2)


def ico_clock(d, cx, cy, r, col):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=col, width=2)
    d.line((cx, cy, cx, cy - r + 3), fill=col, width=2)
    d.line((cx, cy, cx + r - 3, cy + 2), fill=col, width=2)


def ico_pin(d, cx, cy, r, col):
    d.rounded_rectangle((cx - r, cy - r + 1, cx + r - 2, cy), radius=2,
                        outline=col, width=2)
    d.line((cx - 1, cy, cx - 1, cy + r), fill=col, width=2)


def ico_window(d, cx, cy, r, col):
    d.rounded_rectangle((cx - r, cy - r, cx + r, cy + r), radius=2,
                        outline=col, width=2)
    d.line((cx - r, cy - r + 4, cx + r, cy - r + 4), fill=col, width=2)


def ico_folder(d, cx, cy, r, col):
    d.polygon([(cx - r, cy - r + 3), (cx - r + 2, cy - r), (cx - 2, cy - r),
               (cx, cy - r + 3), (cx + r, cy - r + 3)], outline=col)
    d.rounded_rectangle((cx - r, cy - r + 2, cx + r, cy + r), radius=2,
                        outline=col, width=2)


def ico_code(d, cx, cy, r, col):
    d.line((cx - 2, cy - r + 1, cx - r + 2, cy), fill=col, width=2)
    d.line((cx - 2, cy, cx - r + 2, cy + r - 1), fill=col, width=2)
    d.line((cx + 2, cy - r + 1, cx + r - 2, cy), fill=col, width=2)
    d.line((cx + 2, cy, cx + r - 2, cy + r - 1), fill=col, width=2)


# ---------- avatars ----------

def _hash_color(name):
    """Couleur vive deterministe depuis un nom (teinte = hash, HSL->RGB)."""
    hh = (sum(ord(c) * (i + 3) for i, c in enumerate(name)) % 360) / 360.0
    s, l = 0.60, 0.42

    def f(n):
        k = (n + hh * 12) % 12
        a = s * min(l, 1 - l)
        return l - a * max(-1.0, min(k - 3, 9 - k, 1.0))

    return tuple(int(x * 255) for x in (f(0), f(8), f(4)))


def letter_avatar(name, size=44, radius=10):
    """Avatar PIL : lettre sur fond colore determine par le nom."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (size, size), CARD)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius,
                        fill=_hash_color(name))
    letter = (name[:1] or '?').upper()
    f = fipui.font(int(size * 0.52))
    d.text((size / 2, size / 2 - 1), letter, font=f, fill=(255, 255, 255),
           anchor='mm')
    return img


def rounded(img, radius=10):
    """Retourne l'image avec coins arrondis (canal alpha)."""
    from PIL import Image, ImageDraw
    mask = Image.new('L', img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1,
                                            img.size[1] - 1),
                                           radius=radius, fill=255)
    out = img.convert('RGBA')
    out.putalpha(mask)
    return out
