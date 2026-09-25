# PreSonus Revelator io24 Native USB Control Protocol

Reverse-engineering notes and a working Linux implementation.

## How to read this record

This file preserves the investigation, including negative experiments and
conclusions later disproved. The dated material before §1 is reverse
chronological: the newest dated result controls. Within the older notebook,
labels such as “controlling” describe what was known on that date, not the
current implementation.

The current boundaries are the 2026-09-21 Delay and Spring section below, the
2026-09-20 Voice FX, preset, and mixer sections, the 2026-09-19 scene section,
the 2026-09-18 alternate-EQ section, and the three 2026-09-17 VoiceFX sections.
In short: UC Store is `MemP/PrsM`; all six VoiceFX models process audio on
physical Input 1 with the corrected UC 4.7.2 transaction; the Linux Host now
restores UC's explicit Input 1/Input 2 assignment step; and a sent Device
Presets record remains
`WRITE_SENT_UNVERIFIED`, not body readback or cold-boot proof.

## 2026-09-21 controlling safety result: Delay at 96 kHz and Spring Main return

Selecting Voice FX Delay while the io24 was running at 96 kHz caused an
immediate USB disconnect. The device then enumerated as `194f:0405`,
`Revelator IO 24 BOOTLOADER`, rather than its normal `194f:0422` identity. A
physical reconnect restored the normal device. This was a model selection, not
an explicit reset or firmware command. The exact firmware fault is not inferred
from the enumeration result.

The follow-up trace uses the complete firmware 1.28 vendor package at SHA-256
`790f668608448b7c9f244c360ad72e3c51f71b4e8a3d828eab0e08cd03b35de1`.
Its application prefix has SHA-256
`65ad2b65bcef932b41e748bfab87f62a9e529c17cceaa0a6a7679e387952eb1a`.
It is an ARM Cortex-M image with a Thumb reset vector and VFP instructions, not
a recovered SigmaStudio project. No `SigmaDSP`, `ADAU`, `SHARC`, or
`Blackfin` marker occurs in the package. A separate unobserved processor is
not logically impossible, but the online claim that this image is "almost
certainly" an ADAU17x1 project is unsupported and must not drive a board,
EEPROM, PLL, or reflash operation.

There is a concrete rate-dependent mechanism inside the recovered code. A
`Setu` message configures block 201 and then its selected model. Delay's
vtable `+0x30` routine at raw `0x572d4` stores the new sample rate and resizes
two float delay buffers for approximately 0.35 s and 0.25 s, each plus the
current audio quantum. At a 512-frame quantum the two allocator requests total
119,568 bytes at 48 kHz and 234,768 bytes at 96 kHz, an increase of 115,200
bytes. The `vech` state handler also rechecks capacity after a time change.

This directly disproves the stated explanation for the "48 kHz first"
workaround: a later 96 kHz `Setu` does reconfigure Delay and grow its buffers;
the heavy state is not simply carried across without initialization. Starting
at 48 kHz could still change allocator history, but that narrower hypothesis
has not been tested. The trace proves rate-scaled configuration pressure, not
that a 96 kHz allocation actually fails, an instruction-budget overrun, a
watchdog path, or the exact reset cause.

It does expose a concrete failure path if either resize allocation returns
null. The allocator's null branch stores zero into the buffer's logical length
at `buffer+0x14`. Delay's audio loop later loads that length and reaches `SDIV`
at raw `0x4ec74` without a zero guard. With divide-by-zero trapping enabled
that can raise a UsageFault; without it, the zero quotient feeds index math
that is no longer safely bounded. This makes allocation failure a viable reset
mechanism, not a demonstrated 96 kHz event. The bounded analyzer is
`re/cp34_delay_rate_config_static.py`; the immutable report is under
`runs/20260921T194341-0700-delay-96k-static-correction/`.

The complete package closes one gap left by the truncated application dump.
Delay's vtable `+0x18` workspace-size routine returns zero, so its two histories
are private dynamic allocations rather than part of a shared block workspace.
The resize route reaches the initialized default resource at `0x202b0e54`,
whose vtable forwards allocation through the runtime reallocator at raw
`0x3ce70`. The statically visible `_sbrk` start and limit are both
`0x202b1860`. That does not establish exhaustion: Delay is known to work at
48 kHz, so some live allocator setup or arena fact is still missing. It does
establish that the extra 115,200 bytes are requested from a dynamic path and
that the actual 96 kHz allocation result remains unproved.

The earlier physical Delay acceptance ran at 48 kHz. It does not establish
that the firmware transition is safe at 96 kHz. The Linux Host therefore never
selects hardware model 5 at that rate. The complete package also pins the
replacement path: the first `VoFx` stores the requested model and asks block
201 to enter bypass; after its nominal 40 ms transition, a replayed `VoFx`
calls the work virtual synchronously and stores the new delegate at
`block+0x14` before returning. Before an upward clock change, the Host sends
model 0 at the old rate, waits 60 ms, replays model 0, sends the exact
Transformer-Off state, waits two complete old-rate audio quanta, and only then
changes PipeWire's rate. A USB reply is transport evidence, not claimed as an
audio-frame fence; the quantum-scaled old-rate barrier covers that proof
boundary. The same VocalEcho state, with independent On, Time, Feedback and
WetDry controls, runs in the
existing per-input PipeWire insert. Requested and observed rates are both
considered, so a downward change remains on the Host until the lower hardware
clock is visible. Factory and user presets, Host snapshots, scenes, known
device-slot recall and reconnect replay all adopt the same Host path.

The runtime barrier uses the live ALSA period and rate when available, then
the PipeWire forced/default clock, rather than assuming 512 frames. If the
interface is absent, a requested or saved 96 kHz clock is pinned temporarily
at 48 kHz; session replay waits until attach, the model-0 preflight, and the
guarded move to 96 kHz have completed. This covers Host-owned transitions. It
does not claim to intercept an unrelated program changing PipeWire directly.

At 96 kHz, Delay's exact controls and input owner are persisted as the
Host-only `voicefx_delay` feature rather than written into the device shadow.
Scene export makes that state authoritative over a stale shadowed model, and
the rate-aware scene planner validates it without emitting `set_fx`. The Host
persists semantic intent and chooses its safe execution lane from the current
rate; it does not claim to serialize an unreadable firmware model table.
The standalone preset helpers and scene CLI have no audio insert, so their
direct device path retains the hard interlock and requires an explicit current
rate. Other Voice FX models retain their rate-aware hardware transaction. The
Host fallback, transition ordering and DSP response are hardware-free verified;
no live USB or listening run was made while implementing it.

The active Linux PipeWire profile exposed three playback positions,
`[FL, FR, LFE]`, not six. The first Spring implementation hard-coded USB 5-6
and failed before audio could start. Spring now selects the best stereo pair
the exact io24 playback node exposes. Six-channel profiles use USB 5-6,
four-channel profiles use USB 3-4, and stereo or 2.1 profiles use USB 1-2. The
last case mixes the wet-only stream into the ordinary Main playback sink and
makes no device-mixer writes. Dedicated pairs are still assigned to physical
Main 1-2 only and restored exactly. Output gain now lives in the Spring
processor, so the same control works on either route. These graph, DSP,
migration, and route contracts are hardware-free verified; live Spring
audibility remains a separate acceptance check.

The observed Voice FX tail surviving a laptop reboot establishes powered-device
runtime retention while the io24 itself remains powered. It does not establish
nonvolatile storage or survival across an io24 power cycle. The online
standalone/NVRAM explanation is compatible with the earlier cold-start
negative, but neither a second boot path nor an omitted monolithic USB "DSP
blob" has been source-bound. UC uses tagged block and parameter messages, not
one demonstrated complete configuration blob.

## 2026-09-21 controlling Voice FX result: Input 2 is model-specific

The previous Linux Host conclusion combined two different facts. Firmware
block 201 has one model/settings object with two structural lanes, but Universal
Control still assigns the Voice FX owner to one physical input at a time. UC's
component model names `line/ch1/processingChannel` **Assigned Processing
Channel**, and its exact channel code enables `voicefxopt/fxmodel` only for the
channel object whose `processingChannel == 0`. Its Settings UI exposes that as
**Assign Voice FX: Ch1 / Ch2**.

The earlier physical Input-2 Delay run left the readable permutation at its
normal `(0, 1)` state and never sent the Ch2 assignment. Its null result is
therefore evidence that Input 2 was dry while Voice FX remained assigned to
Input 1. It does not show that the explicit Channel 2 route is ineffective.
Static two-lane topology also does not override UC's runtime ownership switch.

The Linux Host now exposes **Voice FX input** and performs the UC ordering:
assign the requested physical input through `set_voicefx_channel(channel)`,
then send the selected model transaction. It remembers the successful target
per device object so ordinary On/Off and parameter edits do not repeatedly
exchange the processing permutation or slot indicators. On reconnect, it reads
JaSt slots 38/39 and follows the live `processingChannel` permutation; automatic
resume does not replay an old route. Explicit preset loads assign the preset's
target before its Voice FX state.

The subsequent 48 kHz physical Input-2 run resolved most of that gate.
Detuner, Vocoder, Ring Modulator, and Filters produced their exact signatures
twice; Delay produced its 250/500/750 ms taps twice. The shared-reverb control
moved about 25 dB, Input-2 stimulus contrast exceeded 33 dB, and restoration
was exact. Therefore the assignment path and model 1 through model 5 processing
on Input 2 are proven.

Transformer/Doubler, model 0, remains the one exception. It stayed below the
2 dB shape threshold under assignment-before-state, model-before-assignment,
and a fresh selector replay; the final two distances were 0.817 and 0.280 dB.
Per-channel L/R, mono, and side analysis also rules out a hidden stereo effect
cancelled by mono averaging. The failure is now model-0-specific, not a general
Channel-2 routing failure. Evidence is under
`runs/20260921T162414-0700-voicefx-input2-assignment/` and
`runs/20260921T182831-0700-transformer-selector-replay/`.

## 2026-09-20 controlling preset result: UC Store is `PrsM`, not a button-slot write

UC 4.7.2 has two separate preset concepts. The four front-panel fast-access
blocks are `MemP/Stat` indexes 0–3; UC exposes their selected index and read-only
titles but no command that assigns a library preset to one of those buttons.
Its explicit **StorePreset** action instead serializes the complete selected
channel record and sends `MemP/PrsM`: indexes 16–21 are Input 1's six Device
Presets and 22–27 are Input 2's six. **RestorePreset** applies a retained
library/local record through the ordinary component setters; it does not recall
`Stat`.

The Linux Host now follows that split. **Send to Device Presets** uses the exact
tagged `PrsM` envelope and records an identity-scoped
`WRITE_SENT_UNVERIFIED` receipt in `device-presets.json`. **Load** replays that
complete retained record through the normal Fat Channel and VoiceFX setters.
The older `Stat` writer is no longer a normal GTK action. A transport reply
still cannot prove stored-body readback, cold-boot persistence, or standalone
VoiceFX audibility because firmware exposes no library-body read command.

Scene export now includes every complete `Stat` and `PrsM` body known from the
current unit's local registries. Scene load validates and reports the libraries
but never overwrites them.

## 2026-09-20 mixer parity boundary and Host spring reverb

The UC 4.7.2 component model, retained device-page capture and recovered mixer
code now give one closed comparison for the Mixer tab. The Linux Host covers
every ordinary io24 routing operation: source fader/send levels and assigns for
Main, Mix A and Mix B; source mute; per-bus solo; output mute and bus master;
phones source; monitor blend; stereo link; Channel Mute Sync; and the shared FX
return. UC presents one selected bus as channel strips, while Linux keeps the
same complete state visible as a matrix on **Routing**.

The remaining UC fields are explicit protocol gaps rather than unfinished
nearby controls:

- `line.chN.pan`: the block-100 mixer has one level per source/bus and ignores
  its index field, so it cannot place one mono source independently. The CLI's
  older `pan` operation is correctly limited to a Host-side balance across a
  stereo pair using UC's recovered -3 dB-centre law; the GTK Host does not
  mislabel that as mono pan.
- `stereopan`: no representable width/mono-collapse control.
- `FXA`: UC names a per-input reverb send, but no independent io24 wire field
  has been proved. The device exposes one unified channel processing scalar;
  substituting it would also change EQ and dynamics.
- `dawpostdsp`: no proved safe route. Apparent neighboring ids collide with
  unrelated object gain/Main-volume controls.
- output `mono` has no proved representation.
- writable component names are UC host-model metadata, not an io24 command.
  Linux now persists them in its shadow and scene/preset files.
- Mirror Main is now a persistent Linux Host latch. Main level, assignment and
  stereo-pair-balance edits continue into the selected aux; clearing the latch
  restores that aux's retained state. This matches UC behavior while the Host
  owns the mix without claiming a firmware-resident latch.
- physical Main mute is readable in `JaSt` slot 42 bit 1, but no writable
  parameter reaches it. UC itself binds `hardwareMute` as display-only. Linux's
  output mute is the distinct software control.

The Effects page also has a **Host spring reverb**, deliberately separate from
device block 202. `io24_spring.c` is a wet-only stereo LADSPA processor:
short dispersive all-pass stages excite two decorrelated banks of damped
resonators. Its public controls are Input 1/2 send gain, Dwell, Tone, Drip,
Width, pre-delay, and Main output gain. `io24_spring.py` validates/persists that
state, builds the content-addressed plugin, emits a no-fallback/no-remix
PipeWire graph and owns its lifecycle.

The graph captures physical Inputs 1/2 after the Fat Channel and returns wet
audio through the best stereo pair exposed by the active io24 playback profile.
A dedicated USB 3-4 or 5-6 pair is an internal transport, not the destination:
while enabled, the Host assigns it to **physical Main 1-2 only**, removes it
from Mix A/Mix B, and restores every prior assignment and known fader state on
disable or clean shutdown. A stereo or 2.1 profile instead uses USB 1-2 and
leaves its existing device routes untouched. The graph still forbids remix and
fallback to another sound device.

`host_features.spring_reverb` keeps the algorithm controls and On state in Host
snapshots and last-session recovery. Named snapshots strip the temporary route
ownership record, exactly as Multiband strips its borrowed mixer/buffer state.
UC scene export reports the object as Host-only, and device-slot code never
serializes it.

Hardware-free contracts cover schema rejection, exact PipeWire target/channel
selection, Main-only route restoration, LADSPA ABI, silence, bounded output,
pre-delayed late energy, tail decay, stereo decorrelation/mono width, GTK state
capture and wheel contents. The implementation has been visually rendered at
desktop and narrow widths with the stub-device paint harness. No USB operation
was performed for this change, so live Main-output audibility remains a
separate acceptance result rather than being inferred from those tests.

## 2026-09-19 controlling result: UC scene save/load and settings parity

The Linux Host now treats a Universal Control `.scene` as a whole-device state
document rather than a pair of channel presets. `io24_scene.py` prevalidates the
complete JSON object and plans every supported top-level section: `global`,
`line`, `return`, `fxreturn`, `aux`, `main`, `fx`, and `presets`. The three
retained UC 4.7.2 scenes produce 94–97 real Host operations each. Those plans
include input state, routing and assigns, DSP amount, complete Fat Channels,
exact Standard/Passive/Vintage EQ, the singleton VoiceFX component, shared
reverb, return-source mute/solo, bus master/output mute, output delay,
headphones source, preset-button mode, Channel Mute Sync, persistent Mirror
Main, and Host component names. Device-resident preset libraries are reported
and never overwritten.

The Host also exports readable plus exactly shadowed state using UC's recognized
`global`, `line`, `return`, `fxreturn`, `aux`, `main`, and `fx` vocabulary. The
write is atomic and is accepted only after the generated object passes the same
planner as import. Complete registry-known `Stat` and `PrsM` bodies are included
under `presets`; physical slot selection is excluded. Missing
write-only state, enabled DSP Amount without an exact retained scalar, monitor
values with no supported scene field, and Host-only extensions are reported as
omissions rather than assigned guessed defaults. This proves Linux Host
round-trip, not acceptance by Universal Control's own scene importer.

The importer materializes exact alternate-EQ coefficients and VoiceFX frames
during planning, before the first device call. It refuses conflicting
per-channel VoiceFX records because firmware block 201 is one shared settings
object. All backend setters are also checked before apply begins. Before the
first write, load captures readable state, the Host's exact write mirror, and
solo state. The first runtime/USB failure stops the plan, prevents later writes,
and replays that checkpoint without selecting a device preset slot. Because the
protocol has no transaction primitive, the result is called complete only when
all attempted controls had exact prestate and every compensating write
succeeded; otherwise the caller receives the specific unknown values and
restore errors as a partial rollback.

UC fields without a proven io24 representation stay typed omissions, not
approximations: mono-source `pan`, `stereopan` width, exact `FXA`,
`dawpostdsp`, and output `mono`. Names and Mirror Main now round-trip as exact
Host-persisted UC component state.
In particular, the Host does not substitute its stereo-pair balance for UC's
mono-source pan.

The GTK Host now exposes scene saving and loading on Presets, Channel Mute Sync on Device,
and UC-style source/output mutes on Routing. Source and bus mutes retain every
individual level and assign while writing the affected sends off; unmute
restores the exact shadowed mix. These write-only settings participate in
reconnect replay. `auxMuteMode`'s wire route is decoded, but `1 = sync enabled`
remains explicitly an inference from UC's field name and the owner's-manual
behavior because the device has no readable state slot for it.

Device-preset language was corrected again after the retained UC Store/Restore
handlers were followed. Store targets the twelve-entry `PrsM` Device Presets
library, not the four `Stat` button blocks. A sent library record remains
`WRITE_SENT_UNVERIFIED`; it is not body readback or durable-commit proof.
Restore is Host-side component replay, so Linux **Load** now does the same.
Standalone Fat Channel audibility and Host-assisted VoiceFX are not conflated.

This repair and its regression tests were hardware-free. No USB, device,
firmware, preset, or audio operation occurred.

## 2026-09-18 controlling result: Passive and Vintage EQ have exact Host routes

Passive Program and Vintage 1970s EQ are now first-class Linux Host models.
UC 4.7.2's embedded component XML supplies their class IDs, fields, ranges,
switch lists, defaults and one independent `eqallon` power field per selected
model. Neither alternate model declares Standard EQ's four per-band power
fields.

The recompute call sites bind the semantic controls to the live packets. UC's
Passive path calls the combined high designer at `0x18001a610`, sends its seven
coefficients as `Lfdf` index 0, calls the combined low designer at
`0x18001adb0`, sends it as `Bqdf` index 1, then supplies identity `Bqdf`
sections at indexes 2 and 3. Vintage calls its low, high, hi-mid and low-mid
designers at `0x18001d470`, `0x18001cf30`, `0x18001cc20` and `0x18001c910`,
then sends them as `Lfdf[0]`, `Bqdf[1]`, `Bqdf[2]` and `Bqdf[3]`. The bounded
analyzer checks each call target plus the adjacent tag, byte width and index.

The Linux driver now validates the complete semantic model before transport,
interprets those hash-pinned designers, checks every float32 denominator for
stability, and emits the four packets in UC's live order. The GTK Host exposes
the exact controls and response curve, model-specific power, preset loading,
Host snapshots and reconnect replay. Selecting Standard removes the alternate
shadow for that input, and selecting Passive or Vintage removes its Standard
band shadow, so reconnect cannot replay two competing EQ models.

The proprietary DLL is read as data and never loaded or executed. The default
path is the retained UC 4.7.2 artifact; `IO24_UC472_DSPUSBDEVICE` may point to
another lawful copy, whose exact size and SHA-256 must match. Hardware-free
source, designer, packet, preset and UI-state regressions pass. No USB/device
operation was used for this result, so exact implementation is established but
a dedicated audible hardware A/B remains a separate gated test.

Primary artifacts: `io24_alt_eq.py`, `io24_uc472_passive_eq.py`,
`io24_uc472_vintage_eq.py`,
`re/uc472_alt_eq_live_route.py`, and
`re/uc_component_model/fatchannelxt_alternate_eq_contract.xml`.

## 2026-09-17 controlling result: all six Host VoiceFX models process audio

The corrected UC 4.7.2 lifecycle is now verified across every model on the
Linux Host's physical Input-1 path. A guarded Main-L -> Input-1 run fully
materialized Transformer, Detuner, Vocoder, Ring Modulator and Filters while
off, then changed only each model's own state tag for two independent on/off
cycles. All five processed the deterministic waveform repeatably. Combined
with the preceding Delay timing proof, this closes Host-driven audio coverage
for all six VoiceFX models.

The source was -22.353 dBFS with 19.525 dB gate contrast; source and off-state
drift were both at most 0.0065 dB. The same-return reverb control rose
+11.708 dB. Model evidence was:

- Transformer: 7.201/7.448 dB gain-normalized spectral change;
- Detuner: the 800 and 1234 Hz stimulus tones appeared at the exact
  -8-semitone targets, 503.97 and 777.37 Hz, in both cycles, with +33.872 to
  +41.135 dB target rise;
- Vocoder: 11.521/11.821 dB spectral change;
- Ring Modulator: the programmed 313.7 Hz sidebands appeared in both cycles,
  with maximum +33.090/+34.409 dB rise and stable guard bands;
- Filters: 4.218/3.856 dB spectral change;
- Delay (preceding run): repeats at 250/500/750 ms in both cycles.

The raw five-model report initially called Detuner missing because a generic
pitch-shift guard rule rejected ordinary broadband redistribution. Both exact
target peaks were already present in the immutable WAVs. The corrected
two-tone classifier requires strong rises at both predicted ratios plus local
peak prominence, and returns
`VOICEFX_REMAINING_MODELS_PROCESSING_DETECTED`; no device packet was changed.

Cleanup restored the exact 11 dB Input-1 prestate, every readable device
field, the PipeWire sink's original mute state and volumes, and all
shadow-backed VoiceFX/reverb/routes. This proves Host-driven processing on
Input 1. It does not prove simultaneous two-lane processing or standalone
VoiceFX after device-preset recall.

Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260917T230349-0700-voicefx-remaining-models/`.

## 2026-09-17 controlling result: corrected Host Delay processing detected

The UC 4.7.2 lifecycle below is now verified end to end on hardware. Main L was
looped into physical Input 1 and the Linux Host selected/materialized Delay,
then used `vech`-only On/Off edits. A deterministic gated waveform produced
repeats at the programmed 250, 500 and 750 ms positions in two independent
cycles. Maximum repeat-window rise was **+12.971 dB** and **+12.985 dB**. The
shared-reverb same-path positive control rose **+12.576 dB**; VoiceFX-off
source stages held within **0.003 dB**, and off-state Mix-A gaps held within
**0.064 dB**.

The Input-1 USB send is post-DSP in this state, consistent with UC's
`dawpostdsp` model. It changed by the same approximately 3.11 dB in the two
Delay-on captures and stayed stable in every VoiceFX-off capture. Source-drift
validation must therefore use VoiceFX-off stages only; including Delay-on
would make a functioning insert invalidate its own proof. Reanalysis of the
immutable captures returns `run_valid: true` and
`VOICEFX_DELAY_PROCESSING_DETECTED`.

Cleanup restored the exact 11 dB Input-1 prestate, every readable device
field, the PipeWire sink's original muted state and volumes, and all
shadow-backed VoiceFX/reverb/routes. The separately valid Input-2 run detected
no Delay, so this result proves the current Input-1 lane rather than
simultaneous two-lane processing. It supersedes the older dry conclusions for
the pre-capture Linux transaction.

Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260917T222751-0700-voicefx-input1-waveform/`.

## 2026-09-17 controlling result: captured UC 4.7.2 VoiceFX lifecycle

The missing Host behavior is now captured, not inferred. Universal Control
`4.7.2.108537` controlled the test unit, firmware `0128`, inside the
isolated Windows guest while Windows audio services were stopped. USB traffic
was recorded on the device's active control endpoint; endpoint 3/83 remained at
zero transfers. No firmware update, reset, DFU action, preset save, playback or
audio capture occurred. The device state, Linux audio binding and VM disk were
restored and verified after detachment.

UC's VoiceFX transaction is:

1. When the algorithm changes, send one `VoFx` selector and immediately
   materialize the selected model.
2. For an ordinary **On**, **Off** or parameter edit, send only that model's
   state tag. Do not resend `VoFx`.
3. Submit the frames as synchronous USB transactions with no extra host sleep.
   In the capture, the six Transformer submissions span 2.571 ms, with roughly
   0.4–0.6 ms between submissions.

The exact captured examples are:

```text
Delay selection:       VoFx(model 5), vech
Delay On/Off:          vech only
Transformer selection: VoFx(model 0), Bqdf(0), Bqdf(1),
                       MBdf(0), MBdf(1), godv
Transformer On/Off or WetDry: godv only
```

Each `MBdf` is `0x1f4` bytes. It has component index and table index at
`+0x08/+0x0c`, up to twenty `{5 x f32 coefficient, f32 sampleRate}` entries at
`+0x10`, and the active count at `+0x1f0`. UC supplies four entries: 44100,
48000, 88200 and 96000 Hz. Transformer table 0 is the 600 Hz low shelf and
table 1 the 550 Hz low shelf. The Linux builder now emits those tables with
unused entries zero-filled; firmware consumes only the count.

The capture contains no separate VoiceFX activation tag. Its `mprm` frames are
ordinary mixer snapshots, not a gate: UC explicitly leaves mixer source 5
(`fxreturn/ch1`) at -96 dB, and selecting, enabling, changing WetDry and
disabling VoiceFX emit no mixer write. Therefore the older Linux prelude that
opened the block-202 reverb return before every block-201 edit was not UC
behavior and has been removed. Reverb routing remains unchanged.

The implementation now follows that lifecycle: it caches the selected model,
materializes only on selection, uses state-only edits thereafter, refreshes
Transformer filter material only when Lows or sample rate changes, includes
both `MBdf` tables, and adds no 20 ms delay. The GTK rack remains a direct
rendering of `dsp_fx_params.xml`: six model components, each with its own
storable **On**, exact control order and exact builder mapping. There is no
master/seventh VoiceFX switch.

This supersedes the older conclusions below that Host-side sequencing was
exhausted, that selector replay was required, or that VoiceFX uses the shared
reverb return. The 2026-09-15 dry listening result tested the older Linux
transaction and does not test this corrected implementation. Audio was
deliberately disabled in the VM capture; the later physical Input-1 waveform
run above now supplies the Linux-Host acceptance for Delay. Device-preset
storage is a separate result: VoiceFX data can be present in a saved record
without becoming audible standalone, while Fat Channel state is audible
without a host.

Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260917T145627-0700-uc472-audio-suppressed-rerun/`.

## 2026-09-15 historical negative: superseded VoiceFX transaction; block 202 audible

This result accurately describes the older Linux transaction used that day; the
corrected UC 4.7.2 transaction and the Input-1 waveform results above supersede
its conclusion about block 201.

Decided by listening, not by a meter, on the test unit, firmware `0128`,
with the Host closed and an instrument cable — then a guitar — in Input 1.

    Input 1 -> channel 1 DSP -> processing scalar ('Para' wire 4, 1.000)
            -> block 20x -> fxreturn/ch1 -> Main (+6.79 dB) -> headphones

On that path the shared reverb (block 202) is **audible**, on cue, in every run.
Block 201 on the identical path with 202 explicitly silenced is not: Delay at
450 ms / feedback 0.55 / 100 % wet across three on-off phases, then all six
models in sequence each fully wet, then a sustained guitar through Delay at
250 ms / feedback 1.00 / 100 % wet. "Signal is dry" throughout; "now theres
reverb again" at the closing positive control. Three independent ear tests, each
with a positive control that fired. Note that the dry signal heard in every run
had itself passed through the channel DSP chain at scalar 1.000, so the insert
reading of block 201 is covered by the same listening test as the send reading.

Two claims in this file do not survive it.

**The "wrong bus" claim under §12S is withdrawn.** That correction says the CP34
nulls "are the expected reading of the wrong bus, not evidence that the insert
is inert." The bus was indeed wrong — the send and return must be open, and only
`_push_reverb(establish_path=True)` ever opened them, which is why the reverb was
the only effect ever heard. But opening them does not make block 201 audible.
CP34 reached the correct conclusion by an unsound route; the conclusion stands.

**The 2026-07-28 "Delay ear-verified" record is reattributed to the reverb.**
That record itself notes the first delay test "only appeared to work because the
reverb's routing was still open", and block 202 shares the return block 201
feeds, so the same confound applies to the verification that followed it. A
reverb with pre-delay and stereo decorrelation is a fair description of "a
panning echo thing".

The send/return repair is real and necessary and stays:
`Io24.establish_effects_return` and `io24gtk.establish_effects_path()` are what
make the reverb audible with the Host closed.

### The per-module On is already on the wire

Universal Control gives each Voice FX module its own `On` toggle rather than one
master, and that is the vendor model, not a UI flourish:
`re/uc_component_model/dsp_fx_params.xml` declares a storable `on` toggle as the
first parameter of `VoiceOfGod`, `DarthVoice`, `RobotVoiceA`, `RobotVoiceB`,
`RobotVoiceC` and `VocalEcho`, and there is no container-level enable above them.
`re/uc_component_model/dspusb_component_model.xml` carries exactly one *mutable*
`voicefx` component per mic strip, preceded by `voicefxopt`
(`InsertFXSelector`, starttag 450), so one module is live at a time and its own
`on` is that module's button. `StereoLineInputDSP` has no `voicefx` at all.

That parameter is what this driver sends. On a model change, `io24_fx.set_fx_*`
emits `VoFx` selection followed by that model's complete materialization; later
On/Off and ordinary control edits send only the selected model's state blob,
whose first word is `on`. The GTK Host exposes the six mutable components as a visual rack.
Selecting a rack unit activates the corresponding `voicefxopt/fxmodel` choice;
every component page is then built in exact XML parameter order and routes each
editable XML ID to its recovered builder keyword. Each page's **On** switch
feeds that selected component's first state word; there is no seventh/master
switch. Lists (`TuneList`, `Carriers`), defaults, bounds, units and skew
midpoints also come from the retained schema, and Vocoder `avoiced` is shown as
read-only rather than invented as a write. The all-six ear sweep sent `on=True`
per model. Matching UC's six On controls fixes the Host's component model and
preserves separate per-model UI state, but it does not change the bytes that
reached the device in that sweep and is not by itself an audibility fix.

Everything host-reachable is now closed by ear. Do not commission further
host-side block-201 sequencing. One route is untested and unreachable from the
host — the device applies its stored preset at power-on, so a cold boot with a
stored Voice-FX-on slot and no host attached is the last place block 201 could
render. `re/io24_native_delay_recall.py` is prepared for it and needs a power
cycle plus separate exact authorization.

Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260915T211916-0700-fx-ear-verdict/`.

## 2026-09-14 controlling FX discriminator: test before gate-control writes

The same-value Input-1 assignment is not the activation. With both channels
processing (flags 385), the guarded assignment plus all six model sequences
produced only **-0.153 to +0.033 dB** changes. The direct Channel-1 gate moved
the same bus **27.041 dB** with stable source and off baselines, so this is a
valid block-201 null rather than a blind meter.

The earlier **-4.024 dB** Transformer positive ran at flags 449, immediately
after Channel-2 DSP Amount had been pinned to exact zero. The authorized
discriminator recreated that readable state by temporarily changing Channel 2
from the supplied 1.0 to zero. Transformer still produced a stable **-0.097 dB** null with **0.016
dB** off drift. The same-session Channel-1 gate control moved the selected bus
**26.614 dB**. The supplied 1.0 was resent and readback returned to nonzero;
firmware cannot verify the exact positive scalar. Other-channel DSP zero as the
flags difference is eliminated.

The positive also differed in write-only and hidden state. It began
Transformer-off rather than Detuner-off, used gate-off before its 30 -> 60 dB
gain step, waited roughly five rather than 0.5 seconds before FX, and showed
equal Main/Mix-A/Mix-B ratios rather than the null's split bus ratios. Moving
the configured-gate control after FX is directionally correct but is not a
one-variable reproduction. Its first authorized live attempt found
`194f:0422` absent at open and made no writes. After re-enumeration, the test
completed but Input 1 changed **-12.973 dB** and the Transformer-off baselines
drifted **-5.112 dB** with **3.194 dB** maximum MAD. The apparent **-2.965 dB**
Transformer midpoint delta is therefore inconclusive. Firmware construction
still initially installs model 0; a repeat must prove a quiet, stable source
before its first write and abort read-only otherwise.

`--transformer-repeatability` now implements that repeat without the eliminated
Channel-2 change: two quiet read-only windows, two Transformer A/B/A cycles,
stable off-return and same-sign on-delta requirements, then the gate control.
It is offline-tested and awaits exact live authorization.

Host preset persistence is not coupled to this unresolved FX state. Named
presets save locally with FX off, and Fat Channel replay completes before the
separate optional FX transaction.

Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260914T181939-0700-user-effects-audio-followup/`.

## 2026-09-14 superseded candidate: measured-good assignment prelude

The current Host's six FX models are inaudible by user listening, but block 201
is not universally inert. The retained attempt-3 measurement found Transformer
on at **-4.024 dB** versus stable off baselines, with **0.045 dB** drift and a
valid **26.248 dB** same-path gain control. That probe inherited the current
armed device state after the older Host was closed; it did not prove a pacing
effect. The Host version preceding the positive sent
`set_voicefx_channel(1)` once before its first model write. `99c8e7b` removed
that prelude, and the later negative Host runs did not reproduce it. The prior
timing diagnosis is withdrawn.

The Host now restores that activation prelude on startup, explicit preset FX
load, and the first FX edit. Since `processingChannel` is real permutation-
sensitive routing, `Io24.reassert_voicefx_input1()` first requires the live
mapping to be the already-normal `0/1`; it then resends Input 1 without changing
the permutation. It refuses an unreadable or swapped route before transport.
The original builder frame order remains, but is not claimed as the activation
mechanism. This is an offline repair pending the six-model meter acceptance.

Fat Channel preset replay and FX replay are also separate transactions. A
normal preset loads first without block 201; the Host then attempts its global
FX state and reports an FX-specific failure without undoing or misreporting the
preset.

The reverb scalar remains a blend inside the parallel FX return, so `100 %`
does not remove direct Main. The user's recollection that UC needed the return
above 0 dB plausibly explains the perceived amount; they approve the Host's new
character and movement controls. Those controls remain labelled as Host
approximations because the device exposes one algorithm rather than those named
modes.

## 2026-09-14 controlling result: Host paths and reachable control surfaces are closed

Host v2's source-bound selector replay was tested live and remained inert:
Channel-1 gate **-19.551 dB**, zero-output Ring Mod **-0.167 dB** against a
**1.722 dB** threshold. Opening playback and capture at an observed 48 kHz /
period 512 produced gate **-19.580 dB** and Ring Mod **+0.005 dB**; Channel 2
produced **+0.035 dB**. The prior claims that v2 was untested and that meter-only
runs were known to be 96 kHz/512 are withdrawn.

`Para` 1..14 and `Pari` 4..19 are fully enumerated; firmware no-ops and all
unclaimed integer IDs are accounted for. Block handler `0x553d4` compares the
three-word `VFxO`, routes `VoFx` to selector `0x55388`, and tail-calls generic
handler `0x5079c` for other model tags. Correctly framed `VFxO` changed Ring Mod
only **-0.132 dB** against **2.724 dB**, with a **-19.647 dB** gate control. It
is not an enable.

No further Host-constructed sequence is justified by current evidence. The next
decisive test is device-resident: cold-boot a selected stored preset whose Voice
FX is unmistakably on, with no Host attached. Audible narrows the missing edge
to host messaging and calls for a UC capture; inaudible is a defensible
firmware-0128 stop-and-publish result. That slot write, selection, power cycle,
and listening test require fresh exact authorization.

Future meter-only probe schemas are v2 and report ALSA timing as unobserved
instead of presenting hardcoded constants as measured configuration. Existing
evidence files remain immutable.

## 2026-09-14 superseded correction: replay VoFx, not model state

The block-201 primary handler at firmware offset `0x553d4` routes the `VoFx`
selector tag to `0x55388`. Non-selector model-state tags instead tail-call
generic handler `0x5079c`, which forwards directly to the currently installed
delegate. Consequently, a model-state frame cannot trigger the selector's
deferred replacement logic.

The exact replacement sequence is:

```text
send VoFx selector
wait 60 ms for the nominal 40 ms bypass transition
replay the same VoFx selector to install the requested model
send the model's state frames
```

The authorized Ring-Mod run used the now-superseded selector -> wait -> state ->
wait -> state-replay order. Its valid gate control moved `-19.278 dB`; Ring Mod
moved only `-0.099 dB` against a `2.814 dB` threshold. That resolves the old
sequence as ineffective, not the corrected selector-replay sequence or Ring Mod
itself. `Io24.send_fx` now implements the source-bound order, hardware-free
verified by 871 passing tests with one expected skip. Fresh exact authorization
is required for another live operation.

## 2026-09-14 superseded test design: zero-output Ring Mod is decisive

Model 3 is source-bound through installer call `0x5f1cc`, constructor
`0x58920`, tagged handler `0x58db0`, and process `0x4ff6c`. The `botb` handler
stores on, output volume, and mix at model offsets `+0x14`, `+0x20`, and
`+0x2c`. The process derives:

```text
dry gain = volume * (1 - mix)
wet gain = 2 * volume * mix
output = wet signal * wet gain + input * dry gain
```

When the model is off, the process uses dry gain 1 and wet gain 0. Therefore a
fully-wet active call with `vol=0, mix=1` must produce dry gain 0 and wet gain
0. This is materially stronger than the underpowered Delay energy test: a
stable flat result with a valid same-session gate control was designed to
resolve the Host sequence used by that run. Static follow-up later proved the
sequence needed selector replay rather than state replay.

The guarded implementation is `re/io24_corrected_ringmod_probe.py`; the pinned
static analyzer is `re/cp34_ringmod_discriminator_static.py`. Both were offline
verified, including restoration-on-failure. The later live execution is
classified by the controlling correction above.

## 2026-09-14 controlling static result: Delay state and lanes are fully bound

The model-5 continuation closes the static question raised by the live scalar
result. Lazy installer `0x5f130` dispatches model 5 to constructor `0x56f34`.
Its primary vtable is `0x6012c7e8`; vtable `+0x00` reaches tagged handler
`0x57240`, and `+0x0c` reaches process routine `0x4ebd4`.

For a non-`VoFx` block-201 message, generic handler `0x5079c` loads
`block+0x14` and tail-calls the delegate's vtable `+0x00`. Model 5 accepts tag
`vech` and copies on, mix, feedback, and time to `model+0x10..+0x1c`. Its
process reads those exact fields.

The audio root's descriptor gives the model one input, two outputs, frame count,
workspace at `device+0x11d0`, and zero at both descriptor flags `+0x18/+0x1c`.
Input and output tables both point at `device+0x5068`, so processing is in
place. The model's on path implements:

```text
delay_write = input + feedback * delayed
output = mix * delayed + (1 - mix) * input
```

This eliminates missing model-5 construction, state application, process
dispatch, or lane binding. It also proves why the live scalar test was
underpowered: feedback coefficient 0.5 at fully wet predicts only +1.249 dB for
stationary white noise. The next objective discriminator must use a temporal
signature or a model such as Ring Mod whose predicted scalar change exceeds
the detector threshold.

## 2026-09-14 superseded live correction: first paced Delay test was inconclusive

The first paced state-replay sequence was tested live on the test unit,
firmware `0128`, with the Channel-1 path open. The controlling
correction above proves it did not trigger rebuild. Fully wet Delay
at feedback 1.0 moved the Main/Input-1 ratio by only **-0.148 dB** against a
**1.990 dB** threshold; off-state drift was **-0.040 dB**. The same-session
direct-gate positive control moved **-18.313 dB** against a **12.014 dB**
threshold with **-0.049 dB** drift. The meter was sensitive to a gross gate
change, but that does not make the Delay level test sensitive enough.

The old +6 dB expectation was wrong for this stimulus. The API's feedback 1.0
is coefficient 0.5 on the wire; for uncorrelated stationary noise, the model's
recurrence predicts only **+1.249 dB**, below the run's **1.990 dB** detection
threshold. Therefore this run proves only that corrected Delay produced no
resolved scalar-level change. It does **not** prove Delay remained inert and
does not accept or reject Host pacing as the missing audibility mechanism.

Readable flags, slots, gains, Main, headphones, link, phantom, and processing
permutation restored exactly. Host-known Transformer-off and `MAIN` gate state
were resent; routes, slots, DSP Amount, and `processingChannel` were untouched.
Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260914T113431-0700-corrected-fx-rebuild-live/`.

## 2026-09-14 controlling correction: block 201 is scheduled through model 0

Constructor `0x591bc` does initially store zero at `block+0x14`, but it then
selects model 0. Selector `0x55388` sets `block+0x44=1`, sees the null delegate,
and calls primary vtable `+0x34`, `0x5f1f4`. Lazy installer `0x5f130` constructs
the model-0 object at `block+0x60`, stores it at `block+0x14` at `0x5f170`, and
clears `block+0x44` at `0x5f184`.

The block audio routine corrects the first continuation's “rebuild-only” label.
`+0x44` is target bypass (`0=process`, `1=bypass`), `+0x39` is latched bypass,
and `+0x40` is the crossfade countdown. Selection requests bypass before model
replacement; installation clears bypass afterward. Handler `0x50af8` first
forwards messages to delegate `vtable+0x10`, then uses the three fields to defer
rebuild work until the old model is safely bypassed. They are audio transition
state, not an absent install gate.

The function previously named the chain installer, `0x3e608`, is a workspace
sizer. Caller `0x3f13a` supplies three object pointers, but `0x3e608` only calls
each object's `vtable+0x18`, takes the maximum returned requirement, and
allocates/resizes `device+0x11d0`. It does not retain those pointers or prove
that block 201 is in the audible processing graph.

The actual graph edge is source-bound elsewhere. Block 201 primary
`vtable+0x0c` maps to reset-relocated raw routine `0x1a580`; it loads
`block+0x14` and invokes selected-model `vtable+0x0c`. Model 0 maps to the known
DSP process at raw `0x1e1a8`. Audio root raw `0x20546..0x2054e` builds the
buffer descriptor at `stack+0x40`, forms `device+0x11e0`, and directly calls
the block routine. Its call condition is sample rate at or below 96 kHz
(`device+0x5e71`) with stereo-link bit 12 clear. The cold-boot run's declared
96 kHz/unlinked state lies on that branch.

The cold-boot negative remains valid for its transmitted sequence. Setup
computes a nominal 40 ms bypass fade; at 96 kHz / 512 this is eight blocks,
42.667 ms. The first correction extended the former 20 ms gap but mistakenly
used a model-state frame to trigger rebuild. Primary handler `0x553d4` proves
those tags bypass the selector and go to the old delegate.

`Io24.send_fx` now sends selector, waits 60 ms, replays selector to invoke the
rebuild, then sends model state. This sequence is hardware-free verified but
not live-executed; neither the underpowered Delay run nor the later decisive
Ring-Mod null tested it.
Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260914T105415-0700-block201-delegate-lifecycle/`.

## 2026-09-14 historical negative: block 201 inert with the then-current transaction

12S reproduces on a cold-booted unit with an open Channel-1 path and a decisive
same-session positive control.

Setup: physical cold boot (`devnum` 2 -> 4), Host closed, Voice FX owner read
from the device (Channel 1), Main-to-Input-1 ratio, 40-sample windows.

**Positive control first.** Direct `set_gate` on Channel 1 moved the ratio
**-19.475 dB** against a 5.46 dB threshold with 0.018 dB drift. The meter sees
Channel-1 processing.

**Block 201, channel gate at its power-on state so the path is open:**

| model | delta | threshold |
|---|---|---|
| delay, fully wet, feedback 1.0 | +0.126 dB | 1.78 dB |
| filters, fully wet | -0.129 dB | 2.15 dB |
| ringmod 220 Hz, fully wet | -0.051 dB | 2.18 dB |

The channel reaches Main, three fully wet models are armed, and nothing happens.
The later controlling correction above proves the selected-model delegate is
constructed, the audio root calls block 201 with buffers, and the block calls
the model process. It also withdraws the old §13a “chain installer” label. The
corrected deferred-rebuild check is now closed negatively above. The remaining
question is downstream per-model process/lane state, not an assumed missing
outer flag, scheduler, install call, or Host wait.

**Separately: selection does not re-apply a stored preset.** After forcing the
power-on gate, selecting slot 0 left the ratio at -0.294 dB; a real re-apply of
`MAIN` would have closed the gate to about -20 dB. `Pari` 16 selects an index
and does not make the firmware reload the body.

**But power-on does apply it.** The cold-boot baseline was -20.894 dB, exactly
`MAIN`'s stored gate closing on a -61 dBFS noise floor against its -48.72 dB
threshold. Re-applying that gate directly reproduced -19.388 dB. The device
holds `MAIN` in slot 0 and applies it at boot.

Evidence: `.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260914T102740-0700-cold-boot-block201/`.

## 2026-09-14 superseded firmware reading: selector sets +0x44 during construction

The byte sequence below is correct but the former end-state interpretation was
not. The controlling correction above follows selector `0x55388` through its
work virtual: lazy installer `0x5f130` constructs the delegate and clears
`+0x44` before the constructor returns.

Constructor `0x591bc`:

```
0x591e6  movs   r5, #0
0x591ec  strh   r0, [r4, #0x38]     ; r0 = 0x101 -> byte +0x39 = 1
0x59202  strb.w r5, [r4, #0x44]     ; +0x44 = 0
0x59214  strd   r5, r5, [r4, #0x3c] ; +0x40 = 0
0x59240  mov    r1, r5              ; model = 0
0x59248  bl     0x55388             ; sets +0x44 = 1 for any model 0..5
```

Selector `0x55388` rejects only a negative model or one >= 6; its `bne 0x553cc`
branch calls a delegate and returns to the same check, so it is not an exit.
`r5` is callee-saved across the intervening `bl 0x10d7e0`.

Do not read `+0x44` as an ordinary positive enable. The controlling trace above
shows it is target bypass: selection sets it so replacement waits for a safe
dry state, and installation clears it to request processing. The §13a
installation interpretation is separately superseded by the `0x3e608`
workspace trace above.

Scope: the construction path only. No claim is made that nothing later changes
those fields; a whole-image displacement scan cannot be attributed to this
object and is not evidence either way.

Evidence: `.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260914T102032-0700-block201-gate-open-at-construction/`.

## 2026-09-14 controlling host result: UC recalls a channel preset host-side

The companion to the absent assignment edge below. Universal Control does not
recall a channel preset by making the device replay a stored record; it applies
the preset to its own model of the channel and lets the ordinary parameter path
push the result. That is why driving `MemP/Stat` plus selector recall on
hardware moved nothing, through three separate live runs and a positive control.

Receiver `0x18003aef0` in the pinned UC 4.7.2 `dspusbdevice.dll` (`de685e89...`)
understands exactly three incoming messages. Message names are compared against
the request object (`mov rcx, r14`); parameter keys against a member at `+0x48`:

| kind | literals |
|---|---|
| messages | `RestorePreset`, `StorePreset`, `GetPresetFileDescriptions` |
| keys | `presetTarget`, `presetFile`, `presetType` |

There is no assign, commit, apply or activate message. `RenamedPreset` and
`PresetFileDescriptions` occur in the same body but are outgoing notification
names, not compares — do not count them as a vocabulary.

`RestorePreset` branches on `presetTarget` against `channel` and `scene`, and
nothing else. The `channel` branch parses the index out of `presetFile`, bounds
it against the model's channel count at `[device+0x160]`, indexes the host-side
channel array at `[device+0x158] + 0xe8` with stride `0x120`, guards it at
`0x18003c8a0`, and calls applier `0x18002e690`. The applier brackets its work in
the channel object's own `+0x8` virtual and delegates component selection to
`0x18002e810`, which builds the filter `voicefx` / `comp` / `opt` / `voicefxopt`
through `0x18002ddf0`.

Over the module's direct call graph, twelve functions materialise a `MemP`,
`Stat` or `PrsM` tag immediate. The applier's entire 80-function subtree reaches
none of them; `StorePreset`'s library sender `0x180037250` reaches one
immediately, as the positive control in the same run. The caveat travels with
the claim: this is direct-call evidence, with 97 indirect call sites still in
that subtree including the document's own apply virtual.

The module holds exactly one parameter-frame builder, `0x1800541d0`, containing
the module's only `SetP` immediate at `0x18005423c` and gating on
`[this+0x68] == 2` to prepend `GetP`/`SetP`. It sits at `+0xd8` of the same
protocol-encoder vtable (`0x1804393e0`) whose `+0x10` is the `MemP` fragmenter,
so records and parameters are two slots of one encoder class rather than two
transports.

Consequences. The Linux Host's direct-setter preset apply is the UC-equivalent
operation, not a workaround for a missing one. The `MemP/Stat` nulls remain true
but were measuring a route the vendor never uses for recall, so they say nothing
about whether the device can recall — the edge was wrong, not the measurement.
And UC does not demonstrate device-resident standalone recall over these three
messages either, which together with the absent assignment edge below means
"save to the device and recall it standalone" is not a UC capability this Host
is failing to match. Whatever drives the unit's four buttons is firmware-side.

The `scene` branch at `0x18003be50` was bounded, not traced.

Analyzer `re/cp34_uc472_restore_preset_route.py`; evidence in
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260914T092114-0700-uc472-restore-preset-route/`.

## 2026-09-14 controlling host result: the fast-access assignment edge is absent

The long-open question of which wire operation assigns a preset to one of the
four fast-access button slots is closed: there is none, because Universal
Control has no such operation to send.

Enumerated exhaustively from the retained, hash-pinned UC 4.7.2 payload, the
shipped skin contains exactly four controls naming a slot, store, assign, mixer
or disk:

| control | kind | effect |
|---|---|---|
| `presetSlot` | RadioButton 1..4 | selects the active slot |
| `sendToMixer` | Button | local list -> device preset library |
| `sendToDisk` | Button | device preset library -> local list |
| `storeNamedRemotePreset` | Button | store into the device library |

UC's component model declares `activePresetSlotIndex` (int 0..3) as the only
**mutable** slot parameter, and the four slot contents `presetSlotTitle1..4` as
**readonly**. `presetHotKeyTitle` is readonly too; `presetButtonMode` (int 0..2)
is storable/mutable.

So the host can select a slot and can read what is in one, but cannot write a
slot's contents. `sendToMixer` targets the twelve-entry device library
(`MemP/PrsM` 16..27), which §13 already separated from the four button slots.
Earlier notes recording `explicit_assign_frame_located: false` should be read as
a property of the product rather than an unfinished search: a capture of a UC
session cannot reveal an assign frame, because none is ever sent, and `PrsM` is
not a route to button assignment.

This says nothing about what the unit's own front panel can do, which remains
the plausible mechanism for populating those slots and is untested.

Analyzer `re/cp34_uc472_fast_access_slot_assignment.py`; evidence in
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260914T091406-0700-fast-access-slot-assignment/`.

## 2026-09-14 controlling storage result: no separate commit follows `MemP/Stat`

The physical-storage trace now resolves the concrete device interface beneath
backend wrapper `0x316b0`. Device construction places the storage manager at
`device+0x1108`; its projection reaches the interface at `device+0x1384`,
whose vtable `0x6012bc80` maps write/read to `0x4aa90`/`0x4aaf4`.

The receiver data flow is source-bound rather than inferred: initializer
`0x20d3c` passes `device+8` to `0x3d4bc`, which stores it at singleton field
`+0x38`. Adapter adjustor `0x316dc` reaches `0x316b0`; that function reloads
the same field and invokes device-vtable slot `+0x10`, anchored to query
`0x20808`. The query projects through the manager to the concrete physical
interface above. Independent review found this missing join in the first
analyzer revision; the corrected version pins the code slices, instructions,
singleton literal, and direct branch targets.

The wrapper's flag `1` is not a deferred-dirty flag. It selects
compare-before-program at `0x52cd8`: changed bytes enter transaction `0x52c0c`,
wait through `0x526dc`, and run the ready poll at `0x52a70` before success is
returned. Unchanged bytes return as a successful no-op. No separate flush or
commit edge follows a successful write on this route. The bounded analyzer is
`re/cp34_firmware_physical_storage_commit.py`.

The outer `SetP` transport still returns no readable storage status and slot
bodies remain unreadable. An authorized native gate off/on/off discriminator
nonetheless matched UC's selected-slot write condition: global slot 3 was
active before every body write and each write received a forced 2 -> 3 recall.
It detected no gate effect (-0.068913 dB versus +0.112856 dB off drift,
0.349676 dB maximum MAD). Readable state restored exactly and the pinned Piano
body was rewritten while active, though that body remains unreadable. Thus
neither inactive-slot timing nor a missing commit command explains the three
native nulls. Stop `Stat` body variants and investigate UC's distinct explicit
Store/assignment (`PrsM`) route.

## 2026-09-14 controlling offline result: Passive EQ coefficient seam complete

UC's retained factory **Big Vocal** record supplies a real Passive Program
state with class `{C0730CBB-5135-4558-9222-C40BDBA036ED}`. The pinned UC 4.7.2
recompute calls `0x18001a610` for its seven-float combined high section and
`0x18001adb0` for its five-float combined low section. It emits `Lfdf` index 0,
`Bqdf` index 1, and identities at indexes 2/3; firmware native slot 2 has that
same one-main-plus-wide structural shape.

`re/uc472_passive_eq.py` now interprets both vendor routines at 44.1, 48, 88.2,
and 96 kHz and can replace only the EQ coefficients in a validated native
record. At this checkpoint the Host's read-only Passive view decoded the actual
20/30/60/100 Hz, 3/4/5/8/10/12/16 kHz, and 5/10/20 kHz switch positions, but
the live Host was not yet wired. The 2026-09-18 result at the top of this file
supersedes that implementation limit. No device operation was used for this
continuation.

## 2026-09-14 controlling device result: native MemP/Stat transport does not apply

Two authorized, reversible Channel-2 slot-3 A/B/A tests now close the current
device-slot writer route negative. The first used a complete native version-2
Piano strip and changed only its Standard EQ between flat and a +15 dB/1 kHz
shelf. The second used firmware's Channel-2-shaped native slot-2 body and
changed only both exact stored gate states between off and on at -40 dB.

Both tests completed exact-length `MemP/Stat(3)` transport and every selector
transition read back, but neither body became measurable DSP state. EQ moved
-0.180 dB with 0.021 dB flat drift; gate moved -0.181 dB with -0.086 dB off
drift. The same meter path previously detected about 6.66 dB from a direct
live shelf. This independently excludes the tagged/native prefix issue,
Standard-EQ arithmetic, and wrong slot-family choice as sufficient
explanations.

The known store-plus-selector sequence is therefore not an accepted Host
device-slot writer. `save_device_slot()` remains a low-level exact-transport
primitive and must not be exposed as a working save action. Reopen this route
only with a separately captured/source-bound commit or apply operation; do not
keep varying record bodies. Both runs restored readable state exactly and sent
the pinned native Piano strip back to slot 3, but stored-body restoration is
necessarily sent-not-readable. Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/{20260914T065602-0700-native-standard-slot-live-attempt-1,20260914T070547-0700-native-gate-slot-live-attempt-1}/`.

## 2026-09-14 controlling native-component result: Standard and Vintage EQ writer seams

The four firmware-native version-2 defaults fully account for each channel-strip
component. The native `eq  ` leaf stores 22 coefficient floats at each of
44.1, 48, 88.2, and 96 kHz: three five-float sections and one seven-float
third-order section. There is no hidden semantic-parameter region. UC 4.7.2's
Vintage switch tables and all four proprietary designers are now recovered;
the pinned-DLL executor can replace only those 88 coefficient bytes per clock
inside a complete validated native record while preserving every other leaf.

Native `gate` contains two exact 60-byte payloads from live `gate` blobs, and
native `comp` contains four rate-specific side-chain biquads followed by two
exact 52-byte payloads from live `cpxt` blobs. Firmware slot 0 matches the
retained `Piano Accomany.scene` Channel 2 gate and FET compressor byte-for-byte
when rebuilt at 44.1 kHz; all four compressor side-chain biquads also match.
The limiter matches the existing exact formulas. UC's HPF is source-bound to
the selector-3 binary64 designer, and the native `filt` leaf now supports an
atomic four-clock coefficient replacement. The embedded 40 Hz firmware values
are mixed historical recomputes rather than a canonical UC oracle. Standard
peaking selector 6, low-shelf selector 8, and high-shelf selector 9 have
source-bound instruction transcriptions.
The Standard semantic builder emits all four native clock sections and its
record-level wrapper changes only the EQ leaf in a validated native base.
At this checkpoint Passive and Vintage were exact offline coefficient seams
that relied on the retained pinned DLL; the 2026-09-18 result later connected
the same pinned designers to the live Host route.
The Standard coefficient vectors are transcription regressions, not captured
UC output; bit identity still requires a UC `Bqdf`/`Stat` capture or explicit
math-library equivalence. No device operation occurred. Evidence:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260914T031459-0700-native-slot-builder/`.

## 2026-09-14 controlling writer correction: native version 2, not tagged JSON

The exact UC 4.7.2 caller context identifies its tagged `MemP/Stat` emission as
a 500 ms settled-state synchronizer. The explicit Store action serializes the
selected library item and sends `MemP/PrsM`; it is not the four-slot writer.
Firmware 1.28 stores incoming `Stat` payload bytes unchanged and presents them
unchanged to `0x531e0`, whose first read requires little-endian u32 `2`.

The Host therefore no longer sends tagged JSON/scene archives as device slot
bodies. `save_uc_device_slot()` and the JSON registry convenience path fail
before transport. `save_device_slot()` accepts only a fully validated native
version-2 record and uses the existing exact `MemP/Stat` fragmentation. The two
native live trials above show that this exact transport plus selector recall is
still not sufficient to apply DSP state; current native slot bodies also remain
unreadable.

## 2026-09-14 controlling live inactive-slot result: selection works, body does not

An authorized no-playback A/B/A test at 96 kHz wrote and selected a flat
record, a +15 dB/1 kHz high-shelf record, and the flat record again in inactive
Input-1 slot 1. The complete original `B A S E` body was Host-known and
restorable. Readable slot selection reached 1 every time, but simultaneous
JaSt Input-1/Main meter ratios changed only +0.143 dB for the boosted record;
flat-state drift was -0.085 dB. Classification:
`NO_SAVED_BODY_RECALL_EFFECT_DETECTED`.

This is a clean negative for the current tagged `SetP | Appl(0) | MemP |
Stat(1) | record` writer/framing: the selector operates, but the record body
sent by this Host did not become active. It does not refute the static firmware
trace of the native storage path and does not prove that native device preset
storage is absent. The original body was resent, with zero replies just as in
its earlier `WRITE_SENT_UNVERIFIED` registry entry; the registry remained
read-only. Readable state restored exactly, and routes, DSP Amount, and
`processingChannel` were untouched. Controlling report:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260913T222036-0700-autonomous-host-acceptance/inactive-slot-recall-live-attempt-1/live-report.json`,
SHA-256 `8db972783d0ce15d799c1fc8b1f6ff7422faee4f13c8fefdac8b962822363299`.

## 2026-09-14 controlling live Voice FX result: Transformer is active

A no-playback live test at 96 kHz used Input 1 preamp self-noise and
simultaneous JaSt Input-1/Main/Mix-A/Mix-B meters. Main and headphone output
controls were held at 0.01. The ordinary Channel 1 noise gate was temporarily
bypassed so its -48.72 dB threshold could not suppress the source; this is
distinct from block 201's internal processing gate.

The gain control passed: raising Input 1 from 30 to 60 dB moved its meter by
26.986 dB and all three bus meters by 26.248 dB. Transformer on then moved the
normalized bus level by **-4.024 dB** relative to its off-before/off-after
midpoint. Its off drift was 0.045 dB. This is direct functional evidence that
block-201 model 0 processes audio in the device's current armed state; it
supersedes any blanket claim that block 201 is inert.

The same off/on/off sweep found no resolved level response for models 1 through
5: De-Tuner -0.015 dB, Vocoder +0.009 dB, Ring Modulator -0.186 dB, Filters
-0.032 dB, and Delay -0.008 dB. Those are not general inaudibility findings.
The observation is a scalar broadband-energy meter, which is weak for
energy-preserving pitch and delay processing, and the run did not independently
prove each selector transition. Those models remain unproved.

The result is scoped to direct model-select/parameter writes in the state found
on 2026-09-14. It does not identify which prior action armed the firmware gate,
prove cold-start activation, device-slot recall, or simultaneous processing of
both structural lanes. Restoration was clean: readable flags/gains/outputs and
the 0/1 processing permutation returned exactly; the complete Host-known
Transformer-off and Channel 1 gate states were resent. Mixer routes, preset
slots, DSP Amount, and `processingChannel` were untouched. Controlling report:
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260913T222036-0700-autonomous-host-acceptance/voicefx-meter-live-attempt-3/live-report.json`,
SHA-256 `0436e1da0a3fa9e3c83a7988f0d7b90507aaeae998403e8e7d36c0ac4578002e`.

## 2026-09-13 controlling Host correction: global FX, no owner mutation

**Historical, superseded by the 2026-09-20 Voice FX correction above.** The
structural two-lane finding remains valid, but it does not remove UC's explicit
one-input-at-a-time assignment step.

The stock UC `processingChannel == 0` conditional arbitrates ownership in its
host component tree. It is not a second block-201 instance and is not proof of
a one-lane DSP. The firmware topology is authoritative: block 201 is one shared
settings object configured with two input/output lanes, and model 0 binds its
private core at lane indexes 0 and 1. The Linux Host therefore configures one
global FX state for both structural lanes and no longer changes
`processingChannel` during ordinary FX edits, preset loads, or reconnect.
That parameter remains a separate, permutation-sensitive routing API.

This does not promote topology to audible acceptance. The later framing trace
at §13s supersedes the older claim that tagged device-slot recall opens the
block-201 gate: the archive reaches `0x531e0` with `0x700b697b` where the native
loader requires first word `2`. A physical preset recall is now reported only
as Fat Channel selection. For a Host-known record, the Host sends its global FX
intent once after recall; neither transport success nor recall proves FX
activation or simultaneous audibility.

## 2026-09-08 controlling correction: wire 4 is one processing scalar

`'Para'/4` and `'Pari'/4` are two encodings of the same per-channel firmware
state, `input1FxMix` / `input2FxMix`. The float route preserves 0.0–1.0. The
integer route converts false/true to 0.0/1.0 and dispatches to that same float
setter. It is therefore a Boolean alias, not an independent preset-enable
parameter.

The io24 JaSt serializer compares each `inputFxMix` to exact zero. It sets slot
42 bit 5 for Channel 1 or bit 6 for Channel 2 only when the scalar is zero.
Those bits report bypass/nonzero; they do not reveal the exact positive mix.
Live isolation confirmed that writing mix 0 bypasses the channel's Fat Channel,
including its compressor, and Boolean enable=true restores full mix 1.0.

Consequences for the Host: expose one processing/effects mix per channel; never
persist or replay a separate enable value; and never infer an exact nonzero
percentage from the JaSt flag. Later historical sections that call this an
independent reverb send or preset-enable state are superseded by this finding.

## 2026-09-06 controlling correction: three independent layers

The exact host branch confirms the layered model and corrects the mapping used
by two live runners:

```text
processing mix Boolean alias: Pari/4, physical channel index 0 or 1
preset slot:     Pari/16, physical channel index 0 or 1
VoiceFX owner:   processingChannel (parameter 149)
VoiceFX model:   fxmodel (parameter 450)
VoiceFX enable:  voicefx.on inside the selected model state
```

For each UC channel object, the initial apply at `0x180040729` and change
refresh at `0x18004193f` evaluate `processingChannel == 0` before calling
ownership switch `0x18003e6c0`. The owning object activates `voicefxopt/fxmodel`
and selects its current model. The non-owner deactivates it and selects host
sentinel `-1`. Parameter-149 changes reach handler `0x18004145e`. This is the
actual `if assigned, select the VoiceFX model for this channel` path; the
separate `voicefx.on` value determines whether that selected model processes.

Critically, assignment does not renumber preset controls. With VoiceFX assigned
to Input 2, `set_preset_enabled(2, ...)` and `set_preset_slot(2, ...)` still
operate the physical Channel-2 button/slot layer. The two earlier slot-3 live
attempts instead called those methods with channel 1. Their own readback shows
Channel 1's selector/enable changed and Channel 2 was not recalled. Both are
classified `INVALID_PHYSICAL_CHANNEL_CONTROL_MAPPING`; their dry listening is
not a Channel-2 VoiceFX result.

The Linux host now mirrors UC's order for an explicit VoiceFX preset load:

```text
set_voicefx_channel(physical_input)
select voicefx.__classid / parameter 450 model
apply voicefx.on and that model's fields
```

The corrected stage keeps every preset bypass/slot/enable call on physical
Channel 2, writes only global `Stat(3)`, and uses a factory-derived Reverb target
with private Wet/Dry raised from 0.295 to 1.0. It is hardware-free verified and
has not yet been executed live in that corrected form. Channel 1's selector was
restored from 3 to 0 and left bypassed; no Channel-1 preset record body was
written by either the faulty attempts or the restoration.

Boundary: this resolves host routing and invalidates the earlier listening
classification. Device-resident commit, corrected Channel-2 recall, cold-boot
survival, and audible VoiceFX remain unproved and require a new exact live
authorization.

## 2026-09-06 controlling Hot Key and VoiceFX model-selection result

The previously unexamined Hot Key is a real state transition, but it is not a
seventh device preset and it is not another effect instance. In the exact UC
4.7.2 host, `presetHotKey` is parameter 151. Its on-path selects a cached
complete-channel snapshot at channel-object offset `+0x180`; its off-path
selects the active normal slot through parameter 150. Both are fed through the
same complete-channel deserializer and post-apply callback. Channel load/save
uses a nested `hotkey` record, and an active Hot Key snapshot is captured with
the same serializer used by `StorePreset`.

The active VoiceFX algorithm is likewise explicit host state, not something UC
discovers by reading the DSP. A saved channel contains `voicefx.__classid`, the
separate storable `voicefx.on` value, and only the selected mutable model
child's parameters. On restore, UC maps the class ID to model `-1` or `0..5`,
creates/selects that model, sends
`SetP | block 201 | index 0 | VoFx | size 16 | u32 model`, installs the mutable
child, and only then applies its parameter state. Parameter 450 is the
`voicefxopt/fxmodel` host selector that drives this transition. It is not a
readable per-channel device register. Model 0 is Transformer/Doubler; `-1` is
the no-model state.

Consequences for the Linux host:

- saving a known state must preserve class/model ID, `voicefx.on`, and the
  selected model's fields;
- applying it must select the model before sending that model's state;
- unknown attached-device VoiceFX state cannot be reconstructed by block-201
  readback, so it requires an explicit selection or a complete known snapshot;
- the earlier Channel-2 slot-3 stage omitted a direct VoiceFX parameter write,
  so its dry closed-host observation did not reproduce this transition.

UC slot objects for either channel may serialize different VoiceFX choices, but
recalling any one configures the same fixed block-201 object. Parameter 450 is a
selection layer over that singleton, not evidence of a second instance.
Different simultaneous algorithms therefore require a second model/parent
object plus index-aware firmware dispatch. That custom-firmware work is now
explicitly deferred until after the first GitHub publication. The current lane
is stock-compatible host completion only.

The singleton has two structural audio lanes and model 0 binds private-reverb
lane indexes 0 and 1. This proves topology, not that stock routing enables both
lanes concurrently. Shared block-202 reverb is separately proven to accept
independent sends from both channels. Exact hash-pinned evidence and regression
tests are in
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260906T090317-0700-hotkey-voicefx-state-route/`.

Boundary: this result proves the static UC state route only. Runtime event
propagation, device-resident commit, standalone recall, simultaneous block-201
activation, and audibility remain unobserved.

## 2026-09-06 final stock-host materialization stage

The Linux host now implements the complete assignment-before-model-before-state
transition without inventing a Hot Key wire command. `voicefx_preset_call()`
maps the six exact UC class IDs to each model's exact storable field names and
rejects incomplete or unknown state before any Fat Channel mutation. An
explicit `with_fx=True`, CLI `--voicefx`, or GTK `Load + Voice FX` load first
calls `set_voicefx_channel()` for the requested physical input. The existing
source-bound builders then emit block-201 `VoFx` selection before configuring
only that selected model. The ordinary factory-preset path remains conservative
and omits these writes unless explicitly requested.

The corrected final stock discriminator is
`re/cp34_channel2_voicefx_materialization_stage.py`. It wraps the already
defined Channel-2 runner and inserts the pinned model-0,
two-filter, `godv` sequence immediately before the unchanged complete
`MemP/Stat(3)` write. A failure in that direct component apply occurs before
the slot writer. The script is hardware-free by default, pins serial and
firmware, retains the greater-than-10-percent battery floor and exact overwrite
acknowledgement, and never targets slot 0. It now requires VoiceFX already
assigned to Input 2, reasserts that same value, and sends all preset-control
operations to physical Channel 2.

An earlier live invocation sent the model/state and `Stat(3)` record but used
physical preset channel 1; it is invalid as a Channel-2 recall test. The
corrected dry plan returns `OFFLINE_READY` and its focused contracts enforce
physical channel 2. This remains the last stock-firmware test: an identifiable
saved effect after full computer disconnection and non-computer-powered
Channel-2 cold boot passes; a repeatable dry result from the corrected sequence
is the terminal stock limitation. Custom firmware is held until after the
initial GitHub publication.

## 2026-09-06 assignment side effect and corrected interpretation

The first authorized Reverb stage stopped before its record write after
`Assign Voice FX = Ch2` exchanged the two exposed active-slot values. Firmware
1.28 makes that side effect explicit: code at `0x2e77a..0x2e7bb` reads both
values and replays `Pari/16` for indexes 0 and 1 in exchanged order. The
66-byte slice SHA-256 is
`4fed3b4674f89b8a94498cf064f9e38fc8f5336da454bff5aeb399b9c4ae1093`.

What the exchange does *not* do is redefine the meaning of the `Pari/4` and
`Pari/16` target indexes. Those remain physical front-panel Channel 1 and
Channel 2. The later runners incorrectly used index 0 because it was the
VoiceFX-owning processing object. Their `Stat(3)` transports remain observed,
but the subsequent recall/enable was on physical Channel 1. The corrected
runner accepts a retained `(processingChannel 1,0)` assignment, normalizes the
physical Channel-2 selector with channel/index 1, and then recalls/enables slot
3 with that same physical index. It never writes `Stat(0)`.

## 2026-09-06 controlling Channel-2 Voice FX assignment correction

UC does not lock Voice FX storage to Channel 1. Its exact settings UI exposes
`Assign Voice FX` as two radio values on
`line/ch1/processingChannel`: `Ch1=0`, `Ch2=1`. The parameter is storable, and
the big preset-selector knob changes presentation according to that assignment.
Stock operation still uses one shared Voice FX engine on one assigned channel
at a time.

UC's exact settled-state synchronizer emits a complete scene-shaped tagged
record through `SetP | Appl(0) | MemP | Stat(slot)`; its physical record starts
with `0x7b` (`{`). Retained UC scenes independently have four `presets.slots`
objects and every one contains a `voicefx` member, including slots 2/3 for
Channel 2. Therefore the four version-2 records embedded in firmware are
startup/default implementation data, not the host wire schema or a legal
content ceiling:

```text
native slots:  0x69230, 0x69670, 0x69ab0, 0x69eb4
lengths:       0x440,   0x440,   0x404,   0x404
native end:    0x6a2b8
library start: 0x6a2b8  ('{' / Broadcast)
```

Those defaults begin with little-endian u32 `2` and contain length-delimited
four-byte-keyed chunks. Slots 0/1 happen to include component 201; slots 2/3 do
not. Promoting that observed default shape into a rule that Channel-2 saves
cannot contain Voice FX was the error.

The prior Channel-2 Slap Echo trial used a correctly encoded tagged
library/settled-state record, not a firmware-native slot body. It also left
`processingChannel` at its baseline `Ch1` assignment. Later attempts corrected
that assignment and physical selector, but the tagged body still did not become
an effective saved preset. The historical plan below is retained only as the
superseded test record:

```text
require and reassert line/ch1/processingChannel = 1  (Assign Voice FX = Ch2)
bypass physical Channel 2; select Channel-2 slot 2 if normalization is needed
select VoiceFX model 0, then apply voicefx.on/lows/width/mix
write the factory-derived 100%-wet Reverb record to Stat(3)
select Stat(3) and enable it through physical preset-control Channel 2
disconnect, cold boot from non-computer USB power, and listen on Input 2
```

The factory-derived maximum-wet record is 842 bytes, SHA-256
`191a5048c4995355932609e9d5ae225f37c50d8f112966f685a11b3f2a08e79c`;
its one-fragment frame SHA-256 is
`d8ea35050b33484444e48c3d54cf1716bce35265a0b2b92f26939ea7bdf92c1f`.
It selects Voice FX model 0 with `on=1`, `lows=0.06`, `width=0.405`, and
`mix=1.0`. Global slot 3 belongs to Channel 2; slot 0 is not targeted.

Model 0 contains a private reverb-core object distinct from shared block 202.
The core binds indexes 0 and 1, so simultaneous two-input private reverb is a
real implementation possibility, not a second stored Voice FX instance. Static
structure does not establish that stock routing opens both lanes concurrently;
that one gate is now the live question. The two lanes necessarily share the one
model-0 `on/lows/width/mix` setting set.

The first sequence stopped before its record write. The next two attempts sent
their transports but recalled through physical Channel 1; both are invalid as
Channel-2 checks. The subsequently corrected run sent the four model/state
messages and maximum-wet `Stat(3)` record, then exposed physical Channel 2 on
global slot 3 while preserving Channel 1. After unplugging and reconnecting the
interface, the user noticed no audible change. Thus the stock sequence failed
the standalone audible acceptance condition. No readable write reply exists,
so the observation does not distinguish commit, nonvolatile persistence,
parsing, model activation, processing-gate restoration, or recall as the
missing internal boundary.

## 2026-09-05 historical structured-state record-prefix gate

The normal indexed structured-state path has an earlier source-bound gate.
Method `0x531e0` constructs a wrapper at `sp+0x60`, stores the caller's record
stream at wrapper offset `+0x8`, and installs specialized vtable `0x66b54`.
That vtable's first slot targets `0x2cd20`, which forwards a read through the
underlying stream's vtable slot `+0x8`. Reader `0x2ce10` requests exactly four
bytes into `sp+0x48`. The wrapper byte-reverse flag is zero, so the reader's
`rev` path is skipped.

```text
0x5320e  ldr.w fp, [sp, #0x48]   ; native four-byte value
0x53212  cmp.w fp, #2             ; required entry value
0x53216  beq 0x53222              ; accepted path toward selection
0x53218  mov sl, r5               ; otherwise return false
```

The retained factory Slap Echo archive at raw `0x6b1c8` begins with bytes
`7b690b70`. The pure `MemP/Stat` encoder produces the same four-byte prefix.
Read natively without reversal, that is `0x700b697b`, not `2`. The controlling
result is
`STRUCTURED_STATE_PREFIX_GATE_SOURCE_BOUND_RAW_FACTORY_ARCHIVE_NOT_DIRECTLY_ELIGIBLE`.

This does **not** show that physical storage failed or that the stored record
differs. It shows only that the raw archive cannot be handed to `0x531e0`
unchanged at byte zero and pass. The bounded evidence does not yet identify
whether the storage/recall path strips framing, advances a cursor, prepends a
type word, or performs another transformation. The actual stream position,
physical commit, stored-record equality, selected component index, Voice FX
activation, and audible processing remain unresolved.

The focused contract passed 1/1 after an observed RED, the firmware-static
module passed 45/45, and the complete hardware-free suite passed 224 tests with
one expected skip in 233.126 seconds. No image or live operation was performed.
Evidence is under
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260905T172026-0700-structured-state-record-prefix-gate/`.

## 2026-09-04 controlling linked gate-transition audio result

The missing-stimulus ambiguity in the first linked Channel-2 slot-3 trial is
now resolved. One separately authorized capture kept gain, routing, output, and
preset content unchanged while it performed this bounded sequence on the test
unit:

```text
Channel 2 slot 3 -> 2 while unlinked
stereo link on
Channel 2 slot 2 -> 3 while linked
exact factory Slap Echo gate on
exact factory Slap Echo gate off
exact factory gate-on restoration sent
stereo link off
```

The temporary gate writes used the recovered factory values exactly:
threshold -47.5 dB, range -60 dB, attack 0.025 s, release 0.7 s, key filter
730.000244 Hz, expander on, key listen off, and 48 kHz. The gate is not readable
through `JaSt`; restoration is therefore an exact-write result, not readback
proof. Independent postflight did prove flags `0x1a1`, link off, Channel 1 slot
0 disabled, and Channel 2 slot 3 enabled with two identical 2040-byte replies.

The 14-second, six-channel S32_LE/48 kHz capture has SHA-256
`9f2307056e44b909668deb4226ec2b191d1e23ade12d641996fc9669e4beafda`.
All four monitor channels changed by approximately 48.142 dB from the gate-on
window to the gate-off window. That is the required positive control: these
capture taps are downstream of and observably affected by the gate. In the
gate-off window they each fit Input 2 with direct coefficient approximately
`0.70794561` and model correlation approximately `0.99999542`. The fitted
150 ms/direct ratio was only approximately `0.00001426`; the predeclared repeat
threshold was `0.05`.

The controlling classification is `NO_REPEAT_DOWNSTREAM_GATE_OFF`. The linked
recalled path did not produce the factory Slap Echo repeat even after its gate
was opened. This is strong audible-path waveform evidence, superseding
`UNPROVED_NO_ABOVE_GATE_STIMULUS`. It does not establish physical `Stat`
storage, stored-record equality, or which record-driven selection or activation
condition remains absent. No further live action was taken. Evidence is under
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260904T025429-0700-gate-transition-probe/`.

## 2026-09-04 controlling Stat channel-pair routing correction

Native `Stat` recall does not unconditionally dispatch the Voice FX state
interface. The live dispatcher at `0x420bc` first tests device flags bit 12,
the established stereo-link flag:

```text
0x42248  flags address = device + 0x5e58
0x4224c  load flags
0x4224e  flags << 19                 ; original bit 12 becomes N
0x42250  bpl 0x424d6                ; bit 12 clear: unlinked path
```

On the unlinked path, `0x424d6` loads the selected `Stat` slot, `0x424e2`
sets selector argument `r3=0`, and `0x424ea` collapses valid slots with
`slot >> 1`. Slots 0/1 therefore enter `0x531e0` with `r1=0`; slots 2/3 enter
with `r1=1`. The callee captures `r1` at `0x531e8` and the nonzero case branches
at `0x53224` to the alternate table path at `0x5338e`. Its equal dispatch uses
table slot 2 at `device+0x5e88`, not Voice FX slot 1 at `device+0x5e80`.

Slot 2 is no longer dynamically unidentified. Base initialization constructs
two identical channel-strip owners at `device+0x43a0` and `device+0x508c` with
a `0xcec` stride. The second owner's state interface is exactly
`device+0x5090`, the object installed in table slot 2. Its vtable method points
through adjustor `0x52aec` to state apply `0x52978`, whose five selectable keys
are `filt`, `gate`, `comp`, `eq  ` and `lim `. Component 201/Voice FX is absent.
Thus an unlinked Channel-2 slot-2/3 recall applies the second channel strip and
does not directly select Voice FX.

When bit 12 is set, the dispatcher falls through instead. A slot greater than
zero passes the gate at `0x42254..0x42258` and, on the existing `r6=0` normal
continuation, runs two `+0x38` state calls:

```text
0x42266  r1=0, r3=1  -> normal indexed component selector
0x42280  r1=1, r3=0  -> alternate slot 2 / second channel strip
```

The normal indexed table has a source-bound Voice FX entry at slot 1, but its
selected index remains record-driven and unresolved. Stereo link therefore
makes Voice FX eligible; it does not by itself prove selection or audible
processing.

The bounded device read during the 2026-09-04 continuation returned flags
`0x1a1` (bit 12 clear), Channel 1 slot 0 disabled, and Channel 2 slot 3 enabled,
with two identical 2040-byte `JaSt` replies. This places both preceding dry
Channel-2 slot-3 observations on the source-bound unlinked/channel-strip route.
It explains their lack of Voice FX without distinguishing accepted transport
from physical record commit: full `Stat` content still cannot be read back.

This section supersedes any blanket statement below that native `Stat` recall
always reaches block 201. Historical sections remain evidence for possible
component routes, not proof that a particular channel/slot selected them. A
linked trial also changes the channel relationship and can invoke the normal
component table, so it requires an explicit lift of any Channel-1-preservation
boundary and a separately accepted restoration limitation.

That lift was granted for one bounded live trial later on 2026-09-04. The
device confirmed Channel 2 slot `3 -> 2`, link on, Channel 2 slot `2 -> 3`, and
link off; the final readable Channel-1 guard matched baseline. Capture channels
3-6 changed to a correlation-`0.99999668`, -3 dB copy of Input 2 while linked,
so the live link state materially changed the captured signal topology. A
150 ms delayed component was absent, but Input 2 contained only -97.15 dBFS RMS
/ -83.66 dBFS peak noise, 36.16 dB below the factory Slap Echo record's enabled
-47.5 dBFS gate. The live result is consequently
`UNPROVED_NO_ABOVE_GATE_STIMULUS`, not evidence that linked recall remained dry
for a valid source.

## 2026-08-31 controlling Doubler/reverb preset correction

Transformer/Doubler does contain reverb internally, but it does **not** enable
or route through shared reverb block 202. Firmware model 0 owns a separate
instance of the same reverb-core implementation.

The source-bound object construction is:

```text
device+0x11e0  block 201 Voice FX
  +0x10        selected model object (model 0 at device+0x11f0)
  +0x384       Doubler-private reverb core (device+0x1574)

device+0x3a70  shared block 202 reverb
  +0x10        shared reverb core (device+0x3a80)
```

Device constructor `0x5d9a8` constructs block 201 through `0x591bc` and block
202 through `0x5b258`. Model factory `0x5f254` selects model 0 and constructs
it through `0x5e270`. Block 202 calls core constructor `0x5af14` at `0x5b274`
with `this+0x10`; Doubler calls that same constructor at `0x5e37c` with
`model+0x384`. Likewise, block 202 calls core configurator `0x5a6a8` at
`0x5ade6`, while Doubler calls the same configurator at `0x5ae0e` on its
private member. Model-0 runtime configuration `0x5bfe0` reaches the private
path at `0x5c06c`. The implementation is shared; the state object and signal
path are not.

The singleton is nevertheless a **two-lane processor**. This is the distinction
the earlier resolver-only analysis missed:

* audio-root code loads both physical-input mapping words at `device+0x5d74`
  and `device+0x5d78` (`0x3f8a4` / `0x3f8aa`), then uses the same `0xcec`
  per-channel stride to select two processing objects (`mla` at `0x3f8bc` and
  `0x3f8c8`);
* the block-201 `Setu` construction sets `r2=2` at `0x3dbf4`, stores that value
  into both adjacent setup counts at `0x3dc10`, addresses the singleton at
  `device+0x11e0`, and dispatches setup through `0x553d4` at `0x3dc1c`;
* Doubler private-core setup passes the loop index in `r3` at `0x5ae90`, calls
  the indexed lane helper `0x56cc8` at `0x5aeac`, changes the index from 0 to 1
  at `0x5aeb2`, and repeats the binding.

Therefore block 201/model 0 is **one shared settings instance over two hardware
input lanes**. The topology can carry the same private-reverb state on both
lanes, but stock simultaneous routing remains unproved; it must not be reported
as an observed capability. Each channel's serialized slot object may carry a
different `voicefx` mapping, but recalling either one replaces the singleton
model/state. It does not create two instances or two independently
parameterized `on/lows/width/mix` states.

The JSON is the preset contract. In the 41 pinned UC 4.7.2 factory records,
the sole record titled `Reverb` is record 30 and contains:

```json
"voicefx": {
  "__classid": "{66A10093-D461-4CAC-A80C-91F6A1BB37E5}",
  "on": 1,
  "lows": 0.06,
  "width": 0.405,
  "mix": 0.295
}
```

That class maps to UC's `VoiceOfGod`/Doubler model. No factory preset record
contains `reverb`, `vrvb`, `FXA`, `size`, `predelay`, or `hp_freq`. Those last
fields belong to the separate `VocalReverb`/block-202 component. Therefore a
device-slot `Stat` save of this effect must preserve the Voice FX class and
`on/lows/width/mix`; it must **not** synthesize a block-202 send or reverb
record. Sample rate is runtime core configuration, not a JSON/`Stat` field, so
96 kHz adds no preset field or alternate save representation. Direct `Bqdf`
coefficient writes are part of the separate live-blob path, not native preset
storage.

This supersedes older sections that describe block 201 as requiring the same
send/return path as reverb. It does not turn the static result into proof of
physical nonvolatile commit, front-panel recall, power-cycle survival, or
audible output. Executable evidence is under
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260831T040258-0700-doubler-private-reverb/`.

## 2026-09-01 deferred custom-firmware per-lane wet/bypass contract

This is an **offline design contract**, not evidence of a patched or accepted
firmware image. Stock firmware presents model 0 as one shared state object;
therefore it cannot interpret per-lane fields without a custom parser and a
deliberate DSP hook after the respective private-core lane. Do not reinterpret
the existing private-core coefficient arrays as wet controls without that
implementation evidence.

This design and the newer, more general independent-model-instance design are
deferred until after the initial GitHub publication. They are not part of the
current stock-host completion lane. A genuinely independent design would add a
second block-201 parent/model object, make dispatch instance-aware, bind one
instance to each physical-input lane, preserve state separately, and first
prove code, RAM, and DSP-cycle headroom.

The proposed backward-compatible extension of the model-0 `voicefx` JSON is:

```json
"voicefx": {
  "__classid": "{66A10093-D461-4CAC-A80C-91F6A1BB37E5}",
  "on": 1,
  "lows": 0.06,
  "width": 0.405,
  "mix": 0.295,
  "wet_ch1": 1.0,
  "wet_ch2": 1.0,
  "bypass_ch1": false,
  "bypass_ch2": false
}
```

`on`, `lows`, `width`, and `mix` retain their current shared meanings. The new
wet values are clamped to `[0.0, 1.0]`; omitted wet values default to `1.0` and
omitted bypass values default to `false`. Thus a pre-extension record takes the
same effective lane-control path as stock firmware. `voicefx.on` remains the
global model enable; it is not replaced by either lane bypass.

The historical custom-firmware design assumed complete tagged `Stat` bytes;
the controlling writer correction at the top of this document supersedes that
assumption. Any future patched structured-state apply path must explicitly
recognize these new fields and set
the two runtime lane controls. Only under an explicit custom-firmware
capability may the host serialize them; normal saves retain the legacy
four-field `voicefx` object because stock acceptance of unknown keys is not
established. For a paired channel-1/channel-2 custom save, the host prepares an
identical complete extended `voicefx` object for one selected slot from 0/1 and
one selected slot from 2/3. The two `Stat` transfers are sequential, so a
second-write failure reports the completed slot(s) and failed slot rather than
claiming an atomic update. Either successful recall then establishes the same
complete two-lane controls; conflicting records follow ordinary
last-recalled-state behavior and do not create independent model instances.

Existing names that sound adjacent are not substitutes: `dspAmount` and
`bypassDSP` are shadow-only host-schema fields without an io24 device binding;
`FXA` is the shared block-202 reverb send; and `processingChannel` permutes
input sources. None is a per-lane Doubler wet/bypass control.

### Explicit local complete-channel bases

`io24.build_paired_doubler_private_reverb_frames_from_local_bases(...)` is the
host-only preservation route for two caller-retained complete JSON channel
records. For example:

```python
frames = io24.build_paired_doubler_private_reverb_frames_from_local_bases(
    0, "channel-1-base.json", 2, "channel-2-base.json",
    enabled=True, lows=0.06, width=0.405, mix=0.295,
)
```

The first and second paths are independently validated complete `Stat` bases;
only their `voicefx` objects are replaced. Consequently each retained Fat
Channel strip stays distinct while the paired Voice FX state is serialized as
required. The builder performs no interface read, selected-slot inference,
merge, factory fallback, or write; it returns local frames only. No CLI or GTK
file picker exposes this route yet, and the caller is responsible for retaining
complete valid records that actually correspond to the desired strips.

The former private-core hook claim was wrong and is now withdrawn. The reset
copy table maps raw `0x0fd8..0x205d0` into ITCM
`0x000002c0..0x0001f8b8`. Model-0 runtime configuration copies the `Setu`
frame quantum from config `+0x0c` to model `+0x338`; raw `0x1e1a8` uses it as
a buffer stride. It is not a lane selector. The actual user WetDry value is
model `+0x350`, loaded into `s17` at raw `0x1e1cc`. The `s0` value loaded from
model `+0x358` at raw `0x1e2be` is instead the Width-derived private-core
scalar `0.2 * (1 + width)`. A real per-lane identity and post-core blend hook
remain unresolved, so the proposed `effective_mix` patch is not source-bound.

No custom firmware image is shipped by this repository. No safe existing
extension storage or executable placement has been proved: the nearby
per-lane 0.5 arrays are live graph coefficients, and models 0–5 remain
factory-dispatchable. The original complete vendor firmware package is required
as the recovery source before any future device action. The offline package
tool accepts only the pinned UC 4.7.2 io24 package (target, vector, full length
and SHA-256), and exposes no live defaults, status, detach, or write command.
Producing a package, staging or writing DFU, testing a restore, and evaluating
audible or standalone persistence are all separate, explicit hardware-
authorization gates.

## 2026-08-31 controlling preset/Voice FX correction

Firmware 1.28's native `Stat` application path reaches the block-201
structured-state loader and establishes all three conditions checked by the
processing gate. This supersedes the vtable ownership and setter-census claims
in §§13c, 13d, 13l, 13m, 13s, 13t, 13u, and 13v where they are used to infer
that preset recall lacks a firmware gate-opening path.

The source-bound route is:

```text
Stat record callback 0x20754
  -> router singleton projection 0x34cb8 / 0x3cc08
  -> singleton record forwarder 0x3cc40
  -> live device dispatcher 0x420bc, Stat branch 0x42200
  -> structured-state apply 0x531e0
  -> component 201 state interface at device+0x11e4
  -> adjustor 0x555b8 -> state loader 0x5545c
```

Constructor `0x591bc` installs the primary vtable at file `0x10c6bc` and the
secondary state-interface vtable at `0x10c700`. The older analysis incorrectly
started the primary vtable at `0x10c6cc`, sixteen bytes late. Consequently,
`0x50af8` is primary slot `+0x10`, not slot `+0x00`; its primary work slot
`+0x34` is file word `0x10c6f0 -> 0x5f1f4`. Thunks `0x54ed8` and `0x555b8`
instead belong to the secondary state interface and adjust `this` by `-4`.

On a valid Voice FX component record, state loader `0x5545c` performs:

```text
0x554ba  [block+0x40] = 0
0x554bc  [block+0x38] = 0x0101  ; therefore byte +0x39 = 1
0x554c2  call 0x55388
0x553a8  [block+0x44] = 1       ; after valid model selection 0..5
```

Gate handler `0x50af8` requires `+0x44 != 0`, `+0x39 != 0`, and `+0x40 == 0`.
The native component-state load therefore supplies every gate condition before
it dispatches the selected model's nested state. The embedded active factory
records carry `voicefx.on=1`; the on/off intent remains inside the record, not
in a missing standalone host packet. This does not claim that `voicefx.on` is
copied directly into `+0x44`, nor does static evidence prove physical commit,
power-cycle survival, front-panel behavior, or audible output.

The §13s measurement remains valid for its exact unidentified recalled slot and
direct block-201 re-arm. It is not a general negative for a known active Voice
FX record applied through the decoded `MemP/Stat` path.

Executable source-bound evidence is sealed under
`.superpowers/sdd/2026-08-28-cp34-preset-effect-control/runs/20260831T032608-0700-voicefx-gate-recall/`.

**Status:** the control protocol is decoded and implemented. Verified working on
hardware from Linux, with no PreSonus software involved:

| | |
|---|---|
| **Preamp & routing** | gain, phantom, high-pass, mutes, channel link, monitor blend, headphone & main volume |
| **Dynamics** | gate, compressor (3 models), limiter — with measured transfer curves |
| **Effects** | shared reverb measured audible; all six VoiceFX models objectively verified through physical Input 1. The separate Host spring passes its hardware-free DSP/route contracts; live Main-output acceptance is pending. |
| **Mixer** | per-source sends/assigns/mute/solo and bus master/mute for Main / Mix A / Mix B, with the gain law measured exact. The UC-only fields without proved io24 representations are listed in the 2026-09-20 parity boundary above. |
| **Metering** | levels and per-stage gain reduction, streamed at 10 Hz |
| **Compatibility** | a UCNET shim so existing PreSonus plugins drive the device unmodified |

See [Safety](#8-safety) for the rules that apply.

Device: `194f:0422` (bootloader: `194f:0405`), firmware `bcdDevice 1.28`,
internal codename **"Jackson"**.

---

## 1. Why this was needed

The io24 is class-compliant for audio, so playback and capture work on Linux out
of the box. Everything else — preamp gain, mixer, EQ, compressor, reverb, routing,
loopback — lives in the device's DSP and is only reachable through PreSonus's
Universal Control, which ships for Windows and macOS only.

The obvious starting point, [oddbear/Revelator.io24.Api](https://github.com/oddbear/Revelator.io24.Api),
documents the **UCNET** protocol. But UCNET turns out to be the wrong layer: it is
spoken over **TCP to `127.0.0.1`**, to a Windows service
(`PreSonusHardwareAccessService.exe`) that acts as a USB↔TCP bridge. On Linux that
service does not exist, and the device does **not** speak UCNET over USB. Any
Linux driver has to *become* that service, which means speaking the device's real,
undocumented native protocol.

That protocol is what this document describes. As far as I can tell it has not
been publicly documented before.

---

## 2. USB topology

`lsusb -v -d 194f:0422` shows seven interfaces:

| Iface | Class | Role |
|-------|-------|------|
| 0, 3 | Audio Control | UAC control |
| 1, 2 | Audio Streaming (isochronous) | PCM in/out — works natively |
| 4 | MIDI Streaming (bulk `0x02`/`0x82`) | MIDI |
| **5** | **Vendor Specific (bulk `0x01`/`0x81`)** | **control channel — "Revelator IO 24 CTRL"** |
| 6 | DFU | firmware update — **do not touch** |

Interface 5 has two alternate settings: **alt 0 has no endpoints**; **alt 1**
exposes the bulk pair, 512-byte packets. The kernel binds `snd-usb-audio` to
interfaces 0–4 and leaves 5 unclaimed, so libusb can take it with no driver
detach.

Access without root, via udev:

```udev
# /etc/udev/rules.d/70-presonus-io24.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="194f", ATTR{idProduct}=="0422", MODE="0660", TAG+="uaccess"
```

---

## 3. Bringing up the control channel

Claim interface 5, select **alt setting 1**, then issue two read-only vendor
control transfers. These are `paesdk::BulkSubdeviceCmd::QueryProtocolVersion` and
`QueryMaxLength`:

| Request | Meaning | io24 response |
|---------|---------|---------------|
| `bmRequestType=0xC1, bRequest=0x00, wValue=0, wIndex=5, wLength=2` | protocol version | `01 00` → 1 |
| `bmRequestType=0xC1, bRequest=0x01, wValue=0, wIndex=5, wLength=4` | max command length | `0x800` = 2048 |
| `bmRequestType=0xC1, bRequest=0x01, wValue=1, wIndex=5, wLength=4` | max response length | `0x800` = 2048 |

There is **no vendor "enable" request** — a long detour early in this project
assumed one existed. The bulk pipe is simply opened.

---

## 4. Framing (paesdk bulk header)

Every bulk message carries an 8-byte header, then the payload:

```
off size field
0    2   u16 totalLength   = payloadLength + 8   (little-endian)
2    1   u8  protocolId    = 0x01
3    1   u8  messageCode   = 0x01 request / 0x81 response
4    1   u8  uniqueId      incrementing, wraps 1..255, never 0
5    1   u8  status        0 = OK
6    2   u16 reserved      0
8   ..   payload
```

Responses echo `uniqueId` and set `messageCode = 0x81`.

> **The pipe is strictly synchronous.** Send one command, read its matching
> response, *then* send the next. Pipelining two commands NAKs and jams the OUT
> endpoint (recoverable with `clear_halt`). This cost real debugging time.

---

## 5. The native payload — a FourCC TLV

All fields little-endian. FourCCs are C constants of the form
`'A'<<24 | 'B'<<16 | 'C'<<8 | 'D'`, stored little-endian — so **on the wire they
appear byte-reversed** (`'GetP'` → `50 74 65 47`).

### Request

```
off  size  field
0    4     u32 cmdTag       'GetP' = 0x47657450   |  'SetP' = 0x53657450
4    4     u32 blockFourCC  'Appl' = 0x4170706c   (the device/application block)
8    4     u32 blockIndex
12   ...   BLOB:
             +0x00  u32 blobTag
             +0x04  u32 blobSize   TOTAL blob bytes, including this 8-byte header
             +0x08  u32 index
             +0x0c  u32 paramId
             +0x10  ...  value / payload
total length = 12 + blobSize        (firmware rejects total <= 19)
```

### Response

```
0    4   u32 'Rply' = 0x52706c79
4    4   u32 blockFourCC   (echoed)
8    4   u32 blockIndex    (echoed)
12   ..  blob — the request blob is memcpy'd in, then the handler fills the rest
```

### Blob types are direction-specific

This is the single most confusing property of the protocol:

| Block | Direction | Accepted blob tags |
|-------|-----------|--------------------|
| `'Appl'` | **GetP** (read) | `'JaSt'` (0x4a615374), `'MemP'` (0x4d656d50) |
| `'Appl'` | **SetP** (write) | `'Para'` (f32), `'Pari'` (i32), `'FRst'`, **`'MemP'`** (synthesized comparison; see §13o) |

`'Para'` and `'Pari'` are **write-only**. A `GetP` carrying `'Para'` returns a
perfectly well-formed `'Rply'` — but its blob is just your own request echoed
back, value untouched. It looks exactly like a successful read returning `0.0`.
I lost time to this before reading the firmware's dispatcher.

---

## 6. Reading live state (works)

```
GetP / block 'Appl' / blockIndex 0 / blob 'JaSt', blobSize 0x7ec
```

Returns a 2048-byte reply. The state array is **little-endian f32** beginning at
payload offset `0x1c` — call that *slot 0*; slot *N* is at `0x1c + 4N`. 503 slots.

Smaller `blobSize` (e.g. `0x100`) returns a correspondingly shorter dump.

**Integer-valued parameters are stored as raw int bits inside that float array**,
so they read as denormals — e.g. `5.42e-43` is the integer `387`.

### Slot map — complete

Recovered by disassembling the firmware's `'JaSt'` serializer (io24 `vtbl[0x24]`
= `0x6005bcc8`, tail-calling the base serializer `0x6004df78`), then confirmed
against hardware. Only slots 0–50 are ever written; the reply builder zero-fills
the tail.

| Slot | Offset | Type | Meaning |
|------|--------|------|---------|
| 0–17 | `0x01c`–`0x060` | f32 | level meters, `memcpy` of an internal `'JaMD'` query to the DSP coprocessor. Slot 4 = input1 linear level, slot 6 = input2 (they feed the autogain sum-of-squares accumulator) |
| **18–35** | `0x064`–`0x0ac` | f32 | **gain-reduction meters** — 18 values, linear gain, `1.0` = no reduction |
| 38 | `0x0b4` | int | `input1ProcessingChannel + 3` |
| 39 | `0x0b8` | int | `input2ProcessingChannel + 3` |
| 40 | `0x0bc` | int 0–3 | `input1SlotIndex` |
| 41 | `0x0c0` | int 0–3 | `input2SlotIndex` |
| 42 | `0x0c4` | bitfield | flags |
| 43 | `0x0c8` | f32 0–1 | **`hpVolume`** (headphone) |
| 44 | `0x0cc` | f32 0–1 | **`mainVolume`** |
| 45 | `0x0d0` | f32 −1…1 | **`monitorMix`** (blend) |
| 46 | `0x0d4` | f32 0–60 | **`input1Gain`** — ch1 preamp gain, dB |
| 47 | `0x0d8` | f32 0–60 | **`input2Gain`** — ch2 preamp gain, dB |
| 48, 49 | — | — | not written |
| 50 | `0x0e4` | 2 bytes | byte0 = `input1PhantomPower`, byte1 = `input2PhantomPower` |
| 51+ | — | — | not written (zero-filled) |

Every hardware observation is reproduced by this map: slots 46/47 as the two
gains, 43/44/45 as the three 0–1 knobs, 40/41/42/50 as the button-driven ints,
and the meter region as the `'JaMD'` block.

> **Correction.** An earlier draft described slots 18–35 as "unity / enabled flags
> reading 1.0". They are **gain-reduction meters** — they read 1.0 only when
> nothing is reducing. Verified on hardware: engaging the gate on ch1 with
> `range = −30 dB` moved **slot 24** from `1.0000` to `0.0316`, exactly matching
> what the `'gate'` block's own `'Redu'` query reported at the same moment.

---

## 6a. The `SetP` parameter-ID space

**There are two different ID spaces, and conflating them is the single biggest
trap in this protocol.**

1. **Internal ids (0–42)** — the firmware's own parameter tables:
   `0x60086bc8` (23 records) and `0x60088c4c` (17 records), stride `0x54`.
   These are what `setParam()` linear-searches, and what the names
   (`input1Gain=13`, `hpVolume=10`, …) belong to. **They are not wire ids.**
2. **Wire paramIds** — what actually goes in the blob at `+0x0c`. A small, dense,
   hand-written switch space, translated to internal ids by a jump table.

### `'Para'` (f32) — handler `0x6004e360`, accepts wire ids **1–14 only**

| Wire id | `index` | Target | Range |
|---------|---------|--------|-------|
| 1 | ignored | `hpVolume` | 0–1 |
| 2 | ignored | `mainVolume` | 0–1 |
| **3** | **0/1** | **`input1Gain` / `input2Gain`** | **0–60 dB** |
| 4 | 0/1 | `input1FxMix` / `input2FxMix` | 0–1 |
| 10 | ignored | `monitorMix` | −1…1 |
| 14 | ignored | `outputDelay` | 0–0.5 |
| 5–9, 11–13 | — | **no-op** (jump-table byte `0x33` → epilogue) | |
| 0, ≥15 | — | **rejected** before the table | |

### `'Pari'` (i32) — accepts wire ids **4–19** (plus id 0 via the io24 override)

| Wire id | `index` | Target |
|---------|---------|--------|
| 0 | 0/1 | `input1PhantomPower` / `input2PhantomPower` |
| 5 | 0/1 | `input1HighPassFilter` / `input2HighPassFilter` |
| 6 | ignored | `hpOutputMute` |
| 7 | 0/1 | `input1Mute` / `input2Mute` |
| 8 | ignored | `muteMode` |
| 9 | ignored | `channelLink` |
| 12 | 0/1 | `input1/2ProcessingChannel` |
| 13 | ignored | `outputDelayBus` |
| 16 | 0/1 | `input1/2SlotIndex` |
| 17 | ignored | `presetMode` |
| 10, 14, 19 | — | no-op |
| 15 | ignored | **not a no-op** — sets `JaSt` slot 42 bit 13; purpose unknown (2026-07-31) |

#### Slot 42 is a button/state bitfield

Recovered by driving each control and diffing the whole state blob:

| bit | meaning | writable? |
|---|---|---|
| 1 | **MAIN OUT mute — the physical front-panel button** | **read-only**: no `'Pari'` id 5–19 moves it |
| 2 | software output mute (`'Pari'` id 6, a *different* control from the button) | yes |
| 3 / 4 | input 1 / input 2 mute (`'Pari'` id 7) | yes |
| 5 | **channel 1 preset DISABLED** — press-and-hold on the preset button latches it | read-only |
| 6 | **channel 2 preset DISABLED** | read-only |
| 12 | stereo link (`'Pari'` id 9) | yes |
| 13 | `'Pari'` id 15, purpose unknown | yes |

**Preset enable/disable IS reported; preset *selection* is not (2026-08-01).**
Established by logging **all 489 stable slots** — an earlier attempt watched only
seven hand-picked slots and would have missed this entirely — while every
front-panel button was pressed in labelled groups:

* **Only slot 42 bits 5 and 6 ever change.** Nothing else in the whole state blob
  moves for any button, so `back` and the short preset press are handled inside
  the device and are invisible to the host.
* A **short** preset press appears as a transient SET→CLEAR pair 0.4–0.5 s apart
  (press, then release). **Press-and-hold**, which deactivates that channel's
  preset function, **latches the bit SET**. With both channels' presets switched
  off the device read `0x1e1` and stayed there.
* So **bit 5 = channel 1 preset disabled, bit 6 = channel 2 preset disabled** —
  the enable/disable state Universal Control displays.
* **Slots 40/41 (`input1SlotIndex` / `input2SlotIndex`) never move for the preset
  button**, whether presets are enabled or disabled. They *are* readable and
  writable from the host (`'Pari'` wire id 16 drives slot 40, verified early in
  this project) — but the front panel does not touch them. The host can see
  *whether* presets are active, and can set a slot index itself, yet the device
  never reports which preset the user selected.

Two earlier claims here were wrong and are corrected above: these bits are not
"channel select", and they are not latching toggles in general — only the
press-and-hold latches.

So an application can *mirror* the front-panel mute but cannot press it, and the
low-pass/high-pass enable appears nowhere in the blob — it is genuinely
write-only and cannot be mirrored at all.

The blob's **`index` field is the channel selector** (0 = ch1, 1 = ch2), consumed
as `internal_id = local_array[index]`. Handlers targeting a single parameter
ignore it entirely. It is *not* the JaSt slot and *not* `blockIndex`.

Meters, clip indicators, `presetSelect`, `volumeKnob` and `encoderAssignment`
(internal ids 29–42) are **not writable from any wire id** — they are
device→host status only.

`setParam()` **clamps** every value to the record's min/max, and an id with no
descriptor is simply not found and silently does nothing.

### ALSA external-control bridge

`io24_alsa_ctl.c` is a client-side ALSA external-control plugin. It translates
ALSA integer elements into the daemon's newline-delimited JSON requests and
never opens the USB interface itself. The daemon remains the sole USB owner.

| ALSA control | Daemon field / setter | Range |
|---|---|---|
| `Main Volume` | `mainVolume` / `mainvol` | `0..100` ↔ `0..1` |
| `Headphone Volume` | `hpVolume` / `hpvol` | `0..100` ↔ `0..1` |
| `Monitor Blend` | `monitorMix` / `blend` | `0..100` ↔ `-1..1`; `50` is the midpoint |
| `Mic/Inst Capture Gain (dB)` | `input1Gain` / `gain`, channel 1 | `0..60` |
| `Headset Capture Gain (dB)` | `input2Gain` / `gain`, channel 2 | `0..60` |
| `Mic/Inst Capture Phantom` | `input1PhantomPower` / `phantom`, channel 1 | boolean |
| `Mic/Inst Capture Processing` | `flags` / `fxmix`, channel 1 | boolean |
| `Headset Capture Processing` | `flags` / `fxmix`, channel 2 | boolean |
| `Main Output Mute` | `flags` bit 1 | read-only |

For `fxmix`, flags bits 5 and 6 are the channel-disabled bits, so a clear bit
means processing is enabled. The physical Main Mute bit is the inverse: bit 1
set means muted. The plugin exposes no setter for that bit because the manual
and wire census do not provide a safe command for the front-panel button.
Every write is based on the daemon's returned state, not only on the requested
value; malformed, missing, or rejected state fails closed.

The plugin is intentionally absent from the default `ctl.!default` definition.
Use the named `io24` control explicitly, normally as `alsamixer -D io24`, so a
missing daemon or a stopped USB owner cannot silently redirect the desktop's
normal audio controls. Hardware validation of the read-only mute and any
additional writable controls remains separate from this software bridge.

---

## 7. How this was derived

Three independent sources had to agree before anything was trusted:

1. **Host driver (x64).** `hwaccess/dspusbdevice.dll` from the Universal Control
   installer handles the whole Revelator/DSP-USB family. It reaches USB through
   **WinUSB in userspace** (`WinUsb_ControlTransfer`, `WinUsb_WritePipe`, …),
   which is why the whole thing is reproducible with libusb. Key routines: the
   serializer at `0x180055340`, `'Para'` builder `0x180054200`, `'Pari'` builder
   `0x180054180`, `ExecuteCommand` `0x18007ded0`.

2. **Device firmware (Thumb-2).** The same DLL **embeds four complete Cortex-M
   firmware images** — one per product. The io24's is at file
   `0x216030..0x323a70`, load VA `0x60020000`. Disassembling its message
   dispatcher (`0x6004d50c`), GetP reply builder (`0x6004d566`) and blob
   dispatchers (`0x6004d9b8` SetP, `0x6004f1c8` GetP) gave the *device side* of
   the protocol — including the direction-specific blob rule above.

3. **The hardware itself.** Every conclusion was checked against the real io24.

Parameter metadata also exists statically: the driver contains **104-byte (0x68)
parameter descriptor records**, SDK-generic across PreSonus devices:

```
+0x00 u32 kind (0=bool 1=int 2=enum 3=float 4=string)
+0x04 u32 id
+0x08 char name[0x20]
+0x28 f32 min | +0x2c max | +0x30 mid | +0x34 default | +0x38 step | +0x3c flags
+0x40 ptr unit | +0x48 ptr taper | +0x50 ptr enum names | +0x58 long label | +0x60 short label
```

Units seen: `gain`, `percent`, `time`, `freq`, `freq/off`, `pan.100`,
`ratio/~/limit`. Tapers: `linear`, `log`, `exp`, `skew`, `linrev`, `gaterange`.
Extraction is retained privately as `re/io24_params.json` (note: that file was
produced with an earlier, stricter parser and is known to be incomplete — it drops
camelCase names, `kind=0` booleans and `id=0`).

The component tree is **not** a static table: it is built at runtime by four
per-model builder functions calling `addChild(parent, routeSegment, className,
(table,count) × 4)` at `0x180029e20`. Components nest by route segment
(`line/ch1/gate`), and there is **no numeric block id** in that structure — which
is exactly why the native protocol needed a different addressing story.

### Dead ends, recorded so nobody repeats them

- **Sending UCNET frames over USB.** Byte-perfect `UM`/`JM` subscribe frames, 18
  device-id values, both `from`/`to` orders — all acknowledged at transport level,
  all ignored. UCNET is spoken to the *Windows service*, never to the device.
- **The "ACI" firmware in `studio192device.dll`.** It looks like the jackpot — a
  full device-side command layer with `ACI_CmdSetDspParams` etc. It is not: the
  strings `ACI_InitUart`, `ACI_InitDmaChannels`, `ACI_CheckRxFrame` and
  `AUD_QueryFrameSizeFromDsp` show it is the **internal UART link between the FX3
  USB controller and the DSP** on a *different product* (Studio 192 / "Artemis").
  `dspusbdevice.dll` contains zero ACI references.
- **Blind opcode probing.** Bare single-byte payloads `0x00`–`0x1f` are all
  accepted with `status=0` and do nothing. The device silently ignores malformed
  commands, so "no error" carries no information.

---

## 8. Safety

### The bootloader incident

During this work, `SetP` / `'Appl'` / `'Para'` with `paramId = 13` and `46`
(values 30.0 / 45.0, index 0 and 1) produced **no reply and no state change**, and
the device then **rebooted into its bootloader** — re-enumerating as
`194f:0405 "Revelator IO 24 BOOTLOADER"` with the ALSA card gone. It recovered
completely on a power cycle. No firmware was flashed; the serial was unchanged.

I initially attributed the reboot to those writes. **The firmware disassembly
refutes that**, and the correction matters:

- `'Para'` wire id **13** hits jump-table byte `0x33` → the epilogue. It is
  **provably a no-op**.
- `'Para'` wire id **46** fails `cmp r1,#0xd` and is **rejected before the jump
  table**.
- Neither path reaches any reset, watchdog or DFU code. The only
  `"Revelator IO 24 DFU"` string is referenced solely from the USB descriptor
  blob, never from a parameter path.
- **`'FRst'` is not a destructive factory reset** — it is a *parameter defaults
  restore*, calling `setParam` with ids 10, 11, … and −30.0 dB.

So the trigger was **not** the parameter values. The likely culprit is
transport-level: those writes were part of a sequence that jammed the bulk OUT
pipe (unanswered commands, `clear_halt` recovery), and something in that state
upset the firmware. That remains an unresolved risk worth respecting — but the
"a bad paramId can brick it" model was wrong.

### Rules

1. **`SetP` is fire-and-forget** — no acknowledgement. Silence tells you nothing
   about whether a write was accepted, rejected, or ignored.
2. **The `JaSt` slot index is not the `SetP` paramId.** They are unrelated spaces
   (§6a). Writing internal id 13 does *not* set `input1Gain`; wire id 3 does.
3. Only send wire ids that appear in the §6a tables. Out-of-range ids are
   rejected and unmapped ones are no-ops, but there is no reason to probe blindly.
4. Values are **clamped by the firmware** to each parameter's min/max, so an
   in-range id cannot be driven out of its declared range.
5. Respect the synchronous rule — never leave the OUT pipe with an outstanding
   unanswered command.
6. Interface 6 (DFU) is never touched. `'FRst'` is not used by this code.

---

## 9. Tooling in this repo

**Driver and documentation**

| File | Purpose |
|------|---------|
| [`io24.py`](io24.py) | **The driver.** Transport, `GetP`/`SetP`, named parameter API, DSP-chain API, and the control CLI |
| [`io24_dsp.py`](io24_dsp.py) | **Coefficient maths.** Compressor (3 models), gate, limiter and biquad designers, reproducing the host's arithmetic and precision. Stdlib only; `python3 io24_dsp.py` runs its self-test |
| [`io24_meters.py`](io24_meters.py) | Metering datagram builders, plus the reverb and FX blob builders |
| [`io24_mixer.py`](io24_mixer.py) | The `fader` taper and mixer blob builders (2135 self-test assertions) |
| [`io24_fx.py`](io24_fx.py) | All six insert-FX model builders and their per-model `on` schemas, plus several biquad designers |
| [`calibrate.py`](calibrate.py) | Measurements needing a stable known signal — `tone` / `level` / `mixer` / `comp` |
| [`io24d.py`](io24d.py) | **The daemon.** Holds the device open and shares it with many clients over a Unix socket |
| [`io24gtk.py`](io24gtk.py) | **The mixer app.** Native GTK4/libadwaita; meters, strips, EQ with live response curve, dynamics, presets, and an XML-driven six-unit Voice FX rack with per-component On controls. USB on a worker thread |
| [`ucnet_shim.py`](ucnet_shim.py) | **UCNET compatibility server** — speaks the protocol existing PreSonus plugins expect, and translates to native USB |
| [`PROTOCOL.md`](PROTOCOL.md) | This document |
| `re/UCNET_SHIM_SPEC.md` (private research evidence) | The derived spec the shim implements |
| [`README.md`](README.md) | Install and use — the user-facing front door |
| `probes/` | Three original first-read/write probes kept as executable evidence |
| `70-presonus-io24.rules` | udev rule for non-root access |
| `systemd/io24d.service` | user unit for the daemon (`--wait`, so plug order does not matter) |

### The UCNET shim

On Windows, plugins (Stream Deck, Touch Portal, …) speak **UCNET over loopback TCP**
to `PreSonusHardwareAccessService.exe`, which bridges to USB. `ucnet_shim.py`
serves that same conversation and translates to our native protocol, so those
plugins can drive the io24 on Linux unmodified.

```bash
python3 ucnet_shim.py                 # or --port N --device-id N --serial S
python3 ucnet_shim.py --dry-run       # protocol only, no USB writes
```

It announces itself by **sending** a `'DA'` datagram to `127.0.0.1:47809` once a
second — deliberately without binding that port, because the client binds it and
only listens. On connect it answers `Subscribe` with a `SubscriptionReply` and a
full `Synchronize` state tree, then applies `PV`/`PS` writes and echoes them back.

Details that matter, all of them learned from the client's source rather than
guessed:

- **Echo every accepted write.** `RawService.SetValue` does not update its own
  cache, so without the echo a plugin's UI never reflects its own change.
- **One `write()` per message.** The client splits reads on the `UC\0\x01` magic
  and never reassembles; a split message is silently lost.
- **Booleans as `0.0`/`1.0`.** JSON `true`/`false` falls through the client's
  `Traverse` and is dropped, leaving the route absent.
- **Bodies under `0x4000`.** The real service drops the link above that.

> **`line/chN/pan` and `line/chN/dawpostdsp` are never forwarded.** The host binds
> them to *block 0*, which resolves to the device object — where `'Para'` wire 2 is
> `mainVolume`. Forwarding a pan write would slam the main output volume. They are
> shadowed host-side instead.

*Verified end to end against the hardware:* a client performing oddbear's exact
handshake received a valid 60-route `Synchronize`, and `PV` writes moved the real
device — `0.500 → 30.0 dB`, `0.750 → 45.0 dB` on the preamp, and phantom power on
and off through the integer path.

**Metering works.** The client advertises a UDP port in its `UM` welcome; the shim
registers it and streams alternating `'levl'` / `'redu'` datagrams there at 10 Hz,
built by [`io24_meters.py`](io24_meters.py) from a single `GetP 'JaSt'` read per
poll. Levels peak-hold and reduction min-holds between frames, matching the real
service.

*Verified:* 45 `levl` + 45 `redu` frames in 8 s (11.2 frames/sec) with values
tracking live audio, decoded by a client-equivalent parser.

> An earlier draft said the format was unknown because `MonitorService.cs` "was
> not in the source set". It was simply never fetched — it is in the same upstream
> repository as everything else.

#### Two bugs worth recording

**The shim originally shared the device across threads without a lock.** Metering,
state refresh and client writes all called into the driver concurrently — but the
native protocol is *strictly synchronous* (§4). The symptoms were a stalled meter
rate (0.5 frames/sec), reads returning zeros, and eventually a libusb segfault.
Every device access in the shim now goes through a single `usb_lock`. This is the
same rule §4 documents, violated in my own code.

**`_exec` used to waste a full timeout cycle per call.** After receiving a reply it
waited another 400 ms to confirm nothing more was coming. The paesdk header's
`totalLength` already says how long the reply is, so it can return immediately —
worth roughly **three orders of magnitude** on read throughput, and the difference
between metering being possible and not.

### The daemon

The USB control interface can only be claimed by **one process at a time** —
running the CLI while something else holds the device gives
`USBError: [Errno 16] Resource busy`. That is precisely the role
`PreSonusHardwareAccessService.exe` plays on Windows, and `io24d.py` fills it on
Linux: it holds the interface, caches state from a background poller, serialises
all USB access behind a lock, and serves clients newline-delimited JSON.

```bash
python3 io24d.py &                     # or --socket PATH --poll SECONDS
python3 io24d.py --client '{"cmd":"status"}'
python3 io24d.py --client '{"cmd":"meters"}'
python3 io24d.py --client '{"cmd":"set","param":"gain","channel":1,"value":40}'
python3 io24d.py --client '{"cmd":"set","param":"limiter","channel":1,"value":true,"threshold":-28}'
```

Any language that can write a line to a Unix socket can drive the device:

```json
{"ok": true, "levels": {"in1": 0.00257, "in2": 4.32e-05},
 "reduction": {"gate": [0.987, 1.0], "comp": [1.0, 1.0], "lim": [1.0, 1.0]}}
```

*Verified:* writes applied and read back through the daemon, **five concurrent
clients** served simultaneously, and a direct CLI correctly rejected with
`Resource busy` while the daemon held the interface.

**Verification probes** (each proved one thing; kept as executable evidence)

| File | Proved |
|------|--------|
| [`probe_getp.py`](probes/probe_getp.py) | first `'Rply'` — the native protocol works |
| [`probe_setp.py`](probes/probe_setp.py) | `'Appl'` parameter write, full-state diff + restore |
| [`probe_limiter.py`](probes/probe_limiter.py) | first DSP-chain write, asymmetric limiting |

**Reverse-engineering artefacts**

| File | Contents |
|------|----------|
| `re/state_map.md` (private research evidence) | Slot → parameter map as originally derived from hardware |
| `re/io24_params.json` (private research evidence) | Parameter descriptors extracted from the driver (incomplete — strict early parser) |
| `re/param_consumers.txt` (not redistributed) | Decompiled functions consuming the parameter tables |
| `re/*.py` | PE parsers and table extractors |

Additional `probe_*.py` files retained in the private research workspace are
historical experiments from the investigation — including the ones that failed. `probe_handshake*.py`,
`probe_sweep*.py`, `probe_optest.py` and `probe_batch.py` document the UCNET and
blind-probing dead ends of §7; `oracle.py` was an attempt at an automated audio
oracle that could not bootstrap. They are kept because a negative result that is
reproducible is worth more than one described in prose. They are referenced here
for provenance but are not part of the clean public source import.

Quick start:

```bash
sudo cp 70-presonus-io24.rules /etc/udev/rules.d/ && sudo udevadm control --reload
pip install --user pyusb
```

Control it from the shell:

```bash
python3 io24.py status              # all named parameters
python3 io24.py meters 10           # live levels + gain reduction
python3 io24.py gain 1 40           # ch1 preamp gain, dB
python3 io24.py hpvol 0.7           # headphone volume
python3 io24.py mainvol 0.5         # main output
python3 io24.py blend 0.25          # monitor blend, -1..1
python3 io24.py phantom 1 on        # 48V
python3 io24.py mute 2 on
python3 io24.py hpfreq 1 300        # high-pass cutoff, Hz (24 = bypass)
python3 io24.py limiter 1 on -28    # limiter with threshold in dBFS
python3 io24.py order 1 eq          # run EQ before the compressor
```

Every control command prints the state read back afterwards, so a write is
visible even though `SetP` itself returns nothing.

Investigation helpers: `dump` (raw state), `stable N` (classify meters vs
parameters), `snap`/`diff` (state snapshots), `watch N` (live-map a physical
control to its slot).

As a library:

```python
from io24 import Io24

dev = Io24()

# --- 'Appl' block: named live state + parameter writes -------------------
p = dev.read_params()
print(p["input1Gain"], p["hpVolume"], p["input1Level"])

dev.set_gain(1, 40.0)            # ch1 preamp gain, dB (clamped 0..60)
dev.set_hp_volume(0.7)           # headphone, 0..1
dev.set_main_volume(0.5)
dev.set_monitor_mix(0.0)         # blend, -1..1
dev.set_phantom(1, True)         # 48V on ch1
dev.set_mute(2, True)
dev.set_highpass(1, True)        # 'Appl'-level high-pass enable
dev.set_channel_link(True)

# --- per-channel DSP chain ----------------------------------------------
dev.read_reduction("lim ", 1)    # [1.0, 1.0]  linear gains, dB = 20*log10(v)
dev.read_reduction("gate", 1)    # gate reduction, 2 instances
dev.read_reduction("opt ", 1)    # whole chain aggregated, 6 values

dev.set_limiter(1, True, -28.0)          # both instances
dev.set_limiter(1, True, -55.0, instance=0)   # one instance only
dev.set_limiter(1, False)                # restore firmware power-on state

dev.set_highpass_freq(1, 300.0)  # 'filt' block, 24..1000 Hz (24 = bypass)
dev.set_biquad("eq  ", 1, coeffs, band=2)     # raw biquad, EQ band 0..3
dev.set_comp_eq_order(1, eq_first=True)

# --- dynamics, in human units (see §9b) ---------------------------------
dev.set_gate(1, on=True, threshold_db=-40, range_db=-60,
             attack_s=0.01, release_s=0.3, keyfilter_hz=730, expander=False)
dev.gate_off(1)                  # byte-exact firmware power-on restore

dev.set_compressor(1, model=0, on=True, threshold_db=-24, ratio=4.0,
                   attack_s=0.01, release_s=0.2, gain_db=6.0, softknee=True)
dev.set_compressor(1, model=1, on=True, peak=50, gain=55)        # Tube
dev.set_compressor(1, model=2, on=True, input_db=-20, output_db=-10,
                   ratio_index=2)                                # FET
dev.compressor_off(1)

dev.close()
```

`Io24.highpass_coeffs(freq, fs, q)` exposes the RBJ maths if you want to compute
coefficients yourself.

### Verification status

| Area | Status |
|------|--------|
| Reading `'Appl'` state (`'JaSt'`) | **verified on hardware** |
| Writing `'Appl'` params (`'Para'`, `'Pari'`) | **verified** — gain 23→45→23 dB; `input1SlotIndex` 0→1→0 |
| DSP chain reduction metering (`'Redu'`) | **verified** — counts 6/2/2/2/0/0 exactly as derived |
| DSP limiter write | **verified** — asymmetric limiting to −33.9 dB on instance 0, instance 1 held at 1.0 |
| `set_limiter()` wire bytes | **verified byte-identical** to the hand-checked message |
| `'filt'`/`'eq  '` biquad writes | accepted by the device; **no read-back exists**, so only audible verification is possible |
| `'gate'` blob + unit conversions | **verified** — reduction meter reads the range floor exactly (−6/−20/−40 dB → 0.5012/0.1000/0.0100) |
| `'cpxt'` compressor writes | **verified working** — reduction responds to threshold and ratio, instance asymmetry, clean restore. Exact transfer curve not validated against controlled signals |
| Limiter release formula | **verified** — `exp(−2π/(t·fs))` reproduces the host constant bit-exactly at t = 0.4 s |
| Coefficient maths ([`io24_dsp.py`](io24_dsp.py)) | 49 self-test assertions pass; reproduces host single/double precision |
| 4-band parametric EQ (`set_eq_band`) | **verified on hardware** — transfer measured against a live source with a 0.008 dB baseline spread; band indexing and metric frequency accuracy both confirmed (§9d) |
| EQ numerical stability | **verified exhaustively** — worst pole 0.99995 over the whole accepted parameter space in float32; shelves/filters overshoot above Q≈1 (§9d) |
| Host-side presets | **verified on hardware** — save → move everything → load restored all seven live values exactly (§9d) |

> `SetP` is **fire-and-forget**: it returns no reply *by design*. An empty
> response to a write is expected and is not an error.

**Both directions are verified on hardware.** Confirmed round trips:

- `'Para'` (float), wire id 3 index 0 → ch1 preamp gain **23 dB → 45 dB → 23 dB**,
  observed in `JaSt` slot 46.
- `'Pari'` (int), wire id 16 index 0 → `input1SlotIndex` **0 → 1 → 0**, observed
  in slot 40.

In both cases a full 489-slot stable-state diff showed **only the intended slot
changing**, and the restore returned the device exactly to baseline.

Note that not every writable parameter is observable: `JaSt` only reports slots
0–50, so e.g. the high-pass filter can be set but not read back through this
state dump.

---

## 9a. The per-channel DSP chain

`'Appl'` is the device/system block (§6, §6a). Separately there is a **six-block
DSP chain, instantiated once per input channel**. Its architecture is
fundamentally different from `'Appl'` and this is the most important structural
fact in the protocol.

### Dispatch

A message whose `blockFourCC` is not `'Appl'` is routed:

```
top-level registry 0x6005cef0
    blockIndex = msg[+0x08]        ; cmp #1 / bls  -> hard bounds check, else NULL
    slot       = *(dev + 0x5d74 + 4*blockIndex)
    container  = dev + 0x43a0 + 0xcec*slot
  -> chain resolver 0x6006efe8 : FourCC -> sub-block object
```

| Block | wire bytes (LE) | container off | handler vtable | slots |
|-------|-----------------|---------------|----------------|-------|
| `'opt '` | `20 74 70 6f` | +0x000 | 0x6012cad4 | 15 |
| `'filt'` | `74 6c 69 66` | +0x058 | 0x6012c8ec | 14 |
| `'gate'` | `65 74 61 67` | +0x204 | 0x6012c614 | 14 |
| `'comp'` | `70 6d 6f 63` | +0x454 | 0x6012c71c | 17 |
| `'eq  '` | `20 20 71 65` | +0x6d4 | 0x6012c5b8 | 14 |
| `'lim '` | `20 6d 69 6c` | +0xc40 | 0x6012c670 | 13 |

Anything else resolves to NULL and is silently dropped. `'comp'` and `'eq  '`
are easy to miss: the firmware derives their FourCCs *arithmetically* from two
literals rather than storing them, so a byte-search does not find them. Both were
**confirmed on hardware** (they reply and echo; a bogus `'xxxx'` does not).

### These blocks have no parameter-id space

Every one of these vtables terminates with the Itanium-ABI `0xfffffffc` marker
long before offsets `+0x84`/`+0x88`, so the `'Appl'` `'Para'`/`'Pari'` layout
**structurally cannot exist here**. They are programmed with **pre-computed DSP
coefficient blobs** instead:

| Block | SetP blob tags | Payload |
|-------|----------------|---------|
| `'filt'` | `'Bqdf'`, `'MBdf'`, `'Setu'` | `'Bqdf'` (0x24) = 5×f32 `[b0, −a1, b1, −a2, b2]`, a0 normalised; identity = `[1,0,0,0,0]` |
| `'gate'` | `'gate'`, `'MBdf'`, `'Setu'` | one 0x48 blob: key-filter biquad + gate coefficients |
| `'comp'` | `'cpxt'`, `'IOSp'`, … | 0x40-byte coefficient blob |
| `'eq  '` | `'Bqdf'`, `'Lfdf'`, `'MBdf'`, `'MLff'` | per-band biquads; **band index is at blob+0x0c**, bounds-checked 0–3 |
| `'lim '` | `'lim '`, `'Setu'` | 0x18 blob: `enable u32`, `inverse-threshold f32`, `release coef f32` |
| `'opt '` | `'Pari'` | **the only wire parameter on the whole chain**: id 0 = `swapcompeq` (0 = comp→eq, 1 = eq→comp) |

**The dB/seconds → coefficient maths lives in the host driver, not the device.**
A Linux driver therefore has to reimplement it. The filter case is fully
recovered: RBJ 2nd-order high-pass, Q = 0.70710678,
`K = tan(pi*f/fs)`, `norm = 1 + K/Q + K²`,
`c = [1/norm, 2(1−K²)/norm, −2/norm, (K/Q−1−K²)/norm, 1/norm]`; cutoff range
24…1000 Hz where 24 = bypass.

### Three different "index" fields

Conflating these is the main way to write to the wrong place.

| Field | Where | Meaning | Checked? |
|-------|-------|---------|----------|
| `blockIndex` | msg +0x08 | **channel strip** (0 = ch1, 1 = ch2) | **yes** — firmware rejects >1 |
| blob `index` | blob +0x08 | per-block: sub-instance for `gate`/`comp`/`lim `; *ignored* by `filt`/`opt `; a sample rate for `'Setu'`; an **output slot** for `'Redu'`/`'Ltcy'` | **no** |
| blob `paramId` | blob +0x0c | only meaningful for `opt `+`'Pari'` (id 0) and `eq`+`'Bqdf'` (band 0–3, checked) | partly |

> **Driver rule: clamp the blob index to 0–1 yourself — the firmware will not.**
> Out-of-range values write past the sub-object (e.g. `lim ` index ≥ 3 writes
> beyond its 0x4c-byte object).

~~`filt` and `eq  ` `'Bqdf'` broadcast to all channels of the block~~ — **this was
wrong, and is corrected by measurement.** It was inferred from the handler not
appearing to branch on the channel index. On hardware, writing a -15 dB shelf and
a 5 kHz high-pass to *channel 2* moved channel 1 by **0.010 dB** (noise), while
the identical write to channel 1 moved it by **5.245 dB** (§9d). `'Bqdf'`
addresses **one channel**, exactly as `gate`, `comp` and `lim ` do.

`gate`, `comp` and `lim ` additionally address one *sub-instance* within the
channel, so a stereo-consistent setting there needs two writes (index 0 and 1).

### Reading

| Block | Accepted GetP tags |
|-------|--------------------|
| `opt ` | `'Redu'`, `'Ltcy'` |
| `filt` | `'Ltcy'`, `'IFac'` |
| `gate` | `'Redu'`, `'Ltcy'`, `'IFac'` |
| `comp` | `'Redu'`, `'Ltcy'`, `'IFac'`, `'IOSp'` |
| `eq  ` | `'Ltcy'`, `'IFac'` |
| `lim ` | `'Redu'`, `'Ltcy'` |

**`'Redu'`** — gain-reduction metering. The request's size field must be ≥ `0x4c`.
The reply holds *N* linear gains from blob+0x08 with **N at blob+0x48**
(1.0 = no reduction, dB = 20·log10(v)). `gate`, `comp` and `lim ` each report 2;
`filt` and `eq  ` report 0; `opt ` aggregates the chain and returns **6**.

*Verified on hardware:* `opt `/`'Redu'` returns count **6**, values
`0.9997, 1, 0.9085, 1, 0.9998, 1` — live reduction, ≈ −0.83 dB on one stage.
An earlier note in this document claimed 4; that was a probe bug (reading a fixed
window instead of the count field), now corrected.

Because the generic reply path memcpy's your request blob *before* calling the
handler, **an unrecognised tag still yields a well-formed reply** — a reply is
not evidence that a tag was understood.

### Verified DSP write

The first write to the DSP chain is confirmed on hardware — limiter on channel 1,
**instance 0 only**:

```
SetP | 'lim ' | blockIndex 0 | blob 'lim ' size 0x18 | index 0 | enable 1
                                | invThreshold f32 | release f32
50 74 65 53 | 20 6d 69 6c | 00 00 00 00 | 20 6d 69 6c | 18 00 00 00
            | 00 00 00 00 | 01 00 00 00 | <invThresh> | <release>
```

`invThreshold = 10**(-thresholdDb/20)` — independently reproduced: −28 dBFS gives
`0x41c8f36f`, matching the constant found in the host driver. `release` for
fs = 48000 is `0x3f7fea8f`.

With speech on input 1 and a −55 dBFS threshold, `'Redu'` tracked live limiting
from −2 dB down to **−33.9 dB on instance 0, while instance 1 stayed at exactly
1.000000**. That asymmetry confirms framing, block routing, blob layout, index
semantics and metering in one observation.

**Power-on restore** (from firmware initialiser `0x60076c6c`): `enable = 0`,
`invThreshold = 0x3faab0d5`, `release = 0x00000000`. Writing `enable = 0` also
makes the handler synchronously store `1.0` into the reduction slot, so the undo
is self-verifying.

> ### `'IFac'` must never be sent over USB
> It is an in-process interface handshake that **treats a wire-supplied value as
> a pointer and writes a firmware address through it**. It exists to be called
> inside the device, not across the bus. This driver never sends it.

## 9b. Dynamics coefficients — the `'cpxt'` and `'gate'` blobs

The maths that turns human units into device coefficients lives in the *host*
driver. It is reimplemented in [`io24_dsp.py`](io24_dsp.py) (stdlib only, 49
self-test assertions), reproducing the host's arithmetic in the same order and
the same precision — the biquad designer in double narrowed to float, everything
else single-precision `powf`/`expf`.

### Compressor models

`compmodel` selects between three **host-side** implementations. The device has
exactly one compressor; the model only decides which maths fills the identical
`'cpxt'` blob.

| idx | Name | Product name | Class GUID |
|-----|------|--------------|------------|
| 0 | Standard | StudioLive Compressor XT | `{870D04F7-212E-4F9C-ADBB-39A97216433F}` |
| 1 | Tube | StudioLive Tube Compressor | `{7F8A4262-D377-48E3-9D48-15D82C400A71}` |
| 2 | FET | StudioLive FET Compressor | `{1F831EC1-B8AC-4EE9-AD53-54227AF53D58}` |

(EQ models: Standard / Passive / Vintage. FX models: Transformer, De-Tuner,
Vocoder, Ring Modulator, Filters, Delay.)

> An earlier draft of this document claimed the model registry was a set of GUIDs
> at file `0xe7430`. **That was wrong** — those are the Windows USB driver
> *InterfaceGUIDs*, which appear verbatim in `Drivers/*/x64/custom.ini` and in
> every `hwaccess/*device.dll`. The real registry is a pair of index-aligned
> `{const char*, int32}` tables at VA `0x1800f0480`–`0x1800f0648`.

### `'cpxt'` — 0x40 bytes

```
+0x00 u32  tag 'cpxt'        +0x04 u32 size 0x40      +0x08 u32 index (clamp 0..1)
+0x0c 5×f32  key/sidechain biquad [b0,-a1,b1,-a2,b2], identity = [1,0,0,0,0]
+0x20 f32  attack  seconds   +0x24 f32 release seconds
+0x28 f32  slope = 1 − 1/ratio
+0x2c f32  knee   dB (> 0)   +0x30 f32 threshold dBFS
+0x34 f32  makeup gain LINEAR (not dB)
+0x38 u32  enable            +0x3c u32 key listen
```

There is **no paramId word** — data starts at `+0x0c`. (Contrast `filt`/`eq  `
`'Bqdf'`, where `+0x0c` *is* the band index.)

### `'gate'` — 0x48 bytes

Host parameter ids are `0=on 1=keylisten 2=expander 3=keyfilter 4=threshold
5=range 6=attack 7=release`. Payload at `+0x0c`: key-filter biquad (RBJ
band-pass, Q = 8.0; ≤ 40 Hz ⇒ identity), then `range = 10^(range_dB/20)`,
`threshold = 10^(−threshold_dB/20)` (inverse, as the limiter), attack/release
seconds, their coefficients (detector runs at **fs/4**), hold in samples, and the
on/expander/keylisten flags.

*Verified on hardware.* With `expander=False` the reduction meter reads the range
floor exactly:

| `range_db` | expected `10^(range/20)` | measured |
|-----------|--------------------------|----------|
| −6 | 0.5012 | **0.5012** |
| −20 | 0.1000 | **0.1000** |
| −40 | 0.0100 | **0.0100** |

With `expander=True` there is no hard floor — reduction is proportional — which
is why an initial test using the expander default appeared to fail.

### Compressor transfer curve — measured

Sweeping the Standard model's threshold against a fixed −7.3 dBFS tone at 4:1:

| threshold | dB over | reduction |
|-----------|---------|-----------|
| −60 dB | 52.7 | −37.30 |
| −50 dB | 42.7 | −29.83 |
| −40 dB | 32.7 | −22.34 |
| −30 dB | 22.7 | −14.82 |
| −20 dB | 12.7 | −7.30 |

**The ratio law is exact.** Ten dB of threshold moves the reduction by 7.47–7.52 dB
— a mean slope of **0.7500 dB/dB**, i.e. a measured ratio of **4.00:1** for a 4:1
setting.

The curve sits a constant **+2.207 dB** above a naive prediction made from the
*peak* meter reading, and that constant has a clean explanation: **the sidechain
detector is RMS**. A sine reads 3.010 dB higher on a peak meter than on an RMS one,
so the detector sees 3.010 dB less signal over the threshold and reduces
`0.75 × 3.010 = 2.258 dB` less. Predicted +2.258, measured +2.207 — agreeing to
**0.05 dB** across a 40 dB span, with only 0.04 dB of spread.

So the compressor implementation is validated end to end, and the measurement also
establishes a fact that was not in the binary analysis: the detector is RMS, not
peak.

### Limiter release

`release_coef = exp(−2π / (t·fs))`. At `t = 0.4 s`, fs = 48000 this reproduces
the known-good constant `0x3f7fea8f` **bit-exactly**, confirming the formula.

> **Precision note.** The threshold must be computed as the host does —
> `powf(10.0f, dB × −0.05f)` in *single* precision. The double-precision
> `10**(-dB/20)` differs by 1 ULP (`0x41c8f36f` vs `0x41c8f36e`). Inaudible, but
> the driver now matches the host byte-for-byte.

## 9c. Numeric blocks

Besides the FourCC blocks, the firmware dispatcher at `0x6005ce98` routes **numeric**
block ids separately, before falling through to the DSP chain resolver:

| Block | Object | blockIndex | Notes |
|-------|--------|-----------|-------|
| 0 | the device object itself | — | **the same object `'Appl'` addresses** — see the shim's BLOCKED routes |
| 100 | `dev + 0x3e28 + 0x74*i` | 0–2 | **the mixer** — main / Mix A / Mix B |
| 201 | `dev + 0x11e0` | — | unidentified |
| 202 | `dev + 0x3a70` | — | unidentified |
| 203 | `dev + 0x3f84 + 0x4c*i` | 0–2 | implements `'Redu'` (1 value per index) |
| other | → chain resolver | 0–1 | the six FourCC DSP blocks (§9a) |

*Probed read-only on hardware:* **block 203 returns `'Redu'` metering at indices 0, 1
and 2** — one value each. Blocks 100, 201 and 202 returned nothing for any tag tried
(`Redu`, `Ltcy`, `JaSt`, `MemP`, `mprm`, `Stat`, `IOSp`, `Setu`), which for block 100
is consistent with the mixer being **write-only** — there is no way to read a mixer
level back.

### The mixer (block 100) — wire form verified on hardware

*Confirmed:* `SetP | block 100 | blockIndex 0 | 'Para' | paramId 3 | f32 gainDb`
controls **channel 1's level in the main mix**. Writing the `−145.0` off sentinel
drove the mix-bus meters (`JaSt` slots 12/13) to silence (−179.7 dB) while the ch1
*input* meter held steady; writing `0.0` restored it. The restore was exact — the
bus sat 5.4 dB below the input meter before and 5.5 dB after — so the level really
is a dB float with unity at `0.0`.

Still unverified: the other paramIds (only `3` = line/ch1 was exercised), the
other paramIds (only `3` = line/ch1 was exercised on hardware). The `fader` taper
and pan law are now recovered — see §9d — but with **no read-back** on this block,
a wrong level cannot be detected or precisely undone, which is why the shim still
shadows mixer routes rather than forwarding them.

### dB scaling — measured, exact

With a stable tone looped from a main output back into input 1, sweeping
`line/ch1` into Mix A (main muted so the loop stays open):

| asked | measured | error |
|-------|----------|-------|
| −3 dB | −3.00 | +0.00 |
| −6 dB | −6.00 | −0.00 |
| −10 dB | −10.00 | −0.00 |
| −15 dB | −15.00 | −0.00 |
| −20 dB | −20.00 | +0.00 |
| −30 dB | −30.00 | +0.00 |
| off (−145) | silent | — |

**The gain law is exact across the whole range**, to the resolution of the meter.

> An earlier run using speech as the source showed a 3.45 dB error on the
> 0 → −6 dB step, which this document attributed to "bus dynamics, probably the
> main limiter". **That was wrong on both counts** — there was no error and no
> limiter involved. Peak-holding a fluctuating source and dividing two peaks that
> occur at different instants simply does not measure gain. The lesson is about
> measurement method, not about the device.

### A calibrated signal source with no cable — the USB return

The device's USB playback endpoint (6 channels, S32_LE, 44.1–96 kHz, see
`/proc/asound/card*/stream0`) arrives inside the mixer as the source
**`return/ch1`**, at **exactly unity gain**. Play a −26 dBFS tone and the bus
meter reads −26.00 dBFS. Proved by muting each return in turn while the tone
played: only `return/ch1` killed it.

That is a better measurement substrate than anything physical — exact, perfectly
steady, repeatable, and it needs no loopback cable, no microphone and nobody to
hold still. `calibrate.py usbtone [secs] [hz] [dbfs]` drives it.

Its one limitation, measured rather than assumed: **it cannot reach the FX.**
With Mix A carrying the FX return alone and a −26 dBFS tone on `return/ch1`, the
FX return sits at −75 dBFS — 49 dB down, i.e. the noise floor. The FX send takes
from the mic/line inputs, so anything touching block 201 or 202 still needs a
real signal on a physical input.

### The gain law, re-measured exactly

The original mixer measurements used speech and a pedal, and had to normalise
away source drift. Redone with the USB-return tone, where the measured bus level
*is* the answer with no normalisation:

| asked | main | Mix A | Mix B |
|---|---|---|---|
| 0 dB | −26.03 | −26.00 | −26.00 |
| −10 | −36.00 | −36.19 | −36.00 |
| −30 | −56.00 | −56.19 | −56.00 |
| −50 | −76.00 | −76.03 | −76.00 |
| −60 | −86.00 | −86.03 | −86.00 |
| −70 | −96.03 | −96.00 | −96.03 |
| −80 | −106.00 | −106.03 | −106.00 |

**Worst deviation 0.03 dB, across a 106 dB range, on all three buses.** The
mixer gain law is exact, and this is the first confirmation that **Mix B scales
correctly** — it was unreachable until the blockIndex fix below.

One trap worth recording: an earlier run of this same sweep showed main and Mix B
apparently collapsing below −40 dB while Mix A tracked perfectly. That was not a
gain-law failure — `return/ch2` and `return/ch3` had been left open in main, and
because ALSA's `plughw` performs channel conversion the tone leaked through them
and floored the measurement. With every source explicitly switched off, all three
buses read −99.00 dBFS and the anomaly vanished. **Switch off every mixer source
you are not measuring, not just the obvious one.**

### Mix B was unreachable — a driver bug, fixed 2026-07-30

`_dsp()` clamped `block_index` to `0..1`. That is correct for the per-channel DSP
blocks, where the index selects one of two channels and an out-of-range value
would write past the sub-object. It is **wrong for the mixer**, where the index
selects the *bus*: `main`=0, `mixa`=1, `mixb`=2.

So every `set_mix_db(..., bus="mixb")` this driver ever sent was silently
redirected into Mix A. The mixer has no read-back, so nothing could reveal it —
the write was accepted, the wrong bus moved, and no meter contradicted the
caller's intent.

Fixed by giving `_dsp()` an explicit `max_index`, defaulting to 1 for the DSP
blocks and passed as `max(MIXER_BUSES.values())` by the mixer.

*Verified on hardware after the fix* (all figures normalised to the input meter,
because the source drifts): switching `line/ch1` off in Mix B collapsed the Mix B
meter by **30.0 dB** while main and Mix A held at +4.66 and +1.66 dB, unchanged
to the hundredth. The control — switching it off in Mix A instead — moved only
Mix A. The three buses are independent, and Mix B now works.

### Wire form

The host side is recovered:

```
SetP | block 100 | blockIndex B | 'Para' size 0x14 | index 0 | paramId S | f32 gainDb
  B = 0 main, 1 Mix A, 2 Mix B
  S = line/ch1 3, line/ch2 4, return/ch1..3 0,1,2, fxreturn/ch1 5
  batch form: blob 'mprm', 0x1f0 bytes, 60 × {u32 paramId, f32 gainDb}
```

The value is a **dB float**, not a linear gain: `−145.0` means off, `−144.0` is the
floor, and the host computes
`volume_dB + aux_dB + 20·log10(blend) + pan_dB` before sending.

It is **not exposed**, deliberately — see §9d, where the taper, pan law and
source-slot map are now recovered. The original blockers were: the exact
parameter→slot assignment inside the link object, the pan law's two modes, and — the
one that actually bites — the `volume` descriptor's taper is **`fader`**, not linear,
over `[−96, +10] dB`, so a normalised 0.5 does *not* map to the obvious level. Combined
with the absence of any read-back, a wrong write cannot be detected or precisely undone.

## 9d. Metering, mixer laws, and where reverb/FX live

### Metering datagrams

The service streams meters to the UDP port the client advertises in its `UM`
welcome. Builder: `dspusbdevice.dll` VA `0x180034030`. Frames alternate between
`'levl'` (levels) and `'redu'` (gain reduction), inside the normal 12-byte UCNET
header with type `MS`:

```
[FourCC 'levl'|'redu'][u16 0][u16 N][N × u16 values]
[u8 G][G × (u16 id, u16 start, u16 count)]      <- self-describing group table
```

That trailing group table is exactly the "unknown2/unknown4" bytes oddbear's
client hard-codes without explanation. **The generated builder reproduces his
constants byte-for-byte** for both the 81-byte and 101-byte packets, which is the
end-to-end proof of the derivation.

Value encoding, verbatim from the sender:
`if (f >= 1.0f) v = 0xFFFF else v = (u16)(int)(f * 65535.0f)` — plain **linear**
scale, truncating, not dB. That is why "FF FF is the lowest value" for the
compressor VU: reduction meters carry linear *gain*, so unity = `0xFFFF`.

Every field maps onto a `JaSt` slot (`blob_offset = 0x10 + 4*slot`), so one
`GetP 'JaSt'` read feeds an entire datagram — including the 18 gain-reduction
values, which are **slots 18–35**.

Implementation: [`io24_meters.py`](io24_meters.py).

### Mixer laws

**The `fader` taper is not a formula — it is a 5-point piecewise-linear table**
of `{normalised, dB}` pairs, selected by the descriptor's min/max:

| normalised | 0.000 | 0.004 | 0.090 | 0.470 | 1.000 |
|-----------|-------|-------|-------|-------|-------|
| dB | −96 | −60 | −40 | −10 | +10 |

Conversion is a segment search plus linear interpolation (`0x18005cfe0` norm→dB,
`0x18005d080` dB→norm). This is **bit-for-bit the same curve as oddbear's
`VolumeValue.cs`** — checked across a 1001-point sweep, max deviation < 2e-4 dB.
(`OutputValue.cs` does *not* agree; it is an exponential fit to a different,
`exp`-tapered control and is explicitly an approximation.)

**Pan law:** `g(x) = K·x² + (1−K)·x`, `K = −0.831783`. `g(0.5) = 0.7079458`, i.e.
exactly −3.00 dB centre. Mode 1 is the left leg (`g(1−pan)`), mode 2 the right
(`g(pan)`).

> **Negative result:** on the io24 the mixer send's pan term is **structurally
> dead** — the link object's pan pointer, stereo flag and mode word are zeroed by
> both constructors and never assigned. This corroborates the shim's decision to
> block `line/chN/pan`, which is bound elsewhere entirely.

**Source-slot map** (`paramId` = the channel's index within its route group):
`line` 0→3, 1→4, 2→6; `return` N→N; `fxreturn` 0→5. `blockIndex` comes from the
destination bus: main→0, Mix A→1, Mix B→2, all accepting the same paramId space.

Implementation: [`io24_mixer.py`](io24_mixer.py) (2135 self-test assertions).

### Reverb and insert FX

They are the two previously-unidentified numeric blocks:

| Block | Object | Role |
|-------|--------|------|
| **201** | `dev + 0x11e0` | Voice-FX / insert-FX slot |
| **202** | `dev + 0x3a70` | reverb |

Both are **singletons** — the dispatcher computes their address without using
`blockIndex`. The identification comes from a direct correspondence between the
two binaries: every component class passes its block id to the common constructor
`0x180047f00`, where `FilterComponent` passes `'filt'`, `GateComponent` `'gate'`,
and `VocalReverbComponent` passes **`0xca` = 202** while all six insert-FX classes
pass **`0xc9` = 201`**.

Like the per-channel DSP blocks they take **coefficient blobs**, not parameter
ids — one flat blob per model, pushed whole on every change. Block 202's handler
(`0x60076e70`) accepts `'vrvb'` and `'Setu'`; the FX tags `'vech' 'godv' 'may4'
'bota' 'botb' 'botc'` all exist as literals in the io24 firmware. Both are
**write-only** — block 202's GetP slot is a bare `bx lr`.

### Reverb — confirmed working on hardware

`SetP | block 202 | 'vrvb'`, 0x38 bytes:

```
+0x00 tag 'vrvb'   +0x04 size 0x38   +0x08 index
+0x0c u32 on       +0x10 f32 mix     +0x14 u32 predelay_enable
+0x18 f32 predelay +0x1c f32 size    +0x20 f32 hp_freq
+0x24 5×f32 high-pass biquad [b0,-a1,b1,-a2,b2]
```

**The reverb is a send effect** — writing the blob alone does nothing audible.
The full path must be open:

1. **the channel's FX send** — `'Para'` wire id **4** + channel index, on the
   `'Appl'` block (`input1FxMix` / `input2FxMix`), and
2. **the FX return into a bus** — mixer `paramId 5` (`fxreturn/ch1`).

With both up, an A/B at `mix 0.90, size 0.95` was **clearly audible**; with the
send closed it was silent, which is what makes the routing conclusion firm rather
than assumed.

That test also **confirmed mixer `paramId 5` on hardware** — until then only
`paramId 3` had been verified, so the source-slot map now has two independent
confirmations.

### Insert FX (block 201) — historical negative with a superseded transaction (2026-07-30)

The current Input-1 result at the top of this file supersedes this section's
block-201 conclusion. The measurements remain useful evidence for what the old
transaction did and for block 202's independently working return path.

> **This section's original claim no longer holds and is left below for the
> record.** Re-tested on 2026-07-30 with a measurement rig that resolves
> **0.008 dB**, block 201 produced *no response whatsoever*, while block 202
> (reverb) responded in the same session on the same routing.
>
> What was tried, all negative (swing measured wet-vs-dry, needs ≈ −6 dB):
>
> | test | result |
> |---|---|
> | Delay: on/off, feedback 0.0 / 0.6 / 0.9, fully wet | ≤ 0.08 dB |
> | Ring Mod (dist=0, unity gain): mix 0 → 0.5 → 1 → 0 | 0.05 dB |
> | …with Mix A carrying **only** the FX return | 0.05 dB |
> | …with Mix A carrying **only** line/ch1 | 0.06 dB |
> | `set_fx_mix` ('Para' id 4) swept 0 → 1 | 0.15 dB — **no effect at all** |
> | `'opt '` `'Pari'` ids 0–7 set to 1, each re-tested | ≤ 0.018 dB |
>
> Two things that ARE established by the same session: the FX return reaches the
> buses (opening `fxreturn/ch1` adds ~4.5 dB, +6 dB more when raised), and the
> **reverb at block 202 processes** (ratio moved −7.4 → −5.0 dB, with a slow
> recovery afterwards that is its tail decaying). So the return path works; block
> 201 simply is not feeding it.
>
> Also disproved: **`'Para'` id 4 (`set_fx_mix`) is not the FX send.** Sweeping it
> across its full range changes nothing on the FX return. Whatever opens the send
> is still unidentified.
>
> **Further negatives, 2026-07-30 (after the mixer blockIndex fix).** The natural
> next hypothesis was that the FX send is a mixer *bus* rather than a parameter —
> the classic architecture, and one the blockIndex clamp had made untestable.
> Feeding `line/ch1` at 0 dB into mixer blockIndex **3, 4, 5 and 6** in turn, with
> Mix A carrying only the FX return and Ring Mod armed at unity gain, produced
> swings of −0.40, −0.15, +0.29 and +0.04 dB. No send bus. In the *same run*, the
> reverb positive control reproduced at **+2.26 dB**, so the rig was good and the
> negative is real.
>
> **The `'opt '` block is now properly tested and cleared (2026-07-31).** An
> earlier 48-point sweep was invalid — it used one baseline taken up front, and
> the source faded 20 dB mid-run (the operator unplugged it), so the gate engaged
> and every id looked like a hit. Redone with a **self-generated signal** (see
> below) and a per-step A-B-A baseline, with both positive controls proved on the
> same routing first: `'Pari'` ids 0–31 and `'Para'` ids 0–15 all landed within
> **±0.09 dB**. Nothing in `'opt '` activates block 201.
>
> **A signal source that needs no cable and no human:** raise the preamp to 60 dB
> and its own noise floor becomes the test signal — broadband, always available,
> and measured stable to **0.100 dB spread (sd 0.035)** over six windows, with the
> level tracking gain 1:1 above 40 dB (so it is genuine amplified noise, not a
> digital floor). Broadband noise is *better* than a tone here: it excites every
> frequency, so any filter or modulator has to show. Keep `line/ch1` out of the
> main bus and measure on Mix A, which does not feed the main output.
>
> With that source, the sharpest statement of the negative: on the routing where
> the rig is provably sensitive, the **reverb control moved −1.105 dB while Ring
> Mod moved −0.003 dB** — a discrimination of about 350:1, same signal, same
> routing, same run. And a second routing correctly reported itself *blind* to the
> reverb (a send effect cannot appear in the channel path), which is the control
> behaving as it should rather than a failure.
>
> **The host DLL was re-extracted and analysed directly (2026-07-31).** The
> Universal Control installer is **deflate-compressed, not LZMA** — that is why
> earlier attempts failed. It is recoverable with nothing but Python:
>
> ```python
> zlib.decompressobj(-15).decompress(open(installer,'rb').read()[0x1601c:])
> ```
>
> (0x1601c is just past the NSIS first header, found by locating `NullsoftInst`.)
> That yields a 264 MB payload; `dspusbdevice.dll` is the PE at payload offset
> 0x922fdc7, 4.7 MB, imagebase 0x180000000 — matching every address in this
> document.
>
> **What the selector actually is.** `voicefxopt` / InsertFXSelectorComponent
> registers exactly **one** parameter: `kind=2, id=450, name="fxmodel"`. Writing
> it is what drives the model factory — at `0x1800498f0` the host checks
> `kind == 2 && descriptor.id == 450`, converts the value with `cvttss2si`, and
> calls the factory at `0x180049960`. The factory requires `model+1 <= 6`, i.e.
> **model −1 through 5, where −1 means "no FX"**.
>
> This also explains why the earlier `'opt '` sweeps found nothing: they covered
> ids 0–31, and the parameter is **id 450**.
>
> **Our wire bytes are byte-identical to the host's** — verified by disassembling
> the push at `0x180049e4f`:
>
> ```
> mov qword [rsp+0x70], 0xc9         ; block 201
> mov dword [rsp+0x20], 0x566f4678   ; 'VoFx'
> mov qword [rsp+0x24], 0x10         ; size 0x10 (QWORD, so index at +0x08 = 0)
> mov dword [rsp+0x2c], r13d         ; model at +0x0c
> ```
>
> which is exactly `struct.pack('<IIII', VOFX, 0x10, 0, model)`. So the device is
> receiving precisely what Universal Control would send, and still does nothing.
>
> **`voicefx` is registered on every product.** An earlier reading of the partial
> decompilation suggested the engine was never registered; that was wrong. Counting
> real cross-references in `.text`, `voicefx` has **19** and `voicefxopt` **12**,
> and every one of the six product component-trees registers both, next to each
> other. The "io24 doesn't have this feature" theory is dead.
>
> **A tempting inference that does NOT hold.** Scanning the io24's own firmware
> image for tag literals: all six model tags are present exactly once each
> (`bota` 0x60077a68, `botb` 0x60078df8, `botc` 0x60079f98, `vech` 0x60077274,
> `godv` 0x6007bd38, `may4` 0x6007bb68), as are `vrvb` and `inia` — but **`VoFx`
> is absent**, in both literal and split `movw`/`movt` form. That looks like the
> answer, and it is not: **`comp` and `eq  ` are equally absent**, and both are
> verified working on hardware. So the firmware plainly dispatches some blocks
> without a matching literal, and `VoFx`'s absence proves nothing on its own.
>
> **Resolved: the `comp`/`eq  ` anomaly, and the likely answer for `VoFx`.**
> Searching both byte orders separates two different things:
>
> - The **dispatch literals** are the byte-reversed form (the LE u32 as it appears
>   on the wire). Every *blob tag* the device demonstrably handles is present in
>   that form: `cpxt`, `Bqdf`, `Redu`, `gate`, `vrvb`, `inia`, `bota`, `botb`,
>   `botc`, `vech`, `godv`, `may4`, `MBdf`, `Para`, `Pari`, `JaSt`, `FRst`.
> - `comp` and `eq  ` appear **only in ASCII order**, at 0x60089274 and 0x60089280,
>   clustered with `opt `/`filt`/`lim ` around 0x6008923c–0x6008928c. That is a
>   *name table*, not dispatch. They work because they are **block** FourCCs whose
>   **blob** tags — `cpxt` and `Bqdf` — do have handlers. So the anomaly is
>   explained and the earlier inference is rehabilitated.
>
> Against that, **`VoFx` is the only tag in the entire known set absent in both
> byte orders**, and absent as a split `movw`/`movt` pair too. Meanwhile the model
> blobs *are* handled — the `'botb'` handler at 0x60078dce copies seven dwords from
> the blob into the device object at +0x14..+0x2c.
>
> **The most probable explanation, consistent with every measurement:** the io24
> firmware implements the six FX model *parameter* blobs but has **no handler for
> the `'VoFx'` model selector**. The selector write is accepted and silently
> ignored (`SetP` is fire-and-forget by design, so nothing complains), the model is
> never selected, and the model blobs configure an object that is never engaged.
> That accounts for the entire body of evidence: correct wire bytes, working
> reverb on the same return path, a live 350:1-sensitive rig, and a uniform zero
> response across every routing, every parameter and every `'opt '` id.
>
> **Confidence, stated honestly:** this is inference from a tag census plus one
> handler disassembly, not from reading the dispatcher's accept-list end to end.
> It is strong — the census cleanly explains `comp`/`eq  `, and `VoFx` is a unique
> outlier — but the decisive step remains disassembling the `SetP` tag dispatch in
> Thumb-2 (load VA 0x60020000, file offset = 0x216030 + VA − 0x60020000) and
> enumerating what it accepts. Until that is done, treat block 201 as **very
> likely not implemented on the io24**, rather than proven so.
>
> **Firmware-level confirmation (2026-07-31).** The device's own block resolver was
> located and disassembled at `0x6005ce98`. It maps the numeric blocks directly:
>
> | block | resolves to | bound |
> |---|---|---|
> | 0 | the device object itself | — |
> | 100 (mixer) | `dev + 0x3e28 + index*0x74` | `index <= 2`, else NULL |
> | 201 (insert FX) | `dev + 0x11e0` | singleton, index ignored |
> | 202 (reverb) | `dev + 0x3a70` | singleton |
> | 203 | `dev + 0x3f84 + index*0x4c` | `index <= 2` |
>
> Two things fall out. First, this independently confirms `dev+0x11e0` for block
> 201, which had only been asserted before. Second, **the mixer is bounded at
> `index <= 2`** — three buses, and anything higher returns NULL. That is the
> firmware-side proof of the hardware result above, where writing to blockIndex
> 3–6 did nothing.
>
> The insert-FX slot's own method at `0x60070af8` reads a **model pointer from
> `slot+0x14`** and, if it is NULL (`cbz r0`), returns immediately without doing
> any work:
>
> ```
> 0x60070afc  ldr   r0, [r0, #0x14]     ; the currently-selected model
> 0x60070afe  cbz   r0, 0x60070b06      ; none selected -> skip everything
> 0x60070b00  ldr   r3, [r0]
> 0x60070b02  ldr   r3, [r3, #0x10]
> 0x60070b04  blx   r3                  ; otherwise run it
> ```
>
> So the slot is a container that does nothing until something populates
> `slot+0x14` — and on the host, the thing that populates it is exactly the
> `'VoFx'` selector, whose tag does not exist anywhere in this firmware.
>
> **Where the proof stops.** What has *not* been done is demonstrating that
> nothing anywhere writes `slot+0x14`. Establishing that needs proper function
> discovery over the Thumb-2 image; a linear sweep desynchronises and silently
> misses real instructions (it failed to find the very `add.w r3, r0, #0x11e0`
> that was disassembled directly). So the finding stands as a **mechanism plus
> strong circumstantial evidence**, not a closed proof.
>
> A practical consequence either way: the six FX builders in `io24_fx.py` are
> correct and byte-verified against the host, so if a `'VoFx'` route is ever found,
> the models should work immediately.
>
> Lead worth following: `re/param_consumers.txt:155` registers
> `voicefxopt` / `InsertFXSelectorComponent` as a **channel** component, in the
> same list as `limit`/LimiterComponent — so the selector is per-channel, and its
> enable is somewhere the `'opt '` block's ids 0–7 do not cover.
>
> Honest reading: the earlier by-ear result below was real listening, but it
> cannot be reproduced by measurement now, and the most likely explanations are
> that the audible effect was the reverb rather than the insert FX, or that some
> device state present then is absent now (the device has been power-cycled
> since). Treat block 201 as **unverified** until it reproduces.

### Insert FX — original by-ear claim (routing interpretation superseded)

> **2026-08-31 supersession.** The conclusion below that block 201 needs the
> same block-202 send/return path is false for Transformer/Doubler. Firmware
> proves that model 0 owns a private reverb-core member, while block 202 owns a
> separate instance. The older measurements remain historical observations but
> cannot identify which stored state produced the sound. See the controlling
> correction at the top of this file.

> **Correction.** This section previously said block 201 was "an insert, not a send,
> so unlike the reverb it needs no send/return routing", on the strength of the host
> class being named `InsertFXSelectorComponent`. **That is wrong.** With the FX send
> closed and the FX return muted, the delay is completely silent; restoring both
> brings it straight back. Block 201 needs the **same send/return path as the
> reverb**:
>
> - the channel's FX send — `'Para'` wire id **4** + channel index, and
> - the FX return into a bus — mixer **`paramId 5`**.
>
> The first delay test appeared to work without this only because the reverb's
> routing happened to still be open from the preceding test. A class *name* is not
> evidence of signal flow.

Two writes configure it:

```
1. model select :  SetP | block 201 | 'VoFx' size 0x10 | u32 model   (0..5)
2. model params :  SetP | block 201 | <model's own tag>
```

Models: 0 Transformer, 1 De-Tuner, 2 Vocoder, 3 Ring Modulator, 4 Filters,
**5 Delay**.

Delay (model 5), tag `'vech'`, 0x1c bytes:

```
+0x00 tag 'vech'  +0x04 size 0x1c  +0x08 index
+0x0c u32 on      +0x10 f32 mix    +0x14 f32 feedback×0.5   +0x18 f32 time (s)
```

**Verified:** distinct repeats at `time 0.60 s`, and the character changed with
`time` as expected. The **repeats alternate left/right — it is a ping-pong stereo
delay**, which is why an early A/B was reported as "a panning echo thing" rather
than a plain echo. A sustained input turns the discrete repeats into a continuous
wash, as expected.

### EQ

The `'eq  '` block takes one `'Bqdf'` blob per band, with the **band index at
`blob+0x0c`** (firmware bounds-checks 0..3) and the five coefficients from
`+0x10`. The band's *shape* is a host-side choice — the device only ever sees
coefficients — so the driver's API takes the shape directly rather than the
host's `eqtype` enum, whose value-to-shape mapping was never established.

```python
dev.set_eq_band(1, "low",   "lowshelf",  freq_hz=120,  gain_db=+4)
dev.set_eq_band(1, "himid", "peaking",   freq_hz=2500, gain_db=-3, q=1.4)
dev.set_eq_band(1, "high",  "highshelf", freq_hz=8000, gain_db=+2)
dev.eq_off(1)
```

The low-level driver API names bands `low` / `lowmid` / `himid` / `high` (or
0–3) and also retains `hp` / `lp` helper shapes for direct callers. Those two
helpers are not controls in UC's Standard component.

The GTK Host follows UC 4.7.2's embedded `fatchannelxt.xml` `Eqxt4` contract
exactly: `eqallon` is one complete-EQ switch, independent of
`eqbandon1..4`; Low and High alone expose `eqbandop1/4` for shelf versus
parametric mode; Low-Mid and Hi-Mid are fixed parametric bands. All four
frequency ranges are 36–18000 Hz, gain is ±15 dB, and Q is 0.1–10 with 0.6 as
both default and skew midpoint. Defaults are 130, 320, 1400 and 5000 Hz. The
bounded recovered schema is retained at
`re/uc_component_model/fatchannelxt_eq_contract.xml`.

Standard peaking, low shelf, and high shelf are transcribed in binary64
instruction order from UC 4.7.2 designer selectors 6, 8, and 9, including
binary32 input/output boundaries. The API's optional high-pass/low-pass band
shapes still use older helpers and are not Standard component paths.

#### Q overshoot

Every combination the API accepts is numerically **stable** — the whole clamped
space (all six shapes x 22 frequencies x 13 gains x 13 Qs, coefficients rounded
to float32 as they are on the wire) has a worst pole magnitude of **0.99995**,
inside the unit circle, with no non-finite coefficient anywhere.

Stability is not the same as sane gain, though:

| shape | peak gain, Q=0.7 | Q=2 | Q=10 |
|---|---|---|---|
| peaking | +15 dB | +15 dB | +15 dB |
| lowshelf / highshelf | +15 dB | +20 dB | **+33 dB** |
| hp / lp | 0 dB | +6 dB | **+20 dB** |

A shelf at Q>1 is a *resonant* shelf and exceeds the gain requested — at Q=10 a
+15 dB shelf peaks at +33 dB. The range is left as the host defines it rather
than narrowed, but this is worth knowing before putting headphones on.

#### Verified on hardware

Measured against a glitch pedal on input 1, using the fact that **JaSt slot 4 is
pre-DSP** while slot 14 (Mix A) is post-DSP, so

    metric = dB(slot 14) - dB(slot 4)

is a transfer measurement that cancels the source completely. Slot 4's position
was established first, with the already-verified `set_highpass_freq`: engaging a
1 kHz high-pass moved the bus by **-13.38 dB** and the input meter by **-0.09 dB**.

That probe turned out to be extraordinarily quiet — the flat baseline, revisited
**8 times interleaved between EQ states, read -3.00/-2.99 dB every time, a spread
of 0.008 dB**, and drifted +0.001 dB across a 20-point sweep. Results:

| setting | effect |
|---|---|
| low shelf -15 dB @ 300 Hz | **-6.18 dB** |
| low shelf +15 dB @ 300 Hz | **+10.24 dB** |
| high shelf -15 dB @ 3 kHz | -0.18 dB |
| high shelf +15 dB @ 3 kHz | +0.77 dB |
| low-pass 300 Hz | -3.26 dB |
| high-pass 5 kHz | **-24.10 dB** |

The small high-shelf numbers are not a weak result — the source simply has little
energy above 3 kHz, which the same measurement independently confirms.

**Band indexing** was tested by cascading the *same* -6 dB peaking filter on 1,
2, 3 then 4 bands: -0.61, -1.06, -1.51, -2.05 dB, i.e. near-equal increments of
-0.45 to -0.61 dB. Bands that collapsed onto one index would have added nothing
after the first.

**Frequency mapping** was tested by sweeping a narrow notch (-15 dB, Q=8) and
using it as a spectrum analyser. It found a sharp peak at **60.0 Hz** with
further peaks at 180, 300, 420 and 540 Hz — mains hum and its odd harmonics,
picked up by the high-gain pedal on an unbalanced cable. Odd multiples removed
13.4% of total power against 8.6% for the even ones. Landing exactly on 60 Hz,
with harmonics on exact odd multiples, is what shows the Hz value is metrically
correct and not merely monotonic.

**EQ writes are per-channel**, which the single-channel tests above could not
show. Writing a -15 dB shelf and a 5 kHz high-pass to *channel 2* while watching
channel 1 moved channel 1 by **0.010 dB** — noise. The identical write to channel
1 moved it by **5.245 dB**. A ratio of about 500, so `blockIndex` selects the
strip and the per-channel API is real, not a fiction over a broadcast.

A note on why the ratio metric was the right choice: the input gain drifted from
25 dB to 20 dB partway through this session (a hand on the hardware knob), and
the input meter wandered between -10.2 and -14.7 dBFS across the tests. The
baseline transfer never moved outside 0.008 dB. Measuring either meter alone
would have produced 4.5 dB of pure artefact.

**Do not stack shelves at full gain.** Each band is a cascaded biquad, so four
bands each asked for +15 dB give **+60 dB**, not +15. The measurements above used
cuts for exactly this reason. Nothing in the API prevents the stack — the device
clamps each band's parameters, not their product.

The high-shelf designer is now transcribed from UC 4.7.2 selector 9, alongside
peaking selector 6 and low-shelf selector 8. These are source-bound binary64
instruction transcriptions with binary32 boundaries; their vectors are
regressions of the transcription, not an independent captured-UC bit-identity
proof.

### All six FX models

| # | Model | Tag | Size | Parameters | Status |
|---|-------|-----|------|------------|--------|
| 0 | Transformer | `'godv'` | 0x20 | on, lows, width, mix | **waveform verified** |
| 1 | De-Tuner | `'may4'` | 0x24 | on, detune (signed semitones), mix | **exact pitch targets verified** |
| 2 | Vocoder | `'bota'` | 0x28 | on, avol, carrier type/freq, voiced, mix | **waveform verified** |
| 3 | Ring Modulator | `'botb'` | 0x28 | on, 2x carrier freq, dist, vol, mix | **exact sidebands verified** |
| 4 | Filters | `'botc'` | 0x28 | on, tune, feedback, damp, dist, vol, mix | **waveform verified** |
| 5 | Delay | `'vech'` | 0x1c | on, time, feedback, mix | **exact repeat timing verified** |

The model to tag mapping is **proved, not guessed**: the FX factory at `0x180049960`
dispatches through a jump table at `0x180049f34` indexed by `model + 1`, and the same
register that indexes it is written verbatim into the `'VoFx'` select blob.
The current Input-1 campaign independently observes the expected timing,
pitch, sideband, and spectral behavior after those mappings are selected.

**Several models push more than one blob.** Transformer emits two `'Bqdf'` low
shelves alongside `'godv'`; De-Tuner emits a fixed 6 kHz low-pass `'Bqdf'`; Vocoder
emits a `'inia'` blob (0x1c4) carrying a 22-biquad filter bank before its first
`'bota'`. A driver that sends only the model blob gets the effect with its tone
filters bypassed rather than matching the Windows host.

Model 4 is *not* what its name suggests — it emits no biquad coefficients and no
LFO; it is a **feedback comb/resonator**.

Builders for all six, with a self-test, are in [`io24_fx.py`](io24_fx.py).

*Verified by ear:* De-Tuner stepped the pitch dry → −8 semitones → −4 → dry, and
Transformer swung boomy → thin → boomy. Both tests were shaped to return to their
starting point, so the result shows the parameter behaving monotonically and
repeatably rather than merely "something changed".

### Historical preset conclusion — superseded by UC's `PrsM` Store route

This section originally concluded that the device did not store preset bodies.
That conclusion was too broad. The experiments below correctly show that moving
`activePresetSlotIndex` does not itself replay state and that the attempted
GetP-style queries return no body. They did not exercise UC's later-recovered
SetP `MemP/PrsM` Store route. See the 2026-09-20 controlling result at the top.

Two experiments, both negative:

1. **Switching the slot moves no state.** Writing `activePresetSlotIndex`
   (`'Pari'` wire id 16) 0→1, 0→2 and 0→3 changed exactly one thing each time:
   `JaSt` slot 40, the index itself. 489 stable slots were monitored across the
   transitions and not one of them moved.
2. **No block answers a preset query.** A read-only sweep of `'PrsM'`, `'Retr'`,
   `'Stor'`, `'List'`, `'Rena'`, `'Dele'`, `'PrsC'`, `'Pset'` and `'Cach'` across
   eight blocks returned nothing anywhere.

Universal Control does hold a complete record on the host and pushes ordinary
component setters on RestorePreset. It also sends a separate write-only `PrsM`
Store transaction. The Linux Host follows both behaviors. Because the io24 does
not return stored bodies or most DSP state, a Host preset can contain only what
the driver read or itself retained. Readable `'Appl'` values — gain, volumes,
blend, phantom — are captured live from `JaSt`; everything else comes from a
mirror of our own writes, kept in `~/.cache/io24/shadow.json` so that it survives
across CLI invocations. `io24.py shadow` prints it; `io24.py shadow clear` resets
it, which is what to do after a power cycle or after Universal Control has driven
the device from another host.

The mirror is keyed by *what a call addresses* — channel, band, mixer
source/bus, compressor instance — so a second write to the same target replaces
the first while per-channel and per-band settings coexist. Only leaf setters are
recorded; `mix_off`, `eq_off` and `compressor_off` call through to them, so the
mirror always holds the canonical primitive call.

*Verified on hardware:* save → move every parameter elsewhere → load restored all
seven live values exactly (`io24.py savepreset` / `loadpreset`, and the same over
the daemon socket).

## 10. Current open questions

**Resolved** since the first draft: the `SetP` paramId space (§6a); the blob
`index` semantics and bounds-checking (§9a); the full `JaSt` slot map (§6); the
six DSP blocks (§9a); the compressor models, `'gate'` unit conversions and limiter
release formula (§9b); the mixer wire form, `fader` taper and pan law (§9c, §9d);
the metering datagram (§9d); the location of reverb and insert FX (§9d); and the
UCNET shim, which is now built and hardware-verified (§9).

**Remaining, in rough order of user impact:**

1. **Device Presets persistence and body readback.** UC's exact `PrsM` Store
   route is implemented and receives a transport reply, but the io24 exposes no
   stored-body read command. Cold-boot persistence, a durable device commit, and
   standalone VoiceFX recall therefore remain unproved. Linux records the honest
   status `WRITE_SENT_UNVERIFIED` and replays known bodies on Load.
2. **VoiceFX Input-2 acceptance.** All six models are objectively verified
   through physical Input 1. The earlier Input-2 Delay run did not change UC's
   assignment from Input 1, so it did not test the repaired route. The Host now
   performs the exact assignment-before-state sequence; a fresh physical
   Input-2 waveform run remains pending.
3. **No faithful io24 command for several UC model fields.** The unresolved set
   is mono-source `pan`, `stereopan` width/mono collapse, independent per-input
   `FXA`, `dawpostdsp`, output mono fold-down, writable component names, and the
   physical Main-mute latch. Names, Auto gain, and Mirror Main are explicit
   Host state; nearby device fields are not substituted. Block 100 also has no
   readback, so mixer writes remain shadow-backed.
4. **Pending audible acceptance.** Passive and Vintage EQ have exact UC
   designers and packet routes but still need a dedicated audible A/B. The Host
   spring reverb has hardware-free graph/processor coverage but still needs its
   live Main-output acceptance run.
5. **96 kHz DSP coverage.** The device clocks at 96 kHz and the Host Mix A/B
   source smoke passed there, but every device DSP block has not been swept at
   that rate. Hardware Delay is deliberately blocked after its model selection
   reset the unit into its bootloader. The desktop Host substitutes its safe
   Delay insert; live acceptance of that fallback and the exact firmware cause
   remain open.
6. **Compressor knee semantics.** The measured transfer curve and emitted
   coefficients are correct; the knee field's meaning remains inferred from the
   firmware algebra rather than directly measured in the coprocessor audio path.

---

## 11. References

- [oddbear/Revelator.io24.Api](https://github.com/oddbear/Revelator.io24.Api) — UCNET
  (host↔service) protocol; the correct starting point, and the reason the USB layer
  turned out to be a different problem.
- PreSonus Universal Control v5.0.0 installer (NSIS; extract with `7zz`) —
  contains `hwaccess/dspusbdevice.dll` and the embedded device firmware images.

*Reverse-engineered for interoperability: enabling hardware its owner already
possesses to work on Linux. Two files in `re/` are extracted from PreSonus's
software rather than written here — the decompilation in `param_consumers.txt`
and the factory presets in `uc_factory_presets.json`; see the README's licence
section. An earlier version of this line claimed no PreSonus code was
redistributed at all, which was untrue of both.*

---

## 12. Hardware inventory (2026-08-01)

Answers to a structured inventory query. Several premises in that query assumed
an XMOS design; the evidence says otherwise, so the XS2/XS3 branch does not
apply to this device.

### A. Chip identity — it is ARM Cortex-M, not XMOS

The io24's embedded firmware image (DLL file 0x216030..0x323a70, load VA
0x60020000) begins with a textbook **ARM Cortex-M exception vector table**:

```
word0 = 0x20000600   initial stack pointer, in the standard Cortex-M SRAM region
word1 = 0x60020335   reset vector, bit0 = 1 (Thumb)
words 1..7           7 of 7 odd AND pointing inside the image
```

Corroborating evidence, all gathered while decoding the protocol:

* The whole image disassembles cleanly as **Thumb-2** and yielded correct,
  hardware-confirmed behaviour (the block resolver at `0x6005ce98`, the FX slot
  method at `0x60070af8`, the ring-mod handler at `0x60078dce`).
* It uses **VFP floating-point instructions** — `vmov.f32`, `vdiv.f32`, `vldr`
  appear in the reverb and biquad code. So this part has a hardware FPU and is
  not fixed-point-only.
* `XMOS` appears **twice in the host DLL and zero times in the io24 image**. The
  DLL serves the whole Revelator family, so those two hits are the sibling-device
  confound the query anticipated. No `xcore`, `XS2`, `XS3`, `XU216`, `XU316`,
  `lib_xua`, `lib_usb_audio` or `tile[` anywhere.
* No ADI / SHARC / ADSP / Blackfin / TI / Tensilica markers either, so the
  two-processor hypothesis has no support from strings.

**USB identity:** `194f:0422`, PreSonus, "Revelator IO 24", `bcdDevice 1.28`,
serial redacted (per-unit), USB 2.0. Not an XMOS vendor ID.

### B. Firmware image structure

Not ELF — the image is a **raw Cortex-M binary** starting at its vector table,
not an ELF with per-tile sections. `readelf`/`objdump` are the wrong tools; the
correct approach is what this project already does, namely load it at
0x60020000 and disassemble Thumb-2. The scattered `jffs2`/`gzip` magic hits are
byte coincidences in 1 MB of code, not filesystems. **One payload, one
processor** — no second image inside.

**Capability sweep is a clean negative.** Of `multiband`, `crossover`, `fft`,
`rta`, `spectrum`, `Hann`, `Hamming`, `Blackman`, `window`, `ifft`, `linkwitz`,
`Butterworth` — **none** appear in the device's firmware. Only `band` (x66),
which is EQ-band handling. There is no evidence of any frequency-domain or
multiband machinery in this device.

**Linux Host implementation.** Multiband is therefore a computer-side input
insert, not a decoded device block. `io24_mbc.py` splits each post-Fat-Channel
capture into four phase-compensated LR4 bands. Each band independently selects
Standard, Tube or FET and retains all three public parameter sets. The existing
UC-derived `cpxt_comp`, `cpxt_tube` and `cpxt_fet` builders compile the selected
model into the common 11-float tuple documented in §9b; `io24_uc_comp.c` consumes
that tuple in a bundled mono LADSPA processor before the bands are summed and
returned on USB playback 1-2. Its RMS 4:1 steady-state transfer reproduces the
measured −7.30 dB point, and its soft-knee equation uses the firmware-derived
`0.5/sqrt(K)` and `0.5*sqrt(K)` constants. This validates that measured
Standard transfer point, the implemented knee equation, and the Host graph
without inventing a firmware multiband command.

**Live Host acceptance (2026-09-20).** The exact production graph was then
measured through a guarded physical Main-L -> Input-1 loop with a -30 dBFS,
1 kHz stimulus. Bracketing crossover-only captures drifted by 0.0438 dB;
Standard, Tube and FET changed the tone by -0.8950, -4.9941 and +1.4821 dB.
The initially preregistered "all models reduce by at least 3 dB" classifier was
wrong: a hardware-free pass through the same production graph gives -2.0195,
-5.0000 and **+4.3495 dB** respectively. The positive FET result follows the
recovered UC fixed-threshold and auto-makeup law rather than indicating a
reversed control. The corrected criterion requires the expected model-specific
sign plus at least 0.5 dB and three times the observed baseline drift. The
immutable captures therefore classify all three models audible, with exact
device/PipeWire restoration and no listening judgment.

### C. DFU surface — download only

Interface **6** is a real DFU 1.10 interface ("Revelator IO 24 DFU"), present in
the normal configuration alongside audio.

| field | value | meaning |
|---|---|---|
| `bmAttributes` | 13 | **Upload Unsupported**, Download Supported, Will Detach, Manifestation Tolerant |
| `wTransferSize` | 4096 | |
| `wDetachTimeout` | 2000 ms | |

**C12 answered: upload is not supported.** The device accepts a firmware write
but will not read its flash back, so DFU gives no recovery baseline and no
before/after diff target. **C14:** DFU sits in the normal configuration and is
entered by `DFU_DETACH` in software, not by an unconditional button-hold — the
losable kind. Nothing here was exercised; this is descriptor reading only.

### E. Compiled constant vs addressable field

All 117 host descriptors dumped and grouped into 26 contiguous blocks.

* **E22 — compressor.** The StudioLive XT block has 7 fields: threshold, ratio,
  attack, release, gain, reduction (read-only) and **`kneewidth`** (id 9, max
  3.0). `kneewidth` is a *field* UC never renders — but its setter is already
  documented in §9b as a **no-op** (jump-table entry 9 is the default return at
  `0x18000eae3`). Present but inert: the knee is chosen solely by the softknee
  toggle.
* **E23 — sidechain Q resolves negative.** The gate exposes `keyfilter`
  (frequency, 40–16000 Hz) and so do all three compressor models — but **neither
  exposes a Q field**. The hypothesis "if one exposes Q and the other does not,
  the field provably exists" fails because *neither* does. Q is a compiled
  constant on both sides; the host bakes it (see `io24_dsp.key_filter`).
* **E24 — Doubler parameter surface.** The block exposes exactly three
  continuous fields—Lows, Width, and WetDry—plus On. There are no separately
  serialized reverb parameters. The 2026-08-31 firmware trace explains why:
  the model owns a private reverb core configured from those high-level fields.
* **E21 — fields present but unrendered, worth following up:**
  * `autogainmode` (id 141, "Automatic Preamp Gain Mode") — a device feature
    this driver does not expose at all.
  * `pan` (143) and `stereopan` (144, "Stereo Width").
  * `aux1` / `aux2` (124/125, "Mix A Level" / "Mix B Level") — bus master
    levels, distinct from the per-source levels this driver already sets.
  * `avoiced` (vocoder) and `bcarrier2freq` (ring mod sub-carrier) — moot while
    block 201 is inert.

### D. Headroom — not yet measured

The 96 kHz test (D16) has not been run. Note that D18's premise — fixed
compile-time logical cores — is XMOS-specific and does not apply to a Cortex-M
part. Worth measuring rather than reasoning about, and cheap to do: set the
device to 96 kHz and check whether any block stops responding.

## 12F. `aux1` / `aux2`, and the device's real sample-rate range (2026-08-01)

### The aux buses are not a separate control

§12E's descriptor dump lists `volume` (id 106), `aux1` (124) and `aux2` (125) as
three float parameters, min −96, max +10, unit `gain`, taper `fader`. §10 item 0
recorded a guess that 124/125 were Mix A / Mix B **master** levels — one control
per bus, sitting above the per-source sends. **That guess was wrong.**

Probed on hardware. A calibrated −20 dBFS tone was driven into the USB return
and routed to all three buses at unity, so main, Mix A and Mix B all metered
−20.00 dB (`JaSt` slots 12/13, 14/15, 16/17). Each candidate was then written at
−80 dB and restored, A-B-A, with every state slot watched — not just the ones
believed to be meters, and with the restore required to recover, so that source
drift cannot read as a hit the way it did in the first `'opt '` sweep (§9c):

| candidate | slots that dropped and recovered |
|---|---|
| block 100, index 0/1/2, paramId 124 | none |
| block 100, index 0/1/2, paramId 125 | none |
| block 100, index 0/1/2, paramId 106 | none |
| block 100, index 0/1/2, paramId 145 (`FXA`) | none |
| `'Appl'`, index 0, paramId 124/125 | none |

Nothing moved anywhere. **124/125 are not wire paramIds** — consistent with §6a,
which already establishes that descriptor ids and wire ids are different spaces.
Block 100's paramId space is the *source* slots 0..6, and the bus is carried in
`blockIndex`, so a number as large as 124 has nowhere to land there.

What they actually are is settled by Universal Control's own object model
(`re/UCNET_SHIM_SPEC.md`): every channel carries
`{volume, mute, solo, lr, assign_aux1, assign_aux2, aux1, aux2, FXA, ...}`. So
`aux1` is *channel N's send to the first aux bus*, not a master. On the wire that
is block 100 at `blockIndex` 1, which this driver has driven as `mixa` since §9c.
The three descriptor ids ascend (106 < 124 < 125) exactly as the three bus
indices do (0 < 1 < 2), and the firmware's resolver bounds the block at index 2,
so there are three buses and no room for a fourth. Mapping `aux1 → mixa` and
`aux2 → mixb` follows from that ordering; it is an inference, not a measurement,
because confirming UC's own labelling would mean running UC.

The remaining UC controls in that model split two ways, and the split matters:

* `assign_aux1/2` and `enableChannelAssign` appear in no descriptor table found
  so far. They look like host-side concepts UC implements on top of the same
  per-source levels — an assign is just "write the off sentinel, remember the
  fader".
* `aux1_mirror_main`, `aux2_mirror_main` and `auxMuteMode` **do exist as device
  parameters**, in the *global* descriptor table at `0x1800e9660` — ids 3, 4 and
  9, alongside `phonesSrc`(0), `phonesMute`(1), `monitorBlend`(2),
  `outputDelay`(5), `outputDelayBus`(6), `phonesVolume`(7) and
  `presetButtonMode`(8). **These have not been probed.** `re/io24_params.json`
  does not list them because its extraction regex dropped camelCase, boolean
  (kind=0) and id=0 entries — a known gap, and the reason a "not in the
  descriptor table" argument is weak evidence here. Anything in that table is a
  candidate for a real device-side control, including a genuine mirror-to-main
  latch and an output delay this driver does not expose at all.

Later UC 4.7.2 code recovery supersedes the one-shot implementation described
here: Linux now keeps a persistent Host latch. While enabled, subsequent Main
level/assign/balance edits are folded into the aux cells; the aux's own hidden
mix remains retained and is restored when the latch clears. No safe io24 wire
command for global ids 3/4 has been proved, so this does not claim that the
latch survives after the Host closes.

The driver now models the same three layers UC shows, folding them into the one
number the hardware takes:

    off               if not assigned
    send + master     otherwise, clamped to the block's −144..+10

Verified on hardware against the bus meters:

| claim | result |
|---|---|
| `aux1`/`aux2` reach the Mix A / Mix B meters | all three buses −20.00 dB |
| unassigning affects one bus only | Mix A → −179.7, main and Mix B unmoved |
| the send level survives an unassign | restored to −20.00 exactly |
| a bus master offsets the bus | −6 / −12 / −20 dB, **error 0.00 dB** |
| a bus master does not leak | main and Mix A moved 0.00 dB |
| `mirror_main` copies main's levels | −35.0 dB copied into Mix A |

The master and the assign are host-side: block 100 holds per-source levels and
nothing above them, so a master costs one USB write per source, and it can only
move sends this driver has set — an unset send has no known position to offset.

A bug this exposed, worth recording because it is the kind that hides: the write
mirror keyed its entries on the *raw* bus argument, so the same send reached as
`aux1` and as `mixa` filed two separate entries and the model was lost across
processes. `_shadow_key` already normalised EQ band names for exactly this
reason; it now normalises bus aliases the same way.

### Sample rate: the device does 96 kHz, and PipeWire was never the obstacle

§12D's headroom test needed the device at 96 kHz, and an earlier attempt here
concluded that PipeWire "would not switch rate". **That conclusion was wrong, and
the error was in the measurement, not the software.**

The device advertises four rates in its USB descriptors, and honours all of them:

    Altset 1  Format: S32_LE  Channels: 6  Bits: 24
    Rates: 44100, 48000, 88200, 96000

Playing directly to `hw:CARD=R24` at 96 kHz, `/proc/asound/card*/pcm0p/sub0/hw_params`
reports `rate: 96000 (96000/1)` and the device's own clock reports back
`Momentary freq = 95998 Hz`. The hardware switches.

Through PipeWire it also switches, once `clock.allowed-rates` is widened (the
stock configuration allows 48000 only):

```
# ~/.config/pipewire/pipewire.conf.d/10-io24-rates.conf
context.properties = {
    default.clock.allowed-rates = [ 44100 48000 88200 96000 ]
}
```

| `clock.force-rate` | device `hw_params` rate |
|---|---|
| 96000 | 96000 |
| 88200 | 88200 |
| 44100 | 44100 |
| 48000 | 48000 |

Two things made the earlier attempt read as a failure, and both are worth
knowing before anyone re-runs this:

1. **`clock.rate` is the wrong number to read.** It is the server's *default*
   rate and stays at 48000 no matter what the graph is doing. The rate the device
   is actually clocking at is in `hw_params`, or in `Momentary freq`.
2. **A forced rate needs something to run.** With every node `suspended` there is
   no graph to re-rate, so forcing a rate and immediately reading back shows
   nothing. Start a stream, then look.

The mixer app read `clock.rate` too, so its selector snapped back to 48 kHz after
every successful change; it now prefers the live `hw_params` rate, falls back to
`clock.force-rate` when the device is idle, and only reports `clock.rate` when
there is nothing better.

For the record, since it comes up: **ASIO is Windows-only and has no Linux
implementation.** On Linux the stack is ALSA in the kernel and PipeWire (or JACK)
in userspace; PipeWire is the correct layer for this, not a compromise. Nothing
about the io24 needs a driver model it does not have.

## 12G. The global descriptor table, and how to crash this device (2026-08-02)

### SUPERSEDED — see §12I. The cause below was wrong

**Everything in this subsection attributing the bootloader drop to unmapped
parameter writes is incorrect**, and is kept only because the measurements around
it are still good. §12I disassembles the firmware's own `'Para'` dispatcher and
shows that wire ids 5-9 and 11-13 branch to a shared `add sp,#0x34 ; pop
{r4-r7,pc}` — writing them does nothing whatsoever, so they cannot have caused
anything. The device dropped into its bootloader a **second** time afterwards
during a run that wrote only `'Para'` 14 and `'Pari'` 13, both real, mapped,
in-range parameters, which rules the parameter theory out on its own.

What both incidents share is heavy `aplay`/`arecord` streaming concurrent with a
stream of control writes, and in both the kernel logged the **audio** interface
failing first (`clock source 5 is not valid`, `cannot get freq: err -110`) before
any disconnect. The cause is transport-level, not parametric.

### The original (wrong) reasoning, retained for the record

**Writing an unmapped wire parameter id crashed the io24 into its bootloader.**

A sweep wrote the value `100` to nine unmapped `'Para'` ids (5–9, 11–14) and
eight unmapped `'Pari'` ids (8, 10, 11, 13, 14, 17–19), restoring each to 0
afterwards. The sweep completed and the device still metered correctly. Shortly
after, the kernel logged:

```
usb 3-1: uac_clock_source_is_valid(): cannot get clock validity for id 5
usb 3-1: clock source 5 is not valid, cannot use
usb 3-1: 1:1: cannot get freq (v2/v3): err -110
usb 3-1: 1:1: cannot set freq 48000 (v2/v3): err -110
usb 3-1: USB disconnect, device number 2
usb 3-1: New USB device found, idVendor=194f, idProduct=0405, bcdDevice= 1.15
usb 3-1: Product: Revelator IO 24 BOOTLOADER
```

The audio clock went invalid first, then the firmware stopped answering, then
the device re-enumerated as **`194f:0405` "Revelator IO 24 BOOTLOADER"** with a
flashing LED and a blank screen, exposing only DFU interfaces.

**It recovered completely from a cold power cycle** — unplug, wait, replug — and
came back as `194f:0422` with `bcdDevice 1.28`, full control interface, 503
readable state slots and no clock errors. The application flash was intact; this
was a watchdog state, not corruption. A replug through a still-powered hub is not
enough: the disconnect must actually reach the device.

Two corrections to earlier reasoning in this document follow from it:

1. **"The firmware clamps out-of-range values, so a wrong number cannot hurt"
   is only true for parameters that have a declared range.** §8 states this as a
   general safety property. It is not one. An unmapped id may have no descriptor
   behind it and therefore no clamp, and one of these ids plausibly selects a
   clock source or rate index — writing 100 to that produces exactly the
   "clock source 5 is not valid" failure observed.
2. **There is no safety net for this class of mistake.** The DFU descriptor
   reports `Upload Unsupported` / `Download Supported`, so the device will accept
   a firmware write and will never hand its existing flash back. No baseline can
   be taken before probing, and a device that did not recover on a power cycle
   would need the vendor's own image.

`set_param()` now refuses any id outside its mapped tables unless the caller
passes `unsafe=True`. That flag is not a formality — use it only from a probe you
are prepared to power-cycle out of, with the device's screen in view.

### `Pari` 12 — a real new control

Sweeping the accepted wire space against the state blob found exactly one
unclaimed id that answers: **`'Pari'` 12**. It is `index`-scoped (per channel)
and writes a value clamped to {3, 4} into `JaSt` slots 38 and 39:

| write | slot 38 | slot 39 |
|---|---|---|
| `Pari 12` idx 0 = 0 | 3 | 4 |
| `Pari 12` idx 0 = 1 | 4 | 3 |
| `Pari 12` = 2 and above | 4 | 4 |

3 and 4 are the mixer source ids for `line/ch1` and `line/ch2` (§9c), so slots
38/39 hold *which source each channel strip is bound to*. It is close to
`processingChannel` in UC's object model, and explains why `link` (`'Pari'` 9)
also moves slot 38.

Confirmed functionally, with input 1 made loud by running its preamp at 60 dB
and input 2 left quiet, `line/ch1` routed to Mix A and `line/ch2` to Mix B:

| state | slots (38,39) | Mix A | Mix B |
|---|---|---|---|
| default | (3, 4) | **-91.4 dB** | -179.7 dB |
| swapped | (4, 3) | **-143.5 dB** | -153.1 dB |
| restored | (3, 4) | -91.6 dB | -179.7 dB |

A 52 dB change on Mix A, reproducible on the restore. The `index`/value semantics
resolve consistently: the value names the input (0 = input 1, 1 = input 2) and
the firmware keeps the two channels a permutation of each other, which is why
`idx 0 = 1` and `idx 1 = 0` both land on (4, 3).

Stated at the confidence it earned: **`'Pari'` 12 re-binds which source feeds a
channel, and the effect is large and repeatable.** What is NOT established is
where in the chain it acts. If it simply swapped the two inputs, the noise should
have reappeared on Mix B at about -91 dB; it only reached -153. So the swap does
not cleanly hand input 1's signal to channel 2, and calling this exactly
`processingChannel` would be claiming more than was measured.

### A measurement bug worth not repeating

The first pass over the whole wire space reported **nothing for every id,
including controls already proven to work**. The cause: the state blob is not all
floats. Integer parameters are stored as their raw bit pattern in a float slot,
so `presetSlot = 1` reads back as `1.4013e-45`, a denormal. The sweep tested
`abs(delta) > 1e-9`, which makes every integer slot mathematically invisible, and
the result was a clean sheet of false negatives.

The blob is a mix of genuine floats (meters, gains) and packed ints (flags,
modes, indices), so **the only comparator valid for both is exact equality on the
32-bit pattern**, with meter jitter rejected by taking the modal value across
reads rather than the mean — averaging denormals is meaningless. Redone that way,
the sweep recovered every known control correctly (phantom → slot 50 bit 0,
presetEnable → slot 42 bit 5, hpMute → bit 2, hpf → bit 3, link → slots 38 + 42,
presetSlot → slot 40), which is what makes its one new hit believable.

### `outputDelay`: not found on `'Appl'`, and not ruled out either

No unmapped wire id delayed either loopback bus. The test was a differential
loopback measurement, which is worth describing because it is sample-exact and
immune to process start jitter: the two loopback buses arrive on different
channels of the *same* capture stream, so cross-correlating them cancels the
unknown `aplay`/`arecord` start offset. Baseline repeatability was **lag 0
samples, correlation quality 1.000, over four runs**, and every id under test
held that exactly.

What that rules out is narrow, and the limits should be stated plainly:

* it cannot see a delay applied **equally to both** loopback buses;
* it cannot see a delay applied **only to the analog outputs**, which is the
  likeliest place for an output delay to live and cannot be observed without a
  physical loopback cable;
* it only covers the `'Appl'` block. The global table is
  `JacksonGlobalComponent` — a separate component — and its parameters may be
  addressed through a block FourCC this driver has not identified.

So `outputDelay`, `aux1_mirror_main`, `aux2_mirror_main` and `auxMuteMode` remain
**unreached, not disproven**. The next step is finding the global component's
block address rather than sweeping more ids on `'Appl'` — and given how the last
sweep ended, that search should be done by reading the host binary, not by
writing to the device.

### Corrected: loopback bus to USB capture channel mapping

Measured by routing a tone into one bus at a time and reading all six capture
channels:

| bus | USB capture channels |
|---|---|
| Mix A | **3–4** |
| Mix B | **5–6** |
| (analog inputs) | 1–2 |

This document and `io24gtk.py` previously said Mix A → 5–6 and Mix B → 1–2, which
was wrong in both entries.

## 12H. The device's real parameter inventory, from its own firmware (2026-08-02)

Extracted without touching the hardware. `dspusbdevice.dll` comes out of the
Universal Control installer by the recipe in `re/README.md`, and the io24's
Cortex-M firmware is embedded in that DLL's `.rdata` at file
`0x216030..0x323a70`, load VA `0x60020000`. Both were analysed offline.

### The parameter tables

The firmware carries **two descriptor tables**, 0x54 bytes per record, laid out
`[u32 kind][u32 id][char name[]] ... [f32 max][...]`, where `kind` 0 = bool,
1 = int/enum, 3 = float. Together they are the complete device-side parameter
set — **40 parameters**:

**Table @fw 0x66bc8 — 23 records**

| id | kind | name | id | kind | name |
|---|---|---|---|---|---|
| 0 | int | `input1SlotIndex` | 12 | float | `monitorMix` |
| 1 | int | `input2SlotIndex` | 13 | float | `input1Gain` (max 60) |
| 2 | int | `presetMode` | 14 | float | `input2Gain` |
| 3 | float | `input1FxMix` | 15 | bool | `input1HighPassFilter` |
| 4 | float | `input2FxMix` | 16 | bool | `input2HighPassFilter` |
| 5 | bool | `muteMode` | 17 | bool | `usbStatus` |
| 6 | bool | `input1Mute` | 18 | bool | `channelLink` |
| 7 | bool | `input2Mute` | 19 | int | `input1ProcessingChannel` |
| 8 | bool | `hpOutputMute` | 20 | int | `input2ProcessingChannel` |
| 9 | bool | **`mainOutputMute`** | 23 | int | **`outputDelayBus`** (−1..4) |
| 10 | float | `hpVolume` | 24 | float | **`outputDelay`** (0..0.5, step 0.002) |
| 11 | float | `mainVolume` | | | |

**Table @fw 0x68c4c — 17 records**

`volumeKnob`(41), `hpVolume`(10), `input1/2PhantomPower`(27/28),
`input1/2LevelMeter`(29/30), `output1/2LevelMeter`(31/32),
`input1/2ReductionMeter`(33/34), `input1/2ClipIndicator`(35/36),
`output1/2ClipIndicator`(37/38), `encoderAssignment`(42),
`input1/2PresetSelect`(39/40).

Ids 21, 22, 25, 26 do not appear in either table.

### What this settles

* **`outputDelay` is real, and it is a device feature.** Float, **0 to 0.5
  seconds in 2 ms steps**, with a companion **`outputDelayBus`** selector over
  −1..4. This is the control Universal Control puts on its sample-rate page, and
  the reason to want it is co-host / stream sync.
* **`mainOutputMute` (id 9) is a device parameter this driver does not expose**,
  and it is distinct from `hpOutputMute` (id 8) — which matches the hardware
  button being a main-out mute, as the user reported and §12 records.
* **`input1/2ProcessingChannel` (19/20) confirms `'Pari'` 12 by name.** The
  earlier identification, made from state-slot behaviour alone, was right.
* **`aux1_mirror_main`, `aux2_mirror_main`, `auxMuteMode`, `phonesSrc`,
  `presetButtonMode` and `muteButtonMode` appear in the host DLL but NOT in the
  device firmware.** They are host-side concepts, exactly like `assign_aux1`.
  That retroactively justifies implementing `mirror_main` host-side in §12F —
  there is no device parameter it could have been bound to.

### There is no separate "global component" block

The question that prompted this — what block address reaches the global
component — has a flat answer: **there isn't one.** `outputDelay` and
`outputDelayBus` sit in the *same* table as `hpVolume`, `mainVolume`,
`monitorMix` and `input1Gain`, which is the table already reached through
`'Appl'`. The block address is `'Appl'`, which the driver has used all along.
Only the **wire paramId** is unknown.

### Why the wire id could not be recovered from the binary

The wire id space is neither the descriptor id space nor the firmware's internal
id space. Confirmed on hardware, read-only and safely, by writing mapped ids and
watching which named parameter moved:

| wire | moves | internal id |
|---|---|---|
| `'Para'` 1 | `hpVolume` | 10 |
| `'Para'` 2 | `mainVolume` | 11 |
| `'Para'` 10 | `monitorMix` | 12 |

so wire 1 → internal 10 and wire 10 → internal 12: a genuine third numbering.

Everything that could be read from data was read, and none of it carries the
mapping:

* The DLL holds **three byte-identical copies** of the firmware's table
  (`0x15a170`, `0x27cbf8`, `0x393c70`). Their ids match the firmware's exactly —
  no translation table there.
* There is **no array of pointers to the descriptor records** anywhere in the
  firmware, so the wire id is not an index into such an array.
* The FourCC constants (`'Appl'`, `'SetP'`, `'GetP'`, `'Rply'` at fw
  `0x2d600`..`0x2d610`) sit in **Thumb literal pools**, not in a dispatch table.

The emitter was located and decoded, at DLL `0x53580` (int) and `0x53600`
(float). It builds the TLV as

    [+0x00] tag 'Para'/'Pari'   [+0x04] 0x14
    [+0x08] index  <- rdx[0x0c] [+0x0c] paramId <- rdx[0x08]
    [+0x10] value

and its caller at `0x56b70` chooses the tag with `cmpl $0x3,(%r8)` — testing the
`kind` field of a descriptor record. So **the wire paramId comes from a host
binding object at offset +0x10, populated at construction time**, not from any
static table. Recovering it means following that construction path through the
DLL's object graph, which is a larger disassembly job than this pass.

`GetP` is not a shortcut: probed read-only across `'Para'` ids 0..15, the device
replies `Rply|Appl|Para|0x14|...` with a **zero value every time**. It echoes the
request and does not report the parameter, consistent with the block being
write-only.

### What remains, and the safe way to do it

Nine unmapped `'Para'` ids remain (5–9, 11–14), and `outputDelay` is a float, so
its wire id is among them. The obvious test is to write each candidate and
measure — and the differential loopback rig from §12G can detect it **if**
`outputDelayBus` happens to point at a loopback bus.

That test must be done at an **in-range value**. `outputDelay`'s declared maximum
is 0.5; writing `0.5` is a legitimate value for the parameter and modest for
anything else in these tables (most cap at 1, gain at 60). The 2026-08-02
bootloader crash (§12G) came from writing **100** to ids with no descriptor
behind them, which is a different act entirely — but the device is the only
witness either way, so the run should be done with the unit's screen visible and
a replug ready.

## 12I. The wire id space, solved from the firmware's own dispatchers (2026-08-02)

§12H concluded that the wire paramId mapping "could not be recovered from the
binary" and pointed at the host DLL's object graph. That was looking in the wrong
binary. **The device firmware decides what a wire id means**, and it does so in
two compact dispatchers that can simply be read.

### `'Para'` — FW VA 0x6004e36e

```
subs r1,#1 ; cmp r1,#13 ; bhi <reject> ; tbb [pc,r1]
table: 41 4d 59 07 33 33 33 33 33 6e 33 33 33 35
```

Fourteen byte offsets, so wire ids **1..14**, matching the bound this document
already recorded. Each arm sets `r4` to the kind and `r1` to the firmware's
internal id before calling setParam:

| wire | arm | internal | parameter |
|---|---|---|---|
| 1 | 0x2e3f4 | 10 | `hpVolume` |
| 2 | 0x2e40c | 11 | `mainVolume` |
| 3 | 0x2e424 | 13/14 | `input1/2Gain` (indexed) |
| 4 | 0x2e380 | 3/4 | `input1/2FxMix` (indexed) |
| **5-9, 11-13** | **0x2e3d8** | — | **`add sp,#0x34 ; pop {r4-r7,pc}` — NO-OP** |
| 10 | 0x2e44e | 12 | `monitorMix` |
| **14** | **0x2e3dc** | **24** | **`outputDelay`** (`movs r4,#3 ; movs r1,#24`) |

### `'Pari'` — FW VA 0x6004e484

```
subs r1,#4 ; cmp r1,#15 ; bhi <reject> ; tbh [pc,r1,lsl #1]
```

Note this one is `tbh` (E8DF F011), a **halfword** table of 16 entries, so wire
ids **4..19**. Indexed pairs load both internal ids and select with the blob's
`index` field, which is what makes them recognisable:

| wire | arm | internal | parameter |
|---|---|---|---|
| **5** | 0x2e57c | **15/16** | `movs r2,#15 ; movs r3,#16` → `input1/2HighPassFilter` |
| 6 | 0x2e5b2 | 8 | `hpOutputMute` |
| **7** | 0x2e5f8 | **6/7** | `movs r2,#6 ; movs r3,#7` → `input1/2Mute` |
| **13** | 0x2e7be | **23** | **`outputDelayBus`** |
| 10, 14, 19 | 0x2e544 | — | `add sp,#0x3c ; pop {r4-r11,pc}` — NO-OP |

This settles a question that hardware testing could not. `'Pari'` 5 is the
high-pass and `'Pari'` 7 is the mute — **which is what `io24.py` has always
done**; a `KNOWN_PARI` label table added earlier the same day had the two the
wrong way round, and only the labels were wrong.

It also explains why an audio test could not tell them apart: the `'Appl'`
high-pass is only an *enable*, and its cutoff lives in the separate `'filt'`
block (§9a). At the default 24 Hz, enabling it does nothing audible to broadband
noise. Measured with a verified 60 dB preamp gain and a −88.8 dB bus, wire 5 gave
−2.05 dB and wire 7 gave +1.54 dB, both inside the ±2 dB spread of the restores.
The state blob is the better witness here: wire 5 changes nothing (the high-pass
is not serialised into `JaSt`) while wire 7 sets slot 42 bit 3.

### `outputDelay`, measured

`'Para'` 14 with `'Pari'` 13 selecting the bus, against the differential loopback
rig of §12G (baseline 0 samples, correlation 1.000):

| `outputDelayBus` | requested | measured lag |
|---|---|---|
| −1, 0 | 50 ms | 0.00 ms — neither loopback bus |
| **1** | 50 ms | **+49.50 ms** — Mix A held back |
| **2, 3, 4** | 50 ms | **−49.25 ms** — Mix B held back |

The sign inverting between bus 1 and bus 2 is exactly a per-bus delay, and the
magnitude matches the request to within the 2 ms quantisation the descriptor
declares. Exposed as `set_output_delay()` / `set_output_delay_bus()` and
`io24.py delay <ms> [bus]`.

Two honest limits: the settings that measured 0.00 ms are not proven inert, only
unobservable — the analog outputs cannot be seen without a loopback cable — and
the bus numbering above is what was measured, not a decoded enumeration.

### Method note

The general lesson is the one §12H got backwards: for a question about what the
*device* does with a wire value, the device's firmware is the authority and the
host binary is hearsay. The host tables turned out to be byte-identical copies of
the firmware's, carrying no extra information at all.

## 12J. Effects on both channels, and two preset gaps (2026-08-02)

### Both channels can use the effects at once

Universal Control presents the io24's processing as something you assign to one
channel and swap. That framing hides two different questions, and they have
different answers.

**The Fat Channel is genuinely per-channel.** EQ, gate, compressor and limiter
are addressed by `blockIndex`, and the firmware bounds those blocks at index 1 —
two instances, one per channel, independently settable (§9a).

**The effects are a shared send, with per-channel sends.** Block 202 (reverb) is
a singleton, written at `blockIndex` 0 only. But its send is not: the firmware
carries `input1FxMix` (internal 3) and `input2FxMix` (internal 4) as two separate
parameters, both reached through `'Para'` wire 4 with the blob `index` selecting
the channel (§12I). A shared effect fed by independent sends is a send-effect
architecture, and it means **both channels reach the effect simultaneously**.

Measured, with the FX return routed into Mix A and each channel tested alone —
the other channel's preamp at 0 dB so it could not contribute:

| channel | send 0 → 1 → 0 | change | baseline recovered to |
|---|---|---|---|
| ch1 | −67.45 → −91.91 → −67.25 dB | **−24.56 dB** | 0.20 dB |
| ch2 | −68.15 → −93.05 → −67.98 dB | **−24.98 dB** | 0.18 dB |

Each channel produces the same large effect on its own, and the baseline returns
to within 0.2 dB, so this is not drift. `fxMix` crossfades dry to wet rather than
adding a send level, which is why opening it *lowers* a noise source's level on
the return — 100 % wet reverb of broadband noise is quieter than the noise. The
direction does not matter for the question being asked; the symmetry does.

**So "one channel at a time" is a host-side UI choice in UC, not a hardware
limit.** This driver exposes both sends, and the app's Effects page says so.

A methodological note, because the first attempt at this measurement produced
nonsense (opening a send appeared to *lower* the level below the floor, and
"both channels" landed 29 dB under it): the reverb moves the FX return by about
+2.26 dB (§9d), while preamp self-noise at 60 dB gain drifts more than that over
the minute a three-way sweep takes. The signal was smaller than the drift. A-B-A
per channel, with the other channel silenced, is what made it resolvable.

### `'Pari'` 12 now has an API

`set_processing_channel(channel, source_input)` / `processing_channel(channel)`,
from §12I's identification. Confirmed on hardware, including the permutation
behaviour predicted from the slot 38/39 values:

    ch1 <- input 1, ch2 <- input 2        (default)
    set_processing_channel(1, 2)  ->      ch1 <- input 2, ch2 <- input 1
    set_processing_channel(1, 1)  ->      ch1 <- input 1, ch2 <- input 2

Setting one channel's source moves the other as well. It is a swap, not an
independent assignment.

### Two things were missing from every preset

Presets are host-side and built from the shadow of the driver's own writes
(§11), so a setter that is not registered as shadowed simply does not exist as
far as presets are concerned. A round-trip test — set one distinctive value in
every category, save, wipe, reload, compare — found two that were not:

* **`set_reverb`** — the effect was configured, the preset saved, and the reverb
  came back off. Given that the effects are the point of the Effects page, this
  was the more damaging of the two.
* **`set_output_delay` / `set_output_delay_bus`** — added in §12I and never
  registered.

Both are now shadowed, along with `set_processing_channel`. The round trip
captures 13 settings and restores all of them, with the live `'Appl'` values
(gains, volumes) exact to 0.00.

The general rule this exposes, worth applying to anything added later: **on this
device, "can the user set it?" and "can the device report it?" are different
questions, and every parameter where the answer is yes/no has to be shadowed
explicitly or it is lost.**

### The app no longer claims a save it has not done

`_pick()` posted its "Saved …" toast synchronously, immediately after handing the
work to the USB thread — so a preset that failed to write still reported success,
and the user had no way to tell. The result is now reported from the worker, with
the actual count, and failures say so. Loading additionally re-syncs the mixer
widgets, which otherwise kept showing pre-load values.

## 12K. The device DOES store presets, and they contain the effects (2026-08-02)

**This section corrects a claim repeated throughout this document and the
README**: that the io24 stores no preset content, only a slot index, and that
presets must therefore be host-side.

That is wrong. The device firmware contains **sixteen complete factory presets**,
stored as serialised objects, and every one of them includes `voicefx`.

### What is actually in the firmware

At fw 0x6a2bb onward, in two banks of eight:

| bank 1 | bank 2 |
|---|---|
| Broadcast | Broadcast |
| Vocal | Vocal |
| Acoustic | Acoustic |
| Electric | Electric |
| V… | Bass Guitar |
| **Slap Echo** | Stereo Acoustic |
| **Detuned Vocal** | Stereo Piano |
| **Robot** | Stereo DJ |

Each is a length-prefixed typed record — `S` string, `i` int, `d` double, `{}`
nested — of the form:

```
preset_name S "Broadcast"   icon_id S "broadcast"
opt    { swapcompeq }
filter { hpf }
gate   { keylisten expander keyfilter threshold range attack release }
limit  { limiteron threshold }
eq     { __classid {A0A8A068-…} eqallon
         eqgain1..4 eqq1..4 eqfreq1..4 eqbandon1..4 eqbandop1/4 }
comp   { __classid {1F831EC1-…} input output attack release ratio
         keyfilter keylisten }
voicefx{ __classid {98A527BA-…} feedback mix time }
```

Eleven distinct `__classid` GUIDs appear across the set, so the effect types are
individually identified.

### Why this matters, and what it says about block 201

The preset names are the tell. **Slap Echo, Detuned Vocal and Robot** correspond
to the delay, detuner and vocoder among the six FX models decoded in §9d — the
models this document records as unreachable because block 201 never responded to
a `'VoFx'` selector write.

Both things can be true: the *direct* block-201 write path may genuinely be
inert on this model, while the effects remain reachable **through preset
recall**, because the firmware carries the voicefx parameters inside each preset
record rather than expecting them to be pushed one at a time. That reframes §9d's
negative result — "the models are not implemented" was too strong; what was shown
is that one particular addressing path does not work.

### What the slot index does, and the limits of the test

Sweeping the slot index 0..3 and measuring the channel DSP path with the
source-cancelling ratio metric (§9d):

    metric = dB(JaSt slot 14, post-DSP) - dB(JaSt slot 4, pre-DSP)

| | |
|---|---|
| positive control (4-stage LP at 120 Hz) | **−23.74 dB**, returning to 0.46 dB |
| slot 0 → 1 → 2 → 3 → 0 → 1 | spread **1.27 dB**, repeatability 0.82–0.99 dB |

The control proves the observable works. The sweep shows no change beyond its own
repeatability — but **this does not show the slots are empty**, and the earlier
conclusion drawn from it was unsound for three separate reasons:

1. **The metric is a broadband level.** The factory presets are gentle vocal and
   broadcast chains; a few dB of EQ tilt and some compression is well inside the
   1 dB noise floor of this measurement. Only the positive control was violent
   enough to be visible. Detecting a preset recall needs a *frequency-resolved*
   measurement, not a level.
2. **Universal Control has never run on this machine.** That is the premise of
   this whole project. No user preset content has ever been written to the
   device, so any user slots would be empty regardless.
3. **Writing the slot index may not be a recall.** `'Pari'` 16 sets
   `input1/2SlotIndex`, and the firmware clamps it to 0..3, while sixteen
   presets exist. An index that addresses four things cannot by itself select
   among sixteen, so there is at least one more mechanism — a bank select, a
   separate recall trigger, or a different transport entirely.

### Corrected status

* The device **does** store preset content, in firmware, including effects.
* The host write is now decoded: UC 4.7.2 serializes a complete scene-shaped
  record and sends `SetP | Appl(0) | MemP | Stat(slot 0..3)`. The separate user
  library uses nested `PrsM` 16..27.
* Firmware dispatch, record assembly, and the bound storage-write interface are
  traced in §13o. Live acceptance and power-cycle survival remain to be
  validated; they are no longer packet-discovery questions.

## 12L. Preset architecture, from PreSonus's own documentation (2026-08-02)

> **Measurement caveat (§13):** the slot-sweep numbers below use the pre/post
> ratio metric, which was later shown not to cancel the source. The conclusion
> holds — it rests on the manual and on 4 slots versus 8 presets — but the dB
> figures are not a sound bound.

The Revelator io44 owner's manual documents the preset system for the sibling
device, and it resolves what §12K left open. Paraphrasing the "Presets and
Scenes" section:

* each channel can reach **2 presets from the hardware Preset buttons**
* there are **6 further presets created by PreSonus**
* plus **6 slots per channel for the user's own presets** (12 across the device)
* the user **chooses which two of the total** the hardware buttons address

So the model is a **device-resident preset library**, with the two buttons acting
as assignable pointers into it — not as slots that hold content themselves.

### This matches the firmware exactly

2 button presets + 6 factory = **8 factory presets per channel**, and §12K found
**16 preset records in firmware, in two banks of eight**. The counts line up, and
it confirms those records are the factory library rather than incidental data.

### And it explains three failed measurements

`'Pari'` 16 writes `input1SlotIndex` / `input2SlotIndex` and the firmware clamps
it to **0..3**, while there are eight factory presets per channel. An index that
addresses four things cannot select among eight — so **the slot index is almost
certainly the button assignment, not a recall trigger**. Writing it would change
no audio at all, which is precisely what three independent attempts found:

| method | result |
|---|---|
| sweep slots, watch all 503 JaSt floats | only the index moved — but the DSP chain is not in that blob, so this proved nothing either way |
| sweep slots, broadband level via the pre/post ratio metric | spread 1.27 dB vs repeatability 0.82–0.99 dB |
| sweep slots, third-octave spectrum of Mix A | worst 4.05 dB vs repeatability 2.69 dB — does not clear the bar |

The third test is the only one that could in principle have seen a Broadcast /
Vocal / Acoustic / Electric difference, and it came out inconclusive rather than
negative. Given the documentation, the likeliest reading is that no recall was
ever being requested.

### Corrected claims

Two statements in this document were wrong and are withdrawn:

1. **"The device stores no preset content, only a slot index."** It stores
   sixteen factory presets in firmware, each including `voicefx` (§12K), and per
   the manual it also has writable user slots.
2. **"Presets must be host-side."** They are host-side *in this driver*, which is
   a limitation of what has been decoded, not a property of the hardware.

### Corrected save boundary

The read-only `MemP` probe below did not test its SetP handler. The save carrier
is now decoded as `SetP | Appl(0) | MemP`, with `Stat` 0..3 for the four device
slots and `PrsM` 16..27 for the user library. What remains unproven is the live
acceptance/recall boundary and nonvolatile survival, not the wire command or
index spaces.

### What this means for a user today

Standalone operation works, with the **factory** presets, driven from the
hardware buttons — that path never involves the host. This project can now build
the exact custom-slot record and frames without transmitting them. Live settings
written through the older parameter path survive the host program closing but
not a power cycle, because they are live parameter state rather than a stored
preset.

## 12M. The preset model, from Universal Control's own files (2026-08-02)

The decisive evidence was not in either binary. Universal Control had been run on
this machine under a previous Windows install, and its data survives at

    <windows>/Windows.old/Users/<user>/Documents/PreSonus/Revelator IO/
        Scene/*.scene        complete device snapshots
        Fat/  FX/Delay  FX/Reverb  GEQ/  Project/  Backup/

`.scene` files are **plain JSON**, and they contain UC's entire data model for
this device. They confirm, from the vendor's own serialisation, several things
this document had inferred, guessed at, or got wrong.

### The whole model, in one file

```
global   phonesSrc, aux1_mirror_main, aux2_mirror_main,
         outputDelay, outputDelayBus, presetButtonMode, auxMuteMode
line     ch1, ch2   (27 keys: username, mute, volume, link, linkmaster,
                     preset_name, solo, lr, assign_aux1, assign_aux2,
                     aux1, aux2, preampgain, pan, stereopan, ...)
return   ch1..ch3      fxreturn ch1      aux ch1,ch2      main ch1     fx ch1
presets  slots        0..3   -- the DEVICE slots, full content each
         userpresets  "<id>.<name>.channel"
```

### What it settles

* **`aux1`/`aux2` are per-channel sends** and `assign_aux1`/`assign_aux2` are
  separate booleans — exactly the send model built in §12F, confirmed against the
  vendor's own field names.
* **`outputDelay` and `outputDelayBus` are `global`**, not per channel — matching
  §12I, where they were found as single (non-indexed) firmware parameters.
* **`aux1_mirror_main`, `aux2_mirror_main`, `auxMuteMode`, `presetButtonMode`,
  `phonesSrc` are host-side globals**, which is why §12H found them in the host
  DLL but not in the device firmware.

### The preset architecture, resolved

`presets.slots` holds **four** entries, each a complete preset:

```
preset_name, opt{swapcompeq}, filter{hpf},
gate{on keylisten expander keyfilter threshold range attack release},
comp{__classid on softknee automode threshold ratio attack release gain
     keyfilter keylisten},
eq{__classid eqallon lowgain lowfreq lowmidgain lowmidfreq
   himidgain himidfreq higain},
limit{limiteron threshold},
voicefx{__classid on time feedback mix}
```

Four slots is **two per channel across two channels**, which is precisely the
`'Pari'` 16 clamp of 0..3 that §12K could not account for, and precisely the
manual's "each channel can access 2 presets using the Preset buttons".

`presets.userpresets` keys are `"<id>.<name>.channel"`, and the ids observed are
**16, 17, 22, 23**. Combined with the sixteen factory presets found in firmware
(§12K) the library numbering falls out cleanly:

| ids | what |
|---|---|
| 0–15 | factory, in firmware — 8 per channel |
| 16–21 | user presets, channel 1 — 6 slots |
| 22–27 | user presets, channel 2 — 6 slots |

which is exactly the manual's "6 preset slots per channel to create your own,
12 in total".

**Every preset contains `voicefx`**, and one of the recovered scenes
(`Piano Accomany.scene`, slot 0) has `voicefx.on = 1` — a stored preset with the
effect switched on. The device stores effects in presets. There is no ambiguity
left about this.

### Corrections this forces

§12K already withdrew "the device stores no preset content". This adds:

* **"Presets are host-side"** — false. UC's scene file records the content of
  four *device* slots plus a user library. It is a snapshot of device state, not
  the storage itself.
* **The slot index was never a recall trigger**, which is why three separate
  measurements (§12L) found nothing when writing it. It selects which of the four
  button slots is current.

### What this unlocks

The `.scene` format is JSON with the vendor's own field names, and nearly every
field maps onto a setter this driver already has — `filter.hpf` →
`set_highpass_freq`, `gate` → `set_gate`, `comp` → `set_compressor`, `eq` →
`set_eq_band`, `limit` → `set_limiter`, `aux1`/`aux2` → `set_send_db`,
`outputDelay` → `set_output_delay`. A **scene importer is straightforward** and
would let a Linux host apply presets the user built in Universal Control, without
needing the undecoded store command at all.

Still not decoded: the wire command that writes a preset **into** a device slot.
That remains the only thing standing between this driver and full standalone
preset authoring.

## 12N. The Vintage EQ, and a correction on Voice FX (2026-08-02)

Both from the Revelator io44 owner's manual, which documents the sibling device's
identical feature set.

### The "simplified" EQ is the Vintage 1970s model

§12M could not apply one channel's EQ because its fields were
`lowgain/lowfreq/lowmidgain/lowmidfreq/himidgain/himidfreq/higain` with the
`*freq` values as small integers. The manual identifies it: the device offers
**three EQ models** — Standard, Passive Program, and Vintage 1970s — and the
Vintage model's controls match the field set exactly:

| manual control | scene field |
|---|---|
| Low Frequency (shelving) | `lowfreq` |
| Low Gain | `lowgain` |
| Low-Mid Frequency | `lowmidfreq` |
| Low-Mid Gain | `lowmidgain` |
| High-Mid Frequency | `himidfreq` |
| High-Mid Gain | `himidgain` |
| **High Gain** — with no High Frequency control | `higain`, and the scene has **no** `hifreq` |

That missing `hifreq` is the confirming detail: a switched Neve-style EQ with a
fixed high shelf.

The **Standard** model is the parametric form (`eqgain1..4`, `eqq1..4`,
`eqfreq1..4` in real Hz), and the manual's "Low Shelf On/Off" / "High Shelf
On/Off" controls confirm `eqbandop1` / `eqbandop4` are the shelf toggles for the
outer bands — which is how `io24_scene.py` already maps them.

**Position counts, from the host descriptors** (min/max, so the count is exact):

| field | range | positions |
|---|---|---|
| `lowfreq` | 0..3 | **4** |
| `lowmidfreq` | 0..2 | **3** |
| `himidfreq` | 0..2 | **3** |
| `lowgain`, `lowmidgain`, `himidgain`, `higain` | −16..+16 dB | continuous |

**2026-09-14 correction:** the exact lists are inline display strings referenced
by the UC 4.7.2 transport DLL's parameter descriptors, not float/double runs.
They are LF `35, 60, 110, 220 Hz`, low-mid `360, 700, 1600 Hz`, and hi-mid
`3200, 4800, 7200 Hz`. All four coefficient designers were also located and
made executable offline against the pinned DLL. At that checkpoint direct live
import was skipped because the running Host did not depend on that retained
binary; it no longer labelled the switch frequencies as guessed or unverified.

**2026-09-18 correction:** the Host now treats the pinned local artifact as an
explicit runtime dependency for these two exact models and fails before
transport when it is unavailable. Direct live import and editing are no longer
skipped; no Standard-biquad approximation is used.

### SUPERSEDED: manual-based one-channel inference

> **2026-08-31 correction.** The manual quote and the singleton resolver prove
> there is one settings instance, but they do not prove one audio lane. The
> later setup trace above proves block 201 is configured two-in/two-out after
> both physical inputs are mapped, and Doubler binds private-core lanes 0 and 1.
> Treat the remainder of this subsection as historical reasoning, not the
> current io24 topology.

This contradicts a claim made earlier in this session and needs stating plainly.
The manual:

> "Revelator io44's Voice FX **can only be used on one of the two Inputs at a
> time**. You can select Channel 1 or 2 for use with Voice FX from the Settings
> menu."

and separately:

> "while you can use any of these effects with the Fat Channel and Reverb, you
> can only use **one effect at a time**."

That "select Channel 1 or 2" setting is **`processingChannel`** — `'Pari'` 12
(§12I), whose whole behaviour now makes sense: it is a *permutation*, moving the
single Voice FX processor between inputs, which is exactly why setting one
channel's value moves the other's (§12J).

**What was measured earlier remains true, but it was measuring something else.**
The −24.56 dB / −24.98 dB result (§12J) came from opening each channel's `fxMix`
send and watching the **FX return** — that is the shared **reverb/FX send bus**,
which genuinely does take both channels at once. It was never the Voice FX
insert. Two different things:

| | both channels at once? |
|---|---|
| Reverb / FX send bus (block 202, per-channel `fxMix`) | **yes** — measured |
| Voice FX (`voicefx`, the character effects) | **no** — one input at a time, selected by `processingChannel` |

So the earlier summary "both channels can have effects" was right about the
reverb and wrong if read as covering Voice FX. The io44 manual is the authority
here; the io24 carries the same `processingChannel` parameter and the same
per-channel `voicefx` block in its scene files, so the same limit is expected,
but this has not been separately confirmed on io24 hardware.

### Preset storage, resolved

> "Revelator io44 can **save two presets per channel on the hardware unit
> itself**. This is useful for times when you want to use Revelator io44 with a
> device that doesn't run Universal Control — for example, a Chromebook, iPad, or
> a camera with an audio input."

So the division is:

| where | what |
|---|---|
| device firmware | 8 factory presets per channel (§12K) |
| **device, writable** | **2 per channel = the 4 `presets.slots`** |
| computer, in UC | 6 user slots per channel — the `presets.userpresets` library |

Four writable device slots is exactly the `'Pari'` 16 clamp of 0..3, and exactly
the four entries in every recovered `.scene`. The manual's factory list for
channel 1 — Broadcast, Vocal, Acoustic Guitar, Electric Guitar, Vintage Channel,
Slap Echo, Detuned Vocal, Robot — is **the eight records found in firmware bank
1**, name for name.

The store command is now decoded. `'Appl'`+SetP accepts `'Para'`, `'Pari'`,
`'FRst'`, and the arithmetically synthesized `'MemP'` comparison. UC 4.7.2
writes one of the four device slots as
`SetP | Appl(0) | MemP | Stat(slot) | serialized-record`; §13o follows that
message through the firmware storage-write interface.

## 12O. Can Voice FX run on both channels? And can a channel hold more than 2 presets? (2026-08-02; corrected 2026-08-31)

The resolver result below is valid — block 201 is a singleton — but its original
one-input interpretation was not. A singleton can have multiple audio lanes.

### Voice FX: one processor, verified in the resolver

The block resolver at FW VA 0x6005ce98 was re-read instruction by instruction
for this question. It handles the numeric blocks like this:

```
cmp r3,#201  ; beq  ->  no index load, no bounds check, no stride multiply
cmp r3,#202  ; beq  ->  same
cmp r3,#203  ; ldr index ; cmp #2 ; bhi <null> ; movs r1,#0x4c ; dev + index*0x4c
cmp r3,#100  ; ldr index ; cmp #2 ; bhi <null> ; movs r1,#0x74 ; dev + index*0x74
```

Blocks **100** (mixer) and **203** load `blockIndex`, bound it, and multiply by a
per-block stride — that is what a multi-instance block looks like. Blocks **201**
(Voice FX) and **202** (reverb) take neither path: their object address is a fixed
offset from the device struct.

**There is exactly one addressable Voice FX settings instance and one shared
reverb instance in the io24's DSP.** There is no second block-201 settings object
for different effects or parameter values. The later audio-root and `Setu` trace,
however, proves that singleton is configured with two input/output lanes; the
`processingChannel` values form the two-lane input permutation rather than
excluding either mapped input.

So:

| want | possible? |
|---|---|
| different Voice FX on ch1 and ch2 at once | **no** — one processor exists |
| Doubler's same private-reverb state on both inputs | **structurally possible, runtime unproved** — two block-201 processing lanes and private-core indexes 0/1; stock simultaneous routing has not been observed |
| shared block-202 reverb on both channels | **yes**, measured (§12J), through its separate per-channel sends |

Direct block-201 parameter writes still do not establish its processing gate.
Native complete-record `Stat` application does, and the paired save path uses
that representation. This private-core topology does not use block 202 or its
independent sends.

### Preset slots: four, and the firmware clamps

The device exposes **four** preset slots, two per channel:

* the host vocabulary names exactly `Preset Slot 1` … `Preset Slot 4`
  (DLL 0x436400–0x436430), plus `Preset Title 1`/`Preset Title 2` for the two
  buttons on a channel
* every recovered `.scene` has exactly four `presets.slots` entries
* `'Pari'` 16 (`input1/2SlotIndex`) is hard-clamped by the firmware to **0..3**
* the io44 manual: "can save **two presets per channel** on the hardware unit
  itself"

`presetButtonMode` (firmware internal id 2, `'Pari'` 4) is the 1-versus-2 setting
— it changes how many of a channel's slots the front-panel button cycles through,
not how many exist.

**Going past two per channel is not possible standalone.** The slot array is
fixed in firmware, and an index above 3 is clamped rather than extended; there is
no fifth slot object to write. What *is* unlimited is host-side presets — this
driver already keeps arbitrarily many as JSON and pushes a full DSP state in one
go — but that needs the host connected, which is precisely the case the device
slots exist to cover.

## 12P. SUPERSEDED: the missed synthesized `'MemP'` SetP handler (2026-08-02;
corrected 2026-08-31)

This pass inspected only the dispatcher's literal pool and therefore reached a
false negative. The pool does contain exactly three literal tags:

```
0x2da1c   0x50617261  'Para'
0x2da20   0x46527374  'FRst'
0x2da24   0x50617269  'Pari'
```

`'MemP'` is absent as a literal because fw `0x2d9cc–0x2d9d4` synthesizes its
integer value arithmetically from `'FRst'` before comparing it. On a match the
dispatcher uses vtable slot `+0x80`. Section 13o traces that accepted SetP path
through root handler `0x2f210`, the preset component, its record writer, and
the bound storage backend.

The read-only observations made here remain valid but say nothing about the
separate SetP handler:

| probe | result |
|---|---|
| `'Stat'` on `'Appl'`, blockIndex 0–3, sizes 0x20–0x7ec | echo only, 5 non-zero bytes (the header), no data at any size |
| block **203** (resolver: `dev + 0x3f84 + index*0x4c`, index ≤ 2) with `'JaSt'`, `'MemP'`, `'Stat'` at index 0–2 | the block is *accepted* — the reply echoes `0xcb` = 203 — but returns no data on any blob |

### Corrected result

The preset store uses the ordinary `SetP | Appl(0) | MemP` path. Nested
`Stat` indexes 0..3 address the four device slots; nested `PrsM` indexes 16..27
address the separate user library. No command-tag sweep or alternate transport
is needed to discover the save transaction.

## 12Q. The three EQ models, the three compressor models, and the Voice FX GUIDs (2026-08-02)

### EQ models

The device has three, and a channel's stored EQ is whichever model was active.
They are distinguishable in a `.scene` by class GUID and by field set:

| model | GUID | fields | real-world model | status here |
|---|---|---|---|---|
| Standard | `{A0A8A068-…}` | `eqallon`; `eqgain1..4`, `eqq1..4`, `eqfreq1..4` (Hz), independent `eqbandon1..4`, outer `eqbandop1/4` | — | **applied as-is**; GTK mirrors UC's exact `Eqxt4` controls and this GUID is also what the firmware's factory presets carry |
| Vintage 1970s | `{E1C5E024-…}` | `lowgain/lowfreq`, `lowmidgain/lowmidfreq`, `himidgain/himidfreq`, `higain` | **Neve 1073** — LF 35/60/110/220 Hz, low-mid 360/700/1600 Hz, hi-mid 3200/4800/7200 Hz, fixed HF, ±16 dB | exact editable Host route using the pinned UC designers; missing/mismatched local artifact fails before transport |
| Passive Program | `{C0730CBB-…}` | Low Boost, Low Attenuation, Low Frequency Select, High Bandwidth, High Boost, High Attenuation, High Frequency, Attenuation Select | **Pultec EQP-1A** — the boost/attenuate-at-once topology is unmistakable | factory **Big Vocal** fixture plus exact editable Host route using both pinned UC designers; missing/mismatched local artifact fails before transport |

`eqallon` is distinct from all four per-band switches. `eqbandop1` /
`eqbandop4` are confirmed as the Low Shelf and High Shelf toggles by both the
manual and UC 4.7.2's embedded `fatchannelxt.xml`; the middle option fields have
empty flags and are not exposed as controls.

### Compressor models

Three: Standard, Tube Leveling Amplifier, Class-A FET — matching the driver's
`set_compressor(model=0/1/2)`. Observed in the scenes:

| GUID | fields | model |
|---|---|---|
| `{870D04F7-…}` | `threshold ratio attack release gain softknee automode` | Standard |
| `{1F831EC1-…}` | `input output attack release ratio(index) keyfilter keylisten` | **FET** |
| (Tube) | — | never used in the recovered scenes |

Both observed models are dispatched correctly by `io24_scene.py`; conflating them
would put a FET *ratio index* into the Standard model's ratio.

### Voice FX GUIDs map onto the decoded models

The manual lists six: Doubler, Detuner, Vocoder, Ring Modulator, Filters, Delay —
matching `FX_MODELS`. Three appear in the recovered scenes, and their field sets
identify them without ambiguity:

| GUID | fields | effect | driver model |
|---|---|---|---|
| `{98A527BA-…}` | `time feedback mix` | Delay | `delay` (5) |
| `{66A10093-…}` | `lows width mix` | **Doubler** — the manual's control list is Lows / Width / Wet-Dry, exactly | `transformer` (0) |
| `{4B4CAD90-…}` | `bcarrierfreq bcarrier2 bcarrier2freq bdist bvol mix` | **Ring Modulator** — a carrier frequency is diagnostic | `ringmod` (3) |

This is the first correspondence between UC's stored effect data and the driver's
independently decoded model blobs, and it means a scene's `voicefx` block could be
applied *if block 201 responded*.

**Note the driver's `transformer` is the Doubler.** The name came from the blob's
low-shelf behaviour before the effect was identified; the manual and the GUID
field set both say Doubler.

### A block-201 retest that did not work

Worth recording so it is not counted as evidence either way. Block 201 was
re-tested using parameter values taken from the recovered scenes — real settings
from real Universal Control sessions on this device, including the Ring Modulator
that `Piano Accomany.scene` has `on: 1` in device slot 0. **The run is void: its
own positive control failed**, with reverb on block 202 producing a 0.19 dB swing
where it should produce tens of dB. The rig was unstable because `fx_off()`
rewrites the FX routing between measurements, so the baseline moved 24 dB
mid-run. Block 201's status is unchanged — still "no measurable response", still
established from earlier work rather than from this.

## 12R. A measurement caveat that invalidates two runs (2026-08-02)

Two block-201 re-tests were attempted after establishing (from the recovered
scenes) that Voice FX is a **channel insert**, not a send effect —
`fxreturn/ch1` is named "Reverb", `fx/ch1` contains only a `reverb` block, and
`voicefx` lives under `line/chN`. That correction stands and matters: every
earlier block-201 test measured the reverb return, which is the wrong signal path
for a channel insert.

But neither re-test produced usable data, and both are void:

1. The first measured the FX return with `fx_off()` between steps, which rewrites
   the routing — the baseline moved 24 dB mid-run, and the positive control
   (reverb) swung 0.19 dB instead of tens of dB.
2. The second measured the channel output with the pre/post ratio metric. Its
   positive control also failed: a 4-stage low-pass at 120 Hz moved the metric
   **0.00 dB**, and the metric sat at exactly **−3.000 dB** through every change
   — EQ applied, EQ flattened, block 201 armed, block 201 disarmed.

−3.000 dB is exactly the mono-to-stereo pan law, and a value that constant is a
fixed relationship rather than a measurement. So in the device's present state
`JaSt` slot 14 is tracking slot 4 by a constant, and **this document's claim that
slot 14 is post-DSP cannot be relied on unconditionally** — earlier in the same
session the identical rig responded correctly (baseline −32.24 dB, low-pass
swing −23.74 dB, recovery 0.46 dB), so the difference is device state, not the
method.

What changed in between is not isolated. Candidates, in order of suspicion: the
scene import applied a full channel strip (gate, comp, limiter, high-pass) to
channel 1; block 201 was armed with three different models; and a great many
writes accumulated without a power cycle. The device has not been power-cycled
since, and its DSP is only reset by a cold boot (§12) — the shadow was cleared
and every block set to its off state, but that is the driver's idea of off, not
the firmware's power-on state.

**Before the next block-201 attempt: power-cycle the unit**, confirm the low-pass
control swings the metric by roughly −23 dB from a clean boot, and only then arm
an FX model. Any block-201 result obtained without that control passing in the
same run should be discarded.

## 12S. Block 201 tested correctly, from a cold boot — it does not respond (2026-08-02)

> **Measurement caveat (§13):** this uses the pre/post ratio metric, later
> shown not to cancel the source. The negative is probably still right — the
> control swung −21 dB while the FX writes gave ~0 — but the stated
> sensitivity was overstated, and it deserves a re-run with a stable input.

This supersedes §9d's hedged "appears not to implement" and §12R's two void runs.
The test finally had all four things it needed at once:

1. **the right signal path** — Voice FX is a channel INSERT (§12R: `fxreturn/ch1`
   is named "Reverb", `fx/ch1` holds only a reverb block, `voicefx` lives under
   `line/chN`), so the observable is the channel output `line/ch1 → Mix A`, not
   the FX return that every earlier attempt measured

   > **CORRECTION (2026-09-15). Item 1 is wrong, and it is the most expensive
   > wrong sentence in this file.** The block-201 correction earlier in this
   > document already establishes the opposite from ear-verified hardware on
   > 2026-07-28: block 201 is **not** a true insert, and with the FX send closed
   > or the FX return unassigned an armed Delay is completely silent. So
   > `line/ch1 → Mix A` is the one path block 201 does **not** reach, and a
   > measurement there is blind by construction. Every CP34 block-201 null —
   > `NO_DIRECT_BLOCK201_METER_EFFECT_DETECTED`, the stable −0.119 and −0.070 dB
   > Transformer readings — ratioed exactly that path without opening the send
   > or assigning `fxreturn/ch1` into the bus it measured. Those results are the
   > expected reading of the wrong bus, not evidence that the insert is inert.
   > The naming argument in §12R is about which object *holds* the reverb, which
   > is not the same question as which bus carries block 201's output; a class
   > name, and a component's place in the object tree, are not evidence of
   > signal flow. Measure the return, with the shared reverb as an in-run
   > positive control through the identical path:
   > `re/io24_fx_shared_return_probe.py`.
   >
   > **WITHDRAWN IN PART (2026-09-15, by ear).** The return was then measured,
   > with the reverb as an in-run positive control, and block 201 rendered
   > nothing on any of its six models while block 202 was plainly audible on
   > that identical path — see the controlling section at the top of this file.
   > So the sentence "not evidence that the insert is inert" is withdrawn: the
   > bus was wrong, but correcting it does not make block 201 audible, and the
   > CP34 conclusion was right. What survives is the narrower claim that a
   > block-201 measurement taken without opening the send and assigning
   > `fxreturn/ch1` is blind, and that §12S item 1's naming argument does not
   > establish signal flow either way.
2. **a cold boot** — the DSP only resets on power cycle, and accumulated writes
   had previously pinned the metric at the −3.00 dB pan law
3. **a passing positive control**, gated in the script rather than assumed
4. **known-good parameters** — taken from `Piano Accomany.scene`, where a Ring
   Modulator is stored with `on: 1` in device slot 0 of this very unit

Result:

| step | metric |
|---|---|
| baseline | −27.018 dB |
| 4-stage low-pass at 120 Hz | **−48.538 dB** (swing **−21.52**) |
| low-pass removed | −27.027 dB (**recovery 0.01 dB**) |
| ringmod, carrier 146.85 Hz, on | −0.57 dB |
| Doubler, lows 0.15 / width 1.0 / mix 1.0, on | +0.14 dB |
| delay, 22.6 ms / feedback 1.0 / mix 1.0, on | +0.12 dB |

A control recovering to 0.01 dB leaves no room to argue the rig was blind. All
three FX swings are inside the noise.

**Conclusion: block 201 does not respond on the io24, and the negative is now
properly earned rather than inferred.** The six FX model blobs are decoded and
byte-verified against the host's own push routine (§9d), the models exist in the
firmware, the presets carry `voicefx` (§12K) — but nothing this driver writes to
block 201 reaches audio.

### What that leaves for Voice FX

The device plainly *can* run these effects: its factory presets include Slap
Echo, Detuned Vocal and Robot, and the user's own stored preset has an active
Ring Modulator. So the effects are reachable **through preset recall**, not
through direct block writes. Which means Voice FX and the preset-store command
are now the same problem, not two:

* the hardware Preset buttons recall device presets, effects included, with no
  host involved — that path works today and needs nothing from this driver
* the preset store command is now decoded as
  `SetP | Appl(0) | MemP | Stat(slot) | record` (§13o); what remains for Voice
  FX is controlled live validation that a stored/recalled record opens the
  audible processing path and survives the declared boundary

### A side finding worth keeping

Before the power cycle the pre/post ratio metric was pinned at exactly
**−3.000 dB** — the mono-to-stereo pan law — through every EQ and FX change, and
was restored to a normal, responsive −27 dB baseline by a cold boot alone. So a
long run of accumulated DSP writes can put this device into a state where the
metering no longer tracks the channel chain, with no error and no other symptom.
Any measurement session of length should power-cycle first and re-verify its
control, or its results are not trustworthy.

## 12T. Voice FX addressing re-checked, and pan not found (2026-08-02)

### Voice FX is block 201, and the model selectors are host-side

Raised because Voice FX behaves in Universal Control like any other Fat Channel
module — pick an effect, and it sits in the chain beside EQ and the compressor.
That suggested it might be a **chain** block (FourCC, per-channel `blockIndex`,
resolved at 0x6006efe8) rather than numeric block 201, which would mean every
block-201 test had the wrong address, not just the wrong signal path.

Checked. The chain resolver's FourCC set is `filt`, `gate`, `cpxt`, `lim `,
`opt ` (plus `Redu`/`Ltcy` for reads). There is **no** voice-FX FourCC anywhere
in the firmware — `VoFx`, `vfx `, `dlay`, `rvrb`, `insf`, `fxsl` all return zero
matches. And on the host side the six insert-FX classes all pass **0xc9 = 201**
to the common component constructor. So block 201 is the right address after all,
and §12S's negative stands.

What did come out of it is a cleaner statement of the architecture:

    fxmodel    firmware: 0    host DLL: 3
    eqmodel    firmware: 0    host DLL: 3
    compmodel  firmware: 0    host DLL: 3

**None of the three "model" selectors exists as a device parameter.** This is
already documented for the compressor (§9b: the model only decides which maths
fills an identical blob) and it evidently generalises to the EQ and to Voice FX.
So Voice FX needs no selector — the model is purely a host-side choice of
coefficients — which makes block 201's silence harder to explain, not easier.
It is an empirical result without a mechanism, and should be treated that way.

### `pan` — real, route not found

Every `line/chN` in a recovered scene carries `pan: 0.5` and `stereopan: 0.0`,
and the host has `Preamp Gain` / `Stereo Width` labels, so the feature plainly
exists. It is in **none** of the firmware's 40 parameters, and `io24_mixer.py`
records that the shared DLL's pan law is "never wired up" for this device
(`pan_mode` 0 = no pan term).

Probed with a calibrated 1 kHz tone into Mix A, watching the **stereo balance**
between `JaSt` slots 14 (L) and 15 (R) — an observable a level write cannot fake:

| write | L / R | balance |
|---|---|---|
| baseline | −20.00 / −20.00 | +0.00 dB |
| block 100, blob `index` 1/2/3, value 0.0 | −20.00 / −20.00 | +0.00 |
| block 100, blob `index` 1/2/3, value 1.0 | −19.00 / −19.00 | +0.00 |
| block 100, paramId 7, 8, 16, 143, 144 | −20.00 / −20.00 | +0.00 |

Nothing moved the balance. Note the `index` rows: value 1.0 raised **both** legs
by exactly 1 dB, which shows the mixer blob's `index` field is simply **ignored**
— the write landed as a 1.0 dB *level* on the same source. So `index` does not
select a parameter kind within block 100, and the source-slot paramId space does
not extend to pan.

Ruled out, then: the mixer block's index field, paramIds adjacent to the source
slots, and the descriptor ids used directly as mixer paramIds. Still to try: pan
as a **per-channel DSP block** (the chain resolver's FourCC space rather than the
mixer), or a mixer paramId well outside the ranges swept here. The feature is
not in doubt; only its address.

### The app's stereo link was cosmetic

Worth recording as a bug class. `io24gtk.py`'s "Link the two channels" switch
only set `link_both`, rebuilt the rack display and greyed out column 2 — it
**never called `set_channel_link()`**. The app therefore showed the channels as
linked while the hardware still had two independent mono inputs. It now drives
`'Pari'` 9 and, because the link can also be changed on the unit, follows the
device's own state from `JaSt` slot 42 bit 12 on the UI tick.

## 12U. Parameter coverage audit (2026-08-02)

Every one of the firmware's device parameters, against what the driver can reach.

**Driven by the driver (23):** `input1/2Gain`, `input1/2PhantomPower`,
`input1/2Mute`, `input1/2HighPassFilter`, `channelLink`, `hpVolume`,
`mainVolume`, `monitorMix`, `input1/2FxMix`, `hpOutputMute`,
`input1/2ProcessingChannel`, `outputDelay`, `outputDelayBus`,
`input1/2SlotIndex`, `presetMode`.

**Read-only status, correctly not driven (7):** `usbStatus`, `volumeKnob`,
`encoderAssignment`, `input1/2PresetSelect`, and the meter/clip parameters.

**Exists in firmware, NOT reachable over the wire (2):**

| parameter | internal id | status |
|---|---|---|
| `mainOutputMute` | 9 | swept every unclaimed `'Pari'` wire id (8, 10, 11, 14, 15, 18, 19) watching `JaSt` slot 42 **bit 1**, the main-out mute flag the front-panel button sets. **Nothing reaches it.** Only wire 15 responded, setting bit 13 — already known and still unexplained. |
| `muteMode` | 5 | wire 8 decodes to it in the dispatcher but produced no observable change |

`mainOutputMute` matters because the hardware mute button is a **main-out** mute,
not a headphone mute, so a host cannot currently mirror or drive the button's
function. `hpOutputMute` (internal 8, wire 6) is reachable and is a different
control.

This is the same shape as block 201: the firmware carries the parameter, and the
host protocol simply has no id that reaches it. Two independent instances of it
now, which suggests the wire id space was defined for a family of devices and the
io24 has capabilities its transport does not expose — rather than each case being
its own mystery.

## 12V. There are only two command tags — the cmdTag sweep is unnecessary (2026-08-02)

§12P listed "a different `cmdTag`" as the cheapest remaining lead for the preset
store, and suggested sweeping plausible 4-byte tags as a write experiment. **That
sweep would have been pointless**, and reading the dispatcher instead of writing
to the device settled it for free.

The message dispatcher at FW VA 0x6004d50c:

```
0x2d51c  ldr r3, ='Appl' ; cmp r1,r3 ; beq      <- block is 'Appl'?
0x2d54e  cmp r6,#19                             <- the documented length floor
0x2d55a  ldr r2, ='SetP' ; cmp r3,r2 ; beq      -> SetP path
0x2d560  ldr r2, ='GetP' ; cmp r3,r2 ; bne      -> REJECT
0x2d578  ldr r0, ='Rply'                        <- reply builder
```

**The firmware compares exactly two command tags and rejects everything else.**
`'Rply'` is only ever emitted, never accepted. So there is no third verb to find,
and `StorePreset` / `RestorePreset` in the host vocabulary are UCNET *message*
names that must ultimately become a `SetP`, not command tags of their own.

### The transport inventory closes the SysEx lead too

The io24's USB descriptors expose seven interfaces:

| # | class | note |
|---|---|---|
| 0 | Audio Control | |
| 1 | Audio Streaming | Mic/Inst 1/2 (capture) |
| 2 | Audio Streaming | Playback L/R |
| 3 | Audio Control, protocol 0 | a second control interface |
| 4 | **MIDI Streaming** | EP 0x02/0x82 — the SysEx carrier of §9 |
| 5 | **Vendor Specific "CTRL"** | EP 0x01/0x81 — what this driver uses |
| 6 | DFU | |

There is **no file-transfer or mass-storage interface**. The only two candidate
paths for preset content are interfaces 5 and 4, and both deliver into the same
dispatcher above — so the MIDI-SysEx carrier cannot expose an operation the bulk
path lacks. §12P's third lead is closed as well.

### Block 0 and the chain blocks, probed read-only

The dispatcher branches on `'Appl'` versus everything else, and the resolver maps
block **0** to the device object itself — never previously probed. GetP on block 0
with `JaSt`, `MemP`, `Stat`, `Para`, `Pari`, `mprm`, `Redu`, `Ltcy`, `opt `,
`Setu`, at blockIndex 0–3, returns an echo every time. The chain FourCC blocks
(`filt`, `gate`, `lim `, `opt `) do the same. One oddity worth a note: `'cpxt'`
returns **no reply at all** rather than an echo, unlike its siblings.

### Where this leaves the preset store

Bounded much more tightly than before, and every cheap lead is now spent:

* it is a **`SetP`** — there is no other verb
* it is **not on `'Appl'`** — that dispatcher's pool is `'Para'`, `'FRst'`,
  `'Pari'` and nothing else (§12P)
* it is **not a different transport** — both carriers reach the same dispatcher
* it is **not block 201/202/203/100/0** with any blob tried, and not a chain
  FourCC block with `JaSt`

What remains is a **blob-tag sweep on `SetP`** against the non-`'Appl'` blocks —
the DSP blocks take coefficient blobs, and a preset blob would be another tag in
whichever dispatcher serves them. That is a genuine write experiment with no
read-only shortcut, and it should be run from a cold boot with nothing streaming.
It is also the last idea on the list.

## 12W. The firmware's closed FourCC vocabulary — recovered offline (2026-08-03)

> **Superseded in part by §13b:** this scan cannot distinguish USB wire tags
> from firmware-internal component messages. `'VFxO'` and `'IFac'` are
> internal; the rest of the shortlist is unverified and should not be
> written to the device on the strength of this section alone.

Done with the device unplugged, from the extracted firmware alone.

### Method: compare, don't search

Searching a firmware image for FourCC byte patterns is useless here — §12P
attempted it and drowned in false positives, because the parameter name strings
generate thousands ("gain" reversed is "niag", and so on). What distinguishes a
real protocol tag is that the code **tests** it: the constant is loaded from a
literal pool and then compared.

So `scan_fourcc_compares.py` walks Thumb-2, resolves every PC-relative literal
load (`ldr rN,[pc,#imm]` and the `ldr.w` T2 form), and keeps those whose literal
is a printable FourCC **and** is followed by a comparison within a short window.
That yields the closed set of tags the firmware recognises.

### The result

Known tags all appear where expected — `'SetP'`, `'GetP'`, `'Appl'`, `'Para'`,
`'Pari'`, `'JaSt'`, `'MemP'`, `'Stat'`, `'Setu'`, `'Redu'`, `'Ltcy'`, `'IOSp'`,
`'MRSp'`, `'gate'`, `'lim '` — which validates the method. Alongside them are
**tags this project has never sent**:

| tag | sites | reading | pool neighbours |
|---|---|---|---|
| **`'VFxO'`** | 3 | **Voice FX On** — an enable, distinct from the model blob | `'Setu'`, `'lim '` |
| `'IFac'` | 14 | insert-FX active? | `'GmCo'`, `'GmBR'` |
| **`'APSP'`** | 2 | active preset…? | **`'Para'`, `'Pari'`, `'Stat'`** at 0x423cc; **`'MemP'`, `'JaMD'`, `'MBPt'`, `'IFac'`** at 0x41e98 |
| **`'PrsM'`** | 2 | "Preset M…" | `'Stat'` (both sites) |
| `'MBPt'` | 3 | — | `'MemP'`, `'APSP'`, `'Stat'` |
| `'SVPt'` / `'ISVP'` | 2 / 2 | save-point? | each other |
| `'PEvP'` | 2 | — | — |
| `'MBdf'` | 4 | recorded as undecoded in this project's notes | — |
| `'Bqdf'` | 18 | biquad coefficients — **the real casing** of the blob this driver sends as `BQDF` | — |
| `'Lfdf'` | 2 | filter coefficients? | — |

### Why this matters

**`'APSP'` is pooled with `'Para'`/`'Pari'`/`'Stat'`, but not in the `'Appl'`
dispatcher.** That block's pools are already fully read — SetP takes
`'Para'`/`'FRst'`/`'Pari'` (§12P), GetP takes `'JaSt'`/`'MemP'`/`'Stat'`. So
0x423cc belongs to a *different* block's blob dispatcher, and that is exactly the
shape the preset store was predicted to have in §12V: a `SetP` carrying an
unidentified blob tag against a non-`'Appl'` block.

**`'VFxO'` is the best explanation yet for block 201.** §12S established that
block 201 accepts model writes and produces no audio, with no mechanism — and
§12T then showed the model *selectors* are host-side only, which made the silence
harder to explain rather than easier. A separate FX **enable** tag resolves it:
the coefficients land, and the effect is never switched on. That is a hypothesis,
not a result, but it is the first one that fits every observation.

### The next step is READ-ONLY

This changes the plan in §12V. That section concluded the remaining experiment
was a blind write sweep with no read-only shortcut — which was true when the tag
space was unbounded. With a named shortlist, the first pass can be `GetP`, which
writes nothing:

`probe_new_tags.py` (in `~/.cache/io24/re/`) sends each new tag to every known
block and scores the reply against a calibrated echo baseline. A tag returning
real data localises the block before anything is written. Only if that comes back
empty does the question become a write experiment, and even then against a
shortlist rather than a 4-byte space.

### Where the extracted inputs now live

`~/.cache/io24/re/` — firmware, host DLL, the io44 manual text, every probe
script, and a README of the located offsets. They were in `/tmp`, which is tmpfs
on this machine, so a reboot would have cost a re-extraction from the 195 MB
installer. Not committed to the repo: `dspusbdevice.dll` is PreSonus's binary.

## 12X. The read-only probe: one new readable blob, and the end of the read-only road (2026-08-03)

§12W's shortlist was probed with `GetP` across 11 blocks × 7 blob sizes × 2 block
indices — 2310 read-only requests. Two results, one useful and one that closes a
line of attack.

### `'APSP'` on block 0 returns real data

The only new (block, tag) pair that answers with content rather than an echo:

| size requested | non-zero payload bytes |
|---|---|
| 0x48 | 11 |
| 0x100 | 15 |
| 0x1f0 / 0x7ec | 16 |

Against a calibrated echo baseline of 5. The count scaling with the requested
size is what distinguishes real content from a fixed echo. The payload is two
records of small integers, at blob+0x14 and blob+0x5c:

```
blob+0x14   2  2  1  2  4  6
blob+0x5c   1  4  4
```

**It is not the preset pointer.** Re-read while sweeping `input1SlotIndex` 0..3,
`input2SlotIndex` 0..1, and toggling the preset enable — **the values never
change**. Static integers describing counts, in a device whose vocabulary already
contains `'IOSp'` (IO Spec) and `'MRSp'` (MR Spec), read as a third capability
descriptor rather than state. Worth having on record, and it is the first new
readable blob found since `'JaSt'`, but it is not what the search was for.

### `GetP` cannot tell a recognised tag from a rejected one

The reply format has no status field. Sent to `'Appl'`:

```
Para (recognised, write-only)  79 6c 70 52 6c 70 70 41 ... 61 72 61 50 00 01 ...
ZZZZ (pure garbage)            79 6c 70 52 6c 70 70 41 ... 5a 5a 5a 5a 00 01 ...
QqQq (pure garbage)            79 6c 70 52 6c 70 70 41 ... 71 51 71 51 00 01 ...
```

**Byte-identical apart from the echoed tag itself.** Same on block 201. So a
`GetP` cannot distinguish "recognised but write-only" from "never heard of it",
and no further read-only experiment can narrow the shortlist. §12W hoped this
would localise the preset store before anything was written; it cannot.

### Consequence

The write experiment is now genuinely unavoidable, and it is the only remaining
avenue. It is however much better targeted than the 4-byte sweep §12V
contemplated — a named shortlist from the firmware's own comparison sites:

| order | tag | why first |
|---|---|---|
| 1 | **`'VFxO'`** on block 201 | the only candidate that explains §12S — block 201 accepting model coefficient blobs and producing no audio. If it is an enable, writing it should make an already-armed model audible, which is a *measurable* outcome with the §12S rig |
| 2 | `'IFac'` on block 201 | insert-FX active? same reasoning, 14 comparison sites |
| 3 | `'PrsM'`, `'MBPt'`, `'SVPt'`, `'ISVP'`, `'PEvP'` on blocks 0 / 201 / the chain blocks | preset-shaped names |

Preconditions, from what this device has already cost: cold boot first, positive
control gated in the script, nothing streaming, unit visible, and the write kept
to a single tag per run so a fault can be attributed.

## 12Y. `'VFxO'` write experiment — negative (2026-08-03)

> **Void twice over:** the blob layout was guessed wrong (§12Z has the real
> one, from the firmware), and the metric does not cancel the source (§13).

The first item on §12X's shortlist, run as a single-tag write with a gated
control.

Rig: the §12S channel-insert one — ch1 preamp noise straight into Mix A, metric
= dB(slot 14) − dB(slot 4). A ringmod was armed first (the user's own
`Piano Accomany` values) so that an enable would have something to switch on.

| step | metric | vs armed |
|---|---|---|
| baseline | −29.619 | — |
| control, 4-stage LP 120 Hz | −49.994 → −27.776 | **swing −20.37, recovery 1.84** |
| ringmod armed on block 201 | −28.855 | +0.76 |
| `VFxO`=1, `[tag][0x14][idx 0][id 0][u32]` | −27.744 | +1.11 |
| `VFxO`=1, same with f32 | −28.923 | −0.07 |
| `VFxO`=1, `[tag][0x10][idx 0][u32]` | −29.011 | −0.16 |

Every swing is inside this run's own noise floor of ~1.8 dB, taken from the
control's recovery. **`'VFxO'` in these shapes is not an FX enable.**

Two limits on how strong this negative is, and they matter:

* **the blob shape is guessed.** `[tag][size][index][id][value]` is the
  parameter-blob layout, but `'VFxO'` may put its value elsewhere, need a
  non-zero index, or want a different size. Three shapes is not the space.
* **sensitivity was only ~2 dB.** The control recovered to 1.84 dB here, against
  0.01 dB from a cold boot in §12S. A subtle effect would not have been seen.

### The device was unharmed

Worth recording. An unknown blob tag was written to a DSP block six times and the
device stayed on app firmware with zero clock faults. That is further support for
§12G's correction — the two bootloader trips were transport-level, from sustained
streaming alongside control traffic, not from parameter or tag values. Write
experiments of this shape are safer than this document previously implied.

Next on the shortlist: `'IFac'` (14 comparison sites, insert-FX active?), then
the preset-shaped names. Each should stay a single-tag run, and each is worth
more from a cold boot, where the control gets an order of magnitude more
sensitive.

## 12Z. Blob layouts from capstone, and a one-off that did not reproduce (2026-08-03)

> **Update:** the +18.44 dB one-off is explained in §13 — the laptop's AC cable
> was unplugged at that moment, changing the noise picked up by an open input,
> and the metric amplifies source changes rather than cancelling them. The blob
> layout finding below is unaffected; it comes from disassembly.

### The tooling problem is solved

Every previous attempt to read this firmware's code by hand lost alignment —
Thumb-2 is variable length, and backward decoding kept landing inside literal
pools and producing nonsense (§12R, and the "code before the pool" attempt in
§12W). `objdump` here has no ARM target, but **capstone 5.0.7 is installed**, so
instruction boundaries are no longer guesswork. Any future disassembly in this
project should use it rather than hand-decoding halfwords.

### `'VFxO'` — the blob is 12 bytes, value at +0x08

At fw 0x3f26e the firmware **builds** a `'VFxO'` message:

```
ldr  r0, [pc, #0x2fc]     ; 'VFxO'
movs r2, #0xc             ; 12
movs r3, #1               ; 1
add  r1, sp, #0x1c
str  r0, [sp, #0x1c]      ; +0x00  tag
strd r2, r3, [sp, #0x20]  ; +0x04  size = 0x0c
                          ; +0x08  value = 1
bl   #0x600753d4
```

So the layout is **`[tag][size=0x0c][value]`** — three words, no `index`, no
`paramId`. §12Y's write test used the parameter-blob shape (`[tag][size][index]
[id][value]`, sizes 0x14/0x10) and therefore never placed the value where the
handler reads it. That negative is void.

### `'IFac'` is not a wire tag at all

At fw 0x21e98:

```
ldr r2, [r1]        ; blob[+0x00] — the tag
cmp r2, r3          ; == 'IFac'
ldr r3, [r1, #8]    ; blob[+0x08] — a sub-tag, compared against two constants
ldr r3, [r1, #0xc]  ; blob[+0x0c] — a POINTER
str r0, [r3]        ; *ptr = result
```

A wire blob cannot carry a host pointer. This is **firmware-internal lookup
code**, which also explains its 14 "comparison sites" — it is a dispatch helper,
not a protocol element. `'IFac'` is removed from the shortlist without costing a
write test, which is the point of doing the disassembly first.

### The retest: one large reading, not reproducible

With the corrected 12-byte layout, a ringmod armed, and a control swinging
−21.06 dB with 0.73 dB recovery, the **first** `VFxO=1` measured **−9.594 dB
against an armed baseline of −28.033 — a +18.44 dB swing**, twenty-five times the
noise floor.

It did not happen again. Follow-up with the model re-armed before every enable:

| trial | armed | `VFxO`=1 | Δ |
|---|---|---|---|
| 1 | −29.205 | −30.004 | −0.80 |
| 2 | −30.475 | −28.397 | +2.08 |
| 3 | −28.442 | −28.688 | −0.25 |
| no model armed | −29.354 | −28.596 | +0.76 |
| Doubler armed instead | −27.411 | −28.624 | −1.21 |

All inside the noise. The re-arm hypothesis (that `VFxO`=0 clears the model) is
disproved, and no variation reproduced it.

**The honest reading is that the +18.44 dB was a measurement artifact**, most
likely a spike in the preamp self-noise during that one 30-sample window — a
source observed drifting more than 20 dB elsewhere in this session. It is
recorded rather than discarded because it is the single largest excursion block
201 has ever produced, and if a future run with a *stable* source reproduces it,
this is where to look. But on the evidence it is noise, and **block 201 remains
unreachable**.

### What this changes

* `'VFxO'`'s layout is now known rather than guessed, so a future test of it is
  worth something. The right next move is the same test with a **stable source**
  — the USB tone cannot reach the FX send (§12M: `return/chN` has no `FXA`
  field), so it needs a real input signal, i.e. the user's pedal or a cable.
* `'IFac'` is eliminated.
* The shortlist is now `'PrsM'`, `'MBPt'`, `'SVPt'`, `'ISVP'`, `'PEvP'` — and
  each should have its layout read from the firmware *before* any write, which is
  cheap now that capstone is in play.

## 13. CORRECTION — the pre/post ratio metric does not cancel the source

**This section invalidates or weakens several measurements made earlier in this
document. Read it before trusting any result that used the "ratio metric".**

### The claim that was wrong

§9d introduced, and later sections leaned on, a supposedly source-independent
measure of the channel DSP path:

    metric = dB(JaSt slot 14, Mix A, post-DSP) − dB(JaSt slot 4, input 1, pre-DSP)

The reasoning was that both slots see the same source, so whatever the source
does cancels in the difference, leaving the gain of the DSP path. **That is not
true on this device.**

### The measurement

Preamp self-noise, nothing connected, gain swept with everything else fixed. If
the difference cancelled the source it would be constant:

| gain | slot 4 (pre) | slot 14 (post) | difference |
|---|---|---|---|
| 0 dB | −88.76 | −138.42 | −49.65 |
| 10 dB | −88.86 | −138.65 | −49.79 |
| 20 dB | −88.61 | −138.80 | −50.20 |
| 30 dB | −86.68 | −135.50 | −48.82 |
| 40 dB | −80.83 | −129.79 | −48.96 |
| 50 dB | −70.40 | −110.12 | **−39.73** |
| 60 dB | −60.07 | −89.79 | **−29.73** |

The difference moves **20 dB**. Worse, from 50 to 60 dB the pre-DSP slot rises
10.3 dB while the post-DSP slot rises **20.3 dB** — the post slot moves at
roughly **twice the dB slope**. Two slots with different slopes cannot be
subtracted in dB and called a ratio; the likeliest cause is that they are not the
same kind of quantity (one amplitude-like, one power-like), which would give
exactly a 2:1 slope.

So the metric does not cancel source changes. **It amplifies them**, by a factor
that depends on absolute level.

### How it was caught

A one-off +18.44 dB excursion during a `'VFxO'` write test (§12Z) that never
reproduced. It was initially written off as preamp noise drift. The user then
supplied the missing fact: nothing was connected to the inputs, and the only
thing that changed at that moment was **the laptop's AC power cable being
unplugged**.

An open, unterminated mic input at 60 dB gain is a high-impedance antenna, and
unplugging AC changes the common-mode noise environment it sits in. That is a
*source* change — precisely what the metric was supposed to be immune to. The
gain sweep above was run to test that explanation and confirmed the metric is not
immune to anything.

### What this invalidates

| section | status |
|---|---|
| §12L preset-slot sweep | **weakened.** "spread 1.27 dB vs repeatability 0.99 dB" is not a sound bound. The conclusion (slot index is a button assignment, not a recall trigger) still stands, but on the *documentation* and the 0..3-versus-8-presets arithmetic, not on this measurement |
| §12S block 201 negative | **weakened, probably still correct.** The metric does respond to real DSP changes — the low-pass control swung −21 dB — and `'VFxO'`/model writes gave ~0. But the stated sensitivity was overstated |
| §12Y, §12Z `'VFxO'` tests | **weakened.** Same reasoning, and §12Z's +18.44 dB is now explained as a source event, not an effect |
| any "recovery to 0.01 dB" claim | **does not mean 0.01 dB sensitivity.** It means the source happened to be stable across those two windows |

### What is unaffected

Every measurement that used **absolute bus levels against a calibrated USB tone**
rather than this ratio:

* the mixer gain law, exact to 0.03 dB over 106 dB (§9c)
* the aux bus master, exact to 0.00 dB with no crosstalk (§12F)
* the output delay, 50 ms asked and 49.50 ms measured (§12I)
* the differential loopback lag, 0 samples at correlation 1.000 (§12G)
* the Mix A / Mix B capture-channel mapping (§12G)

Those are sound. The distinction is that they compared a level against a known
reference, or two channels of one capture stream against each other, rather than
subtracting two device meters of unverified scaling.

### The correct method from here

1. **Use a stable, known source.** Preamp self-noise is not one — it has been
   observed moving more than 20 dB in a session, and it responds to the room and
   the mains environment.
2. **Keep both meters well above their floors.** Slot 14 was ~50 dB below slot 4
   at low gain; nothing measured down there means anything.
3. **Prefer absolute levels against a calibrated tone**, or the differential
   loopback rig of §12G, which compares two channels of the *same* capture stream
   and is genuinely immune to the source.
4. **Verify the observable in the same run**, at the level the experiment will
   use — not merely that a control produces *a* change, but that the metric is
   linear where the measurement sits.

The block-201 question in particular deserves a re-run under these rules before
it is called settled. The USB return cannot feed the FX send (§12M: `return/chN`
has no `FXA` field), so it needs a real input signal — a cable from an output
back to an input, or an instrument.

## 13a. SUPERSEDED — the three-object call sizes workspace; it does not install a chain (2026-08-03)

The disassembly below correctly identifies the three temporary object pointers,
but the “chain installer” interpretation is withdrawn by the 2026-09-14
controlling correction at the top of this document. Callee `0x6005e608` queries
each object's `vtable+0x18`, takes the maximum workspace requirement, and sizes
`device+0x11d0`; it does not retain or link these pointers.

A different line of attack on block 201, made possible by capstone (§12Z): stop
probing the wire and read what the firmware does with the object.

The resolver maps block 201 to `dev + 0x11e0`. Scanning the whole image for
instructions touching that offset gives 13 sites. One of them, at fw 0x3f110,
also touches the reverb's object (`dev + 0x3a70`) — and it is in the same
function that builds the `'VFxO'` message:

```
add.w r3, r5, #0x11e0     ; FX slot      (block 201)
movw  r8, #0x43a0         ; a third object
movw  sb, #0x3a70         ; reverb       (block 202)
movs  r2, #3              ; count = 3
str.w r8, [sp, #0x10]     ; chain[0] = dev+0x43a0
str   r7, [sp, #0x14]     ; chain[1] = dev+0x11e0    <- the FX slot
str.w sb, [sp, #0x18]     ; chain[2] = dev+0x3a70    <- reverb
bl    #0x6005e608         ; install this chain
```

**The FX slot is installed into a three-member processing chain**, alongside the
reverb and one other block, by an explicit call. It is not a processor that is
simply always in the path.

That reframes every result so far. §12S showed that writing model coefficients to
block 201 produces no audio, and §12T showed the model selectors are host-side
only, leaving the silence unexplained. A chain-membership model explains it
without contradiction: **the coefficients land in an object that is not currently
in the signal path.** Parameters written to a block that is not installed would
be stored faithfully and do nothing — exactly what was measured.

The function then labeled the chain installer, `0x6005e608`, has two callers:
this one, and 0x3e768,
which assembles a different set of objects (`dev+0x5048`, `dev+0x6a20`) and is
therefore a different chain.

### What to look at next

The question is no longer "what blob does block 201 want" but **"what causes the
chain at 0x3f13a to be built with the FX slot in it"**. Concretely:

1. disassemble the function containing 0x3f13a from its entry, and find the
   condition that reaches this basic block — the `bpl.w` at 0x3f0e8 and the flag
   tests before it are the immediate candidates
2. identify `dev + 0x43a0`, the third chain member, which is currently unnamed
3. work out what the `'VFxO'` message emitted by this same function is *for* —
   it is built here rather than parsed, so the device may be **reporting** FX
   state rather than accepting it, which would explain why writing it did nothing
   (§12Z/§13)

This historical next-step list is superseded. The controlling trace at the top
source-binds the real audio-root call, buffer handoff, and too-short Host
replacement sequence. The remaining target is live validation of the corrected
sequence, not `0x6005e608`.

## 13b. CORRECTION to §12W — the FourCC scan mixed wire tags with internal ones

§12W scanned the firmware for FourCCs that are **loaded and then compared**, and
presented the result as "the closed set of tags the firmware recognises",
treating the unidentified ones as candidate *wire* blob tags. That framing was
wrong, and it sent two write experiments (§12Y, §12Z) after things that could
never have worked.

### `'VFxO'` is an internal message, not a wire tag

At fw 0x3f26e the firmware builds a `'VFxO'` message — `[tag][size=0x0c][value]`
— and passes it to `0x600753d4`. That function is not a transmitter:

```
push {r4-r7}
ldr  r2, [r1]            ; r2 = message tag
strb r7, [r0, #0x2884]   ; mark the target object DIRTY
ldr  r3, [r1, #0xc]      ; selector
cmp  r3, #5
tbb  [pc, r3]            ; six-way dispatch
```

It takes `(object, message)`, sets a dirty flag, and dispatches — **firmware-internal
message passing between components.** Its five callers build `'Setu'` messages of
various sizes. Nothing here touches USB.

So `'VFxO'` is never compared by any USB blob dispatcher, and writing it over the
wire could not have had an effect regardless of layout. §12Z's negative was
therefore inevitable, and the effort spent guessing its blob shape was misdirected
— though the layout finding itself is correct, and the disassembly that produced
it is what eventually revealed this.

`'IFac'` was already shown internal by the same kind of evidence (§12Z: its
handler dereferences a blob field as a host pointer).

### The distinction that was missing

The firmware uses FourCC tags in **two unrelated roles**:

| role | examples | reachable from the host? |
|---|---|---|
| USB wire tags | `'SetP'`, `'GetP'`, `'Appl'`, `'Para'`, `'Pari'`, `'JaSt'`, `'MemP'`, `'Stat'`, `'FRst'` | yes |
| internal component messages | `'VFxO'`, `'IFac'`, `'Setu'`, and probably most of the rest | **no** |

A "loaded and compared" scan cannot tell them apart, because both roles compare
constants the same way. The remaining §12W shortlist — `'PrsM'`, `'MBPt'`,
`'SVPt'`, `'ISVP'`, `'PEvP'` — is contaminated the same way and **should not be
written to the device** on the strength of that scan alone.

### The correct test for a wire tag

A tag is reachable from the host only if it is compared in code **reachable from
the USB message dispatcher** at fw 0x2d50c. The dispatchers already enumerated
are the authority, and they are small and closed:

* command tags: `'SetP'`, `'GetP'` only (§12V)
* `'Appl'` SetP blobs: `'Para'`, `'FRst'`, `'Pari'` (§12P)
* `'Appl'` GetP blobs: `'JaSt'`, `'MemP'`, `'Stat'` (§12P)

What has **not** been done is the same enumeration for the *non-`'Appl'`* blocks
— the numeric blocks and the chain FourCC blocks each have their own blob
dispatcher, and those pools have never been read. That is the correct next step,
and it is offline: find the blob dispatcher reached for block 201 (and for the
chain blocks), and read its literal pool. Whatever is in that pool is the real,
short, closed list of what block 201 will accept.

That also finally answers the standing question properly. Rather than asking
"what makes block 201 work", ask what its dispatcher accepts — and combine it
with §13a, which shows the block is a chain member that must be installed before
anything written to it can be audible.

## 13c. Block 201's handler is GATED on three object flags (2026-08-03)

Traced from the USB dispatcher rather than guessed at. The route:

```
USB dispatcher (fw 0x2d50c)
  -> not 'Appl' -> numeric block resolver (fw 0x3ce98)
  -> block 201 resolves to dev + 0x11e0
  -> devirtualised at fw 0x3cf58:
        ldr r3,[r0] ; ldr r3,[r3,#0x18] ; cmp r3,r2
        add.w r0, r0, #0x11e0 ; b.w 0x60070af8
```

Block 201's object uses one of two vtables, **fw 0x10c508** and **fw 0x10c6cc**,
and slot +0x00 of both is `0x60070af8`.

### That function is a gate, not a dispatcher

```
0x50af8  push  {r4, lr}
0x50afc  ldr   r0, [r0, #0x14]      ; a delegate, if present
0x50afe  cbz   r0, +0x08
0x50b04  blx   r3                   ;   -> delegate vtable +0x10
0x50b06  ldrb.w r3, [r4, #0x44]     ; FLAG A
0x50b0a  cbz    r3, 0x50b22         ;   zero -> return, do nothing
0x50b0c  ldrb.w r3, [r4, #0x39]     ; FLAG B
0x50b10  cbz    r3, 0x50b22         ;   zero -> return, do nothing
0x50b12  ldr    r3, [r4, #0x40]     ; FLAG C
0x50b14  cbnz   r3, 0x50b22         ;   NON-zero -> return, do nothing
0x50b16  ldr    r3, [r4]            ; all three satisfied:
0x50b1a  ldr    r3, [r3, #0x34]     ;   tail-call own vtable +0x34
0x50b20  bx     r3
0x50b22  pop    {r4, pc}            ; the do-nothing exit
```

**Three conditions on the FX object must all hold** — `+0x44` non-zero, `+0x39`
non-zero, `+0x40` zero — or the handler returns having done nothing at all, with
no error and no reply difference.

This is the first mechanism found that actually fits every observation about
block 201: writes are accepted and stored, the device answers normally, and no
audio changes. Combined with §13a (the FX slot is a chain member that has to be
*installed*), the picture is a processor that is present, addressable, and
switched off.

### A correction, and the mistake that produced it

An earlier draft of this section claimed block 201's dispatcher "accepts exactly
`'Bqdf'` and `'Setu'`". **That was wrong.** The function comparing those two tags
is at fw 0x50b24 — the *next* function along — and it belongs to a different
class, vtable **fw 0x10c79c**. The error came from a helper that scanned a fixed
0x80-byte window from a function's entry and attributed every FourCC it found to
that function; the window spilled past `0x50b22` into the neighbour.

It was caught by dumping the vtables and checking which slot actually points
where, rather than trusting adjacency. Worth recording as the same failure mode as
§13b: a scan that cannot see structure will happily attribute things to the wrong
owner.

### What to do next

The question is now sharply defined and offline:

1. **What sets `[obj+0x44]` and `[obj+0x39]`, and what clears `[obj+0x40]`?**
   Search for stores to those offsets on an object derived from `dev+0x11e0`.
   One of them is very likely the "FX enabled" the host is supposed to set.
2. **Which of the two vtables** (0x10c508 vs 0x10c6cc) the FX object actually
   receives, and when it changes. 0x10c6cc has extra slots that compare `'IFac'`
   and `'MRSp'`; 0x10c508 does not.
3. Only then a write test, aimed at whatever sets the flags.

Note also that slot +0x34 — the thing the gate guards — is the real work
function, and neither vtable's +0x34 has been examined yet.

## 13d. The FX enable is internal firmware state, not a host parameter (2026-08-03)

Chasing §13c's three gate flags to their writers. All offline.

### SUPERSEDED — the filtered census claimed one setter for flag A (`obj+0x44`)

The 2026-08-31 controlling correction identifies direct setter `0x553a8` in
the block's model-selection/state-application path. The following is retained
as the historical, incomplete census that produced the earlier conclusion.

Image-wide there are eleven byte-stores to the three gate offsets. Filtering to
those that write flag A:

| site | value written |
|---|---|
| fw 0x2fd0c | 0 |
| fw 0x38088 | 0 |
| fw 0x5f184 | 0 — block 201's own vtable slot +0x24, i.e. the *disable* method |
| fw 0x59202 | 0 — the FX object's **constructor**, which also loads the `'VFxO'` literal |
| **fw 0x37e06** | **1** |

So the object is constructed with the FX gate **off**, and exactly one place in
the firmware turns it on.

### That setter is itself gated, and is not host-reachable

`fw 0x37e06` sits inside the function at **fw 0x37da0**:

```
push {r4, r5, r6, lr}
mov  r4, r1
ldrb.w r1, [r4, #0x45]
cbnz r1, +0x0a          ; [obj+0x45] must be NON-ZERO, else store 0 and return
ldrb.w r1, [r4, #0x46]
cbz  r1, +0x0a          ; [obj+0x46] must be ZERO, else store 0 and return
...
movs r3, #1
strb.w r3, [r4, #0x44]  ; only here does the FX gate open
```

Two further preconditions, and then the enable. This function is called from
three internal sites (fw 0x37ea4, 0x37f5a, 0x38004) and **appears in no vtable**
— so it is not reachable through the block dispatch the USB protocol uses.

### What that means

Enabling the FX is **firmware-internal state**, driven by conditions on the
object (`+0x45`, `+0x46`) that no host message sets directly. There is no
"turn the FX on" parameter to find, because the firmware does not expose one —
which is consistent with everything else observed:

* no `fxmodel` selector exists as a device parameter (§12T)
* block 201 accepts writes and stores them, and produces no audio (§12S)
* the effects nevertheless work on the device, via **presets** — the factory set
  includes Slap Echo, Detuned Vocal and Robot, and the user's own stored preset
  had a Ring Modulator with `on: 1` (§12K, §12M)

The coherent reading is that **the FX gate is opened by the firmware's own
preset-application path**, not by any host-writable control. That closes the loop
started in §12S: block 201 was never silent because of a wrong blob, a wrong
layout, or a missing selector. It is silent because the processor is switched off
by internal state that the wire protocol does not reach.

### Consequence for this driver

Voice FX cannot be driven from Linux by writing block 201, and no further
blob-tag or layout guessing will change that. The only path to Voice FX is
therefore the **preset store/recall** command — the same conclusion §12S reached
from measurement, now supported by the mechanism.

That also means Voice FX and device presets were never two problems. They are one.

### Method note

Three claims in this investigation were wrong before being corrected, all from
scans that could not see structure: §13b (a compare-scan cannot distinguish wire
tags from internal messages), §13c (a fixed-width window attributed a neighbour's
FourCCs to the wrong function), and §13's ratio metric. In each case the fix was
to check ownership explicitly — read the vtable, follow the call, verify the
observable — rather than trust proximity. Anything further in this firmware
should be done the same way, and capstone makes that cheap.

## 13e. The preset subsystem located, and its addressing decoded (2026-08-03)

The dispatcher sweep — enumerate every vtable, find slots that load AND compare
FourCCs, walk each function to a real return rather than a fixed window — found
29 blob dispatchers. Among them, the preset component.

### Per-block blob tags, finally enumerated

A by-product worth recording. Each DSP block has its own blob tag, and these were
never all known:

| dispatcher | accepts |
|---|---|
| fw 0x2d50c | `'Appl'`, `'SetP'`, `'GetP'` — the top-level message dispatcher |
| fw 0x2d9b8 | `'Para'`, `'FRst'`, `'Pari'` — `'Appl'` SetP |
| fw 0x2f1c8 | `'JaSt'`, `'MemP'` — `'Appl'` GetP |
| fw 0x50b24 | `'Bqdf'`, `'Setu'` |
| fw 0x50c5c | `'lim '`, `'Setu'` |
| fw 0x56e70 | **`'vrvb'`**, `'Setu'` — the reverb block |
| fw 0x57240 | **`'vech'`**, `'Setu'` |
| fw 0x58db0 / 0x59f50 | **`'botb'` / `'botc'`**, `'Setu'` |
| fw 0x420bc | `'Para'`, **`'APSP'`**, `'Stat'` |
| **fw 0x3099c, 0x30b70** | **`'PrsM'`, `'Stat'`** — the preset component |

Neither of block 201's vtables (0x10c508, 0x10c6cc) appears here, because their
slot +0x00 is the gate function of §13c, which compares nothing. Consistent.

### The preset addressing scheme

Vtable **fw 0x674f8** is the preset component. Four of its slots compare tags,
and slots +0x18 / +0x1c decode as:

```
ldr r3, [r1, #8]         ; blob[+0x08] = SUB-TAG
cmp r3, 'PrsM'  -> beq   ;   user preset path
cmp r3, 'Stat'  -> bne reject

'Stat' path:
    ldr r1, [r1, #0xc]   ; blob[+0x0c] = index
    cmp r1, #3
    bgt reject           ;   index must be 0..3

'PrsM' path:
    ldr r1, [r1, #0xc]   ; blob[+0x0c] = index
    sub.w r3, r1, #0x10  ;   index - 16
    cmp r3, #0xb
    bhi reject           ;   index must be 16..27
```

So the blob carries a **sub-tag at +0x08** and an **index at +0x0c**:

| sub-tag | index range | meaning |
|---|---|---|
| `'Stat'` | **0..3** | the four device button slots |
| `'PrsM'` | **16..27** | the twelve user preset slots |

### This matches the scene files exactly

§12M derived the preset library numbering from Universal Control's own `.scene`
files found on the user's machine: user presets keyed `16.Main.channel`,
`17.Main Wash.channel`, `22.Main.channel`, `23.Keys.channel`, with channel 1
occupying 16–21 and channel 2 occupying 22–27 — twelve slots, and four
`presets.slots` entries numbered 0–3.

**The firmware's own bounds check is `index - 16 <= 11`, i.e. 16..27, and `<= 3`
for the slots.** Two entirely independent sources — a host-side JSON file and an
ARM bounds check — agreeing to the exact number. The io44 manual's "6 preset
slots per channel, 12 in total" and "two presets per channel on the hardware
unit" is the third.

### Corrected incoming path (2026-08-31)

No separate preset block id is needed. Numeric block 0 resolves to the device
root, whose `MemP` handler forwards the original message through a stored child
pointer:

```text
SetP/Appl(0)/MemP
  -> root vtable +0x80 = 0x2f210
  -> root+0x70
  -> preset component root+0x1240, vtable 0x674f8
  -> component +0x18 = 0x3099c
  -> record writer 0x3092c
  -> bound firmware storage backend
```

Constructor chain `0x3c890 -> 0x30260 -> 0x2fca8` establishes the
`root+0x70` pointer. Section 13o records the full correction and exact backend
binding.

## 13f. SUPERSEDED: the component-addressability negative (2026-08-03;
corrected 2026-08-31)

This section originally treated `device+0x120c` as the preset component and
looked for that offset in the numeric and FourCC resolvers. Both premises were
wrong:

- `device+0x120c` is a formatted firmware-version string used by the
  `firmwareVersion` state label.
- The preset component is at `device+0x1240`, and root construction stores its
  address at `device+0x70`.
- `SetP | Appl(0) | MemP` first resolves block 0 to the root; root handler
  `0x2f210` then forwards to that child. The child therefore needs no block id
  of its own.

The old conclusion that the preset component is internal-only and unreachable
from the wire is withdrawn. The static save path now reaches the component's
bound storage-write interface; only live acceptance and survival remain to be
validated.

## 13g. WITHDRAWN — §13f's "not addressable" conclusion was premature

§13f concluded that the preset component has no routing block id, on the strength
of enumerating two resolvers. **That enumeration was not complete**, and the
conclusion does not stand.

### The non-`'Appl'` path is a delegate handoff, not a resolver

Tracing the top-level dispatcher (fw 0x2d50c) *forward*, which had never been
done — the resolver at fw 0x3ce98 was found separately and assumed to be what the
dispatcher reached:

```
0x2d510  ldr r6, [r2, #8]      ; message length
0x2d516  cmp r6, #0xb
0x2d518  ble 0x2d524           ; too short -> fall through
0x2d51a  ldr r7, [r2]
0x2d51e  ldr r1, [r7, #4]      ; the BLOCK field
0x2d520  cmp r1, r3            ; == 'Appl'?
0x2d522  beq 0x2d538           ;   yes -> the 'Appl' path

0x2d524  ldr r0, [r0, #0x28]   ; NO -> fetch a DELEGATE object
0x2d526  cmp r0, #0
0x2d528  beq <reject>          ;   none installed -> reject
0x2d52a  ldr r3, [r0]          ;   its vtable
0x2d52e  ldr r3, [r3]          ;   slot +0x00
0x2d536  bx  r3                ;   tail-call it
```

Everything that is not `'Appl'` is **handed to a delegate**, chain-of-
responsibility style. There is no fixed block table at this level, and nothing
here constrains what the delegate may accept.

### And there is a second complete dispatcher

The sweep of §13e had already surfaced **vtable fw 0x6d3a8**, whose slot +0x2c
(fw 0x419a0) is a second `'SetP'`/`'GetP'` message dispatcher, structurally
parallel to the top-level one:

```
cmp r8, #0xb ; ble reject          ; same length floor
ldr sb, [r2] ; ldr r7, [sb, #4]    ; same header shape
add.w r0, r5, #8 ; blx r3          ; <-- ITS OWN resolver, a virtual call
ldr r3, ='SetP' ; cmp sb, r3 ; beq
ldr r3, ='GetP' ; cmp sb, r3 ; bne reject
```

Its resolver is reached through a vtable slot on a different object, so it is
**not** the numeric resolver enumerated in §13a, and its block-id space has never
been read. The same vtable's slots +0x00 and +0x04 handle `'Para'`, `'APSP'`,
`'Stat'`, `'IFac'`, `'JaMD'`, `'MemP'` — and `'APSP'` is the one tag that returned
real data on block 0 (§12X).

### What is actually established, and what is not

Still true:

* only `'SetP'`/`'GetP'` exist as command tags (§12V)
* the preset component accepts sub-tags `'PrsM'` (index 16..27) and `'Stat'`
  (index 0..3), matching the scene files and the manual exactly (§13e)
* block 201's handler is gated on internal flags (§13c–§13d)

**2026-08-31 resolution:** a USB message does reach the preset component, but
not through an additional resolver. Block 0 selects the root and its `MemP`
handler `0x2f210` forwards through `root+0x70` to the component at
`root+0x1240`.

### Next

These historical next steps are closed by §13o. The decisive method was to index
the callable root vtable from `0x6d208` rather than from its two-word header at
`0x6d200`, then follow slot `+0x80` directly.

## 13h. `'APSP'` identified — stream properties, not the preset store (2026-08-05)

§12X's read-only probe was re-run with the device attached, and re-run properly:
15 candidate tags × 11 blocks × 7 declared sizes × 2 block indices, 2310 `GetP`
requests, nothing written. The device stayed on app firmware throughout.

**Exactly one (block, tag) pair returns data rather than an echo:** `'APSP'` on
**block 0**. It scores 16 non-zero bytes against a 5-byte echo baseline, at every
declared size ≥ 0x24. On `'Appl'`, 100, 201, 202 and 203 the same tag echoes
(nz=6), so this is block 0's alone.

### Correction to the first read

The first pass reported "32 distinct payloads across 32 indices" and read that as
an addressable array — the preset store. **That was an artifact of the dedup
key**, which included the blob's index word; the device echoes that word back
verbatim, so every reply differed by construction. The payload proper is
identical at every index, at every `block_index`, and does not move when device
state changes (toggling `'Pari'` 4 on ch1 off→on→off left it byte-identical, and
restored cleanly). **`'APSP'` is not addressable and is not an array.**

### What it actually contains

Two fixed records, at payload offsets 0x20 and 0x68:

```
0x20  02 00 00 00  02 00 00 00  01 00 00 00  02 00 00 00     2, 2, 1, 2
0x30  04 00 00 00  06 00 00 00  00 00 00 00  00 00 00 00     4, 6
0x68            01 00 00 00  04 00 00 00                     1, 4
0x70  04 00 00 00  06 00 00 00                               4, 6
```

The bytes are **not** a static table — neither record appears anywhere in the
firmware image, so the structure is assembled at runtime. The producer is at
**fw 0x6005cd4e** (file 0x3cd4e), and it maps field for field:

```
movs r3, #0x9c              declared size 156
ldr.w ip, [pc, #0x7c]       ip = 'APSP'  (pool 0x3cdcc)
movs r2, #0x3c ; bl memset  record A buffer, sp+0x24, 0x3c bytes
movs r2, #0x40 ; bl memset  record B buffer, sp+0x60, 0x40 bytes
mov.w ip, #2   ; strd ip, ip, [sp, #0x18]    -> 2, 2
str r2, [sp, #0x20]     (r2 = 1)             -> 1
str.w lr, [sp, #0x24]   lr = [r5]            -> 2
movs r3, #4    ; strd r3, r8, [sp, #0x28]    -> 4, r8 = [r5+0xc] = 6
                 strd r2, r3, [sp, #0x64]    -> record B, from [r4], [r4+0xc]
blx r5                                        emit
```

So the `4` is a **hardcoded constant** (`movs r3, #4`) and the `6` is read from a
stream object at `+0xc`. The routine then does
`vldr s0, [r6] ; vcvt.f32.u32 s0, s0 ; blx r3` — converting a u32 to float and
passing it to a callback, i.e. a sample rate.

**Reading:** `'APSP'` is an audio-port/stream properties structure — two records,
playback and capture, each carrying sample width and channel count, emitted when
the rate changes. ALSA corroborates the two constants: this device is `S32_LE`
(**4** bytes per sample) with **6** channels in each direction. An earlier guess
that `[4, 6]` meant *(number of supported rates, channels)* is wrong — the 4 is
immediate, not table-derived.

**Consequence for the preset hunt: none.** The one readable new tag is a format
descriptor. `'PrsM'`, `'MBPt'`, `'SVPt'`, `'ISVP'`, `'PEvP'`, `'VFxO'`, `'IFac'`
all echo on every block at every size tried, so they remain write-only, exactly
like `'Para'`/`'Pari'` — which is the shape a preset *store* would have. §13g's
next steps stand unchanged; this closes off the read-only route rather than
advancing it.

### Verified but not done

Whether `'APSP'` tracks the live rate (48k vs 96k) would confirm the reading
outright, but it needs a stream open on the card while control traffic runs —
the one condition associated with the bootloader drops (§12G). Not attempted.

## 13i. CORRECTION to §12W — the FourCC vocabulary was never closed (2026-08-05)

§12W claimed to have recovered "the closed set of tags the firmware recognises"
by finding PC-relative loads of printable FourCC literals followed by a
comparison. **That set was incomplete by construction.** Found while tracing the
second dispatcher (§13g step 2), at fw 0x41c1c:

```
ldr   r2, [pc, #0x280]      ; = 0x41505350 'APSP'
cmp   r3, r2 ; beq          ; compared as-is
add.w r2, r2, #0x1f60000    ; then MUTATED into a neighbour
add.w r2, r2, #0x1fa00
add.w r2, r2, #0x100        ; r2 = 0x43484e50
cmp   r3, r2                ; = 'CHNP' -- appears nowhere as a literal
```

Thumb-2 cannot encode an arbitrary 32-bit immediate, so the compiler
materialises a nearby tag by adding to one already in a register. **Any tag
produced this way is invisible to a literal scan.** The §12X read-only probe was
therefore run against a short list, which weakens its negative result.

### Method, and two failed attempts worth recording

Anchoring on literal pools and disassembling from convenient offsets does not
work: Thumb-2 is variable length, so whether an instruction decodes correctly
depends on where the walk started. A first pass (4 KB boundaries) missed the very
chain above; a second (pool − 0x400) found 430 literal loads and then *zero*
comparisons, which is impossible on its face.

The decisive bug: **`capstone.disasm()` stops at the first byte sequence it
cannot decode.** On a whole-image linear walk one bad halfword silently ends the
pass — reported as "0 loads seen". The working method is two linear passes
(offset 0 and offset 2) *with resync*: on a stall, advance one halfword, clear
register state, continue; union the passes.

Only a value live at a `cmp` counts. Intermediates in an add-chain are printable
by coincidence — `'Ladf'` is a way-point to `'Lfdf'`, `'M\`SP'`/`'MekP'` to
`'MemP'`, `'omm '`/`'opq '` to `'opt '` — and an earlier draft that reported them
as discoveries was wrong.

### Result: 84 tags reached by a comparison

The method self-validates by rediscovering known relationships without being told
them: `'Lfdf'` ← `'Bqdf'` (§12W had noted `'Bqdf'` is the real casing of the blob
the driver sends as BQDF), `'Pari'` ← `'MemP'`, `'opt '` ← `'lim '`, `'eq  '` ←
`'gate'`, `'MemP'` ← `'FRst'`.

**Synthesised tags (fw offset of the `cmp`):**

| tag | built from | at |
|---|---|---|
| `'CHNP'` | `'APSP'` | 0x41c28 |
| `'VoFx'` | `'VFxO'` | 0x553f2 |
| `'MRSp'` | `'IFac'` | 0x50e16 |
| `'MLff'` | `'MBdf'` | 0x513ea |
| `'Lfdf'` | `'Bqdf'` | 0x507dc |
| `'Pari'` | `'MemP'` | 0x41ba2 |
| `'MemP'` | `'FRst'` / `'APSP'` | 0x2d9d8 / 0x420e0 |
| `'StCV'` | `'StRV'` / `'StIV'` | 0x2771a / 0x27752 |
| `'StVw'` | `'StLb'` | 0x2773a |
| `'Grph'`, `'GRnd'` | `'BGRd'` | 0x2378a, 0x2377e |
| `'OwSM'`, `'IwSM'` | `'OiaS'` | 0x2169a, 0x216a2 |
| `'fbFA'`, `'dbFA'` | `'fbBF'` | 0x21652, 0x2165a |
| `'opt '` | `'lim '` | 0x4f026 |
| `'eq  '` | `'gate'` | 0x4effe |
| `' BSU'` | `' NOM'` | 0x2173e |

**Tags never probed, ranked by comparison-site count** — sites are a proxy for
how much the firmware dispatches on a tag:

`'Setu'` (18), `'Ltcy'` (13), `'MRSp'` (8), `'Redu'` (5), `'StVw'` (5),
`'StCV'` (4), `'MBdf'` (4), `'IOSp'` (2), `'MIDS'` (2), `'StCt'` (2),
`'StTB'` (2), `'StVB'` (2), `'StVV'` (2), plus singletons `'CHNP'`, `'VoFx'`,
`'PBSt'`, `'ACtl'`, `'ADOb'`, `'GPIO'`, `'LEDi'`, `'JDCt'`, `'DsP2'`, `'StCB'`,
`'StIV'`, `'StLb'`, `'StRV'`, `'Grph'`, `'GRnd'`, `'GmBR'`, `'GmCo'`, `'vrvb'`,
`'vech'`, `'godv'`, `'inia'`.

Note `'Setu'` with 18 sites: §12S attributed `'Bqdf'`/`'Setu'` to block 201 and
that attribution was withdrawn as a proximity error, but the tag itself is real
and heavily dispatched. `'VoFx'` is a *second* Voice FX tag distinct from
`'VFxO'`, which §12Y tested and got a negative from.

### Next

Re-run the read-only probe (§12X) against this vocabulary before drawing any
further conclusion from its silence. The previous negative covered 15 tags; there
are now ~60 unprobed, several of them state- and setup-shaped.

## 13j. The expanded probe — two new readable tags, neither of them presets (2026-08-05)

§13i's vocabulary was probed read-only: 58 previously-unsent tags × 11 blocks ×
4 sizes = 2552 `GetP` requests, then a full size × block-index sweep on the hits.
Nothing written; the device stayed on app firmware throughout. This retires the
§13h claim that `'APSP'` was the only readable blob — **it was not, because the
vocabulary being probed was short.**

### `'CHNP'` — channel names, on block 0

Addressable by the **blob index** (unlike `'APSP'`, which echoes it):

```
index 0 -> "Input 1"
index 1 -> "Monitor Left"
index 2+ -> empty
```

Only block 0 answers; `block_index` makes no difference (0–5 all give "Input 1");
no other block returns a name at any index 0–23. So the exposed table is two
entries, not a full channel map. Payload is a NUL-terminated ASCII string at
offset 0x20.

`'CHNP'` is the tag synthesised from `'APSP'` at fw 0x41c28 — **it could not have
been found by a literal scan at all**, which is the concrete pay-off of §13i.

### `'Redu'` — a float array on the dynamics blocks

Answers on `'gate'`, `'lim '`, `'opt '` and block 203; **not** on `'cpxt'`, and
`'Appl'`/100/201/202 only echo. Layout: floats from offset 0x20, with an element
**count at offset 0x54**.

| block | count | floats |
|---|---|---|
| `'opt '` | 6 | 1.0, 1.0, 1.0, 0, 0, 0 |
| `'gate'` | 2 | 0.0, 0.0 |
| `'lim '` | 2 | 0.0, 0.0 |
| 203 | 2 | 0.0, 0.0 |

This is **gain reduction metering** — and, correcting this section as first
written: that is not an inference and was never an open question. **§9a already
records it measured on hardware**, long before this probe: `opt '`/`'Redu'`
returning count 6 with values `0.9997, 1, 0.9085, 1, 0.9998, 1` (≈ −0.83 dB live
reduction), and `'Redu'` tracking live limiting from −2 dB down to −33.9 dB on
instance 0 while instance 1 stayed at exactly 1.0 with speech on input 1.

The values looked static here only because nothing was streaming. The error was
treating `'Redu'` as a new discovery: it is not new at all. `read_reduction()`
has existed in the driver (io24.py) since §9a, and the app already draws
gain-reduction bars from it. It was included in this probe's tag list by mistake.
**`'CHNP'` is the only genuinely new readable tag found here.**

`'cpxt'` not answering is explained in §13n.

### Everything else echoes

The remaining 56 tags — including `'Setu'` (18 comparison sites), `'Ltcy'` (13),
`'MRSp'` (8), `'VoFx'`, `'PBSt'`, `'IOSp'`, the whole `'St**'` family, and the
chain FourCCs tried as blob tags — return the request echoed back on every block
at every size. They are **write-only, exactly like `'Para'`/`'Pari'`**.

### Where this leaves the preset store

The readable-tag sweep did not expose preset content, which is expected: the
save path is a separate `SetP | Appl(0) | MemP` dispatch. Section 13o now traces
that path through nested `Stat`/`PrsM`, root handler `0x2f210`, and the firmware
storage backend. No speculative write experiment or tag shortlist is needed to
discover it.

### Incidental gains

Two features fall out of this regardless of the preset question: channel-name
readback, and dynamics gain-reduction metering for the app's meters — the latter
pending confirmation that the values move under signal.

## 13k. PARTLY SUPERSEDED — one resolver, but the inferred preset address was
wrong (2026-08-05; corrected 2026-08-31)

§13g withdrew §13f's "not addressable" conclusion on the grounds that a *second*
dispatcher existed whose block-id space had never been read. The resolver
finding below remains valid: both entry points use the same numeric resolver.
The later conclusion that this restores the addressability negative does not.

### The second dispatcher shares the first one's resolver

The chain, followed rather than inferred:

```
fw 0x419f0   ldr r3, [r5, #8] ; add.w r0, r5, #8 ; ldr r3, [r3] ; blx r3
```

The constructor at fw 0x41580 builds a singleton at RAM 0x20216660:

```
r3 = 0x6008d3a8                       ; vtable A  (file 0x6d3a8)
str r3, [r4]                          ; obj+0x00 = vtable A
add.w r0, r3, #0x48 ; str r0, [r4,#4] ; obj+0x04 = file 0x6d3f0
add.w r5, r3, #0x54 ; str r5, [r4,#8] ; obj+0x08 = file 0x6d3fc
```

Slot 0 of the vtable at file 0x6d3fc is fw 0x3cf20, which is a **C++ adjustor
thunk**, not a resolver:

```
0x6005cf20  sub.w r0, r0, #8    ; undo the caller's +8
0x6005cf24  b     #0x6005ce98   ; tail-jump
```

fw 0x3ce98 is the numeric block resolver already documented in §13a. So the
"second dispatcher" is a second *entry point* onto the same resolver — the extra
vtable is multiple inheritance, not a second block space. **§13g step 2 closes
negative.**

### The complete block map (never fully read before)

```
0x6005ce98:  cbz  r3      -> mov r3, r0            block 0   = dev itself
             cmp  #0x64   -> dev + 0x3e28 + 0x74*i block 100 = mixer, i <= 2
             cmp  #0xc9   -> dev + 0x11e0          block 201 = singleton
             cmp  #0xca   -> dev + 0x3a70          block 202 = singleton
             cmp  #0xcb   -> dev + 0x3f84 + 0x4c*i block 203 = i <= 2
             otherwise    -> 0  (reject)
```

Block 0 resolving to `dev` itself is why `'APSP'` and `'CHNP'` answer there and
nowhere else (§13h, §13j).

### Where the preset component actually sits

The old `dev+0x120c` identification was wrong; that location is the formatted
firmware-version string. The preset component is constructed at
`dev+0x1240`, and constructor chain `0x3c890 -> 0x30260 -> 0x2fca8` stores its
address at `dev+0x70`. Root block 0 handler `0x2f210` forwards `MemP` through
that pointer. It is therefore reachable without a numeric block id of its own
and is not a member of block 201.

### The write experiment, and why it did not proceed

The paesdk header carries a status byte at offset 5 that `Io24._exec` discards.
If it distinguished accepted from rejected commands it would identify a live
handler with no need to guess blob layouts — a cheap, low-risk lever. Measured:

```
'Para' id2 written back to its own value (true no-op)   status = 0x00
'ZZZZ' on 'Appl'  (garbage tag)                          status = 0x00
'QQQQ' on block 0 (garbage tag)                          status = 0x00
```

**The firmware ACKs at transport level regardless of whether it understood the
command.** No discrimination, so the lever does not exist, and the run stopped
before writing anything speculative. Device state verified byte-identical
against a 2040-byte `'JaSt'` snapshot; unit healthy.

A blind blob sweep remains the wrong next move, but for a different reason: the
exact host-built `MemP | Stat/PrsM` record and its firmware destination are now
known. The unresolved block-201 work concerns audible Voice FX activation, not
preset-record ingress.

## 13l. The gate's enable path has virtual entry points — §13d's reachability claim was too strong (2026-08-05)

§13d claimed flag A (`obj+0x44`) had exactly one setter, fw 0x37e06 inside
fw 0x37da0, and concluded: *"This function is called from three internal sites
and appears in no vtable — so it is not reachable through the block dispatch the
USB protocol uses."*

The premise is true and the conclusion does not follow. **Whether the setter
itself is in a vtable is irrelevant if one of its callers is**, and §13d never
walked the call graph upward. Doing that (call edges from `bl` immediates,
function boundaries from `push {...,lr}` rather than fixed windows — the error
mode of §13c):

```
0x37da0  (sets flag A)
  <- 0x37e2c <- 0x48174, 0x48424, 0x4952a
  <- 0x37ec4 <- 0x47bc0, 0x4a2f4          *
  <- 0x37f7c <- 0x4952a
       0x48424 <- 0x486ac <- 0x48842, 0x489ac
                             0x489ac <- 0x489e4 <- 0x48a7c  *  <- 0x48b68
```

Two ancestors **are** pointer-referenced, i.e. reachable through a virtual call:

* **fw 0x48a7c** — vtable at fw 0x10bba4, slot **+0x18**
* **fw 0x4a2f4** — fw 0x10bc18

So the enable path *does* have virtual entry points. §13d's "internal state, no
host-writable control" may still be right in the end, but it was asserted on
insufficient evidence and is downgraded to open.

### Flag B has six writers, two of them in the same chain

§13d chased only flag A. Flag B (`obj+0x39`) is far more tractable than A or C —
image-wide there are just **six** byte-stores to +0x39, versus 362 to +0x44 and
205 to +0x40 (which are ordinary structure offsets in unrelated classes):

| site | in function |
|---|---|
| fw 0x1a764, 0x1a7e8 | fn 0x1a580 |
| fw 0x32b58, 0x32c2e | stack-relative (`[sp, #0x39]`) — not the object |
| **fw 0x488a0, 0x488b4** | **fn 0x48842** |

`fn 0x48842` is in the flag-A ancestor chain above (it calls 0x486ac). **One
routine plausibly opens both gates** — which is what a single "enable this
processor" method would look like.

### What the gate actually guards

Slot +0x34, never examined before. Both of block 201's vtables point at an
adjustor thunk into heavy VFP code:

```
0x10c508 +0x34 -> fw 0x555b8:  sub.w r0,r0,#4 ; b.w 0x7545c
0x10c6cc +0x34 -> fw 0x54ed8:  sub.w r0,r0,#4 ; b.w 0x74ec0
   ... vldr/vsub.f32/vmov.f32 s5,#1.0 ...
```

This was classified here as the audio processing routine. The 2026-08-31
controlling correction shows that the vtable was indexed sixteen bytes late:
`0x54ed8` and `0x555b8` are secondary structured-state adjustors, while the
actual primary slot `+0x34` is file word `0x10c6f0 -> 0x5f1f4`. The historical
classification below this point must not be used as current vtable ownership.

### Open, and precisely stated

**Which class owns vtable fw 0x10bba4, and is it reachable from the numeric block
resolver (§13k)?** If it is, the enable is host-reachable and §13d is overturned;
if it is not, §13d stands with a proper argument behind it.

### Method note

A byte-search for "literals pointing into the vtable region" produced hits inside
executable code (fw 0x48e94 is `cbnz r4`, fw 0x49d00 is `pop {r3,r4}`) — 4-byte
patterns matching at unaligned offsets. Pointer-reference searches must require
4-byte alignment and a non-code location. Same family of error as §13b/§13c: a
scan that cannot see structure attributes things to the wrong owner.

## 13m. PARTLY SUPERSEDED — heap-node construction is real; its block-201 gate interpretation is not (2026-08-05)

The heap-node construction trace below is retained. Its interpretation as the
block-201 audio-enable path is withdrawn: the controlling typed lifecycle shows
`block+0x44` is target bypass used to stage safe model replacement, and the
actual audio-root call and buffer handoff are source-bound elsewhere.

§13l reopened §13d by showing two ancestors of the flag-A setter are
pointer-referenced (`fw 0x48a7c` in vtable fw 0x10bba4 slot +0x18, and
`fw 0x4a2f4`). The question was whether the class owning that vtable is reachable
from the numeric block resolver (§13k). It is not, and the reason is structural.

### Who owns vtable fw 0x10bba4

The constructor is **fw 0x48e24**. It is a multiply-inherited class:

```
ldr   r1, [pc, #0x6c]    ; = fw 0x10bb9c   vtable group base
add.w r6, r1, #8         ; = fw 0x10bba4   primary vtable (skips the ABI header)
add.w r5, r1, #0x34      ; = fw 0x10bbd0   secondary
add.w r4, r1, #0x4c      ; = fw 0x10bbe8   secondary
str   r6, [r0]           ; obj+0x00 = primary vptr
str   r5, [r0, #0x60] ; str r4, [r0, #0x64] ; str r1, [r0, #0x68]
```

This is why the earlier literal search for the vtable's VA found nothing:
secondary vtables of a multiply-inherited class are **computed** as base+offset
and never named by a literal (the same pattern as fw 0x41580 in §13k).

### The objects are heap-allocated chain nodes

The constructor has exactly one caller, inside **fw 0x4a764**, which is a builder
loop:

```
ldrd r6, sl, [r4]          ; r6 = descriptor array, sl = entry count
loop:
  ldr r3,[r7] ; movs r2,#4 ; movs r1,#0x7c ; blx r3   ; ALLOCATE 0x7c bytes
  bl  0x48e24                                         ; construct in place
  ldr r3,[r5,#0x20] ; str r4,[r5,#0x20]               ; link into a list
  bl  0x6006a460                                      ; per-node init
  ldr r3,[r6,#4] / [r6,#8] ; bl 0x6006a6a8            ; two optional sub-items
  adds r6, #0xc ; add.w r8,r8,#1 ; cmp sl,r8          ; next descriptor, stride 0xc
```

So each node is **dynamically allocated and linked into a list** at
`[r5+0x1c/0x20/0x24]`. It has **no fixed offset within `dev`**. The numeric
resolver (§13k) maps only fixed offsets — `dev`, `dev+0x11e0`, `dev+0x3a70`,
`dev+0x3e28+0x74·i`, `dev+0x3f84+0x4c·i` — so **no block id can name a chain
node**, and the virtual entry points found in §13l are unreachable from the wire.

### And the builder itself is not dispatcher-reachable

```
fw 0x4a764 (builder)  <- fw 0x34dfc  <- fw 0x35034  <- (no callers, no pointer refs)
```

`fw 0x35034` is called by nothing and appears in no table, i.e. it is an
initialisation/task entry, not something a message routes to.

### Conclusion

**§13d's conclusion stands, with a correct argument behind it.** The FX gate is
not opened by a host-writable control; it is opened while the firmware *builds
the signal chain* from an internal descriptor array, which is exactly what §13a
predicted ("the FX slot is a chain member that has to be *installed*"). §13d
asserted this from an irrelevant premise — that the setter is not itself in a
vtable — and §13l was right to reject that reasoning even though the conclusion
survives.

Restating the three flags in that light: they are not a switch the protocol
forgot to expose. They are the record of whether this processor was installed
into a chain, written by the installer, on an object the wire cannot name.

### Consequence, unchanged but now fully explained

Voice FX and device presets remain one problem, reachable only through the
firmware's own chain-building path — i.e. preset recall. No blob tag, no block
id, and no layout guess can substitute, because the target of such a write is a
heap node with no address in the block-id space.

### Method limit worth stating

The call graph is built from `bl` immediates plus a pointer-reference check for
indirect entry. Calls made through a register whose value comes from a structure
this analysis did not model would not appear. "Not reachable" therefore means
"no path found by those two mechanisms", which is strong but not a proof of
absence.

## 13n. Pan implemented host-side, and `'cpxt'` explained (2026-08-07)

### Pan was never missing — it was never a device parameter

Today's firmware sweep established there is no pan/balance/width parameter in the
image, and an earlier draft of this work reported that as "pan does not exist".
That framing was wrong, and the correction came from the user: **Universal Control
panned host-side.** The device never needed a parameter.

§9c already recorded UC's formula — it folds everything into the one number the
mixer accepts:

```
volume_dB + aux_dB + 20*log10(blend) + pan_dB      -> one write
```

and §9d already recorded the law itself, recovered from the host binary:

```
g(x) = K*x^2 + (1-K)*x,  K = -0.831783
g(0.5) = 0.7079458   =  exactly -3.00 dB at centre
mode 1 = left leg g(1-pan),  mode 2 = right leg g(pan)
```

Both were sitting in this document, unused, because the search had been framed as
"find the pan parameter" rather than "reproduce what the host did".

### Implemented

`set_pan(source, bus, pan)` and `set_pair_pan(...)` in io24.py, folded into
`_push_send` alongside level, assign and bus master; `set_pan` added to
`_SHADOWED` so it survives into saved presets; CLI verb
`pan <source> <bus> <0..1|left|centre|right|off>`.

Verified offline against the recovered constants: `pan_gain(0.5)` reproduces
0.7079458 and 20·log10 of it is −3.00000 dB exactly; hard left gives left leg
1.0 and right leg 0.0; a −6 dB send at centre pan reaches the wire as −9.0 dB.

**Verified on hardware 2026-08-09.** Signal on input 1 only, `line/ch1` and
`line/ch2` into Mix A, measuring the Mix A bus meter (JaSt slot 14) against the
input meter (slot 4) so programme-level variation cancels, interleaved repeats:

| pan | measured Δ | predicted Δ | error |
|---|---|---|---|
| 0.00 hard left | reference | — | — |
| 0.25 | −1.21 dB | −0.86 dB | −0.35 |
| 0.50 centre | −3.86 dB | −3.00 dB | −0.86 |
| 1.00 hard right | source removed, bus fell to −154.85 dB | −144 dB commanded | — |

Every point is inside the measurement spread (±1.0 to ±1.7 dB per position). The
precision is limited by using live programme material with peak-hold rather than
a steady tone; a tone would tighten it, but the monotonic ordering and the exact
extremes already settle the question.

The send path itself was calibrated first, to be sure any discrepancy belonged to
pan rather than to the mixer: commanded −3/−6/−12/−20 dB produced −3.28/−5.64/
−11.26/−20.59 dB at the bus, i.e. **linear in dB** as §9c states.

*Measurement note.* A first attempt compared absolute bus levels and produced an
apparent −6.42 dB at centre against a −3.00 dB prediction. That was an artefact:
two nominally identical configurations measured 4.4 dB apart because the source
was speech, not a tone. Normalising bus level against input level removed it.
Peak-hold on programme material is not a 3 dB instrument.

### The honest limitation

Block 100 holds exactly **one level per (source, bus)**, and the mixer blob's
`index` field is *measured-ignored* (§9c: writing index 1/2/3 moved both legs by
the same 1 dB). So a single mono source cannot be placed in the stereo field at
all. What is implemented is therefore a **balance across a stereo pair** —
`line/ch1`+`line/ch2`, `return/ch1`+`return/ch2` — which is what pan means on a
two-input interface and matches UC's own two-mode law. `set_pan` on a source with
no partner raises rather than silently attenuating it.

This also explains §9d's "structurally dead" note without contradicting it: the
*device-side* pan term is dead on the io24, which is precisely why the host has to
do the arithmetic.

### `'cpxt'` does not answer `'Redu'` because it is not a block

Settled today: `'cpxt'` is the compressor's SetP **blob tag**, not a block id. The
compressor's block is `'comp'`. A GetP addressed to block FourCC `'cpxt'` is
rejected by the chain resolver (fw 0x4efe8) before any handler runs — before the
`'Rply'` header is written and before the request-blob copy — which is why it
returns nothing at all rather than an echo. `read_reduction()` in io24.py has
always used `'comp'` and was never affected. Same wire-tag-versus-block confusion
that §13b warned about.

### Also fixed: `send ... off` never reached the wire

`_push_send` tested `lvl is None` to mean "never set", which conflated that with
"explicitly set to off". `set_send_db(src, bus, None)`, `mix_off()`, and the CLI
`send <src> <bus> off` were **all silent no-ops**. Now distinguished by key
presence, verified offline: an explicit off
writes the `MIXER_OFF_DB` sentinel, an unset send still writes nothing.

## 12R. The preset button, settled at the hardware (2026-08-10)

A physical button session with the app open settled what three sections of
this file could not agree on:

* **`'Pari'` 4 is the per-channel preset ENABLE**, not the slot-count mode.
  Press-and-hold on either channel latched the bypass and the host switch
  followed (JaSt slot 42 bits 5/6, as §6 said). §12K's reading of id 4 as
  `presetButtonMode` was wrong — the DLL's `presetButtonMode` is internal
  id 8 in the GLOBAL descriptor table (§12E), a different parameter.

* **The four slot indexes are global, two owned per channel**: resting state
  reads `input1SlotIndex=0`, `input2SlotIndex=2`; the hardware button lands a
  channel on its own pair's first slot (ch1 → 0, ch2 → 2). Host-writing a
  channel's index into the OTHER channel's pair is accepted but produces
  half-states (block indicated off, or nothing indicated, while the button
  LED stays lit) — observed live on both channels.

* **Two-slot button cycling is a host feature, settled by elimination.**
  Global id 8 (`presetButtonMode`) was written to 1 and then 2 on both indexes
  (2026-08-10, device healthy throughout, restored to 0 afterwards): zero
  delta in readable state, and the physical button still cycled nothing —
  confirmed by tap tests after each write. The owner's recollection matches:
  in Universal Control the two-block behaviour "was only enabled to device via
  host" — UC implemented the cycling in software. A press while presets are
  already enabled produces **no readable transition** in JaSt (taps observed
  live against a 50 Hz poll), so there is no event a host can hook to
  replicate it; the app's per-channel slot pair is the equivalent control.
  What draws the device's own two-block panel display remains unfound; id 8
  at values 1 and 2 is now an eliminated candidate. Selecting the channel's
  own SECOND pair slot from the host (2026-08-10) also adds no secondary
  block on the panel — the index moves, the display does not, reinforcing
  that the whole two-block presentation was Universal Control's own drawing
  rather than device state.

## 12S. `'Pari'` 4 is a boolean at the machine level (2026-08-10)

Disassembled the wire-id-4 handler in io24_fw.bin (the 'Pari' tbh table at
0x2e49a routes id 4 to 0x2e55e). The handler is:

    vmov.f32 s0, #1.0
    cmp      r2, #0          ; r2 = the written value
    vseleq   s0, s15, s0     ; s15 = literal 0.0 (@0x2e7f8)
    ...store s0...

i.e. **stored = (value == 0) ? 0.0 : 1.0** — any non-zero input collapses to
1.0. There is no three-way mode; `presetMode` in descriptor table 1 is only the
parameter's NAME, its handler is a plain enable/disable toggle. Writing 2 (the
"dual" guess) is byte-for-byte identical to writing 1. Confirmed live: id 4 = 2
on both channels produced no state change and no panel change.

The firmware analysed IS the user's build: the USB device descriptor embedded
at fw 0x680e4 reads bcdDevice **1.28**, matching the device's own descriptor.
So on 1.28 the two-block front-panel indication is not parameter-reachable, and
the `presetButtonMode(8)` global descriptor entry has no observable effect (id 8
= 1 and 2 both inert, §12R). Best supported conclusion: the two-block behaviour
was Universal Control's own host-side UI — it drew and cycled the second block
itself — consistent with the owner's account that it "was only enabled to
device via host." The panel shows one block per channel on this firmware
because that is all the firmware itself draws.

## 13o. `SetP` + `'MemP'` IS accepted — §12P was wrong (2026-08-10)

**The preset store is writable in principle. The negative in §12P/§12L came from
searching for a literal constant that the firmware never stores.**

The `'Appl'` SetP blob dispatcher at fw 0x2d9b8 compares the incoming blob tag
against four values, and only three of them are literals:

```
02d9be  ldr    r5, [pc,#0x5c]      ; 0x2da1c = 'Para'  -> handler 0x2da02
02d9c6  ldr    r4, [pc,#0x58]      ; 0x2da20 = 'FRst'  -> handler 0x2da14
02d9cc  add.w  r4, r4, #0x7100000  ; 'FRst' + 0x7100000 = 'Mbst'
02d9d0  add.w  r4, r4, #0x2f800    ;        + 0x2f800   = 'Mekt'
02d9d4  add.w  r4, r4, #0x1dc      ;        + 0x1dc     = 'MemP'   <-- SYNTHESISED
02d9d8  cmp    r3, r4
02d9da  bne    ...                 ; no match -> return
02d9dc  ldr    r3, [r0]            ; else: vtable
02d9e0  ldr.w  r3, [r3, #0x80]     ;       slot +0x80
02d9e4  bx     r3                  ;       tail call
```

`'Pari'` is the fourth (literal at 0x2da24, reached via the `bgt` at 0x2d9c4).
Slot assignment is self-confirming: the same vtables carry the SetP dispatcher
at +0x90 and the known GetP pool (fw 0x2f1c8) at +0x94.

### Corrected root dispatch and preset-store handoff (2026-08-31)

The 2026-08-10 trace indexed the root vtable from its two-word header. That was
an eight-byte error. The header is at fw `0x6d200`; the callable vtable begins
at fw `0x6d208`:

| root-vtable location | slot | target |
|---|---:|---|
| `0x6d228` | `+0x20` | fw `0x3c064` — lifecycle/cache work |
| **`0x6d288`** | **`+0x80`** | **fw `0x2f210` — `MemP` handler** |

The source image is 1,104,448 bytes with SHA-256
`65ad2b65bcef932b41e748bfab87f62a9e529c17cceaa0a6a7679e387952eb1a`.
The pointer words are respectively `0x6005c065` and `0x6004f211`, so this is
not a heuristic function-proximity assignment.

The root handler reaches the preset store directly:

1. Constructor chain `0x3c890 -> 0x30260 -> 0x2fca8` stores
   `device+0x1240` at `device+0x70`.
2. `0x3c8dc` constructs `device+0x1240` with preset vtable `0x674f8`.
3. `0x2f210` loads `device+0x70`, selects child vtable slot `+0x18`, and calls
   it with the original `MemP` message. Vtable word `0x67510` resolves that
   slot to `0x3099c`.
4. `0x3099c` accepts `Stat` 0..3 and `PrsM` 16..27 and calls writer `0x3092c`.
   The writer stores the four-byte record length and each serialized fragment
   through wrapper `0x2bfe4`.
5. Startup path `0x31ab0 -> 0x2d660 -> 0x300ac -> 0x306b4 -> 0x2c1c0`
   binds that wrapper to the firmware storage backend. Backend vtable
   `0x676ec` maps its write entry to `0x316dc`, which forwards the write to the
   underlying storage object with flag 1.

So `SetP | Appl(0) | MemP | Stat/PrsM` is not merely accepted: its record bytes
reach the preset component and the component's bound storage-write interface.
The earlier "preset store remains unreached" conclusion is withdrawn.

Per the resolver (below) the root is **block id 0**.

### The complete numeric block resolver (fw 0x3ce98), including its default

```
block 0     -> dev                                  (ROOT)
block 100   -> dev+0x3e28 + 0x74*idx    (idx<=2)
block 201   -> dev+0x11e0               (singleton)
block 202   -> dev+0x3a70               (singleton)
block 203   -> dev+0x3f84 + 0x4c*idx    (idx<=2)
default     -> idx<=1: dev+0x43a0 + 0xcec*dev[0x175c+idx]
               then TAIL-CALLS fw 0x4efe8, the chain FourCC resolver
```

The default arm is not a rejection: **any unrecognised block id with index 0 or
1 resolves to that channel's processing chain**. §13a recorded the five mapped
ids and treated the rest as unreachable; they are not.

### What fw 0x3c064 actually is

fw `0x3c064` remains a lifecycle/cache routine, but it is not vtable slot
`+0x80` and does not receive `MemP`. It republishes input-state views and marks
their cache state:

```
r5 = object base;  r6 = base+0x2704, later base+0x3368
for name in (input1Gain, input1PhantomPower, input1HighPassFilter,
             input2Gain, input2PhantomPower, input2HighPassFilter):
    obj = lookup(r6, {table, name})        ; fw 0x47dc8
    obj->vtable[0]('StVw')                 ; state view
    obj[0x40] |= 2                         ; mark modified
```

It also calls `0x302b0`, whose literals identify `logoImage` and
`firmwareVersion`. That helper binds the formatted firmware-version string at
`device+0x120c` to its state label. The string is produced from format
`"Ver. %d.%02d"`; `device+0x120c` is therefore not a preset component or a
callable delegate. The prior `StLb`/preset interpretation is withdrawn.

Hardware reads taken the same session: `GetP`+`'MemP'` on **block 0** returns
the echoed TLV header with an **empty body**, where `'Appl'` returns the known
single `0x02` at blob+0x16. That remains a valid `GetP` observation; it does not
negate the separately dispatched `SetP` storage path.

### What is still genuinely open

Packet discovery is no longer open. The exact JSON record, `Stat`/`PrsM`
addressing, root dispatch, firmware record writer, and bound storage-backend
handoff are known. Live work, if separately authorized, is now limited to
validating acceptance, editor-independent recall, power-cycle survival, and
audible Voice FX activation/restoration.

## 13p. `'FRst'` sent for the first time — it is a live-parameter defaults
restore, and it does NOT reach the preset slots (2026-08-10)

First time this project has issued `SetP` + `'FRst'`. Accepted; device healthy
throughout. Measured effect, before/after full `read_params()`:

| parameter | before | after |
|---|---|---|
| `hpVolume` | 0.770 | 0.263 |
| `mainVolume` | 0.431 | 0.263 |
| `input1Gain` | 5.0 | 0.0 |
| `input1SlotIndex` | 0 | **0 (unchanged)** |
| `input2SlotIndex` | 2 | **2 (unchanged)** |
| `flags` | 0x0181 | **0x0181 (unchanged)** |

So §6a's reading was right — it restores *live parameters* to defaults (the
0.263 value is the firmware's own default volume) — and it confirms the
disassembly of the root object's handler at fw 0x2f820: setParam calls only,
nothing touching persistent storage.

**Consequence for the preset-slot problem:** the slot ASSIGNMENT is not a live
parameter, so no defaults restore can rebuild it. That rules out the last
live-parameter route. The actual store path is the separately dispatched
`SetP | Appl(0) | MemP | Stat/PrsM` transaction traced in §13o; `FRst` is not
part of it.

Operational note: `FRst` flattens gain and both volumes, so anything invoking
it should save and restore them, or warn. The historical
`io24_firmware.py defaults` command has been removed from the current
offline-only package tool; this record is evidence about the old live run, not
permission to repeat it.

Also confirmed live this session: the unit exposes a **DFU interface (class
0xfe subclass 0x01) at interface 6 while in normal application mode** —
`1-1:1.6`, seen in sysfs with the device running. This proves interface
exposure only. The 2026-09-01 package recovery corrects the historical
inference that a raw `dfu-util` path was established: the old 0x10da40-byte
extraction was truncated, while UC registers a 0x12a694-byte metadata-bearing
package and performs compatibility/image-selection work that this project has
not reproduced. Do not attempt a raw DFU write from this project.

## 13q. The chain resolver's accept-set is complete — and the "unswept default
arm" was never unswept (2026-08-10)

Two corrections and a closed door.

**Correction 1 — the default arm ignores the block id.** fw 0x3cef0 uses only
the *index* ([r1+4], must be 0 or 1); the id itself is discarded. So there is
no space of "unmapped block ids" to sweep: every unmapped id reaches the same
per-channel chain object.

**Correction 2 — that path is not unexplored, it is the Fat Channel.** This
driver sends `SetP | 'gate' | channel` — the FourCC occupies the *block* field.
As a u32 `'gate'` is not 0/100/201/202/203, so it falls to the default arm by
construction. Every EQ, gate, compressor and limiter write this project has
ever made went through it. §13a's "unreachable" and §13o's "never swept" were
both wrong.

**The accept-set, enumerated with register tracking** (the §13i technique that
found `'MemP'`), following every add-chain in fw 0x4efe8:

| tag | sub-object offset | literal or synthesised |
|---|---|---|
| `'filt'` | +0x58 | synthesised |
| `'gate'` | +0x204 | literal @0x4efe8 |
| `'comp'` | +0x454 | synthesised |
| `'eq  '` | +0x6d4 | synthesised |
| `'lim '` | +0xc40 | literal @0x4f014 |
| `'opt '` | +0 | synthesised |

Anything else: `movs r0,#0 ; bx lr`. **Six tags, all six already in use.** There
is no seventh chain element.

### Consequence, corrected 2026-08-31

* SetP blob tags: `'Para'`, `'Pari'`, `'FRst'`, `'MemP'` — all four traced to
  their handlers (§13o, §13p). **`MemP` writes preset content.**
* Numeric blocks: 0, 100, 201, 202, 203, default→chain. Fully mapped (§13o).
* Chain tags: the six above. Complete.

The complete resolver never needed to return the preset child directly. Block 0
returns the root, and root handler `0x2f210` forwards `MemP` through
`root+0x70` to the child at `root+0x1240`. Thus the save operation is an
ordinary control message, not a DFU-only path. The earlier structural-boundary
and reflashing conclusions are withdrawn.

## 13r. SOLVED: `presetMode` is `'Pari'` wire 17, and the two-block display
works on 1.28 (2026-08-10)

**The two-block front-panel preset display is reachable from Linux. No
firmware downgrade is needed.** Established by writing each value and reading
the unit's own screen:

| `'Pari'` wire 17 value | front panel |
|---|---|
| 0 | one preset block per channel |
| **1** | **two blocks per channel** — what the manual documents |
| 2 | no blocks (Preset button LEDs stay lit) |

### Why it took so long: internal ids are NOT wire ids

The DLL vocabulary lists `presetButtonMode` at **internal id 8**, and §12R
probed **wire id 8** on that basis, saw nothing, and recorded the candidate as
eliminated. Wire 8 is internal id **5** (`muteMode`) — an unrelated parameter.
The map only exists in the firmware's own `'Pari'` dispatcher table (fw
0x2e49a); walking each arm and recording the `movs rN,#id` it performs gives:

| wire | internal | name |
|---|---|---|
| 4 | 4 | presetEnable (coincidentally equal — the trap) |
| 8 | 5 | muteMode |
| 16 | 1 | input2SlotIndex |
| **17** | **2** | **presetMode** |

`presetMode` is also in descriptor table 1 as id 2, `int` — visible all along,
never connected to a wire id.

### The handlers differ in kind, and that is the tell

```
wire 4  (presetEnable)      wire 17 (presetMode)
  vmov.f32 s0, #1.0           movs r1, #2        ; internal id
  cmp   r2, #0                movs r3, #0
  vseleq.f32 s0, s15, s0      str  r5, [sp,#0x28]  ; the raw VALUE, stored as-is
  -> collapses to 0.0/1.0     -> keeps the integer
```

Wire 17 takes no index register — `presetMode` is **global**, not per-channel,
consistent with its single descriptor entry.

### Write-only

Swept all 503 state floats while toggling 1 <-> 0: **no slot tracks it**. The
driver can command the mode but cannot read it back, so any UI must present it
as a command rather than a follower.

### Consequences for earlier sections

* §12R's "two-slot cycling was UC host-side software" is **WITHDRAWN** — it
  rested on the wire-8 mis-probe. The behaviour is firmware, on 1.28.
* The user's downgrade hypothesis is not needed. 1.28 retains the feature.
* `io24.py` now exposes `set_preset_mode(0|1|2)` with wire 17 mapped in
  `KNOWN_PARI`, and the GTK app has a "Preset blocks on the unit" control.


### 13r-b. The Preset button IS fully observable — in dual mode (2026-08-10)

§12R recorded "press-while-enabled emits NO readable JaSt transition". That was
measured with `presetMode` at **single** (one slot per channel), where a press
has no second slot to move to — so nothing changed because nothing *could*.
It was a property of the mode, not of the button.

Re-measured with `presetMode = 1` (dual), 90 s at 20 Hz, taps and holds:
**33 transitions**, every one predicted by the pair model:

| observed | meaning |
|---|---|
| `input1SlotIndex` 0 <-> 1 | channel 1's button walks pair {0,1} |
| `input2SlotIndex` 2 <-> 3 | channel 2's button walks pair {2,3} |
| flags 0x0181 -> 0x01a1 | bit 5: channel 1 preset bypassed (hold) |
| flags 0x0181 -> 0x01c1 | bit 6: channel 2 bypassed |
| flags 0x0181 -> 0x01e1 | both bits: both bypassed |

So the front panel is **readable**, and a host UI can and should follow it.
Note the asymmetry that remains: `presetMode` itself (wire 17) is write-only —
no state slot tracks it — while everything the *button* does is reported.

**§12R's finding is superseded**: it is true only in single mode, and this
project inherited it as a general claim. Two earlier attempts to re-measure it
recorded "0 transitions" from windows in which nobody pressed anything; a null
result from an unattended window is not evidence, and both were discarded
rather than recorded.

---

### §13s — the preset recall does NOT open the Voice FX gate (measured)

§13m left one live hypothesis: block 201's gate flags are set by the firmware's
signal-chain builder, and no host message was known to invoke it. Once preset
recall worked from Linux (§13r) and the factory preset table turned out to
carry `voicefx` sections, a recall became the obvious candidate for a message
that runs the builder as a side effect.

It does not. `tests/fx_gate_probe.py` measures it, and the number that makes
the result trustworthy is the **positive control**, not the null.

The rig needs no loopback and no signal source: a cable left in an input and
unterminated is an antenna, and at 55 dB of gain it delivers mains hum well
clear of the floor. Mix A loops back to USB capture 3-4, so the path is
entirely inside the box. Two facts were established by A/B rather than assumed:

- **Capture channel.** Muting the Mix A send drops capture channels 3 and 4 to
  -280 dBFS and leaves 1, 2, 5 and 6 untouched. Mix A is 3-4, confirmed.
- **The off sentinel.** `set_send_db(src, bus, None)` is off; `-144.0` is
  merely a very low fader and still passes signal. An early version of this
  test used -144 and read a -4.6 dBFS "floor", which looked like a broken
  measurement and was in fact a working one being misread.

Stepping one variable at a time, twice, with consistent results:

| step | rms (dBFS) | |
|---|---|---|
| 1. every Mix A source hard-off | -280.0 | true digital silence |
| 2. + FX return into Mix A | -4.2 | the return bus carries |
| 3. + FX send open, mix fully wet | -24.2 | dry falls away |
| 4. + reverb on, block 202 | **-18.2** | **+6.0 dB — positive control** |
| 5. voice FX on, block 201 | -24.5 | **-0.3 dB** |
| 6. after a preset recall | -22.3 | +1.9 dB, and 60 Hz moved with it |

Step 3 is itself informative: `set_fx_mix` is a dry/wet blend on the return
path, so at mix 0 the return carries the input at -4.2 dBFS and at mix 1 it
carries the block's output. Blending fully wet costs 20 dB because what the
block produces is nothing.

Step 4 is the whole point. The reverb reaches Mix A through the *same* send,
the *same* return and the *same* capture in the *same* run, so the rig
demonstrably detects a working DSP block. Against that, block 201's -0.3 dB is
a real negative rather than a rig failure. The +1.9 dB in step 6 tracks a
2.5 dB rise at 60 Hz in the same capture — ambient hum drift, not an effect
appearing; it sits under the 3 dB detection threshold and did not reproduce.

**Conclusion.** Preset recall is not the way in. §13m stands: block 201 is
gated by flags this project has no established way to set, and the Voice FX
models remain decoded, byte-correct, storable and inaudible. What §13r bought
is preset *presentation and recall*, not the FX gate.

**2026-09-14 DSP Amount clarification.** A later authorized no-listening test
made the step-3 blend law quantitative without relying on USB audio capture.
With a controlled +15 dB/1 kHz high-shelf on Channel 2, simultaneous JaSt
Input-2/Main meter ratios placed 0/25/50/75/100% DSP Amount at normalized
positions 0.0000/0.2473/0.4917/0.7552/1.0000. Exact zero retained the dry
signal; it did not mute or scale the output from zero. The functional result is
a linear dry-to-full-chain blend, not a send or channel fader. Waveforms and
meters cannot prove that firmware literally runs two parallel signal paths.

This whole-chain blend is not equally meaningful for every module. Parallel
compression is a conventional use; intermediate values undermine an HPF or
corrective EQ, leak around a gate, and restore peaks around a limiter. Host UX
should therefore present Bypass as the normal chain on/off, recommend 100%
amount for corrective/dynamics protection, and reserve intermediate values for
deliberate parallel processing. Effect-specific Voice FX/Reverb mix controls
remain distinct.

---

### §13t — §13m's call-graph claim corrected; the chain registry mapped; wire sweep negative (2026-08-11)

**The correction.** §13m closed the FX gate question partly on "`fw 0x35034` is
called by nothing and appears in no table". That is false. A whole-image
Thumb-2 sweep of every `BL`/`B.W`/`BLX` target finds a complete chain:

```
fw 0x4a764 (builder)  <- fw 0x34dfc  <- fw 0x35034  <- fw 0x352b0  <- fw 0x21068
```

`fw 0x352b0` is a multiple-inheritance thunk (`add r0, r0, #0x188` then tail
`B.W`), and `fw 0x35034` is type-dispatched: it loads the descriptor's vtable
slot +0xc, compares it against `fw 0x331f4`, and only builds when it matches.
None of that was visible to a data-word scan, which is why §13m missed it —
the same synthesised/computed-address blind spot as §13k and §13l.

**The conclusion still holds, one frame further up.** `fw 0x21068` is called
only from inside itself and has zero pointer references, so the subtree is
still not reachable from any wire message. §13m was right for the wrong reason,
and this is recorded because "verified unreachable" and "unreachable as far as
the tool looked" are different claims and the file asserted the stronger one.

**New: the chain component registry.** `fw 0x34dfc` resolves its components
from a singleton at **RAM 0x202158a0** (vtables `fw 0x66120`/`fw 0x66110`),
through vtable slot +4, keyed by **FourCC**. The table it draws from is 14
entries at VA 0x60087a20:

```
PrFb PrDs PrOb   Mbfo Mbfi Mbdo Mbdi   Bbfc Bbfr Bbdc Bbdr   AFob AFbf AFbd
                                                              ^^^^^^^^^^^^^^
                                                     the three the builder resolves
```

This is a **namespace the project had not seen** — distinct from the numeric
block-id space and from the blob tags (`hcev`, `bota`, `inia`, `FRst`, `MemP`).

**And it is not a way in.** All 14 were swept read-only, in both wire
positions. As a block tag they return 0 bytes; as a blob tag inside `Appl` all
14 return the identical 28-byte **echo**:

```
Rply Appl 00000000  <tag> 00000010 00000000
```

— the request reflected back with a zero payload. Acceptance again carries no
information, exactly as §13q found for the block resolver. The registry is
real, but it lives behind a virtual call on a heap singleton, not behind a tag.

**Still open, in the order worth trying:** (1) does a **sample-rate change**
rebuild the chain — every coefficient depends on fs, so something must, and it
is host-triggerable; (2) re-derive **§13c's three-flag premise** from scratch
rather than inheriting it, given §13m and §12R both proved wrong on inspection;
(3) a **Windows/UC differential capture**, which would settle it outright; (4)
the separately scoped custom-firmware investigation. The latter is not yet a
reflash candidate: only the complete 0x12a694-byte vendor package is valid for
inspection, raw DFU transport/compatibility is unverified, and the bootloader
is not a demonstrated recovery guarantee.

---

### §13u — mining the Universal Control install: what it gave, what it didn't (2026-08-11)

The user's UC 5.0.0 install was searched for anything FX-related the project
did not already have. It yielded a great deal, and none of it opens the gate.

**PreSonus's own component model, recovered.** `Plugins/studiolivepanel.dll`
carries its device model as plain-text XML inside the binary — 12
`uc:ComponentModel` documents, 372 KB, found by scanning for printable runs.
Kept in `re/uc_component_model/`. This is the authoritative source for
parameter names, ranges, defaults, curves and flags, and **it agrees with this
project's decode on every Voice FX parameter** — the detuner really is a 0..8
list index over `TuneList` = -8..0 (so it only pitches down), Filters really
are `pitch`/`regeneration`, and the per-model volume parameters are real. That
decode was done from disassembly with no access to this file.

It also names things: `<!-- Doubler -->` sits directly above
`<uc:ParamList id="VoiceOfGod">`, giving the same processor a *third* name.
And it declares two capabilities nothing exposes — the vocoder's `avoiced`
readonly output, and `VocoderMode` (cheapo / rms follow) and `VocDetectorMode`
(off / Voiced-Unvoiced), which are defined but referenced by no parameter.

**`UC/fatchannelplugins.package`** is `PACKAGEF` + 278 back-to-back zlib
members (18 XML skins, 260 PNGs). UI assets only.

**A tag namespace we had never seen.** Scanning `.text` of
`hwaccess/dspusbdevice.dll` for FourCC immediates written into buffers
(`mov dword [rsp+N], imm32` and `mov r32, imm32`) enumerates every tag UC
builds. Beyond the known set it writes `IFxS`, `IFac`, `Comp`, `gate`, `filt`,
`comp`, `Setu`, `ASet`, `DspC`, `Diag`, `Stat`, `PrsM`, `MRSp`, `TUpd`,
`FwVn`, `MIOH`, `HtDM`, `HtDQ` and more.

`IFxS` looked decisive — it matches `classname="InsertFXSelectorComponent"`
exactly. It is not. At file 0x03cf81 the code is

```
mov  rcx,[rax] ; mov r8,[rcx]
mov  edx, 'IFxS'
call r8                      ; virtual lookup(this, fourcc) -> ptr
test rax,rax ; jz ...        ; null-checked
mov  ecx,0x30 ; <allocate>   ; not found -> construct 0x30 bytes
```

— a **host-side factory keyed by FourCC**, structurally the same shape as the
firmware's own component registry in §13t, and not a wire message.

**Measured anyway.** All nine FX-plausible tags were sent as `SetP` blobs to
block 201 with the vocoder armed, each followed by a capture, against a reverb
positive control in the same run:

```
wet baseline, no effect      -103.2 dBFS
REVERB positive control       -99.0 dBFS   (+4.1 dB)  rig OK
IFxS IFac Comp gate filt comp Setu ASet DspC   +0.1 .. +0.5 dB
```

Nothing. The reverb proves the rig detected a working block throughout.

**Also worth recording:** the DLL embeds **three** ARM firmware images (the
DSP-USB family), which is why FX tag constants appear in three widely
separated regions — those are firmware literal pools, not host data. Only the
`0x01f000..0x023000` cluster is x86 message-building code.

**Where this leaves it.** The gate is not in UC's data model, not in its
component keys, and not in any tag it writes. What has never been captured is
UC *talking to the device*. The user confirms the Voice FX were audible under
UC on this unit, so a working sequence exists on the wire; every attempt to
infer it statically has now failed, twice from the firmware side (§13m, §13t)
and once from the host side (here). A **USB capture of UC enabling the FX** is
the remaining approach that does not require guessing.

---

### §13v — UC's control path is WinUSB; Linux feasibility claim corrected by §13aa (2026-08-11)

Every static route to the Voice FX gate has now failed: from the firmware side
twice (§13m's chain builder is unreachable from the wire; §13t corrected its
reasoning but not its conclusion) and from the host side once (§13u — UC's data
model, component keys and every FourCC it writes, all measured negative).

The user confirms the Voice FX **were audible on this unit under Universal
Control**. A working sequence therefore exists on the wire. It has never been
recorded, and recording it needs no guessing.

The import table of `hwaccess/dspusbdevice.dll` establishes that the installed
module expects fourteen WinUSB calls:

```
WinUsb_Initialize          WinUsb_WritePipe        WinUsb_ReadPipe
WinUsb_ControlTransfer     WinUsb_GetDescriptor    WinUsb_SetPipePolicy
WinUsb_GetPipePolicy       WinUsb_ResetPipe        WinUsb_AbortPipe
WinUsb_SetCurrentAlternateSetting  WinUsb_GetAssociatedInterface
WinUsb_QueryDeviceInformation      WinUsb_GetOverlappedResult  WinUsb_Free
```

plus SETUPAPI for enumeration. This proves the Windows API expected by the
module; it does not prove that Wine implements that API. The original version
of this section incorrectly conflated Wine's `wineusb.sys` USB bus enumerator
with Microsoft's WinUSB function-driver and user-mode API stack. §13aa records
the correction: Wine 11.15 and current upstream 11.16 leave all fourteen
imported calls semantically unimplemented, and Wine does not generate or bind
`USB\MS_COMP_WINUSB` for MI05/MI06.

The io24's vendor interface being available to usbfs is therefore necessary
but not sufficient. Before this capture route can exist, Wine needs Microsoft
OS descriptor translation, a generic WinUSB function binding, and the imported
WinUSB API surface. Interface ownership remains a separate later gate.

**Tooling, written and verified 2026-08-11:**

- `tests/uc_wine_capture.sh` — loads usbmon, records the io24's bus, and drives
  either this project's driver (`baseline`, a sanity check) or UC under Wine
  (`uc`).
- `tests/usbmon_decode.py` — reads a usbmon text capture, unwraps the paesdk
  frame and the FourCC TLV inside, and flags every command, block and blob tag
  this project has never sent.

The decoder was proved before any hardware run, against synthetic frames built
from this project's own `io24_fx` payloads with one foreign tag planted:

```
OUT ep2 SetP Appl idx 0 blob FRst len 16
OUT ep2 SetP #201 idx 0 blob VoFx len 16
OUT ep2 SetP #201 idx 0 blob inia len 452
OUT ep2 SetP #201 idx 0 blob bota len 40
OUT ep2 SetP #201 idx 0 blob IFxS len 16    ** NOT IN OUR SET **
```

It read the real payloads correctly and caught the planted one.

**Capture mechanism only.** usbmon needs sudo; an LD_PRELOAD interposer on
libusb does not. Wine's USB bus library links libusb normally (DT_NEEDED +
PLT), so `tests/usbtap.c` catches transfers that actually reach that library.
It was verified against a purpose-built normally-linked C caller, but that
test never established that `WinUsb_*` calls from Universal Control reach the
library. Under current Wine they do not: the user-mode entry points are stubs.

It does NOT work for this project's own driver, and the reason is worth
recording — pyusb loads libusb through `ctypes.CDLL`, and a dlsym against that
handle resolves to libusb's own symbol rather than the preloaded one. The
baseline is therefore taken by `tests/usbtap_py.py`, which wraps pyusb's
endpoints. Both taps emit the same usbmon text, so the captures diff directly.

**BASELINE CAPTURED 2026-08-11.** `~/.cache/io24/capture/baseline.txt` —
40 transfers, the full Voice FX vocabulary this project can produce:

```
commands : SetP 19, GetP 1, Rply 1
blocks   : #201 x16, Appl x4, #100 x1
blob tags: VoFx 6, Para 3, Bqdf 3, JaSt 2, inia, bota, godv, may4, botb, botc, vech
```

Every model's blob is in it, so anything UC sends beyond this stands out.

**The deferred experiment.** After a complete WinUSB compatibility stack and
interface-ownership design are proven offline, capture `uc` with the FX audibly working, then
`uc_wine_capture.sh diff`. Whatever UC sends that this project does not is the
gate. If the difference turns out to be empty — with the FX genuinely working
in that session — then the gate is not a message at all, §13m's conclusion
becomes the final word, and a firmware patch is the only remaining route. That
is a real outcome, not a failure.

---

### §13w — UC under Wine: the USB half works, the GUI half is blocked by DirectComposition (2026-08-11)

Wine 10.0 installed and a dedicated prefix built at `~/.wine-io24`.

**The bus-enumerator part works.** Wine's USB bus stack is live and reaches libusb:
`wineusb` is registered as "Wine USB bus driver" in the prefix, and
`winedevice.exe` maps `libusb-1.0` at runtime. The presence of files named
`winusb.dll`, `wineusb.sys`, and `wineusb.so` was incorrectly taken as a
complete WinUSB stack. §13aa proves the distinction: `wineusb.sys` is a bus
enumerator, `wineusb.inf` matches only `root\wineusb`, and `winusb.dll` does
not implement the calls Universal Control imports. The USB path therefore
still has a substantial host-side obstacle.

**"This application requires Windows 10 or later" is a misleading message.** The
string lives in `cclgui.dll` surrounded by `D2DGraphicsDevice`,
`Direct2DEngine`, `D2DGradient` — it is what UC prints when its graphics stack
fails to initialise, not a version test. The prefix was already reporting
Windows 10 build 19043, and raising it to 19045 changed nothing.

Tracing `+d2d,+dxgi` shows Direct2D itself works fine: DXGI device created, D2D
device context created, brushes created. Execution then stops dead on

```
fixme:dcomp:DCompositionCreateDevice <dxgi_device>, {c37ea93a-...}, <out>
```

Wine's `dcomp.dll` is 141 KB and exports `DCompositionCreateDevice`,
`...Device2`, `...Device3`, `...CreateSurfaceHandle`, but the v1 entry point is
a stub. UC's own error string `"DirectComposition scratch bitmap failed"`
confirms it depends on DComp for real work, not just device creation.

`dcomp.dll` is a **static** import of `cclgui.dll`, so
`WINEDLLOVERRIDES=dcomp=d` does not induce a fallback — it breaks image loading
outright (`import_dll ... not found, status c0000135`).

**`PreSonusHardwareAccessService.exe` exits immediately when run directly** and
logs nothing: it is an SCM service and expects to be started by the service
control manager, not launched from a shell.

**Where that leaves the capture.** Everything downstream is ready and verified —
`usbtap.c` (LD_PRELOAD, no root), `usbtap_py.py`, `usbmon_decode.py`, and a
complete baseline. The only missing piece is a UC that will render.

Four routes, in order of cost:

1. **An older Universal Control**, from before UC adopted the DirectComposition
   renderer. Still supports the io24, and nothing else about this setup changes.
   No reverse engineering, and the most likely to simply work.
2. **wine-staging**, which carries DComp patches Ubuntu's `wine 10.0~repack`
   does not.
3. **Patch `cclgui.dll`** to take its non-composited path — `CompositedRenderer`
   and `NativeGraphicsEngine` both exist in the binary, so the branch is there.
   Deterministic, invasive.
4. **Headless**: install the hardware service under Wine's SCM and drive it over
   **UCNET** — `dspusbdevice.dll`'s first import is `ucnet.dll`, so the service
   exposes the device and the GUI is only a client. No graphics needed at all,
   but it needs a UCNET client written against `re/UCNET_SHIM_SPEC.md`.

---

### §13x — the DComp blocker is Wine's, not UC's: all three entry points are E_NOTIMPL (2026-08-11)

§13w blamed `DCompositionCreateDevice` on the strength of a `fixme` being the
last line before UC's dialog. Disassembling Ubuntu's own
`wine/x86_64-windows/dcomp.dll` confirms it and goes further — **all three**
creation entry points are stubs:

```
DCompositionCreateDevice   (RVA 0x13b0)   mov eax, 0x80004001   ; E_NOTIMPL
DCompositionCreateDevice2  (RVA 0x14c0)   mov eax, 0x80004001   ; E_NOTIMPL
DCompositionCreateDevice3  (RVA 0x15d0)   mov eax, 0x80004001   ; E_NOTIMPL
```

The `test byte ptr [rip+N], 1 / jne` ahead of each is only the print-the-fixme-
once guard; every path falls through to E_NOTIMPL. So the obvious cheap fix —
shim `DCompositionCreateDevice` to forward to `...Device2` — is worthless,
because v2 is just as empty.

This matters because it moves the blocker from "UC is too new" to "Ubuntu's
Wine is too plain". **wine-staging carries a `dcomp-DCompositionCreateDevice2`
patchset** that mainline does not, which is precisely the missing piece.

**Two independent routes, and they can be tried in either order:**

1. **wine-staging.** One package install, no downloads, and it targets the
   actual stub. Caveat: the patchset may only construct a device object, while
   UC's own error string `"DirectComposition scratch bitmap failed"` shows it
   also needs working surfaces.
2. **An older Universal Control**, from before the renderer changed. UC 5 is the
   January 2026 Fender rebrand and the most likely place a framework bump
   landed; UC 4.5.0.102825 (November 2024) is documented and supports the
   Revelator line. PreSonus gates older downloads behind an account, so no
   direct URL could be confirmed from here.

**Do not guess route 2 — measure it.** `tests/check_dcomp.py` walks a UC
installation (or a single extracted `cclgui.dll`) and reports whether the build
imports `dcomp.dll`. Verified against the installed 5.0.x, which it correctly
flags. Any build it reports clean is worth running under Wine.

The installer format is NSIS, so an older installer can be run under Wine into
a throwaway prefix — no extraction tooling needed.

---

### §13y — Wine staging clears the DirectComposition render blocker (2026-08-12)

Route 1 from §13x worked. On Ubuntu 26.04 (`resolute`), the distro repositories
had no `wine-staging` candidate, so WineHQ's official Resolute repository was
added and `winehq-staging` 11.15 installed. This was not literally a
"one-package, no-download" operation: it downloaded about 304 MB and added
about 1.8 GB. Ubuntu 26.04's APT also rejects WineHQ's ASCII `.key` filetype;
the verified WineHQ key (fingerprint
`D43F 6401 4536 9C51 D786 DDEA 76F1 A20F F987 672F`) had to be dearmored to
`/etc/apt/keyrings/winehq-archive.gpg`, with the source's `Signed-By` updated to
that binary keyring.

The staging `dcomp.dll` is materially different from Ubuntu Wine 10.0's stub.
Its exports are real functions:

```
DCompositionCreateDevice   RVA 0x1740 -> create_device(version=1)
DCompositionCreateDevice2  RVA 0x1870 -> create_device(version=2)
DCompositionCreateDevice3  RVA 0x19c0 -> create_device(version=3)
```

Most importantly, UC 5.0.x remained alive for the complete 25-second staging
render test and produced no DirectComposition failure; the test ended only
because its deliberate timeout sent `TERM`. Under Ubuntu Wine 10.0 the same
application exited immediately at `DCompositionCreateDevice`. The graphics
blocker is therefore cleared.

WineHQ's Resolute package installs the executable at
`/opt/wine-staging/bin/wine` without leaving a `wine` command on this machine's
PATH. `tests/uc_wine_capture.sh` now resolves that executable explicitly and
defaults `WINEPREFIX` to the already-prepared `~/.wine-io24` prefix. The prefix
upgraded successfully from Wine 10.0 to staging 11.15.

**Capture still pending:** the io24 was not connected during this verification,
so no UC wire capture was attempted. The command recorded at the time was:

```
tests/uc_wine_capture.sh uc
```

This is no longer the next command. §13aa proves the required WinUSB discovery,
function-binding, and API layers are absent. Do not run the capture until those
layers and interface ownership have their own offline contracts and a fresh
live authorization. Rendering alone never proved the USB path.

---

### §13z — UC 5 needs UIAnimation v2; a read-only headless probe is ready (2026-08-13)

The 25-second process-survival test in §13y was not a successful render test.
With the io24 connected, UC again displayed **"This application requires
Windows 10 or later."** A focused trace showed `DCompositionCreateDevice`
succeeding first, followed by failed COM activation of:

```
{D25D8842-8884-4A4A-B321-091314379BDD}  UIAnimationManager2
{812F944A-C5C8-4CD9-B0A6-B3DA802F228D}  UIAnimationTransitionLibrary2
```

Neither class is registered in `~/.wine-io24`. That is not just a missed
`regsvr32`: both installed `uianimation.dll` architectures contain and register
only the four v1 classes, and Wine's current upstream implementation likewise
implements `IUIAnimationManager`, not `IUIAnimationManager2`. Aliasing the v2
CLSIDs to Wine's v1 DLL would only turn `REGDB_E_CLASSNOTREG` into an interface
or vtable failure. The v2 objects are real Windows 7 Platform Update / Windows 8
APIs, so UC's generic Windows-version dialog is consistent with this dependency.

PreSonus's official Revelator io24 downloads page currently exposes a Windows
UC 4.7.2.108537 installer. That is the remaining GUI compatibility candidate,
but it must be extracted and checked statically before it is ever run; version
number alone does not prove that it predates the v2 renderer.

The cleaner route avoids the GUI. Static inspection of the already-installed
`PreSonusHardwareAccessService.exe` confirms that it:

- is a console/SCM service with `-install` and `-uninstall` modes;
- imports `ucnet.dll`, `ccltext.dll`, `cclsystem.dll`, Win32 service/device APIs,
  and the Visual C++ runtime;
- imports neither DirectComposition nor UIAnimation.

`tests/ucnet_service_probe.py` is now the safe client for that route. It only
sends the mandatory `UM`, `JM Subscribe`, and `KA` messages, then records the
service's `Synchronize` JSON; there is deliberately no `PV` or `PS` write path.
It passed discovery and Subscribe/Synchronize tests against
`ucnet_shim.py --dry-run` (60 numeric values and 4 strings).

The first real-service attempt was made only in a disposable `/tmp` prefix with
USB, GPU, audio, udev, the session bus, and the real home/prefix hidden. Wine was
limited to half of one CPU, 1--1.5 GiB RAM, zero swap, idle I/O priority, and a
hard runtime. The installed service identifies itself as 5.0.0.111903. Its
`-install` mode exited 53 before SCM registration, so the service never started
and the real probe never sent a packet. A loader trace from the interrupted
prefix resolved 53 to `c0000135`: headless `wineboot` had timed out before
populating core built-ins. A fresh 50-second pass populated more of them but
still waited indefinitely without a display and left `-install` at 53. Both
throwaway prefixes were removed, with no Wine process or port 47809 listener
left behind.

The next service-side prerequisite is therefore a fully initialized throwaway
prefix under an isolated software-only virtual display. Do not repair this by
copying DLLs into the real prefix, and do not expose the io24 until the service
can register, start, and pass the read-only probe with devices still hidden.

The capture harness was also made fail-closed: it no longer kills a running
`io24gtk.py` implicitly, refuses to proceed while an io24 app/daemon/shim is
active, and will not overwrite a non-empty `uc.txt`.

---

### §13aa — CORRECTION: Wine's USB bus is not a WinUSB function stack (2026-08-22)

CP27–CP29 fixed and reproduced the composite-function scope defect: the io24
now projects `MI_00 {0,1,2}`, `MI_03 {3,4}`, `MI_05 {5}`, and `MI_06 {6}` in
the offline WineUSB candidate. That exposed the next boundary cleanly and
invalidated the feasibility premise in §13v/§13w.

The exact `dspusbdevice.dll` import table contains fourteen `WinUsb_*` calls.
Within its one VID/PID `194f:0422` model, records at file offsets `0x27e300`
and `0x27e318` associate interfaces 5 and 6 with `WINUSB`. The same bounded
model window contains a `MSFT100` marker with vendor code `0xa5` and the
`DeviceInterfaceGUID` property name. These are direct facts about the installed
host module's model. They strongly identify the Windows discovery contract it
expects, but they are not a live read of the device's descriptor response.

Microsoft's WinUSB discovery contract explains the expected chain:

1. the device reports `WINUSB` through Microsoft OS feature descriptors;
2. Windows creates the additional compatible ID `USB\MS_COMP_WINUSB`;
3. the in-box `winusb.inf` matches that ID and loads the WinUSB function driver;
4. an extended-properties descriptor can register `DeviceInterfaceGUIDs`; and
5. the application opens that device interface and calls `WinUsb_*`.

The relevant Microsoft documentation is:

- <https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/automatic-installation-of-winusb>
- <https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/microsoft-os-1-0-descriptors-specification>
- <https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-xusbi/601a3107-6583-4e80-867e-118829cb573e>

CP24 retained the opposite Wine outcome. Every MI05 registry instance has only
the three `USB\Class_ff...` IDs; every MI06 instance has only the three
`USB\Class_fe...` IDs. None contains `USB\MS_COMP_WINUSB`, a driver, or a
service. The setup trace examines Wine's `wineusb.inf`, whose only match is
`root\wineusb`, then reports no compatible driver for MI05 at line 7878 and
MI06 at line 8168.

The pinned Wine 11.15 source explains both missing layers:

- `dlls/wineusb.sys/wineusb.c:get_compatible_ids()` creates only USB class IDs;
- `dlls/wineusb.sys/wineusb.inf` installs only the root USB bus enumerator;
- `dlls/winusb/winusb.spec` declares 21 of 22 exports as stubs; and
- the remaining `WinUsb_Free()` implementation explicitly logs that it is a
  stub and returns success without freeing an interface handle.

All thirteen imported functions other than `WinUsb_Free` are declared stubs,
so all fourteen functions imported by `dspusbdevice.dll` are semantically
unimplemented. This is not a Wine 11.15-only regression. Wine master at release
11.16 commit `8da89f8493b21ebfbe344a54dbef0cde23c7ea59` has the same files and behavior.
The `WinUsb_Free` function was itself added in commit
`1df5309a822ac33daabda5654a3eaf93b88dffea`, titled “winusb: Add WinUsb_Free
stub.”

The new hardware-free audit is:

```
python3 tests/io24_winusb_readiness.py \
  --require-known-boundary \
  --dspusb-dll <dspusbdevice.dll> \
  --wine-source <CP29-source> \
  --system-reg <CP24-postflight-system.reg> \
  --service-log <CP24-service-install.log>
```

It confirms one exact boundary and a deliberate structural-readiness RED:

```
vendor imports: 14/14
io24 MI05/MI06 WINUSB model: present
compatible-ID translation: missing
generic WinUSB INF match: missing
required WinUSB APIs implemented: 0/14
stageable from this audit alone: NO
```

`tests/io24_winusb_readiness_contract.py` independently exercises this known
RED and a synthetic structural GREEN. The audit tests necessary source
surfaces, not runtime semantics, and therefore never marks an implementation
stageable. Behavioral contracts remain mandatory for all three layers. Adding
only `USB\MS_COMP_WINUSB` would be an inert partial fix and must not be called
MI05/MI06 support.

The coherent implementation order is now:

1. emulate Microsoft OS 1.0 descriptor discovery against fake libusb, including
   index `0xee`, vendor-code capture, extended compatible IDs, and extended
   properties;
2. add a generic WinUSB function binding and device-interface registration,
   with separate PDO identity and lifecycle tests;
3. implement the fourteen imported user-mode operations over that function
   driver, including synchronous and overlapped transfer, cancellation, pipe
   policy, descriptor, alternate-setting, and associated-interface behavior;
4. reproduce the whole stack in independent clean builds before any selector
   or staging work; and
5. only then choose interface-4 ownership and design a newly authorized live
   diagnostic.

This changes the relation to presets and effects, but not their meaning. The
installed component model and prior device protocol work remain valid. Preset
and Voice FX state are above this transport boundary: Universal Control cannot
enumerate its expected MI05/MI06 WinUSB interfaces today, so it cannot yet
provide new synchronization or effect-wire evidence. MI05 remains the likely
application bulk path and MI06 the direct DFU/firmware path, but neither role
has been live-proven. Firmware is therefore further narrowed, not implicated.

Stop at CP30. Do not stage CP29, run `uc_wine_capture.sh uc`, retry CP24, or
touch a device/prefix. A driver load, USB read, claim/detach, alternate or
configuration change, OUT transfer, Universal Control, Voice FX, firmware
transition/write, or protected-prefix access still requires a fresh explicit
authorization and full preflight.

---

### §13ab — CP31 completes bounded OS 1.0 discovery offline (2026-08-23)

The first of §13aa's three WinUSB layers is now implemented, behavior-tested,
and reproduced in two independent clean builds. WineUSB requests the fixed
18-byte Microsoft OS string at index `0xee`, validates `MSFT100`, uses the
returned vendor code for the extended compatible-ID descriptor at `wIndex=4`,
and retrieves extended properties at `wIndex=5` only for an active function
child that will actually be emitted.

The parser is generic and bounded. It caps each feature descriptor at 4096
bytes; validates version, index, count/length arithmetic, reserved fields,
duplicate interfaces, ID characters and padding; atomically discards all
compatible-ID metadata if any function section is malformed; and validates a
UTF-16LE `DeviceInterfaceGUID` REG_SZ value. An unavailable or malformed
properties descriptor does not destroy a valid compatible ID. The builtin
then emits the descriptor-derived PnP identity before class fallbacks:

```
USB\MS_COMP_WINUSB
USB\MS_COMP_<ID>&MS_SUBCOMP_<SUBID>
USB\MS_COMP_<ID>
```

The unchanged CP29 binary produced zero OS-descriptor requests and failed the
valid fake-device fixture with seven intended assertions. Two more RED/GREEN
cases prevented an extended-properties request for an unknown interface and a
partial-ID leak from a malformed later function section. The final 11-scenario
matrix and a direct PnP-ID formatter contract pass against both clean builds,
along with all function-scope, immediate-cleanup, multi-interface, pipe,
captured-RANGE, USBDI, class-IN, topology, and CP30 self-contract regressions.

The exact patch is:

```
d63c0b6727c6fa313153738aa0a33bfd5f0cc799e90012d92257eebb0a0ed6d4  re/wine-11.15-wineusb-ms-os-10-v1.patch
```

Clean A and B are byte-identical:

```
b16b8d2ab2104dbf98b6a171546258db3dc22c11a4688170710c022e1068f30e  x86_64-windows/wineusb.sys
13669869e34219947862da6dc46e94fb3a3aaf03902b84cca86cbbee663bff94  wineusb.sys.so
228cf6b8006714887d6adeb0bc7dff040fba201f84cae1265a292cc115b517c9  wineusb.so
```

This does not make MI05 or MI06 usable. The parsed GUID is only carried with
the child; there is still no generic WinUSB function binding or registered
device interface, and the vendor module's required `WinUsb_*` operations
remain 0/14. The one new actionable libusb dependency is the bounded
descriptor-IN `libusb_control_transfer`; there is still no claim, detach,
alternate-setting, configuration, or OUT action. The candidate is therefore
not staged or selected.

The synthetic fixture's `0xa5`, WINUSB IDs, and GUID are informed by the
installed vendor-module model. They are not a live read and do not establish
that the physical io24 returns the same descriptors. Preset/effect/component
model evidence is unchanged, and CP31 provides no new synchronization or wire
evidence. Firmware is further narrowed rather than implicated because the
function-binding and user-API host layers still precede it.

The full checkpoint is
`re/wineusb-ms-os-10-v1/checkpoints/CP31_MS_OS_10_DISCOVERY.md`. Next safe work
is offline: specify generic WinUSB function binding and device-interface
lifecycle, while leaving PaeDSPUSB MI00/MI03 and the separate interface-4
physical-ownership decision untouched. Do not stage CP31, run the legacy UC
capture, retry CP24, or touch a device/prefix without new explicit
authorization and full preflight.

---

### §13ac — CP32 completes generic WinUSB function binding offline (2026-08-23)

The second of §13aa's three WinUSB layers is now implemented, behavior-tested,
and reproduced in two independent clean builds. CP31's function PDO exposes
its validated Microsoft OS 1.0 compatible ID and `DeviceInterfaceGUID` through
a private, versioned, reference-balanced query interface. A new generic
`winusb.sys` binds only `USB\MS_COMP_WINUSB`, registers a per-PDO device
interface, enables it after successful start, preserves it across normal stop,
and performs bounded disable plus unconditional local cleanup during failed
restart, surprise removal, and removal.

The function driver is deliberately discoverability-only. Create, read, write,
device-control, and internal-device-control requests fail unsupported. It has
no Unix library, dynamic dependency, libusb symbol, URB submission, WinUSB
IOCTL, claim/detach, configuration, alternate-setting, transfer, or firmware
path. The separate user-mode `dlls/winusb` tree is unchanged, so the exact
fourteen operations imported by `dspusbdevice.dll` remain 0/14 implemented.

The current contracts produce a genuine missing-feature RED against sealed
CP31 and pass against both clean CP32 sides. Both sides also pass the complete
CP31 and predecessor matrix. Patch replay is fuzz-zero and patch-check matches
development across 12,453 regular files. The exact patch is:

```
7df4011ea20fa36bf80f4bc0ba409c9dca812b7d76b00f19f1709774eebf75bb  re/wine-11.15-winusb-function-binding-v1.patch
```

Clean A and B are byte-identical at the new and retained deliverables:

```
b16b8d2ab2104dbf98b6a171546258db3dc22c11a4688170710c022e1068f30e  x86_64-windows/wineusb.sys
28617460085c0a2f8ae4782f3147bcad8a475a5b01daebba72b64380c118808e  wineusb.sys.so
1edd11b0336a241c1ac8c667fb1d21db3bfac08b0ebdc8f672aa2931785490d9  wineusb.so
bacee6355cda7eb6bb19a7c8f73d2b8babfc4a22d19b47c31cffc1985f7427eb  x86_64-windows/winusb.sys
dd1f3f199999546ff64ea7c1daa1306fba0a5958f7c1102df3b49ebfca20be46  winusb.sys.so
```

This still does not make MI05 or MI06 usable and is not staged. The third layer
is the exact fourteen imported `WinUsb_*` operations, including the user/kernel
handle and IOCTL boundary, synchronous and overlapped completion,
cancellation, pipe policy, descriptors, associated interfaces, and alternate
settings. That remains the next coherent offline slice.

Interface 4 is unchanged: the correct MI03 function view is retained, while
Linux physical ownership remains a separate architecture decision. Preset,
effect, synchronization, and prior wire evidence are unchanged. Firmware is
further narrowed rather than implicated because the user API layer still
precedes any meaningful MI05/MI06 application path.

The full checkpoint is
`re/winusb-function-binding-v1/checkpoints/CP32_WINUSB_FUNCTION_BINDING.md`.
Do not stage CP32, run the legacy capture, retry CP24, access a prefix, or touch
the device without new explicit authorization and full preflight. CP24 remains
consumed; reconnection is state evidence, not authorization. Pause at 10%
battery or lower.
