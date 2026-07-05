# app.py — WGXC Tower Fallback Dashboard
import math
import hashlib, hmac, secrets
import os, socket, json, urllib.request, urllib.parse, threading, time, uuid, subprocess, shutil
from datetime import timedelta
from dataclasses import dataclass, asdict, field
from flask import Flask, render_template, request, jsonify, session, redirect, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB upload limit

LIQUIDSOAP_HOST    = os.environ.get('LIQUIDSOAP_HOST', 'localhost')
LIQUIDSOAP_PORT    = int(os.environ.get('LIQUIDSOAP_PORT', 1234))
PROMETHEUS_URL     = os.environ.get('PROMETHEUS_URL', 'http://10.50.0.7:9090')
# Failsafe failover watchdog (failsafe-watchdog.py) — owns the USB-RLY82 relay on
# the Angry Audio Failsafe Gadget's DB-9 logic port. Runs on a SEPARATE box:
# studio-one (10.50.0.20) for the bench, tower-box (10.0.3.3) in production — set
# FAILSAFE_HOST in the service env on deploy. This dashboard feeds Failsafe Input
# A (primary); MacOS Player 2 feeds Input B (backup).
FAILSAFE_HOST      = os.environ.get('FAILSAFE_HOST', '10.50.0.20')
FAILSAFE_PORT      = os.environ.get('FAILSAFE_PORT', '9201')
# Seconds to wait after silencing A before pulsing the relay to B, so the mute has
# propagated through Liquidsoap's output buffer and A is genuinely silent at the
# Gadget — otherwise we switch while A still sounds and AUTO recovery can fight it.
# Tune against the bench/standby box; 1.0s is a safe starting point.
FAILSAFE_MUTE_SETTLE = float(os.environ.get('FAILSAFE_MUTE_SETTLE', '1.0'))
MUSIC_DIR          = os.environ.get('MUSIC_DIR', '/home/tower-one/wgxc-dashboard/music/local')
PLAYLISTS_DIR      = os.environ.get('PLAYLISTS_DIR', '/home/tower-one/wgxc-dashboard/playlists')
LOGS_DIR           = os.environ.get('LOGS_DIR', '/home/tower-one/wgxc-dashboard/logs')
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'ogg', 'flac'}

# Ensure directories exist
os.makedirs(MUSIC_DIR, exist_ok=True)
os.makedirs(PLAYLISTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Emergency fallback playlist bridge: Liquidsoap's automation-loss fallback and
# the restream failover both read emergency_playlist.m3u. That file is generated
# from the GUI playlist named by EMERGENCY_PLAYLIST_NAME — see
# _sync_emergency_m3u(), called on save of that playlist and at startup.
EMERGENCY_PLAYLIST_NAME = os.environ.get('EMERGENCY_PLAYLIST_NAME', 'emergency_fallback')
EMERGENCY_M3U = os.path.join(PLAYLISTS_DIR, 'emergency_playlist.m3u')

# ── Required login + per-operator audit trail ─────────────────────────────────
# Named operators with PINs (users.json, managed by tools/add_user.py). The
# session cookie lives 180 days so a desktop browser stays signed in; the
# audit log answers "who pressed the button" for every state change.
# Loopback is exempt: tower_exporter probes /api/status from 127.0.0.1.
AUTH_DIR    = os.environ.get('AUTH_DIR', os.path.dirname(PLAYLISTS_DIR) or '.')
USERS_FILE  = os.path.join(AUTH_DIR, 'users.json')
SECRET_FILE = os.path.join(AUTH_DIR, '.flask_secret')
AUDIT_LOG   = os.path.join(LOGS_DIR, 'audit.log')

def _load_secret():
    """Signing key for session cookies — persisted so restarts don't log everyone out."""
    try:
        with open(SECRET_FILE) as f:
            s = f.read().strip()
            if s:
                return s
    except FileNotFoundError:
        pass
    s = secrets.token_hex(32)
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(s)
    return s

app.secret_key = _load_secret()
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=180),
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_HTTPONLY=True,
)

def _load_users():
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _check_pin(user, pin):
    u = _load_users().get(user)
    if not u or not pin:
        return False
    h = hashlib.sha256((u['salt'] + pin).encode()).hexdigest()
    return hmac.compare_digest(h, u['pin_sha256'])

def _audit_line(user, text):
    try:
        ts = time.strftime('%Y-%m-%dT%H:%M:%S')
        with open(AUDIT_LOG, 'a') as f:
            f.write(f"{ts}  {user:<10}  {text}\n")
    except Exception as e:
        print(f"audit log error: {e}")

def _audit_request(user):
    detail = ''
    try:
        if request.path == '/api/change_pin':
            pass  # never write PINs into the audit log
        elif request.content_type and 'json' in request.content_type:
            body = request.get_json(silent=True)
            if body:
                detail = json.dumps(body)[:300]
        elif request.files:
            detail = 'file=' + ','.join(f.filename or '?' for f in request.files.values())
    except Exception:
        pass
    _audit_line(user, f"{request.remote_addr}  {request.method} {request.path}  {detail}".rstrip())

AUTH_EXEMPT = ('/login', '/logout', '/favicon.ico', '/sw.js')
# Sign a named operator out after this long with no real user activity. The
# background status poll (a GET every second) does NOT count — only user-action
# POSTs and the activity heartbeat refresh it — so a dashboard left open on an
# unattended screen still times out and frees the single-operator lock.
IDLE_LIMIT = int(os.environ.get('IDLE_TIMEOUT_SECS', 300))

@app.before_request
def _gate():
    local = request.remote_addr in ('127.0.0.1', '::1')
    user = session.get('user')
    if not (local or user):
        # Public assets: login page, PWA service worker/manifest, icons. The
        # browser fetches the manifest WITHOUT credentials, so PWA install
        # would fail if these sat behind auth. /static/ holds only icons+CSS.
        if request.path in AUTH_EXEMPT or request.path.startswith('/static/'):
            return
        if request.path.startswith('/api/'):
            return jsonify({"error": "auth required"}), 401
        return redirect('/login')
    # Idle auto-logout for named operators left unattended past IDLE_LIMIT.
    if user and request.path not in AUTH_EXEMPT:
        la = session.get('last_active')
        if la and (time.time() - la) > IDLE_LIMIT:
            _release(user)
            _audit_line(user, f"{request.remote_addr}  IDLE LOGOUT ({IDLE_LIMIT}s)")
            session.clear()
            if request.path.startswith('/api/'):
                return jsonify({"error": "idle timeout — signed out", "relogin": True}), 401
            return redirect('/login')
    # Authenticated (or on-box): record every state-changing call
    if request.method in ('POST', 'DELETE') and request.path.startswith('/api/'):
        if user:
            session['last_active'] = time.time()  # user action = activity
        _audit_request(user or 'local')
        # Single-operator lock: a named operator may only drive the tower if they
        # hold the lock. Loopback (exporter) and lock-management calls bypass it.
        if user and request.path not in LOCK_EXEMPT:
            h = _holder()
            if h and h["user"] != user:
                return jsonify({"error": "locked", "holder": _pub(h)}), 423
            _claim(user)  # claim if vacant / refresh as a heartbeat

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = (request.form.get('user') or '').strip().lower()
        pin  = (request.form.get('pin') or '').strip()
        if _check_pin(user, pin):
            session.permanent = True
            session['user'] = user
            session['last_active'] = time.time()
            _audit_line(user, f"{request.remote_addr}  LOGIN")
            return redirect('/')
        time.sleep(0.5)  # soften brute-force attempts
        _audit_line(user or '?', f"{request.remote_addr}  LOGIN FAILED")
        return render_template('login.html', users=sorted(_load_users()), error='Wrong PIN — try again')
    if session.get('user'):
        return redirect('/')
    return render_template('login.html', users=sorted(_load_users()), error=None)

@app.route('/logout')
def logout():
    u = session.pop('user', None)
    if u:
        _release(u)
        _audit_line(u, f"{request.remote_addr}  LOGOUT")
    return redirect('/login')

@app.route('/api/whoami')
def whoami():
    u = session.get('user')
    if not u and request.remote_addr in ('127.0.0.1', '::1'):
        u = 'local'
    return jsonify({"user": u})

@app.route('/api/change_pin', methods=['POST'])
def change_pin():
    user = session.get('user')
    if not user:
        return jsonify({"error": "no signed-in operator"}), 400
    cur = (request.json.get('current') or '').strip()
    new = (request.json.get('new') or '').strip()
    if not _check_pin(user, cur):
        time.sleep(0.5)
        return jsonify({"error": "current PIN incorrect"}), 403
    if not (new.isdigit() and 4 <= len(new) <= 12):
        return jsonify({"error": "PIN must be 4-12 digits"}), 400
    users = _load_users()
    salt = secrets.token_hex(8)
    rec = dict(users.get(user, {}))  # preserve name/email
    rec["salt"] = salt
    rec["pin_sha256"] = hashlib.sha256((salt + new).encode()).hexdigest()
    users[user] = rec
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)
    os.chmod(USERS_FILE, 0o600)
    _audit_line(user, f"{request.remote_addr}  PIN CHANGED")
    return jsonify({"result": "success"})

@app.route('/api/audit')
def audit_view():
    limit = int(request.args.get('limit', 100))
    if not os.path.exists(AUDIT_LOG):
        return jsonify({"log": []})
    try:
        with open(AUDIT_LOG) as f:
            lines = f.readlines()
        return jsonify({"log": lines[-limit:]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Single-operator lock ──────────────────────────────────────────────────────
# Only one operator drives the tower at a time. The browser sends a heartbeat;
# if it stops (tab closed, asleep) the lock auto-releases after HOLD_TIMEOUT so
# nobody gets stranded out. A second operator sees an "in use" modal and can
# Contact the holder or Override (audited). State-changing writes are rejected
# with 423 for anyone who isn't the holder. Held in memory — single gunicorn
# worker, so a restart cleanly clears the lock. Loopback (exporter) is exempt.
HOLD_TIMEOUT = 150  # seconds without a heartbeat before the lock auto-releases
_presence = {"user": None, "since": 0.0, "last_seen": 0.0}
_presence_lock = threading.Lock()
# Writes that must never be lock-gated (they manage the lock or the account).
LOCK_EXEMPT = ('/api/claim', '/api/heartbeat', '/api/release', '/api/change_pin')

def _holder():
    """Current live holder dict, or None if vacant/stale."""
    with _presence_lock:
        h = dict(_presence)
    if h["user"] and (time.time() - h["last_seen"]) <= HOLD_TIMEOUT:
        return h
    return None

def _claim(user, override=False):
    """Try to grab/refresh the lock for `user`.
    Returns (ok, blocking_holder, took_from)."""
    now = time.time()
    with _presence_lock:
        cur = _presence["user"]
        fresh = bool(cur) and (now - _presence["last_seen"]) <= HOLD_TIMEOUT
        if fresh and cur != user and not override:
            return False, dict(_presence), None
        took_from = cur if (fresh and cur and cur != user) else None
        if cur != user:
            _presence["since"] = now
        _presence["user"] = user
        _presence["last_seen"] = now
        return True, None, took_from

def _release(user):
    with _presence_lock:
        if _presence["user"] == user:
            _presence.update(user=None, since=0.0, last_seen=0.0)

def _pub(h):
    """Public holder view for the UI — name/email, never the PIN hash."""
    if not h:
        return None
    u = _load_users().get(h["user"], {})
    return {
        "user":  h["user"],
        "name":  u.get("name") or h["user"].title(),
        "email": u.get("email"),
        "since": h["since"],
        "last_seen": h["last_seen"],
    }

@app.route('/api/presence')
def presence():
    local = request.remote_addr in ('127.0.0.1', '::1')
    me = session.get('user') or ('local' if local else None)
    h = _holder()
    return jsonify({"me": me, "holder": _pub(h),
                    "is_me": bool(h and h["user"] == me)})

@app.route('/api/claim', methods=['POST'])
def claim():
    me = session.get('user')
    if not me:
        return jsonify({"error": "no signed-in operator"}), 400
    override = bool((request.get_json(silent=True) or {}).get('override'))
    ok, blocker, took_from = _claim(me, override)
    if not ok:
        return jsonify({"error": "locked", "holder": _pub(blocker)}), 423
    if took_from:
        _audit_line(me, f"{request.remote_addr}  OVERRIDE  took control from {took_from}")
    return jsonify({"ok": True, "holder": _pub(_holder())})

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    me = session.get('user')
    if not me:
        return jsonify({"ok": False}), 400
    h = _holder()
    if h and h["user"] != me:
        return jsonify({"ok": False, "holder": _pub(h)}), 423  # someone took over
    _claim(me)  # refresh last_seen (claims if vacant)
    return jsonify({"ok": True})

@app.route('/api/release', methods=['POST'])
def release():
    me = session.get('user')
    if me:
        _release(me)
    return jsonify({"ok": True})
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Track:
    id: str
    type: str  # "file" | "stream"
    uri: str
    label: str
    duration: float = None
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)

# Dead-air watchdog: if the emergency source is routed to air but the output
# is true silence past the threshold, force the next track — REGARDLESS of
# player state. Recovers from corrupt/zero-byte files, dead streams, and the
# "selected-but-stopped" stuck state that left the box silent indefinitely.
SILENCE_GUARD_DB   = -69.0
SILENCE_GUARD_SECS = 30

class PlaylistEngine:
    def __init__(self, state_path, music_dir):
        self.state_path = state_path
        self.music_dir = music_dir
        self.lock = threading.Lock()
        self.tracks = []
        self.current_index = -1
        self.mode = "repeat_all" # sequential, shuffle, repeat_one, repeat_all — emergency queue defaults to looping
        self.state = "stopped" # playing, stopped
        self.started_at = None
        self._silence_since = None
        self.load()
        
        self.advance_thread = threading.Thread(target=self._advance_loop, daemon=True)
        self.advance_thread.start()

    def load(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r') as f:
                    data = json.load(f)
                    self.tracks = [Track.from_dict(t) for t in data.get('tracks', [])]
                    self.current_index = data.get('current_index', -1)
                    self.mode = data.get('mode', 'repeat_all')
                    # Always start stopped — Liquidsoap's queue is cleared on restart,
                    # so a stale started_at would cause the advance loop to fire for
                    # every track in the queue simultaneously.
                    self.state = 'stopped'
                    self.started_at = None
            except Exception as e:
                print(f"Error loading queue state: {e}")

    def save(self):
        data = {
            "tracks": [asdict(t) for t in self.tracks],
            "current_index": self.current_index,
            "mode": self.mode,
            "state": self.state,
            "started_at": self.started_at
        }
        try:
            temp_path = self.state_path + ".tmp"
            with open(temp_path, 'w') as f:
                json.dump(data, f)
            os.replace(temp_path, self.state_path)
        except Exception as e:
            print(f"Error saving queue state: {e}")

    def _advance_loop(self):
        while True:
            try:
                self._check_advance()
            except Exception as e:
                print(f"Advance loop error: {e}")
            time.sleep(2)

    def _check_advance(self):
        # Snapshot under lock, release before telnet I/O (telnet can block up to
        # 5s and would otherwise stall every Flask handler that needs the lock).
        with self.lock:
            idx        = self.current_index
            n          = len(self.tracks)
            state      = self.state
            started_at = self.started_at
            track      = self.tracks[idx] if 0 <= idx < n else None
        is_stream = bool(track) and track.type == "stream"

        # Always read bus + RMS — even when stopped — so the silence watchdog
        # can recover a dead-silent emergency source regardless of player state.
        res = liq_multi(["radio.ready emergency", "audio.get_rms", "audio.get_bus emergency"])
        res_clean = res[0].strip().lower()
        if res_clean not in ("true", "false"):
            print(f"[advance] Liquidsoap telnet error, skipping cycle: {res[0]!r}")
            return
        is_ready = res_clean == "true"

        def _f(s, default=0.0):
            try:
                return float(s)
            except (TypeError, ValueError):
                return default
        rms = _f(res[1])
        bus = _f(res[2])
        rms_db = 20 * math.log10(rms) if rms > 0 else -100.0
        now = time.time()
        on_air = bus > 0.5

        # ── Silence watchdog (emergency only) ──────────────────────────────
        # Emergency is routed to air but dead-silent past the threshold → force
        # playback forward, REGARDLESS of player state. This is what recovers the
        # "selected-but-stopped / mystery stuck" dead-air case the old guard
        # missed (it only armed while state == "playing").
        guard_tripped = False
        if on_air and rms_db < SILENCE_GUARD_DB:
            if self._silence_since is None:
                self._silence_since = now
            elif now - self._silence_since >= SILENCE_GUARD_SECS:
                guard_tripped = True
        else:
            self._silence_since = None

        # Normal end-of-track auto-advance — only meaningful while a file plays.
        elapsed = now - started_at if started_at else 0
        track_ended = (state == "playing" and track is not None and not is_stream and (
            (not is_ready and elapsed > 5) or
            (track.duration and elapsed > track.duration + 2)))

        if not (track_ended or guard_tripped):
            return

        with self.lock:
            if not self.tracks:
                self._silence_since = None
                return
            if guard_tripped:
                self._silence_since = None
                print(f"DEAD-AIR GUARD: emergency silent below {SILENCE_GUARD_DB} dBFS "
                      f"for {SILENCE_GUARD_SECS}s (state={self.state}) — forcing next track")
                if track:
                    self._log_guard(track)
                # Escape any stuck/broken track: jump to the next track and start
                # it, independent of mode — never sit silent on air.
                self.current_index = ((self.current_index + 1) % len(self.tracks)
                                      if self.current_index >= 0 else 0)
                self._play_current()
            elif self.state == "playing" and self.started_at == started_at:
                # Re-check we're still on the same track before normal advance.
                label = track.label if track else "?"
                print(f"Auto-advancing: Track '{label}' ended (elapsed: {elapsed:.1f}s)")
                self._advance_logic()

    def _advance_logic(self):
        # self.lock should be held
        if not self.tracks:
            self.state = "stopped"
            self.current_index = -1
            self.save()
            return

        if self.mode == "repeat_one":
            pass # index stays same
        elif self.mode == "shuffle":
            import random
            self.current_index = random.randint(0, len(self.tracks) - 1)
        elif self.mode == "repeat_all":
            self.current_index = (self.current_index + 1) % len(self.tracks)
        else: # sequential
            self.current_index += 1
            if self.current_index >= len(self.tracks):
                self.current_index = -1
                self.state = "stopped"
                self.save()
                return

        self._play_current()

    def _play_current(self):
        # self.lock should be held
        if 0 <= self.current_index < len(self.tracks):
            track = self.tracks[self.current_index]
            liq(f"emergency_queue.play_now {track.uri}")
            self.started_at = time.time()
            self.state = "playing"
            self._silence_since = None
            self.save()
            self._log_play(track)

    def _log_guard(self, track):
        log_path = os.path.join(LOGS_DIR, 'playlog.txt')
        ts = time.strftime('%Y-%m-%dT%H:%M:%S')
        line = f"{ts}  GUARD   Dead-air skip ({SILENCE_GUARD_SECS}s < {SILENCE_GUARD_DB} dBFS): {track.label}\n"
        try:
            with open(log_path, 'a') as f:
                f.write(line)
        except Exception as e:
            print(f"Error writing guard line to playlog: {e}")

    def _log_play(self, track):
        log_path = os.path.join(LOGS_DIR, 'playlog.txt')
        ts = time.strftime('%Y-%m-%dT%H:%M:%S')
        line = f"{ts}  {track.type.upper():6}  {track.label}\n"
        try:
            with open(log_path, 'a') as f:
                f.write(line)
        except Exception as e:
            print(f"Error writing to playlog: {e}")

    def get_state(self):
        with self.lock:
            return {
                "tracks": [asdict(t) for t in self.tracks],
                "current_index": self.current_index,
                "mode": self.mode,
                "state": self.state,
                "started_at": self.started_at,
                "server_time": time.time()
            }

    def add(self, track_data, position=None):
        with self.lock:
            if 'id' not in track_data:
                track_data['id'] = str(uuid.uuid4())
            t = Track.from_dict(track_data)
            if position == "next" and self.current_index >= 0:
                self.tracks.insert(self.current_index + 1, t)
            elif isinstance(position, int):
                self.tracks.insert(position, t)
            else:
                self.tracks.append(t)
            self.save()
            return t

    def add_bulk(self, tracks_data, position=None):
        with self.lock:
            new_tracks = []
            for td in tracks_data:
                if 'id' not in td:
                    td['id'] = str(uuid.uuid4())
                new_tracks.append(Track.from_dict(td))
                
            if position == "next" and self.current_index >= 0:
                self.tracks[self.current_index+1:self.current_index+1] = new_tracks
            elif isinstance(position, int):
                self.tracks[position:position] = new_tracks
            else:
                self.tracks.extend(new_tracks)
            self.save()

    def play(self, index):
        with self.lock:
            if 0 <= index < len(self.tracks):
                self.current_index = index
                self._play_current()

    def skip(self):
        with self.lock:
            if self.tracks:
                self._advance_logic()

    def stop(self):
        with self.lock:
            self.state = "stopped"
            liq("emergency_queue.skip") # Clears current if playing
            liq("audio.bus emergency 0.0")
            self.save()

    def remove(self, index):
        with self.lock:
            if 0 <= index < len(self.tracks):
                del self.tracks[index]
                if self.current_index == index:
                    self.current_index = -1
                    self.state = "stopped"
                elif self.current_index > index:
                    self.current_index -= 1
                self.save()

    def move(self, from_idx, to_idx):
        with self.lock:
            if 0 <= from_idx < len(self.tracks) and 0 <= to_idx < len(self.tracks):
                track = self.tracks.pop(from_idx)
                self.tracks.insert(to_idx, track)
                # Adjust current_index
                if self.current_index == from_idx:
                    self.current_index = to_idx
                elif from_idx < self.current_index <= to_idx:
                    self.current_index -= 1
                elif to_idx <= self.current_index < from_idx:
                    self.current_index += 1
                self.save()

    def clear(self):
        with self.lock:
            self.tracks = []
            self.current_index = -1
            self.state = "stopped"
            self.save()

    def set_mode(self, mode):
        with self.lock:
            if mode in ("sequential", "shuffle", "repeat_one", "repeat_all"):
                self.mode = mode
                self.save()

# Liquidsoap telnet helpers — defined BEFORE the engine is instantiated because
# PlaylistEngine() below starts a background advance thread that calls liq_multi
# every 2s. If these lived further down the file, that thread could fire before
# they were bound and throw "name 'liq_multi' is not defined" during startup.
def liq(cmd):
    return liq_multi([cmd])[0]

def liq_multi(cmds):
    responses = []
    try:
        with socket.create_connection((LIQUIDSOAP_HOST, LIQUIDSOAP_PORT), timeout=3) as s:
            s.settimeout(0.5)
            buf = b""
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk: break
                    buf += chunk
                    if b"commands." in buf or b"Available commands" in buf:
                        break
            except socket.timeout:
                pass

            s.settimeout(2)
            for cmd in cmds:
                s.sendall(f"{cmd}\n".encode())
                res = b""
                try:
                    while b"\nEND" not in res:
                        chunk = s.recv(4096)
                        if not chunk: break
                        res += chunk
                except (socket.timeout, ConnectionResetError):
                    pass
                responses.append(res.decode(errors="replace").split("\nEND")[0].strip())

            try: s.sendall(b"exit\n")
            except OSError: pass
    except Exception as e:
        responses = [f"Error: {e}"] * max(len(cmds), 1)
    return responses

def _sync_emergency_m3u():
    """Regenerate emergency_playlist.m3u from the GUI playlist of the same name,
    then hot-reload the two Liquidsoap playlist sources that read it. Local files
    only — streams are useless during the network outage this fallback exists for."""
    src = os.path.join(PLAYLISTS_DIR, f'{EMERGENCY_PLAYLIST_NAME}.json')
    try:
        with open(src) as f:
            items = json.load(f)
    except FileNotFoundError:
        items = []
    except Exception as e:
        print(f"emergency m3u sync: failed to read {src}: {e}")
        return
    uris = [i['uri'] for i in items
            if i.get('type') == 'file' and i.get('uri') and os.path.exists(i['uri'])]
    try:
        with open(EMERGENCY_M3U, 'w') as f:
            f.write('\n'.join(uris) + ('\n' if uris else ''))
    except Exception as e:
        print(f"emergency m3u sync: failed to write {EMERGENCY_M3U}: {e}")
        return
    # Liquidsoap registers the two playlist() sources (emergency_auto,
    # restream_emergency) as playlist / playlist.1 — reload both.
    liq_multi(["playlist.reload", "playlist.1.reload"])
    print(f"emergency m3u sync: {len(uris)} tracks")

engine = PlaylistEngine(os.path.join(PLAYLISTS_DIR, '_queue_state.json'), MUSIC_DIR)
_sync_emergency_m3u()

try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

library_cache = []

def _read_file_info(path, filename, rel_dir=''):
    """Read metadata for a single file — one mutagen header read, no full walk."""
    info = {
        "filename": filename,
        "path": path,
        "folder": rel_dir,
        "title": filename,
        "artist": "UNKNOWN",
        "album": "Unknown Album",
        "duration": 0,
        "duration_display": "0:00",
        "size_mb": round(os.path.getsize(path) / (1024*1024), 2)
    }

    if HAS_MUTAGEN:
        try:
            audio = None
            if filename.lower().endswith('.mp3'):
                audio = MP3(path, ID3=EasyID3)
                info['duration'] = audio.info.length
                info['artist'] = audio.get('artist', ['UNKNOWN'])[0]
                info['title'] = audio.get('title', [filename])[0]
                info['album'] = (audio.get('album', ['']) or [''])[0] or 'Unknown Album'
            elif filename.lower().endswith('.flac'):
                from mutagen.flac import FLAC
                audio = FLAC(path)
                info['duration'] = audio.info.length
                info['artist'] = audio.get('artist', ['UNKNOWN'])[0]
                info['title'] = audio.get('title', [filename])[0]
                info['album'] = (audio.get('album', ['']) or [''])[0] or 'Unknown Album'
            else:
                # wav/ogg/anything else: generic probe. Duration here is what
                # keeps auto-advance alive — without it a track never "ends".
                audio = mutagen.File(path, easy=True)
                if audio is not None:
                    if audio.info:
                        info['duration'] = audio.info.length
                    if hasattr(audio, 'get'):
                        info['artist'] = (audio.get('artist') or ['UNKNOWN'])[0]
                        info['title'] = (audio.get('title') or [filename])[0]
                        info['album'] = (audio.get('album') or [''])[0] or 'Unknown Album'
            if info['duration'] > 0:
                m = int(info['duration'] // 60)
                s = int(info['duration'] % 60)
                info['duration_display'] = f"{m}:{s:02d}"
        except Exception as e:
            print(f"Error reading metadata for {filename}: {e}")

    return info

def scan_library():
    global library_cache
    new_cache = []
    for root, dirs, files in os.walk(MUSIC_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.')]  # skip .originals etc.
        # Relative folder from MUSIC_DIR (empty string for root)
        rel_dir = os.path.relpath(root, MUSIC_DIR)
        if rel_dir == '.':
            rel_dir = ''
        for f in files:
            if not allowed_file(f): continue
            path = os.path.join(root, f)
            if os.path.getsize(path) == 0: continue
            new_cache.append(_read_file_info(path, f, rel_dir))
    library_cache = sorted(new_cache, key=lambda x: x['title'].lower())

threading.Thread(target=scan_library, daemon=True).start()

def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/sw.js')
def service_worker():
    # Served from root so the worker's default scope is '/' (controls the whole
    # app, not just /static/). no-cache so a new SW is picked up on each load.
    resp = send_from_directory(app.static_folder or 'static', 'sw.js')
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/api/queue')
def get_queue():
    return jsonify(engine.get_state())

@app.route('/api/queue/add', methods=['POST'])
def queue_add():
    track = engine.add(request.json.get('track'), request.json.get('position'))
    return jsonify({"result": "success", "track": asdict(track)})

@app.route('/api/queue/add_bulk', methods=['POST'])
def queue_add_bulk():
    engine.add_bulk(request.json.get('tracks', []), request.json.get('position'))
    return jsonify({"result": "success"})

@app.route('/api/queue/play', methods=['POST'])
def queue_play():
    engine.play(request.json.get('index', 0))
    return jsonify({"result": "success"})

@app.route('/api/queue/select', methods=['POST'])
def queue_select():
    """Set current_index without starting playback — used when source is cued for crossfade."""
    idx = request.json.get('index', 0)
    with engine.lock:
        if 0 <= idx < len(engine.tracks):
            engine.current_index = idx
            engine.save()
    return jsonify({"result": "success"})

@app.route('/api/queue/skip', methods=['POST'])
def queue_skip():
    engine.skip()
    return jsonify({"result": "success"})

@app.route('/api/queue/stop', methods=['POST'])
def queue_stop():
    engine.stop()
    return jsonify({"result": "success"})

@app.route('/api/queue/remove', methods=['POST'])
def queue_remove():
    engine.remove(request.json.get('index', -1))
    return jsonify({"result": "success"})

@app.route('/api/queue/move', methods=['POST'])
def queue_move():
    engine.move(request.json.get('from', -1), request.json.get('to', -1))
    return jsonify({"result": "success"})

@app.route('/api/queue/clear', methods=['POST'])
def queue_clear():
    engine.clear()
    return jsonify({"result": "success"})

@app.route('/api/queue/mode', methods=['POST'])
def queue_mode():
    engine.set_mode(request.json.get('mode'))
    return jsonify({"result": "success"})

@app.route('/api/queue/log')
def get_queue_log():
    limit = int(request.args.get('limit', 50))
    log_path = os.path.join(LOGS_DIR, 'playlog.txt')
    if not os.path.exists(log_path):
        return jsonify({"log": []})
    try:
        with open(log_path, 'r') as f:
            lines = f.readlines()
        return jsonify({"log": lines[-limit:]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/status')
def status():
    cmds = [
        "radio.selected",
        "audio.get_volume",
        "audio.get_gain automation",
        "audio.get_gain live",
        "audio.get_gain emergency",
        "radio.ready automation",
        "radio.ready live",
        "radio.ready emergency",
        "audio.get_bus automation",
        "audio.get_bus live",
        "audio.get_bus emergency",
        "audio.get_stream_label",
        "audio.get_rms",
        "audio.get_meta",
        "automation.fallback_active",
        "dsp.gain",
        "dsp.status",
    ]
    res = liq_multi(cmds)
    def parse_float(s, default=0.0):
        try: return float(s)
        except: return default
    rms_linear = parse_float(res[12])
    rms_db = round(20 * math.log10(rms_linear), 1) if rms_linear > 0 else -100.0
    # dsp.gain returns e.g. "-3.20 dB (lin 0.69183)"; dsp.status starts "norm=true ..."
    try:
        dsp_gain_db = float(res[15].split(' ')[0])
    except (ValueError, IndexError):
        dsp_gain_db = None
    dsp_norm_on = res[16].startswith("norm=true")
    return jsonify({
        "selected": res[0],
        "volume":   res[1],
        "trim": {
            "automation": res[2],
            "live":       res[3],
            "emergency":  res[4]
        },
        "ready": {
            "automation": res[5].strip().lower() == "true",
            "live":       res[6].strip().lower() == "true",
            "emergency":  res[7].strip().lower() == "true"
        },
        "bus": {
            "automation": parse_float(res[8]),
            "live":       parse_float(res[9]),
            "emergency":  parse_float(res[10])
        },
        "stream_label": res[11],
        "rms_linear":   rms_linear,
        "rms_db":       rms_db,
        "meta":         res[13] if not res[13].startswith("Error") else "",
        "fallback_active": res[14].strip() == "true",
        "dsp": {
            "norm_on": dsp_norm_on,
            "gain_db": dsp_gain_db
        },
        "host":         socket.gethostname()
    })

@app.route('/api/thermal')
def thermal():
    """Fan RPM + CPU temp for the dashboard thermal widget. Any unreadable
    field returns null — the widget degrades gracefully, never breaks."""
    def read_int(path):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except Exception:
            return None
    fan_rpm = read_int('/sys/devices/platform/applesmc.768/fan1_input')
    fan_max = read_int('/sys/devices/platform/applesmc.768/fan1_max') or 5500
    cpu_temp = None
    try:
        for hw in os.listdir('/sys/class/hwmon'):
            base = f'/sys/class/hwmon/{hw}'
            try:
                with open(f'{base}/name') as f:
                    if f.read().strip() != 'coretemp':
                        continue
            except Exception:
                continue
            milli = read_int(f'{base}/temp1_input')
            if milli is not None:
                cpu_temp = milli / 1000.0
            break
    except Exception:
        pass
    return jsonify({"fan_rpm": fan_rpm, "fan_max": fan_max, "cpu_temp_c": cpu_temp})

@app.route('/api/switch', methods=['POST'])
def switch():
    """Update selected_source label only — JS manages bus ramping for crossfade."""
    src = request.json.get('source', '')
    if src in ('automation', 'live', 'emergency'):
        liq_multi([f"radio.label {src}", "audio.clear_meta"])
        return jsonify({"result": "success"})
    return jsonify({"error": "invalid source"}), 400

@app.route('/api/hardswitch', methods=['POST'])
def hardswitch():
    """Hard-cut: sets bus values immediately. Used on page load to sync state."""
    src = request.json.get('source', '')
    if src in ('automation', 'live', 'emergency'):
        liq_multi([f"radio.select {src}", "audio.clear_meta"])
        return jsonify({"result": "success"})
    return jsonify({"error": "invalid source"}), 400

# ── Server-side crossfade ─────────────────────────────────────────────────────
# The ramp runs in a Flask background thread, not the browser. Closing or
# refreshing the tab mid-fade can no longer strand buses at partial levels —
# the fade always completes server-side.
_fade_lock = threading.Lock()
_fade_state = {"active": False, "from": None, "to": None, "started": 0.0, "duration": 3.0}

def _crossfade_worker(from_src, to_src, duration):
    steps = 30
    try:
        for i in range(1, steps + 1):
            t = i / steps
            ease = 0.5 - 0.5 * math.cos(math.pi * t)  # sine ease in/out
            liq_multi([
                f"audio.bus {from_src} {1.0 - ease:.4f}",
                f"audio.bus {to_src} {ease:.4f}",
            ])
            time.sleep(duration / steps)
    finally:
        # Whatever happened above, land on a clean end state.
        liq_multi([
            f"audio.bus {from_src} 0.0",
            f"audio.bus {to_src} 1.0",
            f"radio.label {to_src}",
            "audio.clear_meta",
        ])
        with _fade_lock:
            _fade_state["active"] = False

@app.route('/api/crossfade', methods=['POST'])
def crossfade():
    from_src = request.json.get('from', '')
    to_src   = request.json.get('to', '')
    try:
        duration = min(10.0, max(0.5, float(request.json.get('duration', 3.0))))
    except (TypeError, ValueError):
        duration = 3.0
    valid = ('automation', 'live', 'emergency')
    if from_src not in valid or to_src not in valid or from_src == to_src:
        return jsonify({"error": "invalid sources"}), 400
    # Don't fade onto a stream that hasn't finished cueing — ramping the bus up
    # to a not-yet-connected input.http leaves the live source stranded (silent,
    # required a browser refresh). Streams must be ready first. Emergency is
    # primed below by the worker, so it's exempt.
    if to_src == 'live' and liq("radio.ready live").strip().lower() != "true":
        return jsonify({"error": "stream not cued yet — wait for it to finish buffering"}), 425
    if to_src == 'emergency':
        with engine.lock:
            has_track = 0 <= engine.current_index < len(engine.tracks)
        if not has_track:
            return jsonify({"error": "no track cued — pick a track first"}), 425
    with _fade_lock:
        if _fade_state["active"]:
            return jsonify({"error": "fade already running"}), 409
        _fade_state.update(active=True, started=time.time(), duration=duration)
        _fade_state["from"] = from_src
        _fade_state["to"] = to_src
    # Fading to the emergency source: start the cued track now so audio
    # arrives while the bus ramps up from zero.
    if to_src == 'emergency':
        with engine.lock:
            if engine.state != "playing" and 0 <= engine.current_index < len(engine.tracks):
                engine._play_current()
    threading.Thread(target=_crossfade_worker, args=(from_src, to_src, duration), daemon=True).start()
    return jsonify({"result": "fading", "duration": duration})

@app.route('/api/crossfade/state')
def crossfade_state():
    with _fade_lock:
        return jsonify(dict(_fade_state))
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/api/bus', methods=['POST'])
def bus():
    """Set routing bus for a source. level=0.0 (off) to 1.0 (on), floats for crossfade."""
    src   = request.json.get('source', '')
    level = request.json.get('level')
    if src not in ('automation', 'live', 'emergency') or level is None:
        return jsonify({"error": "invalid"}), 400
    val = max(0.0, min(1.0, float(level)))
    liq(f"audio.bus {src} {val}")
    return jsonify({"result": "success"})

@app.route('/api/volume', methods=['POST'])
def volume():
    val = request.json.get('level')
    if val is not None:
        liq(f"audio.volume {val}")
        return jsonify({"result": "success"})
    return jsonify({"error": "missing level"}), 400

# ── Failsafe failover (Angry Audio Gadget A/B via the RLY82 watchdog) ──────────
# Production keeps the Gadget's RECOVERY=AUTO (instant hardware flip-back to A the
# moment A has sound) — confirmed on the bench, the DB-9 logic port has no pin to
# override it. So to deliberately ride the backup (MacOS Player 2 on Input B) we
# SILENCE A: with A quiet, AUTO has nothing to recover to, so B holds. The relay
# select-B just makes the cut instant instead of waiting out the Gadget's silence
# delay. "Take back" restores A's audio and selects A.
#
# Note: silencing A uses the master volume (audio.volume 0) — no .liq change, no
# audio-engine restart — which means the dashboard's Master fader will read 0
# while handed off, and dragging it back up un-silences A (AUTO then returns to
# A). The failover panel surfaces the handed-off state so that's not a surprise.
def _failsafe_call(path, method='GET', timeout=3):
    url = f"http://{FAILSAFE_HOST}:{FAILSAFE_PORT}{path}"
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip().startswith('{') else {"raw": body}

@app.route('/api/failsafe/status')
def failsafe_status():
    """Live A/B tally from the watchdog. Degrades gracefully if the watchdog box
    is unreachable so the rest of the dashboard keeps working."""
    try:
        s = _failsafe_call('/status')
    except Exception as e:
        return jsonify({"available": False, "error": str(e)})
    mask = s.get('input_mask')
    on_air = 'A' if mask == 1 else 'B' if mask == 2 else '?'
    return jsonify({
        "available":  True,
        "on_air":     on_air,
        "tripped":    bool(s.get('tripped')),
        "board_up":   bool(s.get('board_up')),
        "input_mask": mask,
    })

@app.route('/api/failsafe/handoff', methods=['POST'])
def failsafe_handoff():
    """Hand broadcast to the backup (Player 2 / Input B): silence A, then cut to B."""
    liq("audio.volume 0")                       # local + reliable; AUTO can't flip back
    time.sleep(FAILSAFE_MUTE_SETTLE)            # let the mute reach the Gadget before we switch
    try:                                        # relay = instant cut (skip the silence delay)
        relay = _failsafe_call('/select?source=B', method='POST')
    except Exception as e:                      # if the watchdog box is down, the Gadget's
        relay = {"error": str(e)}               # own silence sensor still fails over to B
    return jsonify({"result": "success", "silenced_A": True, "relay": relay})

@app.route('/api/failsafe/takeback', methods=['POST'])
def failsafe_takeback():
    """Take broadcast back to primary (Input A): restore A's audio, select A."""
    liq("audio.volume 1.0")
    try:
        relay = _failsafe_call('/select?source=A', method='POST')
    except Exception as e:
        relay = {"error": str(e)}
    return jsonify({"result": "success", "silenced_A": False, "relay": relay})

@app.route('/api/gain', methods=['POST'])
def gain():
    """Set trim (level fader) for a source. Does NOT affect routing."""
    src = request.json.get('source', '')
    val = request.json.get('level')
    if src not in ('automation', 'live', 'emergency') or val is None:
        return jsonify({"error": "invalid"}), 400
    liq(f"audio.gain {src} {val}")
    return jsonify({"result": "success"})

@app.route('/api/music/files')
def music_files():
    query = request.args.get('q', '').lower()
    try:
        tracks = [t for t in library_cache if t['folder'] == '']
        if query:
            tracks = [t for t in tracks if
                      query in t['title'].lower() or
                      query in t['artist'].lower() or
                      query in t['filename'].lower()]
        return jsonify({"tracks": tracks, "total": len(tracks)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/music/play', methods=['POST'])
def music_play():
    filename = request.json.get('filename', '')
    if not filename:
        return jsonify({"error": "no filename"}), 400

    # Resolve to absolute path
    path = os.path.join(MUSIC_DIR, secure_filename(filename))
    if not os.path.exists(path):
        path = os.path.join(MUSIC_DIR, filename)
    if not os.path.exists(path):
        if filename.startswith('http'):
            path = filename
        else:
            return jsonify({"error": "file not found"}), 404

    # Route through PlaylistEngine so queue index stays in sync
    with engine.lock:
        for i, track in enumerate(engine.tracks):
            if track.uri == path or track.uri == filename:
                engine.current_index = i
                engine._play_current()
                return jsonify({"result": "success"})

    # Track not in queue — play directly and update engine state
    # (e.g. one-shot play from library browser without adding to queue)
    liq(f"emergency_queue.play_now {path}")
    engine.started_at = time.time()
    engine.state = "playing"
    engine.save()
    return jsonify({"result": "success"})

@app.route('/api/library/rescan', methods=['POST'])
def rescan_library():
    threading.Thread(target=scan_library, daemon=True).start()
    return jsonify({"result": "rescan started"})

@app.route('/api/library')
def get_library():
    query = request.args.get('q', '').lower()
    folder = request.args.get('path', '').strip('/')

    if query:
        res = [f for f in library_cache if query in f['title'].lower() or query in f['artist'].lower() or query in f['filename'].lower()]
    elif folder:
        res = [f for f in library_cache if f['folder'] == folder]
    else:
        res = [f for f in library_cache if f['folder'] == '']

    # Compute subfolders visible from `folder`
    prefix = folder + '/' if folder else ''
    seen = set()
    subfolders = []
    for f in library_cache:
        d = f['folder']
        if not d:
            continue
        if folder:
            if not d.startswith(prefix):
                continue
            rest = d[len(prefix):]
        else:
            rest = d
        top = rest.split('/')[0]
        if top and top not in seen:
            seen.add(top)
            full = prefix + top if prefix else top
            count = sum(1 for x in library_cache if x['folder'].startswith(full))
            subfolders.append({"name": top, "path": full, "count": count})
    subfolders.sort(key=lambda x: x['name'].lower())

    return jsonify({"library": res[:200], "folders": subfolders, "current_path": folder})

def _norm_album(a):
    a = (a or '').strip()
    if not a or a.lower() in ('<unknown>', 'unknown', 'unknown album'):
        return 'Unknown Album'
    return a

def _norm_artist(a):
    a = (a or '').strip()
    if not a or a.upper() == 'UNKNOWN':
        return 'Unknown Artist'
    return a

@app.route('/api/library/tree')
def get_library_tree():
    """Artist -> Album -> Tracks, grouped from the mutagen scan (durations included).

    This is the source for the three-pane librarian. Built from library_cache
    (not artist_library.json) so durations stay consistent with what is on disk
    and auto-advance timing keeps working.
    """
    artists = {}
    for f in library_cache:
        art = _norm_artist(f.get('artist'))
        alb = _norm_album(f.get('album'))
        a = artists.setdefault(art, {})
        a.setdefault(alb, []).append({
            "title": f.get('title') or f.get('filename'),
            "path": f['path'],
            "filename": f.get('filename', ''),
            "artist": art,
            "album": alb,
            "duration": f.get('duration', 0),
            "duration_display": f.get('duration_display', '0:00'),
        })

    out = []
    for art in sorted(artists, key=lambda x: x.lower()):
        albums = []
        total = 0
        for alb in sorted(artists[art], key=lambda x: x.lower()):
            # Sort tracks by filename so leading track numbers ("08 Dirge.mp3")
            # preserve album running order.
            tracks = sorted(artists[art][alb], key=lambda t: t['filename'].lower())
            total += len(tracks)
            albums.append({"name": alb, "track_count": len(tracks), "tracks": tracks})
        out.append({"name": art, "track_count": total, "album_count": len(albums), "albums": albums})

    return jsonify({"artists": out, "total_artists": len(out),
                    "total_tracks": sum(a['track_count'] for a in out)})

@app.route('/api/music/upload', methods=['POST'])
def music_upload():
    if 'file' not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files['file']
    if not f.filename or not allowed_file(f.filename):
        return jsonify({"error": "invalid file type"}), 400
    filename = secure_filename(f.filename)
    path = os.path.join(MUSIC_DIR, filename)
    f.save(path)
    # Incremental cache update: scan only the new file instead of walking the
    # whole library — keeps the 47,900-hour disk idle and makes the track
    # available in the librarian immediately.
    global library_cache
    info = _read_file_info(path, filename, '')
    merged = [x for x in library_cache if x['path'] != path]
    merged.append(info)
    library_cache = sorted(merged, key=lambda x: x['title'].lower())
    return jsonify({"result": "success", "filename": filename, "track": info})

@app.route('/api/music/delete', methods=['POST'])
def music_delete():
    """Delete one file from the library. Single-file only by design —
    bulk cleanups happen over SSH."""
    global library_cache
    path = (request.json.get('path') or '').strip()
    if not path:
        return jsonify({"error": "no path"}), 400

    # Containment: resolve symlinks/.. and require the file to live in MUSIC_DIR
    real = os.path.realpath(path)
    music_root = os.path.realpath(MUSIC_DIR)
    if not real.startswith(music_root + os.sep):
        return jsonify({"error": "path outside music dir"}), 400
    if not os.path.isfile(real):
        return jsonify({"error": "not found"}), 404

    with engine.lock:
        # Refuse to delete the track that is on air right now
        if engine.state == "playing" and 0 <= engine.current_index < len(engine.tracks):
            cur = engine.tracks[engine.current_index]
            if os.path.realpath(cur.uri) == real:
                return jsonify({"error": "track is currently playing — stop or skip first"}), 409

        os.remove(real)

        # Prune dead entries from the queue, keeping current_index pointing
        # at the same track
        cur_track = engine.tracks[engine.current_index] if 0 <= engine.current_index < len(engine.tracks) else None
        kept = [t for t in engine.tracks
                if not (t.type == "file" and os.path.realpath(t.uri) == real)]
        if len(kept) != len(engine.tracks):
            engine.tracks = kept
            engine.current_index = kept.index(cur_track) if cur_track in kept else -1
            engine.save()

    library_cache = [x for x in library_cache if x['path'] != real]
    return jsonify({"result": "success"})

@app.route('/api/music/stop', methods=['POST'])
def music_stop():
    liq("audio.bus emergency 0.0")
    return jsonify({"result": "success"})

@app.route('/api/music/skip', methods=['POST'])
def music_skip():
    liq("emergency_queue.skip")
    return jsonify({"result": "success"})

@app.route('/api/playlists')
def get_playlists():
    try:
        files = [f.replace('.json', '') for f in os.listdir(PLAYLISTS_DIR)
                 if f.endswith('.json') and not f.startswith('_')]
        return jsonify({"playlists": sorted(files)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlists/<name>')
def get_playlist(name):
    path = os.path.join(PLAYLISTS_DIR, f"{secure_filename(name)}.json")
    if not os.path.exists(path):
        return jsonify({"error": "playlist not found"}), 404
    try:
        with open(path, 'r') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlists/<name>', methods=['POST'])
def save_playlist(name):
    items = request.json.get('items', [])
    path = os.path.join(PLAYLISTS_DIR, f"{secure_filename(name)}.json")
    try:
        with open(path, 'w') as f:
            json.dump(items, f)
        if secure_filename(name) == EMERGENCY_PLAYLIST_NAME:
            _sync_emergency_m3u()
        return jsonify({"result": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlists/<name>', methods=['DELETE'])
def delete_playlist(name):
    path = os.path.join(PLAYLISTS_DIR, f"{secure_filename(name)}.json")
    if os.path.exists(path):
        os.remove(path)
        return jsonify({"result": "success"})
    return jsonify({"error": "not found"}), 404

@app.route('/api/stream/url', methods=['POST'])
def stream_url():
    """Update the URL for the Stream Selector (live) source and restart it."""
    url   = request.json.get('url', '').strip()
    label = request.json.get('label', 'UNKNOWN').strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    if not url.startswith(('http://', 'https://')):
        return jsonify({"error": "invalid url scheme"}), 400
    # Strip control characters — a newline in either field would inject a second
    # telnet command into the Liquidsoap session.
    url   = url.replace('\n', '').replace('\r', '')[:512]
    label = label.replace('\n', '').replace('\r', '')[:64]

    # We use 'live' namespace in telnet because Liquidsoap ID for stream_src is 'live'
    liq_multi([
        f"audio.stream_label {label}",
        f"live.url {url}",
        "live.stop",
        "live.start"
    ])
    return jsonify({"result": "success"})

@app.route('/api/health')
def health():
    # Upstream: TCP check to audio.wavefarm.org:443
    upstream_ok = False
    try:
        with socket.create_connection(('audio.wavefarm.org', 443), timeout=2):
            upstream_ok = True
    except Exception:
        pass

    # ALSA: proxy for "Liquidsoap is alive" — telnet reachable
    alsa_ok = False
    try:
        result = liq("radio.selected")
        alsa_ok = not result.startswith("Error")
    except Exception:
        pass

    return jsonify({"upstream": upstream_ok, "alsa": alsa_ok})

STATIC_STREAMS = [
    {"label": "FMMONITOR", "url": "https://audio.wavefarm.org/fmmonitor.mp3"},
]

@app.route('/api/wavefarm/streams')
def wavefarm_streams():
    prom_url = f'{PROMETHEUS_URL}/api/v1/query?query=wavefarm_stream_loudness_lufs{{type="short_term"}}'
    lufs_by_mount = {}
    try:
        with urllib.request.urlopen(prom_url, timeout=2) as r:
            data = json.loads(r.read())
        for r in data.get('data', {}).get('result', []):
            m = r['metric'].get('mount')
            if m:
                lufs_by_mount[m.replace('.mp3', '')] = round(float(r['value'][1]), 1)
    except Exception:
        pass

    seen = set()
    streams = []

    # Prometheus-discovered streams
    for mount, lufs in lufs_by_mount.items():
        clean_mount = mount if mount.endswith('.mp3') else f"{mount}.mp3"
        url = f"https://audio.wavefarm.org/{clean_mount}"
        key = mount.lower().replace('.mp3', '')
        seen.add(key)
        streams.append({"label": mount.upper(), "url": url, "lufs": lufs})

    # Static pinned streams (always shown, LUFS injected if available)
    for s in STATIC_STREAMS:
        key = s["label"].lower()
        if key not in seen:
            entry = dict(s)
            entry["lufs"] = lufs_by_mount.get(key)
            streams.append(entry)

    return jsonify(sorted(streams, key=lambda x: x['label']))

@app.route('/api/system/health')
def system_health():
    """Disk health guard — SMART status, filesystem read-only check, disk usage."""
    warnings = []
    smart_ok = True
    smart_raw = None

    # 1. SMART attributes via smartctl
    try:
        result = subprocess.run(
            ['sudo', 'smartctl', '-A', '/dev/sda'],
            capture_output=True, text=True, timeout=5
        )
        smart_raw = result.stdout.strip() or result.stderr.strip()
        # smartctl exit code bit 0 = command-line parse error, bit 1 = open device error
        # bit 2 = SMART command failed, bit 3 = disk failing
        # bits 5/6 = prefail/past threshold attributes
        exit_code = result.returncode
        if exit_code & 0b00001000:  # bit 3: disk failing NOW
            smart_ok = False
            warnings.append("SMART: disk failing NOW")
        elif exit_code & 0b00100000:  # bit 5: prefail attribute exceeded threshold
            smart_ok = False
            warnings.append("SMART: prefail threshold exceeded")
        elif exit_code & 0b01000000:  # bit 6: past threshold in the past
            warnings.append("SMART: attribute exceeded threshold in the past")
        elif exit_code & 0b00000110:  # bits 1+2: couldn't read SMART
            smart_ok = False
            smart_raw = "smartctl could not read SMART data"
            warnings.append("SMART: could not read disk data")
    except FileNotFoundError:
        smart_raw = None  # smartctl not installed — not an error
    except PermissionError:
        smart_raw = "smartctl requires elevated privileges"
    except subprocess.TimeoutExpired:
        smart_raw = "smartctl timed out"
        warnings.append("SMART: timed out")
    except Exception as e:
        smart_raw = f"smartctl error: {e}"

    # 2. Read-only filesystem check — attempt a temp write
    read_only = False
    try:
        test_path = '/tmp/.wgxc_rw_check'
        with open(test_path, 'w') as f:
            f.write('ok')
        os.remove(test_path)
    except OSError:
        read_only = True
        warnings.append("Filesystem is READ-ONLY")

    # 3. Disk usage
    disk_usage_pct = None
    try:
        usage = shutil.disk_usage('/')
        disk_usage_pct = round(usage.used / usage.total * 100, 1)
        if disk_usage_pct >= 95:
            warnings.append(f"Disk {disk_usage_pct}% full — critical")
        elif disk_usage_pct >= 85:
            warnings.append(f"Disk {disk_usage_pct}% full — warning")
    except Exception as e:
        warnings.append(f"Disk usage check failed: {e}")

    warning_str = "; ".join(warnings) if warnings else None

    return jsonify({
        "disk_usage_pct": disk_usage_pct,
        "read_only": read_only,
        "smart_ok": smart_ok,
        "smart_raw": smart_raw,
        "warning": warning_str
    })

@app.route('/api/system/info')
def system_info():
    """Live host vitals for the HUD — uptime, memory pressure, CPU temp.

    All reads are local /proc and /sys files: sub-millisecond, zero external
    dependency. Deliberately does NOT touch Prometheus so the HUD keeps working
    even when the monitor box (10.50.0.7) is down. Every field degrades to None
    on failure rather than raising — this is advisory display, never critical.
    """
    info = {"uptime_seconds": None, "mem_used_pct": None, "temp_c": None}

    # Uptime — first field of /proc/uptime is seconds since boot
    try:
        with open('/proc/uptime') as f:
            info["uptime_seconds"] = int(float(f.read().split()[0]))
    except Exception:
        pass

    # Memory pressure — MemAvailable vs MemTotal (kernel's own "free-ish" number)
    try:
        meminfo = {}
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    meminfo[parts[0]] = int(parts[1].strip().split()[0])  # kB
        total = meminfo.get('MemTotal')
        avail = meminfo.get('MemAvailable')
        if total and avail is not None:
            info["mem_used_pct"] = round((total - avail) / total * 100, 1)
    except Exception:
        pass

    # CPU temperature — thermal_zone0 reports millidegrees C (x86_pkg_temp)
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            info["temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        pass

    return jsonify(info)

@app.route('/api/streams')
def streams():
    def lufs(mount):
        expr = f'wavefarm_stream_loudness_lufs{{mount="{mount}",server="audio.wavefarm.org",type="short_term"}}'
        url  = f'{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(expr)}'
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                data = json.loads(r.read())
            res = data.get('data', {}).get('result', [])
            if res: return round(float(res[0]['value'][1]), 1)
        except: pass
        return None
    return jsonify({'automation': lufs('automation'), 'fmmonitor': lufs('fmmonitor')})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
