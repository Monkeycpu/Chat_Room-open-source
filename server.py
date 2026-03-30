import socket
import threading
import json
import os
import random
import string
from datetime import datetime
import time

try:
    import bcrypt
except ImportError:
    print("ERROR: Run 'pip3 install bcrypt --break-system-packages' first!")
    exit()

HOST = "0.0.0.0"
PORT = 5555
UDP_PORT = 5556   # Voice traffic on UDP

# ── SECURITY CONFIG ───────────────────────────────────────────────────────
SIGNUP_CODE      = "74683497"
MAX_MSG_SIZE     = 4194304      # 4MB max packet
MAX_MSGS_PER_WIN = 10
RATE_WINDOW      = 5
MAX_USERNAME_LEN = 32
MAX_PASSWORD_LEN = 128
MAX_CONTENT_LEN  = 2000
MAX_IMAGE_SIZE   = 2000000      # 2MB

ACCOUNTS_FILE = "accounts.json"
SERVERS_FILE  = "servers.json"
MESSAGES_FILE = "messages.json"
DMS_FILE      = "dms.json"


def load_json(file):
    if os.path.exists(file):
        with open(file, "r") as f:
            try:    return json.load(f)
            except: return {}
    return {}


def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)


accounts     = load_json(ACCOUNTS_FILE)
servers_data = load_json(SERVERS_FILE)
messages     = load_json(MESSAGES_FILE)
dms          = load_json(DMS_FILE)

if not servers_data:
    servers_data = {
        "Main Server": {"channels": ["general","coding"], "voice_channels": ["General","Gaming"], "members": [], "invite": "MAIN01"},
        "Dev Room":    {"channels": ["general"],          "voice_channels": ["General"],           "members": [], "invite": "DEV001"},
        "Gaming":      {"channels": ["general"],          "voice_channels": ["General","Squad"],    "members": [], "invite": "GAME01"}
    }
    save_json(SERVERS_FILE, servers_data)

# Migrate existing servers to have voice_channels
changed = False
for srv in servers_data:
    if "invite" not in servers_data[srv]:
        servers_data[srv]["invite"] = ''.join(random.choices(string.ascii_uppercase+string.digits, k=6))
        changed = True
    if "voice_channels" not in servers_data[srv]:
        servers_data[srv]["voice_channels"] = ["General"]
        changed = True
if changed:
    save_json(SERVERS_FILE, servers_data)

user_sockets    = {}   # username -> TCP socket
rate_tracker    = {}   # username -> [timestamps]

# ── VOICE STATE ────────────────────────────────────────────────────────────
# voice_rooms[server][channel] = {username: udp_addr}
voice_rooms     = {}
# Maps username -> (server, channel) they are currently in
voice_active    = {}
# UDP addr -> username  (registered when client sends voice_register)
udp_addr_map    = {}

voice_lock = threading.Lock()


# ── SECURITY HELPERS ──────────────────────────────────────────────────────

def hash_password(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_password(pw, stored):
    try:
        if len(stored) == 64 and not stored.startswith("$2b$"):
            import hashlib
            return hashlib.sha256(pw.encode()).hexdigest() == stored
        return bcrypt.checkpw(pw.encode(), stored.encode())
    except:
        return False


def upgrade_hash_if_needed(username, pw):
    stored = accounts[username]["password"]
    if len(stored) == 64 and not stored.startswith("$2b$"):
        import hashlib
        if hashlib.sha256(pw.encode()).hexdigest() == stored:
            accounts[username]["password"] = hash_password(pw)
            save_json(ACCOUNTS_FILE, accounts)


def rate_limited(username):
    now = time.time()
    rate_tracker.setdefault(username, [])
    rate_tracker[username] = [t for t in rate_tracker[username] if now - t < RATE_WINDOW]
    if len(rate_tracker[username]) >= MAX_MSGS_PER_WIN:
        return True
    rate_tracker[username].append(now)
    return False


def clean(text, max_len=MAX_CONTENT_LEN):
    if not isinstance(text, str): return ""
    return text.strip()[:max_len]


# ── CORE HELPERS ──────────────────────────────────────────────────────────

def dm_key(a, b):
    return ":".join(sorted([a, b]))


def dm_history_for(username):
    result = {}
    for friend in accounts[username].get("friends", []):
        k = dm_key(username, friend)
        result[f"dm:{friend}"] = dms.get(k, [])
    return result


def unread_counts(username):
    counts    = {}
    user_seen = accounts[username].get("seen_indices", {})
    for srv in servers_data:
        history = messages.get(f"{srv}:general", [])
        counts[srv] = max(0, len(history) - user_seen.get(srv, 0))
    return counts


def send_to(sock, data):
    try:
        sock.send((json.dumps(data) + "\n").encode())
    except:
        pass


def broadcast_server(srv, packet, exclude=None):
    for user in servers_data.get(srv, {}).get("members", []):
        if user != exclude and user in user_sockets:
            send_to(user_sockets[user], packet)


def broadcast_all(packet, exclude=None):
    for u, s in list(user_sockets.items()):
        if u != exclude:
            send_to(s, packet)


def update_user_list():
    online = list(user_sockets.keys())
    for s in user_sockets.values():
        send_to(s, {"type": "user_list", "users": online})


def broadcast_voice_state():
    """Send current voice room occupancy to all online users."""
    state = {}
    with voice_lock:
        for srv, chans in voice_rooms.items():
            state[srv] = {}
            for chan, users in chans.items():
                state[srv][chan] = list(users.keys())
    broadcast_all({"type": "voice_state", "state": state})


def new_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))


# ── UDP VOICE RELAY ────────────────────────────────────────────────────────

def udp_relay_loop():
    """Receive audio packets from clients and relay to others in same voice channel."""
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_sock.bind((HOST, UDP_PORT))
    print(f"🎤 UDP Voice relay listening on :{UDP_PORT}")

    recv_total = 0
    while True:
        try:
            data, addr = udp_sock.recvfrom(131072)
            if len(data) < 2:
                continue

            ulen = data[0]
            if len(data) < 1 + ulen:
                continue
            username = data[1:1+ulen].decode("utf-8", errors="ignore")
            audio    = data[1+ulen:]

            recv_total += 1
            if recv_total % 50 == 0:
                print(f"[UDP] {recv_total} packets received. Last from: {username} @ {addr}")

            # Register addr mapping
            with voice_lock:
                udp_addr_map[addr] = username
                if username in voice_active:
                    srv, chan = voice_active[username]
                    # Update stored addr for this user
                    if srv in voice_rooms and chan in voice_rooms[srv]:
                        voice_rooms[srv][chan][username] = addr
                    # Relay to all users in same channel including sender (loopback)
                    targets = {
                        u: a
                        for u, a in voice_rooms.get(srv, {}).get(chan, {}).items()
                        if a is not None
                    }

            for target_user, target_addr in targets.items():
                try:
                    # Prepend sender name so client knows who's speaking
                    name_bytes = username.encode("utf-8")
                    packet = bytes([len(name_bytes)]) + name_bytes + audio
                    udp_sock.sendto(packet, target_addr)
                except:
                    pass
        except Exception as e:
            print(f"[UDP ERROR] {e}")


# ── CLIENT HANDLER ────────────────────────────────────────────────────────

def handle_client(sock):
    username = None
    buf = b""
    try:
        while True:
            chunk = sock.recv(MAX_MSG_SIZE)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    data = json.loads(line.decode("utf-8", errors="ignore"))
                except:
                    continue
                if not isinstance(data, dict):
                    continue
                t = data.get("type", "")

                # ── SIGNUP ────────────────────────────────────────────
                if t == "signup":
                    if clean(data.get("code", ""), 32) != SIGNUP_CODE:
                        send_to(sock, {"type": "error", "msg": "Invalid signup code"})
                        return
                    uname = clean(data.get("username", ""), MAX_USERNAME_LEN)
                    pw    = data.get("password", "")
                    if len(uname) < 2 or " " in uname:
                        send_to(sock, {"type": "error", "msg": "Username must be 2+ chars, no spaces"})
                        return
                    if not uname.replace("_","").replace("-","").isalnum():
                        send_to(sock, {"type": "error", "msg": "Only letters, numbers, _ and - allowed"})
                        return
                    if uname in accounts:
                        send_to(sock, {"type": "error", "msg": "Username already taken"})
                        return
                    if not pw or len(pw) < 4 or len(pw) > MAX_PASSWORD_LEN:
                        send_to(sock, {"type": "error", "msg": "Password must be 4–128 characters"})
                        return
                    accounts[uname] = {
                        "password":     hash_password(pw),
                        "seen_indices": {srv: 0 for srv in servers_data},
                        "friends":      [], "pending": [], "blocked": [], "pfp": ""
                    }
                    save_json(ACCOUNTS_FILE, accounts)
                    send_to(sock, {"type": "success", "msg": "Account created! You can now log in."})
                    return

                # ── LOGIN ─────────────────────────────────────────────
                if t == "login":
                    user = clean(data.get("username", ""), MAX_USERNAME_LEN)
                    pw   = data.get("password", "")
                    if user not in accounts or not check_password(pw, accounts[user]["password"]):
                        send_to(sock, {"type": "error", "msg": "Wrong username or password"})
                        return
                    upgrade_hash_if_needed(user, pw)
                    username = user
                    user_sockets[username] = sock
                    acc = accounts[username]
                    for field, default in [("seen_indices",{}),("pfp",""),("pending",[]),("blocked",[])]:
                        if field not in acc:
                            acc[field] = default
                    if not acc["seen_indices"]:
                        acc["seen_indices"] = {srv: 0 for srv in servers_data}
                    for s in servers_data:
                        if username not in servers_data[s]["members"]:
                            servers_data[s]["members"].append(username)
                    save_json(SERVERS_FILE, servers_data)
                    save_json(ACCOUNTS_FILE, accounts)
                    full_history = dict(messages)
                    full_history.update(dm_history_for(username))

                    # Build current voice state
                    v_state = {}
                    with voice_lock:
                        for srv, chans in voice_rooms.items():
                            v_state[srv] = {chan: list(users.keys()) for chan, users in chans.items()}

                    send_to(sock, {
                        "type":       "login_success",
                        "servers":    servers_data,
                        "history":    full_history,
                        "unreads":    unread_counts(username),
                        "friends":    acc.get("friends", []),
                        "pending":    acc.get("pending", []),
                        "blocked":    acc.get("blocked", []),
                        "pfps":       {u: accounts[u].get("pfp", "") for u in accounts},
                        "dm_unreads": {},
                        "all_users":  [u for u in accounts if u != username],
                        "voice_state": v_state,
                        "udp_port":   UDP_PORT,
                    })
                    update_user_list()

                elif t == "voice_join":
                    srv  = clean(data.get("server", ""), 64)
                    chan = clean(data.get("channel", ""), 64)
                    if srv not in servers_data:
                        continue
                    if chan not in servers_data[srv].get("voice_channels", []):
                        continue
                    with voice_lock:
                        # Leave current channel if in one
                        if username in voice_active:
                            old_srv, old_chan = voice_active[username]
                            if old_srv in voice_rooms and old_chan in voice_rooms[old_srv]:
                                voice_rooms[old_srv][old_chan].pop(username, None)
                        # Join new channel
                        voice_rooms.setdefault(srv, {}).setdefault(chan, {})
                        voice_rooms[srv][chan][username] = None  # addr registered via UDP
                        voice_active[username] = (srv, chan)
                    broadcast_voice_state()
                    send_to(sock, {"type": "voice_joined", "server": srv, "channel": chan})

                elif t == "voice_leave":
                    with voice_lock:
                        if username in voice_active:
                            old_srv, old_chan = voice_active.pop(username)
                            if old_srv in voice_rooms and old_chan in voice_rooms[old_srv]:
                                voice_rooms[old_srv][old_chan].pop(username, None)
                    broadcast_voice_state()

                elif t == "create_voice_channel":
                    srv  = clean(data.get("server", ""), 64)
                    chan = clean(data.get("channel", ""), 64).replace(" ", "_")
                    if srv in servers_data and chan:
                        vc = servers_data[srv].setdefault("voice_channels", [])
                        if chan not in vc:
                            vc.append(chan)
                            save_json(SERVERS_FILE, servers_data)
                            broadcast_all({"type": "server_update", "servers": servers_data})

                elif t == "delete_voice_channel":
                    srv  = clean(data.get("server", ""), 64)
                    chan = clean(data.get("channel", ""), 64)
                    if srv in servers_data and chan != "General":
                        vc = servers_data[srv].get("voice_channels", [])
                        if chan in vc:
                            vc.remove(chan)
                            save_json(SERVERS_FILE, servers_data)
                            # Kick anyone in that channel
                            with voice_lock:
                                kicked = list(voice_rooms.get(srv, {}).get(chan, {}).keys())
                                voice_rooms.get(srv, {}).pop(chan, None)
                                for ku in kicked:
                                    voice_active.pop(ku, None)
                                    if ku in user_sockets:
                                        send_to(user_sockets[ku], {"type": "voice_kicked", "reason": "Channel deleted"})
                            broadcast_all({"type": "server_update", "servers": servers_data})
                            broadcast_voice_state()

                elif t == "update_pfp":
                    pfp = data.get("pfp", "")
                    if len(pfp) > 500000:
                        send_to(sock, {"type": "error", "msg": "Image too large"})
                        continue
                    accounts[username]["pfp"] = pfp
                    save_json(ACCOUNTS_FILE, accounts)
                    broadcast_all({"type": "pfp_updated", "user": username, "pfp": pfp})

                elif t == "channel_message":
                    if rate_limited(username):
                        send_to(sock, {"type": "error", "msg": "Slow down!"})
                        continue
                    srv     = clean(data.get("server", ""), 64)
                    chan    = clean(data.get("channel", ""), 64)
                    content = clean(data.get("content", ""), MAX_CONTENT_LEN)
                    image   = data.get("image", "")
                    if srv not in servers_data: continue
                    if not content and not image: continue
                    if len(image) > MAX_IMAGE_SIZE: continue
                    msg_obj = {
                        "id":      new_id(),
                        "from":    username,
                        "content": content,
                        "time":    datetime.now().strftime("%I:%M %p"),
                        "image":   image,
                        "edited":  False
                    }
                    key = f"{srv}:{chan}"
                    messages.setdefault(key, []).append(msg_obj)
                    save_json(MESSAGES_FILE, messages)
                    broadcast_server(srv, {"type": "channel_message", "server": srv, "channel": chan, "data": msg_obj})

                elif t == "edit_message":
                    key  = clean(data.get("key", ""), 128)
                    mid  = clean(data.get("msg_id", ""), 16)
                    newc = clean(data.get("content", ""), MAX_CONTENT_LEN)
                    if not newc: continue
                    if key.startswith("dm:"):
                        other = key[3:]
                        dkey  = dm_key(username, other)
                        for m in dms.get(dkey, []):
                            if m.get("id") == mid and m["from"] == username:
                                m["content"] = newc
                                m["edited"]  = True
                        save_json(DMS_FILE, dms)
                        for u in [username, other]:
                            if u in user_sockets:
                                send_to(user_sockets[u], {"type": "message_edited", "key": key, "msg_id": mid, "content": newc})
                    else:
                        for m in messages.get(key, []):
                            if m.get("id") == mid and m["from"] == username:
                                m["content"] = newc
                                m["edited"]  = True
                        save_json(MESSAGES_FILE, messages)
                        srv = key.split(":")[0]
                        if srv in servers_data:
                            broadcast_server(srv, {"type": "message_edited", "key": key, "msg_id": mid, "content": newc})

                elif t == "delete_message":
                    key = clean(data.get("key", ""), 128)
                    mid = clean(data.get("msg_id", ""), 16)
                    if key.startswith("dm:"):
                        other = key[3:]
                        dkey  = dm_key(username, other)
                        dms[dkey] = [m for m in dms.get(dkey, []) if m.get("id") != mid]
                        save_json(DMS_FILE, dms)
                        for u in [username, other]:
                            if u in user_sockets:
                                send_to(user_sockets[u], {"type": "message_deleted", "key": key, "msg_id": mid})
                    else:
                        messages[key] = [m for m in messages.get(key, []) if m.get("id") != mid]
                        save_json(MESSAGES_FILE, messages)
                        srv = key.split(":")[0]
                        if srv in servers_data:
                            broadcast_server(srv, {"type": "message_deleted", "key": key, "msg_id": mid})

                elif t == "dm":
                    if rate_limited(username):
                        send_to(sock, {"type": "error", "msg": "Slow down!"})
                        continue
                    to      = clean(data.get("to", ""), MAX_USERNAME_LEN)
                    content = clean(data.get("content", ""), MAX_CONTENT_LEN)
                    image   = data.get("image", "")
                    if not to or (not content and not image): continue
                    if len(image) > MAX_IMAGE_SIZE: continue
                    if to in accounts and username in accounts[to].get("blocked", []):
                        send_to(sock, {"type": "error", "msg": "You are blocked"})
                        continue
                    if to in accounts[username].get("blocked", []):
                        send_to(sock, {"type": "error", "msg": "You blocked this user"})
                        continue
                    ts  = datetime.now().strftime("%I:%M %p")
                    mid = new_id()
                    key = dm_key(username, to)
                    dms.setdefault(key, []).append({
                        "id": mid, "from": username, "content": content,
                        "time": ts, "image": image, "edited": False, "read": False
                    })
                    save_json(DMS_FILE, dms)
                    if to in user_sockets:
                        send_to(user_sockets[to], {
                            "type": "dm", "from": username, "content": content,
                            "time": ts, "image": image, "msg_id": mid
                        })
                        dms[key][-1]["read"] = True
                        save_json(DMS_FILE, dms)
                    send_to(sock, {"type": "dm_sent", "to": to, "msg_id": mid, "read": to in user_sockets})

                elif t == "dm_read":
                    other = clean(data.get("from", ""), MAX_USERNAME_LEN)
                    key   = dm_key(username, other)
                    for m in dms.get(key, []):
                        if m["from"] == other:
                            m["read"] = True
                    save_json(DMS_FILE, dms)
                    if other in user_sockets:
                        send_to(user_sockets[other], {"type": "dm_read_receipt", "by": username})

                elif t == "typing":
                    ctx = clean(data.get("context", ""), 128)
                    ist = bool(data.get("typing", True))
                    if ctx.startswith("dm:"):
                        other = ctx[3:]
                        if other in user_sockets:
                            send_to(user_sockets[other], {"type": "typing", "user": username, "context": ctx, "typing": ist})
                    else:
                        parts = ctx.split(":")
                        if len(parts) == 2 and parts[0] in servers_data:
                            broadcast_server(parts[0], {"type": "typing", "user": username, "context": ctx, "typing": ist}, exclude=username)

                elif t == "mark_read":
                    srv = clean(data.get("server", ""), 64)
                    if srv not in servers_data: continue
                    accounts[username]["seen_indices"][srv] = len(messages.get(f"{srv}:general", []))
                    save_json(ACCOUNTS_FILE, accounts)

                elif t == "create_channel":
                    srv  = clean(data.get("server", ""), 64)
                    chan = clean(data.get("channel", ""), 64).replace(" ", "_")
                    if srv in servers_data and chan and chan not in servers_data[srv].get("channels", []):
                        servers_data[srv].setdefault("channels", ["general"]).append(chan)
                        save_json(SERVERS_FILE, servers_data)
                        broadcast_all({"type": "server_update", "servers": servers_data})

                elif t == "delete_channel":
                    srv  = clean(data.get("server", ""), 64)
                    chan = clean(data.get("channel", ""), 64)
                    if srv in servers_data and chan != "general":
                        chans = servers_data[srv].get("channels", [])
                        if chan in chans:
                            chans.remove(chan)
                            save_json(SERVERS_FILE, servers_data)
                            broadcast_server(srv, {"type": "channel_deleted", "server": srv, "channel": chan})
                            broadcast_all({"type": "server_update", "servers": servers_data})

                elif t == "create_server":
                    name = clean(data.get("name", ""), 64)
                    inv  = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                    if name and name not in servers_data:
                        servers_data[name] = {
                            "channels":       ["general"],
                            "voice_channels": ["General"],
                            "members":        [username],
                            "invite":         inv
                        }
                        save_json(SERVERS_FILE, servers_data)
                        accounts[username]["seen_indices"][name] = 0
                        save_json(ACCOUNTS_FILE, accounts)
                        broadcast_all({"type": "server_update", "servers": servers_data})

                elif t == "join_server":
                    code   = clean(data.get("code", ""), 16).upper()
                    joined = False
                    for sname, sinfo in servers_data.items():
                        if sinfo.get("invite", "").upper() == code:
                            if username not in sinfo["members"]:
                                sinfo["members"].append(username)
                            save_json(SERVERS_FILE, servers_data)
                            accounts[username]["seen_indices"].setdefault(sname, 0)
                            save_json(ACCOUNTS_FILE, accounts)
                            broadcast_all({"type": "server_update", "servers": servers_data})
                            send_to(sock, {"type": "joined_server", "name": sname})
                            joined = True
                            break
                    if not joined:
                        send_to(sock, {"type": "error", "msg": f"Invalid code: {code}"})

                elif t == "delete_server":
                    srv = clean(data.get("server", ""), 64)
                    if srv in ["Main Server", "Dev Room", "Gaming"]:
                        continue
                    if srv in servers_data:
                        del servers_data[srv]
                        save_json(SERVERS_FILE, servers_data)
                        # Clear voice rooms for this server
                        with voice_lock:
                            for u, (s, c) in list(voice_active.items()):
                                if s == srv:
                                    del voice_active[u]
                            voice_rooms.pop(srv, None)
                        broadcast_all({"type": "server_deleted", "server": srv})
                        broadcast_all({"type": "server_update", "servers": servers_data})

                elif t == "delete_history":
                    srv  = clean(data.get("server", ""), 64)
                    chan = clean(data.get("channel", ""), 64)
                    if srv not in servers_data: continue
                    key = f"{srv}:{chan}"
                    messages[key] = []
                    save_json(MESSAGES_FILE, messages)
                    broadcast_server(srv, {"type": "history_wiped", "server": srv, "channel": chan})

                elif t == "search_servers":
                    query   = clean(data.get("query", ""), 64).lower()
                    results = []
                    for sname, sinfo in servers_data.items():
                        if sinfo.get("public", False) and query in sname.lower():
                            results.append({
                                "name":    sname,
                                "members": len(sinfo.get("members", [])),
                                "invite":  sinfo.get("invite", ""),
                                "joined":  username in sinfo.get("members", [])
                            })
                    send_to(sock, {"type": "search_results", "results": results})

                elif t == "add_friend":
                    target = clean(data.get("to", ""), MAX_USERNAME_LEN)
                    if target not in accounts:
                        send_to(sock, {"type": "error", "msg": f"'{target}' not found"})
                        continue
                    if target == username or target in accounts[username].get("blocked", []): continue
                    if username in accounts[target].get("blocked", []): continue
                    if target in accounts[username].get("friends", []): continue
                    accounts[username].setdefault("friends", []).append(target)
                    accounts[target].setdefault("friends", []).append(username)
                    save_json(ACCOUNTS_FILE, accounts)
                    send_to(sock, {"type": "friend_update", "friends": accounts[username]["friends"], "pending": accounts[username].get("pending", []), "dm_history": dm_history_for(username)})
                    if target in user_sockets:
                        send_to(user_sockets[target], {"type": "friend_update", "friends": accounts[target]["friends"], "pending": accounts[target].get("pending", []), "dm_history": dm_history_for(target)})

                elif t == "block_user":
                    target = clean(data.get("user", ""), MAX_USERNAME_LEN)
                    if target not in accounts: continue
                    acc = accounts[username]
                    acc.setdefault("blocked", [])
                    if target not in acc["blocked"]: acc["blocked"].append(target)
                    if target in acc.get("friends", []): acc["friends"].remove(target)
                    if username in accounts[target].get("friends", []): accounts[target]["friends"].remove(username)
                    save_json(ACCOUNTS_FILE, accounts)
                    send_to(sock, {"type": "block_update", "blocked": acc["blocked"], "friends": acc.get("friends", [])})

                elif t == "unblock_user":
                    target = clean(data.get("user", ""), MAX_USERNAME_LEN)
                    acc = accounts[username]
                    if target in acc.get("blocked", []): acc["blocked"].remove(target)
                    save_json(ACCOUNTS_FILE, accounts)
                    send_to(sock, {"type": "block_update", "blocked": acc.get("blocked", []), "friends": acc.get("friends", [])})

                elif t == "remove_friend":
                    target = clean(data.get("user", ""), MAX_USERNAME_LEN)
                    if target in accounts[username].get("friends", []): accounts[username]["friends"].remove(target)
                    if username in accounts[target].get("friends", []): accounts[target]["friends"].remove(username)
                    save_json(ACCOUNTS_FILE, accounts)
                    send_to(sock, {"type": "friend_update", "friends": accounts[username]["friends"], "pending": accounts[username].get("pending", []), "dm_history": {}})

                elif t == "change_password":
                    old_pw = data.get("old_password", "")
                    new_pw = data.get("new_password", "")
                    if not check_password(old_pw, accounts[username]["password"]):
                        send_to(sock, {"type": "error", "msg": "Current password incorrect"})
                        continue
                    if len(new_pw) < 4:
                        send_to(sock, {"type": "error", "msg": "Min 4 characters"})
                        continue
                    accounts[username]["password"] = hash_password(new_pw)
                    save_json(ACCOUNTS_FILE, accounts)
                    send_to(sock, {"type": "success", "msg": "Password changed!"})

                elif t == "change_username":
                    new_name = clean(data.get("new_username", ""), MAX_USERNAME_LEN)
                    pw       = data.get("password", "")
                    if not check_password(pw, accounts[username]["password"]):
                        send_to(sock, {"type": "error", "msg": "Wrong password"})
                        continue
                    if new_name in accounts:
                        send_to(sock, {"type": "error", "msg": "Username taken"})
                        continue
                    if len(new_name) < 2 or " " in new_name:
                        send_to(sock, {"type": "error", "msg": "Invalid username"})
                        continue
                    old_name = username
                    accounts[new_name] = accounts.pop(old_name)
                    username = new_name
                    user_sockets[new_name] = user_sockets.pop(old_name, sock)
                    save_json(ACCOUNTS_FILE, accounts)
                    send_to(sock, {"type": "username_changed", "new_username": new_name})

                elif t == "friend_accept":
                    req = clean(data.get("from", ""), MAX_USERNAME_LEN)
                    acc = accounts[username]
                    if "pending" in acc and req in acc["pending"]: acc["pending"].remove(req)
                    acc.setdefault("friends", [])
                    accounts[req].setdefault("friends", [])
                    if req not in acc["friends"]: acc["friends"].append(req)
                    if username not in accounts[req]["friends"]: accounts[req]["friends"].append(username)
                    save_json(ACCOUNTS_FILE, accounts)
                    send_to(sock, {"type": "friend_update", "friends": acc["friends"], "pending": acc.get("pending", []), "dm_history": dm_history_for(username)})
                    if req in user_sockets:
                        send_to(user_sockets[req], {"type": "friend_update", "friends": accounts[req]["friends"], "pending": accounts[req].get("pending", []), "dm_history": dm_history_for(req)})

                elif t == "invite_to_server":
                    srv    = clean(data.get("server", ""), 64)
                    target = clean(data.get("to", ""), MAX_USERNAME_LEN)
                    code   = servers_data.get(srv, {}).get("invite", "")
                    if target in user_sockets and code:
                        send_to(user_sockets[target], {"type": "server_invite", "from": username, "server": srv, "code": code})

    except Exception as e:
        print(f"[ERROR] {username}: {e}")
    finally:
        if username:
            user_sockets.pop(username, None)
            rate_tracker.pop(username, None)
            # Remove from voice
            with voice_lock:
                if username in voice_active:
                    old_srv, old_chan = voice_active.pop(username)
                    if old_srv in voice_rooms and old_chan in voice_rooms[old_srv]:
                        voice_rooms[old_srv][old_chan].pop(username, None)
            broadcast_voice_state()
        update_user_list()
        sock.close()


def start():
    # Start UDP voice relay in background
    threading.Thread(target=udp_relay_loop, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen()
    print(f"🚀 Chat + Voice Server Online — TCP:{PORT}  UDP:{UDP_PORT}")
    print(f"🔒 Signup code: {SIGNUP_CODE}  |  Max {MAX_MSGS_PER_WIN} msgs per {RATE_WINDOW}s")
    while True:
        conn, addr = srv.accept()
        print(f"[+] {addr}")
        threading.Thread(target=handle_client, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    start()