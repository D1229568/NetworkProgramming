# Co-op Block Adventure 協力方塊大冒險

A 2–4 player online **co-op puzzle platformer** written in Python (Group 18, 網路程式設計期末專題).
Players must work together to clear each level: stand on a teammate's head to reach high
places, push heavy crates together, hold buttons so gates stay open for the others, grab
the key, and get **everyone** through the exit door.

* **Server** – TCP server that manages accounts, rooms, match-making, invitations and level
  unlocking. It runs the whole game simulation (it is the referee) and detects clients that
  drop out. Connections are encrypted with **TLS**, and the server answers **UDP broadcast**
  discovery requests so players on the same WiFi find it by themselves. It uses only the
  Python standard library.
* **Client** – graphical client built with **pygame-ce**: login/register with a list of servers
  found on the network, lobby, room with level select, invitations and chat, the game, and a
  network-statistics overlay (F3). The UI is drawn at the screen's real resolution, so it stays
  sharp with 150% / 200% Windows display scaling, and the window can be resized freely.

Tested on Python 3.14 on Windows (Python 3.10+ works).

---

## 1. How to run

Install the client library once on every computer that runs a client (needs internet):

```powershell
python -m pip install -r requirements.txt
```

Then:

```powershell
python run_server.py                      # TCP port 50555 (game) + UDP port 50556 (discovery)
python run_client.py                      # start one client window per player
```

* **Same WiFi / hotspot:** the login screen lists the servers it finds on the network
  ("Servers on this WiFi"). Click one, then log in. No IP address to type.
* **Other networks** (Tailscale, a VPS, port forwarding): type the server's address in the
  **Server** field. Broadcasts do not cross the internet, so discovery only works on a LAN.
* Windows may ask you to allow Python through the firewall the first time the server starts.
* Server options:
  * `--port 6000` – game port;
  * `--name "Kenji's laptop"` – name shown in the LAN list;
  * `--no-tls` – plain TCP, for a Wireshark demo;
  * `--no-discovery` – do not answer UDP broadcasts.
* Client options: `run_client.py --host 192.168.1.23 --port 6000`.
* Server console commands (type into the server window):
  * `status` lists every connection with its traffic, every room with its host, selected
    level, members and state, and the TLS and discovery status;
  * `kick <user>` removes a user;
  * `quit` stops the server.
* To test alone, run `python -m tools.bot --user bot1 --size 2` to add a headless player. It joins a
  room, presses Ready and stands still. Create the room yourself first, so you are the host.

Files created at runtime (not in git):

* `server/data/`: `users.json` (accounts with salted PBKDF2 password hashes, level progress)
  and `server.crt` / `server.key` (the TLS certificate and private key).
* `client/data/known_servers.json`: the certificate fingerprints this computer trusts.

### Controls

| Key | Action |
| --- | --- |
| ← → or A D | move |
| ↑ / W / Space | jump (hold longer = higher) |
| P | pause / resume (any player) |
| R | vote to restart the level (majority needed) |
| Enter / Esc | type in the room chat / go back to the game |
| Tab | next text field (login and lobby screens) |
| **F3** | show / hide the network stats overlay |
| F9 *(demo)* | drop the TCP connection without logging out (tests reconnect) |
| F10 *(demo)* | freeze outgoing traffic for 10 s (tests the server's heartbeat check) |

### Level select and unlocking

* After joining a room you land on the **level select**, not straight in level 1.
* The **host** (the player who created the room; it passes to the next player if the host
  leaves) clicks a level card to choose it. Changing the level resets everyone's Ready.
* **Level N+1 unlocks only after you clear level N.** Progress is saved per account on the server.
* In a room, a level can only be chosen if **every** player in the room has unlocked it. A
  card says why it is locked: *"Clear level 2 first"* or *"Locked for bob"*. If a new player
  with less progress joins, the selection drops back to a level everyone may play.
* After a level is cleared, every player unlocks the next level. The room returns to the
  level select with the next level already chosen. The cards show your best time.

### Inviting friends

In a room that still has empty seats, click **Invite friends**. You see the players who are in
the lobby; click one to invite them. They get a pop-up: *"alice invites you to the room …
Join now?"* with **Join** / **Decline**. If they decline, you are told. Invitations expire after
30 s, and you cannot spam the same person (5 s cooldown). The server checks everything:

* you must be in a waiting room with a free seat;
* the friend must be online and not in a room;
* the invitation must exist and not be expired.

### Network stats (F3)

The overlay shows what the network layer is doing right now:

* transport: TCP + TLS version and cipher, and the server's certificate fingerprint;
* ping (heartbeat round-trip time) with a 60-second graph;
* messages per second and bytes per second in each direction. It shows both the JSON
  payload and the bytes actually on the wire, so you can see the TLS overhead;
* game snapshots per second and their jitter;
* total traffic and the number of reconnects.

### Levels (all original)

1. **Teamwork 101** – the key is on a ledge 3 tiles high, so one player must jump from a teammate's head.
2. **Pressure Plate** – a button needs 2 bodies (a box counts). A second button on the far side lets the last player through.
3. **Heavy Lifting** – a heavy crate only moves when *all* players push together. It fills a spike pit to make a bridge.
4. **Up and Over** – stack up to climb a gate, then hold the button for the others. Push a box to reach a 4-tile shelf.
5. **Grand Finale** – everything combined, with two gates and three buttons.

---

## 2. Requirement checklist 規定對照

### Server 模組

| # | Requirement | Implementation |
| --- | --- | --- |
| 1 | 管理成員的註冊、登入、離開 | `register` / `login` / `logout` messages (`server/server.py`). Accounts and level progress are kept in `server/users.py`: unique case-insensitive names and salted PBKDF2 passwords. Passwords travel inside **TLS**. Logging in again from a new window takes over the old session. |
| 2 | 組合參與成員，並讓成員知道還缺多少人 | Rooms of 2/3/4 players: **Create**, **Join**, server-side **Quick match** (fills the fullest open room), and **Invite a friend**. Every `room` and `lobby` message carries `missing`. Clients show "Need 2 more" and "Waiting for N more player(s)". |
| 3 | 初始化應用 | When the room is full and everyone is ready, the server builds the **selected** level (`World` in `server/game.py`). It places players on their spawn points and sets up boxes, buttons, gates, key and door, then sends the static level to every client. |
| 4 | 扮演莊家或仲裁者 | Server-authoritative simulation at 60 ticks/s. The server alone computes movement and collisions and decides who holds the key, which buttons are pressed, whether the door opens and when the level is cleared. It also enforces the level-unlock and invitation rules. |
| 5 | 控制成員的動作，讓成員了解是否該行動 | Game-flow state machine `waiting (level select) → countdown → playing → level_clear → waiting`, plus `paused` (`server/room.py`). Clients see a 3-2-1 countdown, "GO!", "PAUSED", and so on. Only the host may choose a level, and inputs are ignored unless the phase is `playing`. |
| 6 | 轉送其他參與者的動作 | Inputs are applied to the shared world, and a snapshot of everyone's position and state goes to all room members 30 times per second. Chat, invitations and system notices are relayed too. |
| 7 | 維護各組運行中的資訊 | The server keeps every room with its host, members, ready/online flags, selected level, phase and level. It pushes this to all clients (lobby room list: "N games in progress"). `status` in the console also shows the traffic of every connection. |
| 8 | 偵測並處理 Client 不正常離線 | A closed or reset socket is detected immediately. A silent client is detected by **heartbeat** (no message for 6 s). A dropped player's seat is kept: the game **pauses** and teammates see "waiting for X… 52 s". If the player logs in again within 60 s they are put straight back. Otherwise they are removed and the level restarts with the remaining players, or the room returns to the level select if fewer than 2 are left. |

### Client 模組

| # | Requirement | Implementation |
| --- | --- | --- |
| 1 | 圖形化介面 | pygame-ce GUI: `client/app.py` (window, main loop), `client/scenes.py` (screens, invite dialog), `client/ui.py` (widgets), `client/game_view.py` (game rendering), `client/netstats.py` (F3 overlay). |
| 2 | 註冊、登入、離開介面 | Log in / Register switch (with password confirmation), the LAN server list and a TLS switch, a Log out button, a Leave button (with a confirm dialog during a game), and closing the window logs out properly. |
| 3 | 接收並顯示其他 Client 的動作 | Snapshots are drawn at 60 FPS. Positions are **interpolated** between snapshots so teammates move smoothly, with landing dust, sparkles and confetti. Chat, invitations and events appear in the chat box, pop-ups and toasts. |
| 4 | 確認合法性後傳送給 Server | Only game keys are used. Input is sent only while the phase is `playing`, you are still in the level, and you are online, and only when the key state changes. Locked or non-host level clicks, form fields, chat length, room size, joinability, the Ready button and repeated invitations are also checked before sending. The server validates everything again. |
| 5 | 偵測並處理與 Server 連線中斷 | The client sends a ping every second (ping and its graph are in F3). If the server is silent for 5 s or the socket breaks, a red banner and a "CONNECTION LOST" overlay appear. The client then reconnects and logs in again automatically (up to 20 tries), which puts the player back in the same game. If the server's certificate is different from before, the client stops and warns instead of connecting. |

---

## 3. Architecture

```
 ┌──────────── client (per player) ────────────┐   TCP + TLS   ┌──────────────── server ─────────────────┐
 │ pygame main loop (60 FPS)                   │◄─────────────►│ accept thread → ClientHandler per client │
 │  • screens, input checks, rendering        │  port 50555   │   reader: TLS handshake, decode frames   │
 │  • heartbeat ping 1/s, auto-reconnect      │               │   writer: send queue (+ latest state)    │
 │ network thread                              │               │ tick thread (60 Hz): rooms → World.step  │
 │  • recv → TLS → FrameDecoder → queue       │      UDP      │ heartbeat thread: drop silent clients    │
 │ discovery thread (on demand)                │──broadcast──►│ discovery thread: answer "who is there?" │
 │  • "is there a server?" → list of replies  │◄──unicast────│ console thread: status / kick / quit     │
 └─────────────────────────────────────────────┘  port 50556   └──────────────────────────────────────────┘
```

| File | Purpose |
| --- | --- |
| `common/protocol.py` | Framing (4-byte length + UTF-8 JSON), constants and shared validators |
| `common/transport.py` | `PlainTransport` / `TlsTransport` (TLS over the TCP socket, thread-safe, counts wire bytes) |
| `common/discovery.py` | UDP broadcast discovery: request and reply formats, `discover()` |
| `server/server.py` | TCP server, sessions, lobby, rooms, invitations, heartbeat, discovery responder, console |
| `server/certgen.py` | Creates the self-signed TLS certificate (RSA-2048 + X.509, pure Python) |
| `server/room.py` | One room: host, level select and unlock rules, ready check, game-flow state machine, pause and reconnect |
| `server/game.py` | Physics and rules: gravity, collisions, stacking, pushing, buttons, gates, key, door |
| `server/levels.py` | The five level maps (ASCII) |
| `server/users.py` | Accounts and per-account progress (unlocked level, best times) |
| `client/network.py` | Client connection (TLS, certificate pinning, traffic counters) and background discovery |
| `client/app.py` | Window, main loop, connection management, reconnect, invitations |
| `client/scenes.py` | Login (with LAN list), Lobby and Room screens (level select, seats, chat, invite dialog) |
| `client/ui.py` | Resolution-independent widget toolkit (buttons, inputs, switches, lists, chat, toasts, dialogs) |
| `client/game_view.py` | Game rendering, interpolation, effects and keyboard input |
| `client/netstats.py` | F3 network statistics overlay |
| `tools/bot.py` | Headless test player |
| `tests/` | Unit and end-to-end tests |

### TCP for the game, UDP for discovery

| | TCP (game, port 50555) | UDP (discovery, port 50556) |
| --- | --- | --- |
| What it carries | logins, rooms, chat, inputs, game snapshots | "is there a server?" and the server's reply |
| Why | every message must arrive **complete and in order** | must reach **every device on the WiFi** without knowing their addresses, which only broadcast can do |
| If a packet is lost | TCP resends it automatically | does not matter; the client just scans again |

Low-latency details on TCP:

* `TCP_NODELAY` is set on both ends (no Nagle buffering of small packets).
* The server sends only **the newest snapshot** to each client; old ones are dropped instead
  of queuing up (`ClientHandler.send_state_bytes`).
* TCP is a byte stream, so each message is length-prefixed and `FrameDecoder` reassembles
  messages split across several `recv()` calls or glued together in one.

### TLS (encryption)

Without TLS, anyone on the same WiFi could read the passwords and chat with a packet sniffer
(try `run_server.py --no-tls` with Wireshark). With TLS (the default) everything on port 50555
is encrypted with TLS 1.3, using Python's `ssl` module:

1. On first start the server creates a **self-signed certificate** (`server/certgen.py`: RSA-2048
   key, X.509 certificate, SHA-256 signature). It prints the certificate's SHA-256 fingerprint.
2. There is no certificate authority, so the client uses **trust on first use**, like SSH. The
   first time it connects to a server it saves the fingerprint in `client/data/known_servers.json`.
3. If a server later shows a **different** certificate, the client refuses to log in and warns
   about a possible man-in-the-middle. The player must explicitly click *Trust new certificate*.
4. Each connection has a reader thread and a writer thread. `common/transport.py` therefore drives
   `ssl.SSLObject` through memory buffers under a lock instead of sharing an `SSLSocket` between
   threads.

The client's **Encrypted (TLS)** switch must match the server. Discovery sets it automatically,
and a mismatch gives a clear error message.

### Message types

Client → server:

* `register`, `login`, `logout`, `ping`, `list_rooms`;
* `create_room {name,size}`, `join_room {room_id}`, `quick_match {size?}`, `leave_room`;
* `invite {to}`, `invite_reply {room_id,accept}`;
* `select_level {level}`, `ready {ready}`, `chat {text}`, `input {left,right,jump}`, `pause`, `restart`.

Server → client:

* `hello {version,name,tls}`, `register_result`, `login_result {ok,rejoin,progress}`, `logout_ok`, `pong`;
* `lobby {rooms,online,games_in_progress}`, `room {members,phase,missing,host,selected,max_level,levels,…}`, `room_left`;
* `invite {from,room_id,room_name,count,size,level}`, `notice {text,kind}`, `chat {from,text,system}`;
* `level` (static map, once per level), `state` (snapshot, 30/s),
  `level_result {index,time,best,record,unlocked,newly_unlocked}`;
* `error {msg}`, `kicked {reason}`.

UDP discovery (JSON datagrams with a magic string): `discover` → `server_info {name,port,tls,fingerprint,players,rooms,open_rooms}`.

---

## 4. Tests

```powershell
python -m unittest discover -s tests -t .
```

39 tests:

* framing edge cases;
* physics rules (jump height, stacking, carrying, heavy box needs every player, buttons and gates, spikes, key and door);
* a **scripted two-player solution for every level**, which proves each level can be finished;
* end-to-end server tests, all over **real TLS connections**:
  * login rules, quick match, input relay, chat;
  * abnormal disconnect → pause → reconnect → resume, reconnect timeout, logout mid-game, heartbeat timeout;
  * pause, restart vote;
  * level select, unlocking, host handoff;
  * invitations (accept, decline, rules, rate limit) and traffic counters;
* plaintext mode (`--no-tls`) and TLS/plain mismatches in both directions, plus certificate pinning (a changed certificate is refused);
* UDP discovery: the server is found, foreign or garbage datagrams are ignored, and replies are rate limited.

### Demo script for the presentation

1. Start the server: it prints its certificate fingerprint and says it answers discovery on UDP 50556.
2. Start two clients on two laptops on the same WiFi. The server appears in **Servers on this WiFi**;
   click it and log in. Press **F3**: TLS 1.3 and the same fingerprint as the server printed.
3. Create a 2-player room and use **Invite friends** to invite the other player; they click **Join**.
4. Show the level select (levels 2–5 locked), clear level 1 by stacking, and level 2 unlocks.
   Watch the snapshots/s, KB/s and ping graph in the F3 overlay while playing.
5. Type `status` in the server console: rooms, hosts, levels and per-connection traffic.
6. Press **F9** in one client: the other shows *PAUSED – waiting for X to reconnect*, then it reconnects by itself.
7. Press **F10** (freeze): after about 6 s the server reports a heartbeat timeout, and the client reconnects.
8. *(Optional, Wireshark)* Run the server with `--no-tls` and switch TLS off in the client. The
   password is readable in the capture. With TLS on, it is not.
