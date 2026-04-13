#!/usr/bin/env python3
"""
OBS Monitor v1.1 — Fenêtre flottante
Panneau de contrôle + bannière d'alerte clignotante sur tous les écrans.
"""

VERSION      = "1.3.5"
GITHUB_REPO  = "anyonesas/obs-monitor"
UPDATE_API   = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

import tkinter as tk
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
import subprocess
import tempfile
import shutil
from collections import deque
from PIL import Image, ImageStat, ImageChops

try:
    import AppKit
    HAVE_APPKIT = True
except ImportError:
    HAVE_APPKIT = False

try:
    import obsws_python as obs_ws
except ImportError:
    sys.exit("pip install obsws-python")

# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "obs": {"host": "localhost", "port": 4455, "password": ""},
    "checks": {
        "audio": {
            "silence_db": -50,
            "silence_duration_s": 3,
            "clip_db": -1,
            "flat_std_db": 2.5,
            "flat_min_db": -45,
            "clip_ratio": 0.4,
            "monitor_inputs": []
        },
        "video": {
            "freeze_threshold": 0.997,
            "freeze_duration_s": 3,
            "dark_threshold": 30,
            "bright_threshold": 242,
            "check_interval_s": 2,
            "monitor_sources": []
        }
    },
    "panel": {"x": None, "y": None},
    "banner": {"y": None}
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    with open(CONFIG_PATH) as f:
        c = json.load(f)
    # Fusionne avec les defaults pour les clés manquantes
    for section, vals in DEFAULT_CONFIG["checks"].items():
        for k, v in vals.items():
            c["checks"].setdefault(section, {}).setdefault(k, v)
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

def boost_all_windows():
    """
    Passe TOUTES les fenêtres de l'app au niveau NSScreenSaverWindowLevel.
    On ne filtre pas par ID (winfo_id ≠ windowNumber sur macOS) : on set tout.
    Appelé au démarrage ET toutes les 5 s pour résister aux resets de macOS.
    """
    if not HAVE_APPKIT:
        return
    try:
        level    = AppKit.NSScreenSaverWindowLevel
        behavior = (AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
                    AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        for ns_win in AppKit.NSApp.windows():
            try:
                ns_win.setLevel_(level)
                ns_win.setCollectionBehavior_(behavior)
            except Exception:
                pass
    except Exception:
        pass

# Alias pour compatibilité avec les anciens appels
def boost_window(tk_win, high=True):
    boost_all_windows()


def version_tuple(v):
    return tuple(int(x) for x in v.lstrip("v").split("."))

def check_for_update():
    """
    Vérifie la dernière release GitHub via curl (SSL fiable sur macOS bundlé).
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
        # Vérification de rate-limit GitHub
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
        print("[update] aucun .dmg trouvé dans les assets")
    except Exception as e:
        print(f"[update] exception: {e}")
    return None, None

def install_update(dmg_url, app_path, on_progress=None):
    """
    Télécharge le DMG, monte, copie le .app, démonte, relance.
    app_path = chemin absolu vers l'OBSMonitor.app en cours d'utilisation.
    """
    try:
        # 1. Téléchargement via curl (SSL fiable même dans app bundlée)
        tmp_dmg = os.path.join(tempfile.gettempdir(), "OBSMonitor_update.dmg")
        if on_progress: on_progress("Téléchargement…")
        subprocess.run(
            ["curl", "-L", "-o", tmp_dmg, "--max-time", "120", dmg_url],
            check=True, capture_output=True
        )

        # 2. Montage
        if on_progress: on_progress("Installation…")
        mnt = os.path.join(tempfile.gettempdir(), "OBSMonitor_mnt")
        subprocess.run(["hdiutil", "attach", tmp_dmg,
                        "-mountpoint", mnt, "-quiet", "-nobrowse"],
                       check=True)

        # 3. Copie du .app
        src_app = os.path.join(mnt, "OBSMonitor.app")
        dst_app = app_path  # remplace l'app en cours
        if os.path.exists(dst_app):
            shutil.rmtree(dst_app)
        shutil.copytree(src_app, dst_app)

        # 4. Démontage
        subprocess.run(["hdiutil", "detach", mnt, "-quiet"], check=False)
        os.remove(tmp_dmg)

        # 5. Redémarrage
        if on_progress: on_progress("Redémarrage…")
        exe = os.path.join(dst_app, "Contents", "MacOS", "OBSMonitor")
        os.execv(exe, [exe])   # remplace le process actuel → redémarre proprement

    except Exception as e:
        if on_progress: on_progress(f"Erreur : {e}")

def get_all_screens():
    """
    Retourne une liste de (x, y, width, height) en coordonnées tkinter
    pour chaque écran physique connecté.
    """
    if not HAVE_APPKIT:
        return [(0, 0, tk.Tk().winfo_screenwidth(), tk.Tk().winfo_screenheight())]
    screens = []
    main_h = AppKit.NSScreen.mainScreen().frame().size.height
    for screen in AppKit.NSScreen.screens():
        f  = screen.frame()
        sx = int(f.origin.x)
        # Conversion coord macOS (origine bas-gauche) → tkinter (origine haut-gauche)
        sy = int(main_h - f.origin.y - f.size.height)
        sw = int(f.size.width)
        sh = int(f.size.height)
        screens.append((sx, sy, sw, sh))
    return screens


# ─────────────────────────────────────────────────────────────────────────────
# Audio Monitor — avec buffer pour analyse de variance
# ─────────────────────────────────────────────────────────────────────────────

class AudioMonitor:
    BUFFER_SIZE = 70   # ~7s à ~10 updates/s

    def __init__(self, cfg):
        self.cfg   = cfg
        self._lock = threading.Lock()
        self._inputs = {}
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
                        "last_sound_t": now,   # pas d'alerte silence immédiate
                        "buf": deque(maxlen=self.BUFFER_SIZE),
                    }

    def known_inputs(self):
        with self._lock:
            return list(self._inputs.keys())

    def issues(self):
        now     = time.time()
        monitor = self.cfg.get("monitor_inputs", [])
        out     = []

        silence_thresh = self.cfg["silence_db"]
        silence_dur    = self.cfg["silence_duration_s"]
        clip_db        = self.cfg.get("clip_db", -1)
        flat_std       = self.cfg.get("flat_std_db", 2.5)
        flat_min       = self.cfg.get("flat_min_db", -45)
        clip_ratio_thr = self.cfg.get("clip_ratio", 0.4)

        with self._lock:
            for name, e in self._inputs.items():
                if monitor and name not in monitor:
                    continue

                db      = e["peak_db"]
                silence = now - e["last_sound_t"]
                buf     = list(e["buf"])

                # ① Silence prolongé
                if silence >= silence_dur:
                    out.append(
                        f"🎤  « {name} »  silence depuis {silence:.0f}s"
                        f"  — micro déconnecté ou muet ?"
                    )
                    continue   # inutile de vérifier le reste

                if len(buf) < 20:
                    continue   # pas assez de données

                mean = sum(buf) / len(buf)
                std  = (sum((v - mean) ** 2 for v in buf) / len(buf)) ** 0.5

                # ② Son trop constant (bourdonnement, micro bloqué)
                if mean > flat_min and std < flat_std:
                    out.append(
                        f"🐝  « {name} »  son trop constant (variation {std:.1f} dB sur 7s)"
                        f"  — bourdonnement / micro bloqué ?"
                    )

                # ③ Saturation chronique (trop longtemps dans le rouge)
                clip_count = sum(1 for v in buf if v >= clip_db)
                ratio      = clip_count / len(buf)
                if ratio >= clip_ratio_thr:
                    out.append(
                        f"🔴  « {name} »  saturé {ratio*100:.0f}% du temps"
                        f"  — baisser le gain du micro"
                    )
                elif db >= clip_db:
                    # Écrêtage ponctuel (non chronique)
                    out.append(f"🔊  « {name} »  écrêtage ponctuel ({db:.1f} dB)")

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

        monitor    = self.cfg.get("monitor_sources", [])
        found      = []
        new_issues = []

        for item in items:
            if not item.get("sceneItemEnabled", True):
                continue
            src = item.get("sourceName", "")
            if not src:
                continue
            found.append(src)
            if monitor and src not in monitor:
                continue

            img = self._capture(client, src)
            if img is None:
                continue

            gray = img.convert("L")
            stat = ImageStat.Stat(gray)
            br   = stat.mean[0]   # luminosité moyenne 0-255
            std  = stat.stddev[0] # écart-type : faible = image uniforme (noire ou blanche)

            dark_thr   = self.cfg["dark_threshold"]    # 30
            bright_thr = self.cfg["bright_threshold"]  # 242

            # Image trop sombre (noire ou très sombre + peu de variation)
            if br < dark_thr:
                new_issues.append(
                    f"📷  « {src} »  image trop sombre (luminosité {br:.0f}/255)"
                    f"  — lumières éteintes ou caméra déconnectée ?"
                )
            # Image trop uniforme même si pas noire : capteur bloqué sur une couleur
            elif std < 4 and br < 60:
                new_issues.append(
                    f"📷  « {src} »  image anormalement uniforme"
                    f"  — caméra bloquée ?"
                )
            elif br > bright_thr:
                new_issues.append(
                    f"💡  « {src} »  surexposée (luminosité {br:.0f}/255)"
                    f"  — éclairage trop fort ?"
                )

            fi = self._freeze(src, img)
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

    def _freeze(self, src, img):
        now = time.time()
        with self._lock:
            prev = self._prev_frames.get(src)

        if prev:
            _, pimg = prev
            a   = pimg.convert("L").resize((80, 45))
            b   = img.convert("L").resize((80, 45))
            diff = ImageChops.difference(a, b)
            rms  = ImageStat.Stat(diff).rms[0]
            # max_diff : le pixel qui a le plus changé (0 = aucun pixel n'a bougé)
            max_diff = diff.getextrema()[1]
            sim  = 1.0 - rms / 128.0

            # Vrai freeze = similarité très haute ET aucun pixel n'a vraiment bougé
            # Scène calme naturelle = sim élevée mais quelques pixels varient (bruit, respiration)
            is_frozen = (sim >= self.cfg["freeze_threshold"]) and (max_diff < 8)

            if is_frozen:
                with self._lock:
                    self._freeze_since.setdefault(src, now)
                    frozen = now - self._freeze_since[src]
                if frozen >= self.cfg["freeze_duration_s"]:
                    return (
                        f"🧊  « {src} »  figée depuis {frozen:.0f}s"
                        f"  — caméra plantée ?"
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
# Bannière d'alerte — une par écran, draggable, toujours au-dessus de tout
# ─────────────────────────────────────────────────────────────────────────────

class AlertBanner:
    H = 76

    def __init__(self, root, saved_y=None):
        self._root    = root
        self._screens = get_all_screens()   # [(sx, sy, sw, sh), ...]
        self._wins    = []                  # une Toplevel par écran
        self._tvars   = []                  # (title_var, body_var) par écran
        self._drag_anchor = 0

        for i, (sx, sy, sw, sh) in enumerate(self._screens):
            y = int(saved_y) if (saved_y is not None and i == 0) else sy + 24
            y = max(sy, min(y, sy + sh - self.H))

            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.0)
            win.geometry(f"{sw}x{self.H}+{sx}+{y}")
            win.configure(bg=ALERT_A)

            # Barre jaune
            tk.Frame(win, width=5, bg=YELLOW).pack(side="left", fill="y")

            content = tk.Frame(win, bg=ALERT_A)
            content.pack(side="left", fill="both", expand=True, padx=(10, 0))

            title_var = tk.StringVar()
            body_var  = tk.StringVar()

            tk.Label(content, textvariable=title_var,
                     bg=ALERT_A, fg="white",
                     font=("SF Pro Display", 13, "bold"), anchor="w"
                     ).pack(fill="x", pady=(10, 1))
            tk.Label(content, textvariable=body_var,
                     bg=ALERT_A, fg="#ffcccc",
                     font=("SF Pro Display", 10),
                     anchor="w", wraplength=sw - 120, justify="left"
                     ).pack(fill="x")

            drag_lbl = tk.Label(win, text="↕", bg=ALERT_A, fg="#884444",
                                font=("SF Pro Display", 18),
                                cursor="sb_v_double_arrow")
            drag_lbl.pack(side="right", padx=14)

            for w in [win, content, drag_lbl]:
                w.bind("<ButtonPress-1>", self._drag_start)
                w.bind("<B1-Motion>",     self._drag_move)

            self._wins.append(win)
            self._tvars.append((title_var, body_var))

        # Boost niveau macOS après un court délai (fenêtres doivent être affichées)
        root.after(300, self._boost_all)

    def _boost_all(self):
        for win in self._wins:
            boost_window(win)

    def _drag_start(self, e):
        self._drag_anchor = e.y_root - self._wins[0].winfo_y()

    def _drag_move(self, e):
        delta_y = e.y_root - self._wins[0].winfo_y() - self._drag_anchor
        for i, (win, (sx, sy, sw, sh)) in enumerate(zip(self._wins, self._screens)):
            new_y = win.winfo_y() + delta_y
            new_y = max(sy, min(new_y, sy + sh - self.H))
            win.geometry(f"{sw}x{self.H}+{sx}+{new_y}")
        self._drag_anchor = e.y_root - self._wins[0].winfo_y()

    def current_y(self):
        return self._wins[0].winfo_y() if self._wins else 24

    def show(self, issues, flash_state):
        n     = len(issues)
        title = f"⚠   {n} PROBLÈME{'S' if n > 1 else ''} DÉTECTÉ{'S' if n > 1 else ''}"
        body  = "   ·   ".join(i.split("  —")[0].strip() for i in issues)
        bg    = ALERT_B if flash_state else ALERT_A
        for i, win in enumerate(self._wins):
            tv, bv = self._tvars[i]
            tv.set(title)
            bv.set(body)
            win.configure(bg=bg)
            self._recolor(win, bg)
            win.attributes("-alpha", 0.93)

    def _recolor(self, w, bg):
        try:
            if str(w.cget("bg")).startswith("#"):
                w.configure(bg=bg)
        except Exception:
            pass
        for child in w.winfo_children():
            self._recolor(child, bg)

    def hide(self):
        for win in self._wins:
            win.attributes("-alpha", 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Panneau de contrôle
# ─────────────────────────────────────────────────────────────────────────────

class ControlPanel:
    W = 290

    def __init__(self, root, cfg, audio_mon, video_mon, on_reconnect=None):
        self._root         = root
        self._cfg          = cfg
        self._audio_mon    = audio_mon
        self._video_mon    = video_mon
        self._on_reconnect = on_reconnect

        self._audio_vars  = {}
        self._video_vars  = {}
        self._known_audio = []
        self._known_video = []

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        px = cfg.get("panel", {}).get("x")
        py = cfg.get("panel", {}).get("y")
        x  = int(px) if px is not None else sw - self.W - 20
        y  = int(py) if py is not None else 60
        x  = max(0, min(x, sw - self.W - 4))
        y  = max(30, min(y, sh - 300))

        root.withdraw()                   # cache avant d'enlever les décorations
        root.overrideredirect(True)       # supprime barre native macOS + dots
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.97)
        root.configure(bg=BG)
        root.geometry(f"{self.W}x100+{x}+{y}")

        self._build()
        self._autosize()
        root.deiconify()                  # réaffiche sans barre native
        # Panneau : NSScreenSaverWindowLevel — toujours au-dessus de tout, OBS inclus
        root.after(300, lambda: boost_window(root, high=True))
        self._enable_drag()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build(self):
        r = self._root

        # ── Barre de titre (propre, sans les dots macOS) — sert aussi d'ancre pour le drag
        self._bar = bar = tk.Frame(r, bg=BG2, height=36)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(
            bar, text="OBS Monitor",
            fg=FG, bg=BG2,
            font=("SF Pro Display", 12, "bold")
        ).pack(side="left", padx=14, pady=8)

        tk.Label(
            bar, text=f"v{VERSION}",
            fg=FG2, bg=BG2,
            font=("SF Pro Display", 9)
        ).pack(side="left", pady=8)

        quit_btn = tk.Label(
            bar, text="✕", fg=FG2, bg=BG2,
            font=("SF Pro Display", 14), cursor="hand2"
        )
        quit_btn.pack(side="right", padx=12)
        quit_btn.bind("<Button-1>", lambda e: self._root.quit())
        quit_btn.bind("<Enter>",    lambda e: quit_btn.configure(fg=RED))
        quit_btn.bind("<Leave>",    lambda e: quit_btn.configure(fg=FG2))

        # Bouton mise à jour (caché jusqu'à ce qu'une update soit dispo)
        self._update_btn = tk.Label(
            bar, text="🔄 Mise à jour", fg=BG, bg=GREEN,
            font=("SF Pro Display", 9, "bold"), cursor="hand2", padx=6, pady=4
        )
        self._update_url  = None
        self._update_ver  = None
        self._update_btn.bind("<Button-1>", lambda e: self._do_update())

        # ── Statut + bouton config
        sf = tk.Frame(r, bg=BG, pady=8)
        sf.pack(fill="x", padx=14)

        self._dot_lbl    = tk.Label(sf, text="●", fg=ORANGE, bg=BG, font=("SF Pro Display", 12))
        self._status_lbl = tk.Label(sf, text=" Connexion à OBS…", fg=ORANGE, bg=BG, font=("SF Pro Display", 10))
        self._dot_lbl.pack(side="left")
        self._status_lbl.pack(side="left")

        cfg_btn = tk.Label(sf, text="⚙", fg=FG2, bg=BG,
                           font=("SF Pro Display", 14), cursor="hand2")
        cfg_btn.pack(side="right")
        cfg_btn.bind("<Button-1>", lambda e: self._toggle_config_panel())
        cfg_btn.bind("<Enter>",    lambda e: cfg_btn.configure(fg=ACCENT))
        cfg_btn.bind("<Leave>",    lambda e: cfg_btn.configure(fg=FG2))

        # ── Panneau de configuration OBS (caché par défaut)
        self._cfg_panel = tk.Frame(r, bg=BG3)
        self._cfg_open  = False

        tk.Label(self._cfg_panel, text="Connexion OBS", fg=FG, bg=BG3,
                 font=("SF Pro Display", 10, "bold")).pack(anchor="w", padx=12, pady=(8, 4))

        # Mot de passe
        pw_row = tk.Frame(self._cfg_panel, bg=BG3)
        pw_row.pack(fill="x", padx=12, pady=2)
        tk.Label(pw_row, text="Mot de passe :", fg=FG2, bg=BG3,
                 font=("SF Pro Display", 10), width=14, anchor="w").pack(side="left")
        self._pw_var = tk.StringVar(value=self._cfg["obs"].get("password", ""))
        pw_entry = tk.Entry(pw_row, textvariable=self._pw_var, bg=BG2, fg=FG,
                            insertbackground=FG, relief="flat",
                            font=("SF Pro Display", 10), show="●")
        pw_entry.pack(side="left", fill="x", expand=True, ipady=4)

        # Bouton appliquer
        apply_btn = tk.Label(self._cfg_panel, text="✓  Appliquer", fg=BG, bg=ACCENT,
                             font=("SF Pro Display", 10, "bold"), cursor="hand2", pady=5)
        apply_btn.pack(fill="x", padx=12, pady=(6, 10))
        apply_btn.bind("<Button-1>", lambda e: self._apply_config())

        self._sep()

        # ── Sources audio
        self._section_label("SOURCES AUDIO  🎤")
        self._audio_frame = tk.Frame(r, bg=BG)
        self._audio_frame.pack(fill="x", padx=14)
        self._audio_placeholder = tk.Label(
            self._audio_frame, text="En attente d'OBS…",
            fg=FG2, bg=BG, font=("SF Pro Display", 10)
        )
        self._audio_placeholder.pack(anchor="w")

        self._sep()

        # ── Sources vidéo
        self._section_label("SOURCES VIDÉO  📷")
        self._video_frame = tk.Frame(r, bg=BG)
        self._video_frame.pack(fill="x", padx=14)
        self._video_placeholder = tk.Label(
            self._video_frame, text="En attente d'OBS…",
            fg=FG2, bg=BG, font=("SF Pro Display", 10)
        )
        self._video_placeholder.pack(anchor="w")

        # ── Bouton Enregistrer la sélection
        self._save_btn = tk.Label(
            r, text="💾  Enregistrer la sélection",
            fg=BG, bg=ACCENT,
            font=("SF Pro Display", 10, "bold"),
            cursor="hand2", pady=5
        )
        self._save_btn.pack(fill="x", padx=14, pady=(6, 2))
        self._save_btn.bind("<Button-1>", lambda e: self._save_sources())

        # ── Problèmes détectés (toujours dans le DOM, juste vide si aucun)
        self._issues_sep   = tk.Frame(r, bg=BORDER, height=1)
        self._issues_frame = tk.Frame(r, bg=BG)
        self._issue_labels = []
        self._issues_sep.pack(fill="x", padx=8, pady=4)
        self._issues_frame.pack(fill="x", padx=14, pady=(0, 4))

        # ── Section "Ce qui est surveillé" — grille compacte
        self._sep()
        self._build_info_section()

    def _sep(self):
        tk.Frame(self._root, bg=BORDER, height=1).pack(fill="x", padx=8, pady=4)

    def _section_label(self, text):
        tk.Label(
            self._root, text=text,
            fg=FG2, bg=BG,
            font=("SF Pro Display", 9, "bold")
        ).pack(anchor="w", padx=14, pady=(4, 3))

    def _build_info_section(self):
        """Grille compacte 2 colonnes — ce qui est surveillé."""
        tk.Label(self._root, text="CE QUI EST SURVEILLÉ",
                 fg=FG2, bg=BG,
                 font=("SF Pro Display", 9, "bold")
                 ).pack(anchor="w", padx=14, pady=(4, 3))

        grid = tk.Frame(self._root, bg=BG)
        grid.pack(fill="x", padx=14, pady=(0, 8))

        items = [
            (YELLOW, "🎤 Silence"),
            (CYAN,   "🐝 Bourdonnement"),
            (RED,    "🔴 Saturation"),
            (ORANGE, "🔊 Écrêtage"),
            (FG2,    "📷 Image noire"),
            (FG,     "💡 Surexposée"),
            (ACCENT, "🧊 Figée"),
        ]

        for i, (color, label) in enumerate(items):
            col = i % 2
            row = i // 2
            tk.Label(grid, text=label, fg=color, bg=BG,
                     font=("SF Pro Display", 10), anchor="w"
                     ).grid(row=row, column=col, sticky="w", padx=(0, 8), pady=1)

    def notify_update(self, version, url):
        """Appelé depuis le thread d'update quand une nouvelle version est dispo."""
        self._update_ver = version
        self._update_url = url
        # Bandeau bien visible sous la barre de titre
        if not hasattr(self, "_update_banner") or not self._update_banner.winfo_exists():
            self._update_banner = tk.Frame(self._root, bg=GREEN, cursor="hand2")
            self._update_banner.pack(fill="x", after=self._bar)
            lbl = tk.Label(
                self._update_banner,
                text=f"🔄  Mise à jour v{version} disponible — cliquer pour installer",
                fg=BG, bg=GREEN,
                font=("SF Pro Display", 10, "bold"), pady=6, cursor="hand2"
            )
            lbl.pack()
            for w in [self._update_banner, lbl]:
                w.bind("<Button-1>", lambda e: self._do_update())
        self._autosize()

    def _do_update(self):
        if not self._update_url:
            return
        self._set_update_banner(ORANGE, "⏳  Téléchargement en cours…")
        app_path = os.path.abspath(
            os.path.join(os.path.dirname(sys.executable), "..", "..", "..")
        )
        def on_progress(msg):
            self._root.after(0, lambda m=msg: self._set_update_banner(ORANGE, f"⏳  {m}"))
        threading.Thread(
            target=install_update,
            args=(self._update_url, app_path, on_progress),
            daemon=True
        ).start()

    def _set_update_banner(self, color, text):
        """Met à jour le bandeau de mise à jour (crée si nécessaire)."""
        if not hasattr(self, "_update_banner") or not self._update_banner.winfo_exists():
            return
        self._update_banner.configure(bg=color)
        for w in self._update_banner.winfo_children():
            try:
                w.configure(bg=color, fg=BG, text=text)
            except Exception:
                pass

    def _toggle_config_panel(self):
        self._cfg_open = not self._cfg_open
        if self._cfg_open:
            self._cfg_panel.pack(fill="x", padx=8, pady=(0, 4), after=self._dot_lbl.master)
        else:
            self._cfg_panel.pack_forget()
        self._autosize()

    def _apply_config(self):
        self._cfg["obs"]["password"] = self._pw_var.get()
        save_config(self._cfg)
        self._cfg_panel.pack_forget()
        self._cfg_open = False
        self._autosize()
        # Signale à l'app de se reconnecter
        if self._on_reconnect:
            self._on_reconnect()

    def _autosize(self):
        """Adapte la hauteur de la fenêtre à son contenu."""
        self._root.update_idletasks()
        h = self._root.winfo_reqheight()
        x = self._root.winfo_x()
        y = self._root.winfo_y()
        self._root.geometry(f"{self.W}x{h}+{x}+{y}")

    def _save_sources(self):
        """Enregistre explicitement la sélection audio+vidéo et donne un retour visuel."""
        mon_a = [n for n, v in self._audio_vars.items() if v.get()]
        if len(mon_a) == len(self._audio_vars):
            mon_a = []
        mon_v = [n for n, v in self._video_vars.items() if v.get()]
        if len(mon_v) == len(self._video_vars):
            mon_v = []
        self._cfg["checks"]["audio"]["monitor_inputs"] = mon_a
        self._cfg["checks"]["video"]["monitor_sources"] = mon_v
        self._audio_mon.cfg = self._cfg["checks"]["audio"]
        self._video_mon.cfg = self._cfg["checks"]["video"]
        save_config(self._cfg)
        # Retour visuel bref
        self._save_btn.configure(text="✓  Sélection enregistrée !", bg=GREEN)
        self._root.after(1800, lambda: self._save_btn.configure(
            text="💾  Enregistrer la sélection", bg=ACCENT))

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _enable_drag(self):
        """Drag lié UNIQUEMENT à la barre de titre pour ne pas interférer
        avec les checkboxes et autres contrôles du panneau."""
        self._dx = self._dy = 0

        def start(e):
            self._dx = e.x_root - self._root.winfo_x()
            self._dy = e.y_root - self._root.winfo_y()

        def move(e):
            x = e.x_root - self._dx
            y = e.y_root - self._dy
            self._root.geometry(f"+{x}+{y}")

        # On lie sur self._bar (barre de titre) + ses enfants directs via propagation
        self._bar.bind("<ButtonPress-1>", start)
        self._bar.bind("<B1-Motion>",     move)

    # ── Mise à jour dynamique ─────────────────────────────────────────────────

    def update_status(self, connected):
        if connected:
            self._dot_lbl.configure(fg=GREEN)
            self._status_lbl.configure(text=" Connecté à OBS", fg=GREEN)
        else:
            self._dot_lbl.configure(fg=ORANGE)
            self._status_lbl.configure(text=" Connexion à OBS…", fg=ORANGE)

    def update_issues(self, issues):
        for lbl in self._issue_labels:
            lbl.destroy()
        self._issue_labels.clear()

        for issue in issues:
            lbl = tk.Label(
                self._issues_frame, text=issue,
                fg=RED, bg=BG,
                font=("SF Pro Display", 10),
                anchor="w", wraplength=262, justify="left"
            )
            lbl.pack(anchor="w", pady=1)
            self._issue_labels.append(lbl)

        # Séparateur visible seulement s'il y a des issues
        if issues:
            self._issues_sep.configure(bg=BORDER)
        else:
            self._issues_sep.configure(bg=BG)   # invisible (fond identique)

        self._autosize()

    def flash_bg(self, has_issues, flash_state):
        bg = ALERT_A if (has_issues and flash_state) else BG
        self._root.configure(bg=bg)

    def refresh_sources(self, audio_names, video_names):
        if audio_names == self._known_audio and video_names == self._known_video:
            return

        self._known_audio = audio_names
        self._known_video = video_names
        mon_a = set(self._cfg["checks"]["audio"].get("monitor_inputs", []))
        mon_v = set(self._cfg["checks"]["video"].get("monitor_sources", []))

        # Audio
        for w in self._audio_frame.winfo_children():
            w.destroy()
        self._audio_vars.clear()
        src_list = audio_names or []
        if src_list:
            for name in src_list:
                checked = (not mon_a) or (name in mon_a)
                var = tk.BooleanVar(value=checked)
                cb  = tk.Checkbutton(
                    self._audio_frame, text=name, variable=var,
                    command=self._on_audio_toggle,
                    fg=FG, bg=BG, selectcolor=BG2,
                    activebackground=BG, activeforeground=FG,
                    font=("SF Pro Display", 11), anchor="w"
                )
                cb.pack(anchor="w", pady=1)
                self._audio_vars[name] = var
        else:
            tk.Label(self._audio_frame, text="Aucune source audio",
                     fg=FG2, bg=BG, font=("SF Pro Display", 10)).pack(anchor="w")

        # Vidéo
        for w in self._video_frame.winfo_children():
            w.destroy()
        self._video_vars.clear()
        if video_names:
            for name in video_names:
                checked = (not mon_v) or (name in mon_v)
                var = tk.BooleanVar(value=checked)
                cb  = tk.Checkbutton(
                    self._video_frame, text=name, variable=var,
                    command=self._on_video_toggle,
                    fg=FG, bg=BG, selectcolor=BG2,
                    activebackground=BG, activeforeground=FG,
                    font=("SF Pro Display", 11), anchor="w"
                )
                cb.pack(anchor="w", pady=1)
                self._video_vars[name] = var
        else:
            tk.Label(self._video_frame, text="Aucune source vidéo",
                     fg=FG2, bg=BG, font=("SF Pro Display", 10)).pack(anchor="w")

        self._autosize()

    def _on_audio_toggle(self):
        monitored = [n for n, v in self._audio_vars.items() if v.get()]
        if len(monitored) == len(self._audio_vars):
            monitored = []
        self._cfg["checks"]["audio"]["monitor_inputs"] = monitored
        self._audio_mon.cfg = self._cfg["checks"]["audio"]
        save_config(self._cfg)

    def _on_video_toggle(self):
        monitored = [n for n, v in self._video_vars.items() if v.get()]
        if len(monitored) == len(self._video_vars):
            monitored = []
        self._cfg["checks"]["video"]["monitor_sources"] = monitored
        self._video_mon.cfg = self._cfg["checks"]["video"]
        save_config(self._cfg)

    def save_position(self):
        self._cfg.setdefault("panel", {})["x"] = self._root.winfo_x()
        self._cfg.setdefault("panel", {})["y"] = self._root.winfo_y()


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrateur
# ─────────────────────────────────────────────────────────────────────────────

class OBSMonitorApp:
    RECONNECT = 5
    TICK_MS   = 380

    def __init__(self):
        self._cfg  = load_config()
        self._lock = threading.Lock()

        self._req_client = None
        self._evt_client = None
        self._connected  = False

        self._flash_st        = False
        self._last_src_refresh = 0

        self._audio = AudioMonitor(self._cfg["checks"]["audio"])
        self._video = VideoMonitor(self._cfg["checks"]["video"], self._get_req)

        self._root  = tk.Tk()
        self._root.title("OBS Monitor")

        self._panel  = ControlPanel(self._root, self._cfg, self._audio, self._video,
                                    on_reconnect=self._force_reconnect)
        self._banner = AlertBanner(
            self._root,
            saved_y=self._cfg.get("banner", {}).get("y")
        )

        self._video.start()
        threading.Thread(target=self._conn_loop, daemon=True).start()

        self._root.after(self.TICK_MS, self._tick)
        self._root.after(2000,         self._save_positions)
        self._root.after(5000,         self._check_update)   # vérif au démarrage
        self._root.after(500,          self._reboost)        # always-on-top persistant


    # ── Connexion OBS ─────────────────────────────────────────────────────────

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

            # ── Découverte immédiate des sources (sans attendre les events) ──
            try:
                # Sources audio
                inp_list = req.get_input_list()
                audio_names = [i["inputName"] for i in inp_list.inputs
                               if i.get("inputKind", "").startswith(
                                   ("coreaudio", "wasapi", "alsa", "pulse",
                                    "av_capture", "dshow_input", "vlc", "ffmpeg")
                               ) or "audio" in i.get("inputKind", "").lower()
                               or "mic" in i.get("inputName", "").lower()
                               or "input" in i.get("inputKind", "").lower()]
                # Si aucun filtre ne matche, prend tout sauf les sources vidéo pures
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

    # ── Tick UI ───────────────────────────────────────────────────────────────

    def _tick(self):
        issues = (self._audio.issues() + self._video.issues()) if self._connected else []
        self._flash_st = not self._flash_st if issues else False

        self._panel.update_status(self._connected)
        self._panel.update_issues(issues)
        self._panel.flash_bg(bool(issues), self._flash_st)

        if issues:
            self._banner.show(issues, self._flash_st)
        else:
            self._banner.hide()

        if self._connected and time.time() - self._last_src_refresh > 5:
            self._panel.refresh_sources(
                self._audio.known_inputs(),
                self._video.known_sources()
            )
            self._last_src_refresh = time.time()

        self._root.after(self.TICK_MS, self._tick)

    def _reboost(self):
        """Re-applique NSScreenSaverWindowLevel toutes les 5 s sur toutes les fenêtres."""
        boost_all_windows()
        self._root.after(5000, self._reboost)

    def _check_update(self):
        """Vérifie les mises à jour en background, notifie si dispo."""
        def _bg():
            ver, url = check_for_update()
            if ver and url:
                self._root.after(0, lambda: self._panel.notify_update(ver, url))
        threading.Thread(target=_bg, daemon=True).start()
        # Re-vérifie toutes les 30 minutes
        self._root.after(30 * 60 * 1000, self._check_update)

    def _save_positions(self):
        self._panel.save_position()
        self._cfg.setdefault("banner", {})["y"] = self._banner.current_y()
        save_config(self._cfg)
        self._root.after(2000, self._save_positions)

    def run(self):
        self._root.mainloop()


if __name__ == "__main__":
    OBSMonitorApp().run()
