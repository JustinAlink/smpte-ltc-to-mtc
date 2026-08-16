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


class TestAdvanceTimecode(unittest.TestCase):
    def test_simple_increment(self):
        self.assertEqual(main.advance_timecode(0, 0, 0, 0, 1, 30), (0, 0, 0, 1))

    def test_frame_rollover(self):
        self.assertEqual(main.advance_timecode(0, 0, 0, 29, 1, 30), (0, 0, 1, 0))
        self.assertEqual(main.advance_timecode(0, 0, 0, 23, 1, 24), (0, 0, 1, 0))

    def test_minute_and_hour_rollover(self):
        self.assertEqual(main.advance_timecode(0, 0, 59, 29, 1, 30), (0, 1, 0, 0))
        self.assertEqual(main.advance_timecode(0, 59, 59, 29, 1, 30), (1, 0, 0, 0))

    def test_day_wraps_at_24h(self):
        self.assertEqual(main.advance_timecode(23, 59, 59, 29, 1, 30), (0, 0, 0, 0))

    def test_add_multiple_frames(self):
        self.assertEqual(main.advance_timecode(0, 0, 0, 28, 2, 30), (0, 0, 1, 0))

    def test_drop_frame_skips_at_minute_boundary(self):
        # Entering a non-multiple-of-ten minute skips frames 00 and 01.
        self.assertEqual(main.advance_timecode(0, 0, 59, 29, 1, 30, True), (0, 1, 0, 2))

    def test_drop_frame_no_skip_on_tenth_minute(self):
        self.assertEqual(main.advance_timecode(0, 9, 59, 29, 1, 30, True), (0, 10, 0, 0))

    def test_drop_frame_ignored_when_not_30fps(self):
        self.assertEqual(main.advance_timecode(0, 0, 59, 24, 1, 25, True), (0, 1, 0, 0))


class TestMtcQuarterFrameValues(unittest.TestCase):
    def test_eight_ordered_pairs(self):
        vals = main.mtc_quarter_frame_values(1, 2, 3, 4, 30)
        self.assertEqual(len(vals), 8)
        self.assertEqual([t for t, _ in vals], list(range(8)))

    def test_type7_encodes_rate_and_hour_tens(self):
        # 23h -> hour tens nibble = 1; 30 fps -> rate code 2.
        vals = main.mtc_quarter_frame_values(23, 0, 0, 0, 30)
        self.assertEqual(vals[7], (7, (2 << 1) | 1))
        # Single-digit hour -> hour tens 0; 24 fps -> rate code 0.
        vals = main.mtc_quarter_frame_values(5, 0, 0, 0, 24)
        self.assertEqual(vals[7], (7, (0 << 1) | 0))


class TestDecoderThresholds(unittest.TestCase):
    def test_thresholds_match_legacy_at_30fps(self):
        dec = main.LTCDecoder(rate=48000, fps=30)
        self.assertAlmostEqual(dec.min_gate, 7.0)
        self.assertAlmostEqual(dec.zero_threshold, 14.0)

    def test_thresholds_scale_with_fps(self):
        dec = main.LTCDecoder(rate=48000, fps=24)
        self.assertAlmostEqual(dec.min_gate, 8.75)
        self.assertAlmostEqual(dec.zero_threshold, 17.5)


class TestGetVolumeDb(unittest.TestCase):
    def test_silence_returns_neg_inf(self):
        self.assertEqual(main.get_volume_db(b'\x00\x00' * 100), float('-inf'))

    def test_empty_returns_neg_inf(self):
        self.assertEqual(main.get_volume_db(b''), float('-inf'))

    def test_known_signal(self):
        # 1000 LSB 16-bit samples: RMS = 1000, dB = 20*log10(1000) = 60.0
        data = b'\xe8\x03' * 200  # 0x03E8 = 1000 in little-endian s16
        self.assertAlmostEqual(main.get_volume_db(data), 60.0, places=5)

    def test_no_audioop_dependency(self):
        self.assertNotIn('audioop', dir(main))

    def test_stride_does_not_change_constant_signal(self):
        # Subsampling the meter must not shift the reading for a steady tone.
        data = b'\xe8\x03' * 2048
        self.assertAlmostEqual(
            main.get_volume_db(data, stride=1),
            main.get_volume_db(data, stride=8),
            places=9,
        )

    def test_buffer_shorter_than_stride_still_reads(self):
        # A single sample must not fall through the subsampling and read -inf.
        self.assertAlmostEqual(main.get_volume_db(b'\xe8\x03', stride=8), 60.0, places=5)


class TestDisambiguateNames(unittest.TestCase):
    def test_unique_names_unchanged(self):
        pairs = [('Mic A', 0), ('Mic B', 1)]
        self.assertEqual(main.disambiguate_names(pairs), pairs)

    def test_duplicates_get_suffixed(self):
        # Windows commonly reports several identically-named devices.
        pairs = [('Microphone', 0), ('Microphone', 3), ('Microphone', 7)]
        self.assertEqual(
            main.disambiguate_names(pairs),
            [('Microphone', 0), ('Microphone (2)', 3), ('Microphone (3)', 7)],
        )

    def test_all_indices_survive_the_name_map(self):
        # The point of the suffixing: every device stays reachable.
        pairs = [('Mic', 0), ('Mic', 1), ('Other', 2)]
        mapping = {name: idx for name, idx in main.disambiguate_names(pairs)}
        self.assertEqual(sorted(mapping.values()), [0, 1, 2])

    def test_empty(self):
        self.assertEqual(main.disambiguate_names([]), [])


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
