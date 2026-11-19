"""Tests for the TCP message framing and the shared validators."""
import unittest

from common import protocol as P


class FramingTest(unittest.TestCase):
    def test_round_trip_with_unicode(self):
        msg = {"type": "chat", "text": "協力方塊 hello"}
        self.assertEqual(P.FrameDecoder().feed(P.encode(msg)), [msg])

    def test_byte_by_byte_and_glued_frames(self):
        msgs = [{"type": "ping", "t": i} for i in range(5)]
        stream = b"".join(P.encode(m) for m in msgs)
        dec = P.FrameDecoder()
        out = []
        for i in range(len(stream)):            # worst case: one byte per recv()
            out.extend(dec.feed(stream[i:i + 1]))
        self.assertEqual(out, msgs)
        self.assertEqual(P.FrameDecoder().feed(stream), msgs)   # all frames in one recv()

    def test_rejects_bad_frames(self):
        with self.assertRaises(P.ProtocolError):
            P.FrameDecoder().feed((10 ** 7).to_bytes(4, "big"))
        with self.assertRaises(P.ProtocolError):
            P.FrameDecoder().feed((3).to_bytes(4, "big") + b"{x}")
        with self.assertRaises(P.ProtocolError):
            P.FrameDecoder().feed((2).to_bytes(4, "big") + b"[]")


class ValidationTest(unittest.TestCase):
    def test_validators(self):
        self.assertIsNone(P.check_username("Kenji_01"))
        self.assertIsNotNone(P.check_username("ab"))
        self.assertIsNotNone(P.check_username("bad name"))
        self.assertIsNotNone(P.check_password("abc"))
        self.assertEqual(P.clean_text(" hi\x00\n there ", 50), "hi there")
        self.assertEqual(len(P.clean_text("x" * 500, P.CHAT_MAX_LEN)), P.CHAT_MAX_LEN)
        self.assertIsNotNone(P.check_room_name("   "))


if __name__ == "__main__":
    unittest.main()
