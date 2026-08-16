# Changelog

## v1.2.0

Reliability release. Focused on the ways the app could crash or mislead you on
a machine other than the developer's.

### Fixed — crashes

- **Starting with no microphone connected** no longer aborts with a Python
  traceback. The app now opens, explains that no audio input was found, and
  disables the listener button.
- **Failing to open the audio input** (device unplugged, already in use by
  another program, or refusing the format) no longer leaves the window stuck
  showing "Disable listener" over a listener that is not running. You get a
  dialog explaining what went wrong and the controls return to normal.
- **Closing the window while listening** no longer prints a traceback. The
  background transmitter is shut down before the window is destroyed.
- **Quickly switching the listener off and on** can no longer end up with two
  transmitters running at once, which previously produced doubled MIDI output.

### Fixed — correctness

- Devices that report the **same name** (common on Windows, e.g. several
  entries called "Microphone") are now numbered, so every one of them can
  actually be selected. Previously only the first was reachable.
- The frame-rate setting is no longer read from the background thread, which
  was the last remaining unsafe cross-thread UI access.
- A failing MIDI backend or MIDI port no longer takes the app down; LTC is
  still decoded and displayed, and the problem is reported in a dialog.
- The timecode readout now shows `--:--:--:--` when there is no signal instead
  of freezing on the last value it saw, and clears when you stop.

### Fixed — responsiveness

- **The window is no longer sluggish.** Audio capture was running on the GUI
  thread, and each read blocked it for ~43 ms at a time, leaving the interface
  frozen roughly 81% of the time. Capture now runs on its own thread and the
  GUI stays responsive. The gap between reads is gone too, so audio no longer
  backs up in the driver buffer.
- Timecode is picked up roughly four times sooner, from reading the input in
  smaller pieces (now viable with capture off the GUI thread).

### Fixed — input level meter

- **The meter was always a full red bar.** Levels were being reported relative
  to one sample step instead of to full scale, so every real signal came out
  as +34 to +90 instead of a negative dBFS value — permanently past the
  clipping threshold and permanently pinned at maximum width. It now reads in
  dBFS and moves with the signal: green with headroom, amber when hot, red
  near clipping, empty on silence.
- A numeric dBFS readout sits above the bar, so it is obvious at a glance
  whether audio is arriving at all.

### Changed

- The input level meter is cheaper to compute and no longer does work
  proportional to every single audio sample.

### Earlier in this line

Previous updates in the same effort added drop-frame (29.97) support,
spec-correct MIDI quarter-frame timing on a drift-free clock, frame-rate-aware
LTC decoding for 24/25/30 fps, a working microphone selector, a four-state
status indicator, a visual input-level meter, removal of the deprecated
`audioop` module (which is gone in Python 3.13), and a friendly dialog when
dependencies are missing instead of a raw import error.
