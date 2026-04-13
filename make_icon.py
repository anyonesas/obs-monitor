#!/usr/bin/env python3
"""Génère l'icône .icns pour l'app."""
import os, struct, zlib
from PIL import Image, ImageDraw

def make_icon(path_icns):
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    imgs = {}
    for s in sizes:
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        pad = s * 0.06
        # Fond sombre arrondi
        r = s * 0.22
        d.rounded_rectangle([pad, pad, s-pad, s-pad], radius=r,
                             fill=(20, 20, 35, 255))
        # Cercle rouge
        cr = s * 0.28
        cx, cy = s*0.5, s*0.48
        d.ellipse([cx-cr, cy-cr, cx+cr, cy+cr], fill=(220, 30, 50, 255))
        # Petite onde sonore stylisée
        lw = max(1, s//32)
        for i, amp in enumerate([0.12, 0.18, 0.12]):
            x = cx + (i-1)*s*0.14
            h = s * amp
            d.line([(x, cy-h), (x, cy+h)],
                   fill=(255, 230, 100, 220), width=lw)
        imgs[s] = img

    # Génère un iconset temporaire
    iconset = path_icns.replace(".icns", ".iconset")
    os.makedirs(iconset, exist_ok=True)

    mapping = {
        16:  ["icon_16x16.png"],
        32:  ["icon_16x16@2x.png", "icon_32x32.png"],
        64:  ["icon_32x32@2x.png"],
        128: ["icon_128x128.png"],
        256: ["icon_128x128@2x.png", "icon_256x256.png"],
        512: ["icon_256x256@2x.png", "icon_512x512.png"],
        1024:["icon_512x512@2x.png"],
    }
    for size, names in mapping.items():
        for name in names:
            imgs[size].save(os.path.join(iconset, name))

    os.system(f"iconutil -c icns '{iconset}' -o '{path_icns}'")
    import shutil; shutil.rmtree(iconset, ignore_errors=True)
    print(f"✓ Icône générée : {path_icns}")

if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "OBSMonitor.icns")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    make_icon(out)
