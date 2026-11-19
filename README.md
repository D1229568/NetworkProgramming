# Co-op Block Adventure 協力方塊大冒險

A 2–4 player online **co-op puzzle platformer** written in Python (Group 18, 網路程式設計期末專題).
Players must work together to clear each level: stand on a teammate's head to reach high
places, push heavy crates together, hold buttons so gates stay open for the others, grab
the key, and get **everyone** through the exit door.

* **Server** – TCP server that manages accounts, rooms, match-making and level unlocking.
  It runs the whole game simulation (it is the referee) and detects clients that drop out.
  It uses only the Python standard library.
* **Client** – graphical client built with **pygame-ce**: login/register, lobby, room with
  level select and chat, and the game. It sends the player's keys to the server and draws
  the state it receives. The UI is drawn at the screen's real resolution, so it stays sharp
  with 150% / 200% Windows display scaling, and the window can be resized freely.

Tested on Python 3.14 on Windows (Python 3.10+ works).

---

## 1. How to run

Install the client library once on every computer that runs a client (needs internet):

```powershell
python -m pip install -r requirements.txt
```

Then:

```powershell
python run_server.py                      # listens on all interfaces, TCP port 50555
python run_client.py                      # start one client window per player
```

* Playing on several computers: start the server on one PC, then type that PC's IP address
  (for example `192.168.1.23`) in the **Server** field of each client. Windows may ask you to
  allow Python through the firewall the first time the server starts.
* Options: `run_server.py --port 6000`, `run_client.py --host 192.168.1.23 --port 6000`.
* Server console commands (type into the server window): `status` lists online users,
  every room with its host, selected level, members and state. `kick <user>` removes a user,
  and `quit` stops the server.
* To test alone, run `python -m tools.bot --user bot1 --size 2` to add a headless player. It joins a
  room, presses Ready and stands still. Create the room yourself first, so you are the host and
  can pick the level.

Accounts and level progress are stored in `server/data/users.json` (passwords are salted
PBKDF2 hashes).

### Controls

| Key | Action |
| --- | --- |
| ← → or A D | move |
| ↑ / W / Space | jump (hold longer = higher) |
| P | pause / resume (any player) |
| R | vote to restart the level (majority needed) |
| Enter / Esc | type in the room chat / go back to the game |
| Tab | next text field (login and lobby screens) |
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

### Levels (all original)

1. **Teamwork 101** – the key is on a ledge 3 tiles high, so one player must jump from a teammate's head.
2. **Pressure Plate** – a button needs 2 bodies (a box counts). A second button on the far side lets the last player through.
3. **Heavy Lifting** – a heavy crate only moves when *all* players push together. It fills a spike pit to make a bridge.
4. **Up and Over** – stack up to climb a gate, then hold the button for the others. Push a box to reach a 4-tile shelf.
5. **Grand Finale** – everything combined, with two gates and three buttons.

One jump rises about 2.5 tiles. A 2-tile step can be jumped alone, but a 3-tile wall needs a
teammate (or a box). Button requirements and crate weights adapt to the number of players.

---

## 2. Requirement checklist 規定對照

### Server 模組

| # | Requirement | Implementation |
| --- | --- | --- |
| 1 | 管理成員的註冊、登入、離開 | `register` / `login` / `logout` messages (`server/server.py`). Accounts and level progress are kept in `server/users.py`: unique case-insensitive names and salted PBKDF2 passwords. Logging in again from a new window takes over the old session. |
| 2 | 組合參與成員，並讓成員知道還缺多少人 | Rooms of 2/3/4 players: **Create**, **Join**, or server-side **Quick match** (fills the fullest open room). Every `room` and `lobby` message carries `missing`. Clients show "Need 2 more" and "Waiting for N more player(s)". |
| 3 | 初始化應用 | When the room is full and everyone is ready, the server builds the **selected** level (`World` in `server/game.py`). It places players on their spawn points and sets up boxes, buttons, gates, key and door, then sends the static level to every client. |
| 4 | 扮演莊家或仲裁者 | Server-authoritative simulation at 60 ticks/s. The server alone computes movement and collisions and decides who holds the key, which buttons are pressed, whether the door opens and when the level is cleared. It also enforces the level-unlock rules. |
| 5 | 控制成員的動作，讓成員了解是否該行動 | Game-flow state machine `waiting (level select) → countdown → playing → level_clear → waiting`, plus `paused` (`server/room.py`). Clients see a 3-2-1 countdown, "GO!", "PAUSED", and so on. Only the host may choose a level, and inputs are ignored unless the phase is `playing`. |
| 6 | 轉送其他參與者的動作 | Inputs are applied to the shared world, and a snapshot of everyone's position and state goes to all room members 30 times per second. Chat and system notices are relayed too. |
| 7 | 維護各組運行中的資訊 | The server keeps every room with its host, members, ready/online flags, selected level, phase and level. It pushes this to all clients (lobby room list: "N games in progress"), and the console shows it with `status`. |
| 8 | 偵測並處理 Client 不正常離線 | A closed or reset socket is detected immediately. A silent client is detected by **heartbeat** (no message for 6 s). A dropped player's seat is kept: the game **pauses** and teammates see "waiting for X… 52 s". If the player logs in again within 60 s they are put straight back. Otherwise they are removed and the level restarts with the remaining players, or the room returns to the level select if fewer than 2 are left. |

### Client 模組

| # | Requirement | Implementation |
| --- | --- | --- |
| 1 | 圖形化介面 | pygame-ce GUI: `client/app.py` (window, main loop), `client/scenes.py` (screens), `client/ui.py` (buttons, text fields, lists, chat, dialogs), `client/game_view.py` (game rendering). |
| 2 | 註冊、登入、離開介面 | Log in / Register switch (with password confirmation), a Log out button, a Leave button (with a confirm dialog during a game), and closing the window logs out properly. |
| 3 | 接收並顯示其他 Client 的動作 | Snapshots are drawn at 60 FPS. Positions are **interpolated** between snapshots so teammates move smoothly, with landing dust, sparkles and confetti. Chat and events appear in the chat box and as pop-up toasts. |
| 4 | 確認合法性後傳送給 Server | Only game keys are used. Input is sent only while the phase is `playing`, you are still in the level, and you are online, and only when the key state changes. Locked or non-host level clicks, form fields, chat length, room size, joinability and the Ready button are also checked before sending. The server validates everything again. |
| 5 | 偵測並處理與 Server 連線中斷 | The client sends a ping every second and measures ping in ms. If the server is silent for 5 s or the socket breaks, a red banner and a "CONNECTION LOST" overlay appear. The client then reconnects and logs in again automatically (up to 20 tries), which puts the player back in the same game. |

---

## 3. Architecture

```
 ┌──────────── client (per player) ────────────┐          ┌──────────────── server ─────────────────┐
 │ pygame main loop (60 FPS)                   │   TCP    │ accept thread → ClientHandler per client │
 │  • screens, input checks, rendering        │◄────────►│   reader thread: decode + dispatch       │
 │  • heartbeat ping 1/s, auto-reconnect      │  frames  │   writer thread: send queue (+ latest    │
 │ network thread                              │          │                  state only)             │
 │  • recv → FrameDecoder → queue → main loop │          │ tick thread (60 Hz): rooms → World.step  │
 └─────────────────────────────────────────────┘          │ heartbeat thread: drop silent clients    │
                                                          │ console thread: status / kick / quit     │
                                                          └──────────────────────────────────────────┘
```

| File | Purpose |
| --- | --- |
| `common/protocol.py` | Framing (4-byte length + UTF-8 JSON), constants and shared validators |
| `server/server.py` | TCP server, sessions, lobby, rooms, heartbeat, console |
| `server/room.py` | One room: host, level select and unlock rules, ready check, game-flow state machine, pause and reconnect |
| `server/game.py` | Physics and rules: gravity, collisions, stacking, pushing, buttons, gates, key, door |
| `server/levels.py` | The five level maps (ASCII) |
| `server/users.py` | Accounts and per-account progress (unlocked level, best times) |
| `client/network.py` | Client TCP connection (background reader thread) |
| `client/app.py` | Window, main loop, connection management, reconnect |
| `client/scenes.py` | Login/Register, Lobby and Room screens (level select cards, seats, players, chat) |
| `client/ui.py` | Resolution-independent widget toolkit (buttons, text inputs, lists, chat, toasts, dialogs) |
| `client/game_view.py` | Game rendering, interpolation, effects and keyboard input |
| `tools/bot.py` | Headless test player |
| `tests/` | Unit and end-to-end tests |

### Why TCP?

The game needs every message to arrive, and in order: logins, room changes, chat, and the
moment a key is pressed or released. TCP gives that for free. Two measures keep the latency low:

* `TCP_NODELAY` is set on both ends (no Nagle buffering of small packets).
* The server sends only **the newest snapshot** to each client. If a client falls behind,
  old snapshots are dropped instead of queuing up (`ClientHandler.send_state_bytes`).

Because TCP is a byte stream, each message is length-prefixed and `FrameDecoder` reassembles
messages that were split across several `recv()` calls or glued together in one.

### Message types

Client → server: `register`, `login`, `logout`, `ping`, `list_rooms`, `create_room {name,size}`,
`join_room {room_id}`, `quick_match {size?}`, `leave_room`, `select_level {level}`, `ready {ready}`,
`chat {text}`, `input {left,right,jump}`, `pause`, `restart`.

Server → client: `hello`, `register_result`, `login_result {ok,rejoin,progress}`, `logout_ok`,
`pong`, `lobby {rooms,online,games_in_progress}`,
`room {members,phase,missing,host,selected,max_level,levels,…}`, `room_left`,
`chat {from,text,system}`, `level` (static map, once per level), `state` (snapshot, 30/s),
`level_result {index,time,best,record,unlocked,newly_unlocked}`, `error {msg}`, `kicked {reason}`.

---

## 4. Tests

```powershell
python -m unittest discover -s tests -t .
```

30 tests:

* framing edge cases;
* physics rules (jump height, stacking, carrying, heavy box needs every player, buttons and gates, spikes, key and door);
* a **scripted two-player solution for every level**, which proves each level can be finished;
* end-to-end server tests over real sockets:
  * login rules, quick match, input relay, chat;
  * abnormal disconnect → pause → reconnect → resume, reconnect timeout, logout mid-game, heartbeat timeout;
  * pause, restart vote;
  * level select rules, unlocking after a clear, a newcomer limiting the room's levels, and host handoff.

### Demo script for the presentation

1. Start the server and two clients. Register two accounts, create a 2-player room, and show
   "Waiting for 1 more player". Join with the second client.
2. Show the level select: levels 2–5 are locked. Ready → countdown, then clear level 1 together
   by stacking. Level 2 unlocks and is selected automatically.
3. Type `status` in the server console to show rooms, hosts, selected levels and progress.
4. Press **F9** in one client: the other client shows *PAUSED – waiting for X to reconnect*.
   The first client shows the red banner, reconnects by itself, and the game resumes.
5. Press **F10** (freeze): after about 6 s the server reports a heartbeat timeout, and the client reconnects.
6. Close a client with Task Manager (crash): the server keeps the seat for 60 s, then removes the
   player and ends or restarts the game.
