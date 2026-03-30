import socket
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox, Menu, simpledialog, filedialog
import json
from datetime import datetime
import base64
import io
import time
import struct

try:
    from PIL import Image, ImageTk, ImageOps, ImageDraw
except ImportError:
    messagebox.showerror("Missing Library", "Please run 'pip install pillow' in your terminal.")
    exit()

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False

# ── STYLE ─────────────────────────────────────────────────────────────────
BG_RAIL       = "#0B0C0D"
BG_SIDEBAR    = "#111214"
BG_CHAT       = "#18191C"
BG_INPUT      = "#1E1F22"
TEXT_WHITE    = "#FFFFFF"
TEXT_MUTED    = "#80848E"
ACCENT_BLUE   = "#5865F2"
ACCENT_RED    = "#DA373C"
ACCENT_GREEN  = "#248046"
ACCENT_PURPLE = "#9146FF"
ACCENT_ORANGE = "#FAA61A"
FONT_BOLD     = ("Segoe UI", 11, "bold")
FONT_REG      = ("Segoe UI", 9)
FONT_TIME     = ("Segoe UI", 8)

HOST = "127.0.0.1"
PORT = 5555

# ── PFP CACHE — never regenerate the same avatar twice ────────────────────
_pfp_cache: dict = {}   # (username, size) -> ImageTk.PhotoImage

def _make_pfp(username, user_pfps, size=(40,40)):
    key = (username, size)
    if key in _pfp_cache:
        return _pfp_cache[key]
    b64_data = user_pfps.get(username, "")
    if not b64_data:
        colors = ["#5865F2","#DA373C","#248046","#9146FF","#FAA61A","#00B0F4","#EB459E"]
        color  = colors[hash(username) % len(colors)]
        big    = (size[0]*4, size[1]*4)
        img    = Image.new('RGB', big, color=color)
        ImageDraw.Draw(img).text((big[0]//2, big[1]//2),
                                  username[0].upper() if username else "?",
                                  fill="white", anchor="mm")
    else:
        try:   img = Image.open(io.BytesIO(base64.b64decode(b64_data)))
        except: img = Image.new('RGB', size, color=ACCENT_BLUE)
    scale = 4
    big   = (size[0]*scale, size[1]*scale)
    img   = img.convert("RGBA")
    img   = ImageOps.fit(img, big, Image.Resampling.LANCZOS)
    mask  = Image.new('L', big, 0)
    ImageDraw.Draw(mask).ellipse((scale, scale, big[0]-scale, big[1]-scale), fill=255)
    out   = Image.new('RGBA', big, (0,0,0,0))
    out.paste(img, (0,0), mask=mask)
    pil   = out.resize(size, Image.Resampling.LANCZOS)
    tk_img = ImageTk.PhotoImage(pil)
    _pfp_cache[key] = tk_img
    return tk_img

VIEW_HOME   = "home"
VIEW_SERVER = "server"
VIEW_DM     = "dm"

TYPING_TIMEOUT = 3000

USER_COLORS = [
    "#5865F2","#EB459E","#FAA61A","#00B0F4",
    "#43B581","#F47FFF","#FF7043","#26C6DA",
    "#AB47BC","#66BB6A","#FF8A65","#42A5F5",
]

def user_color(username):
    return USER_COLORS[hash(username) % len(USER_COLORS)]

import re, webbrowser
URL_RE = re.compile(r"https?://\S+")

EMOJI_GRID = [
    "😀","😂","😍","😎","😭","😤","😢","😡",
    "🔥","❤️","👍","👎","😮","🎉","💀","🤔",
    "😏","🥺","😴","🤣","😅","🙄","😬","🥳",
    "👀","💯","🙏","✨","💪","🎮","🍕","💸",
]

# ── AUDIO CONFIG ───────────────────────────────────────────────────────────
AUDIO_RATE     = 48000   # 48kHz — CD quality, much cleaner
AUDIO_CHUNK    = 2048    # larger buffer = smoother playback, less crackling
AUDIO_FORMAT   = pyaudio.paInt16 if PYAUDIO_AVAILABLE else None
AUDIO_CHANNELS = 1


class VoiceEngine:
    """Handles mic capture → UDP send, and UDP receive → speaker."""

    def __init__(self, server_host, udp_port, username):
        self.server_host = server_host
        self.udp_port    = udp_port
        self.username    = username
        self.active      = False
        self.muted       = False
        self.deafened    = False
        self._pa         = None
        self._in_stream  = None
        self._out_stream = None
        self._udp_sock   = None
        self._send_thread   = None
        self._recv_thread   = None
        self.speaking_users = set()   # users currently heard
        self.on_speaking    = None    # callback(username, bool)

    def start(self):
        if not PYAUDIO_AVAILABLE:
            return False
        try:
            import queue
            self._audio_queue = queue.Queue(maxsize=20)
            self._pa = pyaudio.PyAudio()
            # Separate streams — no duplex collision
            self._in_stream = self._pa.open(
                format=AUDIO_FORMAT, channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE, input=True,
                frames_per_buffer=AUDIO_CHUNK
            )
            self._out_stream = self._pa.open(
                format=AUDIO_FORMAT, channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE, output=True,
                frames_per_buffer=AUDIO_CHUNK
            )
            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_sock.settimeout(0.5)
            self.active = True
            self._send_thread  = threading.Thread(target=self._send_loop,  daemon=True)
            self._recv_thread  = threading.Thread(target=self._recv_loop,  daemon=True)
            self._play_thread  = threading.Thread(target=self._play_loop,  daemon=True)
            self._send_thread.start()
            self._recv_thread.start()
            self._play_thread.start()
            return True
        except Exception as e:
            print(f"[Voice] Start error: {e}")
            self.stop()
            return False

    def stop(self):
        self.active = False
        for stream in [self._in_stream, self._out_stream]:
            try:
                if stream:
                    stream.stop_stream()
                    stream.close()
            except: pass
        try:
            if self._pa: self._pa.terminate()
        except: pass
        try:
            if self._udp_sock: self._udp_sock.close()
        except: pass
        self._pa = self._in_stream = self._out_stream = self._udp_sock = None

    def _send_loop(self):
        name_bytes = self.username.encode("utf-8")
        prefix     = bytes([len(name_bytes)]) + name_bytes
        sent_count = 0
        while self.active:
            try:
                if self.muted:
                    time.sleep(0.05)
                    continue
                audio  = self._in_stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                packet = prefix + audio
                self._udp_sock.sendto(packet, (self.server_host, self.udp_port))
                sent_count += 1
                if sent_count % 50 == 0:
                    print(f"[Voice] Sent {sent_count} packets to {self.server_host}:{self.udp_port}")
            except Exception as e:
                print(f"[Voice SEND ERROR] {e}")
                time.sleep(0.01)

    def _recv_loop(self):
        import queue
        recv_count = 0
        while self.active:
            try:
                data, addr = self._udp_sock.recvfrom(131072)
                if len(data) < 2:
                    continue
                ulen   = data[0]
                sender = data[1:1+ulen].decode("utf-8", errors="ignore")
                audio  = data[1+ulen:]
                recv_count += 1
                if recv_count % 50 == 0:
                    print(f"[Voice] Received {recv_count} packets from {sender}")
                if not self.deafened:
                    try:
                        self._audio_queue.put_nowait(audio)
                    except: pass  # drop if queue full — prevents lag buildup
                if self.on_speaking:
                    self.on_speaking(sender, True)
            except socket.timeout:
                pass
            except Exception as e:
                print(f"[Voice RECV ERROR] {e}")
                time.sleep(0.01)

    def _play_loop(self):
        """Dedicated thread for audio playback — decoupled from network recv."""
        import queue
        while self.active:
            try:
                audio = self._audio_queue.get(timeout=0.5)
                if self._out_stream and not self.deafened:
                    self._out_stream.write(audio)
            except: pass

    def set_muted(self, muted):
        self.muted = muted

    def set_deafened(self, deafened):
        self.deafened = deafened


class ChatClient:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Chat_Room")
        self.root.geometry("1200x720")
        self.root.configure(bg=BG_CHAT)
        self.root.minsize(900, 600)

        self.sock             = None
        self.username         = ""
        self.server           = "Main Server"
        self.channel          = "general"
        self.servers_data     = {}
        self.available_servers = []
        self.chat_history     = {}
        self.unread_counts    = {}
        self.friends          = set()
        self.pending_requests = set()
        self.blocked          = set()
        self.dm_unreads       = {}
        self.all_users        = []
        self.user_pfps        = {}
        self.history_images   = []
        self._rail_images     = []
        self.current_view     = VIEW_HOME
        self.current_dm_user  = None
        self._settings_visible = False
        self._settings_frame  = None
        self.chat_panel       = None
        self._settings_btn    = None
        self._typing_labels   = {}
        self._typing_who      = {}
        self._typing_timers   = {}
        self._my_typing       = False
        self._my_typing_timer = None
        self._ctx_msg_id      = None
        self._ctx_key         = None
        self._last_dm_read    = {}
        self.online_users     = set()
        self.pinned_messages  = {}
        self._search_active   = False
        self._search_results  = []

        # Render cache
        self._history_len     = {}   # key -> number of messages last rendered

        # Voice state
        self.voice_engine     = None
        self.udp_port         = 5556
        self.voice_state      = {}    # {server: {channel: [usernames]}}
        self.current_voice    = None  # (server, channel) or None
        self._voice_muted     = False
        self._voice_deafened  = False
        self._voice_bar       = None  # bottom voice status bar widget

        # Image cache — avoid re-processing same pfp on every message render
        self._pfp_cache       = {}   # (username, size) -> PIL Image

        self.show_login()

    # ── UTILS ─────────────────────────────────────────────────────────────

    def clear(self):
        for w in self.root.winfo_children(): w.destroy()

    def get_pil_pfp(self, username, size=(40, 40)):
        cache_key = (username, size, self.user_pfps.get(username,"")[:20])
        if cache_key in self._pfp_cache:
            return self._pfp_cache[cache_key]
        b64_data = self.user_pfps.get(username, "")
        if not b64_data:
            colors = ["#5865F2","#DA373C","#248046","#9146FF","#FAA61A","#00B0F4","#EB459E"]
            color  = colors[hash(username) % len(colors)]
            big    = (size[0]*4, size[1]*4)
            img    = Image.new('RGB', big, color=color)
            ImageDraw.Draw(img).text((big[0]//2, big[1]//2),
                                     username[0].upper() if username else "?",
                                     fill="white", anchor="mm")
        else:
            try:   img = Image.open(io.BytesIO(base64.b64decode(b64_data)))
            except: img = Image.new('RGB', size, color=ACCENT_BLUE)
        scale = 4
        big   = (size[0]*scale, size[1]*scale)
        img   = img.convert("RGBA")
        img   = ImageOps.fit(img, big, Image.Resampling.LANCZOS)
        mask  = Image.new('L', big, 0)
        ImageDraw.Draw(mask).ellipse((scale, scale, big[0]-scale, big[1]-scale), fill=255)
        out   = Image.new('RGBA', big, (0,0,0,0))
        out.paste(img, (0,0), mask=mask)
        # Store as ImageTk.PhotoImage so render_message never calls ImageTk.PhotoImage again
        tk_img = ImageTk.PhotoImage(out.resize(size, Image.Resampling.LANCZOS))
        if len(self._pfp_cache) > 300:
            self._pfp_cache.pop(next(iter(self._pfp_cache)))
        self._pfp_cache[cache_key] = tk_img
        return tk_img

    def _now(self):
        return datetime.now().strftime("%H:%M")

    def _current_key(self):
        if self.current_view == VIEW_DM:
            return f"dm:{self.current_dm_user}"
        return f"{self.server}:{self.channel}"

    def _safe_send(self, payload):
        try: self.sock.send((json.dumps(payload) + "\n").encode())
        except: pass

    # ── VOICE CHANNEL METHODS ─────────────────────────────────────────────

    def join_voice(self, srv, chan):
        """Join a voice channel."""
        if not PYAUDIO_AVAILABLE:
            self._toast("🎤 Voice Unavailable",
                        "Install PyAudio: pip install pyaudio", ACCENT_RED)
            return

        # Leave current voice channel first
        if self.current_voice:
            self.leave_voice(silent=True)

        self._safe_send({"type": "voice_join", "server": srv, "channel": chan})
        self.current_voice  = (srv, chan)
        self._voice_muted   = False
        self._voice_deafened = False

        # Start audio engine
        self.voice_engine = VoiceEngine(HOST, self.udp_port, self.username)
        self.voice_engine.on_speaking = self._on_peer_speaking
        if not self.voice_engine.start():
            self._toast("🎤 Mic Error", "Could not open microphone", ACCENT_RED)
            self.voice_engine = None
            self.current_voice = None
            return

        self._build_voice_bar()
        self._refresh_voice_ui()
        self._toast("🎤 Joined Voice", f"{srv} › {chan}", ACCENT_GREEN)

    def leave_voice(self, silent=False):
        """Leave the current voice channel."""
        if self.voice_engine:
            self.voice_engine.stop()
            self.voice_engine = None
        if self.current_voice:
            self._safe_send({"type": "voice_leave"})
            self.current_voice = None
        self._remove_voice_bar()
        self._refresh_voice_ui()
        if not silent:
            self._toast("🔇 Left Voice", "Disconnected from voice", TEXT_MUTED)

    def _on_peer_speaking(self, username, speaking):
        """Called when audio received from a peer."""
        pass  # Could animate speaking indicator here

    def _build_voice_bar(self):
        """Green bottom bar showing current voice channel + controls."""
        self._remove_voice_bar()
        if not self.current_voice:
            return
        srv, chan = self.current_voice
        bar = tk.Frame(self.left_panel, bg="#1a3a2a", height=70)
        bar.pack(side="bottom", fill="x", before=self._user_card_frame)
        bar.pack_propagate(False)
        self._voice_bar = bar

        top = tk.Frame(bar, bg="#1a3a2a"); top.pack(fill="x", padx=10, pady=(8,2))
        tk.Label(top, text="🟢  Voice Connected", fg="#3BA55D", bg="#1a3a2a",
                 font=("Segoe UI",8,"bold")).pack(side="left")
        tk.Button(top, text="✕", bg="#1a3a2a", fg=ACCENT_RED,
                  font=("Segoe UI",9), relief="flat", borderwidth=0,
                  cursor="hand2", command=self.leave_voice).pack(side="right")

        mid = tk.Frame(bar, bg="#1a3a2a"); mid.pack(fill="x", padx=10)
        tk.Label(mid, text=f"🔊  {chan}  in  {srv}", fg=TEXT_MUTED,
                 bg="#1a3a2a", font=("Segoe UI",7)).pack(side="left")

        ctrl = tk.Frame(bar, bg="#1a3a2a"); ctrl.pack(fill="x", padx=10, pady=(4,6))

        self._mute_btn = tk.Button(ctrl,
            text="🎤 Muted" if self._voice_muted else "🎤 Live",
            bg="#2a4a3a" if not self._voice_muted else ACCENT_RED,
            fg=TEXT_WHITE, font=("Segoe UI",8,"bold"),
            relief="flat", borderwidth=0, padx=8, cursor="hand2",
            command=self._toggle_mute)
        self._mute_btn.pack(side="left", ipady=4, padx=(0,6))

        self._deaf_btn = tk.Button(ctrl,
            text="🔇 Deaf" if self._voice_deafened else "🔊 Hear",
            bg=ACCENT_RED if self._voice_deafened else "#2a4a3a",
            fg=TEXT_WHITE, font=("Segoe UI",8,"bold"),
            relief="flat", borderwidth=0, padx=8, cursor="hand2",
            command=self._toggle_deafen)
        self._deaf_btn.pack(side="left", ipady=4)

    def _remove_voice_bar(self):
        if self._voice_bar:
            try:
                self._voice_bar.destroy()
            except: pass
            self._voice_bar = None

    def _toggle_mute(self):
        self._voice_muted = not self._voice_muted
        if self.voice_engine:
            self.voice_engine.set_muted(self._voice_muted)
        if self._mute_btn:
            try:
                self._mute_btn.config(
                    text="🎤 Muted" if self._voice_muted else "🎤 Live",
                    bg=ACCENT_RED if self._voice_muted else "#2a4a3a"
                )
            except: pass

    def _toggle_deafen(self):
        self._voice_deafened = not self._voice_deafened
        if self.voice_engine:
            self.voice_engine.set_deafened(self._voice_deafened)
        if self._deaf_btn:
            try:
                self._deaf_btn.config(
                    text="🔇 Deaf" if self._voice_deafened else "🔊 Hear",
                    bg=ACCENT_RED if self._voice_deafened else "#2a4a3a"
                )
            except: pass

    def _refresh_voice_ui(self):
        """Rebuild channel list to show updated voice occupancy."""
        if self.current_view == VIEW_SERVER:
            try:
                self._build_channel_list_panel()
            except: pass

    # ── TOAST ─────────────────────────────────────────────────────────────

    def _toast(self, title, message, color=ACCENT_BLUE):
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg=BG_INPUT)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = 320, 72
        toast.geometry(f"{w}x{h}+{sw}+{sh-h-60}")
        toast.attributes("-alpha", 0.0)
        tk.Frame(toast, bg=color, width=4).pack(side="left", fill="y")
        body = tk.Frame(toast, bg=BG_INPUT)
        body.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        tk.Label(body, text=title, fg=TEXT_WHITE, bg=BG_INPUT,
                 font=("Segoe UI",9,"bold")).pack(anchor="w")
        tk.Label(body, text=message[:60], fg=TEXT_MUTED, bg=BG_INPUT,
                 font=("Segoe UI",8)).pack(anchor="w")
        tk.Button(toast, text="✕", bg=BG_INPUT, fg=TEXT_MUTED, relief="flat",
                  borderwidth=0, cursor="hand2",
                  command=toast.destroy).pack(side="right", padx=6)
        target_x = sw - w - 16
        target_y = sh - h - 60
        steps = 12
        def slide_in(step=0):
            if not toast.winfo_exists(): return
            t = step / steps; ease = 1 - (1-t)**3
            cx = int(sw + (target_x - sw) * ease)
            toast.geometry(f"{w}x{h}+{cx}+{target_y}")
            toast.attributes("-alpha", min(ease, 1.0))
            if step < steps: toast.after(16, lambda: slide_in(step+1))
        slide_in()
        def fade_out(alpha=1.0):
            if not toast.winfo_exists(): return
            if alpha <= 0: toast.destroy(); return
            toast.attributes("-alpha", alpha)
            toast.after(30, lambda: fade_out(alpha - 0.08))
        toast.after(3500, fade_out)

    # ── LOGIN ──────────────────────────────────────────────────────────────

    def show_login(self):
        self.clear()
        self.root.configure(bg="#0B0C0D")
        left = tk.Frame(self.root, bg=ACCENT_BLUE, width=320)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="💬", bg=ACCENT_BLUE, font=("Segoe UI",52)).place(relx=0.5, rely=0.38, anchor="center")
        tk.Label(left, text="CHAT_ROOM", bg=ACCENT_BLUE, fg=TEXT_WHITE,
                 font=("Segoe UI",22,"bold")).place(relx=0.5, rely=0.52, anchor="center")
        tk.Label(left, text="Connect with friends", bg=ACCENT_BLUE, fg="#c0c8ff",
                 font=("Segoe UI",10)).place(relx=0.5, rely=0.59, anchor="center")
        right = tk.Frame(self.root, bg="#0B0C0D")
        right.pack(side="left", fill="both", expand=True)
        form = tk.Frame(right, bg="#0B0C0D")
        form.place(relx=0.5, rely=0.5, anchor="center")
        self._auth_mode = tk.StringVar(value="login")
        tab_row = tk.Frame(form, bg="#0B0C0D"); tab_row.pack(fill="x", pady=(0,24))
        auth_tab_btns = []
        def make_auth_tab(label, mode):
            btn = tk.Button(tab_row, text=label, bg="#0B0C0D", fg=TEXT_MUTED,
                            font=("Segoe UI",11,"bold"), relief="flat", borderwidth=0,
                            cursor="hand2", padx=10)
            btn.pack(side="left", padx=(0,8))
            auth_tab_btns.append(btn)
            def activate():
                self._auth_mode.set(mode)
                for b in auth_tab_btns: b.config(fg=TEXT_MUTED, font=("Segoe UI",11,"bold"))
                btn.config(fg=TEXT_WHITE, font=("Segoe UI",11,"bold"))
                tk.Frame(tab_row, bg=ACCENT_BLUE, height=2).place(in_=btn, relx=0, rely=1.0, relwidth=1.0)
                refresh_form()
            btn.config(command=activate)
            return btn, activate
        login_btn, activate_login   = make_auth_tab("Login",   "login")
        signup_btn, activate_signup = make_auth_tab("Sign Up", "signup")
        fields_frame = tk.Frame(form, bg="#0B0C0D"); fields_frame.pack()
        def field(parent, placeholder, show=None):
            tk.Label(parent, text=placeholder, fg=TEXT_MUTED, bg="#0B0C0D",
                     font=("Segoe UI",8,"bold")).pack(anchor="w", pady=(10,3))
            f = tk.Frame(parent, bg="#1a1a20", highlightthickness=1, highlightbackground="#333")
            f.pack(fill="x", pady=(0,4))
            e = tk.Entry(f, bg="#1a1a20", fg=TEXT_WHITE, insertbackground="white",
                         font=("Segoe UI",10), width=34, relief="flat", borderwidth=0,
                         show=show if show else "")
            e.pack(ipady=12, padx=14)
            return e
        self._login_entries = {}
        def refresh_form():
            for w in fields_frame.winfo_children(): w.destroy()
            self._login_entries = {}
            mode = self._auth_mode.get()
            self._login_entries["user"] = field(fields_frame, "Username")
            self._login_entries["pass"] = field(fields_frame, "Password", show="*")
            if mode == "signup":
                self._login_entries["confirm"] = field(fields_frame, "Confirm Password", show="*")
                self._login_entries["code"]    = field(fields_frame, "Signup Code", show="*")
            self._auth_error   = tk.Label(fields_frame, text="", fg=ACCENT_RED,   bg="#0B0C0D", font=("Segoe UI",8), wraplength=320)
            self._auth_error.pack(anchor="w", pady=(4,0))
            self._auth_success = tk.Label(fields_frame, text="", fg=ACCENT_GREEN, bg="#0B0C0D", font=("Segoe UI",8))
            self._auth_success.pack(anchor="w")
            btn_text  = "LOGIN" if mode == "login" else "CREATE ACCOUNT"
            btn_color = ACCENT_BLUE if mode == "login" else ACCENT_GREEN
            def run_action(m=mode):
                if m == "login": self.do_login()
                else:            self.do_signup()
            for e in self._login_entries.values():
                e.bind("<Return>", lambda ev, m=mode: run_action(m))
            tk.Button(fields_frame, text=btn_text, bg=btn_color, fg=TEXT_WHITE,
                      font=("Segoe UI",10,"bold"), relief="flat", borderwidth=0,
                      cursor="hand2", width=34, command=lambda m=mode: run_action(m)
                      ).pack(pady=(16,0), ipady=10)
        activate_login()

    def _get_field(self, key):
        e = self._login_entries.get(key)
        return e.get().strip() if e else ""

    def _recv_line(self):
        buf = b""
        while True:
            chunk = self.sock.recv(4096)
            if not chunk: break
            buf += chunk
            if b"\n" in buf:
                line, _ = buf.split(b"\n", 1)
                return line.decode("utf-8", errors="ignore")
        return buf.decode("utf-8", errors="ignore")

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((HOST, PORT))

    def do_signup(self):
        user = self._get_field("user"); pw = self._get_field("pass")
        conf = self._get_field("confirm"); code = self._get_field("code")
        if not user or not pw or not conf or not code:
            self._auth_error.config(text="All fields are required."); return
        if pw != conf:
            self._auth_error.config(text="Passwords do not match."); return
        if len(pw) < 4:
            self._auth_error.config(text="Password must be at least 4 characters."); return
        try:
            self.connect()
            self.sock.send((json.dumps({"type":"signup","username":user,"password":pw,"code":code}) + "\n").encode())
            res = json.loads(self._recv_line())
            self.sock.close()
            if res.get("type") == "error":
                self._auth_error.config(text=res.get("msg","Error"))
                self._auth_success.config(text="")
            else:
                self._auth_error.config(text="")
                self._auth_success.config(text="✓ Account created! You can now log in.")
        except Exception as e:
            self._auth_error.config(text=f"Connection failed: {e}")

    def do_login(self):
        user = self._get_field("user"); pw = self._get_field("pass")
        if not user or not pw:
            self._auth_error.config(text="Please enter your username and password."); return
        self._auth_error.config(text="")
        try: self.connect()
        except Exception as e:
            self._auth_error.config(text=f"Cannot connect to server: {e}"); return
        self.username = user
        self._safe_send({"type":"login","username":self.username,"password":pw})
        res = json.loads(self._recv_line())
        if res["type"] == "error":
            self._auth_error.config(text=res["msg"])
            self.sock.close(); return
        if "servers"   in res: self.servers_data = res["servers"]; self.available_servers = list(res["servers"])
        if "history"   in res:
            self.chat_history = {}
            for key, msgs in res["history"].items():
                self.chat_history[key] = [(m["from"],m["content"],m.get("time",""),m.get("id",""),m.get("image",""),m.get("edited",False)) for m in msgs]
        if "friends"   in res: self.friends          = set(res["friends"])
        if "pending"   in res: self.pending_requests = set(res["pending"])
        if "blocked"   in res: self.blocked          = set(res["blocked"])
        if "pfps"      in res: self.user_pfps        = res["pfps"]
        if "all_users" in res: self.all_users        = res["all_users"]
        if "voice_state" in res: self.voice_state    = res["voice_state"]
        if "udp_port"  in res: self.udp_port         = res["udp_port"]
        self.unread_counts = res.get("unreads",  {srv: 0 for srv in self.available_servers})
        self.dm_unreads    = res.get("dm_unreads", {})
        self.setup_ui()
        threading.Thread(target=self.receive, daemon=True).start()

    # ── MAIN LAYOUT ────────────────────────────────────────────────────────

    def setup_ui(self):
        self.clear()
        self._settings_visible = False
        self._settings_frame   = None
        self.chat_panel        = None
        self._voice_bar        = None
        self._user_card_frame  = None

        self.rail = tk.Frame(self.root, bg=BG_RAIL, width=72)
        self.rail.pack(side="left", fill="y")
        self.rail.pack_propagate(False)

        self.left_panel = tk.Frame(self.root, bg=BG_SIDEBAR, width=240)
        self.left_panel.pack(side="left", fill="y")
        self.left_panel.pack_propagate(False)

        self._main_area = tk.Frame(self.root, bg=BG_CHAT)
        self._main_area.pack(side="left", fill="both", expand=True)

        self._build_rail()
        self._show_home_view()

    # ── ANIMATION HELPERS ─────────────────────────────────────────────────

    def _animate_rail_icon(self, widget, to_square=True, step=0, steps=8, srv_name="", color=None):
        # Instant highlight - no per-frame animation
        if color is None: color = ACCENT_BLUE
        try:
            if not widget.winfo_exists(): return
            bg = color if to_square else "#2B2D31"
            pil = Image.new('RGBA', (176,176), (0,0,0,0))
            draw = ImageDraw.Draw(pil)
            radius = 28 if to_square else 88
            draw.rounded_rectangle([4,4,172,172], radius=radius, fill=bg)
            letter = widget._srv_letter if hasattr(widget, '_srv_letter') else '?'
            ImageDraw.Draw(pil).text((88,88), letter, fill="white", anchor="mm")
            tk_img = ImageTk.PhotoImage(pil.resize((44,44), Image.Resampling.LANCZOS))
            widget._hover_img = tk_img
            widget.config(image=tk_img)
        except: pass

    def _glow_btn(self, btn, normal_bg, hover_bg, normal_fg=None, hover_fg=None):
        nfg = normal_fg or TEXT_WHITE; hfg = hover_fg or TEXT_WHITE; steps = 8
        def lerp_hex(a, b, t):
            ar,ag,ab_ = int(a[1:3],16),int(a[3:5],16),int(a[5:7],16)
            br,bg_,bb = int(b[1:3],16),int(b[3:5],16),int(b[5:7],16)
            return f"#{int(ar+(br-ar)*t):02x}{int(ag+(bg_-ag)*t):02x}{int(ab_+(bb-ab_)*t):02x}"
        def _anim(frm, to, step=0):
            try:
                if not btn.winfo_exists(): return
            except: return
            t = step/steps; ease = t*(2-t)
            try: btn.config(bg=lerp_hex(frm, to, ease))
            except: pass
            if step < steps: btn.after(16, lambda: _anim(frm, to, step+1))
            else:
                try: btn.config(bg=to)
                except: pass
        btn.bind("<Enter>", lambda e: _anim(normal_bg, hover_bg))
        btn.bind("<Leave>", lambda e: _anim(hover_bg, normal_bg))

    def _animate_message_in(self, frame, step=0, steps=10):
        pass  # removed - was causing scroll lag

    def _fade_transition(self, build_fn):
        # Instant swap — no animation delay
        build_fn()
        try: self._main_area.config(bg=BG_CHAT)
        except: pass

    def _build_rail(self):
        for w in self.rail.winfo_children(): w.destroy()
        self._rail_images = []
        home_active = self.current_view == VIEW_HOME
        home_container = tk.Frame(self.rail, bg=BG_RAIL)
        home_container.pack(pady=(14,4), padx=10)
        home_lbl = tk.Label(home_container, text="🏠", bg=ACCENT_BLUE if home_active else "#2B2D31",
                            fg=TEXT_WHITE, font=("Segoe UI",18), width=3, cursor="hand2")
        home_lbl.pack()
        home_lbl.bind("<Button-1>", lambda e: self._show_home_view())
        home_container.bind("<Button-1>", lambda e: self._show_home_view())
        if not home_active: self._glow_btn(home_lbl, "#2B2D31", ACCENT_BLUE)
        total_dm = sum(self.dm_unreads.values())
        if total_dm > 0 and not home_active:
            tk.Label(home_container, text=str(total_dm), bg=ACCENT_RED, fg=TEXT_WHITE,
                     font=("Segoe UI",7,"bold"), padx=4, pady=1).place(relx=0.75, rely=0.0, anchor="ne")
        tk.Frame(self.rail, bg="#2B2D31", height=2).pack(fill="x", padx=16, pady=4)
        for srv in self.available_servers:
            has_unread = self.unread_counts.get(srv,0) > 0
            is_active  = (self.current_view == VIEW_SERVER and self.server == srv)
            # Cache rail icons — only regenerate if active state changed
            cache_key  = f"rail_{srv}_{is_active}"
            if cache_key not in _pfp_cache:
                pil = Image.new('RGBA', (176,176), (0,0,0,0))
                draw = ImageDraw.Draw(pil)
                draw.rounded_rectangle([4,4,172,172], radius=88,
                                       fill=ACCENT_BLUE if is_active else "#2B2D31")
                draw.text((88,88), srv[0].upper(), fill="white", anchor="mm")
                _pfp_cache[cache_key] = ImageTk.PhotoImage(pil.resize((44,44), Image.Resampling.LANCZOS))
            tk_img = _pfp_cache[cache_key]
            self._rail_images.append(tk_img)
            lbl = tk.Label(self.rail, image=tk_img, bg=BG_RAIL, cursor="hand2")
            lbl._srv_letter = srv[0].upper()
            lbl.pack(pady=4)
            lbl.bind("<Button-1>", lambda e, s=srv: self._switch_to_server(s))
            lbl.bind("<Button-3>", lambda e, s=srv: self._server_ctx_menu(e, s))
            def _rail_enter(e, w=lbl, active=is_active):
                if not active: self._animate_rail_icon(w, to_square=True)
            def _rail_leave(e, w=lbl, active=is_active):
                if not active: self._animate_rail_icon(w, to_square=False)
            lbl.bind("<Enter>", _rail_enter)
            lbl.bind("<Leave>", _rail_leave)
            if has_unread:
                tk.Label(self.rail, text=str(self.unread_counts[srv]),
                         bg=ACCENT_RED, fg=TEXT_WHITE, font=("Segoe UI",7,"bold"), padx=3).pack()
        add_lbl = tk.Label(self.rail, text="+", bg="#2B2D31", fg=ACCENT_GREEN,
                           font=("Segoe UI",20,"bold"), width=3, cursor="hand2")
        add_lbl.pack(pady=8, padx=10)
        add_lbl.bind("<Button-1>", lambda e: self._add_server_dialog())

    # ── HOME VIEW ──────────────────────────────────────────────────────────

    def _show_home_view(self):
        self.current_view    = VIEW_HOME
        self.current_dm_user = None
        self._build_rail()
        self._build_dm_list_panel()
        self._fade_transition(self._build_main_area_home)

    def _build_dm_list_panel(self):
        for w in self.left_panel.winfo_children(): w.destroy()
        tk.Label(self.left_panel, text="Direct Messages", fg=TEXT_WHITE,
                 bg=BG_SIDEBAR, font=("Segoe UI",11,"bold")).pack(anchor="w", padx=14, pady=(16,8))
        online_friends = [f for f in self.friends if f in self.online_users]
        if online_friends:
            tk.Label(self.left_panel, text=f"🟢  {len(online_friends)} friend{'s' if len(online_friends)!=1 else ''} online",
                     fg="#3BA55D", bg=BG_SIDEBAR, font=("Segoe UI",7)).pack(anchor="w", padx=14, pady=(0,6))
        add_row = tk.Frame(self.left_panel, bg=BG_INPUT, highlightthickness=1, highlightbackground="#333")
        add_row.pack(fill="x", padx=10, pady=(0,4))
        self._add_friend_entry = tk.Entry(add_row, bg=BG_INPUT, fg=TEXT_MUTED,
                                          insertbackground="white", font=FONT_REG, relief="flat", borderwidth=0)
        self._add_friend_entry.pack(side="left", fill="x", expand=True, ipady=7, padx=8)
        self._add_friend_entry.insert(0, "Find or add friend...")
        self._add_friend_entry.bind("<FocusIn>",    self._friend_entry_focus_in)
        self._add_friend_entry.bind("<FocusOut>",   self._friend_entry_focus_out)
        self._add_friend_entry.bind("<KeyRelease>", self._friend_entry_keyrelease)
        self._add_friend_entry.bind("<Return>", lambda e: self._send_friend_request_from_entry())
        tk.Button(add_row, text="＋", bg=BG_INPUT, fg=ACCENT_GREEN,
                  font=("Segoe UI",12,"bold"), relief="flat", borderwidth=0,
                  cursor="hand2", command=self._send_friend_request_from_entry).pack(side="right", padx=6)
        self._suggestions_frame = tk.Frame(self.left_panel, bg="#2B2D31")
        self._suggestions_frame.pack(fill="x", padx=10)
        if self.pending_requests:
            tk.Label(self.left_panel, text="PENDING", fg=TEXT_MUTED, bg=BG_SIDEBAR,
                     font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(6,2))
            for p in self.pending_requests:
                self._dm_list_row(self.left_panel, p, pending=True)
        tk.Label(self.left_panel, text="FRIENDS — DMs", fg=TEXT_MUTED, bg=BG_SIDEBAR,
                 font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(10,2))
        for f in sorted(self.friends):
            self._dm_list_row(self.left_panel, f, unread=self.dm_unreads.get(f,0))
        self._user_card_frame = tk.Frame(self.left_panel, bg=BG_SIDEBAR)
        self._user_card_frame.pack(side="bottom", fill="x")
        self._build_user_card(self._user_card_frame)

    def _friend_entry_focus_in(self, e):
        if self._add_friend_entry.get() == "Find or add friend...":
            self._add_friend_entry.delete(0, tk.END)
            self._add_friend_entry.config(fg=TEXT_WHITE)

    def _friend_entry_focus_out(self, e):
        if not self._add_friend_entry.get():
            self._add_friend_entry.insert(0, "Find or add friend...")
            self._add_friend_entry.config(fg=TEXT_MUTED)
        self._clear_suggestions()

    def _friend_entry_keyrelease(self, e):
        query = self._add_friend_entry.get().strip().lower()
        self._clear_suggestions()
        if not query or query == "find or add friend...": return
        matches = [u for u in self.all_users if query in u.lower() and u not in self.friends and u not in self.blocked and u != self.username][:5]
        for m in matches:
            row = tk.Frame(self._suggestions_frame, bg="#2B2D31", cursor="hand2"); row.pack(fill="x")
            tk_img = self.get_pil_pfp(m, size=(22,22)); row._img = tk_img
            tk.Label(row, image=tk_img, bg="#2B2D31").pack(side="left", padx=6, pady=3)
            tk.Label(row, text=m, fg=TEXT_WHITE, bg="#2B2D31", font=("Segoe UI",8)).pack(side="left")
            tk.Button(row, text="+ Add", bg=ACCENT_BLUE, fg=TEXT_WHITE, font=("Segoe UI",7,"bold"),
                      relief="flat", borderwidth=0, padx=4, cursor="hand2",
                      command=lambda n=m: self._quick_add_friend(n)).pack(side="right", padx=4, pady=3)

    def _clear_suggestions(self):
        if hasattr(self, '_suggestions_frame'):
            for w in self._suggestions_frame.winfo_children(): w.destroy()

    def _quick_add_friend(self, name):
        self._safe_send({"type":"add_friend","to":name})
        self._add_friend_entry.delete(0, tk.END)
        self._clear_suggestions()

    def _dm_list_row(self, parent, username, pending=False, unread=0):
        is_active = (self.current_view == VIEW_DM and self.current_dm_user == username)
        is_online = username in self.online_users
        row_bg = "#2B2D31" if is_active else BG_SIDEBAR
        row = tk.Frame(parent, bg=row_bg, cursor="hand2"); row.pack(fill="x", padx=8, pady=1)
        pfp_wrap = tk.Frame(row, bg=row_bg); pfp_wrap.pack(side="left", padx=(8,6), pady=6)
        tk_img = self.get_pil_pfp(username, size=(34,34)); row._img = tk_img
        tk.Label(pfp_wrap, image=tk_img, bg=row_bg).pack()
        if not pending:
            dot = tk.Label(pfp_wrap, text="●", fg="#3BA55D" if is_online else "#72767D", bg=row_bg, font=("Segoe UI",7))
            dot.place(relx=0.65, rely=0.65)
        info = tk.Frame(row, bg=row_bg); info.pack(side="left", fill="x", expand=True, pady=6)
        tk.Label(info, text=username, fg=user_color(username) if is_active else TEXT_WHITE,
                 bg=row_bg, font=("Segoe UI",9,"bold")).pack(anchor="w")
        if pending:
            tk.Label(info, text="Pending request", fg=TEXT_MUTED, bg=row_bg, font=("Segoe UI",8)).pack(anchor="w")
        else:
            key = f"dm:{username}"; msgs = self.chat_history.get(key,[])
            preview = (msgs[-1][1][:28]+"…") if msgs else ("● Online" if is_online else "○ Offline")
            tk.Label(info, text=preview, fg="#3BA55D" if (not msgs and is_online) else TEXT_MUTED,
                     bg=row_bg, font=("Segoe UI",8)).pack(anchor="w")
        if pending:
            tk.Button(row, text="✓", bg=ACCENT_GREEN, fg=TEXT_WHITE, font=("Segoe UI",9,"bold"),
                      relief="flat", borderwidth=0, padx=6, cursor="hand2",
                      command=lambda n=username: self._accept_friend(n)).pack(side="right", padx=(0,8))
        elif unread > 0:
            tk.Label(row, text=str(unread), bg=ACCENT_RED, fg=TEXT_WHITE,
                     font=("Segoe UI",7,"bold"), padx=5, pady=2).pack(side="right", padx=(0,8))
        if not pending:
            for widget in [row] + list(row.winfo_children()):
                try: widget.bind("<Button-1>", lambda e, u=username: self._open_dm(u))
                except: pass

    def _build_user_card(self, parent):
        card = tk.Frame(parent, bg="#0B0C0D", height=52); card.pack(fill="x"); card.pack_propagate(False)
        tk_img = self.get_pil_pfp(self.username, size=(32,32)); card._img = tk_img
        tk.Label(card, image=tk_img, bg="#0B0C0D").pack(side="left", padx=(10,6), pady=10)
        info = tk.Frame(card, bg="#0B0C0D"); info.pack(side="left", fill="x", expand=True, pady=10)
        tk.Label(info, text=self.username, fg=TEXT_WHITE, bg="#0B0C0D", font=("Segoe UI",9,"bold")).pack(anchor="w")
        tk.Label(info, text="● Online", fg=ACCENT_GREEN, bg="#0B0C0D", font=("Segoe UI",7)).pack(anchor="w")
        self._settings_btn = tk.Button(card, text="⚙", bg="#0B0C0D", fg=TEXT_MUTED,
                  font=("Segoe UI",14), relief="flat", borderwidth=0, cursor="hand2",
                  command=self.toggle_settings)
        self._settings_btn.pack(side="right", padx=6)
        tk.Button(card, text="⏻", bg="#0B0C0D", fg=ACCENT_RED,
                  font=("Segoe UI",13), relief="flat", borderwidth=0,
                  cursor="hand2", command=self.logout).pack(side="right", padx=4)

    # ── FRIENDS HOME ──────────────────────────────────────────────────────

    def _build_main_area_home(self):
        for w in self._main_area.winfo_children(): w.destroy()
        self.chat_panel = None
        header = tk.Frame(self._main_area, bg=BG_CHAT, highlightthickness=1, highlightbackground="#222")
        header.pack(fill="x")
        tk.Label(header, text="🏠  Friends", fg=TEXT_WHITE, bg=BG_CHAT,
                 font=("Segoe UI",13,"bold"), padx=20, pady=15).pack(side="left")
        tabs = tk.Frame(self._main_area, bg=BG_CHAT); tabs.pack(fill="x", padx=20, pady=(10,0))
        def tab_btn(text, cmd, active=False):
            tk.Button(tabs, text=text, bg="#2B2D31" if active else BG_CHAT,
                      fg=TEXT_WHITE if active else TEXT_MUTED, font=("Segoe UI",9,"bold"),
                      relief="flat", borderwidth=0, padx=14, cursor="hand2", command=cmd
                      ).pack(side="left", padx=2, ipady=6)
        tab_btn("All Friends",  self._friends_tab_all,    active=True)
        tab_btn("Pending",      self._friends_tab_pending)
        tab_btn("Blocked",      self._friends_tab_blocked)
        tab_btn("Add Friend",   self._friends_tab_add)
        tk.Frame(self._main_area, bg="#222", height=1).pack(fill="x", padx=20, pady=8)
        self._friends_content = tk.Frame(self._main_area, bg=BG_CHAT)
        self._friends_content.pack(fill="both", expand=True, padx=20)
        self._friends_tab_all()

    def _clear_fc(self):
        for w in self._friends_content.winfo_children(): w.destroy()

    def _friends_tab_all(self):
        self._clear_fc()
        tk.Label(self._friends_content, text=f"ALL FRIENDS — {len(self.friends)}",
                 fg=TEXT_MUTED, bg=BG_CHAT, font=("Segoe UI",8,"bold")).pack(anchor="w", pady=(10,6))
        if not self.friends:
            tk.Label(self._friends_content, text="No friends yet. Use 'Add Friend' to get started!",
                     fg=TEXT_MUTED, bg=BG_CHAT, font=FONT_REG).pack(pady=40); return
        self._fc_imgs = []
        for f in sorted(self.friends):
            card = tk.Frame(self._friends_content, bg=BG_INPUT, highlightthickness=1, highlightbackground="#2B2D31")
            card.pack(fill="x", pady=3)
            tk_img = self.get_pil_pfp(f, size=(40,40)); self._fc_imgs.append(tk_img)
            tk.Label(card, image=tk_img, bg=BG_INPUT).pack(side="left", padx=12, pady=10)
            info = tk.Frame(card, bg=BG_INPUT); info.pack(side="left", fill="x", expand=True, pady=10)
            tk.Label(info, text=f, fg=TEXT_WHITE, bg=BG_INPUT, font=("Segoe UI",10,"bold")).pack(anchor="w")
            tk.Label(info, text="● Online", fg=ACCENT_GREEN, bg=BG_INPUT, font=("Segoe UI",8)).pack(anchor="w")
            btns = tk.Frame(card, bg=BG_INPUT); btns.pack(side="right", padx=12)
            tk.Button(btns, text="💬 Message", bg=ACCENT_BLUE, fg=TEXT_WHITE, font=("Segoe UI",8,"bold"),
                      relief="flat", borderwidth=0, padx=10, cursor="hand2",
                      command=lambda u=f: self._open_dm(u)).pack(side="left", padx=2, ipady=6)
            tk.Button(btns, text="🚫 Block", bg="#2B2D31", fg=ACCENT_RED, font=("Segoe UI",8,"bold"),
                      relief="flat", borderwidth=0, padx=6, cursor="hand2",
                      command=lambda u=f: self._block_user(u)).pack(side="left", padx=2, ipady=6)
            tk.Button(btns, text="✕ Remove", bg="#2B2D31", fg=TEXT_MUTED, font=("Segoe UI",8,"bold"),
                      relief="flat", borderwidth=0, padx=6, cursor="hand2",
                      command=lambda u=f: self._remove_friend(u)).pack(side="left", padx=2, ipady=6)

    def _friends_tab_pending(self):
        self._clear_fc()
        tk.Label(self._friends_content, text=f"PENDING — {len(self.pending_requests)}",
                 fg=TEXT_MUTED, bg=BG_CHAT, font=("Segoe UI",8,"bold")).pack(anchor="w", pady=(10,6))
        if not self.pending_requests:
            tk.Label(self._friends_content, text="No pending requests.", fg=TEXT_MUTED, bg=BG_CHAT, font=FONT_REG).pack(pady=40); return
        self._pend_imgs = []
        for p in self.pending_requests:
            card = tk.Frame(self._friends_content, bg=BG_INPUT, highlightthickness=1, highlightbackground="#2B2D31")
            card.pack(fill="x", pady=3)
            tk_img = self.get_pil_pfp(p, size=(40,40)); self._pend_imgs.append(tk_img)
            tk.Label(card, image=tk_img, bg=BG_INPUT).pack(side="left", padx=12, pady=10)
            tk.Label(card, text=p, fg=TEXT_WHITE, bg=BG_INPUT, font=("Segoe UI",10,"bold")).pack(side="left", fill="x", expand=True, pady=10)
            tk.Button(card, text="✓ Accept", bg=ACCENT_GREEN, fg=TEXT_WHITE, font=("Segoe UI",8,"bold"),
                      relief="flat", borderwidth=0, padx=10, cursor="hand2",
                      command=lambda n=p: self._accept_friend(n)).pack(side="right", padx=12, ipady=6)

    def _friends_tab_blocked(self):
        self._clear_fc()
        tk.Label(self._friends_content, text=f"BLOCKED — {len(self.blocked)}",
                 fg=TEXT_MUTED, bg=BG_CHAT, font=("Segoe UI",8,"bold")).pack(anchor="w", pady=(10,6))
        if not self.blocked:
            tk.Label(self._friends_content, text="No blocked users.", fg=TEXT_MUTED, bg=BG_CHAT, font=FONT_REG).pack(pady=40); return
        self._blk_imgs = []
        for b in sorted(self.blocked):
            card = tk.Frame(self._friends_content, bg=BG_INPUT, highlightthickness=1, highlightbackground="#2B2D31")
            card.pack(fill="x", pady=3)
            tk_img = self.get_pil_pfp(b, size=(40,40)); self._blk_imgs.append(tk_img)
            tk.Label(card, image=tk_img, bg=BG_INPUT).pack(side="left", padx=12, pady=10)
            tk.Label(card, text=b, fg=TEXT_MUTED, bg=BG_INPUT, font=("Segoe UI",10,"bold")).pack(side="left", fill="x", expand=True, pady=10)
            tk.Button(card, text="Unblock", bg=ACCENT_ORANGE, fg=TEXT_WHITE, font=("Segoe UI",8,"bold"),
                      relief="flat", borderwidth=0, padx=10, cursor="hand2",
                      command=lambda n=b: self._unblock_user(n)).pack(side="right", padx=12, ipady=6)

    def _friends_tab_add(self):
        self._clear_fc()
        tk.Label(self._friends_content, text="ADD FRIEND", fg=TEXT_MUTED, bg=BG_CHAT,
                 font=("Segoe UI",8,"bold")).pack(anchor="w", pady=(10,6))
        row = tk.Frame(self._friends_content, bg=BG_INPUT, highlightthickness=1, highlightbackground="#444"); row.pack(fill="x")
        entry = tk.Entry(row, bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_REG, relief="flat", borderwidth=0, insertbackground="white")
        entry.pack(side="left", fill="x", expand=True, ipady=12, padx=14)
        entry.insert(0, "Enter a username")
        entry.bind("<FocusIn>", lambda e: entry.delete(0, tk.END) if entry.get()=="Enter a username" else None)
        suggestions_box = tk.Frame(self._friends_content, bg="#2B2D31"); suggestions_box.pack(fill="x", pady=(2,0))
        status = tk.Label(self._friends_content, text="", fg=ACCENT_GREEN, bg=BG_CHAT, font=("Segoe UI",9))
        def show_suggestions(e=None):
            for w in suggestions_box.winfo_children(): w.destroy()
            query = entry.get().strip().lower()
            if not query or query == "enter a username": return
            matches = [u for u in self.all_users if query in u.lower() and u != self.username and u not in self.blocked][:6]
            for m in matches:
                srow = tk.Frame(suggestions_box, bg="#2B2D31", cursor="hand2"); srow.pack(fill="x")
                tk_img = self.get_pil_pfp(m, size=(24,24)); srow._img = tk_img
                tk.Label(srow, image=tk_img, bg="#2B2D31").pack(side="left", padx=8, pady=4)
                tk.Label(srow, text=m, fg=TEXT_WHITE, bg="#2B2D31", font=("Segoe UI",9)).pack(side="left")
                is_friend = m in self.friends
                tk.Button(srow, text="✓ Already friends" if is_friend else "Add",
                          bg=ACCENT_GREEN if is_friend else ACCENT_BLUE, fg=TEXT_WHITE,
                          font=("Segoe UI",8,"bold"), relief="flat", borderwidth=0, padx=8, cursor="hand2",
                          command=lambda n=m: do_add(n)).pack(side="right", padx=8, pady=4)
        def do_add(name=None):
            if name is None: name = entry.get().strip()
            if not name or name == "Enter a username": return
            self._safe_send({"type":"add_friend","to":name})
            status.config(text=f"Friend request sent to {name}!")
            entry.delete(0, tk.END)
            for w in suggestions_box.winfo_children(): w.destroy()
        entry.bind("<KeyRelease>", show_suggestions)
        entry.bind("<Return>", lambda e: do_add())
        tk.Button(row, text="Send Friend Request", bg=ACCENT_BLUE, fg=TEXT_WHITE,
                  font=("Segoe UI",9,"bold"), relief="flat", borderwidth=0,
                  padx=14, cursor="hand2", command=do_add).pack(side="right", ipady=10)
        status.pack(anchor="w", pady=10)

    # ── DM CHAT ────────────────────────────────────────────────────────────

    def _open_dm(self, user):
        self.current_view = VIEW_DM; self.current_dm_user = user
        self.dm_unreads[user] = 0
        self._safe_send({"type":"dm_read","from":user})
        self._build_rail(); self._build_dm_list_panel(); self._build_dm_chat(user)

    def _build_dm_chat(self, user):
        for w in self._main_area.winfo_children(): w.destroy()
        self.chat_panel = tk.Frame(self._main_area, bg=BG_CHAT); self.chat_panel.pack(fill="both", expand=True)
        header = tk.Frame(self.chat_panel, bg=BG_CHAT, highlightthickness=1, highlightbackground="#222"); header.pack(fill="x")
        hdr_img = self.get_pil_pfp(user, size=(32,32)); header._img = hdr_img
        tk.Label(header, image=hdr_img, bg=BG_CHAT).pack(side="left", padx=(16,8), pady=12)
        self.header_label = tk.Label(header, text=f"@ {user}", fg=TEXT_WHITE, bg=BG_CHAT, font=("Segoe UI",12,"bold"), pady=12)
        self.header_label.pack(side="left")
        tk.Label(header, text="Direct Message", fg=TEXT_MUTED, bg=BG_CHAT, font=("Segoe UI",8), padx=8).pack(side="left")
        tk.Button(header, text="🚫 Block", bg=BG_CHAT, fg=ACCENT_RED, font=("Segoe UI",8),
                  relief="flat", borderwidth=0, cursor="hand2",
                  command=lambda: self._block_user(user)).pack(side="right", padx=10)
        self.chat = scrolledtext.ScrolledText(self.chat_panel, bg=BG_CHAT, fg=TEXT_WHITE,
                                              font=FONT_REG, borderwidth=0, state="disabled",
                                              insertbackground="white", padx=10)
        self.chat.pack(fill="both", expand=True, padx=10, pady=(10,2))
        ctx = f"dm:{user}"
        self._typing_labels[ctx] = tk.Label(self.chat_panel, text="", fg=TEXT_MUTED, bg=BG_CHAT, font=("Segoe UI",8,"italic"))
        self._typing_labels[ctx].pack(anchor="w", padx=20)
        input_frame = tk.Frame(self.chat_panel, bg=BG_CHAT, padx=16, pady=12); input_frame.pack(fill="x")
        tk.Button(input_frame, text="📎", bg=BG_INPUT, fg=TEXT_MUTED, font=("Segoe UI",13),
                  relief="flat", borderwidth=0, cursor="hand2",
                  command=lambda: self._send_image_dm(user)).pack(side="left", padx=(0,6), ipady=8)
        self.entry = tk.Entry(input_frame, bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_REG,
                              borderwidth=0, relief="flat", insertbackground="white",
                              highlightthickness=1, highlightbackground="#333")
        self.entry.pack(side="left", fill="x", expand=True, ipady=12)
        self.entry.insert(0, f"Message @{user}")
        self.entry.bind("<FocusIn>",  lambda e: self.entry.delete(0, tk.END) if self.entry.get().startswith("Message @") else None)
        self.entry.bind("<Return>",   lambda e: self._send_dm(user))
        self.entry.bind("<KeyPress>", lambda e: self._on_typing(f"dm:{user}"))
        dm_send = tk.Button(input_frame, text="SEND", bg=ACCENT_PURPLE, fg=TEXT_WHITE,
                  font=("Segoe UI",9,"bold"), relief="flat", borderwidth=0, padx=20, cursor="hand2",
                  command=lambda: self._send_dm(user))
        dm_send.pack(side="right", padx=(10,0), ipady=10)
        self._glow_btn(dm_send, ACCENT_PURPLE, "#b36bff")
        self.load_history()

    def _send_dm(self, user):
        msg = self.entry.get().strip()
        if not msg or msg.startswith("Message @"): return
        self._safe_send({"type":"dm","to":user,"content":msg})
        key = f"dm:{user}"
        if key not in self.chat_history: self.chat_history[key] = []
        self.chat_history[key].append((self.username, msg, self._now(), "", "", False))
        self.entry.delete(0, tk.END)
        self._stop_typing(f"dm:{user}")
        self.load_history()

    def _send_image_dm(self, user):
        filepath = filedialog.askopenfilename(filetypes=[("Image Files","*.png;*.jpg;*.jpeg;*.gif;*.webp")])
        if not filepath: return
        try:
            img = Image.open(filepath); img.thumbnail((400,400), Image.Resampling.LANCZOS)
            buf = io.BytesIO(); img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            self._safe_send({"type":"dm","to":user,"content":"[Image]","image":b64})
            key = f"dm:{user}"
            if key not in self.chat_history: self.chat_history[key] = []
            self.chat_history[key].append((self.username, "[Image]", self._now(), "", b64, False))
            self.load_history()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ── SERVER / CHANNEL VIEW ──────────────────────────────────────────────

    def _switch_to_server(self, srv):
        self.current_view = VIEW_SERVER; self.server = srv
        self.channel = self.servers_data.get(srv,{}).get("channels",["general"])[0]
        self.unread_counts[srv] = 0
        self._safe_send({"type":"mark_read","server":srv})
        self._build_rail(); self._build_channel_list_panel()
        self._fade_transition(self._build_server_chat)

    def _build_channel_list_panel(self):
        for w in self.left_panel.winfo_children(): w.destroy()
        header = tk.Frame(self.left_panel, bg="#111214", highlightthickness=1, highlightbackground="#222", height=50)
        header.pack(fill="x"); header.pack_propagate(False)
        tk.Label(header, text=self.server, fg=TEXT_WHITE, bg="#111214",
                 font=("Segoe UI",11,"bold"), padx=16).pack(side="left", pady=14)
        invite = self.servers_data.get(self.server,{}).get("invite","")
        if invite:
            inv_lbl = tk.Label(header, text=f"📋 {invite}", fg=TEXT_MUTED, bg="#111214", font=("Segoe UI",7), cursor="hand2")
            inv_lbl.pack(side="right", padx=8)
            def copy_invite(e, code=invite):
                self.root.clipboard_clear(); self.root.clipboard_append(code)
                self._toast("📋 Copied!", f"Invite code: {code}", ACCENT_BLUE)
            inv_lbl.bind("<Button-1>", copy_invite)

        # ── TEXT CHANNELS ────────────────────────────────────────────
        tk.Label(self.left_panel, text="TEXT CHANNELS", fg=TEXT_MUTED, bg=BG_SIDEBAR,
                 font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(14,4))
        for chan in self.servers_data.get(self.server,{}).get("channels",["general"]):
            is_active = chan == self.channel
            c_bg = "#2B2D31" if is_active else BG_SIDEBAR
            row = tk.Frame(self.left_panel, bg=c_bg, cursor="hand2"); row.pack(fill="x", padx=8, pady=1)
            lbl = tk.Label(row, text=f"#  {chan}", fg=TEXT_WHITE if is_active else TEXT_MUTED,
                           bg=c_bg, font=("Segoe UI",9), padx=8, pady=8, anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            del_btn = tk.Button(row, text="🗑", bg=c_bg, fg=ACCENT_RED, font=("Segoe UI",9),
                                relief="flat", borderwidth=0, cursor="hand2", padx=4,
                                command=lambda c=chan: self._delete_channel(c))
            del_btn.pack(side="right", padx=4)
            if chan == "general": del_btn.config(state="disabled", fg=TEXT_MUTED)
            row.bind("<Button-1>", lambda e, c=chan: self._switch_channel(c))
            lbl.bind("<Button-1>", lambda e, c=chan: self._switch_channel(c))

        add = tk.Frame(self.left_panel, bg=BG_SIDEBAR, cursor="hand2"); add.pack(fill="x", padx=8, pady=1)
        add_lbl = tk.Label(add, text="＋  Add Channel", fg=TEXT_MUTED, bg=BG_SIDEBAR, font=("Segoe UI",9), padx=8, pady=6)
        add_lbl.pack(anchor="w")
        add.bind("<Button-1>",     lambda e: self._create_channel_dialog(self.server))
        add_lbl.bind("<Button-1>", lambda e: self._create_channel_dialog(self.server))

        # ── VOICE CHANNELS ────────────────────────────────────────────
        tk.Label(self.left_panel, text="VOICE CHANNELS", fg=TEXT_MUTED, bg=BG_SIDEBAR,
                 font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(14,4))

        for vc in self.servers_data.get(self.server,{}).get("voice_channels",["General"]):
            in_this = (self.current_voice == (self.server, vc))
            vc_bg   = "#1a2a1a" if in_this else BG_SIDEBAR
            vc_row  = tk.Frame(self.left_panel, bg=vc_bg, cursor="hand2"); vc_row.pack(fill="x", padx=8, pady=1)

            # Speaker icon - green if we're in it
            spkr = "🔊" if in_this else "🔈"
            vc_lbl = tk.Label(vc_row, text=f"{spkr}  {vc}",
                              fg="#3BA55D" if in_this else TEXT_MUTED,
                              bg=vc_bg, font=("Segoe UI",9), padx=8, pady=6, anchor="w")
            vc_lbl.pack(side="left", fill="x", expand=True)

            # Who's in this channel
            occupants = self.voice_state.get(self.server, {}).get(vc, [])
            if occupants:
                occ_label = ", ".join(occupants[:3]) + ("…" if len(occupants) > 3 else "")
                tk.Label(vc_row, text=occ_label, fg="#3BA55D", bg=vc_bg,
                         font=("Segoe UI",7)).pack(side="left", padx=(0,4))

            if in_this:
                tk.Button(vc_row, text="✕ Leave", bg="#1a2a1a", fg=ACCENT_RED,
                          font=("Segoe UI",7,"bold"), relief="flat", borderwidth=0,
                          padx=4, cursor="hand2",
                          command=self.leave_voice).pack(side="right", padx=4)
            else:
                def join_vc(e=None, s=self.server, c=vc):
                    self.join_voice(s, c)
                vc_row.bind("<Button-1>", join_vc)
                vc_lbl.bind("<Button-1>", join_vc)

            # Show member avatars in voice channel
            if occupants:
                occ_row = tk.Frame(self.left_panel, bg=BG_SIDEBAR); occ_row.pack(fill="x", padx=20, pady=(0,2))
                occ_row._imgs = []
                for occ_user in occupants[:5]:
                    try:
                        pil_img = self.get_pil_pfp(occ_user, size=(18,18))
                        tk_img  = ImageTk.PhotoImage(pil_img)
                        occ_row._imgs.append(tk_img)
                        lbl = tk.Label(occ_row, image=tk_img, bg=BG_SIDEBAR)
                        lbl.pack(side="left", padx=1)
                        tk.Label(occ_row, text=occ_user, fg="#3BA55D" if occ_user == self.username else TEXT_MUTED,
                                 bg=BG_SIDEBAR, font=("Segoe UI",7)).pack(side="left", padx=(0,6))
                    except: pass

        add_vc = tk.Frame(self.left_panel, bg=BG_SIDEBAR, cursor="hand2"); add_vc.pack(fill="x", padx=8, pady=1)
        add_vc_lbl = tk.Label(add_vc, text="＋  Add Voice Channel", fg=TEXT_MUTED, bg=BG_SIDEBAR,
                              font=("Segoe UI",9), padx=8, pady=6)
        add_vc_lbl.pack(anchor="w")
        add_vc.bind("<Button-1>",     lambda e: self._create_voice_channel_dialog())
        add_vc_lbl.bind("<Button-1>", lambda e: self._create_voice_channel_dialog())

        # ── MEMBERS ───────────────────────────────────────────────────
        members     = self.servers_data.get(self.server,{}).get("members",[])
        online_here  = [m for m in members if m in self.online_users]
        offline_here = [m for m in members if m not in self.online_users]
        if online_here:
            tk.Label(self.left_panel, text=f"ONLINE — {len(online_here)}", fg=TEXT_MUTED, bg=BG_SIDEBAR,
                     font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(14,4))
        self.user_container = tk.Frame(self.left_panel, bg=BG_SIDEBAR); self.user_container.pack(fill="x")
        for u in online_here: self._member_row(self.user_container, u, online=True)
        if offline_here:
            tk.Label(self.left_panel, text=f"OFFLINE — {len(offline_here)}", fg=TEXT_MUTED, bg=BG_SIDEBAR,
                     font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(10,4))
            off_container = tk.Frame(self.left_panel, bg=BG_SIDEBAR); off_container.pack(fill="x")
            for u in offline_here: self._member_row(off_container, u, online=False)

        # user card + voice bar at bottom
        self._user_card_frame = tk.Frame(self.left_panel, bg=BG_SIDEBAR)
        self._user_card_frame.pack(side="bottom", fill="x")
        if self.current_voice:
            self._build_voice_bar()
        self._build_user_card(self._user_card_frame)

    def _create_voice_channel_dialog(self):
        name = simpledialog.askstring("New Voice Channel", "Enter voice channel name:")
        if name:
            self._safe_send({"type":"create_voice_channel","server":self.server,"channel":name})

    def _build_server_chat(self):
        for w in self._main_area.winfo_children(): w.destroy()
        self.chat_panel = tk.Frame(self._main_area, bg=BG_CHAT); self.chat_panel.pack(fill="both", expand=True)
        header = tk.Frame(self.chat_panel, bg=BG_CHAT, highlightthickness=1, highlightbackground="#222"); header.pack(fill="x")
        self.header_label = tk.Label(header, text=f"#  {self.channel}", fg=TEXT_WHITE,
                                     bg=BG_CHAT, font=("Segoe UI",12,"bold"), padx=20, pady=15, anchor="w")
        self.header_label.pack(side="left")
        tk.Button(header, text="↻", bg=BG_INPUT, fg=TEXT_MUTED, font=("Segoe UI",9),
                  relief="flat", borderwidth=0, padx=8, cursor="hand2", command=self.load_history).pack(side="right", padx=8)
        total_members = len(self.servers_data.get(self.server,{}).get("members",[]))
        online_count  = len([u for u in self.online_users if u in self.servers_data.get(self.server,{}).get("members",[])])
        info_frame = tk.Frame(header, bg=BG_CHAT); info_frame.pack(side="right", padx=10)
        tk.Label(info_frame, text=f"🟢 {online_count} online", fg="#3BA55D", bg=BG_CHAT, font=("Segoe UI",8)).pack(side="left", padx=(0,8))
        tk.Label(info_frame, text=f"👥 {total_members} members", fg=TEXT_MUTED, bg=BG_CHAT, font=("Segoe UI",8)).pack(side="left")

        # Pinned bar
        key    = f"{self.server}:{self.channel}"
        pinned = self.pinned_messages.get(key, [])
        if pinned:
            pin_bar = tk.Frame(self.chat_panel, bg="#2B2D31", highlightthickness=1, highlightbackground="#3a3a40")
            pin_bar.pack(fill="x")
            tk.Label(pin_bar, text="📌", bg="#2B2D31", font=("Segoe UI",9)).pack(side="left", padx=(10,4), pady=6)
            last_pin = pinned[-1]
            tk.Label(pin_bar, text=f"{last_pin[0]}: {last_pin[1][:60]}", fg=TEXT_WHITE, bg="#2B2D31", font=("Segoe UI",8)).pack(side="left", pady=6)
            tk.Button(pin_bar, text=f"  {len(pinned)} pinned  ▾", bg="#2B2D31", fg=TEXT_MUTED,
                      font=("Segoe UI",7), relief="flat", borderwidth=0, cursor="hand2",
                      command=lambda k=key: self._show_pinned(k)).pack(side="right", padx=10)

        self.chat = scrolledtext.ScrolledText(self.chat_panel, bg=BG_CHAT, fg=TEXT_WHITE,
                                              font=FONT_REG, borderwidth=0, state="disabled",
                                              insertbackground="white", padx=10)
        self.chat.pack(fill="both", expand=True, padx=10, pady=(10,2))
        ctx = f"{self.server}:{self.channel}"
        self._typing_labels[ctx] = tk.Label(self.chat_panel, text="", fg=TEXT_MUTED, bg=BG_CHAT, font=("Segoe UI",8,"italic"))
        self._typing_labels[ctx].pack(anchor="w", padx=20)
        input_frame = tk.Frame(self.chat_panel, bg=BG_CHAT, padx=16, pady=12); input_frame.pack(fill="x")
        tk.Button(input_frame, text="📎", bg=BG_INPUT, fg=TEXT_MUTED, font=("Segoe UI",13),
                  relief="flat", borderwidth=0, cursor="hand2",
                  command=self._send_image_channel).pack(side="left", padx=(0,6), ipady=8)
        self.entry = tk.Entry(input_frame, bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_REG,
                              borderwidth=0, relief="flat", insertbackground="white",
                              highlightthickness=1, highlightbackground="#333")
        self.entry.pack(side="left", fill="x", expand=True, ipady=12)
        self.entry.bind("<Return>",   lambda e: self.send())
        self.entry.bind("<KeyPress>", lambda e: self._on_typing(f"{self.server}:{self.channel}"))
        srv_send = tk.Button(input_frame, text="SEND", bg=ACCENT_PURPLE, fg=TEXT_WHITE,
                  font=("Segoe UI",9,"bold"), relief="flat", borderwidth=0, padx=20, cursor="hand2", command=self.send)
        srv_send.pack(side="right", padx=(10,0), ipady=10)
        self._glow_btn(srv_send, ACCENT_PURPLE, "#b36bff")
        self.load_history()

    def _switch_channel(self, channel):
        self.channel = channel; self._build_channel_list_panel()
        self._fade_transition(self._build_server_chat)

    def _send_image_channel(self):
        filepath = filedialog.askopenfilename(filetypes=[("Image Files","*.png;*.jpg;*.jpeg;*.gif;*.webp")])
        if not filepath: return
        try:
            img = Image.open(filepath); img.thumbnail((400,400), Image.Resampling.LANCZOS)
            buf = io.BytesIO(); img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            self._safe_send({"type":"channel_message","server":self.server,"channel":self.channel,"content":"[Image]","image":b64})
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ── TYPING ────────────────────────────────────────────────────────────

    def _on_typing(self, context):
        if not self._my_typing:
            self._my_typing = True
            self._safe_send({"type":"typing","context":context,"typing":True})
        if self._my_typing_timer: self.root.after_cancel(self._my_typing_timer)
        self._my_typing_timer = self.root.after(TYPING_TIMEOUT, lambda: self._stop_typing(context))

    def _stop_typing(self, context):
        self._my_typing = False; self._my_typing_timer = None
        self._safe_send({"type":"typing","context":context,"typing":False})

    def _update_typing_label(self, context):
        who = self._typing_who.get(context, set())
        lbl = self._typing_labels.get(context)
        if not lbl: return
        try:
            if not who: lbl.config(text="")
            elif len(who) == 1: lbl.config(text=f"{next(iter(who))} is typing...")
            else: lbl.config(text=f"{', '.join(list(who)[:3])} are typing...")
        except: pass

    # ── MESSAGE ACTIONS ───────────────────────────────────────────────────

    def _show_dots_menu(self, event, sender, content, msg_id, key):
        menu = tk.Toplevel(self.root); menu.overrideredirect(True); menu.attributes("-topmost", True)
        menu.configure(bg="#2B2D31", highlightthickness=1, highlightbackground="#444")
        x = self.root.winfo_pointerx(); y = self.root.winfo_pointery()
        def close_menu(e=None):
            try: menu.destroy()
            except: pass
        def add_btn(text, cmd, color=TEXT_WHITE):
            b = tk.Button(menu, text=text, bg="#2B2D31", fg=color, font=("Segoe UI",9),
                          relief="flat", borderwidth=0, anchor="w", padx=14, cursor="hand2",
                          activebackground="#3A3C42", activeforeground=color,
                          command=lambda c=cmd: [close_menu(), c()])
            b.pack(fill="x", ipady=6)
            b.bind("<Enter>", lambda e, b=b: b.config(bg="#3A3C42"))
            b.bind("<Leave>", lambda e, b=b: b.config(bg="#2B2D31"))
        add_btn("😄  Add Reaction",  lambda: self._show_emoji_picker(msg_id, key))
        add_btn("📋  Copy Text",     lambda: self._copy_text(content))
        add_btn("📌  Pin Message",   lambda: self._pin_message(key, sender, content))
        if sender == self.username:
            add_btn("✏️  Edit",      lambda: self._inline_edit(msg_id, key, content))
            add_btn("🗑  Delete",    lambda: self._delete_msg(msg_id, key), ACCENT_RED)
        menu.update_idletasks()
        w = menu.winfo_reqwidth(); h = menu.winfo_reqheight()
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        menu.geometry(f"+{min(x,sw-w-8)}+{min(y,sh-h-8)}")
        menu.bind("<FocusOut>", close_menu); menu.focus_set()

    def _show_emoji_picker(self, msg_id, key):
        picker = tk.Toplevel(self.root); picker.overrideredirect(True); picker.attributes("-topmost", True)
        picker.configure(bg="#2B2D31", highlightthickness=1, highlightbackground="#444")
        x = self.root.winfo_pointerx(); y = self.root.winfo_pointery()
        def pick(emoji, k=key, mid=msg_id):
            picker.destroy(); self._send_reaction(emoji, mid, k)
        grid = tk.Frame(picker, bg="#2B2D31"); grid.pack(padx=8, pady=8)
        for i, emo in enumerate(EMOJI_GRID):
            btn = tk.Button(grid, text=emo, bg="#2B2D31", fg=TEXT_WHITE, font=("Segoe UI",16),
                            relief="flat", borderwidth=0, cursor="hand2", width=2, command=lambda e=emo: pick(e))
            btn.grid(row=i//8, column=i%8, padx=2, pady=2)
            btn.bind("<Enter>", lambda e, b=btn: b.config(bg="#3A3C42"))
            btn.bind("<Leave>", lambda e, b=btn: b.config(bg="#2B2D31"))
        picker.update_idletasks()
        w = picker.winfo_reqwidth(); h = picker.winfo_reqheight()
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        picker.geometry(f"+{min(x,sw-w-8)}+{min(y,sh-h-8)}")
        picker.bind("<FocusOut>", lambda e: picker.destroy()); picker.focus_set()

    def _send_reaction(self, emoji, msg_id, key):
        if key.startswith("dm:"):
            other = key[3:]
            self._safe_send({"type":"dm","to":other,"content":f"[Reaction: {emoji}]"})
            if key not in self.chat_history: self.chat_history[key] = []
            self.chat_history[key].append((self.username, f"[Reaction: {emoji}]", self._now(), "", "", False))
        else:
            parts = key.split(":")
            if len(parts) == 2:
                self._safe_send({"type":"channel_message","server":parts[0],"channel":parts[1],"content":f"[Reaction: {emoji}]"})
        self.load_history()

    def _copy_text(self, content):
        self.root.clipboard_clear(); self.root.clipboard_append(content)
        self._toast("📋 Copied", "Message copied to clipboard", ACCENT_BLUE)

    def _inline_edit(self, msg_id, key, old_content):
        if not msg_id: return
        msgs = self.chat_history.get(key, [])
        msg  = next((m for m in msgs if m[3] == msg_id), None)
        if not msg or msg[0] != self.username: return
        win = tk.Toplevel(self.root); win.overrideredirect(True); win.attributes("-topmost", True)
        win.configure(bg="#2B2D31", highlightthickness=1, highlightbackground="#555")
        x = self.root.winfo_rootx() + self.root.winfo_width()//2 - 200
        y = self.root.winfo_rooty() + self.root.winfo_height()//2 - 40
        win.geometry(f"400x80+{x}+{y}")
        row = tk.Frame(win, bg="#2B2D31"); row.pack(fill="both", expand=True, padx=8, pady=8)
        entry = tk.Entry(row, bg="#1E1F22", fg=TEXT_WHITE, font=FONT_REG, relief="flat",
                         insertbackground="white", highlightthickness=1, highlightbackground="#555")
        entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0,8))
        entry.insert(0, old_content); entry.select_range(0, tk.END); entry.focus_set()
        def save(e=None):
            new_c = entry.get().strip()
            if new_c and new_c != old_content:
                self._safe_send({"type":"edit_message","key":key,"msg_id":msg_id,"content":new_c})
                for i, m in enumerate(msgs):
                    if m[3] == msg_id: msgs[i] = (m[0],new_c,m[2],m[3],m[4],True)
                self.load_history()
            win.destroy()
        tk.Button(row, text="Save", bg=ACCENT_BLUE, fg=TEXT_WHITE, font=("Segoe UI",8,"bold"),
                  relief="flat", borderwidth=0, padx=10, cursor="hand2", command=save).pack(side="left", ipady=8)
        tk.Button(row, text="✕", bg="#2B2D31", fg=TEXT_MUTED, font=("Segoe UI",9),
                  relief="flat", borderwidth=0, cursor="hand2", command=win.destroy).pack(side="left", padx=(4,0), ipady=8)
        entry.bind("<Return>", save); entry.bind("<Escape>", lambda e: win.destroy())

    def _delete_msg(self, msg_id, key):
        if not messagebox.askyesno("Delete", "Delete this message?"): return
        self._safe_send({"type":"delete_message","key":key,"msg_id":msg_id})
        self.chat_history[key] = [m for m in self.chat_history.get(key,[]) if m[3]!=msg_id]
        self._history_len[key] = 0
        self.load_history(force_full=True)

    def _pin_message(self, key, sender, content):
        if key.startswith("dm:"): return
        pins = self.pinned_messages.setdefault(key, [])
        for p in pins:
            if p[1] == content and p[0] == sender:
                self._toast("📌 Already Pinned", "This message is already pinned", TEXT_MUTED); return
        ts = datetime.now().strftime("%b %d %I:%M %p")
        pins.append((sender, content, ts))
        self._toast("📌 Pinned", f"{sender}: {content[:40]}", ACCENT_ORANGE)
        self._build_server_chat()

    def _show_pinned(self, key):
        pins = self.pinned_messages.get(key, [])
        if not pins: return
        win = tk.Toplevel(self.root); win.title("Pinned Messages"); win.geometry("420x400")
        win.configure(bg="#0e0e10"); win.grab_set()
        tk.Label(win, text="📌  Pinned Messages", fg=TEXT_WHITE, bg="#0e0e10",
                 font=("Segoe UI",12,"bold")).pack(pady=(16,8), padx=16, anchor="w")
        tk.Frame(win, bg="#2B2D31", height=1).pack(fill="x")
        scroll = tk.Frame(win, bg="#0e0e10"); scroll.pack(fill="both", expand=True, padx=12, pady=8)
        for i, (sender, content, ts) in enumerate(reversed(pins)):
            card = tk.Frame(scroll, bg="#1E1F22", highlightthickness=1, highlightbackground="#333")
            card.pack(fill="x", pady=4)
            hdr = tk.Frame(card, bg="#1E1F22"); hdr.pack(fill="x", padx=12, pady=(8,2))
            tk.Label(hdr, text=sender, fg=user_color(sender), bg="#1E1F22", font=("Segoe UI",9,"bold")).pack(side="left")
            tk.Label(hdr, text=ts, fg=TEXT_MUTED, bg="#1E1F22", font=("Segoe UI",7)).pack(side="left", padx=8)
            tk.Label(card, text=content, fg=TEXT_WHITE, bg="#1E1F22", font=FONT_REG, anchor="w",
                     justify="left", wraplength=360).pack(anchor="w", padx=12, pady=(0,8))
            def unpin(idx=len(pins)-1-i, k=key):
                self.pinned_messages[k].pop(idx); win.destroy(); self._build_server_chat()
            tk.Button(card, text="Unpin", bg="#1E1F22", fg=ACCENT_RED, font=("Segoe UI",7),
                      relief="flat", borderwidth=0, cursor="hand2", command=unpin).pack(anchor="e", padx=12, pady=(0,6))

    def _show_tooltip(self, widget, text):
        tip = tk.Toplevel(self.root); tip.overrideredirect(True); tip.attributes("-topmost", True)
        tip.configure(bg="#111214", highlightthickness=1, highlightbackground="#333")
        tk.Label(tip, text=text, fg=TEXT_WHITE, bg="#111214", font=("Segoe UI",8), padx=8, pady=4).pack()
        tip.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty()-28}")
        widget._tip = tip
        widget.bind("<Leave>", lambda e: (lambda: tip.destroy() if tip.winfo_exists() else None)())

    # ── LOAD HISTORY ──────────────────────────────────────────────────────

    def load_history(self, force_full=False):
        if not hasattr(self,'chat') or self.chat is None: return
        try:
            if not self.chat.winfo_exists(): return
        except: return

        key  = self._current_key()
        msgs = self.chat_history.get(key, [])
        last_rendered = self._history_len.get(key, 0)

        # Full rebuild needed: forced, or messages were deleted/edited (count went down)
        if force_full or last_rendered > len(msgs):
            self.chat.config(state="normal")
            self.chat.delete("1.0", tk.END)
            self.history_images = []
            last_rendered = 0

        # Only render new messages
        new_msgs = msgs[last_rendered:]
        if not new_msgs and not force_full: return

        self.chat.config(state="normal")
        for idx, (sender, content, timestamp, msg_id, image, edited) in enumerate(new_msgs):
            self._render_message(sender, content, timestamp, msg_id, image, edited, key, last_rendered + idx, msgs)

        self._history_len[key] = len(msgs)
        self.chat.config(state="disabled")
        self.chat.yview(tk.END)

    def _render_message(self, sender, content, timestamp, msg_id, image, edited, key, idx, msgs):
        msg_frame = tk.Frame(self.chat, bg=BG_CHAT); msg_frame._msg_id = msg_id; msg_frame._msg_from = sender
        # get_pil_pfp now returns cached ImageTk.PhotoImage directly
        tk_img = self.get_pil_pfp(sender)
        self.history_images.append(tk_img)
        tk.Label(msg_frame, image=tk_img, bg=BG_CHAT).pack(side="left", anchor="n", padx=(4,10), pady=6)
        text_frame = tk.Frame(msg_frame, bg=BG_CHAT); text_frame.pack(side="left", anchor="n", fill="x", expand=True, pady=6)
        hdr = tk.Frame(text_frame, bg=BG_CHAT); hdr.pack(anchor="w", fill="x")
        tk.Label(hdr, text=sender, fg=user_color(sender), bg=BG_CHAT, font=FONT_BOLD, cursor="hand2").pack(side="left")
        ts_lbl = tk.Label(hdr, text=f"  {timestamp}", fg=TEXT_MUTED, bg=BG_CHAT, font=FONT_TIME)
        ts_lbl.pack(side="left", padx=(6,0))
        ts_lbl.bind("<Enter>", lambda e, w=ts_lbl, t=datetime.now().strftime(f"Today at {timestamp}"): self._show_tooltip(w, t))
        if edited:
            tk.Label(hdr, text="(edited)", fg=TEXT_MUTED, bg=BG_CHAT, font=("Segoe UI",7,"italic")).pack(side="left", padx=(4,0))
        if self.current_view == VIEW_DM and sender == self.username:
            is_last = (idx == len(msgs) - 1)
            read = getattr(self, '_last_dm_read', {}).get(self.current_dm_user, False)
            receipt_text = "✓✓" if is_last else "✓"
            receipt_color = ACCENT_BLUE if (is_last and read) else TEXT_MUTED
            tk.Label(hdr, text=receipt_text, fg=receipt_color, bg=BG_CHAT, font=("Segoe UI",8)).pack(side="left", padx=(6,0))
        dots_btn = tk.Button(hdr, text="⋯", bg=BG_CHAT, fg=TEXT_MUTED, font=("Segoe UI",11),
                             relief="flat", borderwidth=0, cursor="hand2", padx=4)
        dots_btn.pack(side="right", padx=(0,8)); dots_btn.place_forget()
        def show_dots(e, btn=dots_btn): btn.place(in_=hdr, relx=1.0, rely=0.0, anchor="ne", x=-4, y=2)
        def hide_dots(e, btn=dots_btn): btn.place_forget()
        msg_frame.bind("<Enter>", show_dots); msg_frame.bind("<Leave>", hide_dots)
        text_frame.bind("<Enter>", show_dots); hdr.bind("<Enter>", show_dots)
        dots_btn.bind("<Enter>", show_dots)
        def open_dots_menu(e=None, s=sender, c=content, mid=msg_id, k=key):
            self._show_dots_menu(e, s, c, mid, k)
        dots_btn.config(command=open_dots_menu); dots_btn.bind("<Button-1>", lambda e: open_dots_menu(e))
        if image:
            try:
                pil_chat = Image.open(io.BytesIO(base64.b64decode(image)))
                pil_chat.thumbnail((300,300), Image.Resampling.LANCZOS)
                chat_img = ImageTk.PhotoImage(pil_chat); self.history_images.append(chat_img)
                tk.Label(text_frame, image=chat_img, bg=BG_CHAT, cursor="hand2").pack(anchor="w", pady=4)
            except: pass
        else:
            if "[Reaction:" in content:
                tk.Label(text_frame, text=content, fg=ACCENT_PURPLE, bg=BG_CHAT, font=("Segoe UI",18), anchor="w").pack(anchor="w")
            else:
                self._render_text_with_links(text_frame, content, msg_id, key, sender)
        if sender == self.username:
            text_frame.bind("<Double-Button-1>", lambda e, mid=msg_id, k=key, c=content: self._inline_edit(mid, k, c))
        self.chat.window_create(tk.END, window=msg_frame); self.chat.insert(tk.END, "\n")
        if idx == len(msgs) - 1: self._animate_message_in(msg_frame)

    def _render_text_with_links(self, parent, content, msg_id, key, sender):
        parts = URL_RE.split(content); urls = URL_RE.findall(content)
        frame = tk.Frame(parent, bg=BG_CHAT); frame.pack(anchor="w", fill="x")
        combined = []
        for i, part in enumerate(parts):
            if part: combined.append(("text", part))
            if i < len(urls): combined.append(("url", urls[i]))
        for kind, val in combined:
            if kind == "text":
                tk.Label(frame, text=val, fg=TEXT_WHITE, bg=BG_CHAT, font=FONT_REG,
                         anchor="w", justify="left", wraplength=520).pack(side="left", anchor="w")
            else:
                lbl = tk.Label(frame, text=val, fg="#00AFF4", bg=BG_CHAT, font=FONT_REG, cursor="hand2", anchor="w")
                lbl.pack(side="left", anchor="w")
                lbl.bind("<Button-1>", lambda e, u=val: webbrowser.open(u))
                lbl.bind("<Enter>", lambda e, l=lbl: l.config(fg="#FFFFFF"))
                lbl.bind("<Leave>", lambda e, l=lbl: l.config(fg="#00AFF4"))

    # ── SEND ──────────────────────────────────────────────────────────────

    def send(self):
        msg = self.entry.get().strip()
        if not msg: return
        self._safe_send({"type":"channel_message","server":self.server,"channel":self.channel,"content":msg})
        self.entry.delete(0, tk.END)
        self._stop_typing(f"{self.server}:{self.channel}")

    # ── FRIEND / BLOCK ────────────────────────────────────────────────────

    def _send_friend_request_from_entry(self):
        name = self._add_friend_entry.get().strip()
        if name and name != "Find or add friend...":
            self._safe_send({"type":"add_friend","to":name})
            self._add_friend_entry.delete(0, tk.END); self._clear_suggestions()

    def _accept_friend(self, name): self._safe_send({"type":"friend_accept","from":name})

    def _block_user(self, name):
        if messagebox.askyesno("Block", f"Block {name}?"):
            self._safe_send({"type":"block_user","user":name})
            if self.current_view == VIEW_DM and self.current_dm_user == name:
                self._show_home_view()

    def _unblock_user(self, name): self._safe_send({"type":"unblock_user","user":name})
    def _remove_friend(self, name):
        if messagebox.askyesno("Remove Friend", f"Remove {name}?"):
            self._safe_send({"type":"remove_friend","user":name})

    def render_friends(self):
        if getattr(self, '_render_friends_pending', False): return
        self._render_friends_pending = True
        self.root.after(150, self._do_render_friends)

    def _do_render_friends(self):
        self._render_friends_pending = False
        if self.current_view in (VIEW_HOME, VIEW_DM): self._build_dm_list_panel()
        if self.current_view == VIEW_HOME: self._build_main_area_home()

    # ── SERVER HELPERS ────────────────────────────────────────────────────

    def _add_server_dialog(self):
        self.current_view = "add_server"; self._build_rail()
        for w in self.left_panel.winfo_children(): w.destroy()
        tk.Label(self.left_panel, text="Add Server", fg=TEXT_WHITE, bg=BG_SIDEBAR,
                 font=("Segoe UI",11,"bold")).pack(anchor="w", padx=14, pady=(16,8))
        self._user_card_frame = tk.Frame(self.left_panel, bg=BG_SIDEBAR)
        self._user_card_frame.pack(side="bottom", fill="x")
        self._build_user_card(self._user_card_frame)
        for w in self._main_area.winfo_children(): w.destroy()
        self.chat_panel = None
        header = tk.Frame(self._main_area, bg=BG_CHAT, highlightthickness=1, highlightbackground="#222"); header.pack(fill="x")
        tk.Label(header, text="➕  Add a Server", fg=TEXT_WHITE, bg=BG_CHAT,
                 font=("Segoe UI",13,"bold"), padx=20, pady=15).pack(side="left")
        tk.Button(header, text="✕ Cancel", bg=BG_CHAT, fg=TEXT_MUTED, font=("Segoe UI",8),
                  relief="flat", borderwidth=0, cursor="hand2", command=self._show_home_view).pack(side="right", padx=16)
        tab_bar = tk.Frame(self._main_area, bg=BG_CHAT); tab_bar.pack(fill="x", padx=24, pady=(12,0))
        content_area = tk.Frame(self._main_area, bg=BG_CHAT); content_area.pack(fill="both", expand=True, padx=24, pady=12)
        tab_frames = {}; tab_btns = {}
        def switch_tab(name):
            for n, frm in tab_frames.items(): frm.pack_forget()
            for n, btn in tab_btns.items(): btn.config(bg=BG_CHAT, fg=TEXT_MUTED)
            tab_frames[name].pack(fill="both", expand=True)
            tab_btns[name].config(bg="#2B2D31", fg=TEXT_WHITE)
        def make_tab(label):
            btn = tk.Button(tab_bar, text=label, bg=BG_CHAT, fg=TEXT_MUTED, font=("Segoe UI",9,"bold"),
                            relief="flat", borderwidth=0, padx=14, cursor="hand2")
            btn.pack(side="left", ipady=7, padx=(0,4))
            frm = tk.Frame(content_area, bg=BG_CHAT)
            tab_frames[label] = frm; tab_btns[label] = btn
            btn.config(command=lambda n=label: switch_tab(n)); return frm
        create_frm = make_tab("Create")
        tk.Label(create_frm, text="SERVER NAME", fg=TEXT_MUTED, bg=BG_CHAT,
                 font=("Segoe UI",8,"bold")).pack(anchor="w", pady=(16,4))
        name_row = tk.Frame(create_frm, bg=BG_INPUT, highlightthickness=1, highlightbackground="#444"); name_row.pack(fill="x", ipadx=4)
        name_entry = tk.Entry(name_row, bg=BG_INPUT, fg=TEXT_WHITE, font=("Segoe UI",10), relief="flat", borderwidth=0, insertbackground="white")
        name_entry.pack(fill="x", ipady=12, padx=12)
        create_err = tk.Label(create_frm, text="", fg=ACCENT_RED, bg=BG_CHAT, font=("Segoe UI",8)); create_err.pack(anchor="w", pady=(10,0))
        def do_create():
            name = name_entry.get().strip()
            if not name: create_err.config(text="Please enter a server name."); return
            self._safe_send({"type":"create_server","name":name}); self._show_home_view()
        name_entry.bind("<Return>", lambda e: do_create())
        tk.Button(create_frm, text="CREATE SERVER ➜", bg=ACCENT_GREEN, fg=TEXT_WHITE, font=("Segoe UI",10,"bold"),
                  relief="flat", borderwidth=0, cursor="hand2", command=do_create).pack(anchor="w", pady=(16,0), ipady=10, ipadx=16)
        join_frm = make_tab("Join via Code")
        code_row = tk.Frame(join_frm, bg=BG_INPUT, highlightthickness=1, highlightbackground="#444"); code_row.pack(fill="x")
        code_entry = tk.Entry(code_row, bg=BG_INPUT, fg=TEXT_WHITE, font=("Segoe UI",16),
                              relief="flat", borderwidth=0, insertbackground="white")
        code_entry.pack(side="left", fill="x", expand=True, ipady=14, padx=16)
        join_err = tk.Label(join_frm, text="", fg=ACCENT_RED, bg=BG_CHAT, font=("Segoe UI",8)); join_err.pack(anchor="w", pady=(6,0))
        def do_join():
            code = code_entry.get().strip()
            if not code: join_err.config(text="Please enter an invite code."); return
            self._safe_send({"type":"join_server","code":code}); self._show_home_view()
        code_entry.bind("<Return>", lambda e: do_join())
        tk.Button(join_frm, text="JOIN SERVER ➜", bg=ACCENT_BLUE, fg=TEXT_WHITE, font=("Segoe UI",10,"bold"),
                  relief="flat", borderwidth=0, cursor="hand2", command=do_join).pack(anchor="w", pady=(14,0), ipady=10, ipadx=16)
        switch_tab("Create")

    def _create_channel_dialog(self, srv):
        name = simpledialog.askstring("New Channel", "Enter channel name (no spaces):")
        if name: self._safe_send({"type":"create_channel","server":srv,"channel":name})

    def _delete_channel(self, chan):
        if chan == "general":
            messagebox.showwarning("Can't Delete", "The #general channel cannot be deleted."); return
        if messagebox.askyesno("Delete Channel", f"Delete #{chan}?"):
            self._safe_send({"type":"delete_channel","server":self.server,"channel":chan})
            if self.channel == chan: self.channel = "general"

    def _server_ctx_menu(self, event, srv):
        menu = Menu(self.root, tearoff=0, bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_REG)
        menu.add_command(label="  📋  Copy Invite Code", command=lambda: self._copy_invite(srv))
        menu.add_separator()
        menu.add_command(label="  🗑  Delete Server",     command=lambda: self._delete_server(srv))
        menu.post(event.x_root, event.y_root)

    def _copy_invite(self, srv):
        code = self.servers_data.get(srv,{}).get("invite","")
        if code:
            self.root.clipboard_clear(); self.root.clipboard_append(code)
            self._toast("📋 Copied!", f"Invite code: {code}", ACCENT_BLUE)

    def _delete_server(self, srv):
        if srv in ["Main Server","Dev Room","Gaming"]:
            messagebox.showwarning("Can't Delete", "Default servers cannot be deleted."); return
        if messagebox.askyesno("Delete Server", f"Delete '{srv}'?"):
            self._safe_send({"type":"delete_server","server":srv})

    def render_server_list(self):
        if getattr(self, '_render_srv_pending', False): return
        self._render_srv_pending = True
        self.root.after(150, self._do_render_server_list)

    def _do_render_server_list(self):
        self._render_srv_pending = False
        self._build_rail()
        if self.current_view == VIEW_SERVER: self._build_channel_list_panel()
        total = sum(self.unread_counts.values()) + sum(self.dm_unreads.values())
        self.root.title(f"({total}) Chat_Room" if total > 0 else "Chat_Room")

    def _member_row(self, parent, u, online=True):
        row = tk.Frame(parent, bg=BG_SIDEBAR, cursor="hand2"); row.pack(fill="x", padx=8, pady=1)
        pfp_frame = tk.Frame(row, bg=BG_SIDEBAR); pfp_frame.pack(side="left", padx=(6,6), pady=4)
        tk_img = self.get_pil_pfp(u, size=(28,28)); row._img = tk_img
        tk.Label(pfp_frame, image=tk_img, bg=BG_SIDEBAR).pack()
        tk.Label(pfp_frame, text="●", fg="#3BA55D" if online else "#72767D",
                 bg=BG_SIDEBAR, font=("Segoe UI",6)).place(relx=0.65, rely=0.65)
        name_lbl = tk.Label(row, text=u, fg=user_color(u) if online else TEXT_MUTED,
                            bg=BG_SIDEBAR, font=("Segoe UI",9,"bold"), anchor="w")
        name_lbl.pack(side="left", fill="x", expand=True)
        if u != self.username:
            dm_btn = tk.Button(row, text="✉", bg=BG_SIDEBAR, fg=TEXT_MUTED, font=("Segoe UI",10),
                               relief="flat", borderwidth=0, cursor="hand2", command=lambda n=u: self._open_dm(n))
            dm_btn.pack(side="right", padx=4); dm_btn.pack_forget()
            def show_dm(e, b=dm_btn): b.pack(side="right", padx=4)
            def hide_dm(e, b=dm_btn): b.pack_forget()
            row.bind("<Enter>", show_dm); row.bind("<Leave>", hide_dm)
            name_lbl.bind("<Enter>", show_dm); name_lbl.bind("<Leave>", hide_dm)

    def update_users(self, users):
        self.online_users = set(users)
        if getattr(self, '_update_users_pending', False): return
        self._update_users_pending = True
        self.root.after(150, lambda ul=list(users): self._do_update_users(ul))

    def _do_update_users(self, users):
        self._update_users_pending = False
        if not hasattr(self,'user_container'): return
        try:
            if not self.user_container.winfo_exists(): return
        except: return
        for w in self.user_container.winfo_children(): w.destroy()
        for u in users:
            self._member_row(self.user_container, u, online=True)

    # ── RECEIVE LOOP ──────────────────────────────────────────────────────

    def receive(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
                if not chunk: break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip(): continue
                    try: data = json.loads(line.decode("utf-8", errors="ignore"))
                    except: continue
                    if not isinstance(data, dict): continue
                    t = data.get("type","")

                    if t == "channel_message":
                        ts = data["server"]; tc = data["channel"]; key = f"{ts}:{tc}"; msg = data["data"]
                        if key not in self.chat_history: self.chat_history[key] = []
                        self.chat_history[key].append((msg["from"],msg["content"],msg["time"],msg.get("id",""),msg.get("image",""),msg.get("edited",False)))
                        if ts != self.server or self.current_view != VIEW_SERVER:
                            self.unread_counts[ts] = self.unread_counts.get(ts,0)+1
                            self.root.bell()
                            self.root.after(0, self.render_server_list)
                            self.root.after(0, lambda m=msg, s=ts: self._toast(f"#{s}", f"{m['from']}: {m['content'][:40]}"))
                        else:
                            try: self._safe_send({"type":"mark_read","server":ts})
                            except: pass
                            if tc == self.channel: self.root.after(0, self.load_history)

                    elif t == "dm":
                        sender = data["from"]; content = data["content"]
                        ts_ = data.get("time", self._now()); image = data.get("image",""); msg_id = data.get("msg_id","")
                        key = f"dm:{sender}"
                        if key not in self.chat_history: self.chat_history[key] = []
                        self.chat_history[key].append((sender,content,ts_,msg_id,image,False))
                        if self.current_view==VIEW_DM and self.current_dm_user==sender:
                            self._safe_send({"type":"dm_read","from":sender})
                            self.root.after(0, self.load_history)
                        else:
                            self.dm_unreads[sender] = self.dm_unreads.get(sender,0)+1
                            self.root.bell()
                            self.root.after(0, self.render_server_list)
                            self.root.after(0, self.render_friends)
                            self.root.after(0, lambda s=sender, c=content: self._toast(f"DM from {s}", c[:50], ACCENT_PURPLE))

                    elif t == "message_edited":
                        key = data["key"]; mid = data["msg_id"]; new_c = data["content"]
                        msgs = self.chat_history.get(key,[])
                        for i, m in enumerate(msgs):
                            if m[3] == mid: msgs[i] = (m[0],new_c,m[2],m[3],m[4],True)
                        if self._current_key() == key: self.root.after(0, self.load_history)

                    elif t == "message_deleted":
                        key = data["key"]; mid = data["msg_id"]
                        if key in self.chat_history:
                            self.chat_history[key] = [m for m in self.chat_history[key] if m[3]!=mid]
                        if self._current_key() == key: self.root.after(0, self.load_history)

                    elif t == "typing":
                        ctx = data["context"]; user = data["user"]; ist = data.get("typing",True)
                        self._typing_who.setdefault(ctx, set())
                        if ist:
                            self._typing_who[ctx].add(user)
                            timer_key = (ctx, user)
                            if timer_key in self._typing_timers: self.root.after_cancel(self._typing_timers[timer_key])
                            self._typing_timers[timer_key] = self.root.after(TYPING_TIMEOUT+500, lambda c=ctx, u=user: self._clear_typing(c, u))
                        else:
                            self._typing_who[ctx].discard(user)
                        self.root.after(0, lambda c=ctx: self._update_typing_label(c))

                    elif t == "dm_read_receipt":
                        by = data.get("by","")
                        self._last_dm_read[by] = True
                        if self.current_view == VIEW_DM: self.root.after(0, self.load_history)

                    elif t == "voice_state":
                        self.voice_state = data["state"]
                        self.root.after(0, self._refresh_voice_ui)

                    elif t == "voice_joined":
                        pass  # confirmed

                    elif t == "voice_kicked":
                        reason = data.get("reason","Channel removed")
                        if self.voice_engine: self.voice_engine.stop(); self.voice_engine = None
                        self.current_voice = None
                        self.root.after(0, self._remove_voice_bar)
                        self.root.after(0, self._refresh_voice_ui)
                        self.root.after(0, lambda r=reason: self._toast("🔇 Disconnected", r, ACCENT_RED))

                    elif t == "server_update":
                        self.servers_data = data["servers"]; self.available_servers = list(data["servers"])
                        self.root.after(0, self.render_server_list)

                    elif t == "channel_deleted":
                        srv = data["server"]; chan = data["channel"]
                        if self.current_view==VIEW_SERVER and self.server==srv and self.channel==chan:
                            self.channel = "general"
                            self.root.after(0, self._build_channel_list_panel)
                            self.root.after(0, self._build_server_chat)
                        elif self.current_view==VIEW_SERVER and self.server==srv:
                            self.root.after(0, self._build_channel_list_panel)

                    elif t == "joined_server":
                        self.root.after(0, lambda n=data["name"]: self._toast("Joined Server!", n, ACCENT_GREEN))

                    elif t == "user_list":
                        self.root.after(0, lambda d=data: self.update_users(d["users"]))

                    elif t == "friend_update":
                        self.friends = set(data["friends"]); self.pending_requests = set(data.get("pending",[]))
                        if "dm_history" in data:
                            for k, msgs in data["dm_history"].items():
                                self.chat_history[k] = [(m["from"],m["content"],m.get("time",""),m.get("id",""),m.get("image",""),m.get("edited",False)) for m in msgs]
                        self.root.after(0, self.render_friends)

                    elif t == "block_update":
                        self.blocked = set(data.get("blocked",[])); self.friends = set(data.get("friends",[]))
                        self.root.after(0, self.render_friends)

                    elif t == "pfp_updated":
                        self.user_pfps[data["user"]] = data["pfp"]
                        self.root.after(0, self.load_history)

                    elif t == "server_deleted":
                        srv = data["server"]
                        if srv in self.available_servers: self.available_servers.remove(srv)
                        self.servers_data.pop(srv, None); self.unread_counts.pop(srv, None)
                        if self.current_view == VIEW_SERVER and self.server == srv:
                            self.root.after(0, self._show_home_view)
                        else:
                            self.root.after(0, self.render_server_list)

                    elif t == "error":
                        self.root.after(0, lambda m=data["msg"]: self._toast("Error", m, ACCENT_RED))

                    elif t == "success":
                        self.root.after(0, lambda m=data.get("msg",""): self._toast("✓ Success", m, ACCENT_GREEN))

                    elif t == "friend_request":
                        frm = data.get("from","")
                        self.pending_requests.add(frm)
                        self.root.after(0, lambda name=frm: self._show_friend_request_toast(name))
                        self.root.after(0, self.render_friends)

                    elif t == "username_changed":
                        old_name = self.username; self.username = data["new_username"]
                        self.user_pfps[self.username] = self.user_pfps.pop(old_name, "")
                        self.root.after(0, lambda: self._toast("✓ Username Changed", f"You are now {self.username}", ACCENT_GREEN))

                    elif t == "server_invite":
                        frm = data["from"]; srv = data["server"]; code = data["code"]
                        self.root.after(0, lambda f=frm, s=srv, c=code: self._show_server_invite_toast(f, s, c))

            except Exception as e:
                print(f"[Receive error] {e}")
                break

    def _clear_typing(self, context, user):
        self._typing_who.get(context, set()).discard(user)
        self._update_typing_label(context)

    # ── SETTINGS ──────────────────────────────────────────────────────────

    def toggle_settings(self):
        if self._settings_visible: self._close_settings()
        else: self._open_settings()

    def _open_settings(self):
        self._settings_visible = True
        if self._settings_btn: self._settings_btn.config(text="✕", fg=ACCENT_RED)
        if self.chat_panel: self.chat_panel.pack_forget()
        self._settings_frame = tk.Frame(self._main_area, bg="#0e0e10")
        self._build_settings_content(self._settings_frame)
        self._main_area.update_idletasks()
        w = self._main_area.winfo_width(); h = self._main_area.winfo_height()
        self._settings_frame.place(x=w, y=0, width=w, height=h)
        self._animate_settings_slide(w, 0, w)

    def _animate_settings_slide(self, cur, tgt, tw):
        if cur <= tgt:
            self._settings_frame.place(x=tgt, y=0, width=tw, height=self._main_area.winfo_height()); return
        spd = max(18, int((cur-tgt)*0.35)); newx = max(tgt, cur-spd)
        self._settings_frame.place(x=newx, y=0, width=tw, height=self._main_area.winfo_height())
        self.root.after(12, lambda: self._animate_settings_slide(newx, tgt, tw))

    def _close_settings(self):
        self._settings_visible = False
        if self._settings_btn: self._settings_btn.config(text="⚙", fg=TEXT_MUTED)
        if self._settings_frame:
            tw = self._main_area.winfo_width()
            self._animate_settings_close(0, tw, tw)
        elif self.chat_panel: self.chat_panel.pack(fill="both", expand=True)

    def _animate_settings_close(self, cur, tgt, tw):
        if cur >= tgt:
            if self._settings_frame: self._settings_frame.destroy(); self._settings_frame = None
            if self.chat_panel: self.chat_panel.pack(fill="both", expand=True); return
        spd = max(18, int((tgt-cur)*0.35)); newx = min(tgt, cur+spd)
        if self._settings_frame:
            self._settings_frame.place(x=newx, y=0, width=tw, height=self._main_area.winfo_height())
        self.root.after(12, lambda: self._animate_settings_close(newx, tgt, tw))

    def _build_settings_content(self, parent):
        header = tk.Frame(parent, bg="#0a0a0c", height=60); header.pack(fill="x"); header.pack_propagate(False)
        tk.Label(header, text="USER SETTINGS", fg=TEXT_WHITE, bg="#0a0a0c",
                 font=("Segoe UI",14,"bold"), pady=15, padx=30).pack(side="left")
        tk.Label(header, text=f"Logged in as  {self.username}", fg=TEXT_MUTED,
                 bg="#0a0a0c", font=("Segoe UI",9), padx=30).pack(side="right", pady=15)
        tk.Frame(parent, bg=ACCENT_PURPLE, height=2).pack(fill="x")
        body = tk.Frame(parent, bg="#0e0e10"); body.pack(fill="both", expand=True)
        nav  = tk.Frame(body, bg="#0a0a0c", width=200); nav.pack(side="left", fill="y"); nav.pack_propagate(False)
        content_area = tk.Frame(body, bg="#0e0e10"); content_area.pack(side="left", fill="both", expand=True)
        def make_nav_btn(label, page_fn):
            btn = tk.Button(nav, text=label, bg="#0a0a0c", fg=TEXT_MUTED, font=("Segoe UI",9),
                            borderwidth=0, relief="flat", anchor="w", padx=20, cursor="hand2",
                            activebackground="#1a1a1e", activeforeground=TEXT_WHITE)
            def on_click(b=btn, fn=page_fn):
                for c in nav.winfo_children():
                    if isinstance(c, tk.Button): c.config(bg="#0a0a0c", fg=TEXT_MUTED, font=("Segoe UI",9))
                b.config(bg="#1e1e24", fg=TEXT_WHITE, font=("Segoe UI",9,"bold"))
                for w in content_area.winfo_children(): w.destroy()
                fn(content_area)
            btn.config(command=on_click); btn.pack(fill="x", ipady=10); return btn, on_click
        tk.Label(nav, text="ACCOUNT", fg="#5636a7", bg="#0a0a0c", font=("Segoe UI",8,"bold"), padx=20, pady=12).pack(anchor="w")
        profile_btn, profile_fn = make_nav_btn("  My Profile",     self._settings_page_profile)
        _,           _          = make_nav_btn("  Change Password", self._settings_page_password)
        tk.Label(nav, text="VOICE", fg="#5636a7", bg="#0a0a0c", font=("Segoe UI",8,"bold"), padx=20, pady=12).pack(anchor="w")
        _,           _          = make_nav_btn("  Voice Settings",  self._settings_page_voice)
        tk.Label(nav, text="ABOUT", fg="#5636a7", bg="#0a0a0c", font=("Segoe UI",8,"bold"), padx=20, pady=12).pack(anchor="w")
        _,           _          = make_nav_btn("  App Info",        self._settings_page_about)
        profile_fn()

    def _section_title(self, parent, text):
        tk.Label(parent, text=text, fg=TEXT_WHITE, bg="#0e0e10",
                 font=("Segoe UI",13,"bold")).pack(anchor="w", padx=40, pady=(30,4))
        tk.Frame(parent, bg="#2a2a30", height=1).pack(fill="x", padx=40, pady=(0,20))

    def _card(self, parent, title, subtitle=None):
        card = tk.Frame(parent, bg="#18181e", highlightthickness=1, highlightbackground="#2d2d38")
        card.pack(fill="x", padx=40, pady=6)
        inner = tk.Frame(card, bg="#18181e"); inner.pack(fill="x", padx=20, pady=16)
        tk.Label(inner, text=title, fg=TEXT_WHITE, bg="#18181e", font=("Segoe UI",10,"bold")).pack(anchor="w")
        if subtitle:
            tk.Label(inner, text=subtitle, fg=TEXT_MUTED, bg="#18181e", font=("Segoe UI",8), wraplength=500, justify="left").pack(anchor="w", pady=(3,0))
        return inner

    def _settings_page_profile(self, parent):
        self._section_title(parent, "My Profile")
        preview = tk.Frame(parent, bg="#18181e", highlightthickness=1, highlightbackground="#2d2d38")
        preview.pack(fill="x", padx=40, pady=6)
        tk.Frame(preview, bg="#9146FF", height=80).pack(fill="x")
        af = tk.Frame(preview, bg="#18181e"); af.pack(fill="x", padx=20)
        self._settings_pfp_label = tk.Label(af, bg="#18181e", cursor="hand2"); self._settings_pfp_label.pack(side="left", pady=8)
        self._refresh_settings_pfp_preview()
        self._settings_pfp_label.bind("<Button-1>", lambda e: self._upload_pfp_and_refresh())
        info = tk.Frame(af, bg="#18181e"); info.pack(side="left", padx=15, pady=14)
        self._preview_name_lbl = tk.Label(info, text=self.username, fg=TEXT_WHITE, bg="#18181e", font=("Segoe UI",14,"bold"))
        self._preview_name_lbl.pack(anchor="w")
        tk.Label(info, text="Click avatar to change photo", fg=TEXT_MUTED, bg="#18181e", font=("Segoe UI",8)).pack(anchor="w")
        pfp_card = self._card(parent, "Profile Picture")
        tk.Button(pfp_card, text="📁  UPLOAD NEW PICTURE", bg=ACCENT_PURPLE, fg=TEXT_WHITE,
                  font=("Segoe UI",9,"bold"), relief="flat", borderwidth=0, padx=20, cursor="hand2",
                  command=self._upload_pfp_and_refresh).pack(anchor="w", pady=(10,0), ipady=8)
        uname_card = self._card(parent, "Change Username")
        tk.Label(uname_card, text="New Username", fg=TEXT_MUTED, bg="#18181e", font=("Segoe UI",8)).pack(anchor="w", pady=(10,2))
        uname_entry = tk.Entry(uname_card, bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_REG, relief="flat",
                               highlightthickness=1, highlightbackground="#333", insertbackground="white", width=30)
        uname_entry.pack(anchor="w", ipady=8); uname_entry.insert(0, self.username)
        tk.Label(uname_card, text="Current Password (required)", fg=TEXT_MUTED, bg="#18181e", font=("Segoe UI",8)).pack(anchor="w", pady=(10,2))
        uname_pw_entry = tk.Entry(uname_card, bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_REG, relief="flat",
                                  highlightthickness=1, highlightbackground="#333", insertbackground="white", show="*", width=30)
        uname_pw_entry.pack(anchor="w", ipady=8)
        uname_status = tk.Label(uname_card, text="", fg=ACCENT_GREEN, bg="#18181e", font=("Segoe UI",8)); uname_status.pack(anchor="w", pady=(4,0))
        def do_username_change():
            new_name = uname_entry.get().strip(); pw = uname_pw_entry.get()
            if not new_name or not pw: uname_status.config(text="All fields required.", fg=ACCENT_RED); return
            if new_name == self.username: uname_status.config(text="That's already your username!", fg=ACCENT_RED); return
            self._safe_send({"type":"change_username","new_username":new_name,"password":pw})
            uname_status.config(text="✔ Request sent...", fg=ACCENT_GREEN)
        tk.Button(uname_card, text="SAVE USERNAME", bg=ACCENT_BLUE, fg=TEXT_WHITE, font=("Segoe UI",9,"bold"),
                  relief="flat", borderwidth=0, padx=20, cursor="hand2", command=do_username_change).pack(anchor="w", pady=(12,0), ipady=8)

    def _refresh_settings_pfp_preview(self):
        try:
            tk_img = self.get_pil_pfp(self.username, size=(72,72))
            self._settings_pfp_label.config(image=tk_img); self._settings_pfp_label.image = tk_img
        except: pass

    def _upload_pfp_and_refresh(self):
        self.upload_pfp(); self.root.after(300, self._refresh_settings_pfp_preview)

    def _settings_page_password(self, parent):
        self._section_title(parent, "Change Password")
        card = self._card(parent, "Update Your Password")
        entries = {}
        for label, key in [("Current Password","old"),("New Password","new"),("Confirm","conf")]:
            tk.Label(card, text=label, fg=TEXT_MUTED, bg="#18181e", font=("Segoe UI",8)).pack(anchor="w", pady=(12,2))
            e = tk.Entry(card, bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_REG, show="*", relief="flat",
                         highlightthickness=1, highlightbackground="#333", insertbackground="white", width=36)
            e.pack(anchor="w", ipady=8); entries[key] = e
        status = tk.Label(card, text="", fg=ACCENT_RED, bg="#18181e", font=("Segoe UI",8)); status.pack(anchor="w", pady=(6,0))
        def do_change():
            old,new,conf = entries["old"].get(),entries["new"].get(),entries["conf"].get()
            if not old or not new: status.config(text="All fields required.", fg=ACCENT_RED); return
            if new!=conf: status.config(text="Passwords do not match.", fg=ACCENT_RED); return
            if len(new)<4: status.config(text="Min 4 characters.", fg=ACCENT_RED); return
            self._safe_send({"type":"change_password","old_password":old,"new_password":new})
            status.config(text="✔ Password change requested.", fg=ACCENT_GREEN)
        tk.Button(card, text="SAVE NEW PASSWORD", bg=ACCENT_BLUE, fg=TEXT_WHITE, font=("Segoe UI",9,"bold"),
                  relief="flat", borderwidth=0, padx=20, cursor="hand2", command=do_change).pack(anchor="w", pady=(14,0), ipady=8)

    def _settings_page_voice(self, parent):
        self._section_title(parent, "Voice Settings")
        status_text = "✅ PyAudio installed — voice available" if PYAUDIO_AVAILABLE else "❌ PyAudio not installed"
        status_color = ACCENT_GREEN if PYAUDIO_AVAILABLE else ACCENT_RED
        card = self._card(parent, "Voice Status", "PyAudio is required for voice channels.")
        tk.Label(card, text=status_text, fg=status_color, bg="#18181e", font=("Segoe UI",10,"bold")).pack(anchor="w", pady=(8,0))
        if not PYAUDIO_AVAILABLE:
            tk.Label(card, text="Install with:  pip install pyaudio", fg=TEXT_MUTED, bg="#18181e",
                     font=("Segoe UI",9,"italic")).pack(anchor="w", pady=(4,0))
        if self.current_voice:
            srv, chan = self.current_voice
            vc_card = self._card(parent, "Currently in Voice", f"{srv} › {chan}")
            btn_row = tk.Frame(vc_card, bg="#18181e"); btn_row.pack(anchor="w", pady=(10,0))
            mute_text = "🎤 Unmute" if self._voice_muted else "🎤 Mute"
            tk.Button(btn_row, text=mute_text, bg=ACCENT_RED if self._voice_muted else "#2B2D31",
                      fg=TEXT_WHITE, font=("Segoe UI",9,"bold"), relief="flat", borderwidth=0,
                      padx=10, cursor="hand2", command=self._toggle_mute).pack(side="left", padx=(0,8), ipady=6)
            deaf_text = "🔊 Undeafen" if self._voice_deafened else "🔇 Deafen"
            tk.Button(btn_row, text=deaf_text, bg=ACCENT_RED if self._voice_deafened else "#2B2D31",
                      fg=TEXT_WHITE, font=("Segoe UI",9,"bold"), relief="flat", borderwidth=0,
                      padx=10, cursor="hand2", command=self._toggle_deafen).pack(side="left", padx=(0,8), ipady=6)
            tk.Button(btn_row, text="✕ Disconnect", bg=ACCENT_RED, fg=TEXT_WHITE, font=("Segoe UI",9,"bold"),
                      relief="flat", borderwidth=0, padx=10, cursor="hand2", command=self.leave_voice).pack(side="left", ipady=6)
        else:
            self._card(parent, "Not in a Voice Channel", "Join a voice channel from a server's sidebar.")

    def _settings_page_about(self, parent):
        self._section_title(parent, "App Info")
        self._card(parent, "Chat_Room", "Python + Tkinter real-time chat with voice channels.")
        self._card(parent, "Connection", f"Server:  {HOST}:{PORT}\nVoice UDP:  {self.udp_port}\nUser:  {self.username}")
        self._card(parent, "Stack", "Python 3  ·  Tkinter  ·  Pillow  ·  PyAudio  ·  TCP + UDP Sockets  ·  JSON")
        voice_status = "✅ Voice enabled" if PYAUDIO_AVAILABLE else "❌ Voice disabled — pip install pyaudio"
        self._card(parent, "Voice", voice_status)

    def upload_pfp(self):
        filepath = filedialog.askopenfilename(filetypes=[("Image Files","*.png;*.jpg;*.jpeg;*.gif")])
        if not filepath: return
        try:
            img = Image.open(filepath); img.thumbnail((128,128), Image.Resampling.LANCZOS)
            buf = io.BytesIO(); img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            self.user_pfps[self.username] = b64
            self._safe_send({"type":"update_pfp","pfp":b64})
            self._toast("✓ Profile Picture", "Updated successfully!", ACCENT_GREEN)
            self.root.after(100, self._refresh_settings_pfp_preview)
            self.root.after(150, self.load_history)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _show_friend_request_toast(self, from_user):
        toast = tk.Toplevel(self.root); toast.overrideredirect(True); toast.attributes("-topmost", True)
        toast.configure(bg=BG_INPUT)
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        w, h = 340, 100; toast.geometry(f"{w}x{h}+{sw-w-16}+{sh-h-60}")
        tk.Frame(toast, bg=ACCENT_BLUE, width=4).pack(side="left", fill="y")
        body = tk.Frame(toast, bg=BG_INPUT); body.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        tk.Label(body, text=f"Friend Request from {from_user}", fg=TEXT_WHITE, bg=BG_INPUT, font=("Segoe UI",9,"bold")).pack(anchor="w")
        tk.Label(body, text="Wants to be your friend", fg=TEXT_MUTED, bg=BG_INPUT, font=("Segoe UI",8)).pack(anchor="w")
        btn_row = tk.Frame(body, bg=BG_INPUT); btn_row.pack(anchor="w", pady=(6,0))
        def accept():
            self._safe_send({"type":"friend_accept","from":from_user})
            self.pending_requests.discard(from_user); toast.destroy()
        def decline():
            self.pending_requests.discard(from_user); toast.destroy()
        tk.Button(btn_row, text="Accept", bg=ACCENT_GREEN, fg=TEXT_WHITE, font=("Segoe UI",8,"bold"),
                  relief="flat", borderwidth=0, padx=10, cursor="hand2", command=accept).pack(side="left", padx=(0,6), ipady=3)
        tk.Button(btn_row, text="Decline", bg="#2B2D31", fg=TEXT_MUTED, font=("Segoe UI",8,"bold"),
                  relief="flat", borderwidth=0, padx=10, cursor="hand2", command=decline).pack(side="left", ipady=3)
        toast.after(15000, lambda: toast.destroy() if toast.winfo_exists() else None)

    def _show_server_invite_toast(self, from_user, srv, code):
        toast = tk.Toplevel(self.root); toast.overrideredirect(True); toast.attributes("-topmost", True)
        toast.configure(bg=BG_INPUT)
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        w, h = 340, 100; toast.geometry(f"{w}x{h}+{sw-w-16}+{sh-h-60}")
        tk.Frame(toast, bg=ACCENT_GREEN, width=4).pack(side="left", fill="y")
        body = tk.Frame(toast, bg=BG_INPUT); body.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        tk.Label(body, text=f"Server Invite from {from_user}", fg=TEXT_WHITE, bg=BG_INPUT, font=("Segoe UI",9,"bold")).pack(anchor="w")
        tk.Label(body, text=f"Join: {srv}", fg=TEXT_MUTED, bg=BG_INPUT, font=("Segoe UI",8)).pack(anchor="w")
        btn_row = tk.Frame(body, bg=BG_INPUT); btn_row.pack(anchor="w", pady=(6,0))
        tk.Button(btn_row, text="Accept", bg=ACCENT_GREEN, fg=TEXT_WHITE, font=("Segoe UI",8,"bold"),
                  relief="flat", borderwidth=0, padx=10, cursor="hand2",
                  command=lambda: [self._safe_send({"type":"join_server","code":code}), toast.destroy()]).pack(side="left", padx=(0,6), ipady=3)
        tk.Button(btn_row, text="Decline", bg="#2B2D31", fg=TEXT_MUTED, font=("Segoe UI",8,"bold"),
                  relief="flat", borderwidth=0, padx=10, cursor="hand2", command=toast.destroy).pack(side="left", ipady=3)

    def logout(self):
        if self.current_voice: self.leave_voice(silent=True)
        try: self.sock.close()
        except: pass
        self.sock = None
        self.show_login()


if __name__ == "__main__":
    ChatClient().root.mainloop()