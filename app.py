#!/usr/bin/env python3
"""
OBS Monitor v2.0 — Native macOS NSPanel + rumps menu bar
Panneau flottant natif (AppKit NSPanel) + icône barre de menu (rumps).
"""

VERSION      = "2.4.1"
GITHUB_REPO  = "anyonesas/obs-monitor"
UPDATE_API   = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

import threading
import time
import json
import os
import math
import base64
import io
import sys
import urllib.request
import urllib.error
import datetime
import subprocess
import tempfile
import shutil
from collections import deque
from PIL import Image, ImageStat, ImageChops

try:
    import AppKit
    import Foundation
    from PyObjCTools import AppHelper
    HAVE_APPKIT = True
except ImportError:
    HAVE_APPKIT = False

try:
    import Quartz
    HAVE_QUARTZ = True
except ImportError:
    HAVE_QUARTZ = False

# API CoreGraphics privee — set window level directement par CGWindowID
# Contourne le probleme NSApp.windows() vide dans les apps PyInstaller bundlees
import ctypes as _ctypes
try:
    _CG  = _ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    _CG.CGSMainConnectionID.restype = _ctypes.c_uint
    _CG.CGSSetWindowLevel.argtypes  = [_ctypes.c_uint, _ctypes.c_uint, _ctypes.c_int]
    _CG.CGSSetWindowLevel.restype   = _ctypes.c_int
    # CGSOrderWindow : ordonnancement explicite (au-dessus d'une fenetre specifique)
    # Signature : CGSOrderWindow(connection, wid, mode, relativeToWid)
    # mode: 1 = kCGSOrderAbove, -1 = kCGSOrderBelow, 0 = kCGSOrderOut
    _CG.CGSOrderWindow.argtypes  = [_ctypes.c_uint, _ctypes.c_uint, _ctypes.c_int, _ctypes.c_uint]
    _CG.CGSOrderWindow.restype   = _ctypes.c_int
    _CGS_CONN = _CG.CGSMainConnectionID()
    HAVE_CGS  = (_CGS_CONN != 0)
except Exception:
    HAVE_CGS = False

def _cgs_set_level(wid: int, level: int) -> bool:
    """Fixe le window level via CoreGraphics (wid = CGWindowID)."""
    if not HAVE_CGS or not wid:
        return False
    try:
        return _CG.CGSSetWindowLevel(_CGS_CONN, wid, level) == 0
    except Exception:
        return False

def _cgs_order_above(our_wid: int, target_wid: int) -> bool:
    """
    Place notre fenetre EXPLICITEMENT au-dessus d'une fenetre cible via CGSOrderWindow.
    Ceci est different du window level : c'est un ordonnancement direct dans la pile.
    Fonctionne meme quand le compositeur Metal d'OBS reordonne les fenetres.
    """
    if not HAVE_CGS or not our_wid or not target_wid:
        return False
    try:
        # kCGSOrderAbove = 1
        return _CG.CGSOrderWindow(_CGS_CONN, our_wid, 1, target_wid) == 0
    except Exception:
        return False

try:
    import obsws_python as obs_ws
except ImportError:
    sys.exit("pip install obsws-python")

# ─────────────────────────────────────────────────────────────────────────────
# Config dans ~/.config/obsmonitor/ — persiste entre les mises a jour
CONFIG_DIR  = os.path.join(os.path.expanduser("~"), ".config", "obsmonitor")
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "obs": {"host": "localhost", "port": 4455, "password": ""},
    "checks": {
        "audio": {
            "silence_db": -50,
            "silence_duration_s": 3,
            "clip_db": -1,
            "flat_std_db": 2.5,
            "flat_min_db": -45,
            "flat_duration_s": 5,
            "clip_ratio": 0.4,
            "monitor_inputs": None
        },
        "video": {
            "freeze_threshold": 0.997,
            "freeze_duration_s": 3,
            "dark_threshold": 30,
            "bright_threshold": 242,
            "check_interval_s": 2,
            "monitor_sources": None
        }
    },
    "panel": {"x": None, "y": None},
    "banner": {"y": None},
    "sms": {
        "enabled": False,
        "api_key": "",
        "device": "",            # ex: "9210|0"
        "recipient": "",         # ex: "+33632548891"
        "cooldown_s": 600,       # 10 min entre SMS pour la même erreur
        "min_duration_s": 10,    # erreur doit durer 10s avant SMS
    }
}

def _bundled_config():
    """Config par défaut bundlée dans le .app (credentials pré-remplis)."""
    # En mode PyInstaller frozen, les resources sont dans sys._MEIPASS
    if getattr(sys, 'frozen', False):
        path = os.path.join(sys._MEIPASS, 'config.json')
    else:
        path = os.path.join(BASE_DIR, 'config.json')
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return DEFAULT_CONFIG

def load_config():
    if not os.path.exists(CONFIG_PATH):
        # Premier install : utiliser le config bundlé (credentials pré-remplis)
        base = _bundled_config()
        save_config(base)
        return base
    with open(CONFIG_PATH) as f:
        c = json.load(f)
    # Fusionne avec les defaults pour les cles manquantes (toutes sections)
    for section, vals in DEFAULT_CONFIG["checks"].items():
        for k, v in vals.items():
            c["checks"].setdefault(section, {}).setdefault(k, v)
    if "sms" not in c:
        c["sms"] = dict(DEFAULT_CONFIG["sms"])
    else:
        for k, v in DEFAULT_CONFIG["sms"].items():
            c["sms"].setdefault(k, v)
    return c

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def mul_to_db(v):
    return 20.0 * math.log10(max(v, 1e-10)) if v > 0 else -100.0


# ─────────────────────────────────────────────────────────────────────────────
# Couleurs
# ─────────────────────────────────────────────────────────────────────────────
BG      = "#16161e"
BG2     = "#1e1e2e"
BG3     = "#24243a"
ACCENT  = "#7aa2f7"
GREEN   = "#9ece6a"
RED     = "#f7768e"
ORANGE  = "#ff9e64"
YELLOW  = "#e0af68"
CYAN    = "#7dcfff"
FG      = "#c0caf5"
FG2     = "#565f89"
BORDER  = "#292e42"
ALERT_A = "#7a1520"
ALERT_B = "#b01a28"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers macOS natif
# ─────────────────────────────────────────────────────────────────────────────

LEVEL_PANEL  = 3      # NSFloatingWindowLevel — baseline
LEVEL_BANNER = 5      # Legerement au-dessus du panel

# Niveau maximum CoreGraphics (kCGMaximumWindowLevel)
LEVEL_MAX = 2147483630


def _get_obs_projector_window_ids() -> list:
    """
    Retourne la liste des CGWindowIDs de toutes les fenetres OBS Projector visibles.
    Utilise pour CGSOrderWindow (ordonnancement explicite au-dessus de ces fenetres).
    """
    if not HAVE_QUARTZ:
        return []
    try:
        wl = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll,
            Quartz.kCGNullWindowID
        )
        result = []
        for w in wl:
            owner = (w.get('kCGWindowOwnerName') or '').lower()
            name  = (w.get('kCGWindowName')      or '').lower()
            if 'obs' not in owner:
                continue
            # Projector ET fenetre principale OBS (qui peut aussi couvrir)
            if 'projector' not in name and 'obs' not in name:
                continue
            bounds = w.get('kCGWindowBounds') or {}
            area = float(bounds.get('Width', 0)) * float(bounds.get('Height', 0))
            if area < 10000:
                continue
            wid = w.get('kCGWindowNumber')
            if wid:
                result.append(int(wid))
        return result
    except Exception as e:
        print(f"[obs_wids] error: {e}")
        return []


def _get_obs_projector_level() -> int:
    """
    Retourne le kCGWindowLayer (window level) de la fenetre OBS Projector.
    Si aucune fenetre Projector n'est trouvee, retourne 0.
    Cette valeur est utilisee pour positionner notre panel EXACTEMENT au-dessus.
    """
    if not HAVE_QUARTZ:
        return 0
    try:
        wl = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll,
            Quartz.kCGNullWindowID
        )
        max_level = 0
        for w in wl:
            owner = (w.get('kCGWindowOwnerName') or '').lower()
            name  = (w.get('kCGWindowName')      or '').lower()
            if 'obs' not in owner:
                continue
            if 'projector' not in name:
                continue
            bounds = w.get('kCGWindowBounds') or {}
            area = float(bounds.get('Width', 0)) * float(bounds.get('Height', 0))
            if area < 10000:
                continue
            layer = int(w.get('kCGWindowLayer', 0))
            if layer > max_level:
                max_level = layer
                print(f"[obs_level] Projector '{w.get('kCGWindowName')}' layer={layer}")
        return max_level
    except Exception as e:
        print(f"[obs_level] error: {e}")
        return 0


def _get_our_window_ids():
    """
    Retourne tous les CGWindowIDs appartenant a ce processus via Quartz.
    C'est la methode la plus fiable — aucune dependance sur NSApp.windows()
    (qui est vide dans PyInstaller) ni sur winfo_id() (qui peut retourner 0).
    """
    if not HAVE_QUARTZ:
        return []
    try:
        pid = os.getpid()
        wl = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll,
            Quartz.kCGNullWindowID
        )
        return [int(w['kCGWindowNumber']) for w in wl
                if w.get('kCGWindowOwnerPID') == pid and w.get('kCGWindowNumber')]
    except Exception as e:
        print(f"[quartz] error listing windows: {e}")
        return []

def _ns_win_for_id(wid: int):
    """Retourne l'NSWindow pour un CGWindowID via PyObjC."""
    if not HAVE_APPKIT or not wid:
        return None
    try:
        return AppKit.NSWindow.windowWithWindowNumber_(wid)
    except Exception:
        return None

def boost_tk_windows(tk_wins_panel, tk_wins_banner, order_front=False):
    """Legacy — kept for compatibility but no longer used with NSPanel."""
    pass

def boost_all_windows(order_front=False, banner_wins=None):
    """Compatibilite anciens appels — no-op."""
    pass

def boost_window(tk_win, high=True):
    pass


def version_tuple(v):
    return tuple(int(x) for x in v.lstrip("v").split("."))

def check_for_update():
    """
    Verifie la derniere release GitHub via curl (SSL fiable sur macOS bundle).
    Retourne (version, dmg_url) ou (None, None).
    Loggue les erreurs pour faciliter le diagnostic.
    """
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "10",
             "-H", f"User-Agent: OBSMonitor/{VERSION}",
             UPDATE_API],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            print(f"[update] curl error code {result.returncode}: {result.stderr[:200]}")
            return None, None
        if not result.stdout.strip():
            print("[update] curl returned empty response")
            return None, None
        data = json.loads(result.stdout)
        # Verification de rate-limit GitHub
        if "message" in data:
            print(f"[update] GitHub API: {data['message']}")
            return None, None
        latest = data.get("tag_name", "").lstrip("v")
        print(f"[update] latest={latest!r}  current={VERSION!r}")
        if not latest:
            return None, None
        if version_tuple(latest) <= version_tuple(VERSION):
            print("[update] déjà à jour")
            return None, None
        for asset in data.get("assets", []):
            if asset["name"].endswith(".dmg"):
                return latest, asset["browser_download_url"]
        print("[update] aucun .dmg trouve dans les assets")
    except Exception as e:
        print(f"[update] exception: {e}")
    return None, None

def _real_app_path():
    """
    Retourne le chemin reel du .app, meme sous App Translocation (Gatekeeper).
    Si macOS a deplace l'app dans /private/var/folders/.../AppTranslocation/...,
    on installe dans /Applications/OBSMonitor.app a la place.
    """
    # Methode 1 : NSBundle donne le chemin du bundle en cours
    if HAVE_APPKIT:
        try:
            path = str(AppKit.NSBundle.mainBundle().bundlePath())
            if "AppTranslocation" not in path and "/var/folders" not in path:
                return path
        except Exception:
            pass
    # Methode 2 : chemin relatif a sys.executable
    candidate = os.path.abspath(
        os.path.join(os.path.dirname(sys.executable), "..", "..", "..")
    )
    if "AppTranslocation" in candidate or "/var/folders" in candidate:
        # App sous translocation → installe dans /Applications
        return "/Applications/OBSMonitor.app"
    return candidate

def install_update(dmg_url, app_path, on_progress=None):
    """
    Telecharge le DMG, monte, puis lance un script shell qui :
      - attend que l'app courante quitte (plus de verrou de fichier)
      - remplace le .app
      - relance la nouvelle version
    L'app quitte proprement avec sys.exit() pendant ce temps.
    """
    try:
        # 1. Telechargement
        tmp_dmg = os.path.join(tempfile.gettempdir(), "OBSMonitor_update.dmg")
        if on_progress: on_progress("Téléchargement...")
        subprocess.run(
            ["curl", "-L", "-o", tmp_dmg, "--max-time", "180", dmg_url],
            check=True
        )

        # 2. Montage
        if on_progress: on_progress("Installation...")
        mnt = os.path.join(tempfile.gettempdir(), "OBSMonitor_mnt")
        subprocess.run(["hdiutil", "attach", tmp_dmg,
                        "-mountpoint", mnt, "-quiet", "-nobrowse"],
                       check=True)

        src_app = os.path.join(mnt, "OBSMonitor.app")
        dst_app = app_path

        # 3. Script qui attend la fin de l'app puis remplace et relance
        pid = os.getpid()
        script = f"""#!/bin/bash
# Attend que l'app courante se ferme
while kill -0 {pid} 2>/dev/null; do sleep 0.3; done
sleep 0.5
# Remplace le .app (ditto preserve la signature de code)
rm -rf "{dst_app}"
ditto "{src_app}" "{dst_app}"
xattr -cr "{dst_app}"
# Nettoie
hdiutil detach "{mnt}" -quiet 2>/dev/null || true
rm -f "{tmp_dmg}"
# Relance la nouvelle version
open "{dst_app}"
"""
        script_path = os.path.join(tempfile.gettempdir(), "obs_monitor_update.sh")
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        subprocess.Popen(["bash", script_path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 4. Quitte l'app — le script prend le relais
        if on_progress: on_progress("Redémarrage...")
        time.sleep(0.8)
        os.kill(os.getpid(), 9)   # force quit propre

    except Exception as e:
        if on_progress: on_progress(f"Erreur : {e}")

def get_all_screens():
    """
    Retourne une liste de (x, y, width, height) en coordonnees tkinter-style
    (origine haut-gauche) pour chaque ecran physique connecte.
    """
    if not HAVE_APPKIT:
        return [(0, 0, 1920, 1080)]
    screens = []
    main_h = AppKit.NSScreen.mainScreen().frame().size.height
    for screen in AppKit.NSScreen.screens():
        f  = screen.frame()
        sx = int(f.origin.x)
        # Conversion coord macOS (origine bas-gauche) -> tkinter-style (origine haut-gauche)
        sy = int(main_h - f.origin.y - f.size.height)
        sw = int(f.size.width)
        sh = int(f.size.height)
        screens.append((sx, sy, sw, sh))
    return screens


def find_obs_projector_screen():
    """
    Cherche la fenetre OBS "Projector" via Quartz (toutes les fenetres, tous espaces).
    Retourne (sx, sy, sw, sh) de l'ecran ou elle se trouve, ou None.
    Fallback : si plusieurs ecrans mais Projector introuvable, retourne le 2e ecran.
    """
    screens = get_all_screens()
    if not HAVE_QUARTZ:
        return screens[1] if len(screens) > 1 else None
    try:
        # kCGWindowListOptionAll = toutes les fenetres, meme celles en arriere-plan / autres spaces
        wl = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll,
            Quartz.kCGNullWindowID
        )
        best = None
        best_area = 0
        for w in wl:
            owner = (w.get('kCGWindowOwnerName') or '').lower()
            name  = (w.get('kCGWindowName')      or '').lower()
            # Filtre : fenetre OBS contenant "projector"
            if 'obs' not in owner:
                continue
            if 'projector' not in name:
                continue
            bounds = w.get('kCGWindowBounds') or {}
            wx = float(bounds.get('X', 0))
            wy = float(bounds.get('Y', 0))   # CG coords : origine haut-gauche de l'ecran principal
            ww = float(bounds.get('Width', 0))
            wh = float(bounds.get('Height', 0))
            area = ww * wh
            if area < 10000:   # ignore les mini-fenetres
                continue
            # Garde la plus grande (la vraie fenetre Projector)
            if area <= best_area:
                continue
            best_area = area
            cx = wx + ww / 2
            cy = wy + wh / 2   # CG et tkinter ont la meme origine (haut-gauche)
            # Trouve l'ecran correspondant
            for (sx, sy, sw, sh) in screens:
                if sx <= cx < sx + sw and sy <= cy < sy + sh:
                    best = (sx, sy, sw, sh)
                    break
        if best:
            return best
    except Exception as e:
        print(f"[projector_screen] {e}")
    # Fallback : 2e ecran si disponible (configuration typique streaming)
    return screens[1] if len(screens) > 1 else None


# ─────────────────────────────────────────────────────────────────────────────
# Audio Monitor — avec buffer pour analyse de variance
# ─────────────────────────────────────────────────────────────────────────────

class AudioMonitor:
    BUFFER_SIZE = 70   # ~7s a ~10 updates/s

    def __init__(self, cfg):
        self.cfg   = cfg
        self._lock = threading.Lock()
        self._inputs = {}
        self._flat_since = {}  # {name: timestamp depuis lequel le son est plat}
        # {name: {peak_db, last_sound_t, buf: deque[float]}}

    def on_volume_meters(self, data):
        now = time.time()
        for inp in data.inputs:
            name   = inp["inputName"]
            levels = inp["inputLevelsMul"]
            if not levels:
                continue
            peak = max(ch[1] for ch in levels)
            db   = mul_to_db(peak)
            with self._lock:
                if name not in self._inputs:
                    self._inputs[name] = {
                        "peak_db": db,
                        "last_sound_t": now,
                        "buf": deque(maxlen=self.BUFFER_SIZE),
                    }
                e = self._inputs[name]
                e["peak_db"] = db
                e["buf"].append(db)
                if db > self.cfg["silence_db"]:
                    e["last_sound_t"] = now

    def seed_inputs(self, names):
        """Ajoute des inputs connus avant de recevoir des events de volume."""
        now = time.time()
        with self._lock:
            for name in names:
                if name not in self._inputs:
                    self._inputs[name] = {
                        "peak_db": -100.0,
                        "last_sound_t": now,   # pas d'alerte silence immediate
                        "buf": deque(maxlen=self.BUFFER_SIZE),
                    }

    def known_inputs(self):
        with self._lock:
            return list(self._inputs.keys())

    def issues(self):
        now     = time.time()
        monitor = self.cfg.get("monitor_inputs", None)
        out     = []

        silence_thresh = self.cfg["silence_db"]
        silence_dur    = self.cfg["silence_duration_s"]
        clip_db        = self.cfg.get("clip_db", -1)
        flat_std       = self.cfg.get("flat_std_db", 2.5)
        flat_min       = self.cfg.get("flat_min_db", -45)
        flat_dur       = self.cfg.get("flat_duration_s", 5)
        clip_ratio_thr = self.cfg.get("clip_ratio", 0.4)

        with self._lock:
            for name, e in self._inputs.items():
                # monitor=None → tout surveiller, monitor=[] → rien
                if monitor is not None and name not in monitor:
                    continue

                db      = e["peak_db"]
                silence = now - e["last_sound_t"]
                buf     = list(e["buf"])

                # Silence prolonge
                if silence >= silence_dur:
                    out.append(
                        f"\U0001f3a4  \u00ab {name} \u00bb  silence depuis {silence:.0f}s"
                        f"  \u2014 micro déconnecté ou muet ?"
                    )
                    continue   # inutile de verifier le reste

                if len(buf) < 20:
                    continue   # pas assez de donnees

                mean = sum(buf) / len(buf)
                std  = (sum((v - mean) ** 2 for v in buf) / len(buf)) ** 0.5

                # Son trop constant (bourdonnement, micro bloqué)
                # Doit rester plat pendant flat_dur secondes d'affilée pour déclencher
                if mean > flat_min and std < flat_std:
                    self._flat_since.setdefault(name, now)
                    flat_for = now - self._flat_since[name]
                    if flat_for >= flat_dur:
                        out.append(
                            f"\U0001f41d  \u00ab {name} \u00bb  son trop constant depuis {flat_for:.0f}s (variation {std:.1f} dB)"
                            f"  \u2014 bourdonnement / micro bloqué ?"
                        )
                else:
                    self._flat_since.pop(name, None)

                # Saturation chronique (trop longtemps dans le rouge)
                clip_count = sum(1 for v in buf if v >= clip_db)
                ratio      = clip_count / len(buf)
                if ratio >= clip_ratio_thr:
                    out.append(
                        f"\U0001f534  \u00ab {name} \u00bb  saturé {ratio*100:.0f}% du temps"
                        f"  \u2014 baisser le gain du micro"
                    )
                elif db >= clip_db:
                    # Écrêtage ponctuel (non chronique)
                    out.append(f"\U0001f50a  \u00ab {name} \u00bb  écrêtage ponctuel ({db:.1f} dB)")

        return out


# ─────────────────────────────────────────────────────────────────────────────
# Video Monitor
# ─────────────────────────────────────────────────────────────────────────────

class VideoMonitor:
    def __init__(self, cfg, get_client):
        self.cfg         = cfg
        self._get_client = get_client
        self._lock       = threading.Lock()
        self._issues_buf = []
        self._prev_frames  = {}
        self._freeze_since = {}
        self._known        = []

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self._check()
            except Exception:
                pass
            time.sleep(self.cfg["check_interval_s"])

    def _check(self):
        client = self._get_client()
        if not client:
            with self._lock:
                self._issues_buf = []
                self._known = []
            return
        try:
            scene = client.get_current_program_scene().current_program_scene_name
            items = client.get_scene_item_list(scene).scene_items
        except Exception:
            return

        monitor    = self.cfg.get("monitor_sources", None)
        found      = []
        new_issues = []

        for item in items:
            if not item.get("sceneItemEnabled", True):
                continue
            src = item.get("sourceName", "")
            if not src:
                continue
            found.append(src)
            # monitor=None → pas encore configuré → tout surveiller
            # monitor=[]   → tout décoché → rien surveiller
            # monitor=[..] → surveiller uniquement les sources cochées
            if monitor is not None and src not in monitor:
                continue

            img = self._capture(client, src)
            if img is None:
                continue

            gray = img.convert("L")
            stat = ImageStat.Stat(gray)
            br   = stat.mean[0]   # luminosite moyenne 0-255
            std  = stat.stddev[0] # ecart-type : faible = image uniforme (noire ou blanche)

            dark_thr   = self.cfg["dark_threshold"]    # 30
            bright_thr = self.cfg["bright_threshold"]  # 242

            # Image trop sombre (noire ou tres sombre + peu de variation)
            if br < dark_thr:
                new_issues.append(
                    f"\U0001f4f7  \u00ab {src} \u00bb  image trop sombre (luminosité {br:.0f}/255)"
                    f"  \u2014 lumières éteintes ou caméra déconnectée ?"
                )
            # Image trop uniforme même si pas noire : capteur bloqué sur une couleur
            elif std < 4 and br < 60:
                new_issues.append(
                    f"\U0001f4f7  \u00ab {src} \u00bb  image anormalement uniforme"
                    f"  \u2014 caméra bloquée ?"
                )
            elif br > bright_thr:
                new_issues.append(
                    f"\U0001f4a1  \u00ab {src} \u00bb  surexposée (luminosité {br:.0f}/255)"
                    f"  \u2014 éclairage trop fort ?"
                )

            fi = self._freeze(src, img, br)
            if fi:
                new_issues.append(fi)

        with self._lock:
            self._issues_buf = new_issues
            self._known      = found

    def _capture(self, client, name):
        for fmt in ("jpg", "png"):
            try:
                resp = client.get_source_screenshot(
                    name=name, img_format=fmt,
                    width=320, height=180, quality=75
                )
                raw = resp.image_data
                b64 = raw.split(",", 1)[1] if "," in raw else raw
                return Image.open(io.BytesIO(base64.b64decode(b64)))
            except Exception:
                continue
        return None

    def _freeze(self, src, img, brightness=128):
        now = time.time()
        with self._lock:
            prev = self._prev_frames.get(src)

        if prev:
            _, pimg = prev
            a   = pimg.convert("L").resize((80, 45))
            b   = img.convert("L").resize((80, 45))
            diff = ImageChops.difference(a, b)
            rms  = ImageStat.Stat(diff).rms[0]
            # max_diff : le pixel qui a le plus change (0 = aucun pixel n'a bouge)
            max_diff = diff.getextrema()[1]
            sim  = 1.0 - rms / 128.0

            # Pour les images sombres, le bruit capteur cause des variations
            # de pixels même sur une scène statique → seuil max_diff plus souple
            if brightness < self.cfg.get("dark_threshold", 30):
                max_diff_limit = 30   # images sombres : bruit capteur important
                sim_threshold  = 0.990
            else:
                max_diff_limit = 15   # images normales
                sim_threshold  = self.cfg["freeze_threshold"]

            # Vrai freeze = similarité très haute ET aucun pixel n'a vraiment bougé
            # Scène calme naturelle = sim élevée mais quelques pixels varient (bruit, respiration)
            is_frozen = (sim >= sim_threshold) and (max_diff < max_diff_limit)

            if is_frozen:
                with self._lock:
                    self._freeze_since.setdefault(src, now)
                    frozen = now - self._freeze_since[src]
                if frozen >= self.cfg["freeze_duration_s"]:
                    return (
                        f"\U0001f9ca  \u00ab {src} \u00bb  figée depuis {frozen:.0f}s"
                        f"  \u2014 caméra plantée ?"
                    )
                return None
            else:
                with self._lock:
                    self._freeze_since.pop(src, None)

        with self._lock:
            self._prev_frames[src] = (now, img)
        return None

    def known_sources(self):
        with self._lock:
            return list(self._known)

    def issues(self):
        with self._lock:
            return list(self._issues_buf)


# ─────────────────────────────────────────────────────────────────────────────
# SMSNotifier — envoie des SMS via sms8.io quand des erreurs persistent
# ─────────────────────────────────────────────────────────────────────────────

import re as _re
import urllib.parse as _urlparse

class SMSNotifier:
    """Envoie des SMS via l'API sms8.io pour chaque erreur persistante.

    Logique anti-spam :
      - Une erreur doit durer >= min_duration_s avant déclenchement
      - Cooldown de cooldown_s entre 2 SMS pour la MÊME erreur (clé = type+source)
      - Quand l'erreur disparaît, son état est nettoyé pour repartir à zéro
    """

    API_URL = "https://app.sms8.io/services/send.php"

    def __init__(self, cfg):
        self.cfg = cfg
        self._first_seen = {}   # {key: timestamp}
        self._last_sent  = {}   # {key: timestamp}
        self._lock = threading.Lock()

    @staticmethod
    def _issue_key(text):
        """Clé stable pour identifier le type d'erreur (emoji + nom de source)."""
        m = _re.search(r"\u00ab\s*([^\u00bb]+?)\s*\u00bb", text)
        src = m.group(1) if m else ""
        emoji = text.split(" ", 1)[0] if text else ""
        return f"{emoji}|{src}"

    def process(self, current_issues):
        """Appelé à chaque tick avec la liste des issues actuelles."""
        if not self.cfg.get("enabled", False):
            return
        if not self.cfg.get("api_key") or not self.cfg.get("recipient"):
            return

        now = time.time()
        cooldown = self.cfg.get("cooldown_s", 600)
        min_dur  = self.cfg.get("min_duration_s", 10)

        current_keys = set()
        with self._lock:
            for text in current_issues:
                key = self._issue_key(text)
                current_keys.add(key)
                self._first_seen.setdefault(key, now)
                first_t = self._first_seen[key]
                last_t  = self._last_sent.get(key, 0)
                if (now - first_t) >= min_dur and (now - last_t) >= cooldown:
                    self._last_sent[key] = now
                    self._send_async(text)

            # Nettoyer les erreurs qui ne sont plus actives
            for key in list(self._first_seen.keys()):
                if key not in current_keys:
                    self._first_seen.pop(key, None)

    def notify_event(self, key, message):
        """Envoie un SMS one-shot pour un évènement (ex: déconnexion OBS).

        Pas de durée minimale, mais cooldown respecté.
        """
        if not self.cfg.get("enabled", False):
            return
        if not self.cfg.get("api_key") or not self.cfg.get("recipient"):
            return
        now = time.time()
        cooldown = self.cfg.get("cooldown_s", 600)
        with self._lock:
            last_t = self._last_sent.get(key, 0)
            if (now - last_t) < cooldown:
                return
            self._last_sent[key] = now
        self._send_async(message)

    def _send_async(self, message):
        threading.Thread(target=self._send, args=(message,), daemon=True).start()

    def _send(self, message):
        try:
            device = self.cfg.get("device", "")
            params = {
                "key":     self.cfg["api_key"],
                "number":  self.cfg["recipient"],
                "message": message,
                "devices": json.dumps([device]) if device else "[]",
                "type":    "sms",
                "prioritize": "0",
            }
            url = self.API_URL + "?" + _urlparse.urlencode(params)
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "curl/8.7.1")
            req.add_header("Accept", "*/*")
            # SSL context sans vérif : Python bundlé PyInstaller n'a pas les CA macOS
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                body = r.read().decode("utf-8", errors="replace")[:200]
                print(f"[sms] {r.status} → {message[:60]}  | {body[:80]}")
        except Exception as e:
            print(f"[sms] erreur envoi: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Helper: hex color to NSColor
# ─────────────────────────────────────────────────────────────────────────────

def _hex_to_nscolor(hex_str):
    """Convert '#RRGGBB' to AppKit.NSColor."""
    h = hex_str.lstrip('#')
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Flipped NSView + ObjC button target helper
# ─────────────────────────────────────────────────────────────────────────────

class _FlippedView(AppKit.NSView):
    """NSView subclass with flipped coordinate system (origin top-left)."""
    def isFlipped(self):
        return True



# ─────────────────────────────────────────────────────────────────────────────
# Native NSPanel — floating panel above OBS Projector
# ─────────────────────────────────────────────────────────────────────────────

class NativePanel:
    """
    Floating NSPanel using AppKit. Uses NSWindowStyleMaskNonactivatingPanel
    so it can appear above OBS Projector (Metal rendering).
    Contains: status, source selection checkboxes, monitoring info, issues list.
    """
    W = 300
    PANEL_H = 650

    def __init__(self):
        self._panel = None
        self._text_view = None
        self._status_field = None
        self._update_field = None
        self._info_field = None
        self._lock = threading.Lock()
        self._built = False
        # Source checkboxes
        self._audio_cbs = []   # [(name, NSButton), ...]
        self._video_cbs = []
        self._dynamic_views = []  # all views below fixed header — rebuilt on source change
        self._save_callback = None
        self._last_audio_names = []
        self._last_video_names = []
        self._header_end_y = 0  # Y position after fixed header

    def build(self):
        """Must be called on the main thread (inside rumps/AppKit run loop)."""
        if self._built:
            return
        self._built = True

        screens = AppKit.NSScreen.screens()
        if not screens:
            return
        main_frame = screens[0].frame()
        screen_w = main_frame.size.width
        screen_h = main_frame.size.height

        # Load saved position or default to top-right
        cfg = load_config()
        px = cfg.get("panel", {}).get("x")
        py = cfg.get("panel", {}).get("y")

        if px is not None and py is not None:
            ns_x = int(px)
            ns_y = int(screen_h - int(py) - self.PANEL_H)
        else:
            ns_x = int(screen_w - self.W - 20)
            ns_y = int(screen_h - 60 - self.PANEL_H)

        # Style mask: titled + closable + resizable + utility + non-activating panel
        style = (
            AppKit.NSWindowStyleMaskTitled |
            AppKit.NSWindowStyleMaskClosable |
            AppKit.NSWindowStyleMaskResizable |
            AppKit.NSWindowStyleMaskUtilityWindow |
            AppKit.NSWindowStyleMaskNonactivatingPanel
        )

        rect = Foundation.NSMakeRect(ns_x, ns_y, self.W, self.PANEL_H)
        self._panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False,
        )

        self._panel.setTitle_(f"OBS Monitor v{VERSION}")
        self._panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self._panel.setHidesOnDeactivate_(False)
        self._panel.setFloatingPanel_(True)
        self._panel.setBecomesKeyOnlyIfNeeded_(True)

        behavior = (
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
            AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._panel.setCollectionBehavior_(behavior)

        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(_hex_to_nscolor(BG))
        self._panel.setAlphaValue_(0.97)
        self._panel.setMinSize_(Foundation.NSMakeSize(self.W, 300))
        self._panel.setSharingType_(1)  # NSWindowSharingReadOnly

        # ── Main scroll view wrapping all content ──
        content = self._panel.contentView()
        cf = content.frame()
        cw, ch = cf.size.width, cf.size.height

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, cw, ch)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
        )
        scroll.setDrawsBackground_(True)
        scroll.setBackgroundColor_(_hex_to_nscolor(BG))

        # Flipped document view — origin at top-left, Y grows downward
        doc = _FlippedView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, cw, 1200)
        )
        doc.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        scroll.setDocumentView_(doc)
        content.addSubview_(scroll)
        self._scroll = scroll
        self._doc = doc
        self._doc_width = cw

        self._build_content(doc, cw)

        self._panel.orderFrontRegardless()
        self._boost_above_obs()

    # ── Build all subviews inside the document view ──

    def _build_content(self, doc, cw):
        """Build fixed header elements. Dynamic content is built by _rebuild_dynamic."""
        y = 8

        # ── Status ──
        self._status_field = self._make_label(
            doc, 12, y, cw - 24, 18,
            "Connexion à OBS…", ORANGE, 12, bold=False
        )
        y += 24

        # ── Update notification (hidden) ──
        self._update_field = self._make_label(
            doc, 12, y, cw - 24, 16,
            "", GREEN, 11, bold=True
        )
        self._update_field.setHidden_(True)
        y += 20

        # ── Separator ──
        y = self._add_separator(doc, y, cw)

        self._header_end_y = y

        # Build initial dynamic content (placeholders)
        self._rebuild_dynamic([], [], None)

    def _rebuild_dynamic(self, audio_names, video_names, cfg):
        """Rebuild all content below the fixed header (sources, info, alerts)."""
        doc = self._doc
        cw = self._doc_width

        # Remove all previous dynamic views
        for v in self._dynamic_views:
            try:
                v.removeFromSuperview()
            except Exception:
                pass
        for _, cb in self._audio_cbs:
            try:
                cb.removeFromSuperview()
            except Exception:
                pass
        for _, cb in self._video_cbs:
            try:
                cb.removeFromSuperview()
            except Exception:
                pass
        self._dynamic_views = []
        self._audio_cbs = []
        self._video_cbs = []

        y = self._header_end_y

        # ── SOURCES AUDIO ──
        self._dynamic_views.append(
            self._make_label(doc, 12, y, cw - 24, 16, "SOURCES AUDIO", ACCENT, 10, bold=True)
        )
        y += 20

        monitored_audio = None  # None = tout coché, set() = rien coché
        monitored_video = None
        if cfg:
            raw_a = cfg["checks"]["audio"].get("monitor_inputs", None)
            raw_v = cfg["checks"]["video"].get("monitor_sources", None)
            monitored_audio = set(raw_a) if raw_a is not None else None
            monitored_video = set(raw_v) if raw_v is not None else None

        if audio_names:
            for name in audio_names:
                checked = (monitored_audio is None) or (name in monitored_audio)
                cb = self._make_checkbox(name, checked, y, cw)
                doc.addSubview_(cb)
                self._audio_cbs.append((name, cb))
                y += 20
        else:
            lbl = self._make_label(doc, 20, y, cw - 32, 16,
                                   "En attente de connexion…", FG2, 10, bold=False)
            self._dynamic_views.append(lbl)
            y += 20
        y += 6

        # ── SOURCES VIDÉO ──
        self._dynamic_views.append(
            self._make_label(doc, 12, y, cw - 24, 16, "SOURCES VIDÉO", ACCENT, 10, bold=True)
        )
        y += 20

        if video_names:
            for name in video_names:
                checked = (monitored_video is None) or (name in monitored_video)
                cb = self._make_checkbox(name, checked, y, cw)
                doc.addSubview_(cb)
                self._video_cbs.append((name, cb))
                y += 20
        else:
            lbl = self._make_label(doc, 20, y, cw - 32, 16,
                                   "En attente de connexion…", FG2, 10, bold=False)
            self._dynamic_views.append(lbl)
            y += 20
        y += 4

        # ── Save hint ──
        self._dynamic_views.append(
            self._make_label(doc, 12, y, cw - 24, 14,
                             "✓ La sélection se met à jour automatiquement", FG2, 9, bold=False)
        )
        y += 20

        # ── Separator ──
        y = self._add_separator_dyn(doc, y, cw)

        # ── CE QUI EST SURVEILLÉ ──
        self._dynamic_views.append(
            self._make_label(doc, 12, y, cw - 24, 16, "CE QUI EST SURVEILLÉ", CYAN, 10, bold=True)
        )
        y += 20

        self._info_field = AppKit.NSTextView.alloc().initWithFrame_(
            Foundation.NSMakeRect(12, y, cw - 24, 60)
        )
        self._info_field.setEditable_(False)
        self._info_field.setSelectable_(False)
        self._info_field.setRichText_(True)
        self._info_field.setDrawsBackground_(False)
        self._info_field.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        self._info_field.setTextColor_(_hex_to_nscolor(FG2))
        doc.addSubview_(self._info_field)
        self._dynamic_views.append(self._info_field)
        y += 66

        # ── Separator ──
        y = self._add_separator_dyn(doc, y, cw)

        # ── ALERTES ──
        self._dynamic_views.append(
            self._make_label(doc, 12, y, cw - 24, 16, "ALERTES", RED, 10, bold=True)
        )
        y += 20

        self._text_view = AppKit.NSTextView.alloc().initWithFrame_(
            Foundation.NSMakeRect(8, y, cw - 16, 250)
        )
        self._text_view.setEditable_(False)
        self._text_view.setSelectable_(True)
        self._text_view.setRichText_(True)
        self._text_view.setDrawsBackground_(False)
        self._text_view.setTextContainerInset_(Foundation.NSMakeSize(4, 4))
        self._text_view.textContainer().setWidthTracksTextView_(True)
        self._text_view.setHorizontallyResizable_(False)
        doc.addSubview_(self._text_view)
        self._dynamic_views.append(self._text_view)
        y += 256

        doc.setFrameSize_(Foundation.NSMakeSize(cw, max(y + 10, 600)))

    # ── Helper: create a label ──

    def _make_label(self, parent, x, y, w, h, text, color, size, bold=False):
        lbl = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(x, y, w, h)
        )
        lbl.setStringValue_(text)
        lbl.setTextColor_(_hex_to_nscolor(color))
        lbl.setBackgroundColor_(AppKit.NSColor.clearColor())
        if bold:
            lbl.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size))
        else:
            lbl.setFont_(AppKit.NSFont.systemFontOfSize_(size))
        lbl.setBezeled_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        lbl.setDrawsBackground_(False)
        parent.addSubview_(lbl)
        return lbl

    def _make_checkbox(self, name, checked, y, cw):
        """Create a styled NSButton checkbox."""
        cb = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(16, y, cw - 32, 18)
        )
        cb.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb.setTitle_(name)
        cb.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        cb.setState_(AppKit.NSControlStateValueOn if checked else AppKit.NSControlStateValueOff)
        cell = cb.cell()
        if cell and hasattr(cell, 'setAttributedTitle_'):
            attrs = {
                AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(FG),
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11),
            }
            astr = Foundation.NSAttributedString.alloc().initWithString_attributes_(name, attrs)
            cell.setAttributedTitle_(astr)
        return cb

    def _add_separator(self, parent, y, cw):
        sep = AppKit.NSBox.alloc().initWithFrame_(
            Foundation.NSMakeRect(8, y + 4, cw - 16, 1)
        )
        sep.setBoxType_(AppKit.NSBoxSeparator)
        parent.addSubview_(sep)
        return y + 12

    def _add_separator_dyn(self, parent, y, cw):
        """Add separator and track it in dynamic views."""
        sep = AppKit.NSBox.alloc().initWithFrame_(
            Foundation.NSMakeRect(8, y + 4, cw - 16, 1)
        )
        sep.setBoxType_(AppKit.NSBoxSeparator)
        parent.addSubview_(sep)
        self._dynamic_views.append(sep)
        return y + 12

    # ── Source checkboxes (dynamic) ──

    def refresh_sources(self, audio_names, video_names, cfg):
        """Rebuild source checkboxes and all dynamic content when sources change."""
        if not self._doc:
            return
        if audio_names == self._last_audio_names and video_names == self._last_video_names:
            return  # no change
        self._last_audio_names = list(audio_names)
        self._last_video_names = list(video_names)
        self._rebuild_dynamic(audio_names, video_names, cfg)

    def get_selected_sources(self):
        """Return (audio_names, video_names) of checked sources."""
        audio = [name for name, cb in self._audio_cbs
                 if cb.state() == AppKit.NSControlStateValueOn]
        video = [name for name, cb in self._video_cbs
                 if cb.state() == AppKit.NSControlStateValueOn]
        return audio, video

    def set_save_callback(self, callback):
        """Store callback — triggered via rumps menu, not panel button."""
        self._save_callback = callback

    # ── Boost above OBS ──

    def _boost_above_obs(self):
        """Set our panel level above OBS Projector and use CGSOrderWindow."""
        if not self._panel:
            return
        obs_level = _get_obs_projector_level()
        obs_wids = _get_obs_projector_window_ids()
        dyn_level = max(LEVEL_PANEL, obs_level + 1)
        self._panel.setLevel_(dyn_level)

        wid = self._panel.windowNumber()
        if HAVE_CGS and wid and obs_wids:
            _cgs_set_level(wid, dyn_level)
            for obs_wid in obs_wids:
                _cgs_order_above(wid, obs_wid)

    def show(self):
        if self._panel:
            self._panel.orderFrontRegardless()

    def hide(self):
        if self._panel:
            self._panel.orderOut_(None)

    def is_visible(self):
        return self._panel.isVisible() if self._panel else False

    def update_status(self, connected):
        if not self._status_field:
            return
        try:
            if connected:
                self._status_field.setStringValue_("\u25cf  Connecté à OBS")
                self._status_field.setTextColor_(_hex_to_nscolor(GREEN))
            else:
                self._status_field.setStringValue_("\u25cf  Connexion à OBS…")
                self._status_field.setTextColor_(_hex_to_nscolor(ORANGE))
        except Exception as e:
            print(f"[panel.status] {e}")

    def update_info(self, audio_names, video_names, cfg):
        """Update the 'CE QUI EST SURVEILLÉ' info section."""
        if not self._info_field:
            return
        try:
            acfg = cfg["checks"]["audio"]
            vcfg = cfg["checks"]["video"]
            mon_a = acfg.get("monitor_inputs", None)
            mon_v = vcfg.get("monitor_sources", None)
            a_str = "(toutes)" if mon_a is None else (", ".join(mon_a) if mon_a else "(aucune)")
            v_str = "(toutes)" if mon_v is None else (", ".join(mon_v) if mon_v else "(aucune)")

            lines = [
                f"Audio : {a_str}",
                f"Vidéo : {v_str}",
                f"Seuils : silence {acfg['silence_db']}dB / {acfg['silence_duration_s']}s",
                f"         gel {vcfg['freeze_duration_s']}s, sombre <{vcfg['dark_threshold']}",
            ]
            text = "\n".join(lines)

            storage = self._info_field.textStorage()
            storage.beginEditing()
            rng = Foundation.NSMakeRange(0, storage.length())
            storage.deleteCharactersInRange_(rng)
            attrs = {
                AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(FG2),
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(10),
            }
            astr = Foundation.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            storage.appendAttributedString_(astr)
            storage.endEditing()
        except Exception as e:
            print(f"[panel.info] {e}")

    def update_issues(self, issues):
        """Update the issue list in the text view."""
        if not self._text_view:
            return
        try:
            storage = self._text_view.textStorage()
            storage.beginEditing()
            full_range = Foundation.NSMakeRange(0, storage.length())
            storage.deleteCharactersInRange_(full_range)

            if not issues:
                attrs = {
                    AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(GREEN),
                    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(12),
                }
                ok_str = Foundation.NSAttributedString.alloc().initWithString_attributes_(
                    "\u2705  Aucun problème détecté\n", attrs
                )
                storage.appendAttributedString_(ok_str)
            else:
                for i, issue in enumerate(issues):
                    attrs = {
                        AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(RED),
                        AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11),
                    }
                    line = issue + "\n"
                    if i < len(issues) - 1:
                        line += "\n"
                    attr_str = Foundation.NSAttributedString.alloc().initWithString_attributes_(
                        line, attrs
                    )
                    storage.appendAttributedString_(attr_str)

            storage.endEditing()
        except Exception as e:
            print(f"[panel.issues] {e}")

    def notify_update(self, version, url):
        if not self._update_field:
            return
        try:
            self._update_field.setStringValue_(f"\U0001f504  v{version} disponible")
            self._update_field.setHidden_(False)
        except Exception as e:
            print(f"[panel.update] {e}")

    def save_position(self, cfg):
        if not self._panel:
            return
        try:
            frame = self._panel.frame()
            screens = AppKit.NSScreen.screens()
            if not screens:
                return
            screen_h = screens[0].frame().size.height
            tk_x = int(frame.origin.x)
            tk_y = int(screen_h - frame.origin.y - frame.size.height)
            cfg.setdefault("panel", {})["x"] = tk_x
            cfg.setdefault("panel", {})["y"] = tk_y
        except Exception as e:
            print(f"[panel.save_pos] {e}")

    def periodic_boost(self):
        if self._panel and self._panel.isVisible():
            self._boost_above_obs()
            self._panel.orderFrontRegardless()


# ─────────────────────────────────────────────────────────────────────────────
# Native Alert Banner — red flashing bar across top of screen
# ─────────────────────────────────────────────────────────────────────────────

class _SnoozeTarget(Foundation.NSObject):
    """Cible ObjC pour les boutons snooze de la bannière."""
    _callback = None

    def clicked_(self, sender):
        if self._callback:
            self._callback()


class NativeBanner:
    """
    Plein écran, semi-transparent, rouge clignotant.
    Affiche "APPELER MEMBRE DE L'ÉQUIPE" en grand + le détail des alertes.
    4 boutons snooze pour ignorer temporairement.
    """

    def __init__(self):
        self._panel       = None
        self._lbl_cta     = None   # "APPELER MEMBRE DE L'ÉQUIPE"
        self._lbl_det     = None   # détail des alertes
        self._built       = False
        self._visible     = False
        self._snooze_until = 0.0   # timestamp jusqu'auquel la bannière est muette
        self._snooze_targets = []  # garder les targets en vie (évite GC)

    def build(self):
        if self._built:
            return
        self._built = True

        screens = AppKit.NSScreen.screens()
        if not screens:
            return
        main = screens[0].frame()
        sw = main.size.width
        sh = main.size.height

        style = (
            AppKit.NSWindowStyleMaskBorderless |
            AppKit.NSWindowStyleMaskNonactivatingPanel
        )

        # Plein écran
        rect = Foundation.NSMakeRect(0, 0, sw, sh)
        self._panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False,
        )

        self._panel.setLevel_(AppKit.NSFloatingWindowLevel + 2)
        self._panel.setHidesOnDeactivate_(False)
        self._panel.setFloatingPanel_(True)
        self._panel.setBecomesKeyOnlyIfNeeded_(True)

        behavior = (
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
            AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._panel.setCollectionBehavior_(behavior)

        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(_hex_to_nscolor(ALERT_A))
        self._panel.setAlphaValue_(0.82)   # semi-transparent : on voit encore l'écran
        self._panel.setSharingType_(1)

        content = self._panel.contentView()

        # ── Label principal : APPELER MEMBRE DE L'ÉQUIPE ──
        cta_rect = Foundation.NSMakeRect(20, sh * 0.42, sw - 40, sh * 0.20)
        self._lbl_cta = AppKit.NSTextField.alloc().initWithFrame_(cta_rect)
        self._lbl_cta.setStringValue_("APPELER MEMBRE DE L'ÉQUIPE")
        self._lbl_cta.setTextColor_(AppKit.NSColor.whiteColor())
        self._lbl_cta.setBackgroundColor_(AppKit.NSColor.clearColor())
        self._lbl_cta.setFont_(AppKit.NSFont.boldSystemFontOfSize_(64))
        self._lbl_cta.setBezeled_(False)
        self._lbl_cta.setEditable_(False)
        self._lbl_cta.setSelectable_(False)
        self._lbl_cta.setDrawsBackground_(False)
        self._lbl_cta.setAlignment_(AppKit.NSTextAlignmentCenter)
        self._lbl_cta.cell().setWraps_(True)
        content.addSubview_(self._lbl_cta)

        # ── Label secondaire : détail des alertes ──
        det_rect = Foundation.NSMakeRect(20, sh * 0.32, sw - 40, sh * 0.10)
        self._lbl_det = AppKit.NSTextField.alloc().initWithFrame_(det_rect)
        self._lbl_det.setStringValue_("")
        self._lbl_det.setTextColor_(AppKit.NSColor.whiteColor())
        self._lbl_det.setBackgroundColor_(AppKit.NSColor.clearColor())
        self._lbl_det.setFont_(AppKit.NSFont.boldSystemFontOfSize_(22))
        self._lbl_det.setBezeled_(False)
        self._lbl_det.setEditable_(False)
        self._lbl_det.setSelectable_(False)
        self._lbl_det.setDrawsBackground_(False)
        self._lbl_det.setAlignment_(AppKit.NSTextAlignmentCenter)
        self._lbl_det.cell().setWraps_(True)
        content.addSubview_(self._lbl_det)

        # ── Boutons snooze ──
        snooze_options = [
            ("Ok pour 10 minutes",       600),
            ("Ok pour 30 minutes",       1800),
            ("Ok pour 1h",               3600),
            ("Ok jusqu'à demain matin",  None),   # None = 8h demain
        ]
        n_btns   = len(snooze_options)
        btn_w    = 220
        btn_h    = 44
        spacing  = 20
        total_w  = n_btns * btn_w + (n_btns - 1) * spacing
        start_x  = (sw - total_w) / 2
        btn_y    = sh * 0.16

        for i, (label, duration) in enumerate(snooze_options):
            bx = start_x + i * (btn_w + spacing)
            btn_rect = Foundation.NSMakeRect(bx, btn_y, btn_w, btn_h)
            btn = AppKit.NSButton.alloc().initWithFrame_(btn_rect)
            btn.setTitle_(label)
            btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
            btn.setFont_(AppKit.NSFont.boldSystemFontOfSize_(14))
            # Fond blanc, texte rouge foncé
            btn.setWantsLayer_(True)
            btn.layer().setCornerRadius_(10.0)
            btn.layer().setBackgroundColor_(
                AppKit.NSColor.whiteColor().CGColor()
            )
            btn.setContentTintColor_(_hex_to_nscolor(ALERT_A))

            # Cible ObjC pour l'action
            target = _SnoozeTarget.alloc().init()
            _dur = duration  # capture pour le closure
            target._callback = lambda d=_dur: self.snooze(d)
            self._snooze_targets.append(target)  # garder en vie

            btn.setTarget_(target)
            btn.setAction_("clicked:")
            content.addSubview_(btn)

        # Start hidden
        self._panel.orderOut_(None)

    def snooze(self, duration):
        """Cache la bannière pour `duration` secondes (None = jusqu'à 8h demain)."""
        if duration is None:
            tomorrow = datetime.date.today() + datetime.timedelta(days=1)
            target_dt = datetime.datetime.combine(tomorrow, datetime.time(8, 0))
            duration = (target_dt - datetime.datetime.now()).total_seconds()
        self._snooze_until = time.time() + max(0, duration)
        if self._panel and self._visible:
            self._panel.orderOut_(None)
            self._visible = False
        print(f"[banner] snooze {duration/60:.0f} min")

    def update(self, issues, flash_state):
        """Update banner visibility and content based on issues."""
        if not self._panel:
            return

        # Snooze actif : bannière muette
        if time.time() < self._snooze_until:
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
            return

        if not issues:
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
            return

        # Détail des alertes (ligne par ligne)
        n = len(issues)
        summary_parts = []
        for iss in issues[:3]:
            short = iss.split("\u2014")[0].strip()
            summary_parts.append(short)
        detail = f"\u26a0\ufe0f  {n} ALERTE{'S' if n > 1 else ''}  \u2014  " + "   |   ".join(summary_parts)
        if n > 3:
            detail += f"  (+{n - 3})"

        try:
            self._lbl_det.setStringValue_(detail)
        except Exception:
            pass

        # Flash entre deux rouges
        color = ALERT_B if flash_state else ALERT_A
        self._panel.setBackgroundColor_(_hex_to_nscolor(color))

        if not self._visible:
            self._panel.orderFrontRegardless()
            self._visible = True
        else:
            self._panel.orderFrontRegardless()

        self._boost_above_obs()

    def _boost_above_obs(self):
        if not self._panel:
            return
        obs_level = _get_obs_projector_level()
        obs_wids = _get_obs_projector_window_ids()
        dyn_level = max(LEVEL_BANNER, obs_level + 2)
        self._panel.setLevel_(dyn_level)

        wid = self._panel.windowNumber()
        if HAVE_CGS and wid and obs_wids:
            _cgs_set_level(wid, dyn_level)
            for obs_wid in obs_wids:
                _cgs_order_above(wid, obs_wid)

    def hide(self):
        if self._panel and self._visible:
            self._panel.orderOut_(None)
            self._visible = False


# ─────────────────────────────────────────────────────────────────────────────
# Main app: rumps menu bar + OBS monitoring
# ─────────────────────────────────────────────────────────────────────────────

import rumps


class OBSMonitorRumps(rumps.App):
    RECONNECT = 5
    TICK_S    = 0.4

    def __init__(self):
        super().__init__(
            name="OBS Monitor",
            title="\u26a1 OBS",
            quit_button=None,
        )

        self._cfg  = load_config()
        self._lock = threading.Lock()

        self._req_client = None
        self._evt_client = None
        self._connected  = False

        self._flash_st        = False
        self._last_src_refresh = 0
        self._last_notif_issues = []
        self._last_notif_time  = 0.0
        self._ax_prompt_shown  = False

        self._audio = AudioMonitor(self._cfg["checks"]["audio"])
        self._video = VideoMonitor(self._cfg["checks"]["video"], self._get_req)

        # SMS notifier — partage le dict de config (modifs propagées en direct)
        if "sms" not in self._cfg:
            self._cfg["sms"] = dict(DEFAULT_CONFIG["sms"])
            save_config(self._cfg)
        self._sms = SMSNotifier(self._cfg["sms"])
        self._was_connected = False  # pour détecter perte de connexion

        self._panel  = NativePanel()
        self._banner = NativeBanner()
        self._update_ver = None
        self._update_url = None
        self._prev_issues = []
        self._transparent = False
        self._last_checkbox_sync = 0

        # Build menu
        self._issues_section = rumps.MenuItem("Aucun problème", callback=None)
        self._issues_section.set_callback(None)
        self._show_panel_item = rumps.MenuItem("Afficher le panneau", callback=self._on_show_panel)
        self._hide_panel_item = rumps.MenuItem("Masquer le panneau", callback=self._on_hide_panel)
        self._transparent_item = rumps.MenuItem("Panneau transparent", callback=self._on_toggle_transparent)

        # OBS connection config
        self._config_item = rumps.MenuItem("Configuration OBS…", callback=self._on_config)

        # SMS items
        sms_on = self._cfg.get("sms", {}).get("enabled", False)
        self._sms_toggle_item = rumps.MenuItem(
            "SMS : activé" if sms_on else "SMS : désactivé",
            callback=self._on_toggle_sms,
        )
        self._sms_config_item = rumps.MenuItem("Configuration SMS…", callback=self._on_sms_config)
        self._sms_test_item   = rumps.MenuItem("Envoyer SMS de test", callback=self._on_sms_test)

        self._update_item = rumps.MenuItem("Vérifier mise à jour…", callback=self._on_check_update_menu)
        self._quit_item = rumps.MenuItem("Quitter", callback=self._on_quit)

        self.menu = [
            self._issues_section,
            None,
            self._show_panel_item,
            self._hide_panel_item,
            self._transparent_item,
            None,
            self._config_item,
            self._sms_toggle_item,
            self._sms_config_item,
            self._sms_test_item,
            self._update_item,
            None,
            self._quit_item,
        ]

    def _on_show_panel(self, _):
        self._panel.show()

    def _on_hide_panel(self, _):
        self._panel.hide()

    def _on_toggle_transparent(self, _):
        self._transparent = not self._transparent
        if self._transparent:
            self._panel._panel.setAlphaValue_(0.03)
            self._transparent_item.title = "Panneau opaque"
        else:
            self._panel._panel.setAlphaValue_(0.97)
            self._transparent_item.title = "Panneau transparent"

    def _on_quit(self, _):
        self._save_positions()
        self._banner.hide()
        rumps.quit_application()

    def _on_config(self, _):
        """Show OBS connection config dialog."""
        try:
            c = self._cfg["obs"]
            # Use rumps.Window for simple input
            w = rumps.Window(
                title="Configuration OBS",
                message=f"Hôte actuel : {c['host']}\nPort : {c['port']}\n\nEntrez au format hôte:port:motdepasse",
                default_text=f"{c['host']}:{c['port']}:{c.get('password', '')}",
                ok="Reconnecter",
                cancel="Annuler",
            )
            resp = w.run()
            if resp.clicked:
                parts = resp.text.strip().split(":")
                if len(parts) >= 2:
                    self._cfg["obs"]["host"] = parts[0]
                    self._cfg["obs"]["port"] = int(parts[1])
                    if len(parts) >= 3:
                        self._cfg["obs"]["password"] = ":".join(parts[2:])
                    else:
                        self._cfg["obs"]["password"] = ""
                    save_config(self._cfg)
                    # Force reconnect
                    self._disconnect()
        except Exception as e:
            print(f"[config] {e}")

    def _on_toggle_sms(self, _):
        """Toggle SMS notifications on/off."""
        cur = self._cfg.setdefault("sms", dict(DEFAULT_CONFIG["sms"]))
        cur["enabled"] = not cur.get("enabled", False)
        save_config(self._cfg)
        self._sms_toggle_item.title = "SMS : activé" if cur["enabled"] else "SMS : désactivé"
        rumps.notification(
            title="OBS Monitor",
            subtitle="",
            message="SMS activés" if cur["enabled"] else "SMS désactivés",
            sound=False,
        )

    def _on_sms_config(self, _):
        """Show SMS config dialog (api_key, device, recipient)."""
        try:
            s = self._cfg.setdefault("sms", dict(DEFAULT_CONFIG["sms"]))
            current = f"{s.get('api_key','')}|{s.get('device','')}|{s.get('recipient','')}"
            w = rumps.Window(
                title="Configuration SMS (sms8.io)",
                message="Format : APIKEY|DEVICE|+33XXXXXXXXX\n(device = ID|simSlot, ex : 9210|0)",
                default_text=current,
                ok="Enregistrer",
                cancel="Annuler",
                dimensions=(420, 60),
            )
            resp = w.run()
            if resp.clicked:
                parts = resp.text.strip().split("|")
                if len(parts) >= 3:
                    s["api_key"]   = parts[0].strip()
                    s["device"]    = parts[1].strip()
                    s["recipient"] = "|".join(parts[2:]).strip()
                    save_config(self._cfg)
                    rumps.notification(
                        title="OBS Monitor",
                        subtitle="",
                        message="Configuration SMS enregistrée ✓",
                        sound=False,
                    )
        except Exception as e:
            print(f"[sms_config] {e}")

    def _on_sms_test(self, _):
        """Send a test SMS immediately, ignoring cooldown."""
        s = self._cfg.get("sms", {})
        if not s.get("api_key") or not s.get("recipient"):
            rumps.notification(
                title="OBS Monitor",
                subtitle="SMS non configurés",
                message="Renseigne d'abord la config SMS",
                sound=False,
            )
            return
        # Bypass cooldown by clearing last_sent for the test key
        with self._sms._lock:
            self._sms._last_sent.pop("__test__", None)
        self._sms.notify_event("__test__", f"OBS Monitor v{VERSION} — SMS de test ✓")
        rumps.notification(
            title="OBS Monitor",
            subtitle="",
            message="SMS de test envoyé",
            sound=False,
        )

    def _on_check_update_menu(self, _):
        threading.Thread(target=self._check_update_bg, daemon=True).start()

    def _on_do_update(self, _):
        if not self._update_url:
            return
        app_path = _real_app_path()
        def on_progress(msg):
            print(f"[update] {msg}")
        threading.Thread(
            target=install_update,
            args=(self._update_url, app_path, on_progress),
            daemon=True
        ).start()

    # ── Setup ────────────────────────────────────────────────────────────────

    def _after_start(self):
        """Called after the run loop is ready (via a short timer)."""
        self._panel.build()
        self._banner.build()

        # Wire up save button callback
        self._panel.set_save_callback(self._on_save_sources)

        self._video.start()
        threading.Thread(target=self._conn_loop, daemon=True).start()

        self._schedule_on_main(5.0, self._check_update_bg_wrapper)
        self._schedule_on_main(4.0, self._check_and_request_permissions)
        self._schedule_on_main(3.0, self._write_debug_log)

    def _schedule_on_main(self, delay, func):
        def _wrapper():
            time.sleep(delay)
            try:
                func()
            except Exception as e:
                print(f"[schedule] {e}")
        threading.Thread(target=_wrapper, daemon=True).start()

    # ── OBS Connection ───────────────────────────────────────────────────────

    def _get_req(self):
        with self._lock:
            return self._req_client

    def _connect(self):
        c = self._cfg["obs"]
        try:
            req = obs_ws.ReqClient(
                host=c["host"], port=c["port"],
                password=c["password"], timeout=5
            )
            evt = obs_ws.EventClient(
                host=c["host"], port=c["port"],
                password=c["password"],
                subs=obs_ws.Subs.INPUTVOLUMEMETERS,
            )

            def on_input_volume_meters(data):
                self._audio.on_volume_meters(data)

            evt.callback.register(on_input_volume_meters)

            with self._lock:
                self._req_client = req
                self._evt_client = evt
            self._connected        = True
            self._last_src_refresh = -999   # force refresh immédiat

            # Découverte immédiate des sources
            try:
                inp_list = req.get_input_list()
                audio_names = [i["inputName"] for i in inp_list.inputs
                               if i.get("inputKind", "").startswith(
                                   ("coreaudio", "wasapi", "alsa", "pulse",
                                    "av_capture", "dshow_input", "vlc", "ffmpeg")
                               ) or "audio" in i.get("inputKind", "").lower()
                               or "mic" in i.get("inputName", "").lower()
                               or "input" in i.get("inputKind", "").lower()]
                if not audio_names:
                    audio_names = [i["inputName"] for i in inp_list.inputs]
                self._audio.seed_inputs(audio_names)
            except Exception:
                pass

            return True
        except Exception as e:
            print(f"[OBS Monitor] Connexion impossible : {e}")
            return False

    def _force_reconnect(self):
        self._disconnect()

    def _disconnect(self):
        with self._lock:
            for cl in (self._req_client, self._evt_client):
                try:
                    if cl:
                        cl.base_client.ws.close()
                except Exception:
                    pass
            self._req_client = None
            self._evt_client = None
        self._connected = False

    def _conn_loop(self):
        while True:
            if not self._connected:
                self._connect()
                if not self._connected:
                    time.sleep(self.RECONNECT)
                    continue
            time.sleep(3)
            try:
                cl = self._get_req()
                if cl:
                    cl.get_version()
                else:
                    raise RuntimeError("no client")
            except Exception:
                self._disconnect()

    # ── Source refresh ────────────────────────────────────────────────────────

    def _refresh_sources(self):
        """Discover OBS sources and refresh panel checkboxes + info."""
        now = time.time()
        if now - self._last_src_refresh < 10:
            return
        self._last_src_refresh = now

        audio_names = self._audio.known_inputs()
        video_names = self._video.known_sources()

        # Update panel checkboxes
        try:
            self._panel.refresh_sources(audio_names, video_names, self._cfg)
        except Exception as e:
            print(f"[src_refresh] panel: {e}")

        # Update panel info section
        try:
            self._panel.update_info(audio_names, video_names, self._cfg)
        except Exception as e:
            print(f"[src_refresh] info: {e}")

    def _on_save_sources_menu(self, _):
        """Menu callback for saving source selection."""
        self._on_save_sources()

    def _on_save_sources(self):
        """Save selected sources from panel checkboxes to config."""
        try:
            audio_sel, video_sel = self._panel.get_selected_sources()
            self._cfg["checks"]["audio"]["monitor_inputs"] = audio_sel
            self._cfg["checks"]["video"]["monitor_sources"] = video_sel
            save_config(self._cfg)
            # Update info display
            audio_names = self._audio.known_inputs()
            video_names = self._video.known_sources()
            self._panel.update_info(audio_names, video_names, self._cfg)
            print(f"[save] audio={audio_sel} vidéo={video_sel}")
            # Confirm via notification
            rumps.notification(
                title="OBS Monitor",
                subtitle="",
                message="Sélection des sources enregistrée ✓",
                sound=False,
            )
        except Exception as e:
            print(f"[save_sources] {e}")

    def _sync_checkboxes(self):
        """Auto-sync checkbox state to config (no notification)."""
        try:
            audio_sel, video_sel = self._panel.get_selected_sources()
            old_a = self._cfg["checks"]["audio"].get("monitor_inputs", None)
            old_v = self._cfg["checks"]["video"].get("monitor_sources", None)
            if old_a is None or old_v is None or sorted(audio_sel) != sorted(old_a) or sorted(video_sel) != sorted(old_v):
                self._cfg["checks"]["audio"]["monitor_inputs"] = audio_sel
                self._cfg["checks"]["video"]["monitor_sources"] = video_sel
                save_config(self._cfg)
                # Update info
                audio_names = self._audio.known_inputs()
                video_names = self._video.known_sources()
                self._panel.update_info(audio_names, video_names, self._cfg)
                print(f"[auto-save] audio={audio_sel} vidéo={video_sel}")
        except Exception:
            pass

    # ── Tick (rumps timer) ───────────────────────────────────────────────────

    @rumps.timer(0.4)
    def _tick(self, _):
        """Main update loop — called every 0.4s by rumps."""
        if not self._panel._built:
            self._after_start()
            return

        issues = (self._audio.issues() + self._video.issues()) if self._connected else []
        self._flash_st = not self._flash_st if issues else False

        # Update menu bar icon
        if not self._connected:
            self.title = "\u26a1 OBS"
        elif issues:
            n = len(issues)
            self.title = f"\U0001f534 {n}"
        else:
            self.title = "\u2705 OBS"

        # Update panel
        self._panel.update_status(self._connected)
        self._panel.update_issues(issues)

        # Update banner (flashing red bar)
        self._banner.update(issues, self._flash_st)

        # Update menu dropdown
        self._update_menu_issues(issues)

        # Periodic boost
        self._panel.periodic_boost()

        # Refresh sources periodically when connected
        if self._connected:
            self._refresh_sources()

        # Auto-sync checkbox state to config every 2s
        now_t = time.time()
        if self._connected and now_t - self._last_checkbox_sync >= 2:
            self._last_checkbox_sync = now_t
            self._sync_checkboxes()

        # macOS notifications
        self._maybe_notify(issues)

        # SMS via sms8.io
        try:
            # Détecter une perte de connexion OBS → SMS one-shot
            if self._was_connected and not self._connected:
                self._sms.notify_event(
                    "obs_disconnect",
                    f"OBS Monitor — Connexion à OBS perdue ({time.strftime('%H:%M:%S')})"
                )
            self._was_connected = self._connected
            # Envoyer SMS pour chaque issue persistante
            self._sms.process(issues)
        except Exception as e:
            print(f"[sms.tick] {e}")

        # Save positions periodically
        if int(time.time()) % 5 == 0:
            self._save_positions()

    def _update_menu_issues(self, issues):
        try:
            if not issues:
                self._issues_section.title = "\u2705  Aucun problème"
            else:
                lines = []
                for iss in issues[:3]:
                    short = iss.split("\u2014")[0].strip()[:50]
                    lines.append(short)
                if len(issues) > 3:
                    lines.append(f"… +{len(issues)-3} autres")
                self._issues_section.title = "\n".join(lines)
        except Exception:
            pass

    def _maybe_notify(self, issues):
        now = time.time()
        n = len(issues)

        if n > 0:
            issues_changed = (issues != self._prev_issues)
            enough_time = (now - self._last_notif_time >= 15)
            if issues_changed and enough_time:
                self._send_notification(issues)
                self._last_notif_time = now

        self._prev_issues = list(issues)

    def _send_notification(self, issues):
        try:
            n = len(issues)
            title = f"OBS Monitor \u2014 {n} problème{'s' if n > 1 else ''}"
            body = " | ".join(str(iss).split("\u2014")[0].strip()[:50] for iss in issues[:2])
            if n > 2:
                body += f" (+{n-2} autres)"
            rumps.notification(
                title=title, subtitle="", message=body, sound=True,
            )
        except Exception as e:
            print(f"[notif] {e}")

    def _save_positions(self):
        try:
            self._panel.save_position(self._cfg)
            save_config(self._cfg)
        except Exception:
            pass

    # ── Update check ─────────────────────────────────────────────────────────

    def _check_update_bg_wrapper(self):
        threading.Thread(target=self._check_update_bg, daemon=True).start()

    def _check_update_bg(self):
        ver, url = check_for_update()
        if ver and url:
            self._update_ver = ver
            self._update_url = url
            try:
                self._panel.notify_update(ver, url)
                self._update_item.title = f"Installer v{ver}"
                self._update_item.set_callback(self._on_do_update)
            except Exception as e:
                print(f"[update_ui] {e}")
        self._schedule_on_main(30 * 60, self._check_update_bg)

    # ── Permissions ──────────────────────────────────────────────────────────

    def _check_and_request_permissions(self):
        try:
            import ctypes as _ct
            _ax_lib = _ct.cdll.LoadLibrary(
                '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices'
            )
            _ax_lib.AXIsProcessTrusted.restype = _ct.c_bool
            if not _ax_lib.AXIsProcessTrusted():
                print("[perm] Accessibilité NON accordée → demande")
                self._request_accessibility_permission()
            else:
                print("[perm] Accessibilité OK")
        except Exception as e:
            print(f"[perm_check] {e}")

    def _request_accessibility_permission(self):
        if self._ax_prompt_shown:
            return
        self._ax_prompt_shown = True
        try:
            subprocess.Popen([
                'open',
                'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rumps.notification(
                title="OBS Monitor \u2014 Permission requise",
                subtitle="",
                message="Ouvrez Réglages Système → Confidentialité → Accessibilité et ajoutez OBS Monitor",
                sound=True,
            )
        except Exception as e:
            print(f"[ax_perm] {e}")

    # ── Debug log ────────────────────────────────────────────────────────────

    def _write_debug_log(self):
        try:
            log_path = os.path.join(CONFIG_DIR, "debug.log")
            screens = get_all_screens()
            win_ids = _get_our_window_ids() if HAVE_QUARTZ else []
            proj = find_obs_projector_screen()

            levels = {}
            obs_wins = []
            if HAVE_QUARTZ:
                our_pid = os.getpid()
                wl = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll,
                                                       Quartz.kCGNullWindowID)
                for w in wl:
                    if w.get('kCGWindowOwnerPID') == our_pid:
                        wid = w.get('kCGWindowNumber', 0)
                        lvl = w.get('kCGWindowLayer', '?')
                        nm  = w.get('kCGWindowName') or ''
                        levels[wid] = (lvl, nm)
                    owner = (w.get('kCGWindowOwnerName') or '').lower()
                    if 'obs' in owner:
                        obs_wins.append({
                            'name':   w.get('kCGWindowName'),
                            'owner':  w.get('kCGWindowOwnerName'),
                            'layer':  w.get('kCGWindowLayer'),
                            'bounds': w.get('kCGWindowBounds'),
                        })

            panel_pos = "N/A"
            banner_pos = "N/A"
            if self._panel._panel:
                f = self._panel._panel.frame()
                panel_pos = f"x={int(f.origin.x)} y={int(f.origin.y)} w={int(f.size.width)} h={int(f.size.height)}"
            if self._banner._panel:
                f = self._banner._panel.frame()
                banner_pos = f"x={int(f.origin.x)} y={int(f.origin.y)} w={int(f.size.width)} h={int(f.size.height)}"

            lines = [
                f"=== OBSMonitor v{VERSION} debug (NSPanel + rumps + banner) ===",
                f"HAVE_CGS={HAVE_CGS} HAVE_APPKIT={HAVE_APPKIT} HAVE_QUARTZ={HAVE_QUARTZ}",
                f"Écrans détectés ({len(screens)}) :",
            ]
            for i, s in enumerate(screens):
                lines.append(f"  [{i}] {s}")
            lines.append(f"OBS Projector screen détecté : {proj}")
            lines.append(f"Panel position : {panel_pos}")
            lines.append(f"Banner position : {banner_pos}")
            lines.append(f"Nos window IDs : {win_ids}")
            lines.append(f"Nos niveaux réels :")
            for wid, (lvl, nm) in levels.items():
                lines.append(f"  wid={wid} layer={lvl} name={nm!r}")
            lines.append(f"Fenêtres OBS ({len(obs_wins)}) :")
            for w in obs_wins:
                lines.append(f"  {w}")
            lines.append("")

            with open(log_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            print(f"[debug] log écrit : {log_path}")
        except Exception as e:
            print(f"[debug] erreur log : {e}")


if __name__ == "__main__":
    OBSMonitorRumps().run()
