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

## From rows to the organ

`book_to_organ.py` turns the row events into the organ-format score, given
the layout sheet with a `book scale` column (which book key drives each
solenoid) and which key the top row is:

```bash
python book_to_organ.py valse-brune.events.json --scale limonaire49t_scale.xlsx --top-key 49 -o valse-brune.fororgan.mid
python ../organ-arranger/organ_arranger.py valse-brune.fororgan.mid --organ ../organ-arranger/instrument/organ.yaml
```

Finding the row order without guessing: decode the video's audio, correlate
each row's on/off pattern with the energy at every pitch, and score the
candidate orders against the sheet for every transposition. For the
Orchestrophone1904 scans the top row is key 49, keys descend down the
picture, and that organ sounds a fifth above this sheet's nominal notes
(it is also about a quarter tone flat of A440). The three drum keys came out
empty on La Valse Brune: that book, as scanned, has no percussion, and the
dotted rows are fast repeated melody notes.
