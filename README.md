# io24 for Linux

A native Linux control host for the PreSonus Revelator io24.

The io24 already works as a class-compliant audio interface on Linux. What
Linux does not get out of the box are its mixer and DSP controls, which sit
behind a vendor USB protocol. This project opens those controls without
Windows or macOS. You get a GTK4 desktop app, a command-line tool, preset and
scene support, and detailed protocol notes if you want to dig deeper.

The main Linux interface is a native desktop app. There is no browser service
to set up. The independent
[io24 Controller for Android](https://github.com/Ajuntanaga/io24-android) talks
to the interface directly from a phone and does not depend on this Host.

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

## What works

The Host covers the parts of Universal Control you are likely to use every
day:

| Area | Linux Host support |
|---|---|
| Inputs | gain, phantom power, mute, fixed 80 Hz low cut, stereo link, and automatic Host-side leveling |
| Mixer | Main, Mix A, and Mix B levels and assigns; source and output mute; bus masters; solo; monitor blend; headphones source; persistent Mirror Main |
| Fat Channel | digital HPF, gate, Standard/Tube/FET compressors, limiter, four-band Standard EQ, Passive Program EQ, and Vintage 1970s EQ |
| Device reverb | shared block-202 reverb, measured working with both inputs feeding it |
| Voice FX | Transformer, Detuner, Vocoder, Ring Modulator, Filters, and Delay in the exact UC XML control order. Each model retains its settings, while turning one **On** turns the other five off, matching UC's single active rack. All six are verified on Input 1; models 1–5 are verified on Input 2. Above 48 kHz, Delay automatically moves to the safe Host insert instead of selecting the unaccepted high-rate firmware model. |
| Host effects | four-band multiband compressor and the safe high-rate Voice FX Delay fallback |
| Presets | channel presets, full Host setup files, automatic recovery, exact front-panel block writes, and the UC Device Presets library flow |
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

Each model component has its own stored `on` field in UC's XML, but UC presents
one active rack. The Host now follows both facts: every model keeps its own
parameter values, and turning one model **On** clears the other five `on`
fields. All six models can therefore remember their knob positions without
pretending that six processors run at once.

Voice FX is separate from the shared Reverb section. Voice FX edits do not
open or change the block-202 reverb return.

The io24's hardware Delay is never selected above 48 kHz. Selecting firmware
model 5 at 96 kHz caused the unit to reset into its bootloader; 88.2 kHz has
the same expanded private histories and no physical Delay acceptance, so it is
kept on the safe side of the same boundary. The desktop Host keeps the same
On, Time, Feedback, and WetDry controls working at 88.2 and 96 kHz through a
bounded PipeWire insert on the selected input. Before an upward clock change
it requests model 0 at the old rate, waits beyond the
firmware's 40 ms bypass transition, replays the selector so the firmware stores
the Transformer delegate, sends Transformer Off, and waits two complete
old-rate audio quanta before it changes the rate. The USB reply is not treated
as an audio-frame fence. Preset loads, scene loads, and reconnect replay adopt
Delay on this same Host path. If the interface is absent, a saved or newly
selected high-rate clock is staged at 48 kHz and completed only after attach and
the same preflight.
Moving back to a lower rate keeps the Host insert active until the lower
hardware clock is actually observed.

The high-rate insert changes monitoring in the same way as Multiband: the
selected input's direct bus feed is replaced by a computer round trip on USB
playback 1-2, PipeWire is held at a 128-frame buffer while it runs, and the
return shares the USB playback 1-2 fader with desktop audio. This adds computer
latency; turn Delay off to return to direct monitoring.

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

Four names appear in the app because they save different scopes:

- A **channel preset** is one input sound: Fat Channel plus the selected Voice
  FX. It can live on the computer, in a six-per-input Device Presets library
  destination, or in one of the io24's two front-panel blocks per input.
- A **UC scene** is a portable whole-device and mixer setup in Universal
  Control's `.scene` vocabulary.
- A **full Host setup** is the whole state this Linux Host can restore,
  including Linux-only Multiband and the high-rate Delay fallback.
- **Automatic recovery** is the unnamed last session. It is maintained by the
  Host and is not another preset file the user has to manage.

### Full Host setups

**Save full Host setup** stores the write-only state that the Host last sent,
plus Host-only features such as Multiband and the safe Delay fallback. Loading
a setup restores that state and brings the controls back into line with it. Older files and the
internal APIs may still use the word `snapshot`; the front-facing app now uses
the clearer name.

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
assuming one. The desktop Host supplies it automatically and restores an
88.2/96 kHz Delay scene through its Host insert. The standalone `io24-scene`
loader has no audio insert of its own, so its direct hardware apply rejects
Delay above 48 kHz.

At 88.2/96 kHz the last-session file stores the exact Delay controls and owning
input as a Host-only feature, because writing those edits into the device
shadow would select the unsafe model. Scene export lets that Host state replace
any stale device-shadow copy. A high-rate restore validates the Delay
component but emits no hardware model-5 transaction.

UC scenes also contain generic mixer fields the io24 fixes in hardware. A
centred mono `pan`, disabled independent `FXA` send, post-DSP DAW tap, and
stereo output are accepted as already satisfied. A scene asking for any other
value is reported explicitly; the Host never maps it to an unrelated control.
Stereo width still has no proven io24 command.

### Device Presets and the front-panel buttons

Every channel-preset menu now has **Save to device…**. The confirmation first
asks for Input 1 or Input 2, then for an exact destination. **Preset-button
block 1/2** writes `MemP/Stat` index 0–3. **Device library slot 1–6** writes
Universal Control's separate `MemP/PrsM` collection: indexes 16–21 for Input 1
and 22–27 for Input 2. Library sends keep an identity-bound receipt in
`$XDG_STATE_HOME/io24/device-presets.json`; block sends use the corresponding
device-block registry.

The interface cannot return the stored body. A successful send is therefore
reported as `WRITE_SENT_UNVERIFIED`. It is not claimed as cold-boot persistence
or standalone Voice FX proof. **Load** replays the retained record through the
normal Fat Channel and Voice FX setters, which matches UC's RestorePreset
behavior.

The chosen front-panel block must be inactive while it is replaced. The Host
will ask you to choose the other block if that destination is currently
playing. A library entry is not silently assigned to a front-panel button;
these remain two different storage collections.

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
48 kHz enabled. A fresh Host starts at the safe native 48 kHz rate; after the
user chooses another rate it remembers that successful selection. The selector
sets PipeWire's graph-wide clock, so it can affect other audio devices too. The
[user guide](GUIDE.md) explains how the Host selects, observes, and remembers
the last successful rate and buffer size.

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
  dedicated audible A/B check is still pending. Their editable rack panels use
  original project artwork documented in
  [`docs/design`](docs/design/README.md), with every knob and graph driven by
  the exact decoded controls.
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
| `io24_scene.py` | Universal Control scene import/export |
| `io24d.py` | local JSON-lines daemon for custom clients |
| `ucnet_shim.py` | UCNET compatibility layer |
| `PROTOCOL.md` | reverse-engineering record and evidence boundaries |
| `GUIDE.md` | detailed user guide |
| `PUBLICATION.md` | public-source and release boundary |

The Android controller has its own source, build, and release history in
[`Ajuntanaga/io24-android`](https://github.com/Ajuntanaga/io24-android).

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
disconnect on firmware 0128. The Host never sends that model selection above
48 kHz. It preflights upward clock changes and runs Delay on the computer with
the same four controls instead. Native 48 kHz timing and audio were physically
verified; the 88.2/96 kHz Host path is covered by deterministic DSP and routing
tests and still needs a separately authorized live listening pass.

That protection covers sample-rate changes made through the Host. Another
program can ask PipeWire or ALSA to change the clock without going through the
Host's preflight. Turn Voice FX Delay off before making a direct external clock
change, then let the Host observe the new rate before turning it back on.

The DFU interface reports `Upload Unsupported`, so the device cannot provide a
recovery image before a write. No custom firmware image is included here.

## License and source boundary

The original Linux driver, Host, and documentation are licensed under
GPL-3.0-or-later. See [LICENSE](LICENSE).

Vendor installers, DLLs, firmware, extracted factory preset bodies, decompiler
output, local captures, personal paths, device serials, VMs, and agent work
records are not part of the public repository or Python wheel. Optional factory
data and exact alternate-EQ coefficients must be recovered from your own local
Universal Control installation. The Host remains useful without that optional
catalog.

[PUBLICATION.md](PUBLICATION.md) records the complete clean-source boundary and
the release checks used before publishing.
