#!/usr/bin/env python3
"""Which way round are the rows? Ask the recording.

A book read from a picture gives anonymous rows: row 0 is the top of the scan
or the left of the handheld picture, and whether that is key 1 or key 49 is not
written anywhere. The organ's own sound settles it. For each candidate order
and transposition, every pitched row's on/off pattern is correlated with the
energy at the pitch the scale sheet says that key plays; the right order lights
up its own pitches, the wrong one lights up strangers.

    python book_audio_order.py out/tune.events.json --audio tune.m4a --scale limonaire49_book_scale.xlsx

The audio may be the video's own track (PyAV decodes it, no ffmpeg needed).
The hole times come from the reading line, which sits some way before the key
frame, so the sound lags the holes by a constant; the lag is measured first
from the onsets and applied before scoring. Prints a ranked table; the top
line's `--top-key` is what book_to_organ wants.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

__version__ = "0.1.0"

SR = 22050
N_FFT = 4096
HOP = 512


def decode_mono(path: Path, np):
    import av
    c = av.open(str(path))
    s = c.streams.audio[0]
    rs = av.AudioResampler(format="fltp", layout="mono", rate=SR)
    chunks = []
    for fr in c.decode(s):
        for out in rs.resample(fr):
            chunks.append(out.to_ndarray().reshape(-1))
    for out in rs.resample(None):
        chunks.append(out.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype(np.float32)


def semitone_energy(x, cents: float, lo=36, hi=112, np=None):
    """Log energy per semitone band (MIDI lo..hi) per hop, A4 = 440 Hz shifted by `cents`."""
    n = 1 + (len(x) - N_FFT) // HOP
    win = np.hanning(N_FFT).astype(np.float32)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[idx] * win
    mag = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    notes = np.arange(lo, hi + 1)
    centre = 440.0 * 2 ** ((notes - 69) / 12 + cents / 1200)
    lo_f, hi_f = centre * 2 ** (-0.5 / 12), centre * 2 ** (0.5 / 12)
    E = np.zeros((n, len(notes)), np.float32)
    for i in range(len(notes)):
        b = (freqs >= lo_f[i]) & (freqs < hi_f[i])
        if b.any():
            E[:, i] = mag[:, b].sum(axis=1)
    E = np.log1p(E)
    E = (E - E.mean(axis=0)) / (E.std(axis=0) + 1e-6)
    return E, notes, mag


def indicators(events, n_hops, lag_s, np):
    fps = SR / HOP
    ind = np.zeros((len(events), n_hops), np.float32)
    for k, ivs in enumerate(events):
        for s, e in ivs:
            a, b = int((s + lag_s) * fps), int((e + lag_s) * fps) + 1
            ind[k, max(0, a): min(n_hops, b)] = 1
    return ind


def find_lag(events, mag, np, max_lag_s=4.0):
    """The sound starts a constant time after a hole passes the reading line: the lag
    that best aligns hole onsets with spectral flux."""
    fps = SR / HOP
    n = mag.shape[0]
    flux = np.maximum(np.diff(np.log1p(mag), axis=0), 0).sum(axis=1)
    flux = np.concatenate([[0], flux])
    flux = (flux - flux.mean()) / (flux.std() + 1e-6)
    onsets = np.zeros(n, np.float32)
    for ivs in events:
        for s, e in ivs:
            i = int(s * fps)
            if 0 <= i < n:
                onsets[i] += 1
    best = (0.0, -1e9)
    for lag in np.arange(0, max_lag_s, 1 / fps):
        sh = int(lag * fps)
        score = float((onsets[: n - sh] * flux[sh:]).sum())
        if score > best[1]:
            best = (float(lag), score)
    return best[0]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("events", help="events.json from a book reader")
    p.add_argument("--audio", required=True, help="the recording (the video file itself, or its audio track)")
    p.add_argument("--scale", required=True, help="layout sheet with a 'book scale' column")
    p.add_argument("--keys", type=int, default=49)
    p.add_argument("--transpose", default="-14:14", help="semitone range to try, lo:hi")
    p.add_argument("--cents", default="-50,-25,0,25", help="A4 offsets to try, in cents")
    p.add_argument("--lag", type=float, default=None, help="seconds from the reading line to the sound (else measured)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from book_to_organ import read_scale

    events = json.load(open(a.events))
    keymap = read_scale(Path(a.scale))
    x = decode_mono(Path(a.audio), np)
    print(f"audio {len(x) / SR:.1f} s; {sum(len(e) for e in events)} holes on {sum(1 for e in events if e)} rows", file=sys.stderr)
    lo, hi = (int(v) for v in a.transpose.split(":"))
    cents_list = [float(v) for v in a.cents.split(",")]
    E0, notes, mag = semitone_energy(x, 0.0, np=np)
    lag = a.lag if a.lag is not None else find_lag(events, mag, np)
    print(f"lag reading line -> sound: {lag:.2f} s", file=sys.stderr)
    n = E0.shape[0]
    ind = indicators(events, n, lag, np)
    weights = np.array([np.sqrt(len(e)) for e in events])
    ind_z = (ind - ind.mean(axis=1, keepdims=True)) / (ind.std(axis=1, keepdims=True) + 1e-6)

    def pitched(key):
        km = keymap.get(key)
        if not km:
            return None
        track = km[0].lower()
        if "drum" in track or "regist" in track or "perc" in track:
            return None
        return km[1]

    orders = {"top-key 49 (row 0 = key 49, keys descend)": lambda r: a.keys - r,
              "top-key 1 (row 0 = key 1, keys ascend)": lambda r: r + 1}
    results = []
    for cents in cents_list:
        E, notes, _ = semitone_energy(x, cents, np=np) if cents else (E0, notes, None)
        for name, key_of in orders.items():
            for t in range(lo, hi + 1):
                score, used = 0.0, 0
                for r, ivs in enumerate(events):
                    if len(ivs) < 3:
                        continue
                    note = pitched(key_of(r))
                    if note is None:
                        continue
                    nn = note + t
                    if nn < notes[0] or nn > notes[-1]:
                        continue
                    e = E[:, nn - notes[0]]
                    score += weights[r] * float((ind_z[r] * e).mean())
                    used += 1
                results.append((score, name, t, cents, used))
    results.sort(reverse=True)
    print(f"{'score':>8}  {'order':44} {'semitones':>9} {'cents':>6} rows")
    for score, name, t, cents, used in results[:12]:
        print(f"{score:8.2f}  {name:44} {t:+9d} {cents:+6.0f} {used}")
    best = results[0]
    second_other = next(r for r in results if r[1] != best[1])
    print(f"\nbest: {best[1]} at {best[2]:+d} semitones, {best[3]:+.0f} cents; the other order's best scores "
          f"{second_other[0]:.2f} against {best[0]:.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
