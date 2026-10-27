"""Tests for command encoding, reply buffering and reply classification."""

import unittest

from projector_controller import (
    POWER_OFF,
    POWER_ON,
    POWER_STATUS,
    MessageBuffer,
    PowerState,
    Reply,
    ReplyKind,
    Transaction,
    classify_reply,
    format_bytes,
)


class CommandEncodingTests(unittest.TestCase):
    def test_payloads_have_carriage_return_before_and_after(self):
        self.assertEqual(POWER_ON.payload, b"\r*pow=on#\r")
        self.assertEqual(POWER_OFF.payload, b"\r*pow=off#\r")
        self.assertEqual(POWER_STATUS.payload, b"\r*pow=?#\r")

    def test_only_status_is_a_query(self):
        self.assertTrue(POWER_STATUS.is_query)
        self.assertFalse(POWER_ON.is_query)
        self.assertFalse(POWER_OFF.is_query)

    def test_format_bytes_escapes_control_characters(self):
        self.assertEqual(format_bytes(b"\r*POW=ON#\r\n\x00"), "\\r*POW=ON#\\r\\n\\x00")


class MessageBufferTests(unittest.TestCase):
    def test_fragmented_reply_is_reassembled(self):
        buffer = MessageBuffer()
        self.assertEqual(buffer.feed(b"*PO"), [])
        self.assertEqual(buffer.feed(b"W=O"), [])
        self.assertEqual(buffer.feed(b"N"), [])
        self.assertEqual(buffer.feed(b"#\r\n"), ["*POW=ON#"])
        self.assertEqual(buffer.pending, b"")

    def test_empty_delimiters_are_ignored(self):
        buffer = MessageBuffer()
        self.assertEqual(buffer.feed(b"\r\r\n\n\r"), [])
        self.assertEqual(buffer.feed(b"\r\n*POW=OFF#\r\n\r\n"), ["*POW=OFF#"])

    def test_echo_and_reply_in_one_chunk(self):
        buffer = MessageBuffer()
        self.assertEqual(
            buffer.feed(b">*pow=?#\r\n*POW=ON#\r\n"), [">*pow=?#", "*POW=ON#"]
        )

    def test_hash_terminates_message_without_carriage_return(self):
        buffer = MessageBuffer()
        self.assertEqual(buffer.feed(b"*POW=OFF#"), ["*POW=OFF#"])

    def test_carriage_return_terminates_message_without_hash(self):
        buffer = MessageBuffer()
        self.assertEqual(buffer.feed(b"Illegal format\r"), ["Illegal format"])

    def test_flush_returns_unterminated_tail(self):
        buffer = MessageBuffer()
        self.assertEqual(buffer.feed(b"*POW=O"), [])
        self.assertEqual(buffer.flush(), "*POW=O")
        self.assertIsNone(buffer.flush())


class ClassifyReplyTests(unittest.TestCase):
    def kind(self, message, command=POWER_STATUS):
        return classify_reply(message, command).kind

    def test_status_replies(self):
        self.assertIs(self.kind("*POW=ON#"), ReplyKind.POWER_ON)
        self.assertIs(self.kind("*POW=OFF#"), ReplyKind.POWER_OFF)
        self.assertIs(self.kind("*pow=off#"), ReplyKind.POWER_OFF)
        self.assertIs(self.kind(">*POW=ON#"), ReplyKind.POWER_ON)

    def test_echo_of_query(self):
        self.assertIs(self.kind("*pow=?#"), ReplyKind.ECHO)
        self.assertIs(self.kind(">*pow=?#"), ReplyKind.ECHO)

    def test_power_text_after_set_command_is_not_a_state(self):
        self.assertIs(self.kind("*pow=on#", POWER_ON), ReplyKind.ECHO)
        self.assertIs(self.kind("*POW=ON#", POWER_ON), ReplyKind.ECHO)
        self.assertIs(self.kind("*POW=OFF#", POWER_OFF), ReplyKind.ECHO)

    def test_projector_errors(self):
        self.assertIs(self.kind("*Illegal format#"), ReplyKind.ILLEGAL_FORMAT)
        self.assertIs(self.kind("Unsupported item"), ReplyKind.UNSUPPORTED_ITEM)
        self.assertIs(self.kind("*Block item#", POWER_ON), ReplyKind.BLOCK_ITEM)
        self.assertIs(self.kind("*BLOCK ITEM#"), ReplyKind.BLOCK_ITEM)

    def test_prompt_and_unrecognized(self):
        self.assertIs(self.kind(">"), ReplyKind.PROMPT)
        self.assertIs(self.kind("*POW=WARMING#"), ReplyKind.UNRECOGNIZED)
        self.assertIs(self.kind("garbage\\xff"), ReplyKind.UNRECOGNIZED)


class TransactionTests(unittest.TestCase):
    def tx(self, command, *messages):
        return Transaction(command, replies=[classify_reply(m, command) for m in messages])

    def test_confirmed_state_requires_status_reply(self):
        self.assertIs(self.tx(POWER_STATUS, "*pow=?#", "*POW=ON#").power_state, PowerState.ON)
        self.assertIsNone(self.tx(POWER_STATUS, "*pow=?#").power_state)
        self.assertIsNone(self.tx(POWER_ON, "*POW=ON#").power_state)

    def test_echo_only(self):
        self.assertTrue(self.tx(POWER_STATUS, ">", "*pow=?#").echo_only)
        self.assertFalse(self.tx(POWER_STATUS).echo_only)

    def test_conflicting_states_are_not_confirmed(self):
        self.assertIsNone(self.tx(POWER_STATUS, "*POW=ON#", "*POW=OFF#").power_state)

    def test_error_overrides_state(self):
        tx = self.tx(POWER_STATUS, "*POW=ON#", "*Block item#")
        self.assertIsNone(tx.power_state)
        self.assertEqual(tx.error_reply, Reply("*Block item#", ReplyKind.BLOCK_ITEM))


if __name__ == "__main__":
    unittest.main()
