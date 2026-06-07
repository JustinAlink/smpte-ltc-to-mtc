# SMPTE-LTC to MTC Converter

## Description
This repository contains a Python application for converting, in real time,
SMPTE-LTC timecode (decoded from an audio input) to MIDI Time Code (MTC) sent
to a MIDI output port.

## Features
- Real-time conversion from SMPTE-LTC to MTC.
- Simple Tkinter graphical user interface for easy operation.
- Support for 24, 25 and 30 fps frame rates, with detection thresholds that
  adapt to the selected rate.
- Drop-frame (29.97 fps) timecode handling driven by the LTC drop-frame flag.
- Spec-correct MTC quarter-frame transmission (four messages per frame, eight
  spanning two frames) paced against a monotonic clock to avoid drift.
- Selectable audio input and MIDI output devices.
- A live sync indicator: green when locked to the incoming LTC, orange when the
  internal clock has drifted out of sync, red when stopped.

## Requirements
- Python 3.9+
- The dependencies listed in `requirements.txt`:
  - `PyAudio` (audio capture)
  - `mido` and `python-rtmidi` (MIDI output)
- A working microphone/line input carrying an LTC signal and an available MIDI
  output port (a virtual MIDI port works for testing).

> Note: on Linux, `python3-tk` (Tkinter) and the PortAudio development headers
> may need to be installed via your system package manager.

## Installation

**macOS / Linux:**
```bash
pip install -r requirements.txt
```

**Windows:**
```
pip install -r requirements.txt
```
If PyAudio fails to install on Windows (PortAudio not found), use the
pre-built wheel via pipwin:
```
pip install pipwin
pipwin install pyaudio
pip install mido python-rtmidi
```

If you see a *"Missing dependency"* dialog when launching, run the
appropriate install command above and try again.

## How to Use
Run the application:
```bash
python main.py
```
Then, in the window:
1. Select the audio input device carrying the LTC signal.
2. Choose the desired frame rate (24, 25 or 30 Hz).
3. Select the MIDI output port.
4. Click **Enable listener** to begin real-time conversion. Click again to stop;
   audio and MIDI resources are released cleanly each time.

## Testing
The hardware-independent logic (LTC frame decoding, timecode arithmetic,
drop-frame handling and MTC encoding) is covered by unit tests. The audio and
MIDI backends are stubbed, so the suite runs without any hardware:
```bash
python -m unittest test_main -v
```
