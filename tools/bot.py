"""A tiny headless test player, handy when you want to try the game alone.

    python -m tools.bot --user bot1 --size 2

It registers/logs in, joins a room with quick match, presses Ready and then
just stands still (a perfect stool for jumping on!).  It answers heartbeats
so the server keeps it connected.  Press Ctrl+C to stop it.
"""
from __future__ import annotations

import argparse
import socket
import time

from common import protocol as P


def main(argv=None):
    parser = argparse.ArgumentParser(description="Headless test player")
    parser.add_argument("--host", default=P.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    parser.add_argument("--user", default="bot1")
    parser.add_argument("--password", default="botpass")
    parser.add_argument("--size", type=int, default=None, help="room size for quick match")
    parser.add_argument("--room", type=int, default=None, help="join this room id instead")
    args = parser.parse_args(argv)

    sock = socket.create_connection((args.host, args.port))
    decoder = P.FrameDecoder()

    def send(**msg):
        sock.sendall(P.encode(msg))

    send(type="register", username=args.user, password=args.password)
    send(type="login", username=args.user, password=args.password)
    sock.settimeout(0.5)
    last_ping = 0.0
    phase = None
    print(f"[bot {args.user}] connected to {args.host}:{args.port}")
    try:
        while True:
            if time.monotonic() - last_ping > P.HEARTBEAT_INTERVAL:
                last_ping = time.monotonic()
                send(type="ping", t=time.time())
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                print(f"[bot {args.user}] server closed the connection")
                return
            for msg in decoder.feed(data):
                kind = msg["type"]
                if kind == "login_result":
                    if not msg["ok"]:
                        print(f"[bot {args.user}] login failed: {msg['msg']}")
                        return
                    if not msg.get("rejoin"):
                        if args.room:
                            send(type="join_room", room_id=args.room)
                        else:
                            send(type="quick_match", size=args.size)
                elif kind == "room":
                    if msg["phase"] != phase:
                        phase = msg["phase"]
                        print(f"[bot {args.user}] room {msg['id']} is now {phase}")
                    me = next((m for m in msg["members"] if m["name"] == args.user), None)
                    if msg["phase"] == "waiting" and me and not me["ready"]:
                        send(type="ready", ready=True)
                elif kind == "chat" and msg.get("from") not in (None, args.user):
                    print(f"[bot {args.user}] {msg['from']}: {msg['text']}")
                elif kind == "error":
                    print(f"[bot {args.user}] error: {msg['msg']}")
    except KeyboardInterrupt:
        send(type="logout")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
