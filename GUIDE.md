# io24 User Guide

This is the practical guide to the Linux Host. The app keeps its labels short,
so the longer explanations live here where they are easier to scan and search.

[README.md](README.md) covers installation and the command line.
[PROTOCOL.md](PROTOCOL.md) is the reverse-engineering record.

---

## Mixer

Level and metering per input, plus bus meters and master strips.

The three aligned bus meters identify what the Host can publish. **Mix A** is
USB capture 3–4 and appears as the passive PipeWire source **io24 Host Mix A**.
**Mix B** is USB capture 5–6 and appears as **io24 Host Mix B**. The sources
route nowhere by default; another application must select one explicitly.
They are computer-side, disappear when the Host closes, and do not change the
device's Main or headphone routing. **Main is not exposed by USB capture**
because the device provides no complete Main capture pair. There are no bus-pan
controls. Mix A/B remain fixed-unity stereo sources with no channel crossing.
Older `host_features.pan` snapshot data is ignored and is never re-saved.

The command-line `pan` operation remains a different, older device-send
**stereo-pair balance**. The device has only one level per source/bus, so that
API cannot place an independent mono input in the stereo field.

Against UC 4.7.2, nothing is missing from the ordinary level-routing job:
source faders, Main/Mix A/Mix B sends and assigns, source mute, per-bus solo,
bus mute/master, phones source, blend, stereo link and the FX return all have
Linux controls. The complete send matrix lives on **Routing** instead of UC's
bus-selected Mixer view. UC scenes contain several generic fields whose
ordinary io24 state is fixed: mono inputs are centred, the independent `FXA`
send is off, DAW capture is post-DSP, and output is stereo. Those values load
without a false warning; non-default requests remain explicit omissions.
`stereopan` width/mono collapse and a writable physical Main-mute latch have no
proven command.
Channel/mix names are durable Host metadata. **Mirror Main** is a persistent
Host latch: Main edits continue into that aux, and clearing it restores the
aux's retained mix. The physical Main-mute button is mirrored read-only, just as
UC exposes `hardwareMute` display-only. **Main mute** is the separate writable
Host bus mute. **Phones mute** controls the headphone output and is no longer
mislabelled as a generic output mute.

**Blend** sets playback against direct monitoring. Centre is an even mix.

**Auto** (under each input's fader) is a Linux Host preamp helper. It is not an
active UC 4.7.2 control: that skin comments its Auto button out, and the two
firmware parameters behind the old name are inert. The Host instead measures
about a three-second window and keeps its 95th-percentile loud level near −12
dBFS. An ordinary analog gain move waits for a gap at least 10 dB below that
loud level, so it does not step the preamp in the middle of a word or note. A
recent peak also blocks an upward move that would leave less than 3 dB of
headroom. Corrections happen only outside a 4 dB deadband and are bounded to
+6 dB upward or −9 dB downward; sustained clipping can make one larger −12 dB
safety correction. Each change clears the measurement window, so it cannot
chase the next phrase a second later. It never raises silence or a steady noise
floor. The gain fader is locked while Auto is on. With stereo link on, both
inputs follow the louder one, and an ordinary move waits until both non-silent
inputs are between phrases. This works only while the Host is open, and the
Host remembers which inputs had it on.

Double-click any fader or knob to return it to its default.

---

## Fat Channel

The rack at the top is a navigator. **Click** a unit to open its controls;
**double-click** to switch it in or out. A lit unit is active; the outlined one
is the section you are looking at. Drag the compressor and EQ to swap them;
they are the only two the hardware can reorder, and the rest are drawn with a
lock and refuse the drop rather than pretending.

One module is shown at a time, for one channel. Both racks stay visible so you
can reach either.

**Standard EQ:** **EQ** switches the complete equaliser without erasing its
curve. Each of Low, Low mid, High mid and High has its own independent **band**
switch. Low and High also have the exact UC shelf switch; switching it off uses
parametric mode. The two middle bands are always parametric. There is no hidden
shape menu. All four bands use 36 Hz–18 kHz, ±15 dB and Q 0.1–10 (default 0.6),
matching UC 4.7.2's embedded `Eqxt4` model. Drag a node for frequency/gain and
scroll over it for Q. Global and per-band bypass states survive full Host setups
and reconnects independently.

**Limiter**: threshold plus **release**, 50 ms to 1.5 s. The device has always
taken a release coefficient and the driver has always computed it exactly; the
page simply never offered the control, so every limiter write before this used
the 0.4 s default. UC keeps no release field in its preset record, so release
is live Host state. It rides a full Host setup, not a device slot body.

**Passive and Vintage EQ:** choose the model in **EQ model**. Each model has its
own editable UC controls and its own global **EQ** power state. Passive exposes
Low Boost/Atten and their 20/30/60/100 Hz switch, High Boost and bandwidth with
3/4/5/8/10/12/16 kHz selection, and High Atten with 5/10/20 kHz selection.
Vintage exposes Low, Low-Mid, Hi-Mid and High gain; its first three bands have
the exact 35/60/110/220 Hz, 360/700/1600 Hz and 3.2/4.8/7.2 kHz switches. These
models have no per-band power switches in UC's component model.

Both models use their own interactive rack face rather than a generic slider
list. Drag a knob vertically, scroll over it, or focus the panel and use left
and right to choose a knob and up and down to change it. The inset response
display is calculated from the same coefficients sent by the Host. The rack
lamp follows the selected EQ model's independent power switch. Original design
references are kept in [`docs/design`](docs/design/README.md).

The response graph and device packets are generated by UC 4.7.2's exact
designers, read from the separately retained, hash-pinned
`dspusbdevice.dll` as data. The Host does not load that DLL or substitute
generic shelves. If the local artifact is absent, enabling or editing one of
these models stops before a device write and explains what is missing.
Place the DLL from your local UC 4.7.2 installation at
`~/.cache/io24/re/dspusbdevice.dll`, or point
`IO24_UC472_DSPUSBDEVICE` at it. Full Host setups, reconnect replay, and user
presets retain the selected model and all of its semantic controls independently
for each input.

**High-pass filters**: the Mixer strip's **HPF** is the unit's
fixed preamp switch. The Fat Channel's digital HPF is separate: choose Off,
40 Hz, 80 Hz, or 160 Hz, or select Advanced and set an exact 24 Hz–1 kHz
cutoff. The rack shows the Host's last intended digital value. That DSP value
is write-only, so the Host cannot claim it is device readback.

**Gate**: the key filter tunes what the gate listens to; key listen monitors that
side-chain so you can hear what is triggering it.

**Compressor**: the shaded area between the unity line and the transfer curve is
the gain change: red where the signal is being reduced, green where makeup is
adding. The dot is the live working point, with its input and output read off
the axes.

Three models, and they do not share a parameter set:

| model | what the controls mean |
|---|---|
| Standard | threshold, ratio and makeup, as labelled |
| Tube | an LA-2A topology. Peak Reduction drives it; Limit mode is a steeper curve than Compress |
| FET | an 1176. There is **no threshold**; Input drives a fixed one and auto-makeup compensates |

> The FET model can get **very loud**. Its auto-makeup is a steep function of
> drive: roughly unity at the default, but up to **+53 dB** with the threshold
> slider at minimum. That is faithful to the model, and Universal Control
> behaves the same way. Bring monitors down before exploring it.

**Multiband** is the fourth compressor **Model**, next to Standard, Tube and
FET. The unit's firmware has no multiband, crossover or FFT vocabulary at all,
so this one runs on the computer, and choosing it switches the unit's own
compressor off: Multiband replaces it. It cannot sit inside the unit's chain,
so it comes straight after it. The io24 sends each input to the computer after
the whole Fat Channel, limiter included (its manual: it records "just as you
hear it in your headphones, complete with the Fat Channel preset"), and the
Host takes it there.

With **Multiband** selected and the **Compressor** switched on, the input
passes a phase-compensated 4th-order Linkwitz-Riley four-band split. Each band
has its own **Character** selector and the real UC control surface for that
model: Standard has Threshold, Ratio, Attack, Release, Gain, Soft knee and Auto
mode; Tube has Peak Reduction, Gain and Limit; FET has Input, Output, Attack,
Release and the five ratio-button choices. Key filter and Key listen are common
to all three. The same UC-derived builders used by the device compressor turn
those public controls into the common sidechain/timing/curve tuple consumed by
the Host processor, and the four phase-aligned bands are summed again. Changing
character does not discard the other two models' settings. Then:

- **Recording.** A new input device, **io24 Input 1+2 Multiband**, carries
  Input 1 on the left and Input 2 on the right, each processed when its channel
  has Multiband. It is the default input while Multiband is on; in a recording
  app, pick it instead of the io24's own input.
- **Listening and the stream mixes.** The processed input plays back into the
  io24 on **USB playback 1-2**. The Host takes the input's own feed out of
  Main, Mix A and Mix B wherever it was, and turns USB playback 1-2 on there
  if it was off. The input's fader keeps its position and comes back when
  Multiband is off. USB playback 1-2 also carries the computer's own audio, so
  that one fader sets both. The Host now confirms this playback return is
  audible before rerouting the input. If the io24 PipeWire sink was muted, it
  is opened only while the insert runs and its prior mute is restored after
  the direct input feed is back. Its volume is never changed. A zero-volume or
  unresponsive return is rejected with the direct feed left in place.
- **Buffer.** Monitoring now goes to the computer and back, so while Multiband
  runs the Host holds PipeWire's buffer at 128 frames (about 2.7 ms a period
  at 48 kHz) unless it is already smaller, and puts back what it was
  afterwards. Changing **Buffer** yourself keeps your choice.

Each input has its own Multiband settings; linked channels share them. The
status row says **On** only once the processing and its return are both
running. If the io24's audio is not in PipeWire yet, it says so and waits with
the input's own feed in place. If the processing cannot start, the Compressor
switch goes off and the Host says why. The compressor is bundled as source and
built once into the user's cache with the system C compiler; it requires
PipeWire but no `swh-plugins`, system-wide plugin install or sudo. Closing the
Host gives every input its own feed back, and the next launch puts Multiband
back where it was on. A device block cannot hold Multiband: saving one while it
is selected stores the unit's compressor off.

An objective physical Main-L -> Input-1 run now verifies the complete return
path and all three character graphs without requiring listening: against a
bracketed crossover-only baseline, Standard changed the 1 kHz tone by
-0.895 dB, Tube by -4.994 dB and FET by +1.482 dB, with only 0.044 dB baseline
drift. FET getting louder is expected here: the exact hardware-free graph
measures +4.350 dB for the same UC-derived fixed-threshold/auto-makeup state.
This establishes processing and model selection; the subjective feel of the
round-trip latency while playing remains a matter for the musician.

---

## Effects

### Reverb

A shared effect on block 202. It needs a channel that is processing, with
**Bypass** off and a **DSP amount** above zero on the Device page, plus the **FX
return up in a bus**; set both, or nothing is audible. The channel control is
not an isolated reverb send: bypass skips that channel's EQ, compression,
limiting and effects, while 100% is fully processed. Both channels can feed the
shared engine, but their exact amounts are write-only; device readback reports
only bypass versus nonzero.

Turning the shared reverb switch on re-sends Channel 1's processing as set on
the Device page. A bypassed channel stays bypassed. It also sends the Main-return level
displayed beside the switch, and assigns that return to Main. Moving
the reverb controls updates only the engine. Turning the switch off
disables only the shared engine, preserving the displayed path for the next use.

The device has one reverb algorithm, so the Host presents its real controls
directly: **Room size**, **Pre-delay**, **Input high-pass**, and **Reverb return
blend**. The retired Character menu only moved those same controls to named
positions and did not produce convincingly different reverb types.

**Reverb return blend** is the engine's own dry/wet value and starts at 100 %.
The reverb is a send effect: the FX return carries what comes back from the
engine, so any dry share doubles the input that is already in the bus. The UC
scenes recovered from this unit all keep it fully wet. Set how much reverb you
hear with the FX return level, not with this control.

**Size movement** is genuine host-side augmentation: the Host drifts Room size
on a slow sine (about a 14-second period). **Movement depth** shows the exact
range as ± percent around the displayed Room size. Switching movement off
restores that centre value. A manual Room-size change becomes the new centre.
Movement pauses while reverb is off, updates at four bounded writes per second,
and does not replace the saved centre with a passing modulation value.

**Pre-delay** is the gap between the dry sound and the first reflection. In a
real room, the time sound takes to reach a surface and come back.

### Voice FX

Block 201 is one shared Voice FX processor. Universal Control assigns it to one
physical input through `processingChannel`, and the Linux Host exposes the same
choice as **Voice FX input**. Changing that row can exchange the device's
processing-chain permutation and exposed slot indicators, so the Host sends the
assignment only when the target changes. On reconnect it follows the live
assignment reported by the io24.

The six-unit rack is the model navigator. Click a unit to select its XML
component, or double-click it to toggle that component's own **On** field. The
separate **Model** row provides the same selector for keyboard and screen-reader
use. Each model page has its own **On** switch because `On` belongs to that
component in Universal Control's XML. There is no master enable above the six
models. Universal Control still normalizes this as one active rack: turning a
model on turns the other five off while leaving all of their knob settings
ready for later. The device runs one selected model at a time.

The private core is not the shared Reverb section above and does not use block
202. UC's captured VoiceFX transaction also does not open or change the reverb
return; that routing belongs to the Reverb section alone. The factory preset
named **Reverb** is actually Doubler model 0 with the private-core settings
stored in its `voicefx` JSON object.

UC remembers the selected algorithm as `voicefx.__classid`. On load the Host
resolves that class, selects and fully materializes the model, and only then
applies later control edits directly to that model. It does not resend the
selector for On/Off or an ordinary knob move. A saved channel preset carries
the shared model state, and loading it assigns Voice FX to that preset's target
input before sending the model. A fresh component uses the XML defaults;
loading a preset replaces them with that preset's values. Detuner therefore
starts at XML index 4 (minus 4 semitones) and 50 percent WetDry.

#### What the Host sends

The selected model's **On** switch is the device command: it is the first word
of that model's state blob. The complete UC 4.7.2 USB capture contains no
separate master enable or hidden activation tag. Selecting Delay sends
`VoFx + vech`; later On/Off sends only `vech`. Selecting Transformer sends
`VoFx + two Bqdf + two MBdf + godv`; later On/Off, Width and WetDry changes send
only `godv`. The Host now follows those rules and adds no artificial delay.

The hardware Delay is never selected above 48 kHz. Selecting it at 96 kHz
caused the unit to disconnect and reappear as its bootloader on 2026-09-21;
88.2 kHz is conservatively blocked because it has no physical Delay acceptance.
In the desktop Host, the Delay rack keeps working:
On, Time, Feedback and WetDry drive a sample-rate-safe PipeWire processor on
the selected input. An upward rate change performs the firmware's deferred
replacement at the old rate: request Transformer, wait 60 ms, replay that
selector, send Transformer Off, wait two complete old-rate audio quanta, and
only then let PipeWire move the clock. That final wait scales with the active
buffer and avoids treating a transport reply as an audio-frame fence. Presets,
scenes and reconnect restore use the same Host path. The standalone
`io24-scene` command has no such audio insert, so a direct
high-rate hardware load remains blocked. Native repeat timing was
physically verified at 48 kHz; the Host 88.2/96 kHz path has deterministic DSP
and routing coverage pending a separately authorized listening pass.

That Host path uses the same monitoring insertion as Multiband. It removes the
selected input's direct Main/Mix A/Mix B feed, returns the processed signal on
USB playback 1-2, and temporarily holds PipeWire at 128 frames. Monitoring now
makes a computer round trip, and the return shares the USB playback 1-2 fader
with desktop audio. Turning Delay off restores the direct feed.

When the io24 is disconnected, selecting or restoring 88.2/96 kHz holds
PipeWire at 48 kHz. The Host completes the requested change only after the interface
attaches, the live old rate and ALSA period are known, and the same safety
preflight succeeds. The requested high-rate preference remains selected during
that staging.

The earlier Linux listening campaign used the superseded transaction: it
reselected on every edit, omitted both Transformer `MBdf` tables, inserted
20 ms gaps and opened the reverb return. Its dry result is real for that older
Host but does not test the corrected one. The corrected protocol is implemented
and hardware-verified for all six models on physical Input 1, with two
independent on/off cycles per model. Delay produced the programmed 250, 500 and
750 ms repeats; Detuner produced the exact -8-semitone targets; Ring Modulator
produced the programmed carrier sidebands; and Transformer, Vocoder and Filters
each produced repeatable spectral changes. The same-path reverb control and all
VoiceFX-off baselines passed. The earlier Input-2 Delay run left Voice FX
assigned to Input 1, so its null result did not test the missing Channel 2 Host
route. That assignment path is now implemented and covered by hardware-free
tests. A fresh physical Input-2 waveform run is still pending. Reverb is a
different engine and is independently measured working.

Saving a preset does not substitute for that Host transaction. The record can
contain the VoiceFX fields while standalone recall remains dry; only Fat
Channel processing is established as audible without a host.

Custom firmware is not part of this release, and no image is included or ready
to write. The historical offline design notes remain in
[PROTOCOL.md](PROTOCOL.md) for research use; they are not user instructions.

| model | controls in exact XML order (every row also has On) |
|---|---|
| Transformer | Lows, Width, WetDry |
| Detuner | Detune, WetDry. Nine steps, −8 to 0 semitones: it only pitches **down** |
| Vocoder | Volume, Carrier Type (Noise / Sawtooth / Rect), Carrier Frequency, Voiced (read-only), WetDry |
| Ring Modulator | Frequency, Sub Carrier, Sub Carrier Frequency, Distortion, Volume, WetDry |
| Filters | Pitch, Regeneration, Damping, Distortion, Volume, WetDry |
| Delay | Time, Feedback, WetDry |

UC declares Vocoder's **Voiced** field read-only. Block 201 has no readable
state that could populate it, so the Host preserves it in the recovered schema
but does not display a permanently disabled control.

These names, ranges and defaults are not guesses: they are Universal Control's
own component model, recovered from its binary and retained in the private
research workspace. The vendor-derived XML is deliberately not redistributed;
its decoded contract agrees with this project's implementation on every
parameter.

The implementation follows the same split: [`io24_fx.py`](io24_fx.py) contains
the six exact model packet builders, while `FXVisual` and the per-model control
pages live in [`io24gtk.py`](io24gtk.py). The visual for the selected model is
driven by that model's decoded parameters and visibly marks that model **OFF**
when its own On switch is off. The six-unit rack reuses those signatures as
compact live previews; the XML parameter ID and its exact builder keyword are
the one route behind each editable row.

> **A naming discrepancy worth knowing about.** Universal Control's binary (and
> its UI) calls model 0 **Transformer**; both the io24 and io44 owner's manuals
> call the same processor **Doubler**. The io24 spec sheet lists "Doubler,
> Vocoder, Ring Modulator, Comb Filter, Detuner, Delay, Reverb". PreSonus never
> reconciled the two. This app shows **Doubler / Transformer** so either vendor
> name is recognizable. That same spec sheet's **"Comb Filter"** independently confirms what
> the decode found for Filters: a tuned feedback comb.

---

## Routing

Every source into every bus, with an assign toggle per source. `assign` takes a
source out of a bus while keeping its fader position, so toggling it twice lands
on exactly the level you left. A route for which the Host has never sent a
level is shown parked, not falsely active at 0 dB; unmuting it materializes the
displayed 0 dB send.

**S** solos a source within one bus: the bus's other sources go silent and come
back at exactly their previous levels when the solo is released. Several
sources can be soloed in a bus at once, and each bus has its own solo. The
hardware has no solo, so the Host writes the others off without touching their
faders or assigns. Solo is never saved in a full Host setup, and the Host releases any
active solo when it closes. A route the Host had never set is given the 0 dB it
displays the first time its bus is soloed, so releasing has a real level to
return to.

The mute button beside each **Return** and **FX Return** source silences that
source in Main, Mix A, and Mix B while retaining its three faders and assigns.
The mute button beside each **Bus master** silences that whole output while
retaining every source level and assign in it. These are UC scene semantics
implemented by the Host over the device's individual sends; releasing either
mute restores the exact retained mix. Physical input mute remains on the Mixer
page.

**Bus master** offsets every send in a bus at once. The hardware has no such
control. Block 100 holds one level per source and nothing above them, so it is
applied by rewriting each send. Measured exact to 0.00 dB, with no crosstalk.

**Mirror Main** latches Mix A or Mix B to Main levels, assignments, and
stereo-pair balance. Its own mix remains stored underneath. Main changes keep
flowing to the latched aux; switch the latch off and its exact retained mix
returns. This is durable Linux Host state, not a claim that firmware maintains
the latch after the Host closes.

Mix A is loopback to USB capture 3–4, Mix B to USB capture 5–6. The GTK Host
publishes them as **io24 Host Mix A** and **io24 Host Mix B**; these passive
sources route nowhere by default. Main is not exposed by USB capture. None of
these facts makes either bus audible at Main or headphones without an explicit
route in another host application.

Both need the card profile to expose those capture channels. The io24's six
channels are only all present in the **Pro Audio** profile (and in the
six-channel surround input profiles); the Analog Surround 2.1 profile carries
three, so Mix A and Mix B are not there at all. The Host reads the profile
rather than assuming, and when a bus is missing it says so instead of
publishing a source fed by channels that do not exist. Change the profile in
your desktop's sound settings, or with `wpctl`/`pactl`, and the sources appear
on the next reconcile. The Multiband compressor type is unaffected: it needs
only Inputs 1 and 2, which every profile with a stereo pair carries.

> The mixer has **no read-back**. The device accepts a level and will not report
> one, so these sliders show what this app last sent, not what the hardware
> holds. The Host deliberately replays that durable shadow on reconnect so its
> last mix, source/output mutes, Fat Channel, Voice FX and reverb return rather
> than disappearing after a USB or power cycle. Device-readable controls are
> adopted from the unit instead, and device block selection is quarantined from
> ordinary replay. Use `io24.py shadow clear` when cached bookkeeping is
> unwanted; clearing it does not alter the device.

---

## Device

**Sample rate:** the device supports 44.1, 48, 88.2 and 96 kHz; PipeWire
decides which is used. The Host starts at the safe native 48 kHz rate and a
512-frame buffer on its first launch, then remembers and restores the last
successful selections. The rate selector changes PipeWire's graph-wide clock,
so another audio device can follow it. Two things to know if you check by hand:
PipeWire's
`clock.rate` is the *default* and stays at 48000 whatever the graph is doing.
The real rate is in `/proc/asound/card*/pcm0p/sub0/hw_params`, and a forced rate
only takes effect once something is playing, because a suspended device has no
graph to re-rate.

Every coefficient this Host computes is a function of that rate: EQ and HPF
biquads, gate/compressor/limiter time constants, the reverb, and the
rate-dependent Voice FX filters. The device stores the coefficients, not the
Hz and seconds they came from, so when the clock moves the Host recomputes and
re-sends them and says so. Before this, nothing passed a rate at all and every
write was built for 48 kHz. At 96 kHz a 1 kHz EQ band was landing at 2 kHz and
a 240 ms delay was running at 120 ms.

Voice FX Delay is the exception to normal device-side high-rate support. A
model selection at 96 kHz reset this firmware into its bootloader. The same
private histories are already substantially larger at 88.2 kHz, where no
physical Delay acceptance exists, so the desktop Host leaves that model
bypassed in the unit above 48 kHz and runs Delay on the computer.
The switch is automatic; the visible controls and selected input do not
change. Moving back down keeps the safe Host path until ALSA reports the lower
hardware clock.

The preflight covers changes requested through this Host. If another program
changes PipeWire or ALSA directly, it can bypass that ordering. Turn Delay off
before an external clock change and wait for the Host to show the new observed
rate before enabling it again.

**Buffer** is PipeWire's quantum. Latency is quantum ÷ rate, so smaller is
tighter but works the CPU harder and risks dropouts.

**Output delay** holds one output back so it lines up with a delayed remote
signal, such as a co-host on a call or a stream's video path. 0–500 ms in 2 ms steps.
It is one global delay plus a bus selector, not an independent delay per bus.

**Channel Mute Sync** matches UC's mute-behavior setting: when enabled, a
channel Mute follows Main, Mix A, and Mix B. It is write-only, so the switch and
scene loader show what this Host last sent, not device readback. UC names the
enabled value and the owner's manual defines the behavior; the `1 = enabled`
value remains a protocol inference because the io24 exposes no readable slot
for it.

**Channel processing** gives each input Universal Control's **DSP amount** and
**Bypass**. Behind both is the one processing value the device keeps per
channel: Bypass sends zero, which skips that channel's EQ, dynamics and
effects; otherwise the amount is sent. The amount is remembered and greyed out
while bypassed, and it cannot reach zero by itself, so a bypassed channel always
shows as one. The device reports only bypassed versus processing, so the exact
amount is what this Host last sent. The switch also follows the unit's
press-and-hold once the device has reported the new state for half a second (a
short Preset press only blips it, and is ignored); a channel the unit turns
back on shows 100%. A slot recall on a bypassed channel likewise turns
processing back on at 100%, and the controls follow. UC's own
`dspAmount`/`bypassDSP` routes have no recovered device binding; pairing them
onto this one value is the Host's reading of UC's schema, whose amount minimum
sits just above zero.

Use intermediate DSP Amount values only when a parallel whole-chain sound is
actually wanted. A live no-listening meter test with a controlled EQ showed a
linear dry-to-processed blend at 0/25/50/75/100%; it is not a channel level or
send. That makes the control useful for effects such as parallel compression,
but a poor control for corrective processing:

- With EQ or a high-pass filter, the dry path restores frequencies the filter
  was meant to remove and can change the intended phase response.
- With a gate, the dry path leaks the signal around the gate.
- With a limiter, dry peaks bypass the ceiling, so the output is no longer
  reliably limited.

For those modules, leave DSP Amount at **100%** while processing is enabled and
use the module's own parameters or bypass. Treat **Bypass** as the normal
whole-chain on/off control. Voice FX **Wet/Dry** and reverb **Wet mix** remain
separate effect-specific controls; DSP Amount does not replace them. The live
result establishes the functional blend law, not whether the firmware
literally implements two parallel signal paths.

**Host component names** lets you label Inputs 1/2, all three USB playback
pairs, FX return, Mix A/B, and Main. UC keeps these `username` values in its
component model rather than sending a label-write command to the io24, so Linux
stores them with full Host setups and scenes. The separate **Device-reported
channel names (read-only)** group shows the unit's small `CHNP` table.

The Monitoring strip's **Interface Mute button** status follows the front-panel
MUTE latch. UC also exposes `hardwareMute` only as display state; neither Host
has a writable command for that physical latch. **Main mute** is the
independently writable Host Main-bus mute. **Phones mute** is the separate
headphone-output control.

---

## Presets

The page has a **Preset input** row, a search box, a **Preset name** field with
**Save**, and two drop-downs: **User Presets** and **Factory Presets**. The
search filters both by name and description.

A **channel preset** means one input sound: Fat Channel plus selected Voice FX.
A **UC scene** means the portable whole device and mixer. A **full Host setup**
also contains Linux-only processing such as Multiband and the safe high-rate
Delay fallback. **Automatic recovery** is the unnamed last session maintained
by the Host. Those scopes are shown separately so saving a vocal sound cannot
be mistaken for saving the whole mixer.

### Loading

Click the preset's **Load** button to load it into the channel chosen in
**Preset input**. Its Fat Channel goes to that channel; while the channels are
linked, the Fat Channel goes to both. Voice FX has one shared state, so the same
action assigns it to the target input and sends the preset's recorded model state.

A Host-known **Device Presets** record is replayed through the ordinary Fat
Channel and Voice FX setters, matching UC's RestorePreset behavior. It does not
select a front-panel block. Historical front-panel `Stat` receipts are a
separate legacy list: on their own channel the Host selects that block and then
replays its retained record because selection alone did not reapply the
unreadable body in the retained live test. A block the Host did not write can
only be selected, and the UI says that its body is unknown instead of pretending
to load it. If the target channel is bypassed, Load enables processing; a
channel already processing keeps its exact DSP amount.

A loaded preset is shown on the controls, so the pages stop describing the
previous sound. A recalled Passive/Vintage EQ selects its own exact editable
panel and response curve rather than being misrepresented as four Standard
bands.

### User Presets

First come known Device Presets library records, followed by historical
front-panel-block transport receipts. A receipt is not proof that a body was
installed: the io24 can report a selected front-panel block but cannot return
either kind of stored body, and the objective `Stat` inactive-slot tests did
not apply the test EQ/gate state.

Then come the presets you saved on this computer.

### Saving and Device Presets

Type a name and press **Save**: the **Preset input** channel's current sound,
Voice FX included, joins your presets on this computer, in
`~/.config/io24/user-presets.json` (the factory file's format). Saving over an
existing name asks first. Voice FX is one shared settings object, so either
channel's preset carries the current model state and assigns it to that channel
when loaded.

Each preset's **⋯** menu offers **Save to device…**. Choose Input 1 or Input 2,
then the exact destination. **Preset-button block 1/2** writes one of the four
front-panel `MemP/Stat` bodies. **Device library slot 1–6** uses UC's separate
`MemP/PrsM` Store route: indexes 16–21 belong to Input 1 and 22–27 to Input 2.
The Host refuses to overwrite the front-panel block currently playing. It
validates and sends a complete record, then retains an identity-bound receipt
with status `WRITE_SENT_UNVERIFIED`. The io24 cannot return that body, so the
status is not called device readback, cold-boot persistence, or standalone
VoiceFX proof.

Known Device Presets appear under **User Presets**. **Load** performs UC's
RestorePreset behavior by replaying the retained record through the normal Fat
Channel and VoiceFX setters; it does not move a front-panel selector. Historical
front-panel `Stat` receipts remain visible as legacy Host-assisted records, but
the disproved writer is no longer offered as a normal action. Your own presets
also have **Delete**, which removes them from this computer only.

### Scenes

**Save scene…** writes the readable and Host-known device state atomically with
Universal Control's scene field names. It includes exact semantic Standard,
Passive, and Vintage EQ state from the editor and the selected Voice FX model's
own On value. Above 48 kHz, the exact Host Delay controls and owning input replace
the intentionally stale device-shadow copy, so reloading the scene cannot
materialize hardware model 5. It also includes every complete front-panel-block
and Device Presets body retained for this exact unit. Unknown bodies, physical
slot selection, and any other unknown value are reported and omitted; they are
never guessed. The saved file is proven to round-trip through this Linux Host,
not through the untested Universal Control import path.

**Load scene…** applies a Universal Control `.scene` from the Presets page. The
Host parses and validates the entire file before the first device
write. It covers the global delay and headphone source, Channel Mute Sync,
preset-button mode, both Fat Channels and DSP amounts, Standard/Passive/Vintage
EQ, the complete Main/Mix A/Mix B matrix, source and output mutes, Host solos,
bus masters, persistent Mirror Main, shared Voice FX and shared reverb. The three
retained UC scenes plan 94–97 real Host operations each. Their device-resident
preset libraries are reported but never overwritten.

The io24 has no faithful mapping for mono-source pan, stereo width, exact `FXA`
reverb sends, `dawpostdsp`, or output mono fold-down. The loader reports those
fields explicitly instead of mapping them to a nearby control. Text names and
Mirror Main round-trip as Host-persisted UC state. Because Voice FX is one shared processor, two
conflicting per-channel Voice FX records make validation fail before transport.
Once a valid plan starts, writes are sequential. A USB/runtime failure stops on
that setting, prevents later writes, and triggers compensation from the exact
pre-load readable state, Host write mirror, and solo state. The UI says rollback
is complete only when every attempted control had known prestate and every
restore write succeeded. A partial result names unknown prior values or restore
errors; the device itself has no transaction primitive.

### Factory presets

The originals from Universal Control, decoded from the installer and applied
through this driver. Standard, Passive and Vintage EQ records all load through
their own exact model routes. Any remaining module the io24 does not implement
is skipped rather than silently dropped; a preset that cannot be applied says
why and is not partially presented as successful.

### Front-panel Preset button

**Preset blocks on the unit** (Device page, **Preset button**) sets how many
blocks the unit's Preset button steps through: **One**, **Two** (what the unit
ships with), or **None**. It is write-only; the device never reports it back.

> In **Two**-block mode the Preset button is visible to the app: pressing it
> walks that channel's two blocks and the **· playing** tag follows, and
> press-and-hold bypasses the channel while the Device page's **Bypass**
> follows. In One-block mode a press has nowhere to go, so nothing moves.

The button lands on the pair's first block. On the Mixer, each strip's dot shows
its active block: solid with a breathing brightness (block 1 every 1.8 s, block
2 every 0.9 s), and an outlined dot when bypassed. With reduced motion the dots
stay steady and differ slightly in size.

### Full Host setups

A full Host setup also carries the Host-only **Size movement** switch and
**Movement depth**. The device has no parameter for either one. Saved values
retain the chosen Room-size centre, not a passing modulation sample. Values
that are unreadable, unknown, or out of range are refused with a completed-load
notice rather than quietly moving a control.

The older `savepreset` / `loadpreset` command names keep full Host settings as
JSON for compatibility. A setup
holds the live values the device *can* report, plus this driver's mirror of the
DSP writes it has made.

Two consequences: loading a preset applies what is in it and does not reset
settings the preset never mentioned; and the mirror is a claim about what was
last sent, not a reading. Device preset mode, slot selection, and enable state
are quarantined by default, so loading a full Host setup or reapplying cached
controls cannot move or disable either channel's device slot. The lower-level
API has an explicit opt-in for workflows that intentionally include those
selectors.

GTK full Host setups also save both inputs' Multiband settings under the versioned
`host_features.multiband_insert` object, with reverb movement and which inputs
have Auto gain on. What the Host changed in the unit's mixer for
Multiband, and the buffer it borrowed, belong to the running session and are
left out of setup files. A setup written before 2026-09-11 carries the
retired playback multiband as `host_features.multiband`: its settings are
loaded into both inputs' Multiband, switched off, and a completed-load notice
says so. The passive source lifecycle is reconciled separately. Obsolete `host_features.pan` data is discarded with a completed-load
notice, regardless of its old contents, and is never re-saved. Other invalid
Host-only data is rejected before any device setting is applied. These objects are deliberately absent
from device-slot records, cached-device reapply,
and standalone-device claims. Older setup files without these objects continue
to load and do not change the current Host-only state.

### Keeping settings after reconnect

The Host picks up where it left off, as Universal Control did. Everything it
sends to the unit is kept on disk (`~/.cache/io24/shadow.json`), and its own
features (both inputs' Multiband, reverb movement, and which
inputs have Auto gain on, plus any high-rate Host Delay) are saved to
`~/.config/io24/last-session.json` every few seconds and on exit.

Each time the io24 connects, the Host re-sends the settings the unit cannot
keep through a power cycle (EQ, dynamics, mixer, source/output mutes, reverb,
Voice FX, DSP amount and Bypass) and shows them on the controls. What the unit
reports itself (gains, phantom power, input/headphone mute, stereo link,
volumes, the selected block) comes from the unit, so a change made on the
hardware while the Host was closed stands. The selected block is never moved.
Voice FX model state is restored with its exact transaction and model-local On
state. Its input assignment comes from the live `processingChannel` value on
automatic reconnect, while an explicit preset load assigns the preset's target
input. Host-only features come back once per launch, not on every reconnect.

Two consequences: settings changed from Universal Control on another computer in
between are overwritten on the next connection, and solo starts off each launch,
as it did in Universal Control. Auto Gain is a Linux Host convenience, not an
active UC 4.7.2 control; the Host restores it for the inputs that had it on.

`io24.py startup save` and `systemd/io24-startup.service` remain for applying
settings without the Host window. See
[README.md](README.md#keeping-settings-after-reconnect). For a sound that plays with no
computer at all, the unit's own preset storage is the intended path. Fat
Channel is established standalone; Voice FX is not, so keep the Host available
when that effect is part of the sound.
