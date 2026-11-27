#!/usr/bin/env python3
"""
ZeroSounds2.py - Zero Sounds 2 (PlayStation-style SFX synthesis), reference engine.

Deterministic, procedural sound effects in the style of the PlayStation SPU:
every sound is a handful of SPU "voices", and every voice is

    procedural source sample -> SPU-ADPCM encode/decode -> pitch counter with
    4-point Gaussian interpolation -> SPU ADSR envelope -> stereo volume

mixed and sent through an SPU-topology reverb running at 22.05 kHz.

Everything is derived from hash(soundName, worldSeed) with the ZeroBytes rule
set: no random(), no stored presets, no libm transcendentals in the signal
path. Output is 16-bit stereo at 44.1 kHz and is bit-identical to the
JavaScript engine in ZeroSounds2.jsx (compare CRC-32s with --checksums).

See ZeroSounds2-grounding.md for the design. Requires NumPy.

    python ZeroSounds2.py --list
    python ZeroSounds2.py --sound confirm --out out/
    python ZeroSounds2.py --all --seed 0xDEADBEEF --out out/
    python ZeroSounds2.py --verify
"""
import argparse
import json
import os
import sys
import wave
import zlib

import numpy as np

SR = 44100                 # SPU output rate
DEFAULT_SEED = 0x1337BEEF
M32 = 0xFFFFFFFF

# ============================================================================
# 1. ZB HASH CORE (identical to ZeroSounds 1)
# ============================================================================

def _imul(a, b):
    return (a * b) & M32


def zb_hash(key, seed=0):
    """Murmur3-style mix. key: non-negative int or str. Returns u32."""
    h = ((seed & M32) ^ 0x9E3779B9) & M32
    if isinstance(key, str):
        for ch in key:
            h ^= ord(ch) & 0xFFFF
            h = _imul(h, 0x5BD1E995)
            h ^= h >> 15
        return h & M32
    h ^= int(key) & M32
    h = _imul(h, 0xCC9E2D51)
    h = ((h << 15) | (h >> 17)) & M32
    h = _imul(h, 0x1B873593)
    h ^= h >> 16
    h = _imul(h, 0x85EBCA6B)
    h ^= h >> 13
    h = _imul(h, 0xC2B2AE35)
    h ^= h >> 16
    return h & M32


def zb_hash_vec(keys, seed):
    """Vectorised numeric-key zb_hash over a uint64 array of u32 keys."""
    m = np.uint64(M32)
    h = np.uint64(((seed & M32) ^ 0x9E3779B9) & M32) ^ (keys.astype(np.uint64) & m)
    h = (h * np.uint64(0xCC9E2D51)) & m
    h = ((h << np.uint64(15)) | (h >> np.uint64(17))) & m
    h = (h * np.uint64(0x1B873593)) & m
    h ^= h >> np.uint64(16)
    h = (h * np.uint64(0x85EBCA6B)) & m
    h ^= h >> np.uint64(13)
    h = (h * np.uint64(0xC2B2AE35)) & m
    h ^= h >> np.uint64(16)
    return h


def zb_float(h):
    return (h & 0xFFFFFF) / 0x1000000


def zb_range(h, lo, hi):
    return lo + zb_float(h) * (hi - lo)


def zb_int(h, lo, hi):
    return lo + (h % (hi - lo + 1))


def zb_pick(h, arr):
    return arr[h % len(arr)]

# ============================================================================
# 2. DETERMINISTIC MATH
# Only + - * / floor are used, in a fixed order, so results are bit-identical
# on any IEEE-754 double implementation (CPython, NumPy, every JS engine).
# ============================================================================
PI = 3.141592653589793
TWO_PI = 6.283185307179586
HALF_PI = 1.5707963267948966
LN2 = 0.6931471805599453

_S = [-1.0 / 6.0, 1.0 / 120.0, -1.0 / 5040.0, 1.0 / 362880.0,
      -1.0 / 39916800.0, 1.0 / 6227020800.0, -1.0 / 1307674368000.0]
_E = [1.0 / 2.0, 1.0 / 6.0, 1.0 / 24.0, 1.0 / 120.0, 1.0 / 720.0, 1.0 / 5040.0,
      1.0 / 40320.0, 1.0 / 362880.0, 1.0 / 3628800.0, 1.0 / 39916800.0,
      1.0 / 479001600.0, 1.0 / 6227020800.0]


def dsin(x):
    """sin(x): range reduction + odd Taylor polynomial (Horner)."""
    x = np.asarray(x, dtype=np.float64)
    r = x - np.floor(x / TWO_PI + 0.5) * TWO_PI
    r = np.where(r > HALF_PI, PI - r, r)
    r = np.where(r < -HALF_PI, -PI - r, r)
    r2 = r * r
    p = _S[6]
    for k in (5, 4, 3, 2, 1, 0):
        p = p * r2 + _S[k]
    return r * (1.0 + r2 * p)


def dexp2(x):
    """2**x: exact power-of-two scaling of a Taylor series on the fraction."""
    x = np.asarray(x, dtype=np.float64)
    n = np.floor(x)
    y = (x - n) * LN2
    p = _E[11]
    for k in range(10, -1, -1):
        p = p * y + _E[k]
    e = 1.0 + y * (1.0 + y * p)
    return np.ldexp(e, n.astype(np.int64))


def fdsin(x):
    return float(dsin(x))


def fdexp2(x):
    return float(dexp2(x))


def rnd(x):
    """Round half up (never banker's rounding)."""
    return np.floor(np.asarray(x, dtype=np.float64) + 0.5).astype(np.int64)

# ============================================================================
# 3. SOUND TAXONOMY (anchors only - no parameters are stored)
# ============================================================================
ZB_CATEGORIES = {
    'ui':          dict(index=0, freqLow=500, freqHigh=1400, durLow=0.04, durHigh=0.16, revLow=0.15, revHigh=0.45, rooms=['room', 'studio', 'hall']),
    'pickup':      dict(index=1, freqLow=700, freqHigh=2000, durLow=0.08, durHigh=0.30, revLow=0.25, revHigh=0.60, rooms=['studio', 'hall']),
    'movement':    dict(index=2, freqLow=90,  freqHigh=420,  durLow=0.05, durHigh=0.20, revLow=0.05, revHigh=0.25, rooms=['room']),
    'combat':      dict(index=3, freqLow=110, freqHigh=900,  durLow=0.06, durHigh=0.35, revLow=0.10, revHigh=0.40, rooms=['room', 'studio']),
    'impact':      dict(index=4, freqLow=45,  freqHigh=200,  durLow=0.15, durHigh=0.60, revLow=0.20, revHigh=0.55, rooms=['studio', 'hall']),
    'power':       dict(index=5, freqLow=260, freqHigh=1100, durLow=0.30, durHigh=0.90, revLow=0.30, revHigh=0.65, rooms=['hall', 'space']),
    'damage':      dict(index=6, freqLow=120, freqHigh=520,  durLow=0.10, durHigh=0.60, revLow=0.10, revHigh=0.40, rooms=['room', 'studio']),
    'environment': dict(index=7, freqLow=90,  freqHigh=700,  durLow=0.08, durHigh=0.50, revLow=0.25, revHigh=0.60, rooms=['hall', 'space', 'studio']),
}
CATEGORY_ORDER = ['ui', 'pickup', 'movement', 'combat', 'impact', 'power', 'damage', 'environment']

# arch: bell | synth | arp | noise | hit | sweep.   dir: +1 rising, -1 falling, 0 hashed.
ZB_SOUNDS = {
    'cursor':     dict(category='ui', arch='bell',  styleIndex=0, dir=0,  label='Cursor'),
    'confirm':    dict(category='ui', arch='arp',   styleIndex=1, dir=1,  label='Confirm'),
    'cancel':     dict(category='ui', arch='synth', styleIndex=2, dir=-1, label='Cancel'),
    'pause':      dict(category='ui', arch='arp',   styleIndex=3, dir=-1, label='Pause'),
    'textBlip':   dict(category='ui', arch='synth', styleIndex=4, dir=0,  label='Text blip'),
    'error':      dict(category='ui', arch='sweep', styleIndex=5, dir=-1, label='Error'),
    'memoryCard': dict(category='ui', arch='bell',  styleIndex=6, dir=0,  label='Memory card'),
    'crystal':    dict(category='pickup', arch='arp',  styleIndex=0, dir=1, label='Crystal'),
    'item':       dict(category='pickup', arch='bell', styleIndex=1, dir=0, label='Item'),
    'key':        dict(category='pickup', arch='bell', styleIndex=2, dir=0, label='Key'),
    'health':     dict(category='pickup', arch='arp',  styleIndex=3, dir=1, label='Health'),
    'jump':       dict(category='movement', arch='sweep', styleIndex=0, dir=1,  label='Jump'),
    'land':       dict(category='movement', arch='hit',   styleIndex=1, dir=0,  label='Land'),
    'dash':       dict(category='movement', arch='noise', styleIndex=2, dir=1,  label='Dash'),
    'footstep':   dict(category='movement', arch='hit',   styleIndex=3, dir=0,  label='Footstep'),
    'roll':       dict(category='movement', arch='noise', styleIndex=4, dir=-1, label='Roll'),
    'slash':      dict(category='combat', arch='noise', styleIndex=0, dir=-1, label='Slash'),
    'gunshot':    dict(category='combat', arch='hit',   styleIndex=1, dir=0,  label='Gunshot'),
    'punch':      dict(category='combat', arch='hit',   styleIndex=2, dir=0,  label='Punch'),
    'laser':      dict(category='combat', arch='sweep', styleIndex=3, dir=-1, label='Laser'),
    'magic':      dict(category='combat', arch='sweep', styleIndex=4, dir=1,  label='Magic'),
    'block':      dict(category='combat', arch='bell',  styleIndex=5, dir=0,  label='Block'),
    'explosion':  dict(category='impact', arch='hit',   styleIndex=0, dir=0,  label='Explosion'),
    'crash':      dict(category='impact', arch='noise', styleIndex=1, dir=-1, label='Crash'),
    'thud':       dict(category='impact', arch='hit',   styleIndex=2, dir=0,  label='Thud'),
    'glassBreak': dict(category='impact', arch='bell',  styleIndex=3, dir=0,  label='Glass break'),
    'powerUp':    dict(category='power', arch='arp',   styleIndex=0, dir=1, label='Power up'),
    'levelUp':    dict(category='power', arch='arp',   styleIndex=1, dir=1, label='Level up'),
    'heal':       dict(category='power', arch='bell',  styleIndex=2, dir=0, label='Heal'),
    'charge':     dict(category='power', arch='sweep', styleIndex=3, dir=1, label='Charge'),
    'checkpoint': dict(category='power', arch='arp',   styleIndex=4, dir=1, label='Checkpoint'),
    'hurt':       dict(category='damage', arch='sweep', styleIndex=0, dir=-1, label='Hurt'),
    'death':      dict(category='damage', arch='arp',   styleIndex=1, dir=-1, label='Death'),
    'alarm':      dict(category='damage', arch='arp',   styleIndex=2, dir=1,  label='Alarm'),
    'poison':     dict(category='damage', arch='sweep', styleIndex=3, dir=-1, label='Poison'),
    'door':       dict(category='environment', arch='noise', styleIndex=0, dir=-1, label='Door'),
    'chest':      dict(category='environment', arch='arp',   styleIndex=1, dir=1,  label='Chest'),
    'splash':     dict(category='environment', arch='noise', styleIndex=2, dir=-1, label='Splash'),
    'warp':       dict(category='environment', arch='sweep', styleIndex=3, dir=1,  label='Warp'),
    'switch':     dict(category='environment', arch='hit',   styleIndex=4, dir=0,  label='Switch'),
}
ARCHETYPES = ['bell', 'synth', 'arp', 'noise', 'hit', 'sweep']

ARP_PATTERNS = [[0, 4, 7, 12, 16], [0, 7, 12, 19, 24], [0, 5, 7, 12, 17],
                [0, 3, 7, 10, 15], [0, 12, 7, 12, 19], [0, 6, 0, 6, 0],
                [0, 2, 4, 7, 9], [0, 12, 0, 12, 0]]
FM_RATIOS = [1.0, 2.0, 3.0, 3.5, 4.0, 5.0, 5.5, 7.0, 0.5, 1.5]
WAVE_SHAPES = ['pulse', 'saw', 'tri', 'organ', 'glass']
CYCLES = [28, 56, 112]     # samples per cycle of a tonal source (lo-fi -> hi-fi)
MAX_STEP = 3.5             # keep tonal voices under the SPU 4.0x pitch ceiling

# SPU ADSR register ranges per envelope family
ENV = {
    'perc':  dict(ar=(0, 12), dr=(11, 14), sl=(0, 6),  sr=(44, 58), rr=(10, 13)),
    'sus':   dict(ar=(8, 36), dr=(9, 12),  sl=(8, 15), sr=(52, 66), rr=(7, 11)),
    'noise': dict(ar=(0, 30), dr=(9, 13),  sl=(2, 12), sr=(42, 56), rr=(9, 13)),
    'hit':   dict(ar=(0, 4),  dr=(8, 11),  sl=(0, 3),  sr=(40, 50), rr=(8, 11)),
}

# Reverb presets: delay scale, wall reflection, damping, tail seconds.
# Parametric defaults in the SPU topology - not the Sony preset register tables.
REVERBS = {
    'room':   dict(scale=0.30, wall=0.55, iir=0.65, tail=0.5),
    'studio': dict(scale=0.50, wall=0.62, iir=0.60, tail=0.8),
    'hall':   dict(scale=1.00, wall=0.72, iir=0.50, tail=1.6),
    'space':  dict(scale=1.70, wall=0.80, iir=0.40, tail=2.4),
}

# ============================================================================
# 4. PARAMETER DERIVATION  (world -> category -> sound -> layer -> slot)
# ============================================================================

def _derive_adsr(h, family):
    e = ENV[family]
    return dict(
        ar=zb_int(h(20), e['ar'][0], e['ar'][1]),      # attack rate 0..127
        am=(h(21) & 1) if family == 'sus' else 0,      # attack mode 0 lin / 1 exp
        dr=zb_int(h(22), e['dr'][0], e['dr'][1]),      # decay shift 0..15
        sl=zb_int(h(23), e['sl'][0], e['sl'][1]),      # sustain level 0..15
        sm=1,                                          # sustain mode exp
        sd=1,                                          # sustain direction decrease
        sr=zb_int(h(24), e['sr'][0], e['sr'][1]),      # sustain rate 0..127
        rm=(h(25) & 1) if family == 'sus' else 1,      # release mode 0 lin / 1 exp
        rr=zb_int(h(26), e['rr'][0], e['rr'][1]),      # release shift 0..31
    )


def _derive_source(kind, h, seed, fmax):
    if kind in ('fm', 'wave'):
        cands = [c for c in CYCLES if fmax * c / SR <= MAX_STEP] or [28]
        cycle = zb_pick(h(10), cands)
    if kind == 'fm':
        return dict(kind='fm', cycle=cycle, ratio=zb_pick(h(11), FM_RATIOS),
                    index0=zb_range(h(12), 1.0, 7.0), indexLoop=zb_range(h(13), 0.1, 1.2),
                    attackLoops=zb_int(h(14), 4, 40), seed=seed)
    if kind == 'wave':
        return dict(kind='wave', cycle=cycle, shape=zb_pick(h(11), WAVE_SHAPES),
                    duty=zb_range(h(12), 0.125, 0.5), harmonics=zb_pick(h(13), [6, 10, 16, 24]),
                    seed=seed)
    if kind == 'noise':
        return dict(kind='noise', blocks=292, color=zb_range(h(11), 0.12, 1.0), seed=seed)
    return dict(kind='thump', blocks=zb_int(h(11), 60, 140), f1=zb_range(h(12), 120.0, 320.0),
                f2=zb_range(h(13), 35.0, 70.0), seed=seed)


def _derive(name, category, arch, style_index, direction, world_seed):
    cat = ZB_CATEGORIES[category]
    cat_seed = zb_hash(cat['index'], world_seed)
    sound_seed = zb_hash(style_index, cat_seed)
    hs = lambda k: zb_hash(100 + k, sound_seed)

    base_freq = zb_range(hs(0), cat['freqLow'], cat['freqHigh'])
    hold = zb_range(hs(1), cat['durLow'], cat['durHigh'])
    reverb = dict(preset=zb_pick(hs(2), cat['rooms']), depth=zb_range(hs(3), cat['revLow'], cat['revHigh']))
    if direction == 0:
        direction = 1 if (hs(7) & 1) else -1

    notes = [dict(t=0.0, ratio=1.0)]
    if arch == 'arp':
        pat = zb_pick(hs(5), ARP_PATTERNS)
        count = zb_int(hs(4), 2, len(pat))
        gap = zb_range(hs(6), 0.045, 0.11)
        notes = [dict(t=i * gap, ratio=fdexp2(direction * pat[i] / 12.0)) for i in range(count)]
    max_ratio = max(n['ratio'] for n in notes)
    pan = zb_range(hs(9), -0.5, 0.5)

    def layer(i, kind, role, family, freq=None, step0=None, vol=0.5, pan_=0.0,
              sweep=0.0, sweep_frac=1.0, vib=0.0, hold_=None, notes_=None,
              pmon=False, mute=False, pm_depth=0.0, send=True):
        seed = zb_hash(i + 1, sound_seed)
        h = lambda k: zb_hash(k, seed)
        nts = notes_ if notes_ is not None else [dict(t=0.0, ratio=1.0)]
        fmax = 0.0
        if freq is not None:
            top = max(n['ratio'] for n in nts) * fdexp2(max(0.0, sweep) + vib)
            freq = min(freq, MAX_STEP * SR / 28.0 / top)     # stay under the pitch ceiling
            fmax = freq * top
        src = _derive_source(kind, h, seed, fmax)
        st = freq * src['cycle'] / SR if freq is not None else step0
        hd = hold if hold_ is None else hold_
        v = int(rnd(vol * 0x4000))
        return dict(role=role, source=src, freq=freq, step0=st,
                    sweepOct=sweep, sweepTime=max(0.005, hd * sweep_frac),
                    vibDepth=vib, vibRate=zb_range(h(30), 4.0, 9.0),
                    pmon=pmon, mute=mute, pmDepth=pm_depth,
                    adsr=_derive_adsr(h, family), hold=hd,
                    volL=int(rnd(v * min(1.0, 1.0 - pan_))), volR=int(rnd(v * min(1.0, 1.0 + pan_))),
                    reverb=send, notes=nts)

    L = []
    if arch == 'bell':
        L.append(layer(0, 'fm', 'body', 'perc', freq=base_freq, vol=0.62, pan_=pan,
                       vib=zb_range(hs(10), 0.0, 0.006)))
        L.append(layer(1, 'fm', 'shimmer', 'perc',
                       freq=base_freq * zb_pick(hs(11), [2.0, 2.0, 3.0, 1.5, 4.0]) * (1.0 + zb_range(hs(12), 0.002, 0.008)),
                       vol=0.28, pan_=-pan))
    elif arch == 'synth':
        sw = direction * zb_range(hs(10), 0.0, 0.5)
        L.append(layer(0, 'wave', 'body', 'sus', freq=base_freq, vol=0.50, pan_=pan, sweep=sw))
        L.append(layer(1, 'wave', 'chorus', 'sus', freq=base_freq * (1.0 + zb_range(hs(11), 0.004, 0.012)),
                       vol=0.30, pan_=-pan, sweep=sw))
    elif arch == 'arp':
        kind = 'fm' if (hs(8) & 1) else 'wave'
        note_hold = min(hold, notes[1]['t'] * 1.5) if len(notes) > 1 else hold
        L.append(layer(0, kind, 'body', 'perc', freq=base_freq, vol=0.36, pan_=pan,
                       hold_=note_hold, notes_=notes))
        L.append(layer(1, 'wave' if kind == 'fm' else 'fm', 'octave', 'perc', freq=base_freq * 2.0,
                       vol=0.15, pan_=-pan, hold_=note_hold, notes_=notes))
    elif arch == 'noise':
        sw = direction * zb_range(hs(10), 0.4, 2.5)
        st = zb_range(hs(11), 0.5, 2.0)
        frac = zb_range(hs(12), 0.6, 1.4)
        L.append(layer(0, 'noise', 'body', 'noise', step0=st, vol=0.55, pan_=pan, sweep=sw, sweep_frac=frac))
        L.append(layer(1, 'noise', 'width', 'noise', step0=st * zb_range(hs(13), 0.5, 0.8),
                       vol=0.35, pan_=-pan, sweep=sw, sweep_frac=frac))
    elif arch == 'hit':
        big = hold >= 0.15                      # long holds become dark, heavy impacts
        k = 0.45 if big else 1.0
        L.append(layer(0, 'noise', 'crack', 'noise' if big else 'hit', step0=zb_range(hs(10), 0.8, 2.5) * k,
                       vol=0.50, pan_=pan, sweep=-zb_range(hs(11), 1.0, 3.0), sweep_frac=zb_range(hs(12), 0.3, 1.0)))
        L.append(layer(1, 'thump', 'body', 'perc', step0=zb_range(hs(13), 0.35, 0.9) * k, vol=0.70, pan_=0.0))
    else:  # sweep
        lfo = (hs(10) & 1) == 1
        mod_freq = zb_range(hs(11), 6.0, 40.0) if lfo else base_freq * zb_pick(hs(11), [0.5, 1.0, 2.0, 3.0, 0.25])
        sw = direction * zb_range(hs(12), 0.7, 2.5)
        frac = zb_range(hs(13), 0.6, 1.2)
        kind = 'fm' if (hs(14) & 1) else 'wave'
        L.append(layer(0, 'wave', 'modulator', 'sus', freq=mod_freq, vol=0.5, mute=True, send=False,
                       pm_depth=zb_range(hs(15), 0.05, 0.6)))
        L.append(layer(1, kind, 'carrier', 'sus', freq=base_freq, vol=0.55, pan_=pan,
                       sweep=sw, sweep_frac=frac, pmon=True))
        L.append(layer(2, kind, 'chorus', 'sus', freq=base_freq * (1.0 + zb_range(hs(16), 0.004, 0.012)),
                       vol=0.22, pan_=-pan, sweep=sw, sweep_frac=frac))

    return dict(name=name, category=category, archetype=arch, direction=direction,
                worldSeed=world_seed & M32, soundSeed=sound_seed,
                baseFreq=base_freq, hold=hold, reverb=reverb, layers=L)


def zb_derive_params(name, world_seed=DEFAULT_SEED):
    """Named sound -> patch. O(1), pure."""
    s = ZB_SOUNDS.get(name)
    if s is None:
        raise KeyError('Unknown sound: %s' % name)
    return _derive(name, s['category'], s['arch'], s['styleIndex'], s['dir'], world_seed)


def zb_randomize(n, world_seed=DEFAULT_SEED):
    """Unnamed sound at position n of the infinite patch space."""
    v = zb_hash(n, world_seed ^ 0x5A5A5A5A)
    category = CATEGORY_ORDER[zb_int(zb_hash(0, v), 0, 7)]
    arch = ARCHETYPES[zb_int(zb_hash(1, v), 0, 5)]
    return _derive('patch#%d' % n, category, arch, 1000 + (v & 0xFFFF), 0, world_seed)

# ============================================================================
# 5. PROCEDURAL SOURCE SAMPLES (the "virtual sample ROM")
# Every sample value is a closed-form function of its index n.
# Returns (int list, loop_start or -1).
# ============================================================================

def gen_source(src):
    kind = src['kind']
    if kind == 'fm':
        P = src['cycle']
        n_att = src['attackLoops'] * 2 * P
        n = np.arange(n_att + 2 * P, dtype=np.float64)
        t = np.where(n < n_att, 1.0 - n / n_att, 0.0)
        idx = src['indexLoop'] + (src['index0'] - src['indexLoop']) * (t * t)
        ph = TWO_PI * n / P
        y = dsin(ph + idx * dsin(src['ratio'] * ph))
        return rnd(26000.0 * y).tolist(), n_att
    if kind == 'wave':
        P = src['cycle']
        n = np.arange(2 * P, dtype=np.float64)
        ph = TWO_PI * n / P
        hmax = min(src['harmonics'], P // 2 - 1)
        y = np.zeros(2 * P)
        for k in range(1, hmax + 1):
            a = _wave_amp(src, k)
            if a != 0.0:
                y = y + a * dsin(k * ph)
        peak = float(np.max(np.abs(y)))
        return rnd(26000.0 * y / peak).tolist(), 0
    if kind == 'noise':
        N = 28 * src['blocks']
        w = (zb_hash_vec(np.arange(N, dtype=np.uint64), src['seed']) & np.uint64(0xFFFFFF)).astype(np.float64)
        w = (w / 0x1000000 * 2.0 - 1.0).tolist()
        a = src['color']
        y = 0.0
        out = []
        peak = 0.0
        for v in w:                       # one-pole colour filter over O(1) white noise
            y = y + a * (v - y)
            out.append(y)
            if abs(y) > peak:
                peak = abs(y)
        return rnd(24000.0 * np.array(out) / peak).tolist(), 0
    # thump: sine whose pitch falls f1 -> f2, phase integrated in closed form
    N = 28 * src['blocks']
    n = np.arange(N, dtype=np.float64)
    c1 = src['f1'] / 22050.0
    c2 = src['f2'] / 22050.0
    t = 1.0 - n / N
    t2 = t * t
    ph = TWO_PI * (c2 * n + (c1 - c2) * N * 0.25 * (1.0 - t2 * t2))
    return rnd(28000.0 * (dsin(ph) * t2)).tolist(), -1


def _wave_amp(src, k):
    shape = src['shape']
    jit = 0.85 + 0.3 * zb_float(zb_hash(300 + k, src['seed']))
    if shape == 'saw':
        a = 1.0 / k
    elif shape == 'pulse':
        a = fdsin(PI * k * src['duty']) / k
    elif shape == 'tri':
        a = 0.0 if k % 2 == 0 else (1.0 if (k // 2) % 2 == 0 else -1.0) / (k * k)
    elif shape == 'organ':
        a = zb_range(zb_hash(200 + k, src['seed']), 0.2, 1.0) if k in (1, 2, 3, 4, 6, 8) else 0.0
    else:  # glass
        a = 1.0 / k if k in (1, 3, 4, 7, 9, 13) else 0.0
    return a * jit

# ============================================================================
# 6. SPU-ADPCM  (28 samples -> 16 bytes: shift/filter header + 4-bit nibbles)
# ============================================================================
K0 = [0, 60, 115, 98, 122]
K1 = [0, 0, -52, -55, -60]


def adpcm_codec(s):
    """Encode then decode. Returns (decoded int list, [(shift, filter)] per block)."""
    out = []
    hdr = []
    h1 = h2 = 0        # decoder history
    o1 = o2 = 0        # original history (open-loop range estimate)
    for b in range(0, len(s), 28):
        best_err = -1
        best = None
        for f in range(5):
            k0 = K0[f]
            k1 = K1[f]
            p1, p2, m = o1, o2, 0
            for k in range(28):
                x = s[b + k]
                e = x - ((p1 * k0 + p2 * k1 + 32) >> 6)
                if e < 0:
                    e = -e
                if e > m:
                    m = e
                p2 = p1
                p1 = x
            q = 0
            while q < 12 and 7 * (1 << q) < m:
                q += 1
            for qq in ((q, q + 1) if q < 12 else (q,)):
                step = 1 << qq
                a1, a2, err = h1, h2, 0
                dec = []
                for k in range(28):
                    x = s[b + k]
                    pred = (a1 * k0 + a2 * k1 + 32) >> 6
                    nib = (2 * (x - pred) + step) // (2 * step)
                    if nib > 7:
                        nib = 7
                    elif nib < -8:
                        nib = -8
                    d = pred + nib * step
                    if d > 32767:
                        d = 32767
                    elif d < -32768:
                        d = -32768
                    err += (x - d) * (x - d)
                    dec.append(d)
                    a2 = a1
                    a1 = d
                if best_err < 0 or err < best_err:
                    best_err = err
                    best = (dec, 12 - qq, f)
        out.extend(best[0])
        hdr.append((best[1], best[2]))
        h1, h2 = best[0][27], best[0][26]
        o1, o2 = s[b + 27], s[b + 26]
    return out, hdr

# ============================================================================
# 7. SPU VOICE: Gaussian interpolation + ADSR
# ============================================================================

def _gauss_table():
    """512-entry interpolation kernel. Analytic fit (peak 0.7008, sigma 0.569),
    not the console ROM table."""
    j = np.arange(512, dtype=np.float64)
    x = (511.0 - j) / 256.0
    return rnd(32768.0 * 0.7008 * dexp2(-(x * x) / (2.0 * 0.569 * 0.569) / LN2))


GAUSS = _gauss_table()


def adsr_run(a, keyoff, cap):
    """SPU ADSR state machine. Returns the per-sample level (0..0x7FFF) until
    the release reaches zero (at most `cap` samples after key-off)."""
    out = []
    level = 0
    counter = 0
    phase = 0                      # 0 attack, 1 decay, 2 sustain, 3 release
    sus_level = (a['sl'] + 1) * 0x800
    i = 0
    end = keyoff + cap
    while i < end:
        if i == keyoff:
            phase = 3
        if phase == 0:
            exp, inc, shift, step = a['am'], True, a['ar'] >> 2, 7 - (a['ar'] & 3)
        elif phase == 1:
            exp, inc, shift, step = 1, False, a['dr'], -8
        elif phase == 2:
            inc = a['sd'] == 0
            exp, shift = a['sm'], a['sr'] >> 2
            step = 7 - (a['sr'] & 3) if inc else -8 + (a['sr'] & 3)
        else:
            exp, inc, shift, step = a['rm'], False, a['rr'], -8
        cycles = 1 << (shift - 11 if shift > 11 else 0)
        st = step << (11 - shift if shift < 11 else 0)
        if exp and inc and level > 0x6000:
            cycles *= 4
        if exp and not inc:
            st = (st * level) >> 15
        counter += 1
        if counter >= cycles:
            counter = 0
            level += st
            if level < 0:
                level = 0
            elif level > 0x7FFF:
                level = 0x7FFF
        if phase == 0 and level >= 0x7FFF:
            phase = 1
        elif phase == 1 and level <= sus_level:
            phase = 2
        out.append(level)
        i += 1
        if phase == 3 and level == 0:
            break
    return np.array(out, dtype=np.int64)


def render_voice(pcm, loop_start, layer, ratio, mod=None, mod_off=0):
    """One key-on of one SPU voice. Returns mono int64 samples (post-ADSR)."""
    env = adsr_run(layer['adsr'], int(layer['hold'] * SR), 3 * SR)
    n = len(env)
    i = np.arange(n, dtype=np.float64)
    sweep_n = max(1, int(layer['sweepTime'] * SR))
    u = np.minimum(1.0, i / sweep_n)
    u = u * (2.0 - u)
    ex = layer['sweepOct'] * u
    if layer['vibDepth'] > 0.0:
        ex = ex + layer['vibDepth'] * dsin(TWO_PI * layer['vibRate'] * i / SR)
    step = (layer['step0'] * ratio) * dexp2(ex)
    if mod is not None:                               # SPU pitch modulation (PMON)
        m = np.zeros(n)
        seg = mod[mod_off:mod_off + n]
        m[:len(seg)] = seg
        step = step * ((32768.0 + m) / 32768.0)
    si = np.clip(rnd(step * 4096.0), 1, 0x3FFF)       # 4.12 fixed-point pitch
    pos = np.cumsum(si) - si
    idx = pos >> 12
    fr = (pos >> 4) & 0xFF
    p = np.array(pcm + [0], dtype=np.int64)           # last slot = silence
    L = len(pcm)

    def tap(k):
        if loop_start >= 0:
            k = np.where(k >= L, loop_start + (k - loop_start) % (L - loop_start), k)
        else:
            k = np.where(k >= L, L, k)
        return p[np.where(k < 0, L, k)]

    out = (GAUSS[255 - fr] * tap(idx - 1) + GAUSS[511 - fr] * tap(idx)
           + GAUSS[256 + fr] * tap(idx + 1) + GAUSS[fr] * tap(idx + 2)) >> 15
    return (out * env) >> 15

# ============================================================================
# 8. SPU-TOPOLOGY REVERB (22.05 kHz, wall reflections -> 4 comb taps -> 2 APF)
# ============================================================================

def spu_reverb(in_l, in_r, preset):
    p = REVERBS[preset]
    sc, wall, iir = p['scale'], p['wall'], p['iir']
    d = lambda b: max(2, int(b * sc))
    n_sl, n_sr, n_dl, n_dr = d(3529), d(3907), d(2903), d(3319)
    n_a1, n_a2 = d(613), d(359)
    t1l, t2l, t3l, t4l = int(n_sl * 0.93), int(n_sl * 0.71), int(n_dl * 0.83), int(n_dl * 0.59)
    t1r, t2r, t3r, t4r = int(n_sr * 0.93), int(n_sr * 0.71), int(n_dr * 0.83), int(n_dr * 0.59)
    SL = [0.0] * n_sl; SRb = [0.0] * n_sr; DL = [0.0] * n_dl; DR = [0.0] * n_dr
    A1L = [0.0] * n_a1; A1R = [0.0] * n_a1; A2L = [0.0] * n_a2; A2R = [0.0] * n_a2
    w_sl = w_sr = w_dl = w_dr = w_a1 = w_a2 = 0
    zl1 = zr1 = zl2 = zr2 = 0.0
    g_in = 1.0 - wall
    g1, g2 = 0.6, 0.5
    half = len(in_l) // 2
    out_l = [0.0] * (half * 2)
    out_r = [0.0] * (half * 2)
    pl = pr = 0.0
    for j in range(half):
        xl = (in_l[2 * j] + in_l[2 * j + 1]) * 0.5 * g_in
        xr = (in_r[2 * j] + in_r[2 * j + 1]) * 0.5 * g_in
        a = SL[w_sl]; b = SRb[w_sr]; c = DR[w_dr]; e = DL[w_dl]
        zl1 = zl1 + (xl + a * wall - zl1) * iir
        zr1 = zr1 + (xr + b * wall - zr1) * iir
        zl2 = zl2 + (xl + c * wall - zl2) * iir
        zr2 = zr2 + (xr + e * wall - zr2) * iir
        SL[w_sl] = zl1; SRb[w_sr] = zr1; DL[w_dl] = zl2; DR[w_dr] = zr2
        lo = (0.40 * SL[(w_sl - t1l + n_sl) % n_sl] + 0.35 * SL[(w_sl - t2l + n_sl) % n_sl]
              + 0.35 * DL[(w_dl - t3l + n_dl) % n_dl] + 0.30 * DL[(w_dl - t4l + n_dl) % n_dl])
        ro = (0.40 * SRb[(w_sr - t1r + n_sr) % n_sr] + 0.35 * SRb[(w_sr - t2r + n_sr) % n_sr]
              + 0.35 * DR[(w_dr - t3r + n_dr) % n_dr] + 0.30 * DR[(w_dr - t4r + n_dr) % n_dr])
        z = A1L[w_a1]; v = lo - g1 * z; A1L[w_a1] = v; lo = v * g1 + z
        z = A1R[w_a1]; v = ro - g1 * z; A1R[w_a1] = v; ro = v * g1 + z
        z = A2L[w_a2]; v = lo - g2 * z; A2L[w_a2] = v; lo = v * g2 + z
        z = A2R[w_a2]; v = ro - g2 * z; A2R[w_a2] = v; ro = v * g2 + z
        w_sl = (w_sl + 1) % n_sl; w_sr = (w_sr + 1) % n_sr
        w_dl = (w_dl + 1) % n_dl; w_dr = (w_dr + 1) % n_dr
        w_a1 = (w_a1 + 1) % n_a1; w_a2 = (w_a2 + 1) % n_a2
        out_l[2 * j] = (pl + lo) * 0.5; out_l[2 * j + 1] = lo
        out_r[2 * j] = (pr + ro) * 0.5; out_r[2 * j + 1] = ro
        pl = lo; pr = ro
    return out_l, out_r

# ============================================================================
# 9. RENDER
# ============================================================================

def zs_render(patch):
    """Patch -> dict(left, right int16 arrays, sampleRate, stats)."""
    voices = []        # (offset, mono, layer)
    monos = []
    spu_ram = 0
    snr = []
    for layer in patch['layers']:
        raw, loop_start = gen_source(layer['source'])
        pcm, hdr = adpcm_codec(raw)
        spu_ram += len(hdr) * 16
        r = np.array(raw, dtype=np.float64)
        e = r - np.array(pcm, dtype=np.float64)
        snr.append(10.0 * np.log10(float(np.sum(r * r)) / max(1e-9, float(np.sum(e * e)))))
        prev = monos[-1] if (layer['pmon'] and monos) else None
        parts = []
        for note in layer['notes']:
            off = int(note['t'] * SR)
            v = render_voice(pcm, loop_start, layer, note['ratio'], prev, off)
            parts.append((off, v))
        n = max(off + len(v) for off, v in parts)
        mono = np.zeros(n, dtype=np.int64)
        for off, v in parts:
            mono[off:off + len(v)] += v
        # what the next voice sees on its pitch-modulation input
        monos.append(np.floor(mono * layer['pmDepth']) if layer['mute'] else mono.astype(np.float64))
        voices.append((mono, layer))

    rv = REVERBS[patch['reverb']['preset']]
    n = max(len(m) for m, _ in voices) + int(rv['tail'] * SR)
    n += n & 1
    dry_l = np.zeros(n, dtype=np.int64); dry_r = np.zeros(n, dtype=np.int64)
    snd_l = np.zeros(n, dtype=np.int64); snd_r = np.zeros(n, dtype=np.int64)
    for mono, layer in voices:
        if layer['mute']:
            continue
        l = (mono * layer['volL']) >> 14
        r = (mono * layer['volR']) >> 14
        dry_l[:len(l)] += l; dry_r[:len(r)] += r
        if layer['reverb']:
            snd_l[:len(l)] += l; snd_r[:len(r)] += r
    wet_l, wet_r = spu_reverb(snd_l.astype(np.float64).tolist(), snd_r.astype(np.float64).tolist(),
                              patch['reverb']['preset'])
    depth = patch['reverb']['depth']
    fl = rnd(dry_l + np.array(wet_l) * depth)
    fr = rnd(dry_r + np.array(wet_r) * depth)
    clipped = int(np.sum((fl > 32767) | (fl < -32768) | (fr > 32767) | (fr < -32768)))
    fl = np.clip(fl, -32768, 32767); fr = np.clip(fr, -32768, 32767)
    loud = np.nonzero((np.abs(fl) > 24) | (np.abs(fr) > 24))[0]
    end = min(n, int(loud[-1]) + 257) if len(loud) else 1
    left = fl[:end].astype('<i2'); right = fr[:end].astype('<i2')
    return dict(left=left, right=right, sampleRate=SR, clipped=clipped,
                spuRamBytes=spu_ram, adpcmSnrDb=snr)


def zs_checksum(r):
    """CRC-32 of the interleaved little-endian int16 stream."""
    return zlib.crc32(np.column_stack((r['left'], r['right'])).astype('<i2').tobytes()) & M32


def zs_write_wav(path, r):
    with wave.open(path, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(r['sampleRate'])
        w.writeframes(np.column_stack((r['left'], r['right'])).astype('<i2').tobytes())


def zb_verify_all(world_seed=DEFAULT_SEED):
    """Render every sound twice; report per-sound checks."""
    res = {}
    for name in ZB_SOUNDS:
        a = zs_render(zb_derive_params(name, world_seed))
        b = zs_render(zb_derive_params(name, world_seed))
        peak = int(max(np.max(np.abs(a['left'].astype(np.int64))), np.max(np.abs(a['right'].astype(np.int64)))))
        res[name] = dict(crc='%08X' % zs_checksum(a), deterministic=zs_checksum(a) == zs_checksum(b),
                         seconds=round(len(a['left']) / SR, 3), peak=peak, clipped=a['clipped'],
                         spuRamBytes=a['spuRamBytes'], adpcmSnrDb=round(float(min(a['adpcmSnrDb'])), 1))
    return res

# ============================================================================
# 10. CLI
# ============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description='Zero Sounds 2 - PlayStation-style deterministic SFX synthesis')
    ap.add_argument('--seed', default=hex(DEFAULT_SEED), help='world seed (hex or decimal), default 0x1337BEEF')
    ap.add_argument('--out', default='out', help='output directory for WAV files')
    ap.add_argument('--list', action='store_true', help='list named sounds')
    ap.add_argument('--sound', action='append', default=[], help='render a named sound (repeatable)')
    ap.add_argument('--all', action='store_true', help='render every named sound')
    ap.add_argument('--random', type=int, action='append', default=[], metavar='N', help='render unnamed patch N (repeatable)')
    ap.add_argument('--params', metavar='NAME', help='print the derived patch as JSON')
    ap.add_argument('--verify', action='store_true', help='render all sounds twice and report determinism and levels')
    ap.add_argument('--checksums', action='store_true', help='print {sound: CRC32} JSON for the seed (compare with the JS engine)')
    args = ap.parse_args(argv)
    seed = int(args.seed, 0) & M32

    if args.list:
        for c in CATEGORY_ORDER:
            print('%-12s %s' % (c, ', '.join(k for k, v in ZB_SOUNDS.items() if v['category'] == c)))
        return 0
    if args.params:
        print(json.dumps(zb_derive_params(args.params, seed), indent=2))
        return 0
    if args.checksums:
        print(json.dumps({k: '%08X' % zs_checksum(zs_render(zb_derive_params(k, seed))) for k in ZB_SOUNDS}, indent=1))
        return 0
    if args.verify:
        res = zb_verify_all(seed)
        ok = True
        for k, v in res.items():
            good = v['deterministic'] and v['peak'] > 500 and v['clipped'] == 0
            ok = ok and good
            print('%-11s %s crc=%s %5.2fs peak=%5d clip=%d ram=%5dB adpcm-snr=%5.1fdB' % (
                k, 'ok  ' if good else 'FAIL', v['crc'], v['seconds'], v['peak'], v['clipped'],
                v['spuRamBytes'], v['adpcmSnrDb']))
        print('seed 0x%08X: %d sounds, %s' % (seed, len(res), 'ALL PASS' if ok else 'FAILURES'))
        return 0 if ok else 1

    jobs = [(n, zb_derive_params(n, seed)) for n in (list(ZB_SOUNDS) if args.all else args.sound)]
    jobs += [('patch%d' % n, zb_randomize(n, seed)) for n in args.random]
    if not jobs:
        ap.print_help()
        return 0
    os.makedirs(args.out, exist_ok=True)
    for name, patch in jobs:
        r = zs_render(patch)
        path = os.path.join(args.out, '%s_%08X.wav' % (name, seed))
        zs_write_wav(path, r)
        print('%-28s %5.2fs crc=%08X' % (path, len(r['left']) / SR, zs_checksum(r)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
