import struct
import pyaudio
import math
import time
import threading
import mido
import mido.backends.rtmidi
import tkinter as tk

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 48000
CHUNK = 2048
SYNC_WORD = '0011111111111101'
# Upper bound on accumulated bits between sync words; prevents unbounded
# growth when the input is noise. Comfortably larger than one 80-bit frame.
MAX_BIT_BUFFER = 200

# Biphase-mark detection thresholds, expressed as fractions of the samples
# per bit (= RATE / (80 * fps)). At 48 kHz / 30 fps the samples-per-bit is 20,
# so these reproduce the original hand-tuned constants of 7 and 14.
MIN_GATE_FACTOR = 0.35
ZERO_THRESHOLD_FACTOR = 0.70

# Seconds without a decoded LTC frame before the indicator shows "no signal".
NO_SIGNAL_TIMEOUT = 2.0

jam = '00:00:00:00'
drop_frame_mode = False
_tc_lock = threading.Lock()
_audio_instance: 'pyaudio.PyAudio | None' = None
_audio_stream = None
_midi_port = None
_stop_event = threading.Event()
_last_frame_time: float = 0.0

# Maps microphone display name -> PyAudio device index. Populated by main().
microphone_indices: 'dict[str, int]' = {}


def bin_to_bytes(a: str, size: int = 1) -> bytes:
    return int(a, 2).to_bytes(size, byteorder='little')


def bin_to_int(a: str) -> int:
    # LTC fields are transmitted LSB-first; enumerate assigns 2^0 to the
    # first (least-significant) bit, which matches the LTC standard.
    return sum(int(j) * 2 ** i for i, j in enumerate(a))


def decode_frame(frame: str) -> dict:
    o = {}
    o['frame_units'] = bin_to_int(frame[:4])
    o['user_bits_1'] = int.from_bytes(bin_to_bytes(frame[4:8]), byteorder='little')
    o['frame_tens'] = bin_to_int(frame[8:10])
    o['drop_frame'] = int.from_bytes(bin_to_bytes(frame[10]), byteorder='little')
    o['color_frame'] = int.from_bytes(bin_to_bytes(frame[11]), byteorder='little')
    o['user_bits_2'] = int.from_bytes(bin_to_bytes(frame[12:16]), byteorder='little')
    o['sec_units'] = bin_to_int(frame[16:20])
    o['user_bits_3'] = int.from_bytes(bin_to_bytes(frame[20:24]), byteorder='little')
    o['sec_tens'] = bin_to_int(frame[24:27])
    o['flag_1'] = int.from_bytes(bin_to_bytes(frame[27]), byteorder='little')
    o['user_bits_4'] = int.from_bytes(bin_to_bytes(frame[28:32]), byteorder='little')
    o['min_units'] = bin_to_int(frame[32:36])
    o['user_bits_5'] = int.from_bytes(bin_to_bytes(frame[36:40]), byteorder='little')
    o['min_tens'] = bin_to_int(frame[40:43])
    o['flag_2'] = int.from_bytes(bin_to_bytes(frame[43]), byteorder='little')
    o['user_bits_6'] = int.from_bytes(bin_to_bytes(frame[44:48]), byteorder='little')
    o['hour_units'] = bin_to_int(frame[48:52])
    o['user_bits_7'] = int.from_bytes(bin_to_bytes(frame[52:56]), byteorder='little')
    o['hour_tens'] = bin_to_int(frame[56:58])
    o['bgf'] = int.from_bytes(bin_to_bytes(frame[58]), byteorder='little')
    o['flag_3'] = int.from_bytes(bin_to_bytes(frame[59]), byteorder='little')
    o['user_bits_8'] = int.from_bytes(bin_to_bytes(frame[60:64]), byteorder='little')
    o['sync_word'] = int.from_bytes(bin_to_bytes(frame[64:], 2), byteorder='little')
    o['formatted_tc'] = "{:02d}:{:02d}:{:02d}:{:02d}".format(
        o['hour_tens'] * 10 + o['hour_units'],
        o['min_tens'] * 10 + o['min_units'],
        o['sec_tens'] * 10 + o['sec_units'],
        o['frame_tens'] * 10 + o['frame_units'],
    )
    return o


class LTCDecoder:
    """Stateful biphase-mark LTC decoder.

    State (the partial bit buffer and the phase-tracking variables) is kept
    between calls to ``feed`` so that LTC bits and frames straddling audio
    chunk boundaries are decoded correctly rather than dropped. The detection
    thresholds are derived from the sample rate and frame rate so decoding is
    robust at 24/25 fps, not just 30.
    """

    def __init__(self, rate: int = RATE, fps: int = 30) -> None:
        self.configure(rate, fps)
        self.reset()

    def configure(self, rate: int, fps: int) -> None:
        samples_per_bit = rate / (80 * fps)
        self.min_gate = samples_per_bit * MIN_GATE_FACTOR
        self.zero_threshold = samples_per_bit * ZERO_THRESHOLD_FACTOR

    def reset(self) -> None:
        self.output: list[str] = []
        self.last = None
        self.toggle = True
        self.sp = 1
        self.drop_frame = False

    def feed(self, wave_frames: bytes) -> list[str]:
        """Decode a chunk of 16-bit mono audio, returning any completed
        timecode strings (usually zero or one)."""
        results: list[str] = []
        n_samples = len(wave_frames) // 2
        samples = struct.unpack_from(f'<{n_samples}h', wave_frames)

        for sample in samples:
            cyc = 'Neg' if sample < 0 else 'Pos'

            if cyc != self.last:
                if self.sp >= self.min_gate:
                    if self.sp > self.zero_threshold:
                        bit = '0'
                    elif self.toggle:
                        bit = '1'
                    else:
                        bit = ''

                    if bit:
                        self.output.append(bit)
                    self.toggle = not self.toggle if self.sp <= self.zero_threshold else True

                    if len(self.output) >= len(SYNC_WORD):
                        tail = ''.join(self.output[-len(SYNC_WORD):])
                        if tail == SYNC_WORD and len(self.output) >= 80:
                            frame_data = ''.join(self.output[-80:])
                            self.output.clear()
                            decoded = decode_frame(frame_data)
                            self.drop_frame = bool(decoded['drop_frame'])
                            results.append(decoded['formatted_tc'])
                        elif len(self.output) > MAX_BIT_BUFFER:
                            # Drop the oldest bits to bound memory on noisy input.
                            del self.output[:-MAX_BIT_BUFFER]
                self.sp = 1
            else:
                self.sp += 1
            self.last = cyc
        return results


_decoder = LTCDecoder()


def advance_timecode(h: int, m: int, s: int, f: int, frames_to_add: int,
                     fps: int, drop_frame: bool = False) -> 'tuple[int, int, int, int]':
    """Advance a timecode by ``frames_to_add`` frames, rolling over fields and
    applying SMPTE drop-frame compensation when requested.

    Drop-frame (29.97 fps, signalled by the LTC drop-frame flag and carried at
    a nominal 30 fps) skips frame numbers 00 and 01 at the top of every minute
    except minutes that are multiples of ten.
    """
    for _ in range(frames_to_add):
        f += 1
        if f >= fps:
            f = 0
            s += 1
        if s >= 60:
            s = 0
            m += 1
            if drop_frame and fps == 30 and (m % 10) != 0:
                f = 2
        if m >= 60:
            m = 0
            h += 1
        if h >= 24:
            h = 0
    return h, m, s, f


def mtc_quarter_frame_values(hours: int, minutes: int, seconds: int,
                             frames: int, fps: int) -> 'list[tuple[int, int]]':
    """Return the eight MTC quarter-frame (frame_type, frame_value) pairs that
    encode a single timecode, in transmission order (types 0..7)."""
    mtc_hours = decimal_to_hex_pair(hours)
    mtc_minutes = decimal_to_hex_pair(minutes)
    mtc_seconds = decimal_to_hex_pair(seconds)
    mtc_frames = decimal_to_hex_pair(frames)
    rate = {24: 0, 25: 1, 30: 2}.get(fps, 2)
    return [
        (0, mtc_frames[1]),
        (1, mtc_frames[0]),
        (2, mtc_seconds[1]),
        (3, mtc_seconds[0]),
        (4, mtc_minutes[1]),
        (5, mtc_minutes[0]),
        (6, mtc_hours[1]),
        (7, (rate << 1) | mtc_hours[0]),
    ]


def _send_quarter_frame(frame_type: int, frame_value: int) -> None:
    if _midi_port is None:
        return
    try:
        _midi_port.send(mido.Message('quarter_frame', frame_type=frame_type, frame_value=frame_value))
    except Exception as e:
        print(f"MIDI send error: {e}")


def print_tc() -> None:
    """Free-running MTC transmitter.

    Emits MIDI quarter-frame messages at the spec rate of four per frame
    (eight messages span two frames, encoding one complete timecode). The
    cadence is locked to a monotonic clock so it does not drift, and the
    internal counter is continually re-synced to the latest decoded LTC value.
    All Tkinter widget updates are dispatched to the main thread via
    frame.after() so this thread never touches the UI directly.
    """
    freq = str_frequency_to_int(selected_frequency.get())
    if freq == 0:
        return
    qf_interval = 1.0 / (4 * freq)

    with _tc_lock:
        current_jam = jam
        df = drop_frame_mode
    h, m, s, f = [int(x) for x in current_jam.split(':')]
    last_jam = current_jam

    qf_index = 0
    qf_values: 'list[tuple[int, int]] | None' = None
    next_t = time.monotonic()

    while not _stop_event.is_set():
        # At the start of each eight-message cycle, re-sync to the decoded LTC,
        # evaluate signal state, and latch the timecode for this cycle.
        if qf_index == 0:
            with _tc_lock:
                current_jam = jam
                df = drop_frame_mode
            if current_jam != last_jam:
                h, m, s, f = [int(x) for x in current_jam.split(':')]
                last_jam = current_jam

            tcp = "{:02d}:{:02d}:{:02d}:{:02d}".format(h, m, s, f)
            no_signal = (time.monotonic() - _last_frame_time) > NO_SIGNAL_TIMEOUT

            if no_signal:
                qf_values = None
                frame.after(0, lambda: status_square.configure(bg='grey'))
            elif compare_timestamps(tcp, current_jam, freq) < 1.5:
                qf_values = mtc_quarter_frame_values(h, m, s, f, freq)
                _tcp = tcp
                frame.after(0, lambda t=_tcp: label_timecode.config(text=f"Timecode : {t}"))
                frame.after(0, lambda: status_square.configure(bg='green'))
            else:
                qf_values = None
                frame.after(0, lambda: status_square.configure(bg='orange'))

        if qf_values is not None:
            _send_quarter_frame(*qf_values[qf_index])

        qf_index += 1
        if qf_index >= 8:
            qf_index = 0
            # Eight quarter-frames have elapsed: two frames of real time.
            h, m, s, f = advance_timecode(h, m, s, f, 2, freq, df)

        next_t += qf_interval
        delay = next_t - time.monotonic()
        if _stop_event.wait(timeout=max(0.0, delay)):
            break
        if delay < -qf_interval:
            # Fell more than one interval behind; rebase to avoid a burst.
            next_t = time.monotonic()


def _update_volume_bar(db: float) -> None:
    """Redraw the volume meter canvas. Called from the main thread only."""
    volume_canvas.delete('bar')
    w = volume_canvas.winfo_width()
    if w < 2:
        return
    if db <= -60.0 or db == float('-inf'):
        return
    frac = max(0.0, min(1.0, (db - (-60.0)) / 60.0))
    bar_px = max(1, int(w * frac))
    color = '#ff4444' if db > -3 else '#ffcc00' if db > -12 else '#44cc44'
    volume_canvas.create_rectangle(0, 0, bar_px, 16, fill=color, outline='', tags='bar')


def loop_decode_ltc(stream) -> None:
    global jam, drop_frame_mode, _last_frame_time
    if not enable_listening.get():
        return
    try:
        data = stream.read(CHUNK, exception_on_overflow=False)
    except Exception:
        return
    _update_volume_bar(get_volume_db(data))
    results = _decoder.feed(data)
    if results:
        with _tc_lock:
            jam = results[-1]
            drop_frame_mode = _decoder.drop_frame
        _last_frame_time = time.monotonic()
    if enable_listening.get():
        frame.after(10, lambda: loop_decode_ltc(stream))


def _close_audio() -> None:
    global _audio_stream, _audio_instance
    if _audio_stream is not None:
        try:
            if _audio_stream.is_active():
                _audio_stream.stop_stream()
            _audio_stream.close()
        except Exception:
            pass
        _audio_stream = None
    if _audio_instance is not None:
        try:
            _audio_instance.terminate()
        except Exception:
            pass
        _audio_instance = None


def _close_midi() -> None:
    global _midi_port
    if _midi_port is not None:
        try:
            _midi_port.close()
        except Exception:
            pass
        _midi_port = None


def init_ltc_listener() -> None:
    global _audio_instance, _audio_stream, _midi_port, _last_frame_time

    _close_audio()
    _close_midi()
    _stop_event.clear()
    _last_frame_time = 0.0

    freq = str_frequency_to_int(selected_frequency.get())
    if freq:
        _decoder.configure(RATE, freq)
    _decoder.reset()

    midi_port_name = selected_midi.get()
    if midi_port_name:
        try:
            _midi_port = mido.open_output(midi_port_name)
        except Exception as e:
            print(f"Failed to open MIDI port: {e}")

    # Resolve the selected microphone name back to its PyAudio device index.
    # None falls back to the system default input device.
    device_index = microphone_indices.get(selected_microphone.get())

    _audio_instance = pyaudio.PyAudio()
    t = threading.Thread(target=print_tc, daemon=True)
    t.start()

    _audio_stream = _audio_instance.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK,
        input_device_index=device_index,
    )
    loop_decode_ltc(_audio_stream)


def decimal_to_hex_pair(decimal_value: int) -> list[int]:
    binary_value = bin(decimal_value)[2:].zfill(8)
    return [int(binary_value[:4], 2), int(binary_value[4:], 2)]


def time_to_seconds(time: str, fps: int = 30) -> float:
    hh, mm, ss, ff = map(int, time.split(':'))
    return hh * 3600 + mm * 60 + ss + ff / fps


def compare_timestamps(timestamp1: str, timestamp2: str, fps: int = 30) -> float:
    return abs(time_to_seconds(timestamp1, fps) - time_to_seconds(timestamp2, fps))


def get_default_input_device_name() -> str:
    p = pyaudio.PyAudio()
    default_index = p.get_default_input_device_info()['index']
    default_name = p.get_device_info_by_index(default_index)['name']
    p.terminate()
    return default_name


def get_available_microphones() -> 'list[tuple[str, int]]':
    """Return (name, device_index) pairs for every input-capable device,
    with the system default device first."""
    p = pyaudio.PyAudio()
    info = p.get_host_api_info_by_index(0)
    num_devices = info.get('deviceCount')

    microphones = []
    for i in range(num_devices):
        device_info = p.get_device_info_by_index(i)
        if device_info.get('maxInputChannels') > 0:
            microphones.append((device_info['name'], i))
    p.terminate()

    default_name = get_default_input_device_name()
    for idx, (name, _) in enumerate(microphones):
        if name == default_name:
            microphones.insert(0, microphones.pop(idx))
            break

    return microphones


def get_available_midis() -> list[str]:
    return list(mido.get_output_names())


def str_frequency_to_int(s: str) -> int:
    if s == "24 Hz":
        return 24
    elif s == "25 Hz":
        return 25
    elif s == "30 Hz":
        return 30
    else:
        return 0


def get_volume_db(data: bytes) -> float:
    n = len(data) // 2
    if n == 0:
        return float('-inf')
    samples = struct.unpack_from(f'<{n}h', data)
    mean_sq = sum(s * s for s in samples) / n
    if mean_sq == 0:
        return float('-inf')
    return 20 * math.log10(math.sqrt(mean_sq))


def toggle_read_ltc() -> None:
    enable_listening.set(not enable_listening.get())

    if enable_listening.get():
        # Grey until the first LTC frame arrives.
        status_square.configure(bg='grey')
        toggle_button.configure(text="Disable listener")
        label_microphone.configure(state="disabled")
        label_frequency.configure(state="disabled")
        label_midi.configure(state="disabled")
        init_ltc_listener()
    else:
        _stop_event.set()
        status_square.configure(bg='red')
        volume_canvas.delete('bar')
        toggle_button.configure(text="Enable listener")
        label_microphone.configure(state="normal")
        label_frequency.configure(state="normal")
        label_midi.configure(state="normal")
        _close_audio()
        _close_midi()


def main() -> None:
    global microphone_indices
    global frame, selected_microphone, selected_frequency, selected_midi
    global enable_listening, status_square
    global label_microphone, label_frequency, label_midi
    global toggle_button, label_timecode, volume_canvas

    # Defines values from lists
    mic_list = get_available_microphones()
    microphone_indices = {name: index for name, index in mic_list}
    microphones_options = [name for name, _ in mic_list] or ["(no microphone)"]
    frequencies_options = ["24 Hz", "25 Hz", "30 Hz"]
    midis_options = get_available_midis() or ["(no MIDI output)"]

    # Create main frame
    frame = tk.Tk()
    frame.title("SMPTE LTC to MTC 1.1.0")
    frame.geometry("300x480")
    frame.resizable(width=False, height=False)

    # Define variables from tk
    selected_microphone = tk.StringVar(value=microphones_options[0])
    selected_frequency = tk.StringVar(value=frequencies_options[0])
    selected_midi = tk.StringVar(value=midis_options[0])
    enable_listening = tk.BooleanVar(value=False)

    # Configure grid to center elements
    for i in range(13):
        frame.grid_rowconfigure(i, weight=1)
    for i in range(12):
        frame.grid_columnconfigure(i, weight=1)

    # Status indicator: red=stopped, grey=no signal, orange=out of sync, green=locked
    status_square = tk.Canvas(frame, width=50, height=50, bg='red', highlightthickness=0)
    status_square.grid(row=0, column=4, pady=10, sticky="n")

    # Microphone selector
    tk.Label(frame, text="Select microphone", font=("Helvetica", 10, "bold")).grid(row=1, column=4, pady=5, sticky="n")
    label_microphone = tk.OptionMenu(frame, selected_microphone, *microphones_options)
    label_microphone.grid(row=2, column=4, pady=5, sticky="n")

    # Frequency selector
    tk.Label(frame, text="Select frequency", font=("Helvetica", 10, "bold")).grid(row=3, column=4, pady=5, sticky="n")
    label_frequency = tk.OptionMenu(frame, selected_frequency, *frequencies_options)
    label_frequency.grid(row=4, column=4, pady=5, sticky="n")

    # MIDI output selector
    tk.Label(frame, text="Select MIDI output", font=("Helvetica", 10, "bold")).grid(row=6, column=4, pady=5, sticky="n")
    label_midi = tk.OptionMenu(frame, selected_midi, *midis_options)
    label_midi.grid(row=7, column=4, pady=5, sticky="n")

    # Toggle button
    toggle_button = tk.Button(frame, text="Enable listener", command=toggle_read_ltc)
    toggle_button.grid(row=8, column=4, pady=10, sticky="n")

    # Timecode display
    label_timecode = tk.Label(frame, text="Timecode", font=("Helvetica", 10, "bold"))
    label_timecode.grid(row=9, column=4, pady=10, sticky="n")

    # Input level label + volume meter canvas
    tk.Label(frame, text="Input Level", font=("Helvetica", 9)).grid(
        row=10, column=0, columnspan=12, padx=20, sticky="w")
    volume_canvas = tk.Canvas(frame, height=16, bg='#2b2b2b', highlightthickness=0)
    volume_canvas.grid(row=11, column=0, columnspan=12, padx=20, pady=(0, 8), sticky="ew")

    frame.mainloop()


if __name__ == "__main__":
    main()
