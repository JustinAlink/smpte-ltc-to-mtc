import pyaudio
import audioop
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

jam = '00:00:00:00'
_tc_lock = threading.Lock()
_audio_instance: 'pyaudio.PyAudio | None' = None
_audio_stream = None
_midi_port = None

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
    chunk boundaries are decoded correctly rather than dropped.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.output: list[str] = []
        self.last = None
        self.toggle = True
        self.sp = 1

    def feed(self, wave_frames: bytes) -> list[str]:
        """Decode a chunk of 16-bit mono audio, returning any completed
        timecode strings (usually zero or one)."""
        results: list[str] = []
        for i in range(0, len(wave_frames), 2):
            data = wave_frames[i:i + 2]
            cyc = 'Neg' if audioop.minmax(data, 2)[0] < 0 else 'Pos'

            if cyc != self.last:
                if self.sp >= 7:
                    if self.sp > 14:
                        bit = '0'
                    elif self.toggle:
                        bit = '1'
                    else:
                        bit = ''

                    if bit:
                        self.output.append(bit)
                    self.toggle = not self.toggle if self.sp <= 14 else True

                    if len(self.output) >= len(SYNC_WORD):
                        tail = ''.join(self.output[-len(SYNC_WORD):])
                        if tail == SYNC_WORD and len(self.output) >= 80:
                            frame_data = ''.join(self.output[-80:])
                            self.output.clear()
                            results.append(decode_frame(frame_data)['formatted_tc'])
                        elif len(self.output) > MAX_BIT_BUFFER:
                            # Drop the oldest bits to bound memory on noisy input.
                            del self.output[:-MAX_BIT_BUFFER]
                self.sp = 1
            else:
                self.sp += 1
            self.last = cyc
        return results


_decoder = LTCDecoder()


def print_tc() -> None:
    freq = str_frequency_to_int(selected_frequency.get())
    inter = 1 / freq

    with _tc_lock:
        current_jam = jam
    h, m, s, f = [int(x) for x in current_jam.split(':')]
    last_jam = current_jam

    while enable_listening.get():
        with _tc_lock:
            current_jam = jam

        if current_jam != last_jam:
            h, m, s, f = [int(x) for x in current_jam.split(':')]
            last_jam = current_jam

        tcp = "{:02d}:{:02d}:{:02d}:{:02d}".format(h, m, s, f)

        if compare_timestamps(tcp, current_jam, freq) < 1.5:
            send_mtc_signal(tcp)
            status_color.set("green")
        else:
            status_color.set("orange")
        status_square.configure(bg=status_color.get())

        time.sleep(inter)
        f += 1
        if f >= freq:
            f = 0
            s += 1
        if s >= 60:
            s = 0
            m += 1
        if m >= 60:
            m = 0
            h += 1


def loop_decode_ltc(stream) -> None:
    global jam
    if not enable_listening.get():
        return
    try:
        data = stream.read(CHUNK, exception_on_overflow=False)
    except Exception:
        return
    volume_db = get_volume_db(data)
    label_volume.config(text=f"Volume: {round(volume_db)} dB")
    for tc in _decoder.feed(data):
        with _tc_lock:
            jam = tc
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
    global _audio_instance, _audio_stream, _midi_port

    _close_audio()
    _close_midi()
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


def send_mtc_signal(timecode_str: str) -> None:
    if _midi_port is None:
        return

    frequency = str_frequency_to_int(selected_frequency.get())
    if frequency == 0:
        return

    try:
        hours, minutes, seconds, frames = map(int, timecode_str.split(':'))
    except (ValueError, IndexError):
        raise ValueError("Invalid timecode format. Use HH:MM:SS:FF format.")

    if not 0 <= hours < 24 or not 0 <= minutes < 60 or not 0 <= seconds < 60 or not 0 <= frames < frequency:
        raise ValueError("Invalid timecode values.")

    label_timecode.config(text=f"Timecode : {timecode_str}")

    mtc_hours = decimal_to_hex_pair(hours)
    mtc_minutes = decimal_to_hex_pair(minutes)
    mtc_seconds = decimal_to_hex_pair(seconds)
    mtc_frames = decimal_to_hex_pair(frames)

    mtc_frequency = {24: 0, 25: 1, 30: 2}.get(frequency, 2)

    try:
        _midi_port.send(mido.Message('quarter_frame', frame_type=0, frame_value=mtc_frames[1]))
        _midi_port.send(mido.Message('quarter_frame', frame_type=1, frame_value=mtc_frames[0]))
        _midi_port.send(mido.Message('quarter_frame', frame_type=2, frame_value=mtc_seconds[1]))
        _midi_port.send(mido.Message('quarter_frame', frame_type=3, frame_value=mtc_seconds[0]))
        _midi_port.send(mido.Message('quarter_frame', frame_type=4, frame_value=mtc_minutes[1]))
        _midi_port.send(mido.Message('quarter_frame', frame_type=5, frame_value=mtc_minutes[0]))
        _midi_port.send(mido.Message('quarter_frame', frame_type=6, frame_value=mtc_hours[1]))
        _midi_port.send(mido.Message('quarter_frame', frame_type=7, frame_value=(mtc_frequency << 1) | mtc_hours[0]))
    except Exception as e:
        print(f"MIDI send error: {e}")


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


def get_volume_db(data: bytes, sample_width: int = 2) -> float:
    try:
        rms = audioop.rms(data, sample_width)
        if rms > 0:
            return 20 * math.log10(rms)
        return float('-inf')
    except Exception as e:
        print(f"Erreur lors du calcul du volume : {e}")
        return float('-inf')


def toggle_read_ltc() -> None:
    enable_listening.set(not enable_listening.get())

    if enable_listening.get():
        status_color.set("Orange")
        status_square.configure(bg=status_color.get())
        toggle_button.configure(text="Disable listener")
        label_microphone.configure(state="disabled")
        label_frequency.configure(state="disabled")
        label_midi.configure(state="disabled")
        init_ltc_listener()
    else:
        status_color.set("Red")
        status_square.configure(bg=status_color.get())
        toggle_button.configure(text="Enable listener")
        label_microphone.configure(state="normal")
        label_frequency.configure(state="normal")
        label_midi.configure(state="normal")
        _close_audio()
        _close_midi()


def main() -> None:
    global microphone_indices
    global frame, selected_microphone, selected_frequency, selected_midi
    global enable_listening, status_color, status_square
    global label_microphone, label_frequency, label_midi
    global toggle_button, label_timecode, label_volume

    # Defines values from lists
    mic_list = get_available_microphones()
    microphone_indices = {name: index for name, index in mic_list}
    microphones_options = [name for name, _ in mic_list] or ["(no microphone)"]
    frequencies_options = ["24 Hz", "25 Hz", "30 Hz"]
    midis_options = get_available_midis() or ["(no MIDI output)"]

    # Create main frame
    frame = tk.Tk()
    frame.title("SMPTE LTC to MTC 1.1.0")
    frame.geometry("300x450")
    frame.resizable(width=False, height=False)

    # Define variables from tk
    selected_microphone = tk.StringVar(value=microphones_options[0])
    selected_frequency = tk.StringVar(value=frequencies_options[0])
    selected_midi = tk.StringVar(value=midis_options[0])
    enable_listening = tk.BooleanVar(value=False)
    status_color = tk.StringVar(value="Red")

    # Configure grid to center elements
    for i in range(12):
        frame.grid_rowconfigure(i, weight=1)
        frame.grid_columnconfigure(i, weight=1)

    # Draw status square
    status_square = tk.Canvas(frame, width=50, height=50, bg="red")
    status_square.grid(row=0, column=4, pady=10, sticky="n")

    # Draw microphone selector
    tk.Label(frame, text="Select microphone", font=("Helvetica", 10, "bold")).grid(row=1, column=4, pady=5, sticky="n")
    label_microphone = tk.OptionMenu(frame, selected_microphone, *microphones_options)
    label_microphone.grid(row=2, column=4, pady=5, sticky="n")

    # Draw frequency selector
    tk.Label(frame, text="Select frequency", font=("Helvetica", 10, "bold")).grid(row=3, column=4, pady=5, sticky="n")
    label_frequency = tk.OptionMenu(frame, selected_frequency, *frequencies_options)
    label_frequency.grid(row=4, column=4, pady=5, sticky="n")

    # Draw MIDI output selector
    tk.Label(frame, text="Select MIDI output", font=("Helvetica", 10, "bold")).grid(row=6, column=4, pady=5, sticky="n")
    label_midi = tk.OptionMenu(frame, selected_midi, *midis_options)
    label_midi.grid(row=7, column=4, pady=5, sticky="n")

    # Draw toggle button
    toggle_button = tk.Button(frame, text="Enable listener", command=toggle_read_ltc)
    toggle_button.grid(row=8, column=4, pady=10, sticky="n")

    # Draw timecode
    label_timecode = tk.Label(frame, text="Timecode", font=("Helvetica", 10, "bold"))
    label_timecode.grid(row=9, column=4, pady=10, sticky="n")

    # Draw volume
    label_volume = tk.Label(frame, text="Volume", font=("Helvetica", 10, "bold"))
    label_volume.grid(row=11, column=4, pady=10, sticky="n")

    # Starting main loop
    frame.mainloop()


if __name__ == "__main__":
    main()
