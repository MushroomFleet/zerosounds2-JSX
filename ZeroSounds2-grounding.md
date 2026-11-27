# Zero Sounds 2 JSX (PlayStation-style SFX synthesis): grounding

This is the design document for Zero Sounds 2. It explains why PlayStation sound effects sound the way they do, how to reproduce that character with deterministic procedural synthesis, and how the reference implementation in Python is built. `ZeroSounds2.py`, `ZeroSounds2.jsx` and the demo all derive from this document.

Status of the numbers in here:

- **Hardware facts** (ADPCM block format and filter coefficients, the ADSR step formula, 4.12 pitch counter, 44.1 kHz output, 24 voices, half-rate reverb) describe the real SPU and follow the public psx-spx hardware reference.
- **Parametric defaults** (reverb delay lengths and gains, the Gaussian kernel fit, every hashed parameter range) are choices made for this project. They are marked where they appear. They are tuned by ear-free measurement only (levels, lengths, spectral centroid) and are open to retuning.

## 1. From SNES to PlayStation: what changes

ZeroSounds 1 modelled the SNES: oscillators, a bit-crusher, a low-pass filter and an echo, built from Web Audio nodes. That suits a console whose effects were tiny looped waveforms played at 32 kHz through heavy filtering.

The PlayStation's Sound Processing Unit (SPU) is a different kind of machine, and its effects sound different for five concrete reasons:

| Property | SNES (S-DSP) | PlayStation (SPU) | Audible result |
|---|---|---|---|
| Output | 32 kHz, 8 voices | 44.1 kHz stereo, 24 voices | Brighter top end; effects are layered from several voices |
| Sample memory | 64 KB shared | 512 KB | Real one-shot recordings and FM-synth samples, not just single cycles |
| Compression | BRR, 4 filters | SPU-ADPCM, 5 filters | A fine quantisation grain rather than obvious crunch |
| Envelope | ADSR or GAIN | ADSR with linear and exponential modes per phase | Fast percussive attacks with long exponential tails |
| Reverb | 8-tap FIR echo | Dedicated reverb unit at 22.05 kHz | The glossy "hall" on menu and pickup sounds |

So the PlayStation aesthetic is not "the SNES with more bits". It is short sampled or FM-derived sources, played back through Gaussian interpolation, shaped by exponential envelopes, layered two or three voices deep, panned in stereo, and washed in a dark half-rate reverb. Zero Sounds 2 emulates that signal chain stage by stage, and replaces the one thing a real game needed (a ROM of recorded samples) with procedurally generated source samples.

## 2. The signal chain

```
worldSeed -> categorySeed -> soundSeed -> layerSeed -> slot k        (hash hierarchy)
                                              |
                        +---------------------+
                        v
                 source sample            closed-form function of sample index n
                        |
                 SPU-ADPCM codec          encode, then decode: adds authentic grain
                        |
                 pitch counter            4.12 fixed point, sweep, vibrato, PMON
                        |
                 Gaussian interpolation   4 taps, 512-entry kernel
                        |
                 ADSR                     SPU register-level state machine
                        |
                 volume L/R               14-bit, per voice
                        |
        sum of 2-3 voices ----> dry bus ---------------------+
                        |                                     v
                        +----> reverb send -> SPU reverb -> + -> clamp -> int16 stereo 44.1 kHz
```

The whole sound is rendered offline into a buffer. Nothing depends on wall-clock time or on the audio driver, which is what makes bit-exact reproduction possible.

## 3. The ZeroBytes contract

Zero Sounds 2 keeps the five laws of ZeroSounds 1 and tightens the last one.

| Law | Meaning here |
|---|---|
| O(1) access | Any parameter comes straight from `(soundName, worldSeed)`. Any source sample value comes straight from `(n, sourceSeed)`. |
| Parallelism | Each parameter hashes from its own slot `k`. No parameter reads another's hash. |
| Coherence | Sounds in a category share a category seed and category bounds, so "movement" stays movement-like under any seed. |
| Hierarchy | `worldSeed -> categorySeed -> soundSeed -> layerSeed -> slot[k] -> sample[n]` |
| Determinism | Same inputs give the same **PCM bytes**, on every machine and in both languages. |

ZeroSounds 1 guaranteed identical parameters. Zero Sounds 2 guarantees identical audio, which needs three extra rules:

1. **No libm in the signal path.** `sin`, `exp` and `pow` are not required to round identically across platforms or JS engines. The engine uses its own `dsin` and `dexp2`, built only from `+ - * /` and `floor` in a fixed order (section 5). IEEE-754 requires those operations to be correctly rounded, so the results match everywhere.
2. **Integers after the source.** Once a source sample is rounded to 16-bit, the ADPCM codec, pitch counter, interpolation, envelope and volume are all integer arithmetic, as they are on the hardware. The reverb uses doubles but only multiplies and adds.
3. **One rounding rule.** `floor(x + 0.5)` everywhere. Never the language's default `round`.

The real SPU also has an LFSR noise generator. An LFSR is sequential state, which breaks O(1) access, so noise here is a hashed white-noise *sample* played through the normal voice path instead.

### Hash core

Unchanged from ZeroSounds 1 so seeds and habits carry over:

```
zbHash(key, seed) -> u32      Murmur3-style finaliser; numeric and string keys
zbFloat(h)  = (h & 0xFFFFFF) / 2^24
zbRange(h, lo, hi) = lo + zbFloat(h) * (hi - lo)
zbInt(h, lo, hi)   = lo + h mod (hi - lo + 1)
zbPick(h, list)    = list[h mod len]
```

In Python every multiply is masked to 32 bits; in JavaScript `Math.imul` does the same job.

## 4. Models

### 4.1 Taxonomy (the only stored data)

Eight categories. Each stores bounds, not sounds:

| Category | Base pitch (Hz) | Hold (s) | Reverb depth | Rooms |
|---|---|---|---|---|
| ui | 500 to 1400 | 0.04 to 0.16 | 0.15 to 0.45 | room, studio, hall |
| pickup | 700 to 2000 | 0.08 to 0.30 | 0.25 to 0.60 | studio, hall |
| movement | 90 to 420 | 0.05 to 0.20 | 0.05 to 0.25 | room |
| combat | 110 to 900 | 0.06 to 0.35 | 0.10 to 0.40 | room, studio |
| impact | 45 to 200 | 0.15 to 0.60 | 0.20 to 0.55 | studio, hall |
| power | 260 to 1100 | 0.30 to 0.90 | 0.30 to 0.65 | hall, space |
| damage | 120 to 520 | 0.10 to 0.60 | 0.10 to 0.40 | room, studio |
| environment | 90 to 700 | 0.08 to 0.50 | 0.25 to 0.60 | hall, space, studio |

Forty named sounds. Each is a four-field anchor: `category`, `arch` (archetype), `styleIndex`, `dir` (+1 rising, -1 falling, 0 let the hash decide). All ranges above are parametric defaults.

### 4.2 Archetypes: how voices are layered

An archetype is a recipe for which voices a sound uses. This is the main structural change from ZeroSounds 1, where a sound was one oscillator.

| Archetype | Voices | Models |
|---|---|---|
| bell | FM body + detuned FM shimmer, opposite pans | Menu cursors, item chimes, metal and glass |
| synth | Wavetable + slightly detuned copy | Blips, cancels, short tones |
| arp | Body + octave voice, re-keyed for 2 to 5 notes | Confirm jingles, pickups, level-ups |
| noise | Two noise voices at different rates, opposite pans, pitch-swept | Whooshes, slashes, splashes, doors |
| hit | Noise crack + pitch-drop sine body | Punches, gunshots, footsteps, explosions |
| sweep | Muted modulator voice pitch-modulating a swept carrier, plus a chorus voice | Lasers, jumps, magic, warps |

`hit` splits on hold time: under 0.15 s it is a tight click; at or above 0.15 s both voices play at 0.45x rate with a slower envelope, which gives the long dark body of an explosion.

### 4.3 The patch (output of derivation)

```
Patch {
  name, category, archetype, direction, worldSeed, soundSeed
  baseFreq   Hz
  hold       seconds until key-off
  reverb     { preset: room|studio|hall|space, depth: 0..1 }
  layers[]   Layer
}
Layer {
  role       body | shimmer | chorus | octave | width | crack | modulator | carrier
  source     Source (below)
  freq       Hz, or null for unpitched sources
  step0      pitch counter step at ratio 1 (1.0 = 44.1 kHz playback)
  sweepOct, sweepTime            pitch glide in octaves over seconds
  vibDepth (octaves), vibRate (Hz)
  pmon       pitch-modulated by the previous layer
  mute       not mixed; exists only as a modulation source
  pmDepth    scale applied to a muted layer's output before it modulates
  adsr       { ar, am, dr, sl, sm, sd, sr, rm, rr }   SPU register values
  hold       seconds
  volL, volR 0..0x4000
  reverb     send on/off
  notes[]    { t: seconds, ratio: pitch ratio }
}
Source =
  { kind: fm,    cycle, ratio, index0, indexLoop, attackLoops, seed }
| { kind: wave,  cycle, shape, duty, harmonics, seed }
| { kind: noise, blocks, color, seed }
| { kind: thump, blocks, f1, f2, seed }
```

### 4.4 Source samples: the virtual sample ROM

Each source is a short 16-bit sample whose every value is a closed-form function of its index `n`. Lengths are multiples of 28 because that is the ADPCM block size, and loop points sit on block boundaries as they must on the hardware.

**fm**: a two-operator FM tone, standing in for the DX7 and ROMpler chimes that PlayStation menus were sampled from.

```
P      = cycle (28, 56 or 112 samples per cycle)
nAtt   = attackLoops * 2P                attack portion, then a 2P loop
t      = 1 - n/nAtt  (0 once n >= nAtt)
I(n)   = indexLoop + (index0 - indexLoop) * t^2
ph     = 2*pi*n / P
s[n]   = 26000 * dsin(ph + I(n) * dsin(ratio * ph))
```

The modulation index falls from `index0` to `indexLoop` across the attack, so the tone starts bright and settles, like a struck bell. Ratios are multiples of 0.5, so two cycles always loop seamlessly; the half-integer ratios (3.5, 5.5) give inharmonic, metallic partials.

**wave**: additive wavetable, 2 cycles long. Shapes: pulse (hashed duty), saw, triangle, organ (hashed drawbars), glass (sparse partials). Each harmonic gets a hashed +/-15% jitter. Harmonics stop below Nyquist for the chosen cycle length.

**noise**: 292 blocks (8176 samples) of hashed white noise, `w[n] = 2*zbFloat(zbHash(n, seed)) - 1`, through a one-pole filter whose coefficient `color` (0.12 dark to 1.0 white) is hashed. Looped whole.

**thump**: a sine whose pitch falls from `f1` to `f2` with a cubic curve. The phase is integrated analytically so it is still O(1) in `n`:

```
t  = 1 - n/N
ph = 2*pi * ( c2*n + (c1 - c2) * N/4 * (1 - t^4) )     c = f / 22050
s[n] = 28000 * dsin(ph) * t^2
```

One-shot, no loop.

**Samples per cycle** is the lo-fi control for pitched sources. At 28 samples per cycle the sample holds at most 13 harmonics and leans on the interpolator, which sounds duller and grainier; 112 is clean. The derivation only offers cycle lengths that keep the voice under the pitch ceiling (section 4.6).

### 4.5 SPU-ADPCM

Samples in SPU RAM are stored as 16-byte blocks: a header byte (shift in the low nibble, filter in the high), a flags byte, and 28 signed 4-bit nibbles. Decoding:

```
pred   = (old * K0[f] + older * K1[f] + 32) >> 6
sample = clamp16( pred + (nibble << (12 - shift)) )

f   K0    K1
0     0     0
1    60     0
2   115   -52
3    98   -55
4   122   -60
```

The encoder is not specified by the hardware. This one, per block and per filter:

1. Measures the largest prediction residual using the *original* signal as history (open loop).
2. Picks the smallest step `2^q` with `7 * 2^q >= residual`, and also tries `q + 1`.
3. Encodes closed-loop (history is the decoder's own output) and keeps the filter and step with the least squared error.

That is ten trial encodes per block rather than 65, with near-identical quality. The engine then decodes the result and plays the *decoded* samples. The reconstruction error is the "PlayStation grain". Measured signal-to-noise at the default seed: about 25 to 47 dB on tonal sources, about 19 to 22 dB on noise (noise is unpredictable, so the filters cannot help).

The block count times 16 is the sound's real SPU RAM footprint, which the demo reports. A typical sound here uses 0.1 to 9 KB of the console's 512 KB.

### 4.6 Pitch counter and Gaussian interpolation

Each voice has a pitch register in 4.12 fixed point: `0x1000` plays the sample at 44.1 kHz, and the maximum `0x3FFF` is just under 4x. Every output sample:

```
step    = step0 * noteRatio * 2^(sweepOct * u + vibDepth * dsin(2*pi*vibRate*i/44100))
u       = e * (2 - e),  e = min(1, i / sweepSamples)        ease-out glide
if PMON: step *= (0x8000 + previousVoiceOutput) / 0x8000
stepInt = clamp(round(step * 4096), 1, 0x3FFF)
pos    += stepInt
```

For a pitched source, `step0 = freq * cycle / 44100`. The derivation caps `freq` so the highest note, at the top of its sweep, stays at or below a step of 3.5.

The integer part of `pos` indexes the sample; bits 4 to 11 select one of 256 interpolation phases. The SPU interpolates over four samples with a 512-entry Gaussian table:

```
out = ( g[255-i]*s[k-1] + g[511-i]*s[k] + g[256+i]*s[k+1] + g[i]*s[k+2] ) >> 15
```

This is a smoothing kernel, not a sharp interpolator: it rolls off treble, more so the further a sample is pitched down. That roll-off is a large part of the PlayStation's soft, slightly muffled tone, and it is why pitch-sweeping a noise sample sounds like a filter sweep.

**Parametric default:** the table here is an analytic fit, `g(x) = 0.7008 * exp(-x^2 / (2 * 0.569^2))` sampled over two samples of half-width, chosen so the peak matches the hardware's peak tap and the four taps sum to about one. It is not the console's ROM table. Swapping in the real table is a one-line change and would alter the checksums but not the structure.

**Pitch modulation (PMON)** is a real SPU feature: a voice's pitch is scaled by the previous voice's output. The `sweep` archetype uses it the way games did, with a silent modulator voice. A slow modulator (6 to 40 Hz) gives wobble; an audio-rate one gives FM growl.

### 4.7 ADSR

The envelope is the SPU's own state machine, driven by register values rather than times. Level runs 0 to `0x7FFF`.

| Phase | Mode | Direction | Shift | Step |
|---|---|---|---|---|
| Attack | `am` (lin/exp) | up | `ar >> 2` | `7 - (ar & 3)` |
| Decay | exp | down | `dr` | -8 |
| Sustain | `sm` | `sd` | `sr >> 2` | `7 - (sr & 3)` up, `-8 + (sr & 3)` down |
| Release | `rm` | down | `rr` | -8 |

Per sample:

```
cycles = 1 << max(0, shift - 11)
step   = step << max(0, 11 - shift)
if exponential and rising and level > 0x6000:  cycles *= 4
if exponential and falling:                    step = step * level >> 15
every `cycles` samples:  level = clamp(level + step, 0, 0x7FFF)
attack -> decay at 0x7FFF;  decay -> sustain at (sl + 1) * 0x800;  key-off -> release
```

Useful conversions at 44.1 kHz:

- Linear attack: `ar = 0` is 3 samples; `ar = 28` about 5 ms; `ar = 36` about 26 ms; `ar = 44` about 106 ms.
- Exponential fall: time constant is `2^(shift + 1)` samples. Shift 9 is 23 ms, 11 is 93 ms, 12 is 186 ms, 13 is 372 ms, 14 is 743 ms.

The exponential fall has a hardware quirk that this engine keeps: below level `0x1000` the scaled step floors to -1, so the last 18 dB of a tail falls in a straight line. It is why SPU releases seem to hang, then stop.

Four envelope families set the register ranges (parametric defaults):

| Family | AR | DR | SL | SR | RR | Used by |
|---|---|---|---|---|---|---|
| perc | 0 to 12 | 11 to 14 | 0 to 6 | 44 to 58 | 10 to 13 | bells, arps, thump bodies |
| sus | 8 to 36 | 9 to 12 | 8 to 15 | 52 to 66 | 7 to 11 | synths, sweeps |
| noise | 0 to 30 | 9 to 13 | 2 to 12 | 42 to 56 | 9 to 13 | noise voices, big impacts |
| hit | 0 to 4 | 8 to 11 | 0 to 3 | 40 to 50 | 8 to 11 | short cracks |

A voice renders until its release reaches zero, capped at 3 s after key-off.

### 4.8 Reverb

The SPU reverb runs at 22.05 kHz, half the output rate, which alone gives it a dark tone. Its structure per channel is: input plus a delayed "wall reflection" through a one-pole filter, written to a same-side and a cross-side delay; four comb taps read back and summed; two all-pass filters in series.

```
same[w] = lp( in + wall * same[w - dSame] )
diff[w] = lp( in + wall * otherChannelDiff[w - dDiff] )      cross-feed gives stereo spread
out     = 0.40*same[t1] + 0.35*same[t2] + 0.35*diff[t3] + 0.30*diff[t4]
out     = allpass(allpass(out, g=0.6), g=0.5)
```

The engine averages input pairs down to half rate, processes, and linearly interpolates back up.

**Parametric defaults.** The topology follows the hardware; the numbers do not come from Sony's preset register tables. Base delays at 22.05 kHz are 3529/3907 (same L/R), 2903/3319 (diff L/R), 613 and 359 (all-pass), scaled per preset:

| Preset | Delay scale | Wall | Damping | Tail rendered |
|---|---|---|---|---|
| room | 0.30 | 0.55 | 0.65 | 0.5 s |
| studio | 0.50 | 0.62 | 0.60 | 0.8 s |
| hall | 1.00 | 0.72 | 0.50 | 1.6 s |
| space | 1.70 | 0.80 | 0.40 | 2.4 s |

The input is scaled by `1 - wall` so the loop's gain stays near unity. Stability only needs `wall < 1`.

### 4.9 Mix and output

Voices are scaled by their left and right volumes (`>> 14`, with `0x4000` as unity), summed to a dry bus and, if their send is on, to the reverb bus. Output is `round(dry + wet * depth)`, clamped to 16-bit as the SPU does. Trailing samples quieter than +/-24 are trimmed, leaving 256 samples of margin. Archetype volumes are set so the sum stays inside 16-bit; the verify pass counts any clamped samples as a failure.

## 5. Deterministic math

```
dsin(x):   r = x - floor(x / 2pi + 0.5) * 2pi          to [-pi, pi]
           fold r into [-pi/2, pi/2]
           r * (1 + r^2 * P(r^2))                       Taylor to r^15, Horner
dexp2(x):  n = floor(x);  y = (x - n) * ln2
           e = 1 + y * (1 + y * Q(y))                   Taylor to y^13, Horner
           e * 2^n                                      exact scaling
```

Accuracy is far beyond what 16-bit audio needs (error under 1e-11). What matters is that both are fixed sequences of correctly rounded operations. In NumPy each `p * r2 + c` is two separate array operations, so no fused multiply-add can change the rounding; in JavaScript there is no FMA at all.

One trap when vectorising: the NumPy expression must apply operations in exactly the order the scalar JavaScript does. `TWO_PI * n / P` and `TWO_PI * (n / P)` differ in the last bit. Section 8 describes the test that catches this.

## 6. Python implementation blueprint

`ZeroSounds2.py` is one file with NumPy as its only dependency.

| Section | Functions | Notes |
|---|---|---|
| Hash core | `zb_hash`, `zb_hash_vec`, `zb_float`, `zb_range`, `zb_int`, `zb_pick` | `zb_hash_vec` hashes a whole index array in uint64 for the noise source |
| Math | `dsin`, `dexp2`, `rnd` | Work on scalars and arrays |
| Taxonomy | `ZB_CATEGORIES`, `ZB_SOUNDS`, `ENV`, `REVERBS` | The only stored data |
| Derivation | `zb_derive_params(name, seed)`, `zb_randomize(n, seed)` | Pure, O(1); return a patch dict |
| Sources | `gen_source(src)` | Returns `(samples, loop_start)` |
| Codec | `adpcm_codec(samples)` | Returns decoded samples and per-block `(shift, filter)` |
| Voice | `adsr_run`, `render_voice` | Envelope is a Python loop; pitch and interpolation are vectorised |
| Reverb | `spu_reverb` | Python loop over half-rate samples |
| Render | `zs_render(patch)`, `zs_checksum`, `zs_write_wav`, `zb_verify_all` | |
| CLI | `main` | `--list --sound --all --random --params --verify --checksums --seed --out` |

Non-obvious parts:

- **Vectorised pitch counter.** The integer steps are computed for the whole voice at once; positions are `cumsum(steps) - steps`. Pitch modulation still vectorises because the modulating voice is already fully rendered.
- **Loop wrap as index arithmetic.** A tap index `k >= L` maps to `loopStart + (k - loopStart) mod (L - loopStart)`; one-shots map to a silent slot appended to the sample.
- **Three inherently sequential loops** remain in plain Python: the ADPCM encoder (history-dependent), the ADSR (state machine) and the reverb (feedback). A full verify of 40 sounds, each rendered twice, takes about 10 s. The JavaScript engine renders all 40 in under a second.
- **Seed hierarchy in code.** `catSeed = hash(cat.index, world)`, `soundSeed = hash(styleIndex, catSeed)`, sound-level slots are `hash(100 + k, soundSeed)`, `layerSeed = hash(i + 1, soundSeed)`, layer slots are `hash(k, layerSeed)`: 10 to 14 for the source, 20 to 26 for ADSR, 30 for vibrato rate.

## 7. Tuning guide

| To change | Edit | Effect |
|---|---|---|
| Overall brightness of a category | `freqLow`/`freqHigh` | Shifts base pitch range |
| Snappiness | `durLow`/`durHigh`, the `rr` range of the family | Shorter holds and releases |
| How wet a category is | `revLow`/`revHigh`, `rooms` | Depth and which presets are eligible |
| Lo-fi grain on tones | `CYCLES` | Dropping 112 forces duller, grainier tones |
| Bell character | `FM_RATIOS`, `index0` range | Half-integer ratios are metallic; high index is brighter |
| Bell brightness decay | `attackLoops` range | Longer attack portion keeps the tone bright longer |
| Noise tone | `color` range, noise `step0` range | Low colour and low step are dark and gritty |
| Impact weight | the 0.15 s split and 0.45 factor in `hit` | Lower factor is deeper and longer |
| Sweep drama | `sweepOct` ranges per archetype | Octaves of glide |
| FM growl on sweeps | `pmDepth` range | Above about 0.4 gets harsh |
| Reverb size and darkness | `REVERBS` scale, wall, iir | Wall near 1 is long; low iir is dark |
| Headroom | per-archetype `vol` values | Raise until `--verify` reports clipping, then back off |

After any change, run `python ZeroSounds2.py --verify` on several seeds: it fails a sound that is silent, clips, or is not reproducible. Then run `python build-demo.py` to refresh the checksum oracle and the demo.

Adding a sound is one line in `ZB_SOUNDS` in both engines, with a `styleIndex` not yet used in that category.

## 8. Verification

The success criterion is byte equality, which is easy to test mechanically.

1. **Self-consistency.** `--verify` renders every sound twice and compares CRC-32s of the interleaved PCM. It also requires a peak above 500 and zero clamped samples.
2. **Cross-language oracle.** `--checksums` prints the CRC-32 of every sound. `build-demo.py` writes that table for seed `0x1337BEEF` into `ZeroSounds2.jsx` as `ZS_ORACLE`. The JavaScript `zbVerifyAll` renders the same sounds and compares. A single differing rounding anywhere in the chain changes a checksum.

Results at the time of writing: 40 named sounds plus 4 unnamed patches, at three seeds (132 renders), produced identical CRC-32s in CPython with NumPy and in Node.js. Five seeds passed `--verify` with no clipping.

What this does not verify: that the sounds are *good*, or that they match real hardware. No listening test and no comparison against an SPU emulator has been done. The fidelity claims are structural (the same codec, counter, envelope formula and reverb topology), with the two parametric substitutions noted above.

## 9. Integration notes

**React component.** `ZeroSounds2.jsx` holds the engine (between `// <ENGINE>` markers, pure functions over typed arrays, no DOM), a small Web Audio player (`ZS2Engine`), and the UI. The engine block is the natural TypeScript module boundary.

**Games.** Hold one `worldSeed`. On an event, `zsRender(zbDeriveParams(name, worldSeed))` and play the buffer; cache renders by `(seed, name)`. A render costs roughly 10 to 30 ms in JavaScript, so pre-render at load or on first use rather than per frame. The ZeroSounds 1 seed tricks still apply: hash the seed with a level id, a depth, a game-clock hour or an environment id to shift the whole palette.

**Asset pipelines.** `python ZeroSounds2.py --all --seed S --out dir` writes 40 WAV files that are byte-identical to what the browser plays for seed `S`, so a build can ship files or the seed interchangeably.

**ComfyUI or other node graphs.** The engine maps cleanly to three nodes: derive (name, seed to patch JSON), render (patch to AUDIO), and a patch-edit node between them. Not built here.

**Possible extensions:** export real `.vag` files from the encoder's block data; replace the Gaussian fit with the hardware table; add the SPU's volume-sweep registers; run renders in an AudioWorklet or Web Worker for large batches.
