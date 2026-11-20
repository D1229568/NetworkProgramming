"""End-to-end tests: a real server on a random port and raw TCP (+TLS) clients."""
import shutil
import socket
import ssl
import tempfile
import threading
import time
import unittest

from common import protocol as P
from common.transport import PlainTransport, TlsTransport, client_context
from server.certgen import ensure_certificate
from server.server import GameServer

CERT_DIR = tempfile.mkdtemp()
CERT_FILE, KEY_FILE, _ = ensure_certificate(CERT_DIR)        # generated once for all tests


class RawClient:
    def __init__(self, port, tls=True):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        if tls:
            self.transport = TlsTransport(self.sock, client_context(), server_side=False)
            try:
                self.transport.handshake()
            except Exception:
                self.sock.close()
                raise
        else:
            self.transport = PlainTransport(self.sock)
        self.decoder = P.FrameDecoder()
        self.inbox = []
        self.keepalive = True
        self._lock = threading.Lock()
        threading.Thread(target=self._pinger, daemon=True).start()

    def _pinger(self):
        """Heartbeat in the background, like the real client does."""
        while True:
            time.sleep(0.3)
            if self.keepalive:
                try:
                    self.send(type="ping", t=time.time())
                except (OSError, ssl.SSLError):
                    return

    def send(self, **msg):
        with self._lock:
            self.transport.sendall(P.encode(msg))

    def _pump(self, timeout):
        self.sock.settimeout(timeout)
        try:
            data = self.transport.recv()
        except socket.timeout:
            return True
        except (OSError, ssl.SSLError) as exc:
            raise ConnectionError(str(exc)) from exc
        if not data:
            raise ConnectionError("server closed the connection")
        self.inbox.extend(self.decoder.feed(data))
        return True

    def wait(self, pred, timeout=5.0, what="message"):
        end = time.monotonic() + timeout
        while True:
            for i, msg in enumerate(self.inbox):
                if pred(msg):
                    del self.inbox[:i + 1]
                    return msg
            self.inbox.clear()
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"timed out waiting for {what}")
            self._pump(min(left, 0.2))

    def wait_type(self, kind, timeout=5.0, **fields):
        return self.wait(lambda m: m.get("type") == kind and all(m.get(k) == v for k, v in fields.items()),
                         timeout, f"{kind} {fields}")

    def login(self, name, pw="secret"):
        self.send(type="register", username=name, password=pw)
        self.wait_type("register_result")
        self.send(type="login", username=name, password=pw)
        return self.wait_type("login_result")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = GameServer("127.0.0.1", 0, self.tmp.name, heartbeat_timeout=1.5,
                                 reconnect_timeout=2.0, countdown_seconds=0.2,
                                 level_clear_seconds=0.2, verbose=False,
                                 cert_file=CERT_FILE, key_file=KEY_FILE, discovery=False)
        self.server.start()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.stop()
        time.sleep(0.05)
        self.tmp.cleanup()

    def client(self):
        c = RawClient(self.server.port)
        self.clients.append(c)
        c.wait_type("hello")
        return c

    def start_game(self):
        a, b = self.client(), self.client()
        self.assertTrue(a.login("alice")["ok"])
        self.assertTrue(b.login("bob")["ok"])
        a.send(type="create_room", name="test", size=2)
        room = a.wait_type("room")
        self.assertEqual(room["missing"], 1, "server tells how many players are still missing")
        b.send(type="join_room", room_id=room["id"])
        self.assertEqual(b.wait_type("room")["missing"], 0)
        a.send(type="ready", ready=True)
        b.send(type="ready", ready=True)
        for c in (a, b):
            level = c.wait_type("level")
            self.assertEqual(level["index"], 0)
            c.wait(lambda m: m.get("type") == "state" and m["phase"] == "playing", what="playing")
        return a, b, room["id"]

    def test_register_and_login_rules(self):
        c = self.client()
        c.send(type="register", username="carol", password="pw12")
        self.assertTrue(c.wait_type("register_result")["ok"])
        c.send(type="register", username="Carol", password="pw12")
        self.assertFalse(c.wait_type("register_result")["ok"], "usernames are unique (case-insensitive)")
        c.send(type="register", username="x", password="pw12")
        self.assertFalse(c.wait_type("register_result")["ok"])
        c.send(type="login", username="carol", password="wrong")
        self.assertFalse(c.wait_type("login_result")["ok"])
        c.send(type="create_room", name="nope", size=2)
        self.assertIn("log in", c.wait_type("error")["msg"])
        c.send(type="login", username="carol", password="pw12")
        self.assertTrue(c.wait_type("login_result")["ok"])
        lobby = c.wait_type("lobby")
        self.assertEqual([u["name"] for u in lobby["online"]], ["carol"])

    def test_quick_match_and_room_validation(self):
        a, b = self.client(), self.client()
        a.login("alice")
        b.login("bob")
        a.send(type="create_room", name="", size=2)
        a.wait_type("error")
        a.send(type="create_room", name="room", size=7)
        a.wait_type("error")
        a.send(type="quick_match", size=3)
        room = a.wait_type("room")
        self.assertEqual((room["size"], room["missing"]), (3, 2))
        b.send(type="quick_match")
        room_b = b.wait_type("room")
        self.assertEqual(room_b["id"], room["id"], "quick match fills the open room")
        lobby = a.wait(lambda m: m.get("type") == "lobby" and m["rooms"] and m["rooms"][0]["missing"] == 1)
        self.assertEqual(lobby["rooms"][0]["members"], ["alice", "bob"])

    def test_inputs_move_players_and_chat_is_relayed(self):
        a, b, _ = self.start_game()
        first = a.wait_type("state")
        x0 = next(p["x"] for p in first["players"] if p["n"] == "alice")
        a.send(type="input", left=False, right=True, jump=False)
        state = a.wait(lambda m: m.get("type") == "state" and
                       next(p["x"] for p in m["players"] if p["n"] == "alice") > x0 + 20,
                       what="alice moving right")
        self.assertEqual(state["phase"], "playing")
        b.wait(lambda m: m.get("type") == "state" and
               next(p["x"] for p in m["players"] if p["n"] == "alice") > x0 + 20, what="bob sees it")
        b.send(type="chat", text="  hi team \x07 ")
        msg = a.wait(lambda m: m.get("type") == "chat" and m.get("from") == "bob")
        self.assertEqual(msg["text"], "hi team")

    def test_abnormal_disconnect_pauses_and_reconnect_resumes(self):
        a, b, room_id = self.start_game()
        b.close()                       # no logout: simulates a crash / network failure
        room = a.wait(lambda m: m.get("type") == "room" and
                      any(not x["connected"] for x in m["members"]), what="bob offline")
        self.assertEqual(room["phase"], "paused")
        a.wait(lambda m: m.get("type") == "state" and m["phase"] == "paused" and m["pause"]["waiting"])
        b2 = self.client()
        b2.send(type="login", username="bob", password="secret")
        result = b2.wait_type("login_result")
        self.assertTrue(result["rejoin"])
        b2.wait_type("room", id=room_id)
        b2.wait_type("level")
        a.wait(lambda m: m.get("type") == "state" and m["phase"] == "playing", what="resumed")

    def test_dropped_player_is_removed_after_timeout(self):
        a, b, _ = self.start_game()
        b.close()
        room = a.wait(lambda m: m.get("type") == "room" and m["phase"] == "waiting", timeout=6,
                      what="game ended")
        self.assertEqual(room["missing"], 1)
        self.assertEqual([m["name"] for m in room["members"]], ["alice"])

    def test_logout_mid_game_ends_game_for_two_players(self):
        a, b, _ = self.start_game()
        b.send(type="logout")
        b.wait_type("logout_ok")
        room = a.wait(lambda m: m.get("type") == "room" and m["phase"] == "waiting", what="waiting")
        self.assertEqual(room["missing"], 1)

    def test_heartbeat_timeout_closes_silent_client(self):
        c = self.client()
        c.login("dave")
        c.keepalive = False             # go silent: no pings any more
        start = time.monotonic()
        with self.assertRaises(ConnectionError):
            while time.monotonic() - start < 5:
                c._pump(0.5)
        self.assertGreater(time.monotonic() - start, 1.0)

    def test_pause_and_restart_vote(self):
        a, b, _ = self.start_game()
        a.send(type="pause")
        b.wait(lambda m: m.get("type") == "state" and m["phase"] == "paused" and m["pause"]["by"] == "alice")
        b.send(type="pause")
        a.wait(lambda m: m.get("type") == "state" and m["phase"] == "playing")
        old_serial = a.wait_type("state")["serial"]
        a.send(type="restart")
        b.send(type="restart")
        level = a.wait_type("level")
        self.assertNotEqual(level["serial"], old_serial)

    def _clear_level_now(self, room_id):
        """Pretend the team walked through the door (the physics is tested elsewhere)."""
        with self.server.lock:
            for p in self.server.rooms[room_id].world.players:
                p.entered = True

    def test_level_select_rules_and_unlocking(self):
        a, b = self.client(), self.client()
        self.assertEqual(a.login("alice")["progress"]["unlocked"], 0)
        b.login("bob")
        a.send(type="create_room", name="sel", size=2)
        room = a.wait_type("room")
        self.assertEqual((room["host"], room["selected"], room["max_level"]), ("alice", 0, 0))
        b.send(type="join_room", room_id=room["id"])
        b.wait_type("room")
        b.send(type="select_level", level=0)
        self.assertIn("host", b.wait_type("error")["msg"], "only the host picks")
        a.send(type="select_level", level=1)
        self.assertIn("locked", a.wait_type("error")["msg"], "level 2 is locked at first")

        a.send(type="ready", ready=True)
        b.send(type="ready", ready=True)
        self.assertEqual(a.wait_type("level")["index"], 0)
        a.wait(lambda m: m.get("type") == "state" and m["phase"] == "playing")
        self._clear_level_now(room["id"])
        result = a.wait_type("level_result")
        self.assertEqual((result["index"], result["unlocked"]), (0, 1))
        self.assertTrue(result["newly_unlocked"] and result["record"])
        back = a.wait(lambda m: m.get("type") == "room" and m["phase"] == "waiting")
        self.assertEqual((back["selected"], back["max_level"]), (1, 1), "next level is unlocked and suggested")
        self.assertEqual(self.server.users.progress("bob")["unlocked"], 1)

        a.send(type="select_level", level=0)          # replaying an earlier level is allowed
        a.wait(lambda m: m.get("type") == "room" and m["selected"] == 0)
        a.send(type="select_level", level=1)
        a.wait(lambda m: m.get("type") == "room" and m["selected"] == 1)
        a.send(type="ready", ready=True)
        b.send(type="ready", ready=True)
        started = a.wait(lambda m: m.get("type") == "level", what="new level")
        self.assertEqual(started["index"], 1, "the chosen level starts")

    def test_newcomer_without_progress_limits_the_room(self):
        self.server.users.register("vet", "secret")
        self.server.users.record_clear(["vet"], 0, 10.0, 4)
        self.server.users.record_clear(["vet"], 1, 10.0, 4)
        a, b = self.client(), self.client()
        self.assertEqual(a.login("vet")["progress"]["unlocked"], 2)
        a.send(type="create_room", name="r", size=2)
        room = a.wait_type("room")
        a.send(type="select_level", level=2)
        a.wait(lambda m: m.get("type") == "room" and m["selected"] == 2)
        b.login("newbie")
        b.send(type="join_room", room_id=room["id"])
        clamped = a.wait(lambda m: m.get("type") == "room" and len(m["members"]) == 2)
        self.assertEqual((clamped["selected"], clamped["max_level"]), (0, 0))

    def test_host_moves_on_when_host_leaves(self):
        a, b = self.client(), self.client()
        a.login("alice")
        b.login("bob")
        a.send(type="create_room", name="h", size=3)
        room = a.wait_type("room")
        b.send(type="join_room", room_id=room["id"])
        b.wait_type("room")
        a.send(type="leave_room")
        after = b.wait(lambda m: m.get("type") == "room" and len(m["members"]) == 1)
        self.assertEqual(after["host"], "bob")

    def test_inputs_are_ignored_while_not_playing(self):
        a, b, _ = self.start_game()
        a.send(type="pause")
        paused = a.wait(lambda m: m.get("type") == "state" and m["phase"] == "paused")
        x0 = next(p["x"] for p in paused["players"] if p["n"] == "alice")
        a.send(type="input", left=False, right=True, jump=False)
        time.sleep(0.3)
        later = a.wait(lambda m: m.get("type") == "state")
        self.assertEqual(next(p["x"] for p in later["players"] if p["n"] == "alice"), x0)

    # ------------------------------------------------------------ invitations
    def test_invite_accept_and_decline(self):
        a, b, c = self.client(), self.client(), self.client()
        a.login("alice")
        b.login("bob")
        c.login("carol")
        a.send(type="create_room", name="party", size=3)
        room = a.wait_type("room")
        a.send(type="invite", to="bob")
        self.assertIn("sent", a.wait_type("notice")["text"])
        invite = b.wait_type("invite")
        self.assertEqual((invite["from"], invite["room_id"], invite["count"], invite["size"]),
                         ("alice", room["id"], 1, 3))
        b.send(type="invite_reply", room_id=room["id"], accept=True)
        joined = b.wait_type("room")
        self.assertEqual([m["name"] for m in joined["members"]], ["alice", "bob"])

        a.send(type="invite", to="carol")
        c.send(type="invite_reply", room_id=c.wait_type("invite")["room_id"], accept=False)
        a.wait(lambda m: m.get("type") == "notice" and "carol declined" in m["text"], what="decline notice")

    def test_invite_rules(self):
        a, b, c = self.client(), self.client(), self.client()
        a.login("alice")
        b.login("bob")
        c.login("carol")
        a.send(type="invite", to="bob")
        self.assertIn("not in a room", a.wait_type("error")["msg"])
        a.send(type="create_room", name="r1", size=2)
        room = a.wait_type("room")
        b.send(type="create_room", name="r2", size=2)
        b.wait_type("room")
        a.send(type="invite", to="alice")
        self.assertIn("yourself", a.wait_type("error")["msg"])
        a.send(type="invite", to="bob")
        self.assertIn("already in a room", a.wait_type("error")["msg"])
        a.send(type="invite", to="nobody")
        self.assertIn("not online", a.wait_type("error")["msg"])
        c.send(type="invite_reply", room_id=room["id"], accept=True)
        self.assertIn("expired", c.wait_type("error")["msg"], "cannot accept an invitation nobody sent")
        a.send(type="invite", to="carol")
        a.wait_type("notice")
        a.send(type="invite", to="carol")
        self.assertIn("moment", a.wait_type("error")["msg"], "invitations are rate limited")

    def test_server_counts_traffic(self):
        a = self.client()
        a.login("alice")
        with self.server.lock:
            h = self.server.sessions["alice"]
            self.assertGreater(h.transport.bytes_in, 100)
            self.assertGreater(h.transport.bytes_out, 100)
        self.assertIn("TLS: on", self.server.status_text())


class PlainServerTest(unittest.TestCase):
    """--no-tls mode, and what happens when client and server disagree about TLS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = GameServer("127.0.0.1", 0, self.tmp.name, verbose=False, tls=False, discovery=False)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def test_plaintext_login_works(self):
        c = RawClient(self.server.port, tls=False)
        self.assertFalse(c.wait_type("hello")["tls"])
        self.assertTrue(c.login("alice")["ok"])
        c.close()

    def test_tls_client_cannot_talk_to_plain_server(self):
        with self.assertRaises((ssl.SSLError, ConnectionError, OSError)):
            RawClient(self.server.port, tls=True)


class TlsServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = GameServer("127.0.0.1", 0, self.tmp.name, verbose=False, discovery=False,
                                 cert_file=CERT_FILE, key_file=KEY_FILE)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def test_plain_client_is_rejected_by_tls_server(self):
        c = RawClient(self.server.port, tls=False)
        c.send(type="login", username="alice", password="secret")
        with self.assertRaises(ConnectionError):
            c.wait_type("login_result", timeout=8)
        c.close()

    def test_client_pins_the_certificate(self):
        import queue
        from client.network import Connection, KnownServers

        known = KnownServers(f"{self.tmp.name}/known.json")
        events = queue.Queue()

        def connect():
            conn = Connection(events, tls=True, known=known)
            conn.connect_async("127.0.0.1", self.server.port)
            kind, _, payload = events.get(timeout=5)
            return conn, kind, payload

        conn, kind, _ = connect()
        self.assertEqual(kind, "connected")
        self.assertTrue(conn.first_trust)
        self.assertEqual(known.get(f"127.0.0.1:{self.server.port}"), self.server.fingerprint)
        self.assertIn("TLS", conn.description())
        conn.close()
        known.set(f"127.0.0.1:{self.server.port}", "00:11:22")       # pretend we saw another certificate
        conn, kind, payload = connect()
        self.assertEqual(kind, "connect_failed")
        self.assertEqual(payload["kind"], "fingerprint")
        self.assertEqual(payload["got"], self.server.fingerprint)


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = GameServer("127.0.0.1", 0, self.tmp.name, verbose=False, discovery_port=0,
                                 cert_file=CERT_FILE, key_file=KEY_FILE, name="Test server")
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def test_udp_discovery_finds_the_server(self):
        from common import discovery
        found = discovery.discover(timeout=0.8, port=self.server.discovery_port, targets=["127.0.0.1"])
        self.assertEqual(len(found), 1)
        info = found[0]
        self.assertEqual((info["name"], info["port"], info["tls"], info["host"]),
                         ("Test server", self.server.port, True, "127.0.0.1"))
        self.assertEqual(info["fingerprint"], self.server.fingerprint)

    def test_discovery_ignores_garbage_and_rate_limits(self):
        from common import discovery
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        target = ("127.0.0.1", self.server.discovery_port)
        sock.sendto(b"hello?", target)
        sock.sendto(b'{"magic": "something-else", "type": "discover"}', target)
        with self.assertRaises(socket.timeout):
            sock.recvfrom(2048)
        for _ in range(60):
            sock.sendto(discovery.make_request(), target)
        replies = 0
        try:
            while True:
                sock.recvfrom(2048)
                replies += 1
        except socket.timeout:
            pass
        sock.close()
        self.assertGreater(replies, 0)
        self.assertLessEqual(replies, 25)


def tearDownModule():
    shutil.rmtree(CERT_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
