#!/usr/bin/env python3
"""Flask UI Server – Image Analyzer"""

import base64
import functools
import io
import json
import math
import os
import re
import uuid
import webbrowser
from pathlib import Path
from threading import Timer
from typing import Optional
from datetime import datetime as _dt

import anthropic
from flask import (Flask, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from PIL import Image

def _load_env_file():
    """Read .env from the script directory without requiring python-dotenv."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:   # don't override real env vars
                os.environ[key] = value

_load_env_file()

try:
    from supabase import create_client as _sb_create
    _SUPABASE_OK = True
except ImportError:
    _SUPABASE_OK = False

SCRIPT_DIR          = Path(__file__).parent
CACHE_DIR           = SCRIPT_DIR / "cache"
PRODUCT_PHOTOS_DIR  = SCRIPT_DIR / "static" / "product-photos"
LEGAL_CHECKS_DIR    = SCRIPT_DIR / "data" / "legal_checks"
PRODUCT_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
LEGAL_CHECKS_DIR.mkdir(parents=True, exist_ok=True)


def _save_legal_check(product_id: str, result: dict) -> None:
    """Persist a legal-check result to disk as JSON."""
    try:
        to_save = dict(result)
        to_save.setdefault("_runDate", _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
        path = LEGAL_CHECKS_DIR / f"{product_id}.json"
        path.write_text(json.dumps(to_save, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_legal_check(product_id: str) -> dict:
    """Load a previously saved legal-check result. Returns {} if not found."""
    try:
        path = LEGAL_CHECKS_DIR / f"{product_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

# ── Bootstrap config for secret key before app init ──────────────────────────

def _deep_merge(base: dict, override: dict):
    """Recursively merge override into base in-place."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v

def load_config() -> dict:
    """Load config from Supabase DB, bootstrapped by environment variables."""
    url  = os.environ.get('SUPABASE_URL', '')
    key  = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    anon = os.environ.get('SUPABASE_ANON_KEY', '')

    # Start with env-var bootstrap
    cfg: dict = {
        'supabase': {
            'url':              url,
            'anon_key':         anon,
            'service_role_key': key,
        },
    }

    if _SUPABASE_OK and url and key:
        try:
            sb  = _sb_create(url, key)
            res = sb.table("app_config").select("config").eq("id", 1).execute()
            if res.data and res.data[0].get("config"):
                _deep_merge(cfg, res.data[0]["config"])
                # Env vars always win for credentials
                sb_node = cfg.setdefault('supabase', {})
                if url:  sb_node['url']              = url
                if anon: sb_node['anon_key']         = anon
                if key:  sb_node['service_role_key'] = key
        except Exception as e:
            print(f"[Config] DB load failed: {e}")

    return cfg


def save_config(cfg: dict):
    """Persist config to Supabase DB only (config.yaml removed)."""
    url = os.environ.get('SUPABASE_URL', '') or cfg.get('supabase', {}).get('url', '')
    key = (os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
           or cfg.get('supabase', {}).get('service_role_key', ''))
    if _SUPABASE_OK and url and key:
        try:
            sb = _sb_create(url, key)
            sb.table("app_config").upsert({"id": 1, "config": cfg}).execute()
        except Exception as e:
            print(f"[Config DB] {e}")
    else:
        print("[Config] Cannot save: Supabase not configured")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get('APP_SECRET_KEY', os.urandom(24).hex())

@app.context_processor
def inject_footer_globals():
    """Inject footer variables into every template context."""
    cfg = load_config()
    return {
        'footer_logo':    cfg.get('footer_logo',    'logo-light.png'),
        'footer_company': cfg.get('footer_company', 'dekoire.com'),
    }

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _auth_supabase(email: str, password: str):
    """Try Supabase auth. Returns user dict or raises."""
    cfg = load_config()
    sb_cfg = cfg.get("supabase", {})
    sb = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
    resp = sb.auth.sign_in_with_password({"email": email, "password": password})
    return {"email": resp.user.email, "id": str(resp.user.id)}

def _auth_fallback(email: str, password: str):
    """Config-file fallback auth."""
    cfg = load_config()
    ok = (email == cfg.get("admin_email", "") and
          password == cfg.get("admin_password", "") and
          bool(cfg.get("admin_password", "")))
    if not ok:
        raise ValueError("Invalid credentials")
    return {"email": email, "id": "local"}

def authenticate(email: str, password: str):
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    if _SUPABASE_OK and sb_cfg.get("url") and sb_cfg.get("anon_key"):
        try:
            return _auth_supabase(email, password)
        except Exception:
            pass  # Supabase user not found – fall through to local auth
    return _auth_fallback(email, password)

def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_email"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper

# ── API client ────────────────────────────────────────────────────────────────

def get_client():
    cfg = load_config()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("No API key in config.yaml or ANTHROPIC_API_KEY env var.")
    return anthropic.Anthropic(api_key=key), cfg

# ── Cache & server-side compression ──────────────────────────────────────────

MAX_BYTES = 5 * 1024 * 1024

def save_and_compress(file_bytes: bytes, filename: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    uid  = uuid.uuid4().hex[:10]
    orig = CACHE_DIR / f"{uid}_{Path(filename).name}"
    orig.write_bytes(file_bytes)
    if len(file_bytes) <= MAX_BYTES:
        return orig
    with Image.open(io.BytesIO(file_bytes)) as img:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        scale = math.sqrt(MAX_BYTES / len(file_bytes)) * 0.92
        resized = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )
        for quality in [85, 75, 65, 55, 45, 30]:
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= MAX_BYTES:
                comp = CACHE_DIR / f"{uid}_c.jpg"
                comp.write_bytes(buf.getvalue())
                orig.unlink(missing_ok=True)
                return comp
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=20, optimize=True)
        comp = CACHE_DIR / f"{uid}_c.jpg"
        comp.write_bytes(buf.getvalue())
        orig.unlink(missing_ok=True)
        return comp

# ── Image helpers ─────────────────────────────────────────────────────────────

def aspect_ratio_str(w: int, h: int) -> str:
    gcd = math.gcd(w, h)
    rw, rh = w // gcd, h // gcd
    if max(rw, rh) > 50:
        common = [(1,1),(4,3),(3,4),(16,9),(9,16),(3,2),(2,3),(5,4),(4,5),(21,9)]
        rw, rh = min(common, key=lambda r: abs(r[0]/r[1] - w/h))
    return f"{rw}:{rh}"

def image_meta(file_bytes: bytes) -> dict:
    with Image.open(io.BytesIO(file_bytes)) as img:
        width, height = img.size
    return {
        "breite_px":         width,
        "hoehe_px":          height,
        "ausrichtung":       "Horizontal" if width >= height else "Vertical",
        "seitenverhaeltnis": aspect_ratio_str(width, height),
    }

def encode_from_path(path: Path) -> tuple[str, str]:
    """Base64-encode an image and detect its real MIME type from the bytes,
    not the file extension (mismatched extensions cause Claude 400 errors)."""
    raw  = path.read_bytes()
    data = base64.standard_b64encode(raw).decode()
    # Detect actual format via Pillow; fall back to magic bytes
    mime = _detect_mime(raw)
    return data, mime


def _detect_mime(raw: bytes) -> str:
    """Return the real MIME type of image bytes, independent of file extension."""
    try:
        img = Image.open(io.BytesIO(raw))
        return {
            "JPEG": "image/jpeg", "PNG": "image/png",
            "WEBP": "image/webp", "GIF": "image/gif",
            "BMP":  "image/bmp",  "TIFF": "image/tiff",
        }.get(img.format or "", "image/jpeg")
    except Exception:
        # Magic-byte fallback
        if raw[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

# ── Slug helper ──────────────────────────────────────────────────────────────

def slugify_py(text: str) -> str:
    t = text.replace('ä','ae').replace('ö','oe').replace('ü','ue')
    t = t.replace('Ä','Ae').replace('Ö','Oe').replace('Ü','Ue').replace('ß','ss')
    t = re.sub(r'\s+', '_', t.strip())
    return re.sub(r'[^a-zA-Z0-9_]', '', t)

# ── Final Files folder ────────────────────────────────────────────────────────

def create_final_folder(dekoire_id: str, titel: str, image_bytes: bytes,
                         orig_filename: str, cfg: dict) -> Optional[Path]:
    export_cfg = cfg.get("export", {})
    if not export_cfg.get("create_folder", True):
        return None
    base_str = str(export_cfg.get("final_files_folder", "Final Files"))
    base     = Path(base_str) if Path(base_str).is_absolute() else SCRIPT_DIR / base_str
    slug     = slugify_py(titel) if titel else "untitled"
    folder   = base / f"{dekoire_id}_{slug}"
    folder.mkdir(parents=True, exist_ok=True)
    ext      = Path(orig_filename).suffix.lower() or ".jpg"
    (folder / f"{dekoire_id}_{slug}{ext}").write_bytes(image_bytes)
    return folder

# ── Supabase save ─────────────────────────────────────────────────────────────

THUMBNAILS_DIR = SCRIPT_DIR / "static" / "thumbnails"
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR = SCRIPT_DIR / "static" / "profiles"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
APP_ICONS_DIR = SCRIPT_DIR / "static" / "app-icons"
APP_ICONS_DIR.mkdir(parents=True, exist_ok=True)

# ── Preset icons for External Apps ───────────────────────────────────────────
ICON_PRESETS: dict[str, str] = {
    "github": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.166 6.839 9.489.5.092.682-.217.682-.482 0-.237-.009-.868-.013-1.703-2.782.603-3.369-1.342-3.369-1.342-.454-1.155-1.11-1.463-1.11-1.463-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0 1 12 6.844a9.59 9.59 0 0 1 2.504.337c1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0 0 22 12c0-5.523-4.477-10-10-10z"/></svg>',
    "supabase": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M11.9 1.036c-.015-.986-1.26-1.41-1.874-.637L.764 12.05C.111 12.954.732 14.2 1.824 14.2h9.71l.105 8.764c.015.987 1.26 1.41 1.874.638l9.262-11.652c.653-.903.032-2.15-1.06-2.15h-9.71L11.9 1.036z"/></svg>',
    "figma": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M15.852 8.981h-4.588V0h4.588c2.476 0 4.49 2.014 4.49 4.49s-2.014 4.491-4.49 4.491zM12.735 7.51h3.117c1.665 0 3.019-1.355 3.019-3.019s-1.354-3.019-3.019-3.019h-3.117V7.51zm0 1.471H8.148c-2.476 0-4.49-2.014-4.49-4.49S5.672 0 8.148 0h4.588v8.981zm-4.587-7.51c-1.665 0-3.019 1.355-3.019 3.02s1.354 3.018 3.019 3.018h3.117V1.471H8.148zm4.587 15.019H8.148c-2.476 0-4.49-2.014-4.49-4.49s2.014-4.49 4.49-4.49h4.588v8.98zM8.148 8.981c-1.665 0-3.019 1.355-3.019 3.019s1.355 3.019 3.019 3.019h3.117V8.981H8.148zM8.172 24c-2.489 0-4.515-2.014-4.515-4.49s2.026-4.49 4.515-4.49c2.49 0 4.516 2.014 4.516 4.49S10.661 24 8.172 24zm0-7.509c-1.665 0-3.019 1.355-3.019 3.019s1.354 3.019 3.019 3.019c1.665 0 3.019-1.355 3.019-3.019s-1.354-3.019-3.019-3.019zm7.71 7.509h-.048c-2.447-.015-4.416-2.014-4.416-4.49s1.969-4.49 4.416-4.49h.048c2.447.015 4.416 2.014 4.416 4.49s-1.969 4.49-4.416 4.49zm-.048-7.509c-1.651-.012-3.004 1.338-3.016 2.99-.012 1.652 1.338 3.003 2.99 3.016l.025-.001c1.651.012 3.004-1.338 3.016-2.99.012-1.652-1.338-3.003-2.99-3.016h-.025z"/></svg>',
    "notion": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4.459 4.208c.746.606 1.026.56 2.428.466l13.215-.793c.28 0 .047-.28-.046-.326L17.86 1.968c-.42-.326-.981-.7-2.055-.607L3.01 2.295c-.466.046-.56.28-.374.466zm.793 3.08v13.904c0 .747.373 1.027 1.214.98l14.523-.84c.841-.046.935-.56.935-1.167V6.354c0-.606-.233-.933-.748-.887l-15.177.887c-.56.047-.747.327-.747.933zm14.337.745c.093.42 0 .84-.42.888l-.7.14v10.264c-.608.327-1.168.514-1.635.514-.748 0-.935-.234-1.495-.933l-4.577-7.186v6.952L12.21 19s0 .84-1.168.84l-3.222.186c-.093-.186 0-.653.327-.746l.84-.233V9.854L7.822 9.76c-.094-.42.14-1.026.793-1.073l3.456-.233 4.764 7.279v-6.44l-1.215-.139c-.093-.514.28-.887.747-.933zM1.936 1.035l13.31-.98c1.634-.14 2.055-.047 3.082.7l4.249 2.986c.7.513.934.653.934 1.213v16.378c0 1.026-.373 1.634-1.68 1.726l-15.458.934c-.98.047-1.448-.093-1.962-.747l-3.129-4.06c-.56-.747-.793-1.306-.793-1.96V2.667c0-.839.374-1.54 1.447-1.632z"/></svg>',
    "slack": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>',
    "vercel": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M24 22.525H0l12-21.05 12 21.05z"/></svg>',
    "linear": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M0 14.293L9.707 24H14l-14-14v4.293zM0 9.88L14.12 24H18.3L0 5.7v4.18zM0 5.467L18.533 24H21.8L0 2.2v3.267zM0 1.053L22.947 24H24v-.82L1.053 0H.234L0 .234v.82zM1.053 0l21.894 21.894V19L2.947 0H1.053zM5.467 0l16.48 16.48V12L9.88 0H5.467zM9.88 0l10.013 10.013V5.72L14.173 0H9.88z"/></svg>',
    "discord": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.317 4.492c-1.53-.69-3.17-1.2-4.885-1.49a.075.075 0 0 0-.079.036c-.21.369-.444.85-.608 1.23a18.566 18.566 0 0 0-5.487 0 12.36 12.36 0 0 0-.617-1.23A.077.077 0 0 0 8.562 3c-1.714.29-3.354.8-4.885 1.491a.07.07 0 0 0-.032.027C.533 9.093-.32 13.555.099 17.961a.08.08 0 0 0 .031.055 20.03 20.03 0 0 0 5.993 2.98.078.078 0 0 0 .084-.026c.462-.62.874-1.275 1.226-1.963.021-.04.001-.088-.041-.104a13.201 13.201 0 0 1-1.872-.878.075.075 0 0 1-.008-.125c.126-.093.252-.19.372-.287a.075.075 0 0 1 .078-.01c3.927 1.764 8.18 1.764 12.061 0a.075.075 0 0 1 .079.009c.12.098.245.195.372.288a.075.075 0 0 1-.006.125c-.598.344-1.22.635-1.873.877a.075.075 0 0 0-.041.105c.36.687.772 1.341 1.225 1.962a.077.077 0 0 0 .084.028 19.963 19.963 0 0 0 6.002-2.981.076.076 0 0 0 .032-.054c.5-5.094-.838-9.52-3.549-13.442a.06.06 0 0 0-.031-.028zM8.02 15.278c-1.182 0-2.157-1.069-2.157-2.38 0-1.312.956-2.38 2.157-2.38 1.21 0 2.176 1.077 2.157 2.38 0 1.312-.956 2.38-2.157 2.38zm7.975 0c-1.183 0-2.157-1.069-2.157-2.38 0-1.312.955-2.38 2.157-2.38 1.21 0 2.176 1.077 2.157 2.38 0 1.312-.946 2.38-2.157 2.38z"/></svg>',
    "shopify": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M15.337.009a.297.297 0 0 0-.27.218c-.014.055-.28 1.741-.28 1.741C14.788 1.97 12.08 0 8.81 0 6.252 0 4.14.99 2.62 2.7.748 4.807 0 7.62 0 10.338c0 4.07 2.553 6.42 5.35 6.42 1.484 0 2.795-.554 3.736-1.478l-.358 2.273c-.39 2.478-2.336 4.015-4.593 4.015a5.27 5.27 0 0 1-2.77-.77l-.6 3.802C2.14 23.46 3.838 24 5.75 24c2.754 0 5.245-1.127 6.981-3.132 1.598-1.843 2.538-4.399 2.538-7.238 0-3.296-1.482-5.318-3.68-6.29l.94-5.98a.298.298 0 0 0-.178-.341l-.014-.01z"/></svg>',
    "google": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>',
}

def compress_to_jpeg(image_bytes: bytes, max_mb: float = 2.0) -> bytes:
    """Compress to max_mb MB JPEG, reduce quality then size as needed."""
    max_bytes = int(max_mb * 1024 * 1024)
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        for quality in (85, 75, 65, 50):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue()
        w, h = img.size
        img = img.resize((w // 2, h // 2), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        return buf.getvalue()

def save_thumbnail(image_bytes: bytes, dekoire_id: str) -> str:
    """Save compressed JPEG to static/thumbnails/. Returns local URL path."""
    thumb = compress_to_jpeg(image_bytes)
    (THUMBNAILS_DIR / f"{dekoire_id}.jpg").write_bytes(thumb)
    return f"/static/thumbnails/{dekoire_id}.jpg"

def get_sb_admin():
    """Supabase client with service_role_key (bypasses RLS)."""
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    url    = sb_cfg.get("url", "")
    key    = cfg.get("service_role_key", "") or sb_cfg.get("service_role_key", "")
    if not _SUPABASE_OK or not url or not key:
        raise ValueError("Supabase service_role_key not configured")
    return _sb_create(url, key)

def get_user_profile(user_id: str) -> dict:
    try:
        sb  = get_sb_admin()
        res = sb.table("user_profiles").select("*").eq("user_id", user_id).execute()
        return (res.data or [{}])[0]
    except Exception:
        return {}

def supabase_save(cfg: dict, data: dict, image_bytes: bytes,
                   orig_filename: str) -> str:
    """Save thumbnail locally + insert DB record. Returns image_url."""
    if not _SUPABASE_OK:
        return ""
    sb_cfg     = cfg.get("supabase", {})
    export_cfg = cfg.get("export", {})
    if not sb_cfg.get("url") or not sb_cfg.get("anon_key"):
        return ""
    if not export_cfg.get("save_to_supabase", True):
        return ""

    def join_list(v):
        return ", ".join(v) if isinstance(v, list) else str(v or "")

    try:
        sb         = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
        table      = sb_cfg.get("table_name", "image_analyses")
        dekoire_id = data.get("dekoire_id", uuid.uuid4().hex[:7])

        # Save thumbnail locally (always reliable)
        image_url = save_thumbnail(image_bytes, dekoire_id)

        record = {
            "dekoire_id":       dekoire_id,
            "neuer_dateiname":  data.get("neuer_dateiname", ""),
            "mj_id":            data.get("mj_id", ""),
            "dateiname":        data.get("dateiname", ""),
            "titel":            data.get("titel", ""),
            "beschreibung":     data.get("beschreibung", ""),
            "dominante_farben": join_list(data.get("dominante_farben", [])),
            "ausrichtung":      data.get("ausrichtung", ""),
            "breite_px":        data.get("breite_px") or None,
            "hoehe_px":         data.get("hoehe_px")  or None,
            "seitenverhaeltnis":data.get("seitenverhaeltnis", ""),
            "ist_fotografie":   bool(data.get("ist_fotografie", False)),
            "kunstart":         data.get("kunstart", ""),
            "epoche":           data.get("epoche", ""),
            "tags":             join_list(data.get("tags", [])),
            "pin_titel":        data.get("pin_titel", ""),
            "pin_beschreibung": data.get("pin_beschreibung", ""),
            "pin_ziel_url":     data.get("pin_ziel_url", ""),
            "pin_alt_text":     data.get("pin_alt_text", ""),
            "pin_board":        data.get("pin_board", ""),
            "pin_board_id":     data.get("pin_board_id", ""),
            "pin_media_url":    data.get("pin_media_url", ""),
            "ig_title":         data.get("ig_title", ""),
            "ig_description":   data.get("ig_description", ""),
            "ig_tags":          data.get("ig_tags", ""),
            "ig_location":      data.get("ig_location", ""),
            # ── Extended Social ──
            "ig_alt_text":           data.get("ig_alt_text", ""),
            "ig_content_type":       data.get("ig_content_type", "post"),
            "pin_board_section":     data.get("pin_board_section", ""),
            # ── Etsy ──
            "etsy_title":            data.get("etsy_title", ""),
            "etsy_description":      data.get("etsy_description", ""),
            "etsy_tags":             data.get("etsy_tags", ""),
            "etsy_materials":        data.get("etsy_materials", ""),
            "etsy_who_made":         data.get("etsy_who_made", ""),
            "etsy_when_made":        data.get("etsy_when_made", ""),
            "etsy_occasion":         data.get("etsy_occasion", ""),
            "etsy_recipient":        data.get("etsy_recipient", ""),
            "etsy_shipping_profile": data.get("etsy_shipping_profile", ""),
            "etsy_price":            data.get("etsy_price") or None,
            # ── Shopify ──
            "shopify_title":         data.get("shopify_title", ""),
            "shopify_body_html":     data.get("shopify_body_html", ""),
            "shopify_vendor":        data.get("shopify_vendor", ""),
            "shopify_product_type":  data.get("shopify_product_type", ""),
            "shopify_tags":          data.get("shopify_tags", ""),
            "shopify_sku":           data.get("shopify_sku", ""),
            "shopify_price":         data.get("shopify_price") or None,
            "shopify_compare_price": data.get("shopify_compare_price") or None,
            "shopify_collection":    data.get("shopify_collection", ""),
            "shopify_status":        data.get("shopify_status", "draft"),
            # ── Amazon ──
            "amazon_title":          data.get("amazon_title", ""),
            "amazon_description":    data.get("amazon_description", ""),
            "amazon_bullet_1":       data.get("amazon_bullet_1", ""),
            "amazon_bullet_2":       data.get("amazon_bullet_2", ""),
            "amazon_bullet_3":       data.get("amazon_bullet_3", ""),
            "amazon_bullet_4":       data.get("amazon_bullet_4", ""),
            "amazon_bullet_5":       data.get("amazon_bullet_5", ""),
            "amazon_search_terms":   data.get("amazon_search_terms", ""),
            "amazon_brand":          data.get("amazon_brand", ""),
            "amazon_price":          data.get("amazon_price") or None,
            "amazon_sku":            data.get("amazon_sku", ""),
            "amazon_category":       data.get("amazon_category", ""),
            "amazon_condition":      data.get("amazon_condition", "new"),
            "image_url":        image_url,
            "aufnahmedatum":    data.get("aufnahmedatum", ""),
            "dpi_x":            data.get("dpi_x") or None,
            "dpi_y":            data.get("dpi_y") or None,
            "datei_groesse_kb": data.get("datei_groesse_kb") or None,
        }
        res = sb.table(table).insert(record).execute()
        supabase_id = (res.data[0].get("id", "") if res.data else "")
        return {"image_url": image_url, "supabase_id": supabase_id}
    except Exception as e:
        print(f"[Supabase DB] {e}")
        # Retry without new columns (pre-migration DBs)
        try:
            for k in ("aufnahmedatum","dpi_x","dpi_y","datei_groesse_kb",
                      "ig_alt_text","ig_content_type","pin_board_section","pin_board_id",
                      "etsy_title","etsy_description","etsy_tags","etsy_materials",
                      "etsy_who_made","etsy_when_made","etsy_occasion","etsy_recipient",
                      "etsy_shipping_profile","etsy_price",
                      "shopify_title","shopify_body_html","shopify_vendor",
                      "shopify_product_type","shopify_tags","shopify_sku",
                      "shopify_price","shopify_compare_price","shopify_collection","shopify_status",
                      "amazon_title","amazon_description","amazon_bullet_1","amazon_bullet_2",
                      "amazon_bullet_3","amazon_bullet_4","amazon_bullet_5",
                      "amazon_search_terms","amazon_brand","amazon_price","amazon_sku",
                      "amazon_category","amazon_condition"):
                record.pop(k, None)
            res = sb.table(table).insert(record).execute()
            supabase_id = (res.data[0].get("id", "") if res.data else "")
            return {"image_url": image_url, "supabase_id": supabase_id}
        except Exception as e2:
            print(f"[Supabase DB retry] {e2}")
            return {"image_url": "", "supabase_id": ""}


def _migrate_db():
    """Add new meta columns if missing. Runs once at startup."""
    try:
        import psycopg2
    except ImportError:
        print("[DB migration] psycopg2 not installed, skipping")
        return
    cfg     = load_config()
    sb_cfg  = cfg.get("supabase", {})
    db_pass = cfg.get("db_password", "")
    sb_url  = sb_cfg.get("url", "")
    if not db_pass or not sb_url:
        return
    try:
        proj = sb_url.replace("https://", "").split(".")[0]
        conn = psycopg2.connect(
            host=f"db.{proj}.supabase.co", port=5432, dbname="postgres",
            user="postgres", password=db_pass, sslmode="require", connect_timeout=5,
        )
        cur   = conn.cursor()
        table = sb_cfg.get("table_name", "image_analyses")
        for col, typ in (("aufnahmedatum","TEXT"),("dpi_x","FLOAT"),
                          ("dpi_y","FLOAT"),("datei_groesse_kb","INTEGER")):
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ};")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                user_id UUID UNIQUE NOT NULL,
                vorname TEXT DEFAULT '',
                nachname TEXT DEFAULT '',
                profile_image_url TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        conn.commit(); cur.close(); conn.close()
        print("[DB] migration complete")
    except Exception as e:
        print(f"[DB migration] {e}")

# ── External apps seed ───────────────────────────────────────────────────────

def _seed_external_apps():
    """Pre-populate external_apps with GitHub + Supabase if key not present."""
    cfg = load_config()
    if "external_apps" not in cfg:
        cfg["external_apps"] = [
            {"id": "github",   "name": "GitHub",   "url": "https://github.com",
             "icon_type": "favicon", "icon_preset": "", "icon_color": "#FFFFFF",
             "icon_url": "", "bg_color": "#1F2937"},
            {"id": "supabase", "name": "Supabase", "url": "https://supabase.com",
             "icon_type": "favicon", "icon_preset": "", "icon_color": "#FFFFFF",
             "icon_url": "", "bg_color": "#3ECF8E"},
        ]
        save_config(cfg)

# ── Prompts ───────────────────────────────────────────────────────────────────

PRIVACY = (
    "IMPORTANT: Do not mention brand names, logos, or text; "
    "describe recognizable people by features only, never by name; "
    "do not identify protected symbols."
)

def full_prompt(language: str, max_colors: int) -> str:
    return f"""{PRIVACY}

Analyze the image and respond EXCLUSIVELY with a valid JSON object.
No explanatory text, no markdown code blocks – pure JSON only.
Output language: {language}

{{
  "titel": "Short image title (max 8 words)",
  "beschreibung": "Factual image description, 2–4 sentences, max 500 characters",
  "dominante_farben": ["Color1", "Color2"],
  "ist_fotografie": true,
  "kunstart": "e.g. Landscape Photography | Oil Painting | Digital Illustration",
  "epoche": "e.g. Contemporary | Impressionism | 1980s",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}}

dominante_farben: exactly {max_colors} color names | tags: 5–10 keywords, no brand names"""

REGEN_PROMPTS = {
    "titel":            "Generate a new short image title (max 8 words). Title only.",
    "beschreibung":     "Write a new factual image description (2–4 sentences, max 500 chars). Text only.",
    "dominante_farben": "Name the 2–3 dominant colors as a comma-separated list. Color names only.",
    "ist_fotografie":   'Is this image a photograph? Answer only with "true" or "false".',
    "kunstart":         "What art style/genre does this image show? Answer in 2–5 words.",
    "epoche":           "What epoch/period does this image belong to? Answer in 1–4 words.",
    "tags":             "Generate 5–8 relevant tags as a comma-separated list. No brand names.",
}

SHOP_PROMPTS = {
    "titel":            "You optimize texts for an online poster shop.\nCreate a sales-promoting poster title. No brand names, no personal names. Max 10 words. Title only.\n\nBase: {value}",
    "beschreibung":     "You optimize texts for an online poster shop.\nWrite an emotional, sales-promoting product description (2–3 sentences). No brand names, no personal names. Inspire desire to buy.\n\nBase: {value}",
    "tags":             "You optimize texts for an online poster shop.\nCreate 8–12 SEO-optimized, comma-separated search terms. Suitable for buyer searches, no brand names. Only the comma-separated list.\n\nBase: {value}",
    "kunstart":         "You optimize texts for an online poster shop.\nFormulate an appealing category/style designation. Max 5 words. Text only.\n\nBase: {value}",
    "epoche":           "You optimize texts for an online poster shop.\nFormulate style/epoch attractively for shop filters. Max 4 words. Text only.\n\nBase: {value}",
    "dominante_farben": "You optimize texts for an online poster shop.\nDescribe the color palette poetically for a product description. Comma-separated list. Color names only.\n\nBase: {value}",
}

TRANSLATABLE = ["titel", "beschreibung", "dominante_farben", "kunstart", "epoche", "tags"]

def social_media_prompt(context: dict, boards: list, locations: list, target_url: str) -> str:
    boards_str = "\n".join(f"- {b}" for b in boards) if boards else "- General Art"
    loc_str    = ", ".join(locations) if locations else "Stuttgart"
    tags_raw   = context.get("tags", [])
    tags_str   = ", ".join(tags_raw) if isinstance(tags_raw, list) else str(tags_raw)
    colors_raw = context.get("dominante_farben", [])
    colors_str = ", ".join(colors_raw) if isinstance(colors_raw, list) else str(colors_raw)

    return f"""You are creating social media content for 'dekoire.com', an art print and poster shop.

Analyzed image data:
- Title: {context.get("titel", "")}
- Description: {context.get("beschreibung", "")}
- Art style: {context.get("kunstart", "")}
- Epoch/Period: {context.get("epoche", "")}
- Dominant colors: {colors_str}
- Tags: {tags_str}
- Is Photography: {context.get("ist_fotografie", "")}

Return ONLY a valid JSON object — no markdown, no commentary:

{{
  "pinterest": {{
    "titel": "SEO-optimized Pinterest title, max 100 characters",
    "beschreibung": "Engaging Pinterest description, 100–200 words, keyword-rich for discovery",
    "ziel_url": "{target_url}",
    "alt_text": "Detailed, accessibility-friendly alt text that works best for search, max 500 characters",
    "board": "Most fitting board from this list:\\n{boards_str}",
    "board_section": "Most fitting board section if available, or empty string"
  }},
  "instagram": {{
    "title": "Short, catchy product name for Instagram",
    "description": "Engaging Instagram caption with relevant emojis, 100–150 words, storytelling style",
    "tags": ["#hashtag1", "#hashtag2", "#hashtag3", "#hashtag4", "#hashtag5", "#hashtag6", "#hashtag7", "#hashtag8"],
    "location": "Most fitting location from: {loc_str}",
    "alt_text": "Detailed accessibility alt text for the image, max 500 chars",
    "content_type": "post"
  }}
}}"""


def shops_prompt(context: dict, shop_cfg: dict) -> str:
    """Generate Etsy, Shopify, Amazon fields from product context."""
    shopify_types   = ", ".join(shop_cfg.get("shopify",{}).get("synced_product_types", [])) or "Art Print, Poster, Wall Art"
    shopify_colls   = ", ".join(shop_cfg.get("shopify",{}).get("synced_collections",   [])) or "Art, Prints"
    etsy_ship       = ", ".join([p.get("title","") for p in shop_cfg.get("etsy",{}).get("synced_shipping_profiles", [])]) or "Standard Shipping"
    amazon_cats     = ", ".join(shop_cfg.get("amazon",{}).get("synced_categories", [])) or "Art & Photography"

    tags_raw   = context.get("tags", [])
    tags_str   = ", ".join(tags_raw) if isinstance(tags_raw, list) else str(tags_raw)
    colors_raw = context.get("dominante_farben", [])
    colors_str = ", ".join(colors_raw) if isinstance(colors_raw, list) else str(colors_raw)

    return f"""{PRIVACY}

You are an expert e-commerce copywriter for an art print and poster shop. Generate platform-optimized product listings.

Product context:
- Title: {context.get("titel", "")}
- Description: {context.get("beschreibung", "")}
- Art style: {context.get("kunstart", "")}
- Epoch: {context.get("epoche", "")}
- Colors: {colors_str}
- Tags: {tags_str}
- Is Photography: {context.get("ist_fotografie", "")}

Return ONLY a valid JSON object — no markdown, no commentary:

{{
  "etsy": {{
    "title": "SEO-optimized Etsy title max 140 chars, keyword-rich",
    "description": "Engaging Etsy product description 150-250 words, storytelling, include care instructions",
    "tags": "tag1, tag2, tag3, tag4, tag5, tag6, tag7, tag8, tag9, tag10, tag11, tag12, tag13",
    "materials": "e.g. Fine Art Paper, Archival Ink, Canvas",
    "who_made": "i_did",
    "when_made": "2020_2024",
    "occasion": "most fitting from: anniversary, birthday, christmas, easter, graduation, halloween, housewarming, mothers_day, valentines, wedding, or leave empty",
    "recipient": "most fitting from: babies_and_toddlers, children, friends, grandparents, men, mothers, teens, women, or leave empty"
  }},
  "shopify": {{
    "title": "Clear, concise Shopify product title",
    "body_html": "<p>HTML product description 100-200 words, emotionally engaging</p>",
    "vendor": "dekoire",
    "product_type": "most fitting from: {shopify_types}",
    "tags": "comma-separated SEO tags for Shopify",
    "sku": "auto-generated SKU suggestion like DK-ART-001",
    "collection": "most fitting from: {shopify_colls}"
  }},
  "amazon": {{
    "title": "Amazon title max 200 chars, brand + key features + keywords",
    "description": "Amazon product description max 2000 chars, keyword-rich",
    "bullet_1": "Key feature 1 (material/quality) max 200 chars",
    "bullet_2": "Key feature 2 (dimensions/format) max 200 chars",
    "bullet_3": "Key feature 3 (use case/room) max 200 chars",
    "bullet_4": "Key feature 4 (gifting/occasion) max 200 chars",
    "bullet_5": "Key feature 5 (brand/artist) max 200 chars",
    "search_terms": "backend keywords space-separated max 250 bytes",
    "brand": "dekoire"
  }}
}}"""

# ── Claude helpers ────────────────────────────────────────────────────────────

def call_with_image(client, model, img_data, mime, prompt, max_tokens=1024):
    return client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_data}},
            {"type": "text",  "text": prompt},
        ]}],
    ).content[0].text.strip()

def call_text(client, model, prompt, max_tokens=1024):
    return client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ).content[0].text.strip()

def parse_json(raw: str) -> dict:
    """Robustly extract and parse the first JSON object from a raw Claude response.

    Handles:
    - Markdown code fences (```json ... ``` or ``` ... ```)
    - Leading/trailing prose text
    - Unicode / smart-quote issues
    - BOM or whitespace padding
    """
    text = raw.strip().lstrip("\ufeff")  # strip BOM

    # 1. Strip markdown code fences and try parsing the inner block
    if "```" in text:
        parts = re.split(r"```(?:json)?\s*", text)
        for part in parts:
            part = part.strip()
            if part.startswith("{"):
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    pass

    # 2. Try direct parse (Claude returned clean JSON)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. Find the outermost {...} block by brace-matching
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        # Try replacing problematic unicode quotes before giving up
                        fixed = (
                            candidate
                            .replace("\u201c", '"').replace("\u201d", '"')
                            .replace("\u2018", "'").replace("\u2019", "'")
                        )
                        try:
                            return json.loads(fixed)
                        except json.JSONDecodeError:
                            break  # fall through to error

    raise ValueError(
        f"Could not extract valid JSON from Claude response. "
        f"First 400 chars: {text[:400]!r}"
    )

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    if session.get("user_email"):
        return redirect(url_for("dashboard"))
    cfg = load_config()
    pt  = cfg.get("page_texts", {}).get("login", {})
    return render_template("login.html",
                           login_logo     = cfg.get("login_logo", "dekoire-dark.png"),
                           header_title   = cfg.get("header_title", "Image Analyzer"),
                           page_title     = pt.get("title",    cfg.get("header_title", "Image Analyzer")),
                           login_subtitle = pt.get("subtitle", cfg.get("login_subtitle", "Bitte melde dich an, um fortzufahren.")))

@app.route("/auth/login", methods=["POST"])
def auth_login():
    data     = request.json or {}
    email    = data.get("email", "").strip()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "E-Mail und Passwort erforderlich."}), 400
    try:
        user = authenticate(email, password)
        session["user_email"] = user["email"]
        session["user_id"]    = user["id"]
        if user["id"] != "local":
            profile = get_user_profile(user["id"])
            session["user_vorname"]        = profile.get("vorname", "")
            session["user_nachname"]       = profile.get("nachname", "")
            session["user_profile_image"]  = profile.get("profile_image_url", "")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Login fehlgeschlagen. Bitte Zugangsdaten prüfen."}), 401

@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"success": True})

# ── Main routes ───────────────────────────────────────────────────────────────

@app.route("/")
@require_auth
def dashboard():
    cfg      = load_config()
    vorname  = session.get("user_vorname", "")
    nachname = session.get("user_nachname", "")
    name     = f"{vorname} {nachname}".strip() or session.get("user_email", "")
    ext_apps = cfg.get("external_apps", [])
    # Pre-compute domain for favicon fetching in template
    for app in ext_apps:
        url = app.get("url", "")
        app["domain"] = url.replace("https://", "").replace("http://", "").split("/")[0]
    return render_template("dashboard.html",
                           header_logo               = cfg.get("header_logo", "logo-white.png"),
                           header_title              = cfg.get("header_title", "Image Analyzer"),
                           current_user_email        = session.get("user_email", ""),
                           current_user_name         = name,
                           current_user_profile_image= session.get("user_profile_image", ""),
                           external_apps             = ext_apps,
                           icon_svgs                 = ICON_PRESETS)

# ── Social Media Dashboards ──────────────────────────────────────────────────

def _all_social_posts(platform: str | None = None) -> list:
    """Load all social posts across all products, optionally filtered by platform."""
    cfg     = load_config()
    sb_cfg  = cfg.get("supabase", {})
    svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")

    # Try Supabase first
    if _SUPABASE_OK and sb_cfg.get("url") and svc_key:
        try:
            sb = _sb_create(sb_cfg["url"], svc_key)
            q  = sb.table("social_posts") \
                   .select("id,product_id,platform,status,scheduled_at,caption,pin_title,image_count,created_at,error_message,hashtags") \
                   .order("created_at", desc=True).limit(200)
            if platform:
                q = q.eq("platform", platform)
            res = q.execute()
            return res.data or []
        except Exception:
            pass

    # Fallback: scan all local JSON files
    all_posts: list = []
    if SOCIAL_POSTS_LOCAL.exists():
        for jf in SOCIAL_POSTS_LOCAL.glob("*.json"):
            try:
                posts = json.loads(jf.read_text(encoding="utf-8"))
                for p in posts:
                    if not platform or p.get("platform") == platform:
                        stripped = {k: v for k, v in p.items()
                                    if k not in ("image_data", "image_public_urls")}
                        all_posts.append(stripped)
            except Exception:
                pass
    all_posts.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return all_posts

def _delete_social_post(post_id: str):
    """Remove a post from Supabase and/or local JSON files."""
    cfg     = load_config()
    sb_cfg  = cfg.get("supabase", {})
    svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")
    if _SUPABASE_OK and sb_cfg.get("url") and svc_key:
        try:
            sb = _sb_create(sb_cfg["url"], svc_key)
            sb.table("social_posts").delete().eq("id", post_id).execute()
        except Exception:
            pass
    # Also clean local files
    if SOCIAL_POSTS_LOCAL.exists():
        for jf in SOCIAL_POSTS_LOCAL.glob("*.json"):
            try:
                posts = json.loads(jf.read_text(encoding="utf-8"))
                new   = [p for p in posts if p.get("id") != post_id]
                if len(new) != len(posts):
                    jf.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

@app.route("/social/instagram")
@require_auth
def social_instagram():
    cfg = load_config()
    return render_template("social_posts.html",
                           platform     = "instagram",
                           header_logo  = cfg.get("header_logo",  "logo-white.png"),
                           header_title = cfg.get("header_title", "Image Analyzer"),
                           current_user_email = session.get("user_email", ""))

# ── Pinterest OAuth ──────────────────────────────────────────────────────────

_PIN_SCOPES        = "boards:read,boards:write,pins:read,pins:write,user_accounts:read"
_PIN_API_PROD      = "https://api.pinterest.com/v5"
_PIN_API_SANDBOX   = "https://api-sandbox.pinterest.com/v5"
_PIN_OAUTH_PROD    = "https://www.pinterest.com/oauth/"
_PIN_OAUTH_SANDBOX = "https://www.pinterest.com/oauth/"  # same auth URL, different API base

def _pin_base(pin_cfg: dict) -> str:
    """Return the correct Pinterest API base URL based on environment setting."""
    return _PIN_API_SANDBOX if pin_cfg.get("environment", "sandbox") == "sandbox" else _PIN_API_PROD

def _pin_token(pin_cfg: dict) -> str:
    """Return the token for the current environment, falling back to legacy access_token."""
    env = pin_cfg.get("environment", "sandbox")
    legacy = pin_cfg.get("access_token", "").strip()
    if env == "sandbox":
        return pin_cfg.get("access_token_sandbox", "").strip() or legacy
    return pin_cfg.get("access_token_prod", "").strip() or legacy

@app.route("/api/pinterest/redirect-uri")
@require_auth
def pinterest_redirect_uri():
    uri = url_for("pinterest_oauth_callback", _external=True)
    return jsonify({"redirect_uri": uri})

@app.route("/auth/pinterest")
@require_auth
def pinterest_oauth_start():
    """Redirect user to Pinterest authorization page."""
    import urllib.parse as _up
    cfg     = load_config()
    pin_cfg = cfg.get("pinterest_posting", {})
    client_id = pin_cfg.get("client_id", "").strip()
    if not client_id:
        return redirect(url_for("settings_page") + "?error=pinterest_no_client_id")
    redirect_uri = url_for("pinterest_oauth_callback", _external=True)
    params = _up.urlencode({
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         _PIN_SCOPES,
    })
    return redirect(f"https://www.pinterest.com/oauth/?{params}")


@app.route("/auth/pinterest/callback")
@require_auth
def pinterest_oauth_callback():
    """Exchange authorization code for access token and save it."""
    import urllib.request as _ur, urllib.parse as _up, urllib.error as _ue, ssl as _ssl
    code  = request.args.get("code", "")
    error = request.args.get("error", "")
    if error or not code:
        msg = request.args.get("error_description", error or "Abgebrochen")
        return redirect(url_for("settings_page") + f"?pin_error={_up.quote(msg)}")

    cfg     = load_config()
    pin_cfg = cfg.setdefault("pinterest_posting", {})
    client_id     = pin_cfg.get("client_id", "").strip()
    client_secret = pin_cfg.get("client_secret", "").strip()
    redirect_uri  = url_for("pinterest_oauth_callback", _external=True)

    if not client_id or not client_secret:
        return redirect(url_for("settings_page") + "?pin_error=App-ID+oder+Geheimschlüssel+fehlt")

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = _ssl.CERT_NONE

    body = _up.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": redirect_uri,
    }).encode()
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    env         = pin_cfg.get("environment", "sandbox")
    token_field = "access_token_sandbox" if env == "sandbox" else "access_token_prod"
    req = _ur.Request(
        "https://api.pinterest.com/v5/oauth/token",
        data=body,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with _ur.urlopen(req, timeout=15, context=ctx) as r:
            data = json.loads(r.read())
        token = data.get("access_token", "")
        if not token:
            raise ValueError("Kein Token in der Antwort")
        pin_cfg[token_field]    = token
        pin_cfg["access_token"] = token  # keep legacy field in sync
        if data.get("refresh_token"):
            pin_cfg[f"refresh_token_{env}"] = data["refresh_token"]
        save_config(cfg)
        return redirect(url_for("settings_page") + f"?pin_connected=1&pin_env={env}")
    except _ue.HTTPError as e:
        body_err = e.read().decode("utf-8", "replace")
        import urllib.parse as _up2
        return redirect(url_for("settings_page") + f"?pin_error={_up2.quote(body_err[:200])}")
    except Exception as e:
        import urllib.parse as _up2
        return redirect(url_for("settings_page") + f"?pin_error={_up2.quote(str(e))}")


@app.route("/api/pinterest/create-board", methods=["POST"])
@require_auth
def api_pinterest_create_board():
    """Create a new board via Pinterest API."""
    import urllib.request as _ur, urllib.error as _ue, ssl as _ssl
    data    = request.json or {}
    name    = data.get("name", "").strip()
    privacy = data.get("privacy", "PUBLIC")
    if not name:
        return jsonify({"error": "Name fehlt"}), 400
    cfg     = load_config()
    pin_cfg = cfg.get("pinterest_posting", {})
    token   = _pin_token(pin_cfg)
    base    = _pin_base(pin_cfg)
    if not token:
        return jsonify({"error": "Pinterest Token fehlt"}), 400
    ctx = _ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=_ssl.CERT_NONE
    body = json.dumps({"name": name, "privacy": privacy}).encode()
    req  = _ur.Request(f"{base}/boards", data=body,
                       headers={"Content-Type":"application/json","Authorization":f"Bearer {token}","User-Agent":"Mozilla/5.0"},
                       method="POST")
    try:
        with _ur.urlopen(req, timeout=15, context=ctx) as r:
            result = json.loads(r.read())
        return jsonify({"success": True, "board": result})
    except _ue.HTTPError as e:
        return jsonify({"error": f"Pinterest API {e.code}: {e.read().decode('utf-8','replace')}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/pinterest/boards")
@require_auth
def api_pinterest_boards():
    """Fetch boards from Pinterest API v5 in real-time."""
    import urllib.request as _ur, urllib.error as _ue, ssl as _ssl
    cfg     = load_config()
    pin_cfg = cfg.get("pinterest_posting", {})
    env     = pin_cfg.get("environment", "sandbox")

    # For board loading, if the sandbox-specific token is missing fall back to
    # the production token + production API so boards are still accessible.
    token = _pin_token(pin_cfg)
    if env == "sandbox" and not pin_cfg.get("access_token_sandbox", "").strip():
        token = pin_cfg.get("access_token_prod", pin_cfg.get("access_token", "")).strip()
        base  = _PIN_API_PROD
    else:
        base  = _pin_base(pin_cfg)

    masked  = (token[:6] + "…" + token[-4:]) if len(token) > 12 else ("(leer)" if not token else token)
    print(f"[Pinterest Boards] env={env}  token={masked}  base={base}")
    if not token:
        return jsonify({"error": f"Pinterest Access Token fehlt – bitte unter Einstellungen → Pinterest API verbinden.", "boards": []}), 200
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = _ssl.CERT_NONE
    boards = []
    cursor = None
    try:
        while True:
            url = f"{base}/boards?page_size=250"
            if cursor:
                url += f"&bookmark={cursor}"
            req = _ur.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "User-Agent":    "Mozilla/5.0",
            })
            with _ur.urlopen(req, timeout=15, context=ctx) as r:
                data = json.loads(r.read())
            for b in data.get("items", []):
                boards.append({"id": b.get("id",""), "name": b.get("name","")})
            cursor = data.get("bookmark")
            if not cursor:
                break
    except _ue.HTTPError as e:
        body_err = e.read().decode("utf-8", "replace")
        if e.code == 401:
            return jsonify({
                "error": (
                    "Pinterest: Token ungültig oder abgelaufen (401). "
                    "Bitte einen neuen Token mit den Scopes boards:read und pins:write generieren: "
                    "developers.pinterest.com → Apps → deine App → Generate Access Token."
                ),
                "boards": []
            }), 200
        if e.code == 403:
            return jsonify({
                "error": (
                    "Pinterest: Fehlende Berechtigung (403). "
                    "Der Token benötigt den Scope boards:read – bitte Token neu generieren."
                ),
                "boards": []
            }), 200
        return jsonify({"error": f"Pinterest API Fehler {e.code}: {body_err}", "boards": []}), 200
    except Exception as e:
        return jsonify({"error": str(e), "boards": []}), 200
    return jsonify({"boards": boards})

@app.route("/social/pinterest")
@require_auth
def social_pinterest():
    cfg = load_config()
    return render_template("social_posts.html",
                           platform     = "pinterest",
                           header_logo  = cfg.get("header_logo",  "logo-white.png"),
                           header_title = cfg.get("header_title", "Image Analyzer"),
                           current_user_email = session.get("user_email", ""))

@app.route("/api/social/posts")
@require_auth
def api_all_social_posts():
    platform = request.args.get("platform")
    return jsonify(_all_social_posts(platform or None))

@app.route("/api/dashboard/stats")
@require_auth
def api_dashboard_stats():
    cfg     = load_config()
    sb_cfg  = cfg.get("supabase", {})
    svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")
    stats   = {"products_total": None, "products_etsy": None,
               "posts_pinterest": None, "posts_instagram": None}
    # Product counts
    if _SUPABASE_OK and sb_cfg.get("url") and sb_cfg.get("anon_key"):
        try:
            sb    = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
            table = sb_cfg.get("table_name", "image_analyses")
            res   = sb.table(table).select("id,etsy_title", count="exact").execute()
            rows  = res.data or []
            stats["products_total"] = res.count if res.count is not None else len(rows)
            stats["products_etsy"]  = sum(1 for r in rows if r.get("etsy_title","").strip())
        except Exception:
            pass
    # Social post counts (ok = posted/scheduled, fail = error/failed)
    try:
        all_posts = _all_social_posts()
        for plat, key in (("pinterest","pin"), ("instagram","ig")):
            plat_posts = [p for p in all_posts if p.get("platform") == plat]
            stats[f"posts_{key}_ok"]   = sum(1 for p in plat_posts if p.get("status") in ("posted","scheduled","success"))
            stats[f"posts_{key}_fail"] = sum(1 for p in plat_posts if p.get("status") in ("error","failed"))
            stats[f"posts_{key}_total"]= len(plat_posts)
    except Exception:
        pass
    return jsonify(stats)

@app.route("/api/social/post/<post_id>", methods=["DELETE"])
@require_auth
def api_delete_social_post(post_id):
    try:
        _delete_social_post(post_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/app")
@require_auth
def produkt_create():
    cfg     = load_config()
    pin_cfg = cfg.get("pinterest", {})
    ig_cfg  = cfg.get("instagram",  {})
    pt      = cfg.get("page_texts", {}).get("index", {})
    shops_cfg   = cfg.get("shops", {})
    shopify_cfg = shops_cfg.get("shopify", {})
    etsy_cfg    = shops_cfg.get("etsy", {})
    amazon_cfg  = shops_cfg.get("amazon", {})
    return render_template(
        "produkt_create.html",
        header_logo              = cfg.get("header_logo",    "logo-white.png"),
        header_title             = cfg.get("header_title",   "Image Analyzer"),
        page_title               = pt.get("title",    "Produkt anlegen"),
        page_subtitle            = pt.get("subtitle", ""),
        current_user_email       = session.get("user_email", ""),
        pinterest_boards         = pin_cfg.get("boards",         []),
        pinterest_target_url     = pin_cfg.get("target_url",     "https://dekoire.com"),
        instagram_locations      = ig_cfg.get("locations",       []),
        instagram_default_location = ig_cfg.get("default_location", "Stuttgart"),
        shopify_collections      = shopify_cfg.get("synced_collections", []),
        shopify_product_types    = shopify_cfg.get("synced_product_types", []),
        etsy_shipping_profiles   = etsy_cfg.get("synced_shipping_profiles", []),
        amazon_categories        = amazon_cfg.get("synced_categories", []),
    )

@app.route("/social/post/create")
@require_auth
def social_post_create_page():
    cfg = load_config()
    pin_cfg = cfg.get("pinterest_posting", {})
    sm_cfg  = cfg.get("social_media", {})
    return render_template("social_post_create.html",
        cfg                = cfg,
        header_logo        = cfg.get("header_logo", "logo-white.png"),
        current_user_email = session.get("user_email", ""),
        pin_environment    = pin_cfg.get("environment", "sandbox"),
        image_gen_url      = sm_cfg.get("image_gen_url", "").strip(),
        image_gen_name     = sm_cfg.get("image_gen_name", "").strip() or "Bilder generieren",
    )

@app.route("/api/products/list")
@require_auth
def api_products_list():
    """Return products sorted newest-first for social post creation picker."""
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    if not (_SUPABASE_OK and sb_cfg.get("url") and sb_cfg.get("anon_key")):
        return jsonify([])
    try:
        sb    = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
        table = sb_cfg.get("table_name", "image_analyses")
        res   = sb.table(table).select(
            "id,dekoire_id,titel,image_url,created_at,"
            "pin_board,pin_board_id,pin_titel,pin_beschreibung,pin_ziel_url,pin_alt_text,pin_media_url,"
            "ig_title,ig_description,ig_tags,ig_location"
        ).order("created_at", desc=True).limit(200).execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/products")
@require_auth
def products_page():
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    rows   = []
    error  = None
    if _SUPABASE_OK and sb_cfg.get("url") and sb_cfg.get("anon_key"):
        try:
            sb    = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
            table = sb_cfg.get("table_name", "image_analyses")
            res   = sb.table(table).select(
                "id,dekoire_id,titel,ausrichtung,created_at,image_url,kunstart,tags"
            ).order("created_at", desc=True).execute()
            rows = res.data or []

            # Social post presence map  {product_id: {ig: n, pin: n}}
            svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")
            sp_map: dict = {}
            if svc_key and rows:
                try:
                    sb2    = _sb_create(sb_cfg["url"], svc_key)
                    sp_res = sb2.table("social_posts")                                 .select("product_id,platform,status").execute()
                    for sp in (sp_res.data or []):
                        pid  = sp.get("product_id","")
                        plat = sp.get("platform","")
                        if pid not in sp_map:
                            sp_map[pid] = {"ig": 0, "pin": 0}
                        if plat == "instagram":
                            sp_map[pid]["ig"]  += 1
                        elif plat == "pinterest":
                            sp_map[pid]["pin"] += 1
                except Exception:
                    pass
            for r in rows:
                r["_sp"] = sp_map.get(r.get("id",""), {"ig": 0, "pin": 0})

        except Exception as e:
            error = str(e)
    pt = cfg.get("page_texts", {}).get("products", {})
    return render_template("products.html",
                           rows               = rows,
                           error              = error,
                           header_logo        = cfg.get("header_logo",  "logo-white.png"),
                           header_title       = cfg.get("header_title", "Image Analyzer"),
                           page_title         = pt.get("title",    "Alle Produkte"),
                           page_subtitle      = pt.get("subtitle", ""),
                           current_user_email = session.get("user_email", ""))

@app.route("/shops")
@require_auth
def shops_page():
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    rows   = []
    if _SUPABASE_OK and sb_cfg.get("url") and sb_cfg.get("anon_key"):
        try:
            sb  = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
            res = sb.table(sb_cfg.get("table_name","image_analyses")) \
                    .select("id,titel,image_url,created_at,etsy_title,shopify_title,amazon_title,beschreibung,ausrichtung") \
                    .order("created_at", desc=True).execute()
            rows = res.data or []
        except Exception:
            rows = []
    return render_template("shops.html",
        rows               = rows,
        header_logo        = cfg.get("header_logo",  "logo-white.png"),
        header_title       = cfg.get("header_title", "Image Analyzer"),
        current_user_email = session.get("user_email", ""),
    )

@app.route("/api/products/bulk-delete", methods=["POST"])
@require_auth
def products_bulk_delete():
    ids    = (request.json or {}).get("ids", [])
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    if not ids or not _SUPABASE_OK or not sb_cfg.get("url"):
        return jsonify({"error": "Ungültige Anfrage"}), 400
    svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","") or sb_cfg.get("anon_key","")
    try:
        sb  = _sb_create(sb_cfg["url"], svc_key)
        res = sb.table(sb_cfg.get("table_name","image_analyses"))                 .delete().in_("id", ids).execute()
        return jsonify({"success": True, "deleted": len(res.data or [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/products/export", methods=["POST"])
@require_auth
def products_export():
    """Return full rows for given IDs so the client can build XLSX."""
    ids    = (request.json or {}).get("ids", [])
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    if not _SUPABASE_OK or not sb_cfg.get("url") or not ids:
        return jsonify([])
    try:
        sb  = _sb_create(sb_cfg["url"], sb_cfg.get("anon_key",""))
        res = sb.table(sb_cfg.get("table_name","image_analyses"))                 .select("*").in_("id", ids).execute()
        return jsonify({"ok": True, "rows": res.data or []})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/product/<product_id>")
@require_auth
def product_edit(product_id):
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    row    = {}
    error  = None
    if _SUPABASE_OK and sb_cfg.get("url") and sb_cfg.get("anon_key"):
        try:
            sb  = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
            res = sb.table(sb_cfg.get("table_name","image_analyses")) \
                    .select("*").eq("id", product_id).single().execute()
            row = res.data or {}
        except Exception as e:
            error = str(e)
    pin_cfg    = cfg.get("pinterest", {})
    ig_cfg     = cfg.get("instagram",  {})
    pt         = cfg.get("page_texts", {}).get("product_edit", {})
    shops_cfg  = cfg.get("shops", {})
    shopify_cfg = shops_cfg.get("shopify", {})
    etsy_cfg    = shops_cfg.get("etsy", {})
    amazon_cfg  = shops_cfg.get("amazon", {})
    legal_check_result = _load_legal_check(product_id)
    return render_template("product_edit.html",
                           row                    = row,
                           error                  = error,
                           product_id             = product_id,
                           legal_check_result     = legal_check_result,
                           header_logo            = cfg.get("header_logo",  "logo-white.png"),
                           header_title           = cfg.get("header_title", "Image Analyzer"),
                           page_title             = pt.get("title",    "Produkt bearbeiten"),
                           page_subtitle          = pt.get("subtitle", ""),
                           current_user_email     = session.get("user_email", ""),
                           pinterest_boards       = pin_cfg.get("boards", []),
                           instagram_locations    = ig_cfg.get("locations", []),
                           shopify_collections    = shopify_cfg.get("synced_collections", []),
                           shopify_product_types  = shopify_cfg.get("synced_product_types", []),
                           etsy_shipping_profiles = etsy_cfg.get("synced_shipping_profiles", []),
                           amazon_categories      = amazon_cfg.get("synced_categories", []))

@app.route("/api/product/<product_id>/save", methods=["POST"])
@require_auth
def product_save(product_id):
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    if not _SUPABASE_OK or not sb_cfg.get("url"):
        return jsonify({"error": "Supabase not configured."}), 500
    try:
        updates = request.json or {}
        sb  = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
        sb.table(sb_cfg.get("table_name","image_analyses")) \
          .update(updates).eq("id", product_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/product/<product_id>/publish/shopify", methods=["POST"])
@require_auth
def publish_product_shopify(product_id):
    import urllib.request as _ur, urllib.error as _ue, ssl as _ssl, base64 as _b64
    cfg       = load_config()
    shop_cfg  = cfg.get("shops", {}).get("shopify", {})
    store_url = shop_cfg.get("store_url", "").strip().rstrip("/")
    api_key   = shop_cfg.get("api_key", "").strip()
    api_pass  = shop_cfg.get("api_password", "").strip()
    if not store_url or not api_key or not api_pass:
        return jsonify({"error": "Shopify-Zugangsdaten fehlen (Einstellungen → Zugangsdaten → Shopify)."}), 200
    data = request.json or {}
    ctx = _ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = _ssl.CERT_NONE
    body = {
        "product": {
            "title":        data.get("shopify_title", ""),
            "body_html":    data.get("shopify_body_html", ""),
            "vendor":       data.get("shopify_vendor", ""),
            "product_type": data.get("shopify_product_type", ""),
            "tags":         data.get("shopify_tags", ""),
            "status":       data.get("shopify_status", "draft"),
            "variants": [{
                "price":           str(data.get("shopify_price") or "0"),
                "compare_at_price": str(data.get("shopify_compare_price") or "") or None,
                "sku":             data.get("shopify_sku", ""),
            }],
        }
    }
    creds    = _b64.standard_b64encode(f"{api_key}:{api_pass}".encode()).decode()
    endpoint = f"{store_url}/admin/api/2024-01/products.json"
    raw      = json.dumps(body).encode()
    req = _ur.Request(endpoint, data=raw, headers={
        "Content-Type": "application/json",
        "Authorization": f"Basic {creds}",
    }, method="POST")
    try:
        with _ur.urlopen(req, timeout=20, context=ctx) as r:
            resp = json.loads(r.read())
        sid = resp.get("product", {}).get("id", "")
        return jsonify({"success": True, "message": f"Shopify-Produkt angelegt (ID: {sid})", "shopify_product_id": sid})
    except _ue.HTTPError as e:
        err = e.read().decode("utf-8", "replace")
        return jsonify({"error": f"Shopify API {e.code}: {err}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 200


@app.route("/api/product/<product_id>/publish/etsy", methods=["POST"])
@require_auth
def publish_product_etsy(product_id):
    import urllib.request as _ur, urllib.error as _ue, ssl as _ssl
    cfg      = load_config()
    etsy_cfg = cfg.get("shops", {}).get("etsy", {})
    api_key  = etsy_cfg.get("api_key", "").strip()
    shop_id  = etsy_cfg.get("shop_id", "").strip()
    if not api_key or not shop_id:
        return jsonify({"error": "Etsy-Zugangsdaten fehlen (Einstellungen → Zugangsdaten → Etsy)."}), 200
    data    = request.json or {}
    ctx     = _ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = _ssl.CERT_NONE
    tags_raw = data.get("etsy_tags", "")
    tags     = [t.strip() for t in tags_raw.split(",") if t.strip()][:13]
    price    = data.get("etsy_price") or 0
    body = {
        "title":            data.get("etsy_title", ""),
        "description":      data.get("etsy_description", ""),
        "price":            float(price),
        "quantity":         999,
        "who_made":         data.get("etsy_who_made", "i_did"),
        "when_made":        data.get("etsy_when_made", "2020_2024"),
        "taxonomy_id":      2078,   # Art & Collectibles → Prints
        "tags":             tags,
        "materials":        [m.strip() for m in data.get("etsy_materials", "").split(",") if m.strip()],
        "shipping_profile_id": int(data.get("etsy_shipping_profile") or 0) or None,
        "state":            "draft",
        "type":             "download",
    }
    if not body["shipping_profile_id"]:
        body.pop("shipping_profile_id")
    raw  = json.dumps(body).encode()
    url  = f"https://openapi.etsy.com/v3/application/shops/{shop_id}/listings"
    req  = _ur.Request(url, data=raw, headers={
        "Content-Type":  "application/json",
        "x-api-key":     api_key,
    }, method="POST")
    try:
        with _ur.urlopen(req, timeout=20, context=ctx) as r:
            resp = json.loads(r.read())
        lid = resp.get("listing_id", "")
        return jsonify({"success": True, "message": f"Etsy-Listing angelegt (ID: {lid})", "listing_id": lid})
    except _ue.HTTPError as e:
        err = e.read().decode("utf-8", "replace")
        if e.code == 401:
            return jsonify({"error": "Etsy: Authentifizierung fehlgeschlagen (401). Der API-Key reicht für schreibende Operationen nicht aus – ein OAuth2-Token wird benötigt."}), 200
        return jsonify({"error": f"Etsy API {e.code}: {err}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 200


@app.route("/api/product/<product_id>/publish/amazon", methods=["POST"])
@require_auth
def publish_product_amazon(product_id):
    return jsonify({
        "error": (
            "Amazon SP-API erfordert AWS-Authentifizierung (Signature V4) und ist noch nicht implementiert. "
            "Exportiere die Felder über den CSV/Excel-Export und lade sie im Amazon Seller Central hoch."
        )
    }), 200


@app.route("/api/product/<product_id>/delete", methods=["POST"])
@require_auth
def product_delete(product_id):
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    if not _SUPABASE_OK or not sb_cfg.get("url"):
        return jsonify({"error": "Supabase not configured."}), 500
    try:
        # service_role_key bypasses RLS – required for DELETE
        svc_key = sb_cfg.get("service_role_key", "") or cfg.get("service_role_key", "")
        key     = svc_key or sb_cfg.get("anon_key", "")
        sb      = _sb_create(sb_cfg["url"], key)
        res     = sb.table(sb_cfg.get("table_name", "image_analyses")) \
                    .delete().eq("id", product_id).execute()
        deleted = len(res.data or [])
        if deleted == 0:
            return jsonify({"error": "Kein Eintrag gefunden oder RLS blockiert Delete.", "hint": "service_role_key prüfen"}), 404
        return jsonify({"success": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Product Photos ────────────────────────────────────────────────────────────

def _photo_dir(product_id: str) -> Path:
    d = PRODUCT_PHOTOS_DIR / product_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def _load_photos(product_id: str) -> list:
    meta = _photo_dir(product_id) / "photos.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def _save_photos(product_id: str, photos: list):
    meta = _photo_dir(product_id) / "photos.json"
    meta.write_text(json.dumps(photos, ensure_ascii=False, indent=2), encoding="utf-8")

PHOTO_PROMPT = """You are an e-commerce product photo analyst.
Analyze this product photo and return ONLY a JSON object with these two fields:
{
  "alt_text": "A clear, descriptive alt text for this product photo (1-2 sentences, mention product type, color, angle/perspective if visible, suitable for screen readers and SEO)",
  "tags": "comma-separated list of 6-10 relevant SEO tags in lowercase (product type, color, material, style, perspective, use-case)"
}
No markdown, no explanation — only the raw JSON object."""

@app.route("/api/product/<product_id>/photos", methods=["GET"])
@require_auth
def photos_list(product_id):
    return jsonify(_load_photos(product_id))

@app.route("/api/product/<product_id>/photos/upload", methods=["POST"])
@require_auth
def photos_upload(product_id):
    files = request.files.getlist("photos")
    if not files:
        return jsonify({"error": "Keine Dateien übermittelt"}), 400
    results = []
    cfg = load_config()
    photos = _load_photos(product_id)
    pdir   = _photo_dir(product_id)
    for f in files:
        try:
            raw   = f.read()
            ext   = Path(f.filename).suffix.lower() or ".jpg"
            pid   = uuid.uuid4().hex[:12]
            fname = f"{pid}{ext}"
            (pdir / fname).write_bytes(raw)
            url   = f"/static/product-photos/{product_id}/{fname}"
            # Analyze with Claude
            alt_text, tags = "", ""
            try:
                client, _ = get_client()
                b64 = base64.standard_b64encode(raw).decode()
                mime = "image/jpeg" if ext in (".jpg",".jpeg") else \
                       "image/png"  if ext == ".png"           else \
                       "image/webp" if ext == ".webp"          else "image/jpeg"
                msg = client.messages.create(
                    model=cfg.get("model", "claude-opus-4-5"),
                    max_tokens=512,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                        {"type": "text",  "text": PHOTO_PROMPT},
                    ]}],
                )
                parsed = json.loads(re.sub(r"```json|```", "", msg.content[0].text).strip())
                alt_text = parsed.get("alt_text", "")
                tags     = parsed.get("tags", "")
            except Exception as e:
                print(f"[Photo analyze] {e}")
            entry = {
                "id": pid, "filename": fname, "url": url,
                "alt_text": alt_text, "tags": tags,
                "created_at": __import__("datetime").datetime.utcnow().isoformat(),
            }
            photos.append(entry)
            results.append(entry)
        except Exception as e:
            results.append({"error": str(e), "filename": f.filename})
    _save_photos(product_id, photos)
    return jsonify({"photos": results})

@app.route("/api/product/<product_id>/photos/<photo_id>/save", methods=["POST"])
@require_auth
def photos_save(product_id, photo_id):
    data   = request.json or {}
    photos = _load_photos(product_id)
    for p in photos:
        if p["id"] == photo_id:
            p["alt_text"] = data.get("alt_text", p.get("alt_text", ""))
            p["tags"]     = data.get("tags",     p.get("tags", ""))
    _save_photos(product_id, photos)
    return jsonify({"success": True})

@app.route("/api/product/<product_id>/photos/<photo_id>/delete", methods=["POST"])
@require_auth
def photos_delete(product_id, photo_id):
    photos = _load_photos(product_id)
    match  = next((p for p in photos if p["id"] == photo_id), None)
    if match:
        try:
            (PRODUCT_PHOTOS_DIR / product_id / match["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
        photos = [p for p in photos if p["id"] != photo_id]
        _save_photos(product_id, photos)
    return jsonify({"success": True})

# ── Social Media Posting ───────────────────────────────────────────────────────

import datetime as _dt
import threading as _threading
import time as _time

SOCIAL_UPLOAD_DIR = SCRIPT_DIR / "static" / "social-uploads"
SOCIAL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SOCIAL_POSTS_LOCAL = SCRIPT_DIR / "static" / "social-posts"
SOCIAL_POSTS_LOCAL.mkdir(parents=True, exist_ok=True)

def _social_local_file(product_id: str) -> Path:
    return SOCIAL_POSTS_LOCAL / f"{product_id}.json"

def _load_social_posts(product_id: str) -> list:
    try:
        cfg     = load_config()
        sb_cfg  = cfg.get("supabase", {})
        svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")
        if _SUPABASE_OK and sb_cfg.get("url") and svc_key:
            sb  = _sb_create(sb_cfg["url"], svc_key)
            res = sb.table("social_posts").select("id,platform,status,scheduled_at,caption,pin_title,image_count,created_at,error_message") \
                    .eq("product_id", product_id).order("created_at", desc=True).limit(50).execute()
            return res.data or []
    except Exception:
        pass
    f = _social_local_file(product_id)
    if f.exists():
        try:
            posts = json.loads(f.read_text(encoding="utf-8"))
            # strip large base64 blobs for list view
            return [{k:v for k,v in p.items() if k not in ("image_data","image_public_urls")} for p in posts]
        except Exception:
            pass
    return []

def _save_social_post_record(post: dict):
    """Upsert to Supabase social_posts, fallback to local JSON."""
    try:
        cfg     = load_config()
        sb_cfg  = cfg.get("supabase", {})
        svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")
        if _SUPABASE_OK and sb_cfg.get("url") and svc_key:
            sb = _sb_create(sb_cfg["url"], svc_key)
            sb.table("social_posts").upsert({k:v for k,v in post.items() if k not in ("image_data","image_public_urls")}).execute()
            return
    except Exception as e:
        print(f"[SocialPost DB] {e}")
    # local fallback (keeps image_data)
    f = _social_local_file(post.get("product_id","_"))
    posts = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    posts = [p for p in posts if p.get("id") != post.get("id")]
    posts.insert(0, post)
    f.write_text(json.dumps(posts[:100], ensure_ascii=False, indent=2), encoding="utf-8")

def _update_social_post_status(post_id: str, product_id: str, updates: dict):
    try:
        cfg     = load_config()
        sb_cfg  = cfg.get("supabase", {})
        svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")
        if _SUPABASE_OK and sb_cfg.get("url") and svc_key:
            sb = _sb_create(sb_cfg["url"], svc_key)
            sb.table("social_posts").update(updates).eq("id", post_id).execute()
            return
    except Exception:
        pass
    f = _social_local_file(product_id)
    if f.exists():
        posts = json.loads(f.read_text(encoding="utf-8"))
        for p in posts:
            if p.get("id") == post_id:
                p.update(updates)
        f.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")

def _upload_to_storage(cfg: dict, image_bytes: bytes, filename: str) -> str:
    """Upload to Supabase Storage, return public URL."""
    sb_cfg  = cfg.get("supabase", {})
    bucket  = sb_cfg.get("storage_bucket","images")
    svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")
    if not _SUPABASE_OK or not sb_cfg.get("url") or not svc_key:
        raise ValueError("Supabase Storage nicht konfiguriert (service_role_key fehlt)")
    sb   = _sb_create(sb_cfg["url"], svc_key)
    path = f"social-uploads/{uuid.uuid4().hex[:12]}/{filename}"
    sb.storage.from_(bucket).upload(path, image_bytes, {"content-type":"image/jpeg"})
    return sb.storage.from_(bucket).get_public_url(path)

def _post_to_instagram(cfg: dict, public_urls: list, caption: str, hashtags: str) -> dict:
    import urllib.request as _ur, ssl as _ssl
    ig_cfg  = cfg.get("instagram_posting", {})
    token   = ig_cfg.get("access_token","")
    user_id = ig_cfg.get("user_id","")
    if not token or not user_id:
        raise ValueError("Instagram Access Token oder User ID fehlt (Einstellungen → Social Posting)")
    full_caption = caption + (f"\n\n{hashtags}" if hashtags else "")
    ctx = _ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=_ssl.CERT_NONE
    base = f"https://graph.facebook.com/v19.0/{user_id}"
    def _fb(path, params):
        raw = json.dumps(params).encode()
        req = _ur.Request(f"{base}{path}", data=raw, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"}, method="POST")
        try:
            with _ur.urlopen(req, timeout=30, context=ctx) as r:
                return json.loads(r.read())
        except _ur.HTTPError as e:
            body = e.read().decode("utf-8","replace")
            raise ValueError(f"IG API {e.code}: {body}")
    if len(public_urls) == 1:
        media = _fb("/media", {"image_url": public_urls[0], "caption": full_caption, "access_token": token})
        creation_id = media["id"]
    else:
        item_ids = [_fb("/media", {"image_url":u,"is_carousel_item":True,"access_token":token})["id"] for u in public_urls[:4]]
        media = _fb("/media", {"media_type":"CAROUSEL","children":",".join(item_ids),"caption":full_caption,"access_token":token})
        creation_id = media["id"]
    return _fb("/media_publish", {"creation_id": creation_id, "access_token": token})

def _post_to_pinterest(cfg: dict, image_bytes_list: list, title: str, description: str, board_id: str, link: str) -> dict:
    import urllib.request as _ur, urllib.error as _ue, ssl as _ssl
    pin_cfg = cfg.get("pinterest_posting", {})
    token   = _pin_token(pin_cfg)
    base    = _pin_base(pin_cfg)
    if not token:
        raise ValueError("Pinterest Access Token fehlt (Einstellungen → Pinterest API verbinden)")
    if not board_id:
        raise ValueError("Pinterest Board ID fehlt")
    ctx = _ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=_ssl.CERT_NONE
    def _pin(img_bytes, pin_title):
        b64  = base64.standard_b64encode(img_bytes).decode()
        body = {"board_id":board_id,"title":pin_title,"description":description,"link":link,
                "media_source":{"source_type":"image_base64","content_type":"image/jpeg","data":b64}}
        raw = json.dumps(body).encode()
        req = _ur.Request(f"{base}/pins", data=raw,
                          headers={"Content-Type":"application/json","Authorization":f"Bearer {token}","User-Agent":"Mozilla/5.0"}, method="POST")
        try:
            with _ur.urlopen(req, timeout=30, context=ctx) as r:
                return json.loads(r.read())
        except _ue.HTTPError as e:
            body_err = e.read().decode("utf-8","replace")
            raise ValueError(f"Pinterest API {e.code}: {body_err}")
    if len(image_bytes_list) == 1:
        return _pin(image_bytes_list[0], title)
    results = []
    for i, b in enumerate(image_bytes_list[:4]):
        t = f"{title} ({i+1}/{len(image_bytes_list)})" if len(image_bytes_list) > 1 else title
        results.append(_pin(b, t))
    return {"pins": results}

def _send_campaign_discord(cfg: dict, platform: str, status: str, details: dict):
    import urllib.request as _ur, ssl as _ssl
    webhook = cfg.get("social_posting",{}).get("discord_webhook_campaigns","").strip()
    if not webhook:
        return
    colors  = {"sent":3066993,"scheduled":16776960,"failed":15158332}
    icons   = {"instagram":"📸 Instagram","pinterest":"📌 Pinterest"}
    labels  = {"sent":"✅ Erfolgreich gepostet","scheduled":"⏰ Kampagne geplant","failed":"❌ Fehler"}
    fields  = []
    if details.get("product_id"):
        fields.append({"name":"🆔 Produkt","value":str(details["product_id"]),"inline":True})
    if details.get("scheduled_at"):
        try:
            d = _dt.datetime.fromisoformat(details["scheduled_at"]).strftime("%d.%m.%Y %H:%M")
        except Exception:
            d = details["scheduled_at"]
        fields.append({"name":"📅 Geplant","value":d,"inline":True})
    if details.get("caption"):
        fields.append({"name":"📝 Caption","value":str(details["caption"])[:200],"inline":False})
    if details.get("image_count"):
        fields.append({"name":"🖼️ Bilder","value":str(details["image_count"]),"inline":True})
    if details.get("error"):
        fields.append({"name":"⚠️ Fehler","value":str(details["error"])[:200],"inline":False})
    embed = {"title":f"{icons.get(platform,platform)} – {labels.get(status,status)}","color":colors.get(status,8421504),"fields":fields,"footer":{"text":"dekoire.com · Social Posting"}}
    ctx = _ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=_ssl.CERT_NONE
    try:
        raw = json.dumps({"embeds":[embed]}).encode()
        req = _ur.Request(webhook, data=raw, headers={"Content-Type":"application/json","User-Agent":"DiscordBot (https://github.com, 1.0)"}, method="POST")
        _ur.urlopen(req, timeout=10, context=ctx)
    except Exception as e:
        print(f"[CampaignDiscord] {e}")

def _execute_social_post(post: dict, cfg: dict):
    post_id    = post.get("id","")
    product_id = post.get("product_id","")
    platform   = post.get("platform","")
    status     = "sent"; response={}; error_msg=""
    try:
        raw_data = post.get("image_data",[])
        image_bytes_list = [base64.standard_b64decode(d) for d in raw_data]
        if platform == "instagram":
            public_urls = post.get("image_public_urls",[])
            if not public_urls:
                raise ValueError("Keine öffentlichen Bild-URLs für Instagram verfügbar")
            response = _post_to_instagram(cfg, public_urls, post.get("caption",""), post.get("hashtags",""))
        elif platform == "pinterest":
            response = _post_to_pinterest(cfg, image_bytes_list, post.get("pin_title",""), post.get("pin_description",""), post.get("board_id",""), post.get("pin_link",""))
        else:
            raise ValueError(f"Unbekannte Plattform: {platform}")
    except Exception as e:
        status = "failed"; error_msg = str(e)
        print(f"[SocialPost exec] {e}")
    updates = {"status":status,"response_data":response,"error_message":error_msg,"updated_at":_dt.datetime.utcnow().isoformat()}
    _update_social_post_status(post_id, product_id, updates)
    _send_campaign_discord(cfg, platform, status, {"product_id":product_id,"caption":post.get("caption",""),"image_count":len(post.get("image_data",[])),"error":error_msg})
    return status, error_msg

_post_scheduler_running = False
def _start_post_scheduler():
    global _post_scheduler_running
    if _post_scheduler_running:
        return
    _post_scheduler_running = True
    def _run():
        while True:
            _time.sleep(60)
            try:
                cfg     = load_config()
                sb_cfg  = cfg.get("supabase",{})
                svc_key = sb_cfg.get("service_role_key","") or cfg.get("service_role_key","")
                if not _SUPABASE_OK or not sb_cfg.get("url") or not svc_key:
                    continue
                sb  = _sb_create(sb_cfg["url"], svc_key)
                now = _dt.datetime.utcnow().isoformat()
                res = sb.table("social_posts").select("*").eq("status","scheduled").lte("scheduled_at",now).execute()
                for post in (res.data or []):
                    _execute_social_post(post, cfg)
            except Exception as e:
                print(f"[PostScheduler] {e}")
    _threading.Thread(target=_run, daemon=True, name="post-scheduler").start()
    print("[PostScheduler] started")

_start_post_scheduler()

@app.route("/api/product/<product_id>/social/posts", methods=["GET"])
@require_auth
def social_posts_list(product_id):
    return jsonify(_load_social_posts(product_id))

@app.route("/api/product/<product_id>/social/post", methods=["POST"])
@require_auth
def social_post_create(product_id):
    platform        = request.form.get("platform","")
    caption         = request.form.get("caption","")
    hashtags        = request.form.get("hashtags","")
    scheduled_at    = request.form.get("scheduled_at","").strip()
    board_id        = request.form.get("board_id","")
    pin_title       = request.form.get("pin_title","")
    pin_description = request.form.get("pin_description","")
    pin_link        = request.form.get("pin_link","")
    files           = request.files.getlist("images")
    if not files or not platform:
        return jsonify({"error":"Bitte Bilder und Plattform angeben"}), 400
    if len(files) > 4:
        return jsonify({"error":"Maximal 4 Bilder erlaubt"}), 400
    cfg = load_config()
    image_bytes_list = [f.read() for f in files]
    image_data       = [base64.standard_b64encode(b).decode() for b in image_bytes_list]
    public_urls = []
    if platform == "instagram":
        try:
            for i, b in enumerate(image_bytes_list):
                public_urls.append(_upload_to_storage(cfg, b, f"{uuid.uuid4().hex[:10]}.jpg"))
        except Exception as e:
            return jsonify({"error":f"Bild-Upload fehlgeschlagen: {e}"}), 400
    is_immediate = not scheduled_at
    post = {
        "id":               uuid.uuid4().hex,
        "product_id":       product_id,
        "platform":         platform,
        "status":           "pending" if is_immediate else "scheduled",
        "scheduled_at":     scheduled_at or None,
        "caption":          caption,
        "hashtags":         hashtags,
        "board_id":         board_id,
        "pin_title":        pin_title,
        "pin_description":  pin_description,
        "pin_link":         pin_link,
        "image_data":       image_data,
        "image_public_urls":public_urls,
        "image_count":      len(image_data),
        "response_data":    {},
        "error_message":    "",
        "updated_at":       _dt.datetime.utcnow().isoformat(),
        "created_at":       _dt.datetime.utcnow().isoformat(),
    }
    _save_social_post_record(post)
    if is_immediate:
        status, err = _execute_social_post(post, cfg)
        post["status"] = status
        post["error_message"] = err
    else:
        _send_campaign_discord(cfg, platform, "scheduled", {"product_id":product_id,"caption":caption,"scheduled_at":scheduled_at,"image_count":len(image_data)})
    post_url = ""
    if post.get("status") == "sent":
        resp = post.get("response_data", {})
        pin_id = resp.get("id") or (resp.get("pins",[{}])[0].get("id","") if resp.get("pins") else "")
        if pin_id and platform == "pinterest":
            post_url = f"https://www.pinterest.com/pin/{pin_id}/"
        elif platform == "instagram":
            post_url = resp.get("permalink", "")
    return jsonify({"success":True,"status":post["status"],"error":post.get("error_message",""),"post_url":post_url})

@app.route("/api/social/standalone/post", methods=["POST"])
@require_auth
def social_standalone_post():
    return social_post_create("standalone")

@app.route("/api/social/generate-field", methods=["POST"])
@require_auth
def social_generate_field():
    """Generate a single social-media field with AI (no product context required)."""
    data      = request.json or {}
    field     = data.get("field", "")
    title_ctx = data.get("title", "").strip()
    cfg       = load_config()
    client, _ = get_client()
    model     = cfg.get("model", "claude-opus-4-5")
    if not client:
        return jsonify({"error": "Kein KI-Client konfiguriert"}), 400

    prompts = {
        "pin_titel":        f"Write a short, catchy Pinterest pin title (max 100 chars). Context: {title_ctx or 'art print'}. Return only the title.",
        "pin_beschreibung": f"Write an engaging Pinterest pin description (2–3 sentences, max 500 chars). Context: {title_ctx or 'art print'}. Return only the description.",
        "pin_alt_text":     f"Write a concise alt text for an image (max 500 chars). Context: {title_ctx or 'art print'}. Return only the alt text.",
        "ig_description":   f"Write an Instagram caption (1–3 sentences, engaging, no hashtags). Context: {title_ctx or 'art print'}. Return only the caption.",
        "ig_tags":          f"Generate 10–15 relevant Instagram hashtags for: {title_ctx or 'art print'}. Format: #tag1 #tag2 … Return only hashtags.",
    }
    # Handle _all variants for bulk generation
    if field in ("pin_all", "ig_all"):
        keys = ["pin_titel","pin_beschreibung","pin_alt_text"] if field == "pin_all" else ["ig_description","ig_tags"]
        result = {}
        map_key = {"pin_titel":"pin_titel","pin_beschreibung":"pin_beschreibung","pin_alt_text":"pin_alt_text",
                   "ig_description":"ig_description","ig_tags":"ig_tags"}
        out_key = {"pin_titel":"pin_titel","pin_beschreibung":"pin_beschreibung","pin_alt_text":"pin_alt_text",
                   "ig_description":"ig_description","ig_tags":"ig_tags"}
        for k in keys:
            try:
                resp = client.messages.create(model=model, max_tokens=300,
                    messages=[{"role":"user","content": prompts[k]}])
                result[k] = resp.content[0].text.strip()
            except Exception:
                pass
        return jsonify(result)

    if field not in prompts:
        return jsonify({"error": "Unbekanntes Feld"}), 400
    try:
        resp = client.messages.create(model=model, max_tokens=300,
            messages=[{"role":"user","content": prompts[field]}])
        return jsonify({"value": resp.content[0].text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/product/<product_id>/regen", methods=["POST"])
@require_auth
def product_regen(product_id):
    data      = request.json or {}
    field     = data.get("field", "")
    image_url = data.get("image_url", "")
    context   = data.get("context", {})
    cfg       = load_config()
    client, _ = get_client()
    model     = cfg.get("model", "claude-opus-4-5")

    # Social media regen (text-only)
    if field == "social":
        pin_cfg = cfg.get("pinterest", {})
        ig_cfg  = cfg.get("instagram",  {})
        try:
            prompt = social_media_prompt(context,
                boards     = pin_cfg.get("boards",   []),
                locations  = ig_cfg.get("locations", []),
                target_url = pin_cfg.get("target_url","https://dekoire.com"))
            raw    = call_text(client, model, prompt, max_tokens=2000)
            return jsonify(parse_json(raw))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Image-based field regen
    if field not in REGEN_PROMPTS:
        return jsonify({"error": f"Unknown field: {field}"}), 400
    if not image_url:
        return jsonify({"error": "No image_url provided."}), 400
    try:
        # Local path (e.g. /static/thumbnails/xxx.jpg) → read from disk
        if image_url.startswith("/static/"):
            local_path = SCRIPT_DIR / image_url.lstrip("/")
            img_bytes  = local_path.read_bytes()
        else:
            import urllib.request as _ur
            with _ur.urlopen(image_url, timeout=15) as resp:
                img_bytes = resp.read()
        mime     = _detect_mime(img_bytes)
        img_data = base64.standard_b64encode(img_bytes).decode()
        raw  = call_with_image(client, model, img_data, mime,
                               f"{PRIVACY}\n\n{REGEN_PROMPTS[field]}", max_tokens=256)
        if field in ("dominante_farben", "tags"):
            value = [v.strip() for v in raw.split(",") if v.strip()]
        elif field == "ist_fotografie":
            value = raw.lower() == "true"
        else:
            value = raw.strip()
        return jsonify({"field": field, "value": value})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Legal Risk Check ──────────────────────────────────────────────────────────

_LEGAL_CHECK_TEXT_PROMPT = """You are an expert legal risk analyst for art and poster products sold on Etsy and e-commerce platforms in the EU and USA. Your job is to catch potential trademark, copyright, and IP violations BEFORE products get listed. Missing a real risk is worse than flagging a potential one — be thorough and strict.

════════════════════════════════════════
PRODUCT TO ANALYZE
════════════════════════════════════════
Title:       {title}
Description: {description}
Tags:        {tags}

════════════════════════════════════════
STEP 1 — TEXT ANALYSIS (title, description, tags)
════════════════════════════════════════
Scan all text fields for:
• Trademark/brand names — company names, product lines, car brands, fashion brands, tech brands, sports teams (e.g. Porsche, Nike, Supreme, Ferrari, Apple, Adidas, Disney, Coca-Cola, Louis Vuitton)
• Artist or creator names — any recognizable artist living or deceased (e.g. Basquiat, Banksy, Warhol, Monet, Van Gogh, Dalí, Klimt, Haring, Kusama, Kaws, Lichtenstein)
• Celebrity / person names — athletes, musicians, actors, public figures
• Franchise / character names — fictional characters, universes, IP (e.g. Batman, Mickey Mouse, Star Wars, Harry Potter, Pokémon)
• Risky claim phrases: "official", "licensed", "authorized", "original", "authentic", "inspired by [name]", "im Stil von [name]", "replica", "reproduction"
Each finding → textFindings entry. If an artist name appears → also add to artistFindings.

════════════════════════════════════════
STEP 2 — IMAGE ANALYSIS
════════════════════════════════════════
{image_instruction}

────────────────────────────────────────
2a) READ ALL TEXT IN THE IMAGE
────────────────────────────────────────
Read every piece of text visible in the image: signatures, inscriptions, logos, watermarks, labels, graffiti text, number plates, product labels, clothing text — everything.
For each piece of text found:
• If it contains a brand name, person name, artist signature, or trademark → add to imageFindings with type "text_in_image" AND severity "high" if it is a clear brand/person name.
• If it contains an artist's signature → also add to artistFindings.

────────────────────────────────────────
2b) IDENTIFY BRANDS, PRODUCTS & LOGOS IN THE IMAGE
────────────────────────────────────────
Look for any visually identifiable branded items regardless of visible text:
• VEHICLES: Even without readable text, the shape/design of branded vehicles is often trademarked. Porsche (distinctive 911/Cayenne silhouette), Ferrari (prancing horse body shape), Lamborghini, BMW, Mercedes, etc. → flag as RED if clearly identifiable.
• LOGOS: Brand logos, even partial, distorted or stylized versions.
• CONSUMER PRODUCTS: Sneaker designs (Air Jordan sole shape, Adidas triple stripes), tech products (iPhone form factor, Apple logo), luxury goods.
• CHARACTERS & FIGURES: Cartoon characters, mascots, superhero costumes, even if drawn in an artistic style.
• SPORTS: Team uniforms, club crests, team colors + number combinations.
Flag with high confidence if the brand/product is unmistakable. Do NOT give the benefit of the doubt for clearly identifiable branded items.

────────────────────────────────────────
2c) ARTISTIC STYLE ANALYSIS
────────────────────────────────────────
Carefully evaluate the visual style, technique, brushwork, color palette, motifs, and composition. Ask: does this image strongly evoke a specific known artist?

Check specifically for (this list is not exhaustive — use your full knowledge):
• Jean-Michel Basquiat — crude neo-expressionist figures, crown motifs, skull imagery, text and words integrated into the painting, childlike raw lines, urban/street energy, dark outlines, multi-layered backgrounds with scrawled text or crossed-out words
• Andy Warhol — repeated silkscreen-style celebrity portraits, bold flat colors, pop art aesthetic, high contrast
• Banksy — stencil graffiti, political/satirical subjects, street art aesthetic, monochrome with color accents
• Keith Haring — bold outlined cartoon-like figures, radiant baby, patterns of repeating shapes, thick black outlines
• Salvador Dalí — melting objects, surrealist dreamscapes, hyper-realistic rendering of impossible scenes
• Frida Kahlo — self-portrait style, Mexican folk art elements, floral headpieces, symbolic objects
• Yayoi Kusama — polka dot obsession, infinity net patterns, pumpkins
• KAWS — modified cartoon characters with X-shaped eyes and skull motifs, "Companion" figure
• Roy Lichtenstein — halftone dot patterns, comic book panels, bold outlines, speech bubbles
• Gustav Klimt — gold leaf ornamentation, decorative flat patterns, erotic symbolism
• Egon Schiele — angular expressionist figures, visible contour lines, raw emotional portraits
• Hokusai / Japanese woodblock — ukiyo-e style, The Great Wave aesthetic, flat color areas, bold outlines
• Alphonse Mucha — Art Nouveau decorative borders, flowing female figures, floral frames
• Mark Rothko — large color field rectangles, soft blurred edges, meditative mood
• Jackson Pollock — drip painting, chaotic layered paint splatters
• Cindy Sherman / street photography — photographic style works
If there is a clear stylistic match, add to imageFindings (type "artist_style") AND add the artist to artistFindings. A strong match with a living artist or one who died within the last 70 years → HIGH severity.

────────────────────────────────────────
2d) SPECIFIC ARTWORK SIMILARITY
────────────────────────────────────────
Does the image closely resemble a specific iconic artwork or photograph?
Examples: Mona Lisa, Starry Night, The Scream, Girl with a Pearl Earring, American Gothic, Nighthawks, The Birth of Venus, Vermeer works, specific Warhol prints, Banksy stencils.
Flag as "specific_artwork" type with the artwork name and artist.

════════════════════════════════════════
STEP 3 — SCORING RULES (apply strictly)
════════════════════════════════════════
Start at 0. Add points for each finding:
• Clearly identifiable branded vehicle (e.g. Porsche) in image: +50–65
• Brand logo clearly visible in image: +45–60
• Brand name in title or tags: +35–50
• Artist name in title or tags: +25–40
• Artist name or signature readable in the image: +40–55
• Strong style match to living artist or artist dead <70 years: +30–45
• Strong style match to artist dead >70 years but very close to specific work: +15–25
• "Official"/"licensed"/"authorized" claim in text: +40–55
• Celebrity/person name in title: +30–45
• Franchise character clearly depicted: +45–60
• Multiple medium-risk findings together compound → push toward RED
• Score 0–34 = green, 35–69 = yellow, 70–100 = red

════════════════════════════════════════
OUTPUT — Return ONLY valid JSON, no markdown fences, no extra text:
════════════════════════════════════════
{{
  "status": "green",
  "score": 12,
  "summary": "2–4 sentences. Use cautious language: potentially, may, appears to, resembles, could be flagged. NEVER say legally safe or legally permitted.",
  "textFindings": [
    {{
      "term": "exact term or phrase from the product text",
      "type": "brand",
      "reason": "Why this is a potential legal risk",
      "severity": "high"
    }}
  ],
  "artistFindings": [
    {{
      "name": "Artist full name",
      "deathYear": 1988,
      "copyrightStatus": "protected",
      "assessment": "Risk assessment for this artist reference (style match, name in text, or signature in image)"
    }}
  ],
  "imageFindings": [
    {{
      "reference": "What was identified (brand name, artwork name, style description, readable text)",
      "type": "brand_visual",
      "confidence": "high",
      "assessment": "What was found and why it is a risk"
    }}
  ],
  "recommendations": [
    "One concrete actionable recommendation per item"
  ]
}}

Allowed enum values:
• status: "green" | "yellow" | "red"
• score: integer 0–100
• textFindings[].type: "brand" | "artist" | "claim" | "phrase" | "celebrity"
• textFindings[].severity: "low" | "medium" | "high"
• artistFindings[].copyrightStatus: "protected" | "likely_protected" | "public_domain" | "unclear"
• imageFindings[].type: "artist_style" | "specific_artwork" | "product_similarity" | "brand_visual" | "text_in_image"
• imageFindings[].confidence: "low" | "medium" | "high"

Final rules:
• Empty arrays [] are fine when nothing is found
• ALWAYS write ALL output text in GERMAN — summary, reason, assessment, recommendations — everything in German regardless of the product language
• NEVER use: "rechtlich sicher", "rechtlich erlaubt", "garantiert unproblematisch", "kein Risiko", "sicher zu verkaufen"
• DO use: "potenziell problematisch", "erhöhtes Risiko", "sollte manuell geprüft werden", "könnte als Verletzung gewertet werden", "ähnelt stark dem Stil von", "starke visuelle Ähnlichkeit", "mögliche Urheberrechtsverletzung"
"""

@app.route("/api/product/<product_id>/legal-check", methods=["POST"])
@require_auth
def product_legal_check(product_id):
    """Run a legal risk check on a product using Claude AI.

    Accepts either:
    - multipart/form-data  → 'data' field (JSON string) + optional 'image' file
    - application/json     → JSON body with text fields + optional image_url
    """
    cfg = load_config()

    # ── Parse request (multipart or JSON) ────────────────────────────────────
    inline_img_bytes = None
    inline_img_mime  = None
    if request.content_type and "multipart" in request.content_type:
        import json as _json
        data = _json.loads(request.form.get("data", "{}"))
        if "image" in request.files:
            f = request.files["image"]
            raw_bytes = f.read()
            inline_img_bytes = raw_bytes
            inline_img_mime  = _detect_mime(raw_bytes)
    else:
        data = request.json or {}

    # ── Load product data from DB (for saved products) ────────────────────────
    row = {}
    if product_id != "new":
        sb_cfg = cfg.get("supabase", {})
        if _SUPABASE_OK and sb_cfg.get("url") and sb_cfg.get("anon_key"):
            try:
                sb  = _sb_create(sb_cfg["url"], sb_cfg["anon_key"])
                res = sb.table(sb_cfg.get("table_name","image_analyses")) \
                        .select("*").eq("id", product_id).single().execute()
                row = res.data or {}
            except Exception:
                pass

    # ── Merge client-supplied text fields ─────────────────────────────────────
    # Only overwrite image_url if client sends a non-empty value so we don't
    # clobber a valid DB image_url with an empty JS string.
    for k in ("titel","beschreibung","tags","etsy_tags","etsy_title","etsy_description"):
        if data.get(k) is not None:
            row[k] = data[k]
    if data.get("image_url"):
        row["image_url"] = data["image_url"]

    # ── Assemble text inputs ─────────────────────────────────────────────────
    title = (row.get("titel") or row.get("etsy_title") or "").strip()
    desc  = (row.get("beschreibung") or row.get("etsy_description") or "").strip()
    raw_tags = row.get("tags") or row.get("etsy_tags") or ""
    if isinstance(raw_tags, list):
        tags = ", ".join(str(t) for t in raw_tags if t)
    else:
        tags = str(raw_tags).strip()
    image_url = (row.get("image_url") or "").strip()

    if not any([title, desc, tags, image_url]):
        return jsonify({"error": "Keine Produktdaten verfügbar für die Prüfung."}), 400

    # ── Build prompt ─────────────────────────────────────────────────────────
    has_image = bool(image_url)
    if has_image:
        image_instruction = (
            "An image of the product is attached. You MUST perform ALL four sub-steps "
            "(2a through 2d) described below. Do not skip any step.\n\n"
            "IMPORTANT: Be aggressive. If you can clearly identify a branded vehicle "
            "(e.g. a Porsche), a logo, an artist's signature, or a distinctive artistic "
            "style matching a known artist — flag it. Do not give the benefit of the doubt "
            "for clearly identifiable IP. A Porsche silhouette alone, even without any text, "
            "is a trademark risk. Basquiat-style painting elements are a copyright risk."
        )
    else:
        image_instruction = (
            "No product image was provided. Skip steps 2a–2d entirely. "
            "Set imageFindings to [] and note the absence of image analysis in your summary."
        )

    # Escape any literal { } in user-supplied text so str.format() doesn't
    # interpret them as placeholders and raise a KeyError.
    def _esc(s: str) -> str:
        return s.replace("{", "{{").replace("}", "}}")

    prompt = _LEGAL_CHECK_TEXT_PROMPT.format(
        title             = _esc(title or "(not provided)"),
        description       = _esc(desc  or "(not provided)"),
        tags              = _esc(tags  or "(not provided)"),
        image_instruction = image_instruction,
    )

    # ── Call Claude ──────────────────────────────────────────────────────────
    try:
        client, _ = get_client()
    except ValueError as e:
        return jsonify({"error": str(e)}), 500

    model = cfg.get("model", "claude-opus-4-5")

    try:
        img_data = img_mime = None

        if inline_img_bytes:
            # ── Inline upload via multipart (no URL needed) ──────────────────
            # Always normalise through save_and_compress (same pipeline as
            # /api/analyze): converts to JPEG, compresses if too large.
            # Then re-detect MIME from the resulting bytes so it always matches.
            try:
                tmp_path = save_and_compress(inline_img_bytes, "legal_check_tmp.jpg")
                inline_img_bytes = tmp_path.read_bytes()
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass  # use raw bytes if compression fails
            img_data = base64.standard_b64encode(inline_img_bytes).decode()
            img_mime = _detect_mime(inline_img_bytes)  # detect AFTER normalisation

        elif has_image:
            # ── Fetch from stored URL / local path ───────────────────────────
            try:
                if image_url.startswith("/static/"):
                    local_path = SCRIPT_DIR / image_url.lstrip("/")
                    img_bytes  = local_path.read_bytes()
                else:
                    import urllib.request as _ur
                    with _ur.urlopen(image_url, timeout=20) as _resp:
                        img_bytes = _resp.read()
                img_mime = _detect_mime(img_bytes)
                img_data = base64.standard_b64encode(img_bytes).decode()
            except Exception as img_err:
                # Image could not be loaded — switch prompt to text-only mode
                no_img_note = (
                    f"(Hinweis: Bild konnte nicht geladen werden: {img_err}. "
                    "Bitte nur Textfelder prüfen.)"
                )
                fallback_instruction = (
                    "No product image was provided or image could not be loaded. "
                    "Skip steps 2a–2d. Set imageFindings to []."
                )
                prompt = _LEGAL_CHECK_TEXT_PROMPT.format(
                    title             = _esc(title or "(not provided)") + " " + no_img_note,
                    description       = _esc(desc  or "(not provided)"),
                    tags              = _esc(tags  or "(not provided)"),
                    image_instruction = fallback_instruction,
                )

        if img_data and img_mime:
            raw = call_with_image(client, model, img_data, img_mime, prompt, max_tokens=3000)
        else:
            raw = call_text(client, model, prompt, max_tokens=3000)

        result = parse_json(raw)

        # Ensure required keys exist with safe defaults
        result.setdefault("status", "yellow")
        result.setdefault("score",  50)
        result.setdefault("summary", "")
        result.setdefault("textFindings",   [])
        result.setdefault("artistFindings", [])
        result.setdefault("imageFindings",  [])
        result.setdefault("recommendations", [])

        # Tag whether image was actually analysed
        result["_imageAnalysed"] = bool(img_data)

        # Persist for later retrieval (skip for unsaved "new" products)
        if product_id != "new":
            _save_legal_check(product_id, result)

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/product/<product_id>/legal-check", methods=["GET"])
@require_auth
def product_legal_check_get(product_id):
    """Return the most recently saved legal-check result for a product."""
    result = _load_legal_check(product_id)
    if not result:
        return jsonify({}), 204   # No content — not yet run
    return jsonify(result)


@app.route("/api/product/<product_id>/legal-check/store", methods=["POST"])
@require_auth
def product_legal_check_store(product_id):
    """Store a pre-computed legal-check result (e.g. after product creation)."""
    if product_id == "new":
        return jsonify({"error": "Produkt-ID erforderlich"}), 400
    data = request.json or {}
    if data:
        _save_legal_check(product_id, data)
    return jsonify({"ok": True})


@app.route("/api/vision-check", methods=["POST"])
@require_auth
def vision_check():
    """Google Cloud Vision Web Detection — finds similar/matching images on the web."""
    cfg     = load_config()
    api_key = cfg.get("google_cloud_vision_key", "").strip()
    if not api_key:
        return jsonify({"error": "not_configured"}), 400

    f = request.files.get("image")
    if not f:
        # Check for JSON body with image_url (product_edit mode)
        body = request.json or {}
        image_url = body.get("image_url", "").strip()
        if image_url:
            import urllib.request as _ur2
            import ssl as _ssl2
            try:
                ctx2 = _ssl2.create_default_context()
                with _ur2.urlopen(image_url, timeout=15, context=ctx2) as resp2:
                    raw = resp2.read()
            except Exception as e:
                return jsonify({"error": f"Bild-URL konnte nicht geladen werden: {e}"}), 400
        else:
            return jsonify({"error": "Kein Bild übermittelt"}), 400
    else:
        raw = f.read()

    try:
        tmp = save_and_compress(raw, "vision_tmp.jpg")
        raw = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
    except Exception:
        pass

    b64 = base64.standard_b64encode(raw).decode()
    payload = json.dumps({
        "requests": [{
            "image": {"content": b64},
            "features": [{"type": "WEB_DETECTION", "maxResults": 20}]
        }]
    }).encode()

    import urllib.request as _ur
    req = _ur.Request(
        f"https://vision.googleapis.com/v1/images:annotate?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        with _ur.urlopen(req, timeout=30, context=ctx) as resp:
            result = json.loads(resp.read())
        wd = result.get("responses", [{}])[0].get("webDetection", {})
        return jsonify({
            "entities":     [{"description": e.get("description",""), "score": round(e.get("score",0),2)}
                             for e in wd.get("webEntities", []) if e.get("description")],
            "similarImages": [i.get("url","") for i in wd.get("visuallySimilarImages", []) if i.get("url")][:12],
            "fullMatches":   [i.get("url","") for i in wd.get("fullMatchingImages", []) if i.get("url")][:6],
            "pages":         [{"url": p.get("url",""), "title": p.get("pageTitle","")}
                             for p in wd.get("pagesWithMatchingImages", []) if p.get("url")][:8],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/settings")
@require_auth
def settings_page():
    cfg = load_config()
    pt  = cfg.get("page_texts", {})
    shops_cfg   = cfg.get("shops", {})
    shopify_cfg = shops_cfg.get("shopify", {})
    etsy_cfg    = shops_cfg.get("etsy", {})
    amazon_cfg  = shops_cfg.get("amazon", {})
    return render_template(
        "config_page.html",
        cfg                    = cfg,
        current_user_email     = session.get("user_email", ""),
        header_logo            = cfg.get("header_logo",  "logo-white.png"),
        header_title           = cfg.get("header_title", "Image Analyzer"),
        icon_presets           = list(ICON_PRESETS.keys()),
        icon_svgs              = ICON_PRESETS,
        external_apps          = cfg.get("external_apps", []),
        page_texts             = pt,
        shops_cfg              = shops_cfg,
        shopify_cfg            = shopify_cfg,
        etsy_cfg               = etsy_cfg,
        amazon_cfg             = amazon_cfg,
        shopify_collections    = shopify_cfg.get("synced_collections", []),
        shopify_product_types  = shopify_cfg.get("synced_product_types", []),
        etsy_shipping_profiles = etsy_cfg.get("synced_shipping_profiles", []),
        amazon_categories      = amazon_cfg.get("synced_categories", []),
    )

@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(SCRIPT_DIR / "assets", filename)

# ── Config API ────────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
@require_auth
def api_get_config():
    cfg = load_config()
    safe = dict(cfg)
    if safe.get("anthropic_api_key"):
        k = safe["anthropic_api_key"]
        safe["anthropic_api_key"] = k[:12] + "…" + k[-4:] if len(k) > 16 else "***"
    if safe.get("admin_password"):
        safe["admin_password"] = "***"
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
@require_auth
def api_save_config():
    updates = request.json or {}
    try:
        cfg = load_config()
        # Merge top-level keys; handle nested dicts
        for k, v in updates.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif v != "***" and not (k == "anthropic_api_key" and "…" in str(v)):
                cfg[k] = v
        save_config(cfg)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/config/db-status", methods=["GET"])
@require_auth
def config_db_status():
    """Check if app_config table exists and has data."""
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    url    = sb_cfg.get("url", "")
    key    = sb_cfg.get("service_role_key", "") or cfg.get("service_role_key", "")
    if not _SUPABASE_OK or not url or not key:
        return jsonify({"available": False, "reason": "Supabase service_role_key nicht konfiguriert"})
    try:
        sb  = _sb_create(url, key)
        res = sb.table("app_config").select("id,updated_at").eq("id", 1).execute()
        if res.data:
            return jsonify({"available": True, "updated_at": res.data[0].get("updated_at", "")})
        return jsonify({"available": False, "reason": "Tabelle existiert, aber noch kein Eintrag"})
    except Exception as e:
        return jsonify({"available": False, "reason": str(e)})

@app.route("/api/config/sync-to-db", methods=["POST"])
@require_auth
def config_sync_to_db():
    """Push current YAML config to Supabase app_config table."""
    cfg    = load_config()
    sb_cfg = cfg.get("supabase", {})
    url    = sb_cfg.get("url", "")
    key    = sb_cfg.get("service_role_key", "") or cfg.get("service_role_key", "")
    if not _SUPABASE_OK or not url or not key:
        return jsonify({"error": "Supabase service_role_key nicht konfiguriert"}), 400
    try:
        sb = _sb_create(url, key)
        sb.table("app_config").upsert({"id": 1, "config": cfg}).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Analysis API ──────────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
@require_auth
def analyze():
    if "image" not in request.files:
        return jsonify({"error": "No image submitted."}), 400
    f          = request.files["image"]
    file_bytes = f.read()
    cache_path = None
    try:
        client, cfg = get_client()
        meta        = image_meta(file_bytes)
        cache_path  = save_and_compress(file_bytes, f.filename)
        img_data, mime = encode_from_path(cache_path)
        raw      = call_with_image(client, cfg.get("model","claude-opus-4-5"), img_data, mime,
                                   full_prompt(cfg.get("output_language","English"),
                                               int(cfg.get("max_colors", 3))))
        analysis = parse_json(raw)
        return jsonify({**meta, **analysis, "dateiname": f.filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cache_path and cache_path.exists():
            cache_path.unlink()

@app.route("/api/regenerate", methods=["POST"])
@require_auth
def regenerate():
    if "image" not in request.files:
        return jsonify({"error": "No image submitted."}), 400
    field = request.form.get("field", "")
    if field not in REGEN_PROMPTS:
        return jsonify({"error": f"Unknown field: {field}"}), 400
    f          = request.files["image"]
    file_bytes = f.read()
    cache_path = None
    try:
        client, cfg = get_client()
        cache_path  = save_and_compress(file_bytes, f.filename)
        img_data, mime = encode_from_path(cache_path)
        raw  = call_with_image(client, cfg.get("model","claude-opus-4-5"), img_data, mime,
                               f"{PRIVACY}\n\n{REGEN_PROMPTS[field]}", max_tokens=256)
        if field in ("dominante_farben", "tags"):
            value = [v.strip() for v in raw.split(",") if v.strip()]
        elif field == "ist_fotografie":
            value = raw.lower() == "true"
        else:
            value = raw
        return jsonify({"field": field, "value": value})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cache_path and cache_path.exists():
            cache_path.unlink()

@app.route("/api/shop-copy", methods=["POST"])
@require_auth
def shop_copy():
    data  = request.json or {}
    field = data.get("field", "")
    value = str(data.get("value", "")).strip()
    if field not in SHOP_PROMPTS or not value:
        return jsonify({"error": "Invalid field or empty value."}), 400
    try:
        client, cfg = get_client()
        text = call_text(client, cfg.get("model","claude-opus-4-5"),
                         SHOP_PROMPTS[field].format(value=value), max_tokens=300)
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/translate", methods=["POST"])
@require_auth
def translate():
    data     = request.json or {}
    language = data.get("language", "English")
    fields   = {k: data[k] for k in TRANSLATABLE if k in data and data[k]}
    if not fields:
        return jsonify({"error": "No translatable fields."}), 400
    try:
        client, cfg = get_client()
        prompt = (
            f"Translate all values in this JSON to {language}. "
            "Keep the exact same structure and keys. "
            "Reply ONLY with the JSON, no markdown blocks. "
            "Lists stay as lists.\n\n"
            + json.dumps(fields, ensure_ascii=False)
        )
        raw        = call_text(client, cfg.get("model","claude-opus-4-5"), prompt)
        translated = parse_json(raw)
        return jsonify(translated)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/generate-social", methods=["POST"])
@require_auth
def generate_social():
    context = request.json or {}
    try:
        client, cfg = get_client()
        pin_cfg  = cfg.get("pinterest", {})
        ig_cfg   = cfg.get("instagram",  {})
        prompt   = social_media_prompt(
            context,
            boards     = pin_cfg.get("boards",   []),
            locations  = ig_cfg.get("locations", []),
            target_url = pin_cfg.get("target_url", "https://dekoire.com"),
        )
        raw    = call_text(client, cfg.get("model","claude-opus-4-5"), prompt, max_tokens=2000)
        result = parse_json(raw)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/generate-shops", methods=["POST"])
@require_auth
def generate_shops():
    context   = request.json or {}
    cfg       = load_config()
    client, _ = get_client()
    model     = cfg.get("model", "claude-opus-4-5")
    shop_cfg  = cfg.get("shops", {})
    try:
        raw  = call_text(client, model, shops_prompt(context, shop_cfg), max_tokens=2000)
        data = parse_json(raw)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sync/shopify", methods=["POST"])
@require_auth
def sync_shopify():
    cfg       = load_config()
    shop_cfg  = cfg.get("shops", {}).get("shopify", {})
    store_url = shop_cfg.get("store_url", "")
    api_key   = shop_cfg.get("api_key", "")
    api_pass  = shop_cfg.get("api_password", "")
    if not store_url or not api_key:
        return jsonify({"error": "Shopify credentials not configured"}), 400
    import urllib.request as _ur, ssl as _ssl, base64 as _b64
    try:
        ctx = _ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=_ssl.CERT_NONE
        domain = store_url.replace("https://","").replace("http://","").rstrip("/")
        token  = _b64.b64encode(f"{api_key}:{api_pass}".encode()).decode()
        headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        # Fetch collections
        req = _ur.Request(f"https://{domain}/admin/api/2024-01/custom_collections.json?limit=250", headers=headers)
        with _ur.urlopen(req, context=ctx) as r:
            colls_data = json.loads(r.read())
        collections = [c["title"] for c in colls_data.get("custom_collections", [])]
        # Fetch products for product_types
        req2 = _ur.Request(f"https://{domain}/admin/api/2024-01/products.json?limit=250&fields=product_type", headers=headers)
        with _ur.urlopen(req2, context=ctx) as r:
            prods_data = json.loads(r.read())
        types = list({p["product_type"] for p in prods_data.get("products", []) if p.get("product_type")})
        # Save to config
        if "shops" not in cfg: cfg["shops"] = {}
        if "shopify" not in cfg["shops"]: cfg["shops"]["shopify"] = {}
        cfg["shops"]["shopify"]["synced_collections"]   = collections
        cfg["shops"]["shopify"]["synced_product_types"] = types
        save_config(cfg)
        return jsonify({"success": True, "collections": collections, "product_types": types})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sync/etsy", methods=["POST"])
@require_auth
def sync_etsy():
    cfg       = load_config()
    etsy_cfg  = cfg.get("shops", {}).get("etsy", {})
    api_key   = etsy_cfg.get("api_key", "")
    shop_id   = etsy_cfg.get("shop_id", "")
    if not api_key or not shop_id:
        return jsonify({"error": "Etsy credentials not configured"}), 400
    import urllib.request as _ur, ssl as _ssl
    try:
        ctx = _ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=_ssl.CERT_NONE
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        req = _ur.Request(f"https://openapi.etsy.com/v3/application/shops/{shop_id}/shipping-profiles", headers=headers)
        with _ur.urlopen(req, context=ctx) as r:
            sp_data = json.loads(r.read())
        profiles = [{"id": p["shipping_profile_id"], "title": p["title"]} for p in sp_data.get("results", [])]
        if "shops" not in cfg: cfg["shops"] = {}
        if "etsy" not in cfg["shops"]: cfg["shops"]["etsy"] = {}
        cfg["shops"]["etsy"]["synced_shipping_profiles"] = profiles
        save_config(cfg)
        return jsonify({"success": True, "shipping_profiles": profiles})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sync/amazon", methods=["POST"])
@require_auth
def sync_amazon():
    # Amazon SP-API is complex — store manually entered categories
    cfg  = load_config()
    data = request.json or {}
    cats = data.get("categories", [])
    if "shops" not in cfg: cfg["shops"] = {}
    if "amazon" not in cfg["shops"]: cfg["shops"]["amazon"] = {}
    cfg["shops"]["amazon"]["synced_categories"] = cats
    save_config(cfg)
    return jsonify({"success": True, "categories": cats})

@app.route("/api/save-product", methods=["POST"])
@require_auth
def save_product():
    # Accept multipart (with image) or JSON (without)
    if request.content_type and "multipart" in request.content_type:
        data         = json.loads(request.form.get("data", "{}"))
        img_file     = request.files.get("image")
        image_bytes  = img_file.read() if img_file else None
        image_fname  = img_file.filename if img_file else data.get("dateiname", "image.jpg")
    else:
        data        = request.json or {}
        image_bytes = None
        image_fname = data.get("dateiname", "image.jpg")

    if not data:
        return jsonify({"error": "No data."}), 400

    try:
        cfg        = load_config()
        result     = {"success": True}
        dekoire_id = data.get("dekoire_id", "")

        # ── 1. Final Files folder ─────────────────────────────────────────────
        if image_bytes:
            titel  = data.get("titel", "")
            folder = create_final_folder(dekoire_id, titel, image_bytes, image_fname, cfg)
            if folder:
                result["folder"] = str(folder)

        # ── 2. Supabase save ──────────────────────────────────────────────────
        if image_bytes:
            sb_result = supabase_save(cfg, data, image_bytes, image_fname)
            image_url = sb_result.get("image_url", "")
            if image_url:
                result["image_url"] = image_url
            if sb_result.get("supabase_id"):
                result["supabase_id"] = sb_result["supabase_id"]

        result["dekoire_id"] = dekoire_id
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Discord Notifications ─────────────────────────────────────────────────────

def _build_discord_embed(ntype: str, payload: dict, result: dict, changes: list) -> dict:
    """Build a rich Discord embed dict."""
    def trunc(s, n=80):
        s = str(s or "—")
        return s[:n] + "…" if len(s) > n else s

    if ntype == "create":
        fields = []
        if payload.get("dekoire_id"):
            fields.append({"name": "🆔 Dekoire ID",   "value": payload["dekoire_id"], "inline": True})
        if payload.get("dateiname"):
            fields.append({"name": "📁 Dateiname",    "value": trunc(payload["dateiname"], 60), "inline": True})
        if payload.get("titel"):
            fields.append({"name": "📝 Titel",        "value": trunc(payload["titel"]), "inline": False})
        if payload.get("beschreibung"):
            fields.append({"name": "📄 Beschreibung", "value": trunc(payload["beschreibung"], 200), "inline": False})
        if payload.get("kunstart"):
            fields.append({"name": "🖼️ Kunstart",    "value": payload["kunstart"], "inline": True})
        if payload.get("epoche"):
            fields.append({"name": "🕰️ Epoche",      "value": payload["epoche"],  "inline": True})
        farben = payload.get("dominante_farben", [])
        if farben:
            fields.append({"name": "🎨 Farben", "value": ", ".join(farben) if isinstance(farben, list) else str(farben), "inline": True})
        tags = payload.get("tags", [])
        if tags:
            tag_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
            fields.append({"name": "🏷️ Tags", "value": trunc(tag_str, 120), "inline": True})
        if payload.get("breite_px") and payload.get("hoehe_px"):
            s = f"{payload['breite_px']} × {payload['hoehe_px']} px"
            if payload.get("ausrichtung"):      s += f" · {payload['ausrichtung']}"
            if payload.get("seitenverhaeltnis"):s += f" · {payload['seitenverhaeltnis']}"
            fields.append({"name": "📐 Bildgröße", "value": s, "inline": False})
        if payload.get("aufnahmedatum"):
            fields.append({"name": "📷 Aufnahmedatum", "value": payload["aufnahmedatum"], "inline": True})
        if payload.get("datei_groesse_kb"):
            kb = payload["datei_groesse_kb"]
            fields.append({"name": "💾 Dateigröße", "value": f"{kb/1024:.1f} MB ({kb} KB)", "inline": True})

        saved = []
        saved.append("✅ Excel gespeichert")
        if result.get("folder"):    saved.append(f"✅ Ordner: `{result['folder'].split('/')[-1]}`")
        if result.get("image_url"): saved.append("✅ Supabase gespeichert")
        fields.append({"name": "💾 Ergebnis", "value": "\n".join(saved), "inline": False})

        embed: dict = {"title": "🎨 Neues Produkt angelegt", "color": 3066993, "fields": fields,
                       "footer": {"text": "dekoire.com · Image Analyzer"}}
        img = result.get("image_url", "")
        if img and img.startswith("http"):
            embed["thumbnail"] = {"url": img}
        return embed

    elif ntype == "edit":
        fields = []
        if payload.get("dekoire_id"):
            fields.append({"name": "🆔 Dekoire ID", "value": payload["dekoire_id"], "inline": True})
        if payload.get("titel"):
            fields.append({"name": "📝 Titel", "value": trunc(payload["titel"]), "inline": True})
        if changes:
            lines = []
            for c in changes[:10]:
                b = trunc(c.get("before") or "—", 55)
                a = trunc(c.get("after")  or "—", 55)
                lines.append(f"**{c.get('field','')}**\n~~{b}~~ → `{a}`")
            fields.append({"name": f"📊 Änderungen ({len(changes)})", "value": "\n".join(lines), "inline": False})
        fields.append({"name": "💾 Status", "value": "✅ Datenbank aktualisiert", "inline": False})

        embed = {"title": "✏️ Produkt bearbeitet", "color": 3447003, "fields": fields,
                 "footer": {"text": "dekoire.com · Image Analyzer"}}
        img = payload.get("image_url", "")
        if img and img.startswith("http"):
            embed["thumbnail"] = {"url": img}
        return embed

    return {"title": "Notification", "color": 8421504}


@app.route("/api/notify/discord", methods=["POST"])
@require_auth
def notify_discord():
    cfg     = load_config()
    webhook = cfg.get("notifications", {}).get("discord_webhook", "").strip()
    if not webhook:
        return jsonify({"skipped": True})

    body_data = request.json or {}
    ntype     = body_data.get("type", "")
    payload   = body_data.get("payload", {})
    result    = body_data.get("result", {})
    changes   = body_data.get("changes", [])

    try:
        embed   = _build_discord_embed(ntype, payload, result, changes)
        import urllib.request as _ur
        import urllib.error  as _ue
        import ssl as _ssl
        ctx     = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = _ssl.CERT_NONE
        raw     = json.dumps({"embeds": [embed]}).encode()
        req     = _ur.Request(webhook, data=raw,
                              headers={"Content-Type": "application/json",
                                       "User-Agent": "DiscordBot (https://github.com, 1.0)"},
                              method="POST")
        try:
            with _ur.urlopen(req, timeout=10, context=ctx):
                pass
        except _ue.HTTPError as he:
            body = he.read().decode("utf-8", errors="replace")
            return jsonify({"error": f"HTTP {he.code}: {he.reason}", "discord_response": body}), 500
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── External Apps API ────────────────────────────────────────────────────────

@app.route("/api/external-apps", methods=["GET"])
@require_auth
def list_external_apps():
    cfg = load_config()
    return jsonify(cfg.get("external_apps", []))

@app.route("/api/external-apps", methods=["POST"])
@require_auth
def add_external_app():
    data = request.json or {}
    name = data.get("name", "").strip()
    url  = data.get("url", "").strip()
    if not name or not url:
        return jsonify({"error": "Name und URL erforderlich"}), 400
    # Ensure URL has scheme
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    app_id = uuid.uuid4().hex[:8]
    new_app = {
        "id":          app_id,
        "name":        name,
        "url":         url,
        "icon_type":   data.get("icon_type",   "favicon"),
        "icon_preset": data.get("icon_preset", ""),
        "icon_color":  data.get("icon_color",  "#FFFFFF"),
        "icon_url":    data.get("icon_url",    ""),
        "bg_color":    data.get("bg_color",    "#EEF2FF"),
    }
    cfg = load_config()
    apps = cfg.get("external_apps", [])
    apps.append(new_app)
    cfg["external_apps"] = apps
    save_config(cfg)
    domain = url.replace("https://", "").replace("http://", "").split("/")[0]
    new_app["domain"] = domain
    return jsonify({"success": True, "app": new_app})

@app.route("/api/external-apps/<app_id>", methods=["DELETE"])
@require_auth
def delete_external_app(app_id):
    cfg  = load_config()
    apps = cfg.get("external_apps", [])
    cfg["external_apps"] = [a for a in apps if a.get("id") != app_id]
    save_config(cfg)
    # Clean up uploaded icon if exists
    icon_path = APP_ICONS_DIR / f"{app_id}.jpg"
    icon_path.unlink(missing_ok=True)
    return jsonify({"success": True})

@app.route("/api/external-apps/<app_id>", methods=["POST"])
@require_auth
def update_external_app(app_id):
    data = request.json or {}
    cfg  = load_config()
    apps = cfg.get("external_apps", [])
    for a in apps:
        if a.get("id") == app_id:
            for field in ("name", "url", "icon_type", "icon_preset", "icon_color", "icon_url", "bg_color"):
                if field in data:
                    a[field] = data[field]
            break
    cfg["external_apps"] = apps
    save_config(cfg)
    return jsonify({"success": True})

@app.route("/api/external-apps/reorder", methods=["POST"])
@require_auth
def reorder_external_apps():
    """Accepts a list of app IDs in the desired new order."""
    ordered_ids = request.json or []
    cfg  = load_config()
    apps = cfg.get("external_apps", [])
    id_map = {a["id"]: a for a in apps}
    reordered = [id_map[i] for i in ordered_ids if i in id_map]
    # Append any apps not included in the order (safety net)
    seen = set(ordered_ids)
    for a in apps:
        if a["id"] not in seen:
            reordered.append(a)
    cfg["external_apps"] = reordered
    save_config(cfg)
    return jsonify({"success": True})

@app.route("/api/external-apps/upload-icon", methods=["POST"])
@require_auth
def upload_app_icon():
    if "icon" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f         = request.files["icon"]
    app_id    = request.form.get("app_id", uuid.uuid4().hex[:8])
    compressed = compress_to_jpeg(f.read(), max_mb=0.5)
    (APP_ICONS_DIR / f"{app_id}.jpg").write_bytes(compressed)
    icon_url = f"/static/app-icons/{app_id}.jpg"
    return jsonify({"success": True, "icon_url": icon_url, "app_id": app_id})

# ── User Management API ───────────────────────────────────────────────────────

@app.route("/api/users", methods=["GET"])
@require_auth
def list_users():
    try:
        sb         = get_sb_admin()
        auth_resp  = sb.auth.admin.list_users()
        users_list = auth_resp if isinstance(auth_resp, list) else getattr(auth_resp, "users", [])
        result = []
        for u in users_list:
            uid = str(u.id)
            try:
                pres    = sb.table("user_profiles").select("*").eq("user_id", uid).execute()
                profile = (pres.data or [{}])[0]
            except Exception:
                profile = {}
            result.append({
                "id":                uid,
                "email":             u.email,
                "created_at":        str(u.created_at)[:10],
                "vorname":           profile.get("vorname", ""),
                "nachname":          profile.get("nachname", ""),
                "profile_image_url": profile.get("profile_image_url", ""),
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/users", methods=["POST"])
@require_auth
def create_user():
    data     = request.json or {}
    email    = data.get("email", "").strip()
    password = data.get("password", "")
    vorname  = data.get("vorname", "").strip()
    nachname = data.get("nachname", "").strip()
    if not email or not password:
        return jsonify({"error": "E-Mail und Passwort erforderlich"}), 400
    try:
        sb  = get_sb_admin()
        res = sb.auth.admin.create_user({"email": email, "password": password, "email_confirm": True})
        uid = str(res.user.id)
        sb.table("user_profiles").upsert(
            {"user_id": uid, "vorname": vorname, "nachname": nachname, "profile_image_url": ""},
            on_conflict="user_id",
        ).execute()
        return jsonify({"success": True, "id": uid, "email": email, "vorname": vorname, "nachname": nachname})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/users/<user_id>/profile", methods=["POST"])
@require_auth
def update_user_profile_route(user_id):
    data = request.json or {}
    try:
        sb      = get_sb_admin()
        cur_res = sb.table("user_profiles").select("profile_image_url").eq("user_id", user_id).execute()
        cur_img = ((cur_res.data or [{}])[0]).get("profile_image_url", "")
        sb.table("user_profiles").upsert(
            {"user_id": user_id, "vorname": data.get("vorname",""), "nachname": data.get("nachname",""), "profile_image_url": cur_img},
            on_conflict="user_id",
        ).execute()
        if user_id == session.get("user_id"):
            session["user_vorname"]  = data.get("vorname", "")
            session["user_nachname"] = data.get("nachname", "")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/users/<user_id>/profile-image", methods=["POST"])
@require_auth
def upload_profile_image(user_id):
    if "image" not in request.files:
        return jsonify({"error": "No image provided"}), 400
    f         = request.files["image"]
    compressed = compress_to_jpeg(f.read(), max_mb=0.3)
    (PROFILES_DIR / f"{user_id}.jpg").write_bytes(compressed)
    image_url = f"/static/profiles/{user_id}.jpg"
    try:
        sb      = get_sb_admin()
        cur_res = sb.table("user_profiles").select("vorname,nachname").eq("user_id", user_id).execute()
        cur     = (cur_res.data or [{}])[0]
        sb.table("user_profiles").upsert(
            {"user_id": user_id, "vorname": cur.get("vorname",""), "nachname": cur.get("nachname",""), "profile_image_url": image_url},
            on_conflict="user_id",
        ).execute()
        if user_id == session.get("user_id"):
            session["user_profile_image"] = image_url
    except Exception as e:
        print(f"[Profile image DB] {e}")
    return jsonify({"success": True, "image_url": image_url})

# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    CACHE_DIR.mkdir(exist_ok=True)
    _migrate_db()
    _seed_external_apps()
    url = "http://localhost:5001"
    print(f"Image Analyzer UI → {url}")
    Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(debug=False, port=5001)
