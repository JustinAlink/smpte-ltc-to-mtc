"""Unit tests for the pure logic in main.py.

The audio (pyaudio) and MIDI (mido) backends are stubbed with mocks so the
module can be imported without the hardware dependencies installed. Only the
pure, hardware-independent functions are exercised here; the GUI bootstrap in
main() is never invoked.
"""

import sys
import unittest
from unittest import mock

# Stub hardware-backed dependencies before importing the module under test.
sys.modules.setdefault('pyaudio', mock.MagicMock())
sys.modules.setdefault('mido', mock.MagicMock())
sys.modules.setdefault('mido.backends', mock.MagicMock())
sys.modules.setdefault('mido.backends.rtmidi', mock.MagicMock())
sys.modules.setdefault('tkinter', mock.MagicMock())

import main  # noqa: E402


def encode_field(value: int, nbits: int) -> str:
    """Encode an integer LSB-first into an nbits string, matching how LTC
    transmits multi-bit fields."""
    return ''.join(str((value >> i) & 1) for i in range(nbits))


def build_frame(hh: int, mm: int, ss: int, ff: int) -> str:
    """Construct an 80-bit LTC frame string for the given timecode."""
    bits = ['0'] * 80

    def place(s: str, start: int) -> None:
        for k, ch in enumerate(s):
            bits[start + k] = ch

    place(encode_field(ff % 10, 4), 0)    # frame units
    place(encode_field(ff // 10, 2), 8)   # frame tens
    place(encode_field(ss % 10, 4), 16)   # second units
    place(encode_field(ss // 10, 3), 24)  # second tens
    place(encode_field(mm % 10, 4), 32)   # minute units
    place(encode_field(mm // 10, 3), 40)  # minute tens
    place(encode_field(hh % 10, 4), 48)   # hour units
    place(encode_field(hh // 10, 2), 56)  # hour tens
    place(main.SYNC_WORD, 64)             # sync word
    return ''.join(bits)


class TestBinToInt(unittest.TestCase):
    def test_lsb_first(self):
        # LTC is LSB-first: leftmost char carries weight 2^0.
        self.assertEqual(main.bin_to_int('1000'), 1)
        self.assertEqual(main.bin_to_int('0100'), 2)
        self.assertEqual(main.bin_to_int('1100'), 3)
        self.assertEqual(main.bin_to_int('0000'), 0)
        self.assertEqual(main.bin_to_int('1111'), 15)


class TestDecodeFrame(unittest.TestCase):
    def test_round_trip(self):
        frame = build_frame(1, 23, 45, 12)
        self.assertEqual(main.decode_frame(frame)['formatted_tc'], '01:23:45:12')

    def test_max_values(self):
        frame = build_frame(23, 59, 59, 29)
        self.assertEqual(main.decode_frame(frame)['formatted_tc'], '23:59:59:29')


class TestTimeToSeconds(unittest.TestCase):
    def test_respects_fps(self):
        # The historic bug hardcoded /30; these only hold with correct fps.
        self.assertEqual(main.time_to_seconds('00:00:00:12', 24), 0.5)
        self.assertEqual(main.time_to_seconds('00:00:01:00', 24), 1.0)
        self.assertEqual(main.time_to_seconds('00:00:00:25', 25), 1.0)
        self.assertEqual(main.time_to_seconds('00:00:01:15', 30), 1.5)

    def test_default_fps_is_30(self):
        self.assertEqual(main.time_to_seconds('00:00:01:15'), 1.5)


class TestCompareTimestamps(unittest.TestCase):
    def test_absolute_value(self):
        # Direction must not matter (historic bug allowed negatives through).
        self.assertEqual(main.compare_timestamps('00:00:01:00', '00:00:02:00', 30), 1.0)
        self.assertEqual(main.compare_timestamps('00:00:02:00', '00:00:01:00', 30), 1.0)


class TestDecimalToHexPair(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(main.decimal_to_hex_pair(0), [0, 0])
        self.assertEqual(main.decimal_to_hex_pair(255), [15, 15])
        self.assertEqual(main.decimal_to_hex_pair(23), [1, 7])    # 0x17
        self.assertEqual(main.decimal_to_hex_pair(14), [0, 14])   # 0x0E


class TestStrFrequencyToInt(unittest.TestCase):
    def test_known_and_unknown(self):
        self.assertEqual(main.str_frequency_to_int('24 Hz'), 24)
        self.assertEqual(main.str_frequency_to_int('25 Hz'), 25)
        self.assertEqual(main.str_frequency_to_int('30 Hz'), 30)
        self.assertEqual(main.str_frequency_to_int('bogus'), 0)


class TestLTCDecoder(unittest.TestCase):
    def test_reset_clears_state(self):
        dec = main.LTCDecoder()
        dec.output.extend(['1', '0', '1'])
        dec.sp = 99
        dec.reset()
        self.assertEqual(dec.output, [])
        self.assertEqual(dec.sp, 1)

    def test_silence_yields_nothing(self):
        dec = main.LTCDecoder()
        self.assertEqual(dec.feed(b'\x00\x00' * 100), [])

    def test_buffer_is_bounded_on_noise(self):
        # Alternating samples create constant polarity flips (no valid frame);
        # the buffer must stay bounded rather than grow without limit.
        dec = main.LTCDecoder()
        noise = (b'\x00\x80' + b'\xff\x7f') * 5000  # min/max 16-bit swings
        dec.feed(noise)
        self.assertLessEqual(len(dec.output), main.MAX_BIT_BUFFER)


if __name__ == '__main__':
    unittest.main()
