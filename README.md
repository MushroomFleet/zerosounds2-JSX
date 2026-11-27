# ZeroSounds2-JSX

**Deterministic PlayStation-style sound effect synthesis for games and applications.**

Zero Sounds 2 is the sequel to [ZeroSounds-JSX](https://github.com/MushroomFleet/ZeroSounds-JSX). The original synthesised SNES-style effects; this one models the PlayStation's sound chip (the SPU) for a later, glossier sound: layered voices, sampled-FM chimes, soft Gaussian-interpolated playback, exponential envelopes and a dark hall reverb.

It keeps the ZeroBytes method, *position-is-seed, zero bytes store infinity*. Every sound is derived from a hash of `(soundName, worldSeed)`. There is no `Math.random()`, no stored presets and no sample files. One 32-bit integer defines a library of 40 named sounds and an unlimited space of unnamed ones.

New in this version: the guarantee covers the audio itself. The same seed produces the same 16-bit PCM, byte for byte, in the browser and in Python.

## How it works

Each sound is two or three hardware-style voices. For every voice the engine:

1. Hashes its way down `worldSeed -> category -> sound -> voice -> parameter`.
2. Computes a short source sample (an FM tone, a wavetable, a noise loop or a pitch-drop sine) directly from the sample index.
3. Encodes that sample to SPU-ADPCM (28 samples in 16 bytes) and decodes it again, which adds the console's grain.
4. Plays it through a 4.12 fixed-point pitch counter with 4-point Gaussian interpolation and an SPU ADSR envelope.

The voices are panned, mixed, and sent through a reverb that runs at 22.05 kHz like the original. The result is rendered offline to a 44.1 kHz stereo buffer and then played.

To make the bytes reproducible, the signal path avoids the platform's `sin`, `exp` and `pow`, which are allowed to round differently between machines. It uses its own versions built from plain arithmetic. The full design, with the formulas, is in [`ZeroSounds2-grounding.md`](ZeroSounds2-grounding.md).

**Accuracy note.** The ADPCM format, pitch counter, ADSR formula and reverb structure follow the real hardware. Two things are approximations chosen for this project: the Gaussian interpolation table is an analytic fit rather than the console's ROM table, and the reverb delay lengths and gains are not Sony's preset values. This is a style synthesiser, not an emulator.

## Contents

| File | What it is |
|---|---|
| [`ZeroSounds2-grounding.md`](ZeroSounds2-grounding.md) | Design document: why PlayStation effects sound as they do, every model and formula, tuning guide, verification method |
| [`ZeroSounds2.py`](ZeroSounds2.py) | Reference engine and command-line tool. Renders WAV files and acts as the oracle the JavaScript engine is checked against |
| [`ZeroSounds2.jsx`](ZeroSounds2.jsx) | React component, plus the same engine in JavaScript and a small Web Audio player |
| [`ZeroSounds2-demo.html`](ZeroSounds2-demo.html) | Standalone interactive demo with the component inlined. No build step |
| [`build-demo.py`](build-demo.py) | Refreshes the checksum table in the `.jsx` from the Python engine and re-inlines the `.jsx` into the demo |
| `README.md` | This file |

## Using the demo

Open `ZeroSounds2-demo.html` in a browser. It works straight from disk; it needs an internet connection the first time to load React, Babel and the fonts from their CDNs.

- **Press a sound** to render and play it. The inspector shows the stereo waveform, length, sample RAM used, reverb, a CRC-32 of the audio, and each voice's source, pitch and ADSR registers with its envelope drawn.
- **Change the world seed** by typing hex, dragging the slider or pressing **Next seed**. All 40 sounds are re-derived.
- **Download WAV** saves exactly what you heard.
- **Unnamed patches** steps through the sounds that have numbers instead of names.
- **Check all 40 sounds** renders everything twice and compares checksums. At seed `0x1337BEEF` it also compares against checksums produced by `ZeroSounds2.py`, so a pass means the browser and Python engines agree bit for bit.

## Using the React component

Copy `ZeroSounds2.jsx` into your project. It has no dependencies beyond React.

```jsx
import ZeroSounds2 from './components/ZeroSounds2';

export default function App() {
  return <ZeroSounds2 initialSeed={0xDEADBEEF} onPlay={(name) => console.log(name)} />;
}
```

| Prop | Type | Default | Meaning |
|---|---|---|---|
| `initialSeed` | number | `0x1337BEEF` | World seed on mount (unsigned 32-bit) |
| `initialVolume` | number | `0.7` | Master volume, 0 to 1 |
| `onPlay` | function | `null` | Called as `(name, patch, render)` after a sound starts |
| `className` | string | `''` | Extra class on the root element |

The component injects its own styles under the `zs2` class prefix. It uses the Chakra Petch and IBM Plex Mono fonts if the page provides them and falls back to system fonts otherwise.

## Using the engine in a game

The engine functions are exported from the same file and do not need React or the DOM.

```js
import { zbDeriveParams, zbRandomize, zsRender, zsEncodeWav, ZS2Engine } from './ZeroSounds2';

const player = new ZS2Engine();          // create after a user gesture
const cache = new Map();

function playSound(name, worldSeed) {
  const key = `${worldSeed}:${name}`;
  if (!cache.has(key)) cache.set(key, zsRender(zbDeriveParams(name, worldSeed)));
  player.play(cache.get(key));
}

playSound('confirm', game.worldSeed);
```

A render takes roughly 10 to 30 ms, so cache renders or pre-render at load rather than rendering per frame.

The seed tricks from ZeroSounds 1 still work, because the whole library follows the seed:

```js
// The palette shifts as the player descends
playSound('footstep', zbHash(player.depth, baseSeed));

// Every item has its own sound, identical on every client
player.play(zsRender(zbRandomize(item.id, worldSeed)));
```

### API

```js
zbHash(key, seed)                 // u32; numeric or string key (same as ZeroSounds 1)
zbFloat(h)  zbRange(h, lo, hi)  zbInt(h, lo, hi)

zbDeriveParams(name, worldSeed)   // named sound -> patch
zbRandomize(n, worldSeed)         // unnamed sound number n -> patch
zsRender(patch)                   // -> { left, right: Int16Array, sampleRate: 44100,
                                  //      clipped, spuRamBytes, layerEnvs }
zsChecksum(render)                // CRC-32 of the interleaved PCM
zsEncodeWav(render)               // -> Uint8Array, a 16-bit stereo WAV file
zbVerifyOne(name, worldSeed)      // -> { crc, deterministic, oracle, ok }
zbVerifyAll(worldSeed)            // -> { [name]: result }

const player = new ZS2Engine();
player.play(render);              // creates the AudioContext on first use
player.setVolume(0.5);
```

A patch looks like this (see the grounding document for every field):

```js
{ name, category, archetype, worldSeed, soundSeed, baseFreq, hold,
  reverb: { preset, depth },
  layers: [{ role, source, freq, step0, sweepOct, sweepTime, vibDepth, vibRate,
             pmon, mute, pmDepth, adsr: { ar, am, dr, sl, sm, sd, sr, rm, rr },
             hold, volL, volR, reverb, notes: [{ t, ratio }] }] }
```

Patches are plain data. You can edit one before rendering it, for example to change `reverb.preset` per room.

## Using the Python engine

```bash
pip install numpy

python ZeroSounds2.py --list                              # sound names by category
python ZeroSounds2.py --sound confirm --out out/          # one WAV
python ZeroSounds2.py --sound jump --sound laser --seed 0xDEADBEEF --out out/
python ZeroSounds2.py --all --out out/                    # all 40
python ZeroSounds2.py --random 7 --out out/               # unnamed patch 7
python ZeroSounds2.py --params confirm                    # the derived patch as JSON
python ZeroSounds2.py --verify                            # determinism and level check
python ZeroSounds2.py --checksums                         # CRC-32 per sound, as JSON
```

Files are written as `<name>_<SEED>.wav`, 16-bit stereo at 44.1 kHz, and match the browser's output for the same seed byte for byte.

From Python:

```python
import ZeroSounds2 as zs

patch = zs.zb_derive_params('explosion', 0xDEADBEEF)
render = zs.zs_render(patch)           # dict with 'left' and 'right' int16 arrays
zs.zs_write_wav('explosion.wav', render)
```

`--verify` over all 40 sounds takes about 10 seconds; the envelope, codec and reverb loops are plain Python.

## Sound library

40 named sounds in 8 categories. Each is a four-field anchor, not a preset.

| Category | Sounds |
|---|---|
| **ui** | cursor, confirm, cancel, pause, textBlip, error, memoryCard |
| **pickup** | crystal, item, key, health |
| **movement** | jump, land, dash, footstep, roll |
| **combat** | slash, gunshot, punch, laser, magic, block |
| **impact** | explosion, crash, thud, glassBreak |
| **power** | powerUp, levelUp, heal, charge, checkpoint |
| **damage** | hurt, death, alarm, poison |
| **environment** | door, chest, splash, warp, switch |

Six archetypes decide how a sound's voices are layered:

| Archetype | Voices |
|---|---|
| `bell` | FM body and a detuned FM shimmer |
| `synth` | Wavetable and a detuned copy |
| `arp` | Body and octave voice, re-keyed for 2 to 5 notes |
| `noise` | Two noise voices at different rates, pitch-swept |
| `hit` | Noise crack and a pitch-drop sine body |
| `sweep` | A silent voice pitch-modulating a swept carrier, plus a chorus voice |

### Adding a sound

Add one line to `ZB_SOUNDS` in both `ZeroSounds2.py` and `ZeroSounds2.jsx`, using a `styleIndex` not already taken in that category, then run `python build-demo.py`.

```js
stab: { category: 'combat', arch: 'noise', styleIndex: 6, dir: -1, label: 'Stab' },
```

`dir` is `1` for rising, `-1` for falling, `0` to let the hash choose.

## Parameters

Both engines use the same names and ranges.

| Parameter | Range | Meaning |
|---|---|---|
| `worldSeed` | 0 to `0xFFFFFFFF` | Defines the whole library |
| `baseFreq` | per category, 45 to 2000 Hz | Root pitch |
| `hold` | per category, 0.04 to 0.9 s | Time until key-off |
| `reverb.preset` | room, studio, hall, space | Reverb size |
| `reverb.depth` | 0.05 to 0.65 | Reverb level |
| `source.kind` | fm, wave, noise, thump | Source sample type |
| `source.cycle` | 28, 56, 112 | Samples per cycle; lower is grainier |
| `step0` | up to 3.5 | Pitch counter step (1.0 plays at 44.1 kHz) |
| `sweepOct` | -3 to 2.5 | Pitch glide in octaves |
| `adsr.ar` / `sr` | 0 to 127 | SPU attack and sustain rate registers |
| `adsr.dr` / `sl` | 0 to 15 | SPU decay shift and sustain level |
| `adsr.rr` | 0 to 31 | SPU release shift |
| `volL`, `volR` | 0 to `0x4000` | Voice volume per channel |

## Requirements

| Interface | Needs |
|---|---|
| Demo | A current browser; internet access for the React, Babel and font CDNs |
| React component | React 17 or later (hooks). No other packages |
| Python engine | Python 3.8 or later, NumPy |
| `build-demo.py` | Same as the Python engine |

## Verification

What has been checked:

- 40 named sounds and 4 unnamed patches at three seeds (132 renders) gave identical CRC-32s from the Python engine and from the JavaScript engine under Node.js.
- `--verify` passed at five seeds: every sound reproducible, non-silent and free of clipping.
- The demo was mounted in a headless browser test: 40 pads rendered, sounds played, and the in-page check reported all 40 matching the Python checksums.

What has not been checked: no listening tests and no comparison against real hardware or an SPU emulator. Treat the parameter ranges as a starting point for tuning by ear.

## Continuing development

The repository is meant to be cloned and developed further, for example with Claude Code. `ZeroSounds2-grounding.md` is the context document, `ZeroSounds2.py` is the behavioural oracle (any port must reproduce its checksums), and the engine block in `ZeroSounds2.jsx` is the natural boundary for a TypeScript module. There is no `ZeroSounds2-TINS.md` implementation plan yet.

## License

See `LICENSE`. No licence file has been added to this repository yet; the original ZeroSounds-JSX ships its own `LICENSE`, so add the one you intend before publishing.

## 📚 Citation

### Academic Citation

If you use this codebase in your research or project, please cite:

```bibtex
@software{zerosounds2_jsx,
  title = {ZeroSounds2-JSX: ZeroBytes-Aligned Deterministic PlayStation-Style Audio Synthesis},
  author = {Drift Johnson},
  year = {2026},
  url = {https://github.com/MushroomFleet/zerosounds2-JSX},
  version = {1.0.0}
}
```

### Donate

[![Ko-Fi](https://cdn.ko-fi.com/cdn/kofi3.png?v=3)](https://ko-fi.com/driftjohnson)
