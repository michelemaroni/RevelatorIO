# io24 for Linux

A native Linux control host for the PreSonus Revelator io24.

The io24 already works as a class-compliant audio interface on Linux. What
Linux does not get out of the box are its mixer and DSP controls, which sit
behind a vendor USB protocol. This project opens those controls without
Windows or macOS. You get a GTK4 desktop app, a command-line tool, preset and
scene support, and detailed protocol notes if you want to dig deeper.

The main interface is a native desktop app. There is no browser or phone
controller to set up.

## Quick start

Here is a clean setup for Debian or Ubuntu:

```bash
sudo apt install build-essential libusb-1.0-0 pipewire-bin wireplumber \
  python3-gi python3-gi-cairo python3-venv gir1.2-gtk-4.0 gir1.2-adw-1
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install .
sudo cp 70-presonus-io24.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

The virtual environment keeps the Python packages local while still using your
distro's GTK bindings. Replug the interface, then launch the Host:

```bash
.venv/bin/io24-mixer
```

You can also run the app directly from the checkout:

```bash
.venv/bin/python io24gtk.py
```

Check the connection from a terminal with:

```bash
.venv/bin/io24 status
```

If you want the app in your desktop menu, run `./install-desktop.sh`. The
launcher uses the checkout's `.venv` when it is available and falls back to
the system Python otherwise.

If `pyusb` reports `No backend available`, the libusb system package is
missing. If the device is connected but `io24 status` cannot find it, recheck
the udev rule and replug the interface.

## ALSA control bridge

The optional native bridge exposes the device's readable, safely writable
controls to stock `alsamixer` and `amixer` without opening USB in the mixer:

```bash
sudo apt install libasound2-dev libjson-c-dev alsa-utils
make
sudo make install PREFIX=/usr
```

Add this once to `~/.asoundrc`, keeping any existing ALSA configuration:

```text
</usr/share/io24/io24.asoundrc>
```

Start `io24d` first, then select the bridge explicitly:

```bash
alsamixer -D io24
amixer -D io24 contents
```

`Main Volume` and `Headphone Volume` use `0..100`; `Monitor Blend` uses
`0..100` with `50` as the device midpoint; input gains use `0..60` dB. The
phantom and per-input processing controls are booleans. `Main Output Mute` is
read-only because the physical front-panel state is readable but has no proven
host write command. Input mutes, processing assignment, HPF, and other controls
without trustworthy readback are intentionally not exposed.

The bridge is a client of `io24d`; it never replaces the default PipeWire ALSA
control. It uses `IO24D_SOCKET` when set, otherwise
`$XDG_RUNTIME_DIR/io24d.sock`, and reports an error if the daemon is unavailable.
Run the hardware-free native test with `make test`.

## What works

The Host covers the parts of Universal Control you are likely to use every
day:

| Area | Linux Host support |
|---|---|
| Inputs | gain, phantom power, mute, fixed 80 Hz low cut, stereo link, and automatic Host-side leveling |
| Mixer | Main, Mix A, and Mix B levels and assigns; source and output mute; bus masters; solo; monitor blend; headphones source; persistent Mirror Main |
| Fat Channel | digital HPF, gate, Standard/Tube/FET compressors, limiter, four-band Standard EQ, Passive Program EQ, and Vintage 1970s EQ |
| Device reverb | shared block-202 reverb, measured working with both inputs feeding it |
| Voice FX | Transformer, Detuner, Vocoder, Ring Modulator, Filters, and Delay with the exact UC XML control order and an independent **On** state for each model. All six are verified on Input 1; models 1–5 are verified on Input 2. At 96 kHz, Delay automatically moves to the safe Host insert instead of selecting the faulty firmware model. |
| Host effects | four-band multiband compressor, the 96 kHz Voice FX Delay fallback, and a separate spring-reverb processor returned to physical Main 1-2 on the active stereo playback pair |
| Presets | Host snapshots, complete Host-known channel presets, UC Device Presets Store/Load flow, and front-panel preset status |
| Scenes | import and export using Universal Control's `.scene` vocabulary, with validation and rollback reporting |
| Device settings | 44.1/48/88.2/96 kHz, buffer size, output delay, preset-button mode, Channel Mute Sync, and component names stored by the Host |
| Metering | both inputs, Main, Mix A, Mix B, and gain reduction |

The GTK app keeps both channel strips visible, includes draggable EQ nodes and
a live response view, and uses the recovered rack artwork and component layout
for Voice FX. The app can open before the interface is connected and attaches
when the device appears.

Only one process can own the USB control interface at a time. Run
`io24-mixer`, `io24d`, or `io24-ucnet-shim`, not several of them together.

## Voice FX, including Input 2

The io24 has one stock Voice FX processor. Universal Control assigns that
processor to Input 1 or Input 2 through `processingChannel`, then sends the
selected model's state. The Host now follows that same order:

1. Choose **Voice FX input** in the Effects tab.
2. Choose a model from the rack or the **Model** row.
3. Use that model's own **On** switch and controls.

The assignment is cached after it succeeds, so moving a slider does not keep
swapping the device's processing route. On reconnect, the Host reads the live
assignment from the io24 instead of replacing it with a stale cached value.

All six models have been waveform-verified through physical Input 1. On Input
2, Detuner, Vocoder, Ring Modulator, Filters, and Delay have each passed two
physical waveform cycles after the assignment repair. Transformer/Doubler is
the remaining exception: it stayed dry under three independently ordered
transactions even though the same Input-2 rig detected the other models and a
shared-reverb control. That is a model-0 issue, not a general Channel-2 failure.

Each model component keeps its own stored **On** value. This is how the UC
component XML is structured; it is not a single master switch painted six
different ways. The device still holds one selected model at a time, so the six
buttons do not represent six simultaneous processors.

Voice FX is separate from the shared Reverb section. Voice FX edits do not
open or change the block-202 reverb return.

The io24's hardware Delay is never selected at 96 kHz. Selecting firmware
model 5 at that rate caused the unit to reset into its bootloader. The desktop
Host now keeps the same On, Time, Feedback, and WetDry controls working at
96 kHz through a bounded PipeWire insert on the selected input. Before an
upward clock change it requests model 0 at the old rate, waits beyond the
firmware's 40 ms bypass transition, replays the selector so the firmware stores
the Transformer delegate, sends Transformer Off, and waits two complete
old-rate audio quanta before it changes the rate. The USB reply is not treated
as an audio-frame fence. Preset loads, scene loads, and reconnect replay adopt
Delay on this same Host path. If the interface is absent, a saved or newly
selected 96 kHz clock is staged at 48 kHz and completed only after attach and
the same preflight.
Moving back to a lower rate keeps the Host insert active until the lower
hardware clock is actually observed.

An online "48 kHz first" workaround is not enabled automatically. Static
firmware tracing shows that the later 96 kHz setup still reconfigures Delay and
grows its two rate-dependent buffers. That sequence may change allocation
history. The firmware also consumes a failed resize as a zero-length buffer in
unchecked divisor/index math, making allocation failure a credible fault path,
but it does not prove that the 96 kHz request actually fails. The complete
vendor package further shows that Delay requests no shared workspace and uses
the default runtime allocator for both histories. Its live arena and the exact
reset cause remain unresolved. The Host fallback avoids that code path rather
than relying on an unproved initialization trick.

## Presets and scenes

There are three different save paths, and the Host keeps them separate.

### Host snapshots

**Save snapshot** stores the write-only state that the Host last sent, plus
Host-only features such as Multiband and Spring. Loading a snapshot restores
that state and brings the controls back into line with it.

The device cannot read its DSP blocks back, so `~/.cache/io24/shadow.json` is a
record of the last successful Host writes, not proof of current hardware state.
If Universal Control changed the device elsewhere, clear the shadow before
relying on it:

```bash
io24 shadow clear
```

Older `host_features.pan` data is ignored and is never saved again. The io24
does not expose a true mono-input pan command.

### Universal Control scenes

The Presets tab can load or save a `.scene` file. The command-line equivalents
are:

```bash
io24-scene --dry-run "My Scene.scene"
io24-scene --sample-rate 48000 "My Scene.scene"
io24-scene --export io24-host.scene
```

Scene load validates the full plan before its first write. If a later write
fails, it stops and attempts to restore the exact readable and Host-known state
captured before the load. The report says whether that rollback was complete or
partial.

The command-line loader requires the io24's current sample rate instead of
assuming one. The desktop Host supplies it automatically and restores a
96 kHz Delay scene through its Host insert. The standalone `io24-scene` loader
has no audio insert of its own, so its direct hardware apply still rejects
Delay at 96 kHz.

At 96 kHz the last-session file stores the exact Delay controls and owning
input as a Host-only feature, because writing those edits into the device
shadow would select the unsafe model. Scene export lets that Host state replace
any stale device-shadow copy. A 96 kHz restore validates the Delay component
but emits no hardware model-5 transaction.

Some UC fields have no proven io24 command: mono-source pan, stereo width,
independent `FXA` sends, `dawpostdsp`, and output mono fold-down. The importer
reports those fields instead of quietly mapping them to a different control.

### Device Presets and the front-panel buttons

Universal Control uses `MemP/PrsM` for its twelve Device Presets destinations:
six for Input 1 and six for Input 2. **Send to Device Presets** follows that
route and keeps an identity-bound receipt in
`$XDG_STATE_HOME/io24/device-presets.json`.

The interface cannot return the stored body. A successful send is therefore
reported as `WRITE_SENT_UNVERIFIED`. It is not claimed as cold-boot persistence
or standalone Voice FX proof. **Load** replays the retained record through the
normal Fat Channel and Voice FX setters, which matches UC's RestorePreset
behavior.

The four front-panel button records use a different `MemP/Stat` path. The Host
can follow their selection, but it does not pretend that a Device Presets entry
has been assigned to one of those buttons.

Fat Channel state has been confirmed audible without the Host after device
recall. Voice FX still needs the Host transaction, so keep the Host open when
that effect is part of the sound.

## Mix A, Mix B, and recording

Mix A and Mix B are the computer-facing loopback buses. Universal Control calls
them `aux1` and `aux2`; either name works in the CLI.

- **io24 Host Mix A** carries USB capture 3–4.
- **io24 Host Mix B** carries USB capture 5–6.

These PipeWire sources route nowhere by default. Your recorder, DAW, or stream
software must select and route them. Main is not exposed by USB capture because
the io24 does not provide a complete Main capture pair.

Examples:

```bash
io24 send line/ch1 aux1 -6
io24 bus aux1
io24 assign line/ch1 aux1 on
io24 busmaster aux1 -3
```

The `assign` command removes a source from a bus without losing its fader
position. `mirror <bus>` copies the Main mix into an aux bus and keeps following
Main until the latch is released.

## Useful commands

```bash
io24 gain 1 35
io24 meters 10
io24 eq 1 lowmid peaking 800 -6 1.4
io24 savepreset vocal.json
io24 delay 40 mixa
io24 delay off
```

Run `io24 --help` for the complete command list.

The device supports 44.1, 48, 88.2, and 96 kHz. PipeWire often ships with only
48 kHz enabled. The [user guide](GUIDE.md) explains how to allow all four rates
and how the Host remembers the last successful rate and buffer size.

## Keeping settings after reconnect

The GTK Host remembers its last session and reapplies write-only DSP and mixer
state whenever the io24 reconnects. Readable hardware state, including gain,
phantom power, mute, stereo link, volumes, the selected preset block, and the
live Voice FX assignment, comes from the interface.

For a headless setup, save a startup preset:

```bash
io24 startup save
cp systemd/io24-startup.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable io24-startup
```

This is still Host-side persistence. Most DSP settings survive closing the
program but reset after a cold power cycle unless the device itself recalls
them.

## What is not fully verified yet

- UC Device Presets Store reaches the exact recovered route, but the io24 has
  no stored-body readback. Cold-boot persistence remains unproved.
- Voice FX is confirmed on Input 1. Input 2 is confirmed for models 1–5;
  Transformer/Doubler remains the isolated model-0 exception.
- Passive and Vintage EQ use exact UC 4.7.2 designers and packet routes. Their
  dedicated audible A/B check is still pending.
- The Host spring reverb is covered by offline DSP and routing tests. Its final
  physical Main-output acceptance run is still pending.
- The compressor knee interpretation remains inferred.

These limits are tracked in [PROTOCOL.md](PROTOCOL.md). That file preserves old
experiments as historical evidence, so the newest dated result controls when an
older section disagrees.

## Project layout

| File | Purpose |
|---|---|
| `io24.py` | USB transport, native parameters, DSP calls, snapshots, and CLI |
| `io24gtk.py` | GTK4/libadwaita Host |
| `io24_fx.py` | exact Voice FX schemas and packet builders |
| `io24_dsp.py` | gate, compressor, limiter, and filter coefficient builders |
| `io24_mixer.py` | mixer taper, balance law, and mixer packets |
| `io24_mbc.py` | Host multiband insert |
| `io24_spring.py` | Host spring processor and PipeWire route control |
| `io24_scene.py` | Universal Control scene import/export |
| `io24d.py` | local JSON-lines daemon for custom clients |
| `io24_alsa_ctl.c` | native ALSA external-control bridge for `alsamixer` |
| `Makefile` | native bridge build, install, and hardware-free test target |
| `alsa/io24.asoundrc` | ALSA `ctl.io24` configuration |
| `ucnet_shim.py` | UCNET compatibility layer |
| `PROTOCOL.md` | reverse-engineering record and evidence boundaries |
| `GUIDE.md` | detailed user guide |
| `PUBLICATION.md` | public-source and release boundary |

## Thanks and prior work

[Oddbear's Revelator.io24.Api](https://github.com/oddbear/Revelator.io24.Api)
made this project possible. Oddbjørn Bakke documented the UCNET side of the
Revelator io24 and built practical integrations for tools such as Stream Deck,
Touch Portal, and Loupedeck. That work supplied the original route and client
map and gave this project a solid place to begin.

The io24 does not speak UCNET directly over USB, so this repository continues
below that layer. Its native USB protocol, DSP messages, and Linux Host behavior
were recovered from the shipped Universal Control binaries, the device
firmware, public manuals, and controlled tests. Oddbear's work and this lower
level implementation solve different parts of the same problem, and the Linux
Host would not have reached this point without that foundation.

This project is independent and is not affiliated with or endorsed by
PreSonus.

## Development

Want to work on it? Install the test dependencies and run the hardware-free
suite:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
make test
```

Hardware, USB, audio-routing, firmware, and preset persistence checks are never
ordinary CI steps. They require a separately authorized local campaign with a
defined signal path and restoration plan. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [PUBLICATION.md](PUBLICATION.md).

## Safety

The driver never sends the firmware-reset command. Do not flash the interface
from this repository.

During earlier stress tests, sustained audio I/O combined with rapid control
writes pushed the unit into its bootloader twice. It recovered after a full
cold power cycle and the flash was not damaged, but this is still worth taking
seriously. If the interface identifies itself as `Revelator IO 24 BOOTLOADER`,
unplug it at the device end, wait about 15 seconds, and reconnect it.

Selecting Voice FX Delay at 96 kHz also produced an immediate bootloader
disconnect on firmware 0128. The Host never sends that model selection at
96 kHz. It preflights upward clock changes and runs Delay on the computer with
the same four controls instead. Native 48 kHz timing and audio were physically
verified; the new 96 kHz Host path is covered by deterministic DSP and routing
tests and still needs a separately authorized live listening pass.

The DFU interface reports `Upload Unsupported`, so the device cannot provide a
recovery image before a write. No custom firmware image is included here.

## License and source boundary

The original Linux driver, Host, and documentation are licensed under
GPL-3.0-or-later. See [LICENSE](LICENSE).

Vendor installers, DLLs, firmware, extracted factory preset bodies, decompiler
output, local captures, personal paths, device serials, VMs, and agent work
records are not part of the public repository or Python wheel. Optional factory
data and exact alternate-EQ coefficients must be recovered from your own lawful
Universal Control copy. The Host remains useful without that optional catalog.

[PUBLICATION.md](PUBLICATION.md) records the complete clean-source boundary and
the release checks used before publishing.
