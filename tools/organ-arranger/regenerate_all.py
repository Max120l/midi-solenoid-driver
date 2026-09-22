#!/usr/bin/env python3
"""Regenerate every planned arrangement from the source files, wherever they are.

The plans in tunes/ are committed; the source .mid files never are. This finds,
for each plan, the source whose track names carry every source the plan names,
then runs the three steps -- transcribe, arrange, preview -- into one folder per
tune:

    python regenerate_all.py --sources ~/Downloads --out ~/Music/Pellevoisin
    python regenerate_all.py --sources ~/Downloads --out ~/Music/Pellevoisin --only mule star-trek-tos

Several files may fit a plan (a roll maker's template names every track the
same, a "(1)" download copy sits beside the original). The file named like the
plan wins, then the one long enough for the plan's own from/until marks, then
the shorter name; the report lists the runners-up so a wrong pick is visible.
`--pick tune=path` overrides.
Nothing is written for a plan whose source is not found; the report says so.
"""
import argparse
import pathlib
import subprocess
import sys

import mido
import yaml

HERE = pathlib.Path(__file__).resolve().parent
ORGAN = HERE / "instrument" / "organ.yaml"
SKIP = (".organ.mid", ".preview.mid", ".fororgan.mid", ".book.mid")


def source_keys(path: pathlib.Path) -> set[str] | None:
    """The 'Name#index[/chN]' keys organ_transcribe gives a file's tracks."""
    try:
        mid = mido.MidiFile(str(path))
    except Exception:
        return None
    keys = set()
    for index, track in enumerate(mid.tracks):
        channels = {m.channel for m in track if m.type == "note_on" and m.velocity > 0}
        if not channels:
            continue
        name = "".join(ch for ch in track.name if ch.isprintable() and ch != "�").strip() or f"Track {index + 1}"
        split = len(channels) > 1
        for ch in channels:
            keys.add(f"{name}#{index}" + (f"/ch{ch + 1}" if split else ""))
    return keys


def plan_needs(plan: pathlib.Path) -> tuple[set[str], float]:
    """The sources a plan names, and the latest time mark it uses (0 if none)."""
    d = yaml.safe_load(plan.read_text(encoding="utf-8")) or {}
    srcs, marks = set(), [0.0]

    def walk(x):
        if isinstance(x, dict):
            if x.get("source"):
                srcs.add(str(x["source"]))
            for k, v in x.items():
                if k in ("from", "until") and isinstance(v, (int, float)):
                    marks.append(float(v))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(d)
    return srcs, max(marks)


def name_score(tune: str, path: pathlib.Path) -> int:
    """How many of the plan's name words the file's name carries: 'over-the-waves'
    against 'Over the Waves.mid' is 3, against 'Snow Waltz.mid' is 0."""
    stem = "".join(ch if ch.isalnum() else " " for ch in path.stem.lower()).split()
    return sum(1 for w in tune.split("-") if w in stem)


def length(path: pathlib.Path) -> float:
    try:
        return mido.MidiFile(str(path)).length
    except Exception:
        return 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sources", type=pathlib.Path, action="append", required=True, help="folder(s) searched recursively for the source .mid files")
    ap.add_argument("--out", type=pathlib.Path, required=True, help="one folder per tune is made here")
    ap.add_argument("--plans", type=pathlib.Path, default=HERE / "tunes")
    ap.add_argument("--organ", type=pathlib.Path, default=ORGAN)
    ap.add_argument("--only", nargs="*", default=None, help="tune names (plan file names without .plan.yaml)")
    ap.add_argument("--pick", action="append", default=[], metavar="TUNE=PATH", help="use this source for this tune")
    ap.add_argument("--dry-run", action="store_true", help="report the matches, write nothing")
    a = ap.parse_args(argv)
    picks = {p.split("=", 1)[0]: pathlib.Path(p.split("=", 1)[1]) for p in a.pick}

    candidates: dict[pathlib.Path, set[str]] = {}
    for d in a.sources:
        for f in sorted(d.expanduser().rglob("*")):
            if f.suffix.lower() not in (".mid", ".midi") or f.name.lower().endswith(SKIP) or f.name.lower().startswith("try_"):
                continue
            keys = source_keys(f)
            if keys:
                candidates[f] = keys

    py = sys.executable
    failures = 0
    for plan in sorted(a.plans.glob("*.plan.yaml")):
        tune = plan.name[: -len(".plan.yaml")]
        if a.only is not None and tune not in a.only:
            continue
        want, last_mark = plan_needs(plan)
        if tune in picks:
            hits = [picks[tune]]
        else:
            hits = [f for f, keys in candidates.items() if want and want <= keys]
            # the file named like the plan first; then a file shorter than the
            # plan's last time mark cannot be the one; then the shorter name
            hits.sort(key=lambda f: (-name_score(tune, f), 0 if length(f) >= last_mark else 1, len(f.name), str(f)))
        if not hits:
            print(f"{tune:32} NO SOURCE: needs {sorted(want)}")
            failures += 1
            continue
        src = hits[0]
        others = "" if len(hits) < 2 else "   (also fits: " + ", ".join(h.name for h in hits[1:4]) + ")"
        print(f"{tune:32} <- {src.name}{others}")
        if a.dry_run:
            continue
        dst = a.out.expanduser() / tune
        dst.mkdir(parents=True, exist_ok=True)
        fororgan = dst / f"{tune}.fororgan.mid"
        steps = [
            [py, str(HERE / "organ_transcribe.py"), str(src), "--organ", str(a.organ), "--plan", str(plan), "-o", str(fororgan), "-q"],
            [py, str(HERE / "organ_arranger.py"), str(fororgan), "--organ", str(a.organ), "-o", str(dst / f"{tune}.organ.mid"), "-q"],
            [py, str(HERE / "preview_gm.py"), str(fororgan), "--organ", str(a.organ), "-o", str(dst / f"{tune}.preview.mid")],
        ]
        for cmd in steps:
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(HERE))
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()[-2:]
                print(f"{'':32}    FAILED in {pathlib.Path(cmd[1]).name}: {' | '.join(tail)}")
                failures += 1
                break
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
