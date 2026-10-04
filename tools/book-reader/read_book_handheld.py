#!/usr/bin/env python3
"""Read a cardboard organ book from handheld footage of it going into the key frame.

The scan reader (read_book_video) has a fixed playhead and holes drawn in red.
This one has the real thing: a camera held over the organ, the cardboard sliding
under the pressure roller into the key frame, holes as dark rectangles on light
card, the picture shaking and tilted. It works in four stages, each leaving a
picture to check:

  calib    find the roller and the book's edges on a reference frame, build the
           homography that straightens the book plane; writes PREFIX.calib.png
  mosaic   stabilise every frame against the fixed woodwork (features matched to
           the reference frame), straighten a band of card just below the
           roller, measure how far the book advanced since the previous frame,
           and lay the bands into one long image of the book; writes
           PREFIX.mosaic.png and PREFIX.track.json (frame -> position)
  read     find the holes on the mosaic, fit the lattice of key columns, turn
           every hole into a [start, end] in seconds through the frame->position
           track; writes PREFIX.events.json, .book.mid, .rows.json, .book.png
  all      the three in turn

    python read_book_handheld.py burlesque.mp4 --keys 49 --out scratch/burlesque --stage all

Rows are anonymous and numbered from the LEFT of the picture as the book comes
towards the camera; `--flip` numbers them from the right. book_to_organ.py takes
it from there with --top-key, exactly as for the scans. Timing is by frame, not
by millimetre: a hole's time is the frame in which it passed the reading band,
so the book's speed never has to be known, and a steady drive is assumed only
for smoothing the measured advance.

The homography straightens the trapezoid the book makes in the picture into a
rectangle; the small perspective left along the card's length only stretches
the mosaic, and the frame->position track absorbs it. What matters, the
position across the book, is held by the two edges the calibration found and
by the self-check: after the mosaic is built the holes' columns are measured
and a residual shear is taken out.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

__version__ = "0.1.0"

DEFAULT_KEYS = 49
BOOK_W = 600                 # rectified pixels across the book, edge to edge
BAND_TOP, BAND_BOTTOM = 18, 100   # rectified rows below the roller contact line that are read: close to the roller, where every frame agrees
MIN_INLIERS = 40
ORB_FEATURES = 4000


# ----------------------------------------------------------------------------
# calibration on a reference frame
# ----------------------------------------------------------------------------

def find_roller(gray, np, cv2):
    """The pressure roller is a long horizontal spring, and nothing else in the
    picture is periodic: the rows whose spectrum along x is dominated by the coil
    pitch (4 to 16 px). Returns (y_top, y_bottom) of the roller band."""
    h, w = gray.shape
    seg = gray[:, int(0.25 * w): int(0.8 * w)].astype(np.float32)
    seg = seg - seg.mean(axis=1, keepdims=True)
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.shape[1]), axis=1))
    freqs = np.fft.rfftfreq(seg.shape[1])
    band = (freqs > 1 / 16) & (freqs < 1 / 4)
    ratio = spec[:, band].sum(axis=1) / (spec[:, 1:].sum(axis=1) + 1e-6)
    ratio = np.convolve(ratio, np.ones(7) / 7, mode="same")
    peak = int(np.argmax(ratio[: int(0.75 * h)]))
    thr = ratio[peak] * 0.5
    top = peak
    while top > 0 and ratio[top - 1] > thr:
        top -= 1
    bot = peak
    while bot < h - 1 and ratio[bot + 1] > thr:
        bot += 1
    return top, bot


def find_roller_line(gray, np, cv2):
    """The roller is not level in the picture: its axis is fitted from the coil band
    found in several column strips. Returns (slope, intercept, half_height) with the
    axis as y = slope * x + intercept."""
    h, w = gray.shape
    xs, ys, hhs = [], [], []
    n_strips = 8
    x0, x1 = int(0.2 * w), int(0.88 * w)
    for s in range(n_strips):
        a = x0 + (x1 - x0) * s // n_strips
        b = x0 + (x1 - x0) * (s + 1) // n_strips
        # find_roller looks at the middle 55 % of what it is given: hand it the strip alone
        top, bot = find_roller(gray[:, a:b], np, cv2)
        if bot - top < 20 or bot - top > 0.5 * h:
            continue
        xs.append((a + b) / 2)
        ys.append((top + bot) / 2)
        hhs.append((bot - top) / 2)
    if len(xs) < 3:
        raise SystemExit("could not find the roller in enough column strips")
    xs, ys = np.array(xs), np.array(ys)
    for _ in range(2):
        m, c = np.polyfit(xs, ys, 1)
        res = np.abs(ys - (m * xs + c))
        keep = res < max(6.0, 2.5 * np.median(res) + 1)
        if keep.sum() >= 3:
            xs, ys = xs[keep], ys[keep]
    m, c = np.polyfit(xs, ys, 1)
    return float(m), float(c), float(np.median(hhs))


def find_edges(gray, y_from, y_to, np, cv2):
    """The book's left and right edges below the roller: in each row, the outermost
    long run of light card. Fits a line to each. Returns ((x_at_y_from, x_at_y_to) left,
    same right) and the row range actually used."""
    h, w = gray.shape
    g = cv2.GaussianBlur(gray, (0, 0), 2.0).astype(np.float32)
    pts_l, pts_r = [], []
    box = 61                                                # wider than any hole: holes vanish, the card stays
    for y in range(y_from, y_to, 2):
        row = g[y]
        lo, hi = np.percentile(row, 8), np.percentile(row, 92)
        if hi - lo < 40:
            continue
        light = (row > (lo + hi) / 2).astype(np.float32)   # the card against the dark wood, whatever the exposure
        frac = np.convolve(light, np.ones(box) / box, mode="same")
        card = np.nonzero(frac > 0.6)[0]
        if len(card) < 0.3 * w:
            continue
        if card[0] > box:                                   # an edge at the picture's border is no edge
            pts_l.append((y, int(card[0])))
        if card[-1] < w - 1 - box:
            pts_r.append((y, int(card[-1]) + 1))
    if len(pts_l) < 10 or len(pts_r) < 10:
        raise SystemExit(f"could not find the book's edges below the roller ({len(pts_l)} left, {len(pts_r)} right rows)")

    def fit(pts):
        ys = np.array([p[0] for p in pts], float)
        xs = np.array([p[1] for p in pts], float)
        for _ in range(3):                                  # robust: drop the outliers twice
            a, b = np.polyfit(ys, xs, 1)
            res = np.abs(xs - (a * ys + b))
            keep = res < max(3.0, 2.5 * np.median(res) + 1)
            ys, xs = ys[keep], xs[keep]
        a, b = np.polyfit(ys, xs, 1)
        return a, b

    al, bl = fit(pts_l)
    ar, br = fit(pts_r)
    return (al, bl), (ar, br)


def spring_extent(gray, m, c, hh, np, cv2):
    """How far the coils run along the roller axis: the x range where narrow column
    strips around the axis still show the coil periodicity. The ends of the spring
    are, near enough, the edges of the book."""
    h, w = gray.shape
    strip_w = 64                                             # wide enough to resolve a 12 px coil pitch
    xs, ok = [], []
    for x in range(0, w - strip_w, 8):
        ys = np.arange(int(max(0, m * x + c - hh * 0.7)), int(min(h, m * x + c + hh * 0.7)))
        if len(ys) < 20:
            xs.append(x + strip_w / 2); ok.append(False)
            continue
        patch = gray[ys[0]: ys[-1] + 1, x: x + strip_w].astype(np.float32)
        seg = patch - patch.mean(axis=1, keepdims=True)
        spec = np.abs(np.fft.rfft(seg, axis=1))
        freqs = np.fft.rfftfreq(strip_w)
        band = (freqs > 1 / 16) & (freqs < 1 / 4)
        ratio = (spec[:, band].sum(axis=1) / (spec[:, 1:].sum(axis=1) + 1e-6)).mean()
        xs.append(x + strip_w / 2); ok.append(ratio > 0.4)
    xs, ok = np.array(xs), np.array(ok)
    # the longest run of coil strips
    best, cur, start = (0, 0, 0), 0, 0
    for i in range(len(ok) + 1):
        if i < len(ok) and ok[i]:
            if cur == 0:
                start = i
            cur += 1
        else:
            if cur > best[0]:
                best = (cur, start, i - 1)
            cur = 0
    if best[0] < 5:
        raise SystemExit("could not find the spring's extent along the roller")
    return float(xs[best[1]]), float(xs[best[2]])


def travel_vanishing_point(gray, c_contact, m, np, cv2):
    """Where the book's long holes point: each elongated dark hole below the roller
    gives a line along the travel direction; the lines meet at the vanishing point."""
    h, w = gray.shape
    g = gray.astype(np.float32)
    bg = np.maximum(cv2.GaussianBlur(g, (0, 0), 25), 1)
    dark = ((g / bg) < 0.6).astype(np.uint8) * 255
    below = np.zeros_like(dark)
    yy, xx = np.mgrid[0:h, 0:w]
    below[yy > m * xx + c_contact + 6] = 255
    dark &= below
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, labels, stats, cents = cv2.connectedComponentsWithStats(dark, connectivity=8)
    lines = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 150 or bw > 0.3 * w or bh > 0.6 * h:
            continue
        pts = np.column_stack(np.nonzero(labels[y:y + bh, x:x + bw] == i))[:, ::-1].astype(np.float32)
        pts += (x, y)
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(pts)
        long_, short = max(rw, rh), min(rw, rh)
        if short < 4 or long_ < 2.2 * short or long_ < 24:
            continue
        theta = np.deg2rad(ang if rw >= rh else ang + 90)
        d = np.array([np.cos(theta), np.sin(theta)])
        nrm = np.array([-d[1], d[0]])
        lines.append((nrm[0], nrm[1], -(nrm[0] * cx + nrm[1] * cy), long_))
    if len(lines) < 4:
        raise SystemExit(f"only {len(lines)} long holes in the reference frame: pick another --ref-frame")
    L = np.array(lines)
    for _ in range(3):
        A = L[:, :3] * np.sqrt(L[:, 3:4])                    # longer holes weigh more
        _, _, vt = np.linalg.svd(A)
        p = vt[-1]
        if abs(p[2]) < 1e-9:
            break
        vp = p[:2] / p[2]
        res = np.abs(L[:, 0] * vp[0] + L[:, 1] * vp[1] + L[:, 2])
        keep = res < max(8.0, 2.5 * np.median(res))
        if keep.sum() < 4:
            break
        L = L[keep]
    return float(vp[0]), float(vp[1]), int(len(L))


def calibrate(frame, keys, np, cv2) -> dict:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    m, c, hh = find_roller_line(gray, np, cv2)
    # the card appears just under the spring: the contact line is the axis moved down
    # by the roller's apparent radius and a margin, parallel to the axis
    c_contact = c + hh + 8
    xs_l, xs_r = spring_extent(gray, m, c, hh, np, cv2)
    vx, vy, nlines = travel_vanishing_point(gray, c_contact, m, np, cv2)
    # the book's edges: the lines through the vanishing point and the spring's ends
    # (taken on the roller axis), written x = a*y + b like any travel line

    def edge_through(x0, y0):
        a = (x0 - vx) / (y0 - vy)
        return a, vx - vy * a

    al, bl = edge_through(xs_l, m * xs_l + c)
    ar, br = edge_through(xs_r, m * xs_r + c)

    def meet(a, b, cc):
        """Where the travel line x = a*y + b meets the cross line y = m*x + cc."""
        y = (m * b + cc) / (1 - m * a)
        return a * y + b, y

    L = 400.0                                                # image px down to the lower cross line
    xl0, yl0 = meet(al, bl, c_contact)
    xr0, yr0 = meet(ar, br, c_contact)
    c_lower = c_contact + L
    xl1, yl1 = meet(al, bl, c_lower)
    xr1, yr1 = meet(ar, br, c_lower)
    width0 = float(np.hypot(xr0 - xl0, yr0 - yl0))
    scale = BOOK_W / width0                                  # rectified px per image px at the contact line
    length = float(np.hypot(xl1 - xl0, yl1 - yl0)) * scale
    src = np.float32([[xl0, yl0], [xr0, yr0], [xr1, yr1], [xl1, yl1]])
    dst = np.float32([[0, 0], [BOOK_W, 0], [BOOK_W, length], [0, length]])
    H = cv2.getPerspectiveTransform(src, dst)
    return {"roller_axis": [m, c], "roller_half_height": hh, "contact_c": float(c_contact),
            "spring_x": [xs_l, xs_r], "vanishing_point": [vx, vy], "vp_lines": nlines,
            "left": [float(al), float(bl)], "right": [float(ar), float(br)],
            "quad": src.tolist(), "book_w": BOOK_W, "length": float(length),
            "H": H.tolist(), "frame_size": [int(w), int(h)]}


def draw_calib(frame, cal, out_png, np, cv2):
    img = frame.copy()
    q = np.int32(cal["quad"])
    cv2.polylines(img, [q.reshape(-1, 1, 2)], True, (0, 255, 0), 2)
    w = img.shape[1]
    m, c = cal["roller_axis"]
    hh = cal["roller_half_height"]
    for cc, col in ((c - hh, (255, 128, 0)), (c + hh, (255, 128, 0)), (cal["contact_c"], (0, 0, 255))):
        cv2.line(img, (0, int(cc)), (w, int(m * w + cc)), col, 1)
    Hm = np.array(cal["H"])
    # the reading band in image space
    inv = np.linalg.inv(Hm)
    for yb in (BAND_TOP, BAND_BOTTOM):
        pts = cv2.perspectiveTransform(np.float32([[[0, yb], [BOOK_W, yb]]]), inv)[0]
        cv2.line(img, tuple(np.int32(pts[0])), tuple(np.int32(pts[1])), (0, 255, 255), 2)
    rect = cv2.warpPerspective(frame, Hm, (BOOK_W, int(cal["length"])))
    cv2.imwrite(str(out_png), img)
    cv2.imwrite(str(out_png).replace(".calib.png", ".rectified.png"), rect)


# ----------------------------------------------------------------------------
# stabilisation and the mosaic
# ----------------------------------------------------------------------------

def fixed_mask(cal, np, cv2):
    """Where the fixed parts are, in reference-frame coordinates: everything that is
    not the moving card. The card region is the book quad extended to the bottom."""
    w, h = cal["frame_size"]
    m = np.full((h, w), 255, np.uint8)
    (al, bl), (ar, br) = cal["left"], cal["right"]
    slope, c = cal["roller_axis"]
    top_c = c - cal["roller_half_height"] - 10              # the spring too: periodic, so a poor landmark
    poly = np.int32([[-10, top_c], [w + 10, slope * w + top_c], [w + 10, h], [-10, h]])
    cv2.fillPoly(m, [poly], 0)
    # the rails either side of the card are fixed: give them back
    rail = np.zeros_like(m)
    for y in range(int(min(top_c, slope * w + top_c)), h):
        xa = int(al * y + bl) - 14
        xb = int(ar * y + br) + 14
        if xa > 0:
            rail[y, :xa] = 255
        if xb < w:
            rail[y, xb:] = 255
    m |= rail
    return m


def band_of(frame, Hframe, length, np, cv2):
    rect = cv2.warpPerspective(frame, Hframe, (BOOK_W, int(length)), flags=cv2.INTER_LINEAR)
    g = cv2.cvtColor(rect, cv2.COLOR_BGR2GRAY)
    return g[BAND_TOP:BAND_BOTTOM].astype(np.float32)


def build_mosaic(video: Path, cal: dict, out: Path, ref_index: int, step_hint: float | None, np, cv2) -> dict:
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, ref_index)
    ok, ref = cap.read()
    if not ok:
        raise SystemExit("cannot read the reference frame")
    gref = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    mask = fixed_mask(cal, np, cv2)
    orb = cv2.ORB_create(ORB_FEATURES)
    kref, dref = orb.detectAndCompute(gref, mask)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    Hcal = np.array(cal["H"])
    length = cal["length"]
    band_h = BAND_BOTTOM - BAND_TOP
    search = 8                                   # px either way around the predicted position
    fresh = 16                                   # rows at the band's bottom that are new material each frame
    tmpl_h = band_h - fresh

    # the mosaic grows as the book goes by; generous first, trimmed at the end
    cap_rows = int(n * 9 + band_h + 100)
    acc = np.zeros((cap_rows, BOOK_W), np.float64)
    wacc = np.zeros((cap_rows, 1), np.float64)
    wy = (np.hanning(band_h + 2)[1:-1][:, None] * 0.7 + 0.3)

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    last_H = np.eye(3)
    pos = np.zeros(n)
    good = np.zeros(n, bool)
    matched = np.zeros(n, bool)
    dxs = np.zeros(n)
    scores = np.zeros(n)
    split = np.zeros(n)                          # advance of the band's bottom half minus its top half
    inliers_log = []
    recent = []
    nominal = step_hint if step_hint else 6.0

    def lay(i, band, dx):
        y = int(round(pos[i]))
        b = np.roll(band, -dx, axis=1) if dx else band
        acc[y:y + band_h] += b * wy
        wacc[y:y + band_h] += wy

    prev_band = None
    for i in range(n):
        ok, frame = cap.read()
        if not ok:
            n = i
            break
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        k, d = orb.detectAndCompute(g, mask)
        Hs = None
        if d is not None and len(k) > 20:
            m = bf.match(dref, d)
            if len(m) >= 12:
                p0 = np.float32([kref[x.queryIdx].pt for x in m])
                p1 = np.float32([k[x.trainIdx].pt for x in m])
                Hm, inl = cv2.findHomography(p1, p0, cv2.RANSAC, 4.0)
                ninl = int(inl.sum()) if inl is not None else 0
                inliers_log.append(ninl)
                if Hm is not None and ninl >= MIN_INLIERS:
                    Hs = Hm
        if Hs is None:
            Hs = last_H
        else:
            last_H = Hs
            good[i] = True
        band = band_of(frame, Hcal @ Hs, length, np, cv2)
        if i == 0:
            pos[0] = 0.0
            lay(0, band, 0)
            prev_band = band
            continue
        # match the band (less its fresh bottom rows) against the mosaic so far,
        # around where a steady drive predicts it
        pred = pos[i - 1] + nominal
        w0 = max(0, int(round(pred)) - search)
        win = acc[w0:w0 + tmpl_h + 2 * search] / np.maximum(wacc[w0:w0 + tmpl_h + 2 * search], 1e-6)
        tmpl = band[:tmpl_h]
        res = cv2.matchTemplate(win.astype(np.float32), tmpl, cv2.TM_CCOEFF_NORMED)
        _, best, _, (mx, my) = cv2.minMaxLoc(res)
        # sub-pixel in y by a parabola through the peak
        dy = my
        if 0 < my < res.shape[0] - 1:
            a, b, c = res[my - 1, mx], res[my, mx], res[my + 1, mx]
            den = a - 2 * b + c
            if den < 0:
                dy = my + 0.5 * (a - c) / den
        if best > 0.45 and abs(dy - search) < search - 0.5:
            pos[i] = w0 + dy
            matched[i] = True
            dxs[i] = mx                           # the template search is in y only (window as wide as the band)
            scores[i] = best
            # the differential advance: top third against bottom third of the template
            r_top = cv2.matchTemplate(win.astype(np.float32), tmpl[: tmpl_h // 3], cv2.TM_CCOEFF_NORMED)
            r_bot = cv2.matchTemplate(win[2 * tmpl_h // 3:].astype(np.float32), tmpl[2 * tmpl_h // 3:], cv2.TM_CCOEFF_NORMED)
            split[i] = (cv2.minMaxLoc(r_bot)[3][1]) - (cv2.minMaxLoc(r_top)[3][1])
            step = pos[i] - pos[i - 1]
            recent.append(step)
            if len(recent) > 60:
                recent.pop(0)
            if len(recent) >= 10:
                nominal = float(np.median(recent))
        else:
            pos[i] = pos[i - 1] + nominal
        lay(i, band, 0)
        prev_band = band
        if i % 300 == 0:
            print(f"  frame {i}/{n}  inliers {inliers_log[-1] if inliers_log else '-'}  step {nominal:.2f}  match {best:.2f}",
                  file=sys.stderr)
    cap.release()
    total = int(np.ceil(pos[n - 1] + band_h)) + 2
    mosaic = (acc[:total] / np.maximum(wacc[:total], 1e-6)).astype(np.float32)
    np.save(str(out.with_suffix(".mosaic.npy")), mosaic)
    cv2.imwrite(str(out) + ".mosaic.png", np.clip(mosaic, 0, 255).astype(np.uint8))
    steps = np.diff(pos[:n])
    track = {"video": video.name, "fps": fps, "frames": int(n), "ref_frame": ref_index, "book_w": BOOK_W,
             "band": [BAND_TOP, BAND_BOTTOM], "nominal_step_px": float(np.median(steps)),
             "stabilised_frames": int(good.sum()), "matched_frames": int(matched.sum()),
             "median_match": float(np.median(scores[matched])) if matched.any() else 0.0,
             "split_px_median": float(np.median(split[matched])) if matched.any() else 0.0,
             "pos": [round(float(p), 3) for p in pos[:n]],
             "median_inliers": float(np.median(inliers_log)) if inliers_log else 0}
    json.dump(track, open(str(out) + ".track.json", "w"))
    print(f"mosaic {total} x {BOOK_W} px from {n} frames; advance {track['nominal_step_px']:.2f} px/frame "
          f"(min {steps.min():.2f}, max {steps.max():.2f}); {int(good.sum())} frames stabilised, "
          f"{int(matched.sum())} matched (median score {track['median_match']:.2f}); bottom-vs-top advance "
          f"{track['split_px_median']:+.2f} px; median inliers {track['median_inliers']:.0f}", file=sys.stderr)
    return track


# ----------------------------------------------------------------------------
# reading the mosaic
# ----------------------------------------------------------------------------

def fit_pitch(xs, weights, lo: float, hi: float, np):
    """The spacing of the key tracks: fold the hole centres modulo every candidate
    pitch and keep the one whose fold is sharpest. Returns (pitch, phase, sharpness)."""
    best = None
    for pitch in np.arange(lo, hi, 0.01):
        ph = (xs % pitch) / pitch * 2 * np.pi
        c, s = (weights * np.cos(ph)).sum(), (weights * np.sin(ph)).sum()
        r = np.hypot(c, s) / weights.sum()
        if best is None or r > best[2]:
            phase = (np.arctan2(s, c) % (2 * np.pi)) / (2 * np.pi) * pitch
            best = (float(pitch), float(phase), float(r))
    return best


def column_offsets(xs, ys, weights, pitch, total, np, window=4000, step=1000):
    """The lattice's phase down the book: the card wanders a little in the key frame
    and the stabilisation a little more, so the offset is measured per window and
    interpolated. Returns (y_knots, offset_knots)."""
    knots_y, knots_o = [], []
    base = None
    for y0 in range(0, max(1, total - window // 2), step):
        sel = (ys >= y0) & (ys < y0 + window)
        if weights[sel].sum() < 20:
            continue
        ph = (xs[sel] % pitch) / pitch * 2 * np.pi
        c, s = (weights[sel] * np.cos(ph)).sum(), (weights[sel] * np.sin(ph)).sum()
        off = (np.arctan2(s, c) % (2 * np.pi)) / (2 * np.pi) * pitch
        if base is None:
            base = off
        else:                                   # unwrap: stay on the branch nearest the previous knot
            while off - knots_o[-1] > pitch / 2:
                off -= pitch
            while off - knots_o[-1] < -pitch / 2:
                off += pitch
        knots_y.append(y0 + window / 2)
        knots_o.append(off)
    return np.array(knots_y), np.array(knots_o)


def hole_mask(mosaic, np, cv2):
    """Dark rectangles on light card, whatever the lighting did along the book."""
    m = mosaic.astype(np.float32)
    bg = np.maximum(cv2.GaussianBlur(m, (0, 0), 25), 1)
    ratio = m / bg
    mask = (ratio < 0.62).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return mask, ratio


def read_mosaic(out: Path, keys: int, flip: bool, np, cv2) -> dict:
    mosaic = np.load(str(out.with_suffix(".mosaic.npy")))
    track = json.load(open(str(out) + ".track.json"))
    fps, pos = track["fps"], np.array(track["pos"])
    band_top = track["band"][0]
    total, width = mosaic.shape
    mask, ratio = hole_mask(mosaic, np, cv2)
    ncomp, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = np.array([(cents[c][0], cents[c][1], stats[c][2], stats[c][3])
                      for c in range(1, ncomp) if stats[c][4] >= 12 and 3 <= stats[c][2] <= 48 and stats[c][3] >= 3])
    if len(comps) < 20:
        raise SystemExit("too few holes on the mosaic")
    # one key track per hole width, near enough: the pitch is searched around the
    # typical width and must let 49 tracks cover every hole
    xs, ys, ws, hs = comps.T
    weight = np.sqrt(hs)
    wmed = float(np.median(ws))
    narrow = comps[ws <= wmed * 1.3]                       # single-track holes carry the phase
    pitch, phase, sharp = fit_pitch(narrow[:, 0], np.sqrt(narrow[:, 3]), wmed * 0.8, wmed * 1.35, np)
    ky, ko = column_offsets(narrow[:, 0], narrow[:, 1], np.sqrt(narrow[:, 3]), pitch, total, np)
    off_at = (lambda y: np.interp(y, ky, ko)) if len(ky) > 1 else (lambda y: phase + 0 * np.asarray(y))
    # which lattice index is key 0: the leftmost track that is ever used
    lo_x, hi_x = np.percentile(xs, 0.5), np.percentile(xs, 99.5)
    k_first = int(np.floor((lo_x - off_at(float(np.median(ys)))) / pitch + 0.5))
    k_last = int(np.floor((hi_x - off_at(float(np.median(ys)))) / pitch + 0.5))
    used_span = k_last - k_first + 1
    if used_span > keys:
        print(f"warning: holes span {used_span} tracks, more than {keys} keys: the pitch may be wrong", file=sys.stderr)
    # centre the unused tracks: as many spare on the left as on the right
    spare = keys - used_span
    k0 = k_first - spare // 2

    def col_x(k, y):
        return off_at(y) + (k0 + k) * pitch

    # read each key track as runs of dark rows, which keeps chained holes and
    # adjacent-track scales apart whatever the connected components did
    half = max(2, int(round(pitch * 0.3)))
    yy = np.arange(total)
    events = [[] for _ in range(keys)]
    holes_read = 0
    frames = np.arange(len(pos))
    order = np.argsort(pos)

    def time_at(y):
        return float(np.interp(y - band_top, pos[order], frames[order])) / fps

    dark = (mask > 0)
    cs = np.concatenate([np.zeros((total, 1), np.int32), np.cumsum(dark, axis=1, dtype=np.int32)], axis=1)
    for k in range(keys):
        xc = np.rint(off_at(yy) + (k0 + k) * pitch).astype(int)
        x0 = np.clip(xc - half, 0, width - 1)
        x1 = np.clip(xc + half + 1, 1, width)
        # fraction of dark pixels across the track's core, per row
        frac = (cs[yy, x1] - cs[yy, x0]) / np.maximum(x1 - x0, 1)
        on = frac > 0.5
        # runs
        d = np.diff(np.concatenate([[0], on.astype(np.int8), [0]]))
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        kk = keys - 1 - k if flip else k
        for s, e in zip(starts, ends):
            if e - s < 3:
                continue
            events[kk].append([round(time_at(s), 3), round(time_at(e), 3)])
            holes_read += 1
    for k in range(keys):
        events[k].sort()

    stats_rows = []
    for k in range(keys):
        ivs = events[k]
        lens = [e - s for s, e in ivs]
        stats_rows.append({"row": k, "holes": len(ivs),
                           "mean_len_s": round(sum(lens) / len(lens), 3) if lens else None,
                           "max_len_s": round(max(lens), 3) if lens else None,
                           "first_s": round(ivs[0][0], 2) if ivs else None,
                           "last_s": round(ivs[-1][1], 2) if ivs else None})
    json.dump({str(k): v for k, v in enumerate(events)}, open(str(out) + ".events.json", "w"))   # keyed by row, as book_to_organ reads it
    json.dump({"video": track["video"], "fps": fps, "frames": track["frames"], "keys": keys,
               "col_pitch_px": pitch, "fold_sharpness": sharp, "first_track_index": k0,
               "offset_knots": [[float(a), float(b)] for a, b in zip(ky, ko)],
               "flip": flip, "components": int(len(comps)), "holes_read": holes_read,
               "stats": stats_rows}, open(str(out) + ".rows.json", "w"), indent=1)

    # the book picture: the mosaic with the tracks drawn and every hole read outlined
    img = cv2.cvtColor(np.clip(mosaic, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    for k in range(keys):
        pts = np.int32([[round(col_x(k, y)), y] for y in range(0, total, 200)] + [[round(col_x(k, total - 1)), total - 1]])
        cv2.polylines(img, [pts.reshape(-1, 1, 2)], False, (0, 160, 255) if k % 5 else (0, 80, 255), 1)
    for k in range(keys):
        kk = keys - 1 - k if flip else k
        for s, e in events[kk]:
            ys_ = int(np.interp(s * fps, frames, pos)) + band_top
            ye_ = int(np.interp(e * fps, frames, pos)) + band_top
            xk = int(round(col_x(k, (ys_ + ye_) / 2)))
            cv2.rectangle(img, (xk - half - 1, ys_), (xk + half + 1, ye_), (0, 255, 0), 1)
    for s in range(0, int(track["frames"] / fps) + 1, 10):
        y = int(np.interp(s * fps, frames, pos)) + band_top
        cv2.line(img, (0, y), (12, y), (255, 0, 0), 2)
        cv2.putText(img, f"{s}s", (14, y + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 0, 0), 1)
    cv2.imwrite(str(out) + ".book.png", img)

    import mido
    tpb = 480
    mid = mido.MidiFile(type=1, ticks_per_beat=tpb)
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tr.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    msgs = []
    for k in range(keys):
        for s, e in events[k]:
            msgs.append((s, 1, k))
            msgs.append((e, 0, k))
    msgs.sort(key=lambda m: (m[0], m[1]))
    last = 0.0
    for t, is_on, k in msgs:
        dt = int(round((t - last) * 2 * tpb))
        tr.append(mido.Message("note_on" if is_on else "note_off", channel=0, note=k, velocity=100 if is_on else 0, time=dt))
        last = t
    tr.append(mido.MetaMessage("end_of_track", time=0))
    mid.save(str(out) + ".book.mid")
    used = sum(1 for k in range(keys) if events[k])
    print(f"{len(comps)} dark patches, {holes_read} holes read on the tracks; pitch {pitch:.2f} px (fold {sharp:.2f}), "
          f"tracks {k_first - k0}..{k_last - k0} of {keys} used, offset drift {ko.max() - ko.min() if len(ko) else 0:.1f} px "
          f"-> {out}.book.mid / .events.json / .rows.json / .book.png", file=sys.stderr)
    return {"holes": holes_read, "pitch": pitch}


# ----------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("video")
    p.add_argument("--keys", type=int, default=DEFAULT_KEYS)
    p.add_argument("--out", help="output prefix (default: the video's path without extension)")
    p.add_argument("--stage", choices=["calib", "mosaic", "read", "all"], default="all")
    p.add_argument("--ref-frame", type=int, default=900, help="frame the calibration and the stabilisation refer to")
    p.add_argument("--step", type=float, default=None, help="expected advance per frame in rectified px (else measured)")
    p.add_argument("--flip", action="store_true", help="number the rows from the right of the picture")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("needs opencv-python-headless and numpy", file=sys.stderr)
        return 2
    video = Path(a.video)
    out = Path(a.out) if a.out else video.with_suffix("")
    out.parent.mkdir(parents=True, exist_ok=True)
    calib_path = Path(str(out) + ".calib.json")

    if a.stage in ("calib", "all"):
        cap = cv2.VideoCapture(str(video))
        cap.set(cv2.CAP_PROP_POS_FRAMES, a.ref_frame)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            print("cannot read the reference frame", file=sys.stderr)
            return 2
        cal = calibrate(frame, a.keys, np, cv2)
        json.dump(cal, open(calib_path, "w"), indent=1)
        draw_calib(frame, cal, Path(str(out) + ".calib.png"), np, cv2)
        m, c = cal["roller_axis"]
        print(f"roller axis y = {m:+.4f} x + {c:.0f}, half height {cal['roller_half_height']:.0f}, spring x {np.int32(cal['spring_x']).tolist()}; "
              f"travel vanishing point {np.int32(cal['vanishing_point']).tolist()} from {cal['vp_lines']} holes; "
              f"book quad {np.int32(cal['quad']).tolist()} -> {out}.calib.png / .rectified.png", file=sys.stderr)
    if a.stage in ("mosaic", "all"):
        cal = json.load(open(calib_path))
        build_mosaic(video, cal, out, a.ref_frame, a.step, np, cv2)
    if a.stage in ("read", "all"):
        read_mosaic(out, a.keys, a.flip, np, cv2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
