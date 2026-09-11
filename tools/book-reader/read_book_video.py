#!/usr/bin/env python3
"""
read_book_video -- read a cardboard organ book from a book-scan video.

Some organ enthusiasts publish their books as scrolling scans: the cardboard
moves under a fixed vertical playhead, one row of holes per key, and the holes
that are sounding are drawn in red. That picture *is* the score, so it can be
read exactly, frame by frame, with no listening involved:

  1. find the playhead (the blue vertical line),
  2. fit a regular lattice of N key rows to the hole profile (neighbouring
     holes touch at video resolution, so peak-picking would merge rows),
  3. for every frame, note which rows are red at the playhead,
  4. optionally join chain perforations (--chain-gap N frames) for scales
     that punch sustained notes as rows of short holes. Off by default: on
     the Limonaire 49 books read so far, dotted rows were fast repeated notes.

Outputs, next to the video (or at --out PREFIX):
  PREFIX.rows.json     lattice, playhead, per-row hole statistics
  PREFIX.events.json   per row, the [start_s, end_s] intervals
  PREFIX.book.mid      one track, MIDI note = row index (0 = top row)
  PREFIX.book.png      the whole book unrolled, time left to right
  PREFIX.overlay.png   one frame with the fitted rows drawn, to check alignment

Rows are anonymous here. The organ's scale (which row is which pipe, drum or
register) turns the row-indexed MIDI into an organ-format file: with the
scale as an organ.yaml-style track map, `organ_arranger` does the rest.

    read_book_video.py valse-brune.webm --keys 49
    read_book_video.py book.mp4 --keys 52 --out scratch/mybook --check-frame 80
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

__version__ = "0.1.0"

DEFAULT_KEYS = 49
CHAIN_GAP_FRAMES = 0           # off: on these books the dotted rows are repeated notes, not chained sustains


def masks(frame, np, cv2):
    b, g, r = cv2.split(frame.astype(np.int16))
    red = (r > 150) & (g < 110) & (b < 110)
    white = (r > 225) & (g > 225) & (b > 225)
    blue = (b > 120) & (r < 110) & (g < 110)
    return red, white, blue


def fit_lattice(profile, keys: int, height: int):
    """Pitch and offset of `keys` equally spaced rows that best cover the
    hole profile. Returns (pitch, offset, score)."""
    import numpy as np
    prof = np.asarray(profile, dtype=float)
    ks = np.arange(keys)
    best = None
    for pitch100 in range(450, 1200, 2):                        # 4.5 .. 12 px per row
        pitch = pitch100 / 100
        span = (keys - 1) * pitch
        if span >= height - 1:
            continue
        offs = np.arange(0, height - 1 - span, 0.2)
        ys = np.rint(offs[:, None] + ks[None, :] * pitch).astype(int)
        scores = prof[np.clip(ys, 0, height - 1)].sum(axis=1)
        i = int(np.argmax(scores))
        if best is None or scores[i] > best[2]:
            best = (pitch, float(offs[i]), float(scores[i]))
    return best


def read(video: Path, keys: int, out: Path, check_frame: int, chain_gap: int) -> dict:
    import cv2
    import numpy as np
    import mido

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # pass 1: playhead column and hole-row profile, from every 10th frame
    blue_cols = np.zeros(W)
    hole_rows = np.zeros(H)
    for i in range(0, n, 10):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, frame = cap.read()
        if not ok:
            break
        red, white, blue = masks(frame, np, cv2)
        blue_cols += blue.sum(axis=0)
        hole_rows += (white | red).sum(axis=1)
    playhead = int(np.argmax(blue_cols))
    hole_rows[:6] = 0
    hole_rows[-6:] = 0                                  # the frame border is not a row
    profile = hole_rows / max(hole_rows.max(), 1)
    pitch, off, score = fit_lattice(profile, keys, H)
    rows = [off + k * pitch for k in range(keys)]
    print(f"{video.name}: {W}x{H} @ {fps:g} fps, {n} frames ({n / fps:.1f} s); playhead x={playhead}; "
          f"{keys} rows, pitch {pitch:.2f} px from y={off:.1f} (fit {score:.1f})")

    # pass 2: red at the playhead, every frame
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    x0, x1 = max(0, playhead - 3), min(W, playhead + 4)
    on = np.zeros((n, keys), dtype=bool)
    check = None
    for f in range(n):
        ok, frame = cap.read()
        if not ok:
            n = f
            break
        if f == check_frame:
            check = frame.copy()
        red, _, _ = masks(frame, np, cv2)
        col = red[:, x0:x1].any(axis=1)
        for k, y in enumerate(rows):
            lo, hi = int(round(y - pitch / 2)) + 1, int(round(y + pitch / 2))
            on[f, k] = col[max(lo, 0):min(hi, H)].any()

    # intervals, with chain perforations joined
    events: dict[int, list[list[float]]] = {}
    for k in range(keys):
        ivs: list[list[float]] = []
        start = None
        for f in range(n):
            if on[f, k] and start is None:
                start = f
            if not on[f, k] and start is not None:
                ivs.append([start / fps, f / fps])
                start = None
        if start is not None:
            ivs.append([start / fps, n / fps])
        joined: list[list[float]] = []
        for s, e in ivs:
            if joined and (s - joined[-1][1]) * fps <= chain_gap + 0.5:
                joined[-1][1] = e
            else:
                joined.append([s, e])
        events[k] = joined

    # statistics per row, for whoever maps rows to the scale
    stats = []
    for k in range(keys):
        ivs = events[k]
        lens = [e - s for s, e in ivs]
        stats.append({"row": k, "holes": len(ivs),
                      "mean_len_s": round(sum(lens) / len(lens), 3) if lens else None,
                      "max_len_s": round(max(lens), 3) if lens else None,
                      "first_s": round(ivs[0][0], 2) if ivs else None,
                      "last_s": round(ivs[-1][1], 2) if ivs else None})

    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"video": video.name, "fps": fps, "frames": n, "playhead_x": playhead, "keys": keys,
               "row_pitch_px": pitch, "row_offset_px": off, "rows_y": rows, "chain_gap_frames": chain_gap,
               "stats": stats}, open(f"{out}.rows.json", "w"), indent=1)
    json.dump(events, open(f"{out}.events.json", "w"))

    # the book unrolled
    img = np.full((keys * 4, n, 3), (200, 180, 150), dtype=np.uint8)
    for k in range(keys):
        for s, e in events[k]:
            a, b = int(s * fps), max(int(e * fps), int(s * fps) + 1)
            img[k * 4 + 1:k * 4 + 3, a:b] = (255, 255, 255)
    cv2.imwrite(f"{out}.book.png", img)

    # the check frame with rows drawn
    if check is not None:
        big = cv2.resize(check, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
        for k, y in enumerate(rows):
            cv2.line(big, (0, int(round(y * 2))), (big.shape[1] - 1, int(round(y * 2))), (0, 160, 0), 1)
            if k % 5 == 0:
                cv2.putText(big, str(k), (4, int(round(y * 2)) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 90, 0), 1)
        cv2.line(big, (playhead * 2, 0), (playhead * 2, big.shape[0] - 1), (255, 0, 0), 1)
        cv2.imwrite(f"{out}.overlay.png", big)

    # row-indexed MIDI
    tpb, tempo = 480, 500_000
    mid = mido.MidiFile(type=1, ticks_per_beat=tpb)
    cond = mido.MidiTrack()
    cond.append(mido.MetaMessage("track_name", name=f"{video.stem} (book scan, {keys} rows)", time=0))
    cond.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    cond.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(cond)
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("track_name", name="Book rows", time=0))
    evs = []
    for k, ivs in events.items():
        for s, e in ivs:
            evs.append((int(round(mido.second2tick(s, tpb, tempo))), 1, k))
            evs.append((int(round(mido.second2tick(e, tpb, tempo))), 0, k))
    evs.sort()
    prev = 0
    for tick, is_on, k in evs:
        tr.append(mido.Message("note_on" if is_on else "note_off", channel=0, note=k,
                               velocity=100 if is_on else 0, time=tick - prev))
        prev = tick
    tr.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tr)
    mid.save(f"{out}.book.mid")

    print(f"{sum(len(v) for v in events.values())} holes on {sum(1 for v in events.values() if v)} of {keys} rows "
          f"-> {out}.book.mid / .events.json / .rows.json / .book.png")
    return {"fps": fps, "rows": rows, "events": events, "stats": stats}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="read_book_video", description="Read an organ book from a book-scan video.")
    p.add_argument("video")
    p.add_argument("--keys", type=int, default=DEFAULT_KEYS, help=f"rows on the book (default {DEFAULT_KEYS})")
    p.add_argument("--out", help="output prefix (default: the video's path without extension)")
    p.add_argument("--check-frame", type=int, default=100, help="frame to draw the fitted rows on (default 100)")
    p.add_argument("--chain-gap", type=int, default=CHAIN_GAP_FRAMES,
                   help=f"join holes in a row separated by this many frames or fewer (default {CHAIN_GAP_FRAMES})")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)
    try:
        import cv2, numpy, mido  # noqa: F401
    except ImportError as e:
        print(f"error: needs opencv-python-headless, numpy and mido: pip install opencv-python-headless numpy mido ({e})",
              file=sys.stderr)
        return 2
    video = Path(a.video)
    out = Path(a.out) if a.out else video.with_suffix("")
    read(video, a.keys, out, a.check_frame, a.chain_gap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
