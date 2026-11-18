r"""Clipper output -> posting queue. Run every 5 minutes (install-tasks.ps1 sets this up). No AI tokens used.

    python clip_feeder.py            # feed
    python clip_feeder.py --dry      # show what would be scheduled, change nothing

What it does
  1. Reads finished clips (an .mp4 plus its .json) from the GTA Auto Clipper output folder.
  2. Skips clips that are tests, not gameplay-confirmed, too old, or vetoed.
  3. Gives each clip (best score first) to the channel that has been fed the FEWEST clips so far,
     so every channel gets an equal share, on that channel's next free slot of its own posting grid.
  4. Writes the title/description/caption and copies the clip to queue/<channel>/ready/ with an exact
     "when". autopost.py does the upload.

Veto: an agent (or you) creates output/<clip name>.bad containing one line why. A vetoed clip is never
fed, and is pulled back out of ready/ if it has not been posted yet.

State: queue/clip_feeder.json   Log: queue/clip_feeder.log
"""
import datetime as dt, glob, math, os, shutil, sys, time
from pathlib import Path

from common import Q, load_config, make_logger, read_json, tzinfo, write_json

DRY = "--dry" in sys.argv
MIN_AGE = 60   # seconds: the clipper may still be writing the file


def grid_slots(now, yt_per_hour, lookahead):
    """Yield (time, k) for the channel's posting grid: one slot every 60/yt_per_hour minutes from local midnight."""
    step = 60 / yt_per_hour
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    earliest = now + dt.timedelta(minutes=12)          # never schedule closer than 12 minutes ahead
    k = max(0, math.ceil((earliest - midnight).total_seconds() / 60 / step))
    while True:
        t = (midnight + dt.timedelta(minutes=k * step)).replace(second=0, microsecond=0)
        if t > now + lookahead:
            return
        yield t, k
        k += 1


def taken(chan):
    """Minutes already used on this channel (scheduled/ and ready/)."""
    used = set()
    for sub, key in (("scheduled", "post_at"), ("ready", "when"), ("ready", "post_at")):
        for fn in glob.glob(str(Q / chan / sub / "*.json")):
            try:
                v = read_json(fn).get(key)
                if v and v != "now":
                    used.add(dt.datetime.fromisoformat(v).replace(second=0, microsecond=0))
            except Exception:
                pass
    return used


def next_slot(ch, used, now, lookahead):
    """First free grid slot for this channel: returns (time, instagram_too) or None."""
    yt = ch.get("youtube_per_hour", 2)
    ig = ch.get("instagram_per_hour", 0)
    every_ig = max(1, round(yt / ig)) if ig else 0
    for t, k in grid_slots(now, yt, lookahead):
        if t.replace(tzinfo=None) not in {u.replace(tzinfo=None) for u in used}:
            return t, bool(every_ig) and k % every_ig == 0
    return None


def make_meta(cfg, clip, when, ig_ok, n):
    fd = cfg.get("feeder", {})
    titles = fd.get("titles") or ["Check this out"]
    tags = fd.get("hashtags") or ["#shorts"]
    title = titles[n % len(titles)]
    credit = clip.get("credit") or clip.get("streamer") or "the streamer"
    src = clip.get("source_url") or ""
    credit_line = f"{fd.get('credit_label', 'Gameplay')}: {credit} (live) {src}".strip()
    site = fd.get("site_line", "")
    return {
        "title": f"{title} {tags[0]}"[:95],
        "youtube_description": f"{title}\n\n{credit_line}\n\n{site}\n\n" + " ".join(tags),
        "instagram_caption": f"{title}\n\n{credit_line}\n\n" + " ".join(tags),
        "hashtags": tags, "when": when.isoformat(),
        "platforms": ["youtube", "instagram"] if ig_ok else ["youtube"],
        "source": "clip_feeder", "clip": clip.get("video"), "credit": credit, "source_url": src,
    }


def main():
    cfg = load_config()
    tz = tzinfo(cfg)
    log = make_logger(tz, "clip_feeder.log")
    fd = cfg.get("feeder", {})
    src_dir = Path(fd["clipper_output"])
    feed_since = dt.datetime.fromisoformat(fd.get("feed_since", "2000-01-01T00:00")).replace(tzinfo=tz)
    lookahead = dt.timedelta(hours=fd.get("lookahead_hours", 2))
    min_score = fd.get("min_score", 0)
    now = dt.datetime.now(tz)
    channels = cfg["channels"]
    Q.mkdir(exist_ok=True)
    statef = Q / "clip_feeder.json"
    state = read_json(statef) if statef.exists() else {"fed": {}, "n": 0}
    lock = Q / "clip_feeder.lock"
    if not DRY:
        if lock.exists() and time.time() - lock.stat().st_mtime < 1800:
            return
        lock.write_text(str(os.getpid()))
    try:
        for bad in glob.glob(str(src_dir / "*.bad")):      # vetoes
            name = Path(bad).stem
            q = state["fed"].get(name)
            if q and q not in ("failed", "vetoed"):
                ch = q.split("-clip-")[0]
                for ext in (".json", ".mp4"):
                    f = Q / ch / "ready" / f"{q}{ext}"
                    if f.exists():
                        if not DRY:
                            f.unlink()
                        log(f"{name}: vetoed, removed from {ch}/ready")
            state["fed"][name] = "vetoed"

        clips = []
        for j in glob.glob(str(src_dir / "*.json")):
            name = Path(j).stem
            mp4 = src_dir / f"{name}.mp4"
            if name in state["fed"] or not mp4.exists() or time.time() - mp4.stat().st_mtime < MIN_AGE:
                continue
            if dt.datetime.fromtimestamp(mp4.stat().st_mtime, tz) < feed_since:
                continue
            try:
                c = read_json(j)
            except Exception:
                continue
            if c.get("test") or not c.get("gameplay_confirmed") or (c.get("score") or 0) < min_score:
                continue
            clips.append((c.get("score") or 0, name, c))
        clips.sort(key=lambda x: -x[0])                     # best first

        used = {ch: taken(ch) for ch in channels}
        for score, name, c in clips:
            fed_n = {ch: sum(1 for v in state["fed"].values() if isinstance(v, str) and v.startswith(ch + "-clip-"))
                     for ch in channels}
            options = [(s, ch) for ch in channels if (s := next_slot(channels[ch], used[ch], now, lookahead))]
            if not options:
                continue                                     # every channel is full up to the lookahead; wait for the next run
            (when, ig_ok), chan = min(options, key=lambda o: (fed_n[o[1]], o[0][0]))   # fewest fed first = equal share
            used[chan].add(when)
            qname = f"{chan}-clip-{name}"
            meta = make_meta(cfg, c, when, ig_ok, state["n"])
            log(f"{name} (score {score}) -> {chan} {when:%a %H:%M} {'YT+IG' if ig_ok else 'YT'}")
            if DRY:
                continue
            rdy = Q / chan / "ready"; rdy.mkdir(parents=True, exist_ok=True)
            work = Q / chan / "feeding"; work.mkdir(exist_ok=True)
            tmp = work / f"{qname}.mp4"                      # built outside ready/ so autopost never sees a half-written file
            try:
                shutil.copy2(src_dir / f"{name}.mp4", tmp)
            except Exception as e:
                log(f"{name}: FAILED {type(e).__name__}: {str(e)[:200]}")
                tmp.unlink(missing_ok=True)
                state["fed"][name] = "failed"
                continue
            write_json(rdy / f"{qname}.json", meta)
            os.replace(tmp, rdy / f"{qname}.mp4")
            os.utime(rdy / f"{qname}.mp4", (time.time() - 300, time.time() - 300))   # autopost's "still copying" guard
            state["fed"][name] = qname
            state["n"] += 1
            write_json(statef, state)
        if not DRY:
            write_json(statef, state)
    finally:
        if not DRY and lock.exists():
            lock.unlink()


if __name__ == "__main__":
    main()
