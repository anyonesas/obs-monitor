#!/usr/bin/env python3
"""
OBS Monitor v2.0 — Native macOS NSPanel + rumps menu bar
Panneau flottant natif (AppKit NSPanel) + icône barre de menu (rumps).
"""

VERSION      = "2.5.64"
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

try:
    import Vision
    # CIImage est dans Quartz (pyobjc-framework-Quartz), pas dans un module CoreImage séparé
    HAVE_VISION = True
except ImportError:
    HAVE_VISION = False

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
PANEL_LOG_PATH = os.path.join(CONFIG_DIR, "panel.log")

def _dlog(msg):
    """Debug log → fichier panel.log (les print() dans PyInstaller ne sortent
    nulle part visiblement). Aussi print pour les launches depuis Terminal."""
    try:
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {msg}\n"
        with open(PANEL_LOG_PATH, "a", encoding="utf-8") as _f:
            _f.write(line)
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass

DEFAULT_CONFIG = {
    "obs": {"host": "localhost", "port": 4455, "password": ""},
    "checks": {
        "audio": {
            "silence_db": -62,
            "silence_duration_s": 10,
            "clip_db": -1,
            "flat_std_db": 2.5,
            "flat_min_db": -45,
            "flat_duration_s": 5,
            "clip_ratio": 0.4,
            "monitor_inputs": None,
            "silence_enabled": True,
            "clip_enabled": True,
            "flat_enabled": True,
        },
        "video": {
            "freeze_threshold": 0.997,
            "freeze_duration_s": 3,
            "dark_threshold": 30,
            "bright_threshold": 242,
            "check_interval_s": 2,
            "monitor_sources": None,
            "headroom_threshold": 0.35,   # ratio max d'espace vide au-dessus du visage (0–1)
            "headroom_duration_s": 5,     # secondes avant d'afficher l'avertissement cadrage
            "freeze_enabled": True,
            "dark_enabled": True,
            "bright_enabled": True,
            "headroom_enabled": True,
        }
    },
    "panel": {"x": None, "y": None},
    "banner": {
        "y": None,
        "active_from": "00:00",    # heure début écran rouge (HH:MM)
        "active_until": "23:59",   # heure fin écran rouge (HH:MM)
        "active_days": "lun,mar,mer,jeu,ven,sam,dim",  # jours actifs (abréviations FR séparées par virgule)
        "cooldown_s": 0,           # délai avant réactivation écran rouge après résolution (s)
        "notif_cooldown_s": 1800,  # délai min entre deux notifications macOS (s) — défaut 30 min
        "enabled": True,
        "notif_enabled": True,
    },
    "warn_banner": {
        "active_from": "00:00",
        "active_until": "23:59",
        "active_days": "lun,mar,mer,jeu,ven,sam,dim",
    },
    "sms": {
        # v2.5.64+ : envoi via Anyone SMS Relay (sms-01.anyone-internal.com)
        # au lieu de sms8.io. Auth Bearer avec une cle uk_xxx.
        "enabled": False,
        "api_key": "",               # uk_xxx... (bearer token Upstream)
        "phone_gateway_id": "",      # pgw_xxx (vide = auto-selection)
        "relay_base_url": "https://sms-01.anyone-internal.com",
        "recipient": "",             # ex: "+33632548891"
        "cooldown_s": 600,           # 10 min entre SMS pour la même erreur
        "min_duration_s": 30,        # erreur doit durer 30s avant SMS
        "send_from": "10:00",
        "send_until": "18:30",
        "days": "lun,mar,mer,jeu,ven,sam,dim",
    },
    "scene_switch": {
        "enabled": False,
        "scene_1p": "",   # nom de scène OBS 1 personne
        "scene_2p": "",   # nom de scène OBS 2 personnes
        "trigger_s": 10,  # secondes de détection avant switch (10s par défaut)
        "cooldown_s": 30, # secondes de cooldown après switch
    },
    # Selection des sources par scene OBS (legacy v2.5.49) :
    #   { "Nom scene": { "audio": ["Micro 1"], "video": ["Cam Wide"] } }
    "scenes": {},
    # Nouveau (v2.5.52+) : portee de surveillance par source.
    # Splittee en audio/video (v2.5.62+) car une meme source (Blackmagic, etc.)
    # peut etre a la fois audio et video et necessiter un controle independant.
    #   "*"        = toutes scenes (defaut si absent)
    #   ""         = desactivee (jamais surveillee)
    #   "<scene>"  = surveillee uniquement quand cette scene est active
    "source_scenes": {},          # legacy fallback (lu si pas d'entree dans audio/video)
    "audio_source_scenes": {},    # specifique audio (v2.5.62+)
    "video_source_scenes": {},    # specifique video (v2.5.62+)
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
        # v2.5.64 : migration sms8 → Anyone Relay. Si l'ancien api_key sms8 est
        # detecte (40 hex chars sans prefix uk_), on le remet a zero pour eviter
        # qu'il soit envoye au mauvais endpoint. L'user reconfigure via le menu.
        s = c["sms"]
        if s.get("api_key") and not s["api_key"].startswith("uk_"):
            print(f"[sms] legacy sms8 api_key detecte, reset pour Relay")
            s["api_key"] = ""
            s.pop("device", None)
            s["enabled"] = False
    # Banner defaults
    if "banner" not in c:
        c["banner"] = dict(DEFAULT_CONFIG["banner"])
    else:
        for k, v in DEFAULT_CONFIG["banner"].items():
            c["banner"].setdefault(k, v)
    if "warn_banner" not in c:
        c["warn_banner"] = dict(DEFAULT_CONFIG["warn_banner"])
    else:
        for k, v in DEFAULT_CONFIG["warn_banner"].items():
            c["warn_banner"].setdefault(k, v)
    if "scene_switch" not in c:
        c["scene_switch"] = dict(DEFAULT_CONFIG["scene_switch"])
    else:
        for k, v in DEFAULT_CONFIG["scene_switch"].items():
            c["scene_switch"].setdefault(k, v)
    c.setdefault("scenes", {})
    c.setdefault("source_scenes", {})
    c.setdefault("audio_source_scenes", {})
    c.setdefault("video_source_scenes", {})
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
WARN_A  = "#7a5c00"   # ambre foncé (fond écran jaune)
WARN_B  = "#c49500"   # ambre vif (flash)


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
    Télécharge le DMG, monte, remplace le .app PENDANT QUE l'app tourne
    (macOS autorise le remplacement d'un bundle en cours d'exécution),
    puis lance un mini-script qui attend la mort du PID et fait juste `open`.
    """
    import shutil
    log_path = os.path.join(CONFIG_DIR, "update.log")

    def ulog(msg):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        try:
            with open(log_path, "a") as _f:
                _f.write(line)
        except Exception:
            pass
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    try:
        ulog(f"=== Mise à jour démarrée ===")
        ulog(f"URL    : {dmg_url}")
        ulog(f"dst    : {app_path}")

        # ── 1. Téléchargement ──────────────────────────────────────────────
        tmp_dmg = os.path.join(tempfile.gettempdir(), "OBSMonitor_update.dmg")
        ulog("Téléchargement…")
        r = subprocess.run(
            ["curl", "-L", "-o", tmp_dmg, "--max-time", "300", dmg_url],
            capture_output=True,
        )
        if r.returncode != 0:
            ulog(f"ERREUR curl ({r.returncode}) : {r.stderr.decode()[:300]}")
            return
        ulog(f"Téléchargé : {tmp_dmg}  ({os.path.getsize(tmp_dmg)//1024} Ko)")

        # ── 2. Montage ─────────────────────────────────────────────────────
        mnt = os.path.join(tempfile.gettempdir(), "OBSMonitor_mnt")
        # Démontage préventif si déjà monté
        subprocess.run(["hdiutil", "detach", mnt, "-quiet", "-force"],
                       capture_output=True)
        if os.path.exists(mnt):
            shutil.rmtree(mnt, ignore_errors=True)

        ulog("Montage DMG…")
        r = subprocess.run(
            ["hdiutil", "attach", tmp_dmg, "-mountpoint", mnt,
             "-quiet", "-nobrowse"],
            capture_output=True,
        )
        if r.returncode != 0:
            ulog(f"ERREUR hdiutil attach ({r.returncode}) : {r.stderr.decode()[:300]}")
            return
        ulog(f"Monté sur {mnt}  (contenu : {os.listdir(mnt)})")

        src_app = os.path.join(mnt, "OBSMonitor.app")
        if not os.path.exists(src_app):
            ulog(f"ERREUR : OBSMonitor.app absent du DMG")
            subprocess.run(["hdiutil", "detach", mnt, "-quiet"], capture_output=True)
            return

        dst_app     = app_path
        staging_app = dst_app + "_update_staging"

        # ── 3. Copier la NOUVELLE version dans un dossier staging ─────────
        # On NE TOUCHE PAS à l'app courante pendant qu'elle tourne.
        # Le swap (rm + mv) se fera dans le script de relance, après la mort du process.
        ulog(f"Copie staging : {src_app} → {staging_app}…")
        r = subprocess.run(
            ["bash", "-c", f"rm -rf '{staging_app}' && ditto '{src_app}' '{staging_app}' && xattr -cr '{staging_app}'"],
            capture_output=True,
        )
        ulog(f"Copie staging → {r.returncode}  {r.stderr.decode()[:200]}")

        if r.returncode != 0:
            ulog("Tentative avec privilèges admin (osascript)…")
            osa_cmd = (
                f"rm -rf '{staging_app}' ; "
                f"ditto '{src_app}' '{staging_app}' ; "
                f"xattr -cr '{staging_app}'"
            )
            r2 = subprocess.run(
                ["osascript", "-e", f"do shell script \"{osa_cmd.replace(chr(34), chr(92)+chr(34))}\" with administrator privileges"],
                capture_output=True,
            )
            ulog(f"osascript staging → {r2.returncode}  {r2.stderr.decode()[:200]}")
            if r2.returncode != 0:
                ulog("ERREUR copie staging impossible — abandon")
                return

        if not os.path.exists(staging_app):
            ulog("ERREUR : staging introuvable après copie — abandon")
            return

        ulog("Copie staging OK")

        # ── 4. Démonter DMG (on n'en a plus besoin) ───────────────────────
        subprocess.run(["hdiutil", "detach", mnt, "-quiet"], capture_output=True)
        try:
            os.remove(tmp_dmg)
        except Exception:
            pass
        ulog("DMG démonté")

        # ── 5. Script de swap + relance (process indépendant) ─────────────
        # Le swap (rm + mv) se fait APRÈS la mort de ce process.
        # On utilise start_new_session=True pour que le script survive au SIGKILL.
        pid = os.getpid()
        swap_cmd = (
            f"rm -rf '{dst_app}' && "
            f"mv '{staging_app}' '{dst_app}' && "
            f"xattr -cr '{dst_app}'"
        )
        script = (
            "#!/bin/bash\n"
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.2; done\n"
            "sleep 0.5\n"
            f"{swap_cmd} 2>/tmp/obs_swap.log\n"
            f"open '{dst_app}'\n"
        )
        script_path = os.path.join(tempfile.gettempdir(), "obs_monitor_open.sh")
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)

        try:
            subprocess.Popen(
                ["bash", script_path],
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ulog("Script swap+relance lancé (start_new_session)")
        except Exception as e:
            ulog(f"start_new_session échoué ({e}) — fallback osascript")
            osa = f"do shell script \"bash '{script_path}' &>/dev/null &\""
            subprocess.Popen(["osascript", "-e", osa])
            ulog("Script lancé via osascript")

        ulog("Fermeture dans 1s…")

        # ── 6. Quitte proprement ───────────────────────────────────────────
        time.sleep(1.0)
        os.kill(os.getpid(), 9)

    except Exception as e:
        import traceback
        ulog(f"EXCEPTION : {e}\n{traceback.format_exc()}")

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

    SILENCE_FIRST   = 10   # s — silence continu pour le 1er déclenchement
    SILENCE_RETRIG  = 30   # s — silence continu pour re-déclencher (rolling depuis dernier son)

    def __init__(self, cfg):
        self.cfg   = cfg
        self._lock = threading.Lock()
        self._inputs     = {}
        self._flat_since = {}   # {name: ts} son plat depuis quand
        self._silence_on    = set()  # inputs dont l'alerte silence est actuellement active
        self._once_alerted  = set()  # inputs ayant eu au moins une alerte (seuil → 30s)
        # Saturation continue : db >= clip_db pendant >= 4s consecutives → alerte 10s
        self._red_since       = {}   # {name: ts depuis lequel db >= clip_db}
        self._red_alert_until = {}   # {name: ts jusqu'auquel on garde l'alerte rouge}
        # Selection par scene OBS
        self._current_scene = None   # nom scene OBS active
        self._scenes_cfg    = {}     # legacy : cfg["scenes"][scene]["audio"]
        self._source_scenes = {}     # legacy : cfg["source_scenes"]
        self._kind_source_scenes = {} # nouveau v2.5.62+ : cfg["audio_source_scenes"]
        # {name: {peak_db, last_sound_t, buf: deque[float]}}

    def set_scenes_cfg(self, scenes_cfg):
        self._scenes_cfg = scenes_cfg

    def set_source_scenes(self, source_scenes, kind_source_scenes=None):
        """Old: source_scenes (legacy). New: kind_source_scenes (audio-specific).
        Audio monitor prefers kind_source_scenes if a key exists, else falls back."""
        self._source_scenes = source_scenes
        if kind_source_scenes is not None:
            self._kind_source_scenes = kind_source_scenes

    def set_current_scene(self, name):
        with self._lock:
            self._current_scene = name

    def _is_monitored(self, name):
        """True si la source `name` doit etre surveillee sur la scene courante."""
        cs = self._current_scene
        # 1) Priorite : nouveau modele per-source-per-kind (audio_source_scenes)
        if name in self._kind_source_scenes:
            spec = self._kind_source_scenes[name]
            if spec == "":
                return False
            if spec == "*":
                return True
            return spec == cs
        # 2) Legacy v2.5.52 : per-source generique
        if name in self._source_scenes:
            spec = self._source_scenes[name]
            if spec == "":
                return False
            if spec == "*":
                return True
            return spec == cs
        # 2) Legacy : selection par scene (cfg["scenes"][scene]["audio"])
        if cs and cs in self._scenes_cfg:
            v = self._scenes_cfg[cs].get("audio")
            if v is not None:
                return name in v
        # 3) Legacy global : monitor_inputs
        mon = self.cfg.get("monitor_inputs", None)
        if mon is None:
            return True
        return name in mon

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
                    # Son revenu pendant une alerte active → on sort de l'état alerte
                    # Le cooldown rolling (30s depuis dernier son) sera géré par last_sound_t
                    if name in self._silence_on:
                        self._silence_on.discard(name)

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
                if not self._is_monitored(name):
                    continue

                db      = e["peak_db"]
                silence = now - e["last_sound_t"]
                buf     = list(e["buf"])

                # ── Silence ──────────────────────────────────────────────────
                # Règles :
                #  • Alerte déjà active → la maintenir (silence toujours présent)
                #  • Pas encore alerté  → déclencher après SILENCE_FIRST (10s)
                #  • Déjà alerté 1× (son revenu)  → re-déclencher après SILENCE_RETRIG (30s)
                #    Le compteur 30s est rolling : reset à chaque son via last_sound_t
                if self.cfg.get("silence_enabled", True):
                    if name in self._silence_on:
                        # Alerte en cours : on la maintient tant que le silence dure
                        out.append(
                            f"\U0001f3a4  \u00ab {name} \u00bb  silence depuis {silence:.0f}s"
                            f"  \u2014 micro déconnecté ou muet ?"
                        )
                        continue

                    if silence >= silence_dur:
                        # Seuil : 10s pour le 1er déclenchement, 30s pour les suivants
                        thresh = self.SILENCE_RETRIG if name in self._once_alerted else self.SILENCE_FIRST
                        if silence < thresh:
                            pass
                        else:
                            # Déclenchement
                            self._silence_on.add(name)
                            self._once_alerted.add(name)
                            out.append(
                                f"\U0001f3a4  \u00ab {name} \u00bb  silence depuis {silence:.0f}s"
                                f"  \u2014 micro déconnecté ou muet ?"
                            )
                            continue   # inutile de verifier le reste
                else:
                    # silence_enabled=False : on nettoie l'état d'alerte silence
                    self._silence_on.discard(name)

                if len(buf) < 20:
                    continue   # pas assez de donnees

                mean = sum(buf) / len(buf)
                std  = (sum((v - mean) ** 2 for v in buf) / len(buf)) ** 0.5

                # Son trop constant (bourdonnement, micro bloqué)
                # Doit rester plat pendant flat_dur secondes d'affilée pour déclencher
                if self.cfg.get("flat_enabled", True):
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
                else:
                    self._flat_since.pop(name, None)

                # Saturation continue : db >= clip_db pendant >= 4s consecutives
                # → alerte verrouillee pendant 10s (meme si la satu s'arrete avant)
                if self.cfg.get("clip_enabled", True):
                    if db >= clip_db:
                        self._red_since.setdefault(name, now)
                        red_for = now - self._red_since[name]
                        if red_for >= 4.0:
                            self._red_alert_until[name] = now + 10.0
                    else:
                        self._red_since.pop(name, None)

                    if now < self._red_alert_until.get(name, 0.0):
                        out.append(
                            f"\U0001f534  \u00ab {name} \u00bb  saturation continue"
                            f"  \u2014 baisser le gain du micro"
                        )
                    elif db >= clip_db:
                        # Ecretage ponctuel (non chronique, pas encore 4s)
                        out.append(f"\U0001f50a  \u00ab {name} \u00bb  écrêtage ponctuel ({db:.1f} dB)")

        return out


# ─────────────────────────────────────────────────────────────────────────────
# Video Monitor
# ─────────────────────────────────────────────────────────────────────────────

class VideoMonitor:
    VIDEO_FIRST  = 10   # s — durée continue pour 1er déclenchement (idem audio)
    VIDEO_RETRIG = 30   # s — durée continue pour re-déclenchement (rolling depuis dernière image normale)

    def __init__(self, cfg, get_client):
        self.cfg         = cfg
        self._get_client = get_client
        self._lock       = threading.Lock()
        self._issues_buf = []
        self._prev_frames = {}   # {src: (ts, img)} — dernière frame NON gelée
        self._known       = []
        # État par source/type — uniquement accédé par le thread _check (pas de lock nécessaire)
        self._v_last_normal  = {}   # {src: {type: float}} — dernière fois que la condition était absente
        self._v_cond_on      = {}   # {src: set}  — alertes actuellement actives
        self._v_once_alerted = {}   # {src: set}  — types ayant eu au moins une alerte
        self._headroom_buf   = []   # avertissements de cadrage (bannière jaune)
        # Auto-switch de scène par comptage de visages
        self._scene_switch_cfg   = {}
        self._face_ts_buf        = []   # [(timestamp, face_count)]
        self._scene_switch_until = 0.0  # cooldown jusqu'à ce timestamp
        self._switch_warn_buf    = []   # avertissements bannière jaune si switch échoue
        # Selection par scene OBS
        self._current_scene = None
        self._scenes_cfg    = {}
        self._source_scenes = {}     # legacy v2.5.52
        self._kind_source_scenes = {} # nouveau v2.5.62+ : video_source_scenes

    def set_scenes_cfg(self, scenes_cfg):
        self._scenes_cfg = scenes_cfg

    def set_source_scenes(self, source_scenes, kind_source_scenes=None):
        self._source_scenes = source_scenes
        if kind_source_scenes is not None:
            self._kind_source_scenes = kind_source_scenes

    def set_current_scene(self, name):
        with self._lock:
            self._current_scene = name

    def _is_monitored(self, name):
        """True si la source video `name` doit etre surveillee sur la scene courante."""
        cs = self._current_scene
        # 1) Priorite : nouveau modele per-source-per-kind (video_source_scenes)
        if name in self._kind_source_scenes:
            spec = self._kind_source_scenes[name]
            if spec == "":
                return False
            if spec == "*":
                return True
            return spec == cs
        # 2) Legacy v2.5.52 : per-source generique
        if name in self._source_scenes:
            spec = self._source_scenes[name]
            if spec == "":
                return False
            if spec == "*":
                return True
            return spec == cs
        if cs and cs in self._scenes_cfg:
            v = self._scenes_cfg[cs].get("video")
            if v is not None:
                return name in v
        mon = self.cfg.get("monitor_sources", None)
        if mon is None:
            return True
        return name in mon

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
            raw_items = client.get_scene_item_list(scene).scene_items
        except Exception:
            return

        # OBS retourne les items du top-level. Si un item est un Groupe,
        # on doit ouvrir le groupe pour voir ses enfants. On filtre aussi
        # les sources audio-only (mic) qui sont parfois ajoutees comme
        # scene_items mais n'ont aucun visuel a surveiller.
        AUDIO_ONLY_KINDS = (
            "coreaudio_input_capture", "coreaudio_output_capture",
            "wasapi_input_capture", "wasapi_output_capture",
            "wasapi_process_output_capture",
            "alsa_input_capture", "pulse_input_capture", "pulse_output_capture",
        )
        def _is_audio_only(it):
            k = it.get("inputKind", "") or ""
            return any(k.startswith(a) for a in AUDIO_ONLY_KINDS)

        items = []
        for it in raw_items:
            if not it.get("sceneItemEnabled", True):
                continue
            if _is_audio_only(it):
                continue
            if it.get("isGroup"):
                try:
                    group_name = it.get("sourceName", "")
                    children = client.get_group_scene_item_list(group_name).scene_items
                    for ch in children:
                        if (ch.get("sceneItemEnabled", True)
                            and not _is_audio_only(ch)):
                            items.append(ch)
                except Exception:
                    pass
            else:
                items.append(it)

        # Track la scene OBS active pour le filtrage par scene
        with self._lock:
            self._current_scene = scene
        found        = []
        new_issues   = []
        new_headroom = []
        new_switch_warn = []
        now          = time.time()
        max_faces_this_tick = 0

        for item in items:
            src = item.get("sourceName", "")
            if not src:
                continue
            found.append(src)
            if not self._is_monitored(src):
                continue

            img = self._capture(client, src)
            if img is None:
                continue

            gray = img.convert("L")
            stat = ImageStat.Stat(gray)
            br   = stat.mean[0]
            std  = stat.stddev[0]

            dark_thr   = self.cfg["dark_threshold"]
            bright_thr = self.cfg["bright_threshold"]

            # ── Gel (freeze) ──────────────────────────────────────────────
            # Cas spécial : image totalement noire (luminosité < 5) = caméra éteinte.
            # Certaines sources OBS génèrent des frames noires non identiques (bruit
            # de compression) qui trompent le détecteur de similarité → forcer is_frozen.
            is_black_screen = (br < 5)
            is_frozen = is_black_screen or self._freeze_condition(src, img, br)
            if self.cfg.get("freeze_enabled", True):
                ok, dur = self._check_condition(src, "freeze", is_frozen, now)
                if ok:
                    new_issues.append(
                        f"\U0001f9ca  \u00ab {src} \u00bb  figée depuis {dur:.0f}s"
                        f"  \u2014 caméra plantée ?"
                    )
            else:
                self._check_condition(src, "freeze", False, now)

            # ── Sombre ────────────────────────────────────────────────────
            # Ignoré si l'image est figée (black screen = gel/cam éteinte → doublon)
            if self.cfg.get("dark_enabled", True):
                is_dark = (br < dark_thr) and not is_frozen
                ok, dur = self._check_condition(src, "dark", is_dark, now)
                if ok:
                    new_issues.append(
                        f"\U0001f4f7  \u00ab {src} \u00bb  image trop sombre depuis {dur:.0f}s"
                        f"  (luminosité {br:.0f}/255)  \u2014 lumières éteintes ou caméra déconnectée ?"
                    )
            else:
                self._check_condition(src, "dark", False, now)

            # ── Uniforme (capteur bloqué sur une couleur) ─────────────────
            is_uniform = (std < 4 and 10 <= br < 60) and not is_frozen
            ok, dur = self._check_condition(src, "uniform", is_uniform, now)
            if ok:
                new_issues.append(
                    f"\U0001f4f7  \u00ab {src} \u00bb  image anormalement uniforme depuis {dur:.0f}s"
                    f"  \u2014 caméra bloquée ?"
                )

            # ── Surexposée ────────────────────────────────────────────────
            if self.cfg.get("bright_enabled", True):
                is_bright = (br > bright_thr) and not is_frozen
                ok, dur = self._check_condition(src, "bright", is_bright, now)
                if ok:
                    new_issues.append(
                        f"\U0001f4a1  \u00ab {src} \u00bb  surexposée depuis {dur:.0f}s"
                        f"  (luminosité {br:.0f}/255)  \u2014 éclairage trop fort ?"
                    )
            else:
                self._check_condition(src, "bright", False, now)

            # ── Headroom + comptage visages (Vision) ──────────────────────
            if not is_frozen and not is_black_screen:
                hr, face_count = self._run_vision(img)
                max_faces_this_tick = max(max_faces_this_tick, face_count)
                if self.cfg.get("headroom_enabled", True):
                    headroom_thr = float(self.cfg.get("headroom_threshold", 0.35))
                    is_bad_framing = (hr is not None) and (hr > headroom_thr)
                    ok, dur = self._check_condition(src, "headroom", is_bad_framing, now)
                    if ok:
                        new_headroom.append(
                            f"\u2195  \u00ab {src} \u00bb  cadrage à corriger depuis {dur:.0f}s"
                            f"  (air au-dessus : {hr*100:.0f}%)"
                        )
                else:
                    self._check_condition(src, "headroom", False, now)
            else:
                self._check_condition(src, "headroom", False, now)

        # ── Auto-switch de scène selon le nombre de visages ──────────────
        sc = self._scene_switch_cfg
        if sc.get("enabled") and HAVE_VISION:
            self._face_ts_buf.append((now, max_faces_this_tick))
            trigger_s  = float(sc.get("trigger_s", 20))
            cooldown_s = float(sc.get("cooldown_s", 30))
            # Nettoyage du buffer : garder seulement les 60 dernières secondes
            self._face_ts_buf = [(t, c) for t, c in self._face_ts_buf if now - t < 60]
            if now > self._scene_switch_until:
                window = [(t, c) for t, c in self._face_ts_buf if now - t <= trigger_s]
                # Déclenche seulement si le buffer contient au moins trigger_s secondes de données
                if window and self._face_ts_buf and (now - self._face_ts_buf[0][0]) >= trigger_s:
                    counts   = [c for _, c in window]
                    all_2p   = all(c >= 2 for c in counts)
                    all_1p   = all(c == 1 for c in counts)
                    scene_1p = sc.get("scene_1p", "")
                    scene_2p = sc.get("scene_2p", "")
                    try:
                        if all_2p and scene_2p and scene == scene_1p:
                            try:
                                client.set_current_program_scene(scene_2p)
                                print(f"[scene_switch] → {scene_2p}")
                                self._scene_switch_until = now + cooldown_s
                                self._face_ts_buf.clear()
                            except Exception as e:
                                new_switch_warn.append(
                                    f"\u26a0\ufe0f  Passage scène 2 personnes impossible — {e}"
                                )
                        elif all_1p and scene_1p and scene == scene_2p:
                            try:
                                client.set_current_program_scene(scene_1p)
                                print(f"[scene_switch] → {scene_1p}")
                                self._scene_switch_until = now + cooldown_s
                                self._face_ts_buf.clear()
                            except Exception as e:
                                new_switch_warn.append(
                                    f"\u26a0\ufe0f  Passage scène 1 personne impossible — {e}"
                                )
                    except Exception:
                        pass

        with self._lock:
            self._issues_buf   = new_issues
            self._headroom_buf = new_headroom + new_switch_warn
            self._switch_warn_buf = new_switch_warn
            self._known        = found

    def _capture(self, client, name):
        # PNG d'abord : lossless, pas d'artefacts JPEG qui fausseraient
        # la détection de gel (max_diff fluctuant à cause de la recompression)
        for fmt in ("png", "jpg"):
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

    def _check_condition(self, src, ctype, is_active, now):
        """Logique 10s/30s rolling identique à l'audio, pour une condition vidéo.

        Retourne (doit_alerter: bool, durée_s: float).
        Appelé uniquement depuis _check() → thread unique → pas de lock nécessaire.

        Règles :
          • 1er déclenchement  : condition présente en continu >= VIDEO_FIRST (10s)
          • Re-déclenchement   : condition présente en continu >= VIDEO_RETRIG (30s)
            depuis la dernière image normale (rolling : chaque image normale reset le timer)
          • Alerte déjà active : maintenue tant que la condition est présente
        """
        if src not in self._v_last_normal:
            self._v_last_normal[src]  = {}
            self._v_cond_on[src]      = set()
            self._v_once_alerted[src] = set()

        if not is_active:
            # Condition absente → reset timer + éteindre alerte
            self._v_last_normal[src][ctype] = now
            self._v_cond_on[src].discard(ctype)
            return False, 0.0

        last_normal = self._v_last_normal[src].get(ctype, now)
        duration    = now - last_normal

        if ctype in self._v_cond_on[src]:
            # Alerte déjà active → maintenir
            return True, duration

        thresh = self.VIDEO_RETRIG if ctype in self._v_once_alerted[src] else self.VIDEO_FIRST
        if duration >= thresh:
            self._v_cond_on[src].add(ctype)
            self._v_once_alerted[src].add(ctype)
            return True, duration

        return False, duration

    def _freeze_condition(self, src, img, brightness=128):
        """Détecte si l'image est figée par rapport à la dernière frame non-gelée.
        Retourne True/False — le timing est géré par _check_condition."""
        with self._lock:
            prev = self._prev_frames.get(src)

        if prev is None:
            with self._lock:
                self._prev_frames[src] = (time.time(), img)
            return False

        _, pimg = prev
        a    = pimg.convert("L").resize((80, 45))
        b    = img.convert("L").resize((80, 45))
        diff = ImageChops.difference(a, b)
        rms  = ImageStat.Stat(diff).rms[0]
        max_diff = diff.getextrema()[1]
        sim  = 1.0 - rms / 128.0

        if brightness < self.cfg.get("dark_threshold", 30):
            max_diff_limit = 40    # images sombres : bruit capteur plus fort
            sim_threshold  = 0.990
        else:
            max_diff_limit = 30
            sim_threshold  = self.cfg["freeze_threshold"]

        is_frozen = (sim >= sim_threshold) and (max_diff < max_diff_limit)

        if not is_frozen:
            # Mettre à jour la référence uniquement quand l'image bouge
            with self._lock:
                self._prev_frames[src] = (time.time(), img)

        return is_frozen

    # ── Vision (cadrage + comptage visages) ──────────────────────────────────

    def _run_vision(self, img):
        """
        Détecte les visages dans l'image PIL via Apple Vision.
        Retourne (headroom: float|None, face_count: int).
        headroom = espace vide au-dessus du visage principal (None si pas de visage).
        face_count = nombre de visages détectés.
        """
        if not HAVE_VISION or not HAVE_APPKIT or not HAVE_QUARTZ:
            return None, 0
        try:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            raw = buf.read()
            ns_data = Foundation.NSData.dataWithBytes_length_(raw, len(raw))
            ci_image = Quartz.CIImage.imageWithData_(ns_data)
            if ci_image is None:
                return None, 0

            handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(
                ci_image, {}
            )
            request = Vision.VNDetectFaceRectanglesRequest.alloc().init()
            handler.performRequests_error_([request], None)

            results = request.results()
            if not results or len(results) == 0:
                return None, 0

            face_count = len(results)
            best = max(results, key=lambda f: f.boundingBox().size.width * f.boundingBox().size.height)
            bb = best.boundingBox()
            face_top = bb.origin.y + bb.size.height
            headroom = max(0.0, 1.0 - face_top)
            return headroom, face_count
        except Exception as e:
            print(f"[vision] {e}")
            return None, 0

    def _detect_headroom(self, img):
        headroom, _ = self._run_vision(img)
        return headroom

    def headroom_issues(self):
        """Retourne la liste des avertissements de cadrage + switch (pour bannière jaune)."""
        with self._lock:
            return list(self._headroom_buf)

    def set_scene_switch_cfg(self, cfg: dict):
        """Met à jour la config auto-switch depuis le thread principal."""
        self._scene_switch_cfg = cfg

    def known_sources(self):
        with self._lock:
            return list(self._known)

    def issues(self):
        with self._lock:
            return list(self._issues_buf)


# ─────────────────────────────────────────────────────────────────────────────
# SMSNotifier — envoie des SMS via Anyone SMS Relay quand des erreurs persistent
# ─────────────────────────────────────────────────────────────────────────────

import re as _re
import urllib.parse as _urlparse

class SMSNotifier:
    """Envoie des SMS via Anyone SMS Relay (sms-01.anyone-internal.com).

    Logique anti-spam :
      - Une erreur doit durer >= min_duration_s avant déclenchement
      - Cooldown de cooldown_s entre 2 SMS pour la MÊME erreur (clé = type+source)
      - Quand l'erreur disparaît, son état est nettoyé pour repartir à zéro
    """

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

    def _in_send_window(self):
        """Vérifie si l'heure actuelle est dans la plage d'envoi configurée."""
        try:
            now_t = datetime.datetime.now().time()
            from_s = self.cfg.get("send_from", "00:00")
            until_s = self.cfg.get("send_until", "23:59")
            h0, m0 = (int(x) for x in from_s.split(":"))
            h1, m1 = (int(x) for x in until_s.split(":"))
            start = datetime.time(h0, m0)
            end   = datetime.time(h1, m1)
            return start <= now_t <= end
        except Exception:
            return True  # en cas d'erreur de parsing, on envoie quand même

    def process(self, current_issues):
        """Appelé à chaque tick avec la liste des issues actuelles."""
        if not self.cfg.get("enabled", False):
            return
        if not self.cfg.get("api_key") or not self.cfg.get("recipient"):
            return
        if not self._in_send_window():
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
                    ts = time.strftime("%H:%M")
                    self._send_async(f"[{ts}] {text}")

            # Nettoyer les erreurs qui ne sont plus actives
            for key in list(self._first_seen.keys()):
                if key not in current_keys:
                    self._first_seen.pop(key, None)

    def notify_event(self, key, message):
        """Envoie un SMS one-shot pour un évènement (ex: déconnexion OBS).

        Pas de durée minimale, mais cooldown et plage horaire respectés.
        """
        if not self.cfg.get("enabled", False):
            return
        if not self.cfg.get("api_key") or not self.cfg.get("recipient"):
            return
        if not self._in_send_window():
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
        """Envoi via Anyone SMS Relay (sms-01.anyone-internal.com).
        Auth Bearer avec api_key. Si phone_gateway_id est defini on l'utilise,
        sinon phoneGatewaySelection='any' (auto-selection)."""
        try:
            api_key   = (self.cfg.get("api_key") or "").strip()
            recipient = (self.cfg.get("recipient") or "").strip()
            gw_id     = (self.cfg.get("phone_gateway_id") or "").strip()
            base_url  = (self.cfg.get("relay_base_url") or
                         "https://sms-01.anyone-internal.com").rstrip("/")

            if not api_key or not recipient:
                print("[sms] api_key ou recipient manquant")
                return

            url = f"{base_url}/api/upstream/messages/outbound"

            # Idempotency key : empeche un double-envoi en cas de retry reseau.
            # Hash sur message + minute pour identifier "meme alerte dans la meme minute"
            import hashlib
            idem = "obs-" + hashlib.sha256(
                (message + time.strftime("%Y%m%d%H%M")).encode("utf-8")
            ).hexdigest()[:24]

            payload = {
                "toPhoneNumber": recipient,
                "messageText": message,
                "idempotencyKey": idem,
                # Alertes time-sensitive : expirent apres 10 min
                # si pas dispatchees (sinon SMS arrive tardivement, peu utile)
                "expiresAfterSeconds": 600,
            }
            if gw_id:
                payload["phoneGatewayId"] = gw_id
            else:
                payload["phoneGatewaySelection"] = "any"

            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "OBSMonitor")

            import ssl as _ssl
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE

            try:
                with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                    resp = r.read().decode("utf-8", errors="replace")[:300]
                    print(f"[sms] {r.status} → {message[:60]}  | {resp[:120]}")
            except urllib.error.HTTPError as he:
                err_body = ""
                try:
                    err_body = he.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                print(f"[sms] HTTP {he.code} → {err_body}")
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



# Flipped NSView + ObjC button target helper
# ─────────────────────────────────────────────────────────────────────────────

class _FlippedView(AppKit.NSView):
    """NSView subclass with flipped coordinate system (origin top-left)."""
    def isFlipped(self):
        return True


class _ActionTarget(Foundation.NSObject):
    """Cible ObjC générique pour actions NSControl (steppers, boutons, etc.)."""
    _callback = None
    def action_(self, sender):
        if self._callback:
            try:
                self._callback(sender)
            except Exception as e:
                print(f"[action_target] {e}")


# ─────────────────────────────────────────────────────────────────────────────
# _FormPanel — panneau NSPanel natif avec champs texte étiquetés
# ─────────────────────────────────────────────────────────────────────────────

class _FormPanel:
    """Panneau NSPanel natif avec champs texte étiquetés — dialogs de configuration."""

    ROW_H = 28
    SEC_H = 18
    PAD   = 14
    BTN_H = 30

    def __init__(self, title, width=480):
        self._title         = title
        self._width         = width
        self._panel         = None
        self._fields        = {}   # key → NSTextField (éditable)
        self._save_cb       = None
        self._cancel_target = None
        self._save_target   = None

    def show(self, sections, on_save):
        """
        sections : list of (section_title: str, rows: list of (label: str, key: str, value: str))
        on_save  : callable({key: str_value})  — appelé si l'utilisateur clique Enregistrer
        """
        if self._panel and self._panel.isVisible():
            self._panel.makeKeyAndOrderFront_(None)
            return

        self._save_cb = on_save
        self._fields  = {}

        W  = self._width
        LW = 200   # largeur colonne label
        FW = W - LW - self.PAD * 3  # largeur champ texte

        # Hauteur totale du contenu
        n_sections = len(sections)
        n_rows     = sum(len(rows) for _, rows in sections)
        content_h  = (self.PAD
                      + n_sections * (self.SEC_H + 8)
                      + n_rows     * (self.ROW_H + 6)
                      + self.PAD * 2 + self.BTN_H + self.PAD)

        # Centrer sur l'écran principal
        scr = AppKit.NSScreen.mainScreen().frame()
        sx  = (scr.size.width  - W) / 2
        sy  = (scr.size.height - content_h) / 2

        rect  = Foundation.NSMakeRect(sx, sy, W, content_h)
        style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        panel.setTitle_(self._title)
        panel.setLevel_(AppKit.NSFloatingWindowLevel)
        panel.setHidesOnDeactivate_(False)
        panel.setBackgroundColor_(_hex_to_nscolor(BG))

        # Vue retournée (origine en haut à gauche)
        doc = _FlippedView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, W, content_h)
        )
        panel.contentView().addSubview_(doc)

        FG_COLOR    = _hex_to_nscolor("#FFFFFF")
        ACC_COLOR   = _hex_to_nscolor(ACCENT)
        FIELD_BG    = _hex_to_nscolor("#2C2C2C")

        y = self.PAD

        for sec_title, rows in sections:
            # Titre de section
            slbl = AppKit.NSTextField.alloc().initWithFrame_(
                Foundation.NSMakeRect(self.PAD, y, W - self.PAD * 2, self.SEC_H)
            )
            slbl.setStringValue_(sec_title)
            slbl.setEditable_(False)
            slbl.setBezeled_(False)
            slbl.setDrawsBackground_(False)
            slbl.setFont_(AppKit.NSFont.boldSystemFontOfSize_(10))
            slbl.setTextColor_(ACC_COLOR)
            doc.addSubview_(slbl)
            y += self.SEC_H + 6

            for label, key, value in rows:
                # Label
                rlbl = AppKit.NSTextField.alloc().initWithFrame_(
                    Foundation.NSMakeRect(self.PAD, y + 5, LW, self.ROW_H - 6)
                )
                rlbl.setStringValue_(label)
                rlbl.setEditable_(False)
                rlbl.setBezeled_(False)
                rlbl.setDrawsBackground_(False)
                rlbl.setFont_(AppKit.NSFont.systemFontOfSize_(12))
                rlbl.setTextColor_(FG_COLOR)
                rlbl.setAlignment_(AppKit.NSTextAlignmentRight)
                doc.addSubview_(rlbl)

                # Champ texte éditable
                field = AppKit.NSTextField.alloc().initWithFrame_(
                    Foundation.NSMakeRect(LW + self.PAD * 2, y + 3, FW, self.ROW_H - 4)
                )
                field.setStringValue_(str(value) if value is not None else "")
                field.setEditable_(True)
                field.setSelectable_(True)
                field.setBezeled_(True)
                field.setFont_(AppKit.NSFont.systemFontOfSize_(12))
                field.setTextColor_(FG_COLOR)
                field.setBackgroundColor_(FIELD_BG)
                doc.addSubview_(field)
                self._fields[key] = field
                y += self.ROW_H + 6

            y += 4  # espace entre sections

        y += self.PAD

        # Boutons Annuler / Enregistrer
        btn_w = 120
        cancel_btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(self.PAD, y, btn_w, self.BTN_H)
        )
        cancel_btn.setTitle_("Annuler")
        cancel_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        doc.addSubview_(cancel_btn)

        save_btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(W - self.PAD - btn_w, y, btn_w, self.BTN_H)
        )
        save_btn.setTitle_("Enregistrer")
        save_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        save_btn.setKeyEquivalent_("\r")
        doc.addSubview_(save_btn)

        # Cibles ObjC pour les boutons (pattern déjà utilisé dans _SnoozeTarget)
        ct = _SnoozeTarget.alloc().init()
        ct._callback = lambda: panel.orderOut_(None)
        cancel_btn.setTarget_(ct)
        cancel_btn.setAction_("clicked:")
        self._cancel_target = ct

        st = _SnoozeTarget.alloc().init()
        st._callback = self._on_save
        save_btn.setTarget_(st)
        save_btn.setAction_("clicked:")
        self._save_target = st

        self._panel = panel
        panel.makeKeyAndOrderFront_(None)

    def _on_save(self):
        values = {key: f.stringValue() for key, f in self._fields.items()}
        if self._panel:
            self._panel.orderOut_(None)
        if self._save_cb:
            try:
                self._save_cb(values)
            except Exception as e:
                print(f"[_FormPanel] save error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# WebKit — chargement du framework pour WKWebView
# ─────────────────────────────────────────────────────────────────────────────

HAVE_WEBKIT        = False
_WKWebView         = None
_WKWebViewConfig   = None
_WKUserCC          = None

if HAVE_APPKIT:
    try:
        _wk_bundle = Foundation.NSBundle.bundleWithPath_(
            "/System/Library/Frameworks/WebKit.framework"
        )
        if _wk_bundle and not _wk_bundle.isLoaded():
            _wk_bundle.load()
        import objc as _objc_wk
        _WKWebView       = _objc_wk.lookUpClass("WKWebView")
        _WKWebViewConfig = _objc_wk.lookUpClass("WKWebViewConfiguration")
        _WKUserCC        = _objc_wk.lookUpClass("WKUserContentController")
        HAVE_WEBKIT = True
    except Exception as _wk_e:
        print(f"[webkit] {_wk_e}")


class _WKMsgHandler(Foundation.NSObject):
    """Reçoit les messages JS → Python via window.webkit.messageHandlers.obsMonitor."""
    _callback = None

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        if not self._callback:
            return
        try:
            body = message.body()
            s = body.UTF8String().decode("utf-8") if hasattr(body, "UTF8String") else str(body)
            import json as _j
            self._callback(_j.loads(s))
        except Exception as e:
            print(f"[wk_handler] {e}")


# ─────────────────────────────────────────────────────────────────────────────
# _WebSettingsPanel — panneau HTML/CSS via WKWebView
# ─────────────────────────────────────────────────────────────────────────────

class _WebSettingsPanel:
    DAYS_FR  = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    DAY_KEYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]

    def __init__(self, title):
        self._title   = title
        self._panel   = None
        self._webview = None
        self._handler = None
        self._on_save = None
        self._fallback = None

    def show(self, sections, on_save):
        if self._panel and self._panel.isVisible():
            self._panel.makeKeyAndOrderFront_(None)
            return
        self._on_save = on_save
        if not HAVE_WEBKIT:
            if not self._fallback:
                self._fallback = _SettingsPanel(self._title)
            self._fallback.show(sections, on_save)
            return
        html = self._gen_html(sections)
        self._build(html)

    def _on_msg(self, data):
        action = data.get("action") if isinstance(data, dict) else None
        if action == "cancel":
            if self._panel:
                self._panel.orderOut_(None)
        elif action == "save":
            vals = data.get("values", {})
            if self._on_save:
                try:
                    self._on_save(vals)
                except Exception as e:
                    print(f"[web_panel] save: {e}")
            if self._panel:
                self._panel.orderOut_(None)

    def _build(self, html):
        self._handler = _WKMsgHandler.alloc().init()
        self._handler._callback = self._on_msg

        cfg = _WKWebViewConfig.alloc().init()
        ucc = _WKUserCC.alloc().init()
        ucc.addScriptMessageHandler_name_(self._handler, "obsMonitor")
        cfg.setUserContentController_(ucc)

        scr = AppKit.NSScreen.mainScreen().visibleFrame()
        W   = min(640, scr.size.width  * 0.85)
        H   = min(720, scr.size.height * 0.90)
        px  = scr.origin.x + (scr.size.width  - W) / 2
        py  = scr.origin.y + (scr.size.height - H) / 2

        self._webview = _WKWebView.alloc().initWithFrame_configuration_(
            Foundation.NSMakeRect(0, 0, W, H), cfg
        )
        self._webview.loadHTMLString_baseURL_(html, None)

        style = (AppKit.NSWindowStyleMaskTitled |
                 AppKit.NSWindowStyleMaskClosable |
                 AppKit.NSWindowStyleMaskResizable)
        self._panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(px, py, W, H),
            style, AppKit.NSBackingStoreBuffered, False,
        )
        self._panel.setTitle_("⚙️  Configuration OBS Monitor")
        self._panel.setLevel_(AppKit.NSFloatingWindowLevel + 3)
        self._panel.setHidesOnDeactivate_(False)
        self._panel.setContentView_(self._webview)
        self._panel.makeKeyAndOrderFront_(None)

    # ── HTML generator ────────────────────────────────────────────────────────

    def _gen_html(self, sections):
        css = f"""
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --bg:      {BG};
  --bg2:     {BG2};
  --bg3:     {BG3};
  --surface: #1e1e30;
  --surf2:   #252540;
  --border:  rgba(255,255,255,0.08);
  --accent:  {ACCENT};
  --green:   {GREEN};
  --red:     {RED};
  --yellow:  {YELLOW};
  --fg:      {FG};
  --fg2:     #a6adc8;
  --fg3:     {FG2};
  --r:       10px;
  --rsm:     6px;
}}
html, body {{ height: 100%; display: flex; flex-direction: column; }}
body {{
  font-family: -apple-system, 'SF Pro Text', sans-serif;
  background: var(--bg);
  color: var(--fg);
  font-size: 13px;
  line-height: 1.4;
}}
.content {{
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}}
.content::-webkit-scrollbar {{ width: 8px; }}
.content::-webkit-scrollbar-track {{ background: rgba(255,255,255,0.04); border-radius: 4px; }}
.content::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.25); border-radius: 4px; }}
.content::-webkit-scrollbar-thumb:hover {{ background: rgba(255,255,255,0.4); }}
.section {{
  background: var(--bg2);
  border-radius: var(--r);
  border: 1px solid var(--border);
  overflow: hidden;
  flex-shrink: 0;
}}
.section-header {{
  padding: 8px 14px 6px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--accent);
  border-bottom: 1px solid var(--border);
  background: rgba(122,162,247,0.06);
}}
.section-body {{ padding: 2px 0; }}
.row {{
  display: flex;
  align-items: center;
  padding: 8px 14px;
  gap: 10px;
  border-bottom: 1px solid var(--border);
}}
.row:last-child {{ border-bottom: none; }}
.row:hover {{ background: rgba(255,255,255,0.02); }}
.row-label {{ flex: 1; color: var(--fg); font-size: 13px; }}
.row-label small {{ display: block; color: var(--fg3); font-size: 11px; margin-top: 1px; }}
/* Toggle */
.toggle-wrap {{ display: flex; align-items: center; gap: 10px; width: 100%; cursor: pointer; }}
.toggle {{ position: relative; width: 36px; height: 20px; flex-shrink: 0; }}
.toggle input {{ display: none; }}
.toggle-track {{
  position: absolute; inset: 0;
  border-radius: 10px;
  background: var(--fg3);
  transition: background 0.2s;
  cursor: pointer;
}}
.toggle input:checked ~ .toggle-track {{ background: var(--accent); }}
.toggle-thumb {{
  position: absolute; top: 3px; left: 3px;
  width: 14px; height: 14px;
  border-radius: 50%;
  background: white;
  transition: transform 0.2s;
  pointer-events: none;
}}
.toggle input:checked ~ .toggle-track .toggle-thumb {{ transform: translateX(16px); }}
/* Stepper */
.stepper {{ display: flex; align-items: center; gap: 5px; flex-shrink: 0; }}
.stepper-val {{
  width: 52px; text-align: center;
  background: var(--surf2);
  border: 1px solid var(--border);
  border-radius: var(--rsm);
  color: var(--fg);
  font-size: 13px; font-weight: 600;
  padding: 4px 6px; outline: none;
}}
.stepper-val:focus {{ border-color: var(--accent); }}
.stepper-btns {{ display: flex; flex-direction: column; gap: 2px; }}
.stepper-btn {{
  width: 20px; height: 13px;
  background: var(--surf2);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--fg2); font-size: 8px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  user-select: none; transition: background 0.1s;
}}
.stepper-btn:hover {{ background: var(--accent); color: white; border-color: var(--accent); }}
.stepper-unit {{ color: var(--fg3); font-size: 12px; min-width: 44px; }}
/* Days */
.days-wrap {{ display: flex; gap: 4px; flex: 1; }}
.day-btn {{
  flex: 1; padding: 5px 2px;
  border-radius: var(--rsm);
  border: 1px solid var(--border);
  background: var(--surf2);
  color: var(--fg3); font-size: 10.5px; font-weight: 700;
  text-align: center; cursor: pointer;
  transition: all 0.15s; user-select: none;
}}
.day-btn.active {{ background: var(--accent); border-color: var(--accent); color: white; }}
.day-btn:hover:not(.active) {{ border-color: var(--accent); color: var(--fg); }}
/* Time / Text */
.time-field {{
  width: 68px; text-align: center;
  background: var(--surf2); border: 1px solid var(--border);
  border-radius: var(--rsm); color: var(--fg);
  font-size: 13px; font-weight: 600;
  padding: 4px 6px; outline: none;
}}
.time-field:focus {{ border-color: var(--accent); }}
.text-field {{
  flex: 1; background: var(--surf2);
  border: 1px solid var(--border);
  border-radius: var(--rsm); color: var(--fg);
  font-size: 12px; padding: 5px 10px; outline: none;
  font-family: 'SF Mono', monospace;
}}
.text-field:focus {{ border-color: var(--accent); }}
.text-field::placeholder {{ color: var(--fg3); }}
/* Badges */
.badge-off {{
  font-size: 10px; padding: 2px 7px;
  border-radius: 20px;
  background: rgba(247,118,142,0.15); color: var(--red);
  font-weight: 600; letter-spacing: 0.03em; flex-shrink: 0;
}}
.badge-on {{
  font-size: 10px; padding: 2px 7px;
  border-radius: 20px;
  background: rgba(158,206,106,0.15); color: var(--green);
  font-weight: 600; letter-spacing: 0.03em; flex-shrink: 0;
}}
/* Footer */
.footer {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
  flex-shrink: 0;
}}
.btn {{
  padding: 7px 20px; border-radius: var(--rsm);
  font-size: 13px; font-weight: 500;
  cursor: pointer; border: 1px solid var(--border);
  transition: all 0.15s;
}}
.btn-cancel {{ background: transparent; color: var(--fg2); }}
.btn-cancel:hover {{ background: var(--surf2); color: var(--fg); }}
.btn-save {{ background: var(--accent); color: white; border-color: var(--accent); }}
.btn-save:hover {{ opacity: 0.85; }}
"""
        js = """
function toggleBadge(key, checked) {
  const b = document.getElementById('badge-' + key);
  if (!b) return;
  b.className = checked ? 'badge-on' : 'badge-off';
  b.textContent = checked ? 'ACTIF' : 'INACTIF';
}
function step(btn, delta) {
  const inp = btn.closest('.stepper').querySelector('.stepper-val');
  inp.value = (parseInt(inp.value) || 0) + delta;
}
document.querySelectorAll('.day-btn').forEach(b => {
  b.addEventListener('click', () => b.classList.toggle('active'));
});
document.querySelectorAll('input[data-type="toggle"]').forEach(cb => {
  cb.addEventListener('change', () => toggleBadge(cb.dataset.key, cb.checked));
});
function getVals() {
  const v = {};
  document.querySelectorAll('input[data-type="toggle"]').forEach(el => {
    v[el.dataset.key] = el.checked;
  });
  document.querySelectorAll('input[data-type="stepper"]').forEach(el => {
    v[el.dataset.key] = parseInt(el.value) || 0;
  });
  document.querySelectorAll('input[data-type="time"]').forEach(el => {
    v[el.dataset.key] = el.value;
  });
  document.querySelectorAll('input[data-type="text"]').forEach(el => {
    v[el.dataset.key] = el.value;
  });
  document.querySelectorAll('.days-wrap[data-type="days"]').forEach(wrap => {
    const active = [...wrap.querySelectorAll('.day-btn.active')].map(b => b.dataset.day);
    v[wrap.dataset.key] = active.join(',') || 'lun,mar,mer,jeu,ven,sam,dim';
  });
  return v;
}
function save() {
  window.webkit.messageHandlers.obsMonitor.postMessage(
    JSON.stringify({action:'save', values:getVals()})
  );
}
function cancel() {
  window.webkit.messageHandlers.obsMonitor.postMessage(JSON.stringify({action:'cancel'}));
}
"""
        secs_html = []
        for sec_title, items in sections:
            rows = []
            for item in items:
                label, key, ftype = item[0], item[1], item[2]
                val   = item[3] if len(item) > 3 else None
                extra = item[4:] if len(item) > 4 else ()

                if ftype == "toggle":
                    ch  = "checked" if val else ""
                    bon = "badge-on" if val else "badge-off"
                    btx = "ACTIF"   if val else "INACTIF"
                    rows.append(f"""<div class="row toggle-row">
  <label class="toggle-wrap">
    <div class="toggle">
      <input type="checkbox" data-key="{key}" data-type="toggle" {ch}>
      <div class="toggle-track"><div class="toggle-thumb"></div></div>
    </div>
    <span class="row-label">{label}</span>
    <span class="{bon}" id="badge-{key}">{btx}</span>
  </label>
</div>""")

                elif ftype == "stepper":
                    mn, mx, st, unit = extra[0], extra[1], extra[2], extra[3]
                    iv = int(round(float(val))) if val is not None else 0
                    rows.append(f"""<div class="row">
  <span class="row-label">{label}</span>
  <div class="stepper">
    <input class="stepper-val" data-key="{key}" data-type="stepper" value="{iv}" type="text">
    <div class="stepper-btns">
      <div class="stepper-btn" onclick="step(this,{st})">▲</div>
      <div class="stepper-btn" onclick="step(this,{-st})">▼</div>
    </div>
    <span class="stepper-unit">{unit}</span>
  </div>
</div>""")

                elif ftype == "days":
                    active = set(d.strip().lower() for d in str(val or "").split(","))
                    if not active or not any(d in self.DAY_KEYS for d in active):
                        active = set(self.DAY_KEYS)
                    btns = "".join(
                        f'<div class="day-btn{"  active" if dk in active else ""}" data-day="{dk}">{dn}</div>'
                        for dk, dn in zip(self.DAY_KEYS, self.DAYS_FR)
                    )
                    rows.append(f"""<div class="row">
  <span class="row-label" style="flex:0 0 90px;min-width:90px">{label}</span>
  <div class="days-wrap" data-key="{key}" data-type="days">{btns}</div>
</div>""")

                elif ftype == "time":
                    sv = str(val) if val else "00:00"
                    rows.append(f"""<div class="row">
  <span class="row-label">{label}</span>
  <input class="time-field" data-key="{key}" data-type="time" value="{sv}">
</div>""")

                elif ftype == "text":
                    sv = str(val) if val else ""
                    sv = sv.replace('"', '&quot;')
                    rows.append(f"""<div class="row">
  <span class="row-label">{label}</span>
  <input class="text-field" data-key="{key}" data-type="text" value="{sv}" placeholder="">
</div>""")

            secs_html.append(f"""<div class="section">
  <div class="section-header">{sec_title}</div>
  <div class="section-body">
    {"".join(rows)}
  </div>
</div>""")

        return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<style>{css}</style></head>
<body>
<div class="content">{"".join(secs_html)}</div>
<div class="footer">
  <button class="btn btn-cancel" onclick="cancel()">Annuler</button>
  <button class="btn btn-save" onclick="save()">Enregistrer ✓</button>
</div>
<script>{js}</script>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# _SettingsPanel — panneau de configuration natif scrollable (fallback)
# Contrôles adaptés : toggles, steppers entiers, sélecteur de jours, heures
# ─────────────────────────────────────────────────────────────────────────────

class _SettingsPanel:
    """
    Panneau de configuration natif avec :
    - toggle  : checkbox ON/OFF
    - stepper : entier avec boutons +/− et unité
    - days    : 7 boutons jour cochables
    - time    : champ HH:MM
    - text    : champ texte libre
    Scrollable pour les longues listes de paramètres.
    """
    W      = 580
    PAD    = 20
    ROW_H  = 36
    DAYS_H = 48
    SEC_H  = 26
    GAP    = 10
    BTN_H  = 32

    DAYS_FR   = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    _DAY_MAP  = {"lun":0,"mar":1,"mer":2,"jeu":3,"ven":4,"sam":5,"dim":6}
    _DAY_KEYS = ["lun","mar","mer","jeu","ven","sam","dim"]

    def __init__(self, title):
        self._title   = title
        self._panel   = None
        self._ctrls   = {}
        self._targets = []
        self._on_save = None

    def _parse_days(self, days_str):
        s = set()
        for d in str(days_str).split(","):
            d = d.strip().lower()
            if d in self._DAY_MAP:
                s.add(self._DAY_MAP[d])
        return s if s else set(range(7))

    def _encode_days(self, days_set):
        return ",".join(self._DAY_KEYS[i] for i in range(7) if i in days_set) or "lun,mar,mer,jeu,ven,sam,dim"

    def show(self, sections, on_save):
        """
        sections : list of (section_title, items)
        item : (label, key, field_type, current_value, *extra)
          field_type "toggle"  : bool
          field_type "stepper" : extra = (min, max, step, unit_str)
          field_type "days"    : str "lun,mar,..."
          field_type "time"    : str "HH:MM"
          field_type "text"    : str
        """
        if self._panel and self._panel.isVisible():
            self._panel.makeKeyAndOrderFront_(None)
            return

        self._ctrls   = {}
        self._targets = []
        self._on_save = on_save

        W = self.W

        # Hauteur totale du contenu
        content_h = self.PAD
        for _, items in sections:
            content_h += self.SEC_H + 6
            for item in items:
                content_h += self.DAYS_H if item[2] == "days" else self.ROW_H
            content_h += self.GAP
        content_h += self.PAD

        scr      = AppKit.NSScreen.mainScreen().visibleFrame()
        panel_h  = min(content_h + 60, scr.size.height * 0.90)
        px       = scr.origin.x + (scr.size.width  - W)       / 2
        py       = scr.origin.y + (scr.size.height - panel_h) / 2

        style = (AppKit.NSWindowStyleMaskTitled |
                 AppKit.NSWindowStyleMaskClosable |
                 AppKit.NSWindowStyleMaskResizable)
        self._panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(px, py, W, panel_h),
            style, AppKit.NSBackingStoreBuffered, False,
        )
        self._panel.setTitle_(self._title)
        self._panel.setLevel_(AppKit.NSFloatingWindowLevel + 3)
        self._panel.setHidesOnDeactivate_(False)

        cv       = self._panel.contentView()
        BTN_AREA = 52
        scroll_h = panel_h - BTN_AREA

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, BTN_AREA, W, scroll_h)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(AppKit.NSNoBorder)

        inner_h = max(content_h, scroll_h)
        inner   = _FlippedView.alloc().initWithFrame_(Foundation.NSMakeRect(0, 0, W, inner_h))

        y = self.PAD

        for sec_title, items in sections:
            # ── En-tête de section ────────────────────────────────────
            sh = AppKit.NSTextField.alloc().initWithFrame_(
                Foundation.NSMakeRect(self.PAD, y, W - 2*self.PAD, self.SEC_H)
            )
            sh.setStringValue_(sec_title)
            sh.setTextColor_(_hex_to_nscolor(ACCENT))
            sh.setFont_(AppKit.NSFont.boldSystemFontOfSize_(11))
            sh.setBezeled_(False); sh.setEditable_(False); sh.setSelectable_(False)
            sh.setDrawsBackground_(False)
            inner.addSubview_(sh)
            y += self.SEC_H + 4

            for item in items:
                label, key, ftype = item[0], item[1], item[2]
                val   = item[3] if len(item) > 3 else None
                extra = item[4:] if len(item) > 4 else ()

                if ftype == "toggle":
                    cb = AppKit.NSButton.alloc().initWithFrame_(
                        Foundation.NSMakeRect(self.PAD, y + 4, W - 2*self.PAD, self.ROW_H - 6)
                    )
                    cb.setButtonType_(AppKit.NSButtonTypeSwitch)
                    cb.setTitle_("  " + label)
                    cb.setFont_(AppKit.NSFont.systemFontOfSize_(13))
                    cb.setState_(AppKit.NSOnState if val else AppKit.NSOffState)
                    inner.addSubview_(cb)
                    self._ctrls[key] = ("toggle", cb)
                    y += self.ROW_H

                elif ftype == "stepper":
                    s_min, s_max, s_step, unit = extra[0], extra[1], extra[2], extra[3]
                    ival    = int(round(float(val))) if val is not None else 0
                    lbl_w   = W - 2*self.PAD - 148
                    row_cy  = y + (self.ROW_H - 22) // 2

                    ll = AppKit.NSTextField.alloc().initWithFrame_(
                        Foundation.NSMakeRect(self.PAD, y, lbl_w, self.ROW_H)
                    )
                    ll.setStringValue_(label)
                    ll.setFont_(AppKit.NSFont.systemFontOfSize_(13))
                    ll.setBezeled_(False); ll.setEditable_(False); ll.setSelectable_(False)
                    ll.setDrawsBackground_(False)
                    ll.setTextColor_(AppKit.NSColor.labelColor())
                    ll.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
                    inner.addSubview_(ll)

                    val_x = self.PAD + lbl_w + 8
                    tf = AppKit.NSTextField.alloc().initWithFrame_(
                        Foundation.NSMakeRect(val_x, row_cy, 55, 22)
                    )
                    tf.setStringValue_(str(ival))
                    tf.setFont_(AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                        13, AppKit.NSFontWeightMedium))
                    tf.setAlignment_(AppKit.NSTextAlignmentCenter)
                    inner.addSubview_(tf)

                    sp = AppKit.NSStepper.alloc().initWithFrame_(
                        Foundation.NSMakeRect(val_x + 58, row_cy, 19, 22)
                    )
                    sp.setMinValue_(s_min); sp.setMaxValue_(s_max)
                    sp.setIncrement_(s_step); sp.setIntValue_(ival)
                    sp.setValueWraps_(False)
                    tgt = _ActionTarget.alloc().init()
                    _tf = tf
                    tgt._callback = lambda s, f=_tf: f.setStringValue_(str(int(s.intValue())))
                    self._targets.append(tgt)
                    sp.setTarget_(tgt); sp.setAction_("action:")
                    inner.addSubview_(sp)

                    ul = AppKit.NSTextField.alloc().initWithFrame_(
                        Foundation.NSMakeRect(val_x + 80, y, W - val_x - 80 - self.PAD, self.ROW_H)
                    )
                    ul.setStringValue_(unit)
                    ul.setFont_(AppKit.NSFont.systemFontOfSize_(12))
                    ul.setTextColor_(AppKit.NSColor.secondaryLabelColor())
                    ul.setBezeled_(False); ul.setEditable_(False); ul.setSelectable_(False)
                    ul.setDrawsBackground_(False)
                    inner.addSubview_(ul)

                    self._ctrls[key] = ("stepper", tf, sp)
                    y += self.ROW_H

                elif ftype == "days":
                    days_set = self._parse_days(val)
                    avail_w  = W - 2*self.PAD
                    btn_w    = (avail_w - 6*6) / 7
                    day_btns = []
                    for di, dname in enumerate(self.DAYS_FR):
                        bx = self.PAD + di * (btn_w + 6)
                        db = AppKit.NSButton.alloc().initWithFrame_(
                            Foundation.NSMakeRect(bx, y + 6, btn_w, 34)
                        )
                        db.setButtonType_(AppKit.NSButtonTypeOnOff)
                        db.setBezelStyle_(AppKit.NSBezelStyleRounded)
                        db.setTitle_(dname)
                        db.setFont_(AppKit.NSFont.boldSystemFontOfSize_(12))
                        db.setState_(AppKit.NSOnState if di in days_set else AppKit.NSOffState)
                        inner.addSubview_(db)
                        day_btns.append(db)
                    self._ctrls[key] = ("days", day_btns)
                    y += self.DAYS_H

                elif ftype == "time":
                    lbl_w  = W - 2*self.PAD - 82
                    row_cy = y + (self.ROW_H - 22) // 2
                    ll = AppKit.NSTextField.alloc().initWithFrame_(
                        Foundation.NSMakeRect(self.PAD, y, lbl_w, self.ROW_H)
                    )
                    ll.setStringValue_(label)
                    ll.setFont_(AppKit.NSFont.systemFontOfSize_(13))
                    ll.setBezeled_(False); ll.setEditable_(False); ll.setSelectable_(False)
                    ll.setDrawsBackground_(False)
                    ll.setTextColor_(AppKit.NSColor.labelColor())
                    inner.addSubview_(ll)
                    tf = AppKit.NSTextField.alloc().initWithFrame_(
                        Foundation.NSMakeRect(self.PAD + lbl_w + 8, row_cy, 74, 22)
                    )
                    tf.setStringValue_(str(val) if val else "00:00")
                    tf.setFont_(AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                        13, AppKit.NSFontWeightMedium))
                    tf.setAlignment_(AppKit.NSTextAlignmentCenter)
                    tf.cell().setPlaceholderString_("HH:MM")
                    inner.addSubview_(tf)
                    self._ctrls[key] = ("time", tf)
                    y += self.ROW_H

                elif ftype == "text":
                    lbl_w  = 190
                    row_cy = y + (self.ROW_H - 22) // 2
                    ll = AppKit.NSTextField.alloc().initWithFrame_(
                        Foundation.NSMakeRect(self.PAD, y, lbl_w, self.ROW_H)
                    )
                    ll.setStringValue_(label)
                    ll.setFont_(AppKit.NSFont.systemFontOfSize_(13))
                    ll.setBezeled_(False); ll.setEditable_(False); ll.setSelectable_(False)
                    ll.setDrawsBackground_(False)
                    ll.setTextColor_(AppKit.NSColor.labelColor())
                    inner.addSubview_(ll)
                    tf = AppKit.NSTextField.alloc().initWithFrame_(
                        Foundation.NSMakeRect(self.PAD + lbl_w + 8, row_cy,
                                             W - 2*self.PAD - lbl_w - 8, 22)
                    )
                    tf.setStringValue_(str(val) if val else "")
                    tf.setFont_(AppKit.NSFont.systemFontOfSize_(13))
                    inner.addSubview_(tf)
                    self._ctrls[key] = ("text", tf)
                    y += self.ROW_H

            y += self.GAP

        y += self.PAD
        inner.setFrame_(Foundation.NSMakeRect(0, 0, W, max(y, scroll_h)))
        scroll.setDocumentView_(inner)
        cv.addSubview_(scroll)

        # ── Boutons fixes en bas ───────────────────────────────────────
        cancel = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(self.PAD, 10, 110, self.BTN_H)
        )
        cancel.setTitle_("Annuler")
        cancel.setBezelStyle_(AppKit.NSBezelStyleRounded)
        ct = _ActionTarget.alloc().init()
        ct._callback = lambda _: self._panel.orderOut_(None)
        self._targets.append(ct)
        cancel.setTarget_(ct); cancel.setAction_("action:")
        cv.addSubview_(cancel)

        save = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(W - self.PAD - 130, 10, 130, self.BTN_H)
        )
        save.setTitle_("Enregistrer ✓")
        save.setBezelStyle_(AppKit.NSBezelStyleRounded)
        save.setKeyEquivalent_("\r")
        st = _ActionTarget.alloc().init()
        st._callback = lambda _: self._do_save()
        self._targets.append(st)
        save.setTarget_(st); save.setAction_("action:")
        cv.addSubview_(save)

        self._panel.makeKeyAndOrderFront_(None)
        self._panel.orderFrontRegardless()

    def _do_save(self):
        vals = {}
        for key, info in self._ctrls.items():
            t = info[0]
            if t == "toggle":
                vals[key] = (info[1].state() == AppKit.NSOnState)
            elif t == "stepper":
                try:
                    vals[key] = int(info[1].stringValue())
                except Exception:
                    vals[key] = int(info[2].intValue())
            elif t == "days":
                active = {di for di, b in enumerate(info[1]) if b.state() == AppKit.NSOnState}
                vals[key] = self._encode_days(active)
            elif t == "time":
                vals[key] = info[1].stringValue().strip() or "00:00"
            elif t == "text":
                vals[key] = info[1].stringValue().strip()
        if self._on_save:
            self._on_save(vals)
        if self._panel:
            self._panel.orderOut_(None)

    def close(self):
        if self._panel:
            self._panel.orderOut_(None)


# ─────────────────────────────────────────────────────────────────────────────
# Native NSPanel — floating panel above OBS Projector
# ─────────────────────────────────────────────────────────────────────────────

class NativePanel:
    """
    Floating NSPanel using AppKit. Uses NSWindowStyleMaskNonactivatingPanel
    so it can appear above OBS Projector (Metal rendering).
    Contains: status, source selection checkboxes, monitoring info, issues list.
    """
    W = 420
    PANEL_H = 720

    def __init__(self):
        self._panel = None
        self._text_view = None
        self._status_field = None
        self._scene_field  = None
        self._update_field = None
        self._info_field = None
        self._lock = threading.Lock()
        self._built = False
        # Popup buttons per source : (name, NSPopUpButton, _ActionTarget)
        self._audio_popups = []
        self._video_popups = []
        self._dynamic_views = []  # all views below fixed header — rebuilt on source change
        self._save_callback = None
        self._scene_choice_cb = None  # callback(kind:str, name:str, choice:str)
        self._last_audio_names = []
        self._last_video_names = []
        self._last_scene = None
        self._last_all_scenes = []
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

        # Style mask: titled + closable + miniaturizable + resizable + non-activating panel
        style = (
            AppKit.NSWindowStyleMaskTitled |
            AppKit.NSWindowStyleMaskClosable |
            AppKit.NSWindowStyleMaskMiniaturizable |
            AppKit.NSWindowStyleMaskResizable |
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
        self._panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        self._panel.setAlphaValue_(1.0)
        self._panel.setMinSize_(Foundation.NSMakeSize(self.W, 300))
        self._panel.setSharingType_(1)  # NSWindowSharingReadOnly

        # ── Frosted glass background (NSVisualEffectView) ──
        content = self._panel.contentView()
        cf = content.frame()
        cw, ch = cf.size.width, cf.size.height

        try:
            effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(
                Foundation.NSMakeRect(0, 0, cw, ch)
            )
            effect.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
            )
            effect.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
            effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
            effect.setState_(AppKit.NSVisualEffectStateActive)
            content.addSubview_(effect)
        except Exception:
            # Fallback : solid dark bg
            self._panel.setOpaque_(True)
            self._panel.setBackgroundColor_(_hex_to_nscolor(BG))

        # ── Main scroll view wrapping all content ──
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, cw, ch)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
        )
        scroll.setDrawsBackground_(False)

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
        y = 14
        pad = 14   # marge horizontale

        # ── Carte STATUT (fond) ──
        card_h = 78
        self._make_card(doc, pad, y, cw - 2*pad, card_h, bg_hex=BG2, corner=12)

        # Statut connexion (au-dessus, en coord doc flipped)
        self._status_field = self._make_label(
            doc, pad + 14, y + 14, cw - 2*pad - 28, 22,
            "● Connexion à OBS…", ORANGE, 15, bold=True
        )

        # Scène OBS courante
        self._scene_field = self._make_label(
            doc, pad + 14, y + 44, cw - 2*pad - 28, 20,
            "Scène : —", FG2, 12, bold=False
        )
        y += card_h + 12

        # ── Notification update (masquée par défaut) ──
        self._update_field = self._make_label(
            doc, pad + 4, y, cw - 2*pad - 8, 18,
            "", GREEN, 12, bold=True
        )
        self._update_field.setHidden_(True)
        y += 22

        self._header_end_y = y

        # Build initial dynamic content (placeholders)
        self._rebuild_dynamic([], [], None, [])

    def _rebuild_dynamic(self, audio_names, video_names, cfg, all_scenes=None):
        """Rebuild all content below the fixed header (sources + popups, info, alerts)."""
        _dlog(f"[panel.rebuild_dynamic] audio={list(audio_names)} video={list(video_names)}")
        doc = self._doc
        cw = self._doc_width

        # Remove all previous dynamic views
        for v in self._dynamic_views:
            try:
                v.removeFromSuperview()
            except Exception:
                pass
        for tup in self._audio_popups + self._video_popups:
            try:
                if tup and len(tup) > 1 and tup[1] is not None:
                    tup[1].removeFromSuperview()
            except Exception:
                pass
        self._dynamic_views = []
        self._audio_popups = []
        self._video_popups = []

        y = self._header_end_y
        pad = 14
        all_scenes = list(all_scenes or [])

        legacy_scenes = {}
        audio_scenes  = {}
        video_scenes  = {}
        if cfg:
            legacy_scenes = cfg.get("source_scenes", {}) or {}
            audio_scenes  = cfg.get("audio_source_scenes", {}) or {}
            video_scenes  = cfg.get("video_source_scenes", {}) or {}

        def _current_spec(name, kind):
            """Resolve spec for (name, kind) : audio/video-specific then legacy."""
            d = audio_scenes if kind == "audio" else video_scenes
            if name in d:
                return d[name]
            if name in legacy_scenes:
                return legacy_scenes[name]
            return "*"

        def _make_section_card(title_emoji, title_text, title_color, contents_height):
            """Crée une section card avec header (emoji + titre) et renvoie y_content_start."""
            nonlocal y
            header_h  = 30
            total_h   = header_h + contents_height + 12
            card = self._make_card(doc, pad, y, cw - 2*pad, total_h, bg_hex=BG2, corner=10)
            self._dynamic_views.append(card)
            title_lbl = self._make_label(
                doc, pad + 14, y + 9, cw - 2*pad - 28, 18,
                f"{title_emoji}  {title_text}", title_color, 11, bold=True
            )
            self._dynamic_views.append(title_lbl)
            return y + header_h

        def _make_source_row(name, kind, cy):
            """Cree une ligne : [checkbox] [label source] [popup choix scene].
            spec '' = checkbox OFF + popup grise
            spec '*' = checkbox ON + popup "Toutes les scenes"
            spec '<scene>' = checkbox ON + popup "<scene>" """
            current = _current_spec(name, kind)
            is_active = (current != "")

            cb_x  = pad + 14
            cb_w  = 22
            lbl_x = cb_x + cb_w + 6
            pop_w = 142
            pop_x = cw - pad - pop_w - 4
            lbl_w = max(60, pop_x - lbl_x - 6)

            # CHECKBOX actif/inactif
            cb = None
            cb_target = None
            try:
                cb = AppKit.NSButton.alloc().initWithFrame_(
                    Foundation.NSMakeRect(cb_x, cy + 2, cb_w, 22)
                )
                cb.setButtonType_(AppKit.NSButtonTypeSwitch)
                cb.setTitle_("")
                cb.setState_(AppKit.NSControlStateValueOn if is_active else AppKit.NSControlStateValueOff)
                doc.addSubview_(cb)
            except Exception as e:
                _dlog(f"[panel.row.cb_init] {name!r} : {e}")

            # LABEL source
            try:
                lbl = self._make_label(doc, lbl_x, cy + 4, lbl_w, 20, name, FG, 12, bold=False)
                self._dynamic_views.append(lbl)
            except Exception as e:
                _dlog(f"[panel.row.label] {name!r} : {e}")

            # POPUP choix scene
            pop = None
            try:
                pop = AppKit.NSPopUpButton.alloc().initWithFrame_(
                    Foundation.NSMakeRect(pop_x, cy, pop_w, 26)
                )
                pop.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            except Exception as e:
                _dlog(f"[panel.row.popup_init] {name!r} : {e}")

            if pop is not None:
                for title in (["Toutes les sc\u00e8nes"] + list(all_scenes)):
                    try:
                        pop.addItemWithTitle_(title)
                    except Exception as e:
                        _dlog(f"[panel.row.popup_item] {name!r} {title!r} : {e}")
                try:
                    if current in ("", "*") or not current:
                        pop.selectItemWithTitle_("Toutes les sc\u00e8nes")
                    else:
                        pop.selectItemWithTitle_(current)
                        if pop.indexOfSelectedItem() < 0:
                            pop.selectItemWithTitle_("Toutes les sc\u00e8nes")
                    pop.setEnabled_(is_active)
                except Exception as e:
                    _dlog(f"[panel.row.popup_sel] {name!r} : {e}")
                try:
                    doc.addSubview_(pop)
                except Exception as e:
                    _dlog(f"[panel.row.popup_add] {name!r} : {e}")

            # ── Callbacks ──
            # Checkbox : ON -> "*" (ou scene actuelle si selectionnee dans popup), OFF -> ""
            if cb is not None:
                try:
                    cb_target = _ActionTarget.alloc().init()
                    cb_target._callback = (
                        lambda sender, _n=name, _k=kind, _p=pop:
                        self._on_source_active_toggled(_k, _n, sender, _p)
                    )
                    cb.setTarget_(cb_target)
                    cb.setAction_("action:")
                except Exception as e:
                    _dlog(f"[panel.row.cb_action] {name!r} : {e}")

            # Popup : change scene (uniquement si actif)
            pop_target = None
            if pop is not None:
                try:
                    pop_target = _ActionTarget.alloc().init()
                    pop_target._callback = (
                        lambda sender, _n=name, _k=kind:
                        self._on_scene_choice(_k, _n, sender.titleOfSelectedItem())
                    )
                    pop.setTarget_(pop_target)
                    pop.setAction_("action:")
                except Exception as e:
                    _dlog(f"[panel.row.popup_action] {name!r} : {e}")

            # On garde toutes les refs (cb, pop, targets) pour eviter le GC
            return (name, pop, (cb, cb_target, pop_target))

        # ── Carte SOURCES AUDIO ──
        audio_h = max(28, len(audio_names) * 30 + 4) if audio_names else 28
        content_y = _make_section_card("🎤", "SOURCES AUDIO", ACCENT, audio_h)
        if audio_names:
            cy = content_y
            for name in audio_names:
                try:
                    self._audio_popups.append(_make_source_row(name, "audio", cy))
                except Exception as e:
                    _dlog(f"[panel.row] audio {name!r} : {e}")
                cy += 30
        else:
            lbl = self._make_label(doc, pad + 18, content_y + 4, cw - 2*pad - 36, 18,
                                   "En attente de connexion…", FG2, 11, bold=False)
            self._dynamic_views.append(lbl)
        y += 30 + audio_h + 12 + 10

        # ── Carte SOURCES VIDÉO ──
        video_h = max(28, len(video_names) * 30 + 4) if video_names else 28
        content_y = _make_section_card("📷", "SOURCES VIDÉO", ACCENT, video_h)
        if video_names:
            cy = content_y
            for name in video_names:
                try:
                    self._video_popups.append(_make_source_row(name, "video", cy))
                except Exception as e:
                    _dlog(f"[panel.row] video {name!r} : {e}")
                cy += 30
        else:
            lbl = self._make_label(doc, pad + 18, content_y + 4, cw - 2*pad - 36, 18,
                                   "En attente de connexion…", FG2, 11, bold=False)
            self._dynamic_views.append(lbl)
        y += 30 + video_h + 12 + 6

        # ── Hint sous les sources ──
        hint = self._make_label(
            doc, pad + 4, y, cw - 2*pad - 8, 14,
            "Pour chaque source : choisis la scène où la surveiller", FG2, 10, bold=False
        )
        self._dynamic_views.append(hint)
        y += 22

        # ── Carte SURVEILLANCE ──
        survey_h = 90
        content_y = _make_section_card("🔍", "SURVEILLANCE", CYAN, survey_h)
        self._info_field = AppKit.NSTextView.alloc().initWithFrame_(
            Foundation.NSMakeRect(pad + 14, content_y, cw - 2*pad - 28, survey_h - 4)
        )
        self._info_field.setEditable_(False)
        self._info_field.setSelectable_(False)
        self._info_field.setRichText_(True)
        self._info_field.setDrawsBackground_(False)
        self._info_field.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        self._info_field.setTextColor_(_hex_to_nscolor(FG2))
        doc.addSubview_(self._info_field)
        self._dynamic_views.append(self._info_field)
        y += 30 + survey_h + 12 + 8

        # ── Carte ALERTES ──
        alerts_h = 240
        content_y = _make_section_card("⚠️", "ALERTES", RED, alerts_h)
        self._text_view = AppKit.NSTextView.alloc().initWithFrame_(
            Foundation.NSMakeRect(pad + 14, content_y, cw - 2*pad - 28, alerts_h - 4)
        )
        self._text_view.setEditable_(False)
        self._text_view.setSelectable_(True)
        self._text_view.setRichText_(True)
        self._text_view.setDrawsBackground_(False)
        self._text_view.setTextContainerInset_(Foundation.NSMakeSize(6, 6))
        self._text_view.textContainer().setWidthTracksTextView_(True)
        self._text_view.setHorizontallyResizable_(False)
        doc.addSubview_(self._text_view)
        self._dynamic_views.append(self._text_view)
        y += 30 + alerts_h + 12 + 10

        doc.setFrameSize_(Foundation.NSMakeSize(cw, max(y + 14, 680)))

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
            Foundation.NSMakeRect(20, y, cw - 40, 20)
        )
        cb.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb.setTitle_(name)
        cb.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        cb.setState_(AppKit.NSControlStateValueOn if checked else AppKit.NSControlStateValueOff)
        cell = cb.cell()
        if cell and hasattr(cell, 'setAttributedTitle_'):
            attrs = {
                AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(FG),
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(12),
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

    # ── Visual helpers : cartes arrondies + pills ──────────────────────────

    def _make_card(self, parent, x, y, w, h, bg_hex=None, corner=10):
        """Crée une carte arrondie (NSView layer-backed) qui sert de fond.
        Le contenu doit être ajouté SEPAREMENT à `parent` (au-dessus) avec les
        mêmes coordonnées dans `parent` (flipped), pour rester aligné."""
        v = AppKit.NSView.alloc().initWithFrame_(
            Foundation.NSMakeRect(x, y, w, h)
        )
        v.setWantsLayer_(True)
        if bg_hex:
            v.layer().setBackgroundColor_(_hex_to_nscolor(bg_hex).CGColor())
        v.layer().setCornerRadius_(corner)
        parent.addSubview_(v)
        return v

    def _make_pill(self, parent, x, y, w, h, text, bg_hex, fg_hex, font_size=12, bold=True):
        """Crée un badge type pill (rectangle arrondi avec texte centré)."""
        pill = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(x, y, w, h)
        )
        pill.setStringValue_("  " + text + "  ")
        pill.setBezeled_(False)
        pill.setEditable_(False)
        pill.setSelectable_(False)
        pill.setDrawsBackground_(False)
        pill.setAlignment_(AppKit.NSTextAlignmentLeft)
        attrs = {
            AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(fg_hex),
            AppKit.NSFontAttributeName: (
                AppKit.NSFont.boldSystemFontOfSize_(font_size) if bold
                else AppKit.NSFont.systemFontOfSize_(font_size)
            ),
        }
        astr = Foundation.NSAttributedString.alloc().initWithString_attributes_(
            "  " + text + "  ", attrs
        )
        pill.setAttributedStringValue_(astr)
        pill.setWantsLayer_(True)
        pill.layer().setBackgroundColor_(_hex_to_nscolor(bg_hex).CGColor())
        pill.layer().setCornerRadius_(h / 2.0)
        parent.addSubview_(pill)
        return pill

    def _make_stripe(self, parent, x, y, w, h, color_hex):
        """Petite barre verticale colorée (pour le côté des alertes)."""
        v = AppKit.NSView.alloc().initWithFrame_(
            Foundation.NSMakeRect(x, y, w, h)
        )
        v.setWantsLayer_(True)
        v.layer().setBackgroundColor_(_hex_to_nscolor(color_hex).CGColor())
        v.layer().setCornerRadius_(w / 2.0)
        parent.addSubview_(v)
        return v

    # ── Source checkboxes (dynamic) ──

    def refresh_sources(self, audio_names, video_names, cfg, scene_name=None, all_scenes=None):
        """Rebuild source popups and all dynamic content when sources/scene/all_scenes change."""
        if not self._doc:
            return
        # MAJ du champ scene meme si rien d'autre ne change
        if self._scene_field:
            try:
                self._scene_field.setStringValue_(f"Scène : {scene_name or '—'}")
            except Exception:
                pass
        all_scenes = list(all_scenes or [])
        # DIAGNOSTIC v2.5.56 : on retire le check "no change" et on force rebuild
        # systematique tant que le bug "En attente" n'est pas resolu. Coût : un
        # rebuild toutes les 3s.
        _dlog(f"[panel.refresh] audio={list(audio_names)} video={list(video_names)} "
              f"scene={scene_name!r} all_scenes={all_scenes}")
        # IMPORTANT : on ne marque comme "applique" qu'apres un rebuild reussi.
        # Sinon une exception silencieuse laisse l'UI bloquee a l'etat precedent
        # (= placeholder "En attente") pour toutes les refresh suivantes.
        try:
            self._rebuild_dynamic(audio_names, video_names, cfg, all_scenes)
            self._last_audio_names = list(audio_names)
            self._last_video_names = list(video_names)
            self._last_scene = scene_name
            self._last_all_scenes = all_scenes
        except Exception as e:
            import traceback
            _dlog(f"[panel.rebuild] EXCEPTION : {e}")
            print(traceback.format_exc())
            # On NE met PAS a jour _last_* → la prochaine refresh re-tentera

    def set_scene_choice_callback(self, cb):
        """Stocke le callback appele quand l'utilisateur change le dropdown
        de scope d'une source. Signature : cb(kind:str, name:str, choice:str)."""
        self._scene_choice_cb = cb

    def _on_scene_choice(self, kind, name, choice):
        """Wrapper appele par chaque popup quand l'utilisateur change la selection."""
        if self._scene_choice_cb:
            try:
                self._scene_choice_cb(kind, name, choice)
            except Exception as e:
                _dlog(f"[panel.scene_choice] {e}")

    def _on_source_active_toggled(self, kind, name, checkbox, popup):
        """Appele quand la checkbox actif/inactif d'une source est basculee.
        - ON  → choisit la scene du popup (ou 'Toutes les scenes' si vide)
        - OFF → desactive (envoie sentinel '__OFF__' au callback)"""
        try:
            on = (checkbox.state() == AppKit.NSControlStateValueOn)
            if on:
                title = (popup.titleOfSelectedItem() if popup is not None
                         else "Toutes les scènes")
                if popup is not None:
                    try:
                        popup.setEnabled_(True)
                    except Exception:
                        pass
                self._on_scene_choice(kind, name, title or "Toutes les scènes")
            else:
                if popup is not None:
                    try:
                        popup.setEnabled_(False)
                    except Exception:
                        pass
                self._on_scene_choice(kind, name, "__OFF__")
        except Exception as e:
            _dlog(f"[panel.cb_toggle] {name!r} : {e}")

    def set_save_callback(self, callback):
        """(Legacy) Stocke callback ; n'est plus utilise depuis v2.5.52."""
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
            # Status avec dot coloree integree (couleur differente du texte)
            dot_color = GREEN if connected else ORANGE
            txt_color = FG if connected else FG2
            label     = "Connecté à OBS" if connected else "Connexion à OBS…"
            attrs_dot = {
                AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(dot_color),
                AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(18),
            }
            attrs_txt = {
                AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(txt_color),
                AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(15),
            }
            mstr = Foundation.NSMutableAttributedString.alloc().initWithString_attributes_(
                "\u25cf  ", attrs_dot
            )
            mstr.appendAttributedString_(
                Foundation.NSAttributedString.alloc().initWithString_attributes_(label, attrs_txt)
            )
            self._status_field.setAttributedStringValue_(mstr)
        except Exception as e:
            _dlog(f"[panel.status] {e}")

    def update_info(self, audio_names, video_names, cfg, scene_name=None):
        """Update the 'CE QUI EST SURVEILLÉ' info section."""
        if not self._info_field:
            return
        try:
            acfg = cfg["checks"]["audio"]
            vcfg = cfg["checks"]["video"]
            legacy_scenes = cfg.get("source_scenes", {}) or {}
            audio_scenes  = cfg.get("audio_source_scenes", {}) or {}
            video_scenes  = cfg.get("video_source_scenes", {}) or {}

            def _check_spec(spec):
                if spec == "":
                    return False
                if spec == "*":
                    return True
                return spec == scene_name

            def _is_active(name, kind):
                d = audio_scenes if kind == "audio" else video_scenes
                if name in d:
                    return _check_spec(d[name])
                if name in legacy_scenes:
                    return _check_spec(legacy_scenes[name])
                return True

            a_active = [n for n in (audio_names or []) if _is_active(n, "audio")]
            v_active = [n for n in (video_names or []) if _is_active(n, "video")]
            a_str = ", ".join(a_active) if a_active else "(aucune)"
            v_str = ", ".join(v_active) if v_active else "(aucune)"

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
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11),
            }
            astr = Foundation.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            storage.appendAttributedString_(astr)
            storage.endEditing()
        except Exception as e:
            _dlog(f"[panel.info] {e}")

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
                # Etat OK : gros checkmark centre, vert eclatant
                para = AppKit.NSMutableParagraphStyle.alloc().init()
                para.setAlignment_(AppKit.NSTextAlignmentCenter)
                para.setParagraphSpacingBefore_(28.0)

                attrs_emoji = {
                    AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(GREEN),
                    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(38),
                    AppKit.NSParagraphStyleAttributeName: para,
                }
                attrs_txt = {
                    AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(GREEN),
                    AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(14),
                    AppKit.NSParagraphStyleAttributeName: para,
                }
                storage.appendAttributedString_(
                    Foundation.NSAttributedString.alloc().initWithString_attributes_(
                        "\u2705\n", attrs_emoji
                    )
                )
                storage.appendAttributedString_(
                    Foundation.NSAttributedString.alloc().initWithString_attributes_(
                        "Tout est OK\n", attrs_txt
                    )
                )
            else:
                # Liste d'alertes : chaque issue avec puce rouge + spacing
                for i, issue in enumerate(issues):
                    para = AppKit.NSMutableParagraphStyle.alloc().init()
                    para.setFirstLineHeadIndent_(6.0)
                    para.setHeadIndent_(20.0)
                    para.setParagraphSpacing_(6.0)
                    attrs_bullet = {
                        AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(RED),
                        AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(14),
                        AppKit.NSParagraphStyleAttributeName: para,
                    }
                    attrs_txt = {
                        AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(FG),
                        AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(12),
                        AppKit.NSParagraphStyleAttributeName: para,
                    }
                    storage.appendAttributedString_(
                        Foundation.NSAttributedString.alloc().initWithString_attributes_(
                            "\u25cf  ", attrs_bullet
                        )
                    )
                    storage.appendAttributedString_(
                        Foundation.NSAttributedString.alloc().initWithString_attributes_(
                            issue + "\n", attrs_txt
                        )
                    )

            storage.endEditing()
        except Exception as e:
            _dlog(f"[panel.issues] {e}")

    def notify_update(self, version, url):
        if not self._update_field:
            return
        try:
            self._update_field.setStringValue_(f"\U0001f504  v{version} disponible")
            self._update_field.setHidden_(False)
        except Exception as e:
            _dlog(f"[panel.update] {e}")

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
            _dlog(f"[panel.save_pos] {e}")

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
        self._snooze_until   = 0.0  # timestamp jusqu'auquel la bannière est muette (snooze manuel)
        self._cooldown_until = 0.0  # timestamp jusqu'auquel la bannière est muette (cooldown auto)
        self._snooze_targets = []   # garder les targets en vie (évite GC)
        self.cfg = {}               # référence vers self._cfg["banner"] (injecté par OBSMonitorApp)

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
        btn_h    = 50
        spacing  = 24
        total_w  = n_btns * btn_w + (n_btns - 1) * spacing
        start_x  = (sw - total_w) / 2
        btn_y    = sh * 0.12

        for i, (label, duration) in enumerate(snooze_options):
            bx = start_x + i * (btn_w + spacing)
            btn_rect = Foundation.NSMakeRect(bx, btn_y, btn_w, btn_h)
            btn = AppKit.NSButton.alloc().initWithFrame_(btn_rect)
            btn.setBezelStyle_(AppKit.NSBezelStyleRegularSquare)
            btn.setBordered_(False)
            btn.setWantsLayer_(True)
            btn.layer().setCornerRadius_(12.0)
            btn.layer().setBackgroundColor_(
                AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.92).CGColor()
            )
            # Texte rouge foncé avec attributedTitle pour forcer la couleur
            attrs = {
                AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(ALERT_A),
                AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(15),
            }
            astr = Foundation.NSAttributedString.alloc().initWithString_attributes_(label, attrs)
            btn.setAttributedTitle_(astr)

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

    # Mapping abréviation FR → weekday (lundi=0 … dimanche=6)
    _DAY_MAP = {
        "lun": 0, "mar": 1, "mer": 2, "jeu": 3,
        "ven": 4, "sam": 5, "dim": 6,
    }

    def _in_active_window(self):
        """Retourne True si le jour ET l'heure actuels sont dans la plage configurée."""
        try:
            now_dt = datetime.datetime.now()
            now_t  = now_dt.time()

            # ── Vérification du jour ──────────────────────────────────────
            days_str = self.cfg.get("active_days", "lun,mar,mer,jeu,ven,sam,dim")
            active_days = {
                self._DAY_MAP[d.strip().lower()]
                for d in days_str.split(",")
                if d.strip().lower() in self._DAY_MAP
            }
            if not active_days:
                active_days = set(range(7))   # si vide → tous les jours
            if now_dt.weekday() not in active_days:
                return False

            # ── Vérification de l'heure ───────────────────────────────────
            frm = datetime.time(*map(int, self.cfg.get("active_from", "00:00").split(":")))
            unt = datetime.time(*map(int, self.cfg.get("active_until", "23:59").split(":")))
            if frm <= unt:
                return frm <= now_t <= unt
            else:  # plage chevauchant minuit
                return now_t >= frm or now_t <= unt
        except Exception:
            return False

    def update(self, issues, flash_state):
        """Update banner visibility and content based on issues."""
        if not self._panel:
            return

        if not self.cfg.get("enabled", True):
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
            return

        now = time.time()

        # Si plus d'alerte : cacher et démarrer cooldown.
        # NE PAS reset le snooze manuel — sinon une interruption d'1 tick (la
        # personne se recentre brièvement) annule le « Ok pour 1h » et la
        # bannière revient au prochain drift. On laisse le snooze expirer seul.
        if not issues:
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
                # Démarrer le cooldown auto
                cooldown = float(self.cfg.get("cooldown_s", 0))
                if cooldown > 0:
                    self._cooldown_until = now + cooldown
            return

        # Snooze manuel actif
        if now < self._snooze_until:
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
            return

        # Cooldown auto actif (après résolution d'une alerte précédente)
        if now < self._cooldown_until:
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
            return

        # Hors plage horaire
        if not self._in_active_window():
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
# NativeWarningBanner — écran jaune "RECADRER LA CAMÉRA"
# Moins urgent que NativeBanner (rouge) — avertissement de cadrage uniquement.
# ─────────────────────────────────────────────────────────────────────────────

class NativeWarningBanner:
    """
    Plein écran, semi-transparent, ambre/jaune.
    Affiche "RECADRER LA CAMÉRA" + détail du cadrage.
    Bouton pour ignorer temporairement.
    """

    _DAY_MAP = {
        "lun": 0, "mar": 1, "mer": 2, "jeu": 3,
        "ven": 4, "sam": 5, "dim": 6,
    }

    def __init__(self):
        self._panel          = None
        self._lbl_cta        = None
        self._lbl_det        = None
        self._built          = False
        self._visible        = False
        self._snooze_until   = 0.0
        self._snooze_targets = []
        self.cfg             = {}   # injected by OBSMonitorApp: self._cfg["warn_banner"]

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
        rect = Foundation.NSMakeRect(0, 0, sw, sh)
        self._panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False,
        )
        self._panel.setLevel_(AppKit.NSFloatingWindowLevel + 1)   # sous le banner rouge
        self._panel.setHidesOnDeactivate_(False)
        self._panel.setFloatingPanel_(True)
        self._panel.setBecomesKeyOnlyIfNeeded_(True)
        behavior = (
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
            AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._panel.setCollectionBehavior_(behavior)
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(_hex_to_nscolor(WARN_A))
        self._panel.setAlphaValue_(0.80)
        self._panel.setSharingType_(1)

        content = self._panel.contentView()

        # ── Label principal ──
        cta_rect = Foundation.NSMakeRect(20, sh * 0.44, sw - 40, sh * 0.18)
        self._lbl_cta = AppKit.NSTextField.alloc().initWithFrame_(cta_rect)
        self._lbl_cta.setStringValue_("RECADRER LA CAMÉRA")
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

        # ── Label secondaire : détail ──
        det_rect = Foundation.NSMakeRect(20, sh * 0.34, sw - 40, sh * 0.10)
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

        # ── Bouton snooze ──
        snooze_options = [
            ("Ok pour 10 minutes",  600),
            ("Ok pour 30 minutes",  1800),
            ("Ok pour 1h",          3600),
        ]
        n_btns  = len(snooze_options)
        btn_w   = 220
        btn_h   = 50
        spacing = 24
        total_w = n_btns * btn_w + (n_btns - 1) * spacing
        start_x = (sw - total_w) / 2
        btn_y   = sh * 0.14

        for i, (label, duration) in enumerate(snooze_options):
            bx = start_x + i * (btn_w + spacing)
            btn_rect = Foundation.NSMakeRect(bx, btn_y, btn_w, btn_h)
            btn = AppKit.NSButton.alloc().initWithFrame_(btn_rect)
            btn.setBezelStyle_(AppKit.NSBezelStyleRegularSquare)
            btn.setBordered_(False)
            btn.setWantsLayer_(True)
            btn.layer().setCornerRadius_(12.0)
            btn.layer().setBackgroundColor_(
                AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.92).CGColor()
            )
            attrs = {
                AppKit.NSForegroundColorAttributeName: _hex_to_nscolor(WARN_A),
                AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(15),
            }
            astr = Foundation.NSAttributedString.alloc().initWithString_attributes_(label, attrs)
            btn.setAttributedTitle_(astr)

            target = _SnoozeTarget.alloc().init()
            _dur   = duration
            target._callback = lambda d=_dur: self.snooze(d)
            self._snooze_targets.append(target)
            btn.setTarget_(target)
            btn.setAction_("clicked:")
            content.addSubview_(btn)

        self._panel.orderOut_(None)

    def snooze(self, duration):
        self._snooze_until = time.time() + max(0, duration)
        if self._panel and self._visible:
            self._panel.orderOut_(None)
            self._visible = False
        print(f"[warn_banner] snooze {duration/60:.0f} min")

    def _in_active_window(self):
        """Retourne True si le jour ET l'heure actuels sont dans la plage configurée."""
        try:
            now_dt = datetime.datetime.now()
            now_t  = now_dt.time()

            days_str = self.cfg.get("active_days", "lun,mar,mer,jeu,ven,sam,dim")
            active_days = {
                self._DAY_MAP[d.strip().lower()]
                for d in days_str.split(",")
                if d.strip().lower() in self._DAY_MAP
            }
            if not active_days:
                active_days = set(range(7))
            if now_dt.weekday() not in active_days:
                return False

            frm = datetime.time(*map(int, self.cfg.get("active_from", "00:00").split(":")))
            unt = datetime.time(*map(int, self.cfg.get("active_until", "23:59").split(":")))
            if frm <= unt:
                return frm <= now_t <= unt
            else:
                return now_t >= frm or now_t <= unt
        except Exception:
            return False

    def update(self, headroom_issues, flash_state):
        if not self._panel:
            return

        now = time.time()

        if not headroom_issues:
            # NE PAS reset le snooze ici — sinon le « Ok pour 1h » saute dès
            # que le recadrage redevient bon une fraction de seconde.
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
            return

        if now < self._snooze_until:
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
            return

        if not self._in_active_window():
            if self._visible:
                self._panel.orderOut_(None)
                self._visible = False
            return

        # Texte de détail
        detail = "  |  ".join(
            iss.split("—")[0].strip() for iss in headroom_issues[:2]
        )
        try:
            self._lbl_det.setStringValue_(detail)
        except Exception:
            pass

        # Flash ambre
        color = WARN_B if flash_state else WARN_A
        self._panel.setBackgroundColor_(_hex_to_nscolor(color))

        if not self._visible:
            self._panel.orderFrontRegardless()
            self._visible = True
        else:
            self._panel.orderFrontRegardless()

        # Boost au-dessus d'OBS (mais sous le banner rouge)
        obs_level = _get_obs_projector_level()
        self._panel.setLevel_(max(AppKit.NSFloatingWindowLevel + 1, obs_level + 1))

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
        self._video.set_scene_switch_cfg(
            self._cfg.setdefault("scene_switch", dict(DEFAULT_CONFIG["scene_switch"]))
        )
        # Selection des sources par scene (cfg["scenes"]) — partagee avec les monitors
        self._scenes_cfg = self._cfg.setdefault("scenes", {})
        self._audio.set_scenes_cfg(self._scenes_cfg)
        self._video.set_scenes_cfg(self._scenes_cfg)
        # v2.5.52 : portee par source (legacy fallback)
        self._source_scenes = self._cfg.setdefault("source_scenes", {})
        # v2.5.62 : per-source-per-kind (audio et video independants)
        self._audio_source_scenes = self._cfg.setdefault("audio_source_scenes", {})
        self._video_source_scenes = self._cfg.setdefault("video_source_scenes", {})
        self._audio.set_source_scenes(self._source_scenes, self._audio_source_scenes)
        self._video.set_source_scenes(self._source_scenes, self._video_source_scenes)
        self._current_scene = None
        self._all_scenes = []   # liste des scenes OBS (peuplee dans _connect)

        # SMS notifier — partage le dict de config (modifs propagées en direct)
        if "sms" not in self._cfg:
            self._cfg["sms"] = dict(DEFAULT_CONFIG["sms"])
            save_config(self._cfg)
        self._sms = SMSNotifier(self._cfg["sms"])
        self._was_connected = False  # pour détecter perte de connexion

        self._panel  = NativePanel()
        self._form_alerts_config = _FormPanel("⚙️  Seuils et horaires des alertes", width=500)
        self._settings_panel = _WebSettingsPanel("⚙️  Configuration OBS Monitor")
        self._banner         = NativeBanner()
        self._warn_banner    = NativeWarningBanner()
        self._banner.cfg = self._cfg.setdefault("banner", dict(DEFAULT_CONFIG["banner"]))
        warn_cfg = self._cfg.setdefault("warn_banner", dict(DEFAULT_CONFIG["warn_banner"]))
        self._warn_banner.cfg = warn_cfg
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
        self._config_item  = rumps.MenuItem("Configuration OBS…", callback=self._on_config)
        self._alerts_config_item = rumps.MenuItem("⚙️  Seuils et horaires des alertes…", callback=self._on_alerts_config)

        # SMS items
        sms_on = self._cfg.get("sms", {}).get("enabled", False)
        self._sms_toggle_item = rumps.MenuItem(
            "SMS : activé" if sms_on else "SMS : désactivé",
            callback=self._on_toggle_sms,
        )
        self._sms_test_item = rumps.MenuItem("Envoyer SMS de test", callback=self._on_sms_test)

        # Selection des sources par scene
        # (v2.5.52+) Le menu "Mémoriser/Effacer sources pour cette scène"
        # est remplacé par les dropdowns directement dans le panneau.

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
            self._alerts_config_item,
            self._sms_toggle_item,
            self._sms_test_item,
            None,
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
        self._warn_banner.hide()
        rumps.quit_application()

    def _on_alerts_config(self, _):
        """Ouvre le panneau natif de configuration des seuils et de l'écran rouge."""
        a  = self._cfg["checks"]["audio"]
        v  = self._cfg["checks"]["video"]
        b  = self._cfg.setdefault("banner", dict(DEFAULT_CONFIG["banner"]))
        w  = self._cfg.setdefault("warn_banner", dict(DEFAULT_CONFIG["warn_banner"]))
        s  = self._cfg.setdefault("sms", dict(DEFAULT_CONFIG["sms"]))
        sc = self._cfg.setdefault("scene_switch", dict(DEFAULT_CONFIG["scene_switch"]))

        sections = [
            ("AUDIO — Silence", [
                ("Surveiller le silence",             "silence_enabled",    "toggle", a.get("silence_enabled", True)),
                ("Silence si niveau en dessous de",   "silence_db",         "stepper", int(a.get("silence_db", -62)),         -100, -1, 1, "dB"),
                ("Durée avant alerte",                "silence_duration_s", "stepper", int(a.get("silence_duration_s", 10)),   1, 300, 1, "secondes"),
            ]),
            ("AUDIO — Saturation (clipping)", [
                ("Surveiller la saturation",          "clip_enabled",  "toggle", a.get("clip_enabled", True)),
                ("Saturation si niveau au-dessus de", "clip_db",       "stepper", int(a.get("clip_db", -1)),  -20, 0, 1, "dB"),
            ]),
            ("AUDIO — Bourdonnement (signal plat)", [
                ("Surveiller le bourdonnement",            "flat_enabled",    "toggle", a.get("flat_enabled", True)),
                ("Durée signal plat avant alerte",         "flat_duration_s", "stepper", int(a.get("flat_duration_s", 5)), 1, 120, 1, "secondes"),
            ]),
            ("VIDÉO — Image figée", [
                ("Surveiller l'image figée",          "freeze_enabled",    "toggle", v.get("freeze_enabled", True)),
                ("Durée figée avant alerte",          "freeze_duration_s", "stepper", int(v.get("freeze_duration_s", 3)), 1, 60, 1, "secondes"),
            ]),
            ("VIDÉO — Image sombre", [
                ("Surveiller l'image sombre",         "dark_enabled",    "toggle", v.get("dark_enabled", True)),
                ("Sombre si luminosité inférieure à", "dark_threshold",  "stepper", int(v.get("dark_threshold", 30)),  5, 120, 1, "/ 255"),
            ]),
            ("VIDÉO — Surexposition", [
                ("Surveiller la surexposition",       "bright_enabled",    "toggle", v.get("bright_enabled", True)),
                ("Saturée si luminosité supérieure à","bright_threshold",  "stepper", int(v.get("bright_threshold", 242)), 150, 255, 1, "/ 255"),
            ]),
            ("ÉCRAN JAUNE — Cadrage (espace au-dessus du visage)", [
                ("Activer l'écran jaune",                   "headroom_enabled",    "toggle", v.get("headroom_enabled", True)),
                ("Espace max au-dessus du visage",          "headroom_pct",        "stepper", int(v.get("headroom_threshold", 0.35)*100), 10, 60, 1, "%"),
                ("Durée avant avertissement",               "headroom_duration_s", "stepper", int(v.get("headroom_duration_s", 5)), 1, 30, 1, "secondes"),
                ("Jours actifs",                            "warn_days",           "days",    w.get("active_days", "lun,mar,mer,jeu,ven,sam,dim")),
                ("Actif à partir de",                       "warn_from",           "time",    w.get("active_from", "00:00")),
                ("Actif jusqu'à",                           "warn_until",          "time",    w.get("active_until", "23:59")),
            ]),
            ("AUTO-SWITCH SCÈNE (1↔2 personnes)", [
                ("Activer le switch auto de scène",         "sc_enabled",   "toggle",  sc.get("enabled", False)),
                ("Scène 1 personne  (nom OBS exact)",       "sc_scene_1p",  "text",    sc.get("scene_1p", "")),
                ("Scène 2 personnes (nom OBS exact)",       "sc_scene_2p",  "text",    sc.get("scene_2p", "")),
                ("Durée de détection avant switch",         "sc_trigger_s", "stepper", int(sc.get("trigger_s", 20)), 5, 60, 5, "secondes"),
            ]),
            ("ÉCRAN ROUGE", [
                ("Activer l'écran rouge",             "banner_enabled", "toggle", b.get("enabled", True)),
                ("Jours actifs",                      "active_days",    "days",   b.get("active_days", "lun,mar,mer,jeu,ven,sam,dim")),
                ("Actif à partir de",                 "active_from",    "time",   b.get("active_from", "00:00")),
                ("Actif jusqu'à",                     "active_until",   "time",   b.get("active_until", "23:59")),
                ("Cooldown après résolution",         "cooldown_s",     "stepper", int(b.get("cooldown_s", 0)), 0, 3600, 30, "secondes"),
            ]),
            ("NOTIFICATIONS macOS", [
                ("Activer les notifications",         "notif_enabled",      "toggle", b.get("notif_enabled", True)),
                ("Fréquence minimum entre notifs",    "notif_cooldown_min", "stepper", int(b.get("notif_cooldown_s", 1800)//60), 1, 240, 1, "minutes"),
            ]),
            ("SMS (Anyone Relay)", [
                ("Activer les SMS",                   "sms_enabled",        "toggle", s.get("enabled", False)),
                ("Clé API",                           "sms_api_key",        "text",   s.get("api_key", "")),
                ("Périphérique  (ex : 9210|0)",       "sms_device",         "text",   s.get("device", "")),
                ("Destinataire  (ex : +33612…)",      "sms_recipient",      "text",   s.get("recipient", "")),
                ("Cooldown entre SMS",                "sms_cooldown_min",   "stepper", int(s.get("cooldown_s", 600)//60), 1, 120, 1, "minutes"),
                ("Délai avant premier SMS",           "sms_min_dur_s",      "stepper", int(s.get("min_duration_s", 30)), 10, 300, 5, "secondes"),
                ("Jours d'envoi",                     "sms_days",           "days",   s.get("days", "lun,mar,mer,jeu,ven,sam,dim")),
                ("Envoyer à partir de",               "sms_send_from",      "time",   s.get("send_from", "10:00")),
                ("Envoyer jusqu'à",                   "sms_send_until",     "time",   s.get("send_until", "18:30")),
            ]),
        ]

        def on_save(vals):
            def iv(k, d):
                try: return int(vals[k])
                except: return d
            def sv(k, d): return vals.get(k, d) if isinstance(vals.get(k, d), str) else d
            def bv(k, d): return bool(vals.get(k, d))

            a["silence_enabled"]    = bv("silence_enabled",    True)
            a["silence_db"]         = iv("silence_db",         -62)
            a["silence_duration_s"] = iv("silence_duration_s", 10)
            a["clip_enabled"]       = bv("clip_enabled",       True)
            a["clip_db"]            = iv("clip_db",            -1)
            a["flat_enabled"]       = bv("flat_enabled",       True)
            a["flat_duration_s"]    = iv("flat_duration_s",    5)

            v["freeze_enabled"]     = bv("freeze_enabled",     True)
            v["freeze_duration_s"]  = iv("freeze_duration_s",  3)
            v["dark_enabled"]       = bv("dark_enabled",       True)
            v["dark_threshold"]     = iv("dark_threshold",     30)
            v["bright_enabled"]     = bv("bright_enabled",     True)
            v["bright_threshold"]   = iv("bright_threshold",   242)
            v["headroom_enabled"]   = bv("headroom_enabled",   True)
            v["headroom_threshold"] = iv("headroom_pct",       35) / 100.0
            v["headroom_duration_s"]= iv("headroom_duration_s", 5)

            w["active_days"]        = sv("warn_days",           "lun,mar,mer,jeu,ven,sam,dim")
            w["active_from"]        = sv("warn_from",           "00:00")
            w["active_until"]       = sv("warn_until",          "23:59")

            b["enabled"]            = bv("banner_enabled",     True)
            b["active_days"]        = sv("active_days",        "lun,mar,mer,jeu,ven,sam,dim")
            b["active_from"]        = sv("active_from",        "00:00")
            b["active_until"]       = sv("active_until",       "23:59")
            b["cooldown_s"]         = iv("cooldown_s",         0)
            b["notif_enabled"]      = bv("notif_enabled",      True)
            b["notif_cooldown_s"]   = iv("notif_cooldown_min", 30) * 60

            s["enabled"]            = bv("sms_enabled",        False)
            s["api_key"]            = sv("sms_api_key",        "")
            s["device"]             = sv("sms_device",         "")
            s["recipient"]          = sv("sms_recipient",      "")
            s["cooldown_s"]         = iv("sms_cooldown_min",   10) * 60
            s["min_duration_s"]     = iv("sms_min_dur_s",      30)
            s["days"]               = sv("sms_days",           "lun,mar,mer,jeu,ven,sam,dim")
            s["send_from"]          = sv("sms_send_from",      "10:00")
            s["send_until"]         = sv("sms_send_until",     "18:30")

            sc["enabled"]           = bv("sc_enabled",    False)
            sc["scene_1p"]          = sv("sc_scene_1p",  "")
            sc["scene_2p"]          = sv("sc_scene_2p",  "")
            sc["trigger_s"]         = iv("sc_trigger_s", 20)

            self._banner.cfg      = b
            self._warn_banner.cfg = w
            self._video.set_scene_switch_cfg(sc)
            # Sync SMS toggle menu item
            try:
                self._sms_toggle_item.title = "SMS : activé" if s["enabled"] else "SMS : désactivé"
            except Exception:
                pass
            save_config(self._cfg)
            rumps.notification("OBS Monitor", "", "Configuration enregistrée ✓", sound=False)

        self._settings_panel.show(sections, on_save)

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
        """Show SMS config dialog (api_key Bearer Relay, phone_gateway_id, recipient)."""
        try:
            s = self._cfg.setdefault("sms", dict(DEFAULT_CONFIG["sms"]))
            current = f"{s.get('api_key','')}|{s.get('phone_gateway_id','')}|{s.get('recipient','')}"
            w = rumps.Window(
                title="Configuration SMS (Anyone Relay)",
                message=("Format : UPSTREAM_KEY|PHONE_GATEWAY_ID|+33XXXXXXXXX\n"
                         "UPSTREAM_KEY commence par uk_…\n"
                         "PHONE_GATEWAY_ID commence par pgw_… (vide = auto-selection)"),
                default_text=current,
                ok="Enregistrer",
                cancel="Annuler",
                dimensions=(500, 80),
            )
            resp = w.run()
            if resp.clicked:
                parts = resp.text.strip().split("|")
                if len(parts) >= 3:
                    s["api_key"]          = parts[0].strip()
                    s["phone_gateway_id"] = parts[1].strip()
                    s["recipient"]        = "|".join(parts[2:]).strip()
                    s.setdefault("relay_base_url", "https://sms-01.anyone-internal.com")
                    save_config(self._cfg)
                    rumps.notification(
                        title="OBS Monitor",
                        subtitle="",
                        message="Configuration SMS enregistrée ✓",
                        sound=False,
                    )
        except Exception as e:
            print(f"[sms_config] {e}")

    def _sms_hours_label(self):
        s = self._cfg.get("sms", {})
        frm = s.get("send_from", "00:00")
        til = s.get("send_until", "23:59")
        return f"Horaires SMS : {frm} — {til}"

    def _on_sms_hours(self, _):
        """Régle la plage horaire d'envoi des SMS."""
        try:
            s = self._cfg.setdefault("sms", dict(DEFAULT_CONFIG["sms"]))
            frm = s.get("send_from", "10:00")
            til = s.get("send_until", "18:30")
            w = rumps.Window(
                title="Horaires d'envoi des SMS",
                message="Format : HH:MM-HH:MM  (ex : 10:00-18:30)\nLaisser vide pour envoyer à toute heure.",
                default_text=f"{frm}-{til}",
                ok="Enregistrer",
                cancel="Annuler",
                dimensions=(280, 60),
            )
            resp = w.run()
            if resp.clicked:
                text = resp.text.strip()
                if not text:
                    s["send_from"] = "00:00"
                    s["send_until"] = "23:59"
                else:
                    parts = text.split("-")
                    if len(parts) == 2:
                        s["send_from"]  = parts[0].strip()
                        s["send_until"] = parts[1].strip()
                save_config(self._cfg)
                self._sms_hours_item.title = self._sms_hours_label()
                rumps.notification(
                    title="OBS Monitor",
                    subtitle="",
                    message=f"Horaires SMS : {s['send_from']} — {s['send_until']}",
                    sound=False,
                )
        except Exception as e:
            print(f"[sms_hours] {e}")

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
        update_item = self._update_item
        def on_progress(msg):
            # setTitle: sur NSMenuItem DOIT être appelé depuis le main thread
            # (macOS 15 crashe avec SIGTRAP "Must only be used from the main thread")
            text = msg[:60]
            def _apply():
                try:
                    update_item.title = text
                except Exception:
                    pass
            try:
                if HAVE_APPKIT:
                    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)
                else:
                    _apply()
            except Exception:
                pass
        rumps.notification(
            "OBS Monitor", "",
            f"Mise à jour v{self._update_ver} — téléchargement en cours… L'app va redémarrer automatiquement.",
            sound=False,
        )
        threading.Thread(
            target=install_update,
            args=(self._update_url, app_path, on_progress),
            daemon=False,   # non-daemon : survit si rumps tente de quitter
        ).start()

    # ── Setup ────────────────────────────────────────────────────────────────

    def _after_start(self):
        """Called after the run loop is ready (via a short timer)."""
        self._panel.build()
        self._banner.build()
        self._warn_banner.build()

        # Wire up save button callback
        self._panel.set_scene_choice_callback(self._on_scene_choice_from_panel)

        self._video.start()
        threading.Thread(target=self._conn_loop, daemon=True).start()

        # Shortcut global Cmd+1 → toggle enregistrement OBS
        self._setup_record_hotkey()

        self._schedule_on_main(5.0, self._check_update_bg_wrapper)
        self._schedule_on_main(4.0, self._check_and_request_permissions)
        self._schedule_on_main(3.0, self._write_debug_log)

    def _setup_record_hotkey(self):
        """Ecoute Cmd+1 (keyCode 18 = touche "1/&") via NSEvent
        global + local monitor → toggle l'enregistrement OBS."""
        if not HAVE_APPKIT:
            return
        try:
            mask = AppKit.NSEventMaskKeyDown

            def _handler(event):
                try:
                    flags = event.modifierFlags()
                    kc = event.keyCode()
                    if (flags & AppKit.NSEventModifierFlagCommand) and kc == 18:
                        # Sur main thread pour Notif Center
                        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
                            self._toggle_record_via_obs
                        )
                except Exception as e:
                    _dlog(f"[hotkey] {e}")
                return event  # ne pas consommer (event traverse normalement)

            # Global : evenements quand OBS Monitor n'est PAS focus
            self._hotkey_global = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                mask, _handler
            )
            # Local : evenements quand OBS Monitor est focus
            self._hotkey_local = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                mask, _handler
            )
            _dlog("[hotkey] Cmd+1 → toggle record actif")
        except Exception as e:
            _dlog(f"[hotkey.setup] {e}")

    def _toggle_record_via_obs(self):
        """Bascule l'enregistrement OBS via WebSocket. Notification de feedback."""
        req = self._get_req()
        if not req:
            rumps.notification("OBS Monitor", "", "OBS non connecté", sound=False)
            return
        try:
            res = req.toggle_record()
            active = getattr(res, "output_active", None)
            if active is None:
                # OBS WS v5 : toggle_record peut ne pas retourner outputActive
                # On relit l'etat
                try:
                    status = req.get_record_status()
                    active = getattr(status, "output_active", None)
                except Exception:
                    pass
            msg = ("🔴 Enregistrement démarré" if active
                   else "⏹ Enregistrement arrêté")
            rumps.notification("OBS Monitor", "", msg, sound=False)
            _dlog(f"[hotkey.record] toggle → active={active}")
        except Exception as e:
            _dlog(f"[hotkey.record] {e}")
            rumps.notification("OBS Monitor", "",
                               f"Erreur enregistrement : {e}", sound=False)

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
                subs=(obs_ws.Subs.INPUTVOLUMEMETERS | obs_ws.Subs.SCENES),
            )

            def on_input_volume_meters(data):
                self._audio.on_volume_meters(data)

            def on_current_program_scene_changed(data):
                try:
                    name = getattr(data, "scene_name", None)
                    if name:
                        self._handle_scene_change(name)
                except Exception as e:
                    _dlog(f"[scene_event] {e}")

            evt.callback.register(on_input_volume_meters)
            evt.callback.register(on_current_program_scene_changed)

            with self._lock:
                self._req_client = req
                self._evt_client = evt
            self._connected        = True
            self._last_src_refresh = -999   # force refresh immédiat

            # Recupere la scene OBS courante au connect
            try:
                cur = req.get_current_program_scene().current_program_scene_name
                self._handle_scene_change(cur)
            except Exception as e:
                _dlog(f"[scene_init] {e}")

            # Liste complete des scenes OBS (pour les dropdowns du panel)
            try:
                sl = req.get_scene_list()
                names = []
                for s in (sl.scenes or []):
                    if isinstance(s, dict):
                        n = s.get("sceneName") or s.get("scene_name") or s.get("name")
                        if n:
                            names.append(n)
                self._all_scenes = names
                _dlog(f"[scenes] all_scenes = {names}")
            except Exception as e:
                _dlog(f"[scenes] get_scene_list error : {e}")
                self._all_scenes = []

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

    # ── Scene tracking ───────────────────────────────────────────────────────

    def _handle_scene_change(self, scene_name):
        """Appele a chaque changement de scene OBS (event + au connect)."""
        if scene_name == self._current_scene:
            return
        prev = self._current_scene
        self._current_scene = scene_name
        self._audio.set_current_scene(scene_name)
        self._video.set_current_scene(scene_name)
        # Force un refresh du panel pour montrer les sources de la nouvelle scene
        self._last_src_refresh = -999
        _dlog(f"[scene] {prev!r} -> {scene_name!r}")

    def _on_scene_choice_from_panel(self, kind, source_name, choice):
        """Callback : l'utilisateur a change le dropdown de scope pour une source.
        choice est un titre du popup : "Toutes les scènes" / "Désactivée" / "<nom scene>".
        Convertit en valeur stockee dans cfg["source_scenes"][source_name] :
          "*"        = toutes scenes
          ""         = desactivee
          "<scene>"  = scene specifique
        """
        # v2.5.62 : ecriture dans la dict specifique au kind (audio ou video)
        key = "audio_source_scenes" if kind == "audio" else "video_source_scenes"
        ss = self._cfg.setdefault(key, {})
        if choice == "__OFF__":
            spec = ""
        elif choice == "Toutes les scènes":
            spec = "*"
        elif choice == "Désactivée":  # legacy compat
            spec = ""
        else:
            spec = choice or "*"
        ss[source_name] = spec
        save_config(self._cfg)
        _dlog(f"[source_scope] {kind} {source_name!r} -> {spec!r}")
        # MAJ live de l'affichage "ce qui est surveille"
        try:
            audio_names = self._audio.known_inputs()
            video_names = self._video.known_sources()
            self._panel.update_info(audio_names, video_names, self._cfg, self._current_scene)
        except Exception:
            pass

    # ── Source refresh ────────────────────────────────────────────────────────

    def _refresh_sources(self):
        """Discover OBS sources and refresh panel checkboxes + info."""
        now = time.time()
        if now - self._last_src_refresh < 3:
            return
        self._last_src_refresh = now

        # Refetch scene list (peut avoir change : scene ajoutee/supprimee/renommee)
        try:
            req = self._get_req()
            if req:
                sl = req.get_scene_list()
                names = []
                for s in (sl.scenes or []):
                    if isinstance(s, dict):
                        n = s.get("sceneName") or s.get("scene_name") or s.get("name")
                        if n:
                            names.append(n)
                if names and names != self._all_scenes:
                    _dlog(f"[scenes] update : {self._all_scenes} -> {names}")
                    self._all_scenes = names
        except Exception as e:
            _dlog(f"[scenes_refresh] {e}")

        audio_names = self._audio.known_inputs()
        video_names = self._video.known_sources()
        # Note : un meme nom peut apparaitre dans les deux listes (Blackmagic
        # capture card avec audio embarque). Les 2 sont reglees independamment
        # via cfg["audio_source_scenes"] et cfg["video_source_scenes"].

        # Update panel popups (sources + scope) + info
        try:
            self._panel.refresh_sources(audio_names, video_names,
                                        self._cfg, self._current_scene,
                                        self._all_scenes)
        except Exception as e:
            _dlog(f"[src_refresh] panel: {e}")

        try:
            self._panel.update_info(audio_names, video_names,
                                    self._cfg, self._current_scene)
        except Exception as e:
            _dlog(f"[src_refresh] info: {e}")

    def _sync_checkboxes(self):
        """(Obsolete depuis v2.5.52 — les dropdowns ecrivent directement dans
        cfg["source_scenes"] via _on_scene_choice_from_panel.)"""
        return

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

        # Update warning banner (flashing yellow — cadrage)
        h_issues = self._video.headroom_issues() if self._connected else []
        self._warn_banner.update(h_issues, self._flash_st)

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

        # SMS via Anyone Relay
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

        if not self._cfg.get("banner", {}).get("notif_enabled", True):
            self._prev_issues = list(issues)
            return

        if n > 0:
            issues_changed = (issues != self._prev_issues)
            cooldown = float(self._cfg.get("banner", {}).get("notif_cooldown_s", 1800))
            enough_time = (now - self._last_notif_time >= cooldown)
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
