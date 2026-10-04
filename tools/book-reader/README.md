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
does the rest. On the Limonaire 49 books read so far the dotted rows were
fast repeated melody notes, not chain perforations: `--chain-gap` (joining
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

## Handheld footage: the real book going into the key frame

The scans above are the easy case. `read_book_handheld.py` reads the other
kind of video: a camera held over the organ while the cardboard slides under
the pressure roller, the picture tilted and shaking, the holes real. It works
in stages, each leaving a picture to check:

```bash
python read_book_handheld.py burlesque.mp4 --keys 49 --out scratch/burlesque/out/burlesque --stage all
```

1. **calib** finds the roller on a reference frame by the periodicity of its
   coils, the spring's ends along it, and the travel direction from where the
   long holes point (their lines meet at the vanishing point); from those it
   builds the homography that straightens the book plane. Check
   `PREFIX.calib.png` (the quad on the frame) and `PREFIX.rectified.png`
   (the holes must stand in vertical columns).
2. **mosaic** stabilises every frame against the fixed woodwork (ORB features
   matched to the reference frame, the moving card masked out), straightens
   a band of card just below the roller, matches it against the mosaic built
   so far to measure the book's advance, and lays it in. `PREFIX.mosaic.png`
   is the whole book; `PREFIX.track.json` maps every frame to its position.
3. **read** finds the dark patches, fits the pitch of the key tracks from the
   fold of their centres, lets the lattice's phase drift slowly down the book
   (the card wanders in the key frame), and reads every track as runs of dark
   rows, so chained holes and scales in neighbouring tracks stay apart. A
   hole's time is the frame in which it passed the reading band, so the
   book's speed never has to be known. It writes the same `.events.json`,
   `.book.mid`, `.rows.json` and `.book.png` as the scan reader; rows are
   numbered from the left of the picture (`--flip` from the right).

Then `book_to_organ.py` as for the scans. The two tracks at the picture's
left margin may lie in the guide's shadow and read as holes seconds long;
compare the rows' total on-time with the piece before trusting them.

On "LIMONAIRE 49 Touches - Marche Burlesque" (Orchestrophone1904, 1080p30,
5,926 frames): 5,399 frames matched the mosaic at a median score of 0.90,
pitch 11.97 px, 3,863 holes read, 3,688 notes after dropping the two shadowed
tracks. The video itself stays out of the repository.

## Which way round: ask the recording

`book_audio_order.py` settles whether row 0 is key 1 or key 49, and what the
recorded organ's transposition is, by correlating every pitched row's on/off
pattern with the energy at the pitch the scale sheet gives that key, for both
orders and a range of transpositions and tunings:

```bash
python book_audio_order.py out/burlesque.events.json --audio burlesque.m4a --scale limonaire49_book_scale.xlsx
```

PyAV decodes the audio, so the video file itself will do. The sound lags the
reading line by a constant (the key frame is some way past the roller), which
is measured from the onsets first. For Marche Burlesque: row 0 = key 49, the
organ eight semitones above the sheet's nominal pitches and a quarter tone
flat, the right order scoring 70 against 29 for the wrong one.
`limonaire49_book_scale.xlsx` is the sheet for this scale.
