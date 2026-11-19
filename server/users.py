"""Persistent member accounts (register / login).

Passwords are never stored: we keep a random salt and a PBKDF2-SHA256 hash.
Data lives in a small JSON file that is replaced atomically on every write.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time

from common.protocol import check_password, check_username

PBKDF2_ROUNDS = 60_000


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


class UserDB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._users = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._users, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def register(self, name, password) -> tuple[bool, str]:
        error = check_username(name) or check_password(password)
        if error:
            return False, error
        salt = os.urandom(16)
        digest = _hash(password, salt)          # slow on purpose; done outside the lock
        with self._lock:
            key = name.lower()                  # usernames are case-insensitive
            if key in self._users:
                return False, "That username is already taken"
            self._users[key] = {"name": name, "salt": salt.hex(), "hash": digest.hex(),
                                "created": int(time.time()), "games": 0, "clears": 0}
            self._save()
        return True, "Account created - you can log in now"

    def verify(self, name, password) -> tuple[bool, str, str | None]:
        """Return (ok, message, canonical username)."""
        if check_username(name) or check_password(password):
            return False, "Invalid username or password", None
        with self._lock:
            record = dict(self._users.get(name.lower(), {}))
        if not record:
            return False, "Invalid username or password", None
        digest = _hash(password, bytes.fromhex(record["salt"]))
        if not hmac.compare_digest(digest.hex(), record["hash"]):
            return False, "Invalid username or password", None
        return True, "OK", record["name"]

    def progress(self, name: str) -> dict:
        """unlocked = index of the highest level this member may play (0 = level 1);
        best = best clear time per level index (as a string key)."""
        with self._lock:
            rec = self._users.get(name.lower(), {})
            return {"unlocked": rec.get("unlocked", 0), "best": dict(rec.get("best", {})),
                    "clears": rec.get("clears", 0), "games": rec.get("games", 0)}

    def record_clear(self, names, level_index: int, seconds: float, last_index: int) -> dict:
        """Store a cleared level for every member of the team.

        Clearing level i unlocks level i+1.  Returns {name: per-player result}.
        """
        results = {}
        with self._lock:
            for name in names:
                rec = self._users.get(name.lower())
                if rec is None:
                    continue
                old_unlocked = rec.get("unlocked", 0)
                rec["unlocked"] = max(old_unlocked, min(level_index + 1, last_index))
                rec["clears"] = rec.get("clears", 0) + 1
                if level_index == last_index:
                    rec["games"] = rec.get("games", 0) + 1     # finished the final level
                best = rec.setdefault("best", {})
                key = str(level_index)
                record = key not in best or seconds < best[key]
                if record:
                    best[key] = round(seconds, 2)
                results[name] = {"unlocked": rec["unlocked"], "best": best[key], "record": record,
                                 "newly_unlocked": rec["unlocked"] > old_unlocked}
            self._save()
        return results
