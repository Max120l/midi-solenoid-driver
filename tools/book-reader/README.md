# book-reader

Read a cardboard organ book from a book-scan video.

Some enthusiasts publish their books as scrolling scans: the cardboard moves
under a fixed vertical playhead, one row of holes per key, and the holes that
are sounding are drawn in red. That picture *is* the score, so it can be read
exactly, frame by frame, with no listening involved.

```bash
pip install opencv-python-headless numpy mido      # plus yt-dlp to fetch a video
python read_book_video.py valse-brune.webm --keys 49 --out scratch/valse-brune
```

It finds the playhead (the blue line), fits a regular lattice of `--keys`
rows to the hole profile (at video resolution neighbouring holes touch, so
peak-picking would merge rows), and for every frame records which rows are
red at the playhead. It writes:

| File | Contents |
|---|---|
| `PREFIX.book.mid` | one track, MIDI note = row index, 0 at the top, 25 fps timing |
| `PREFIX.events.json` | per row, `[start_s, end_s]` intervals |
| `PREFIX.rows.json` | lattice, playhead, per-row statistics (holes, lengths, first/last) |
| `PREFIX.book.png` | the whole book unrolled, time left to right |
| `PREFIX.overlay.png` | `--check-frame` with the fitted rows drawn — look at it before trusting anything |

Rows are anonymous. The scale — which row is which pipe, drum or register —
turns the row MIDI into an organ-format file; from there `organ_arranger`
does the rest. On the Limonaire 49 the alternating dotted rows are the
snare's two beaters rolling, not chain perforations: `--chain-gap` (joining
holes separated by N frames or fewer) is therefore off by default, and only
for scales that punch sustained notes as rows of short holes.

Tested on "49T Limonaire - LA VALSE BRUNE" (Orchestrophone1904): 540x360 at
25 fps, 49 rows at 6.16 px, 4,485 holes on 47 rows in 197 s.
