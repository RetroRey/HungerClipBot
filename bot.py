"""
Daily Twitch clip compilation -> YouTube uploader (Hunger category only).

Steps:
  1. Get the top Twitch clips for a game from the last N hours (Twitch Helix API)
  2. Download each clip (yt-dlp)
  3. Re-encode each to the same format with the streamer's name burned in (ffmpeg)
  4. Stitch them together
  5. Upload to YouTube with a credits list in the description (YouTube Data API)

Required environment variables (set as GitHub Actions secrets):
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN

Usage:
  python bot.py                 # full run: fetch, build, upload
  python bot.py --no-upload     # build the video only (out/compilation.mp4)
"""

import argparse
import datetime as dt
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
WORK = ROOT / "work"
OUT = ROOT / "out"
FONT = ROOT / "fonts" / "DejaVuSans-Bold.ttf"


# --------------------------------------------------------------------------- #
# Config / helpers
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing environment variable: {name}")
    return val


def run(cmd: list[str]) -> None:
    print("  $", " ".join(str(c) for c in cmd[:6]), "..." if len(cmd) > 6 else "")
    subprocess.run(cmd, check=True)


# --------------------------------------------------------------------------- #
# 1. Twitch
# --------------------------------------------------------------------------- #
def twitch_token(client_id: str, client_secret: str) -> str:
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def twitch_get(path: str, params: dict, client_id: str, token: str) -> dict:
    r = requests.get(
        f"https://api.twitch.tv/helix/{path}",
        params=params,
        headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def twitch_clips(params: dict, client_id: str, token: str, pages: int = 3) -> list[dict]:
    """Fetch up to pages*100 clips matching params."""
    clips, cursor = [], None
    for _ in range(pages):
        p = dict(params, first=100)
        if cursor:
            p["after"] = cursor
        page = twitch_get("clips", p, client_id, token)
        clips.extend(page["data"])
        cursor = page.get("pagination", {}).get("cursor")
        if not cursor:
            break
    return clips


def select_clips(game_clips: list[dict], featured: list[dict], featured_id: str | None,
                 cfg: dict) -> list[dict]:
    """
    Pick the clips for today's video.
      1. Pick the top clips normally, by view count. The featured channel competes
         like everyone else here, so its clips can get in on their own merit.
      2. Then add `featured_clips` RANDOM clips from the featured channel's clips in
         this category, on top of any of theirs that made it in normally. The random
         pick never repeats a clip that is already in the video. (No language / view
         filters - the guaranteed clip always goes in.) To make room, the
         lowest-viewed normal clips from other streamers are dropped if needed.
      3. The guaranteed clip is placed at a random spot in the video (never first,
         so the video title still comes from the day's top clip). Clips that made
         it in normally stay in their view-count position.
    """
    by_views = lambda c: c["view_count"]
    picked, per_streamer, total = [], {}, 0.0

    for c in sorted(game_clips, key=by_views, reverse=True):
        if len(picked) >= cfg["max_clips"]:
            break
        if cfg.get("language") and c.get("language") != cfg["language"]:
            continue
        if c["view_count"] < cfg.get("min_views", 0):
            continue
        if per_streamer.get(c["broadcaster_id"], 0) >= cfg["max_clips_per_streamer"]:
            continue
        if total + c["duration"] > cfg["max_total_seconds"]:
            continue
        picked.append(c)
        per_streamer[c["broadcaster_id"]] = per_streamer.get(c["broadcaster_id"], 0) + 1
        total += c["duration"]

    if not featured_id:
        return picked

    # Never pick a clip that's already in the video (same clip ID)
    seen = {c["id"] for c in picked}
    pool = [c for c in featured if c["id"] not in seen]
    extras = random.sample(pool, min(cfg["featured_clips"], len(pool)))

    for extra in extras:
        # Make room by dropping the lowest-viewed clip that isn't the featured channel's
        while True:
            over_count = len(picked) + 1 > cfg["max_clips"]
            over_time = sum(c["duration"] for c in picked) + extra["duration"] > cfg["max_total_seconds"]
            if not (over_count or over_time):
                break
            droppable = [c for c in picked if c["broadcaster_id"] != featured_id]
            if not droppable:
                break
            picked.remove(min(droppable, key=by_views))
        pos = random.randint(1, len(picked)) if picked else 0
        picked.insert(pos, extra)

    return picked


def enough_footage(n_clips: int, seconds: float, cfg: dict) -> bool:
    """At least min_clips different clips OR at least min_total_seconds of footage."""
    return n_clips >= cfg["min_clips"] or seconds >= cfg["min_total_seconds"]


def fetch_top_clips(cfg: dict) -> list[dict]:
    client_id = env("TWITCH_CLIENT_ID")
    token = twitch_token(client_id, env("TWITCH_CLIENT_SECRET"))

    games = twitch_get("games", {"name": cfg["game_name"]}, client_id, token)["data"]
    if not games:
        sys.exit(f"Twitch category not found: {cfg['game_name']!r} (check exact spelling)")
    game_id = games[0]["id"]

    now = dt.datetime.now(dt.timezone.utc)
    started = now - dt.timedelta(hours=cfg["lookback_hours"])
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    window = {"started_at": started.strftime(fmt), "ended_at": now.strftime(fmt)}

    game_clips = twitch_clips({"game_id": game_id, **window}, client_id, token)

    # Featured channel: ask Twitch for that channel's clips directly (so a smaller
    # channel isn't missed), then keep only the ones in this category.
    featured, featured_id = [], None
    fc = (cfg.get("featured_channel") or "").strip().lower()
    if fc and fc != "change_me":
        users = twitch_get("users", {"login": fc}, client_id, token)["data"]
        if not users:
            print(f"  ! featured channel {fc!r} not found on Twitch - skipping it")
        else:
            featured_id = users[0]["id"]
            featured = [
                c for c in twitch_clips({"broadcaster_id": featured_id, **window},
                                        client_id, token, pages=1)
                if c["game_id"] == game_id
            ]
            print(f"Featured channel {fc}: {len(featured)} {cfg['game_name']} clip(s) today.")

    picked = select_clips(game_clips, featured, featured_id, cfg)

    # Look up channel logins so the credit links in the description are correct
    if picked:
        ids = list({c["broadcaster_id"] for c in picked})
        users = twitch_get("users", {"id": ids}, client_id, token)["data"]
        logins = {u["id"]: u["login"] for u in users}
        for c in picked:
            c["broadcaster_login"] = logins.get(c["broadcaster_id"], c["broadcaster_name"].lower())

    total = sum(c["duration"] for c in picked)
    print(f"Picked {len(picked)} clips ({total:.0f}s) out of {len(game_clips)} found.")
    return picked


# --------------------------------------------------------------------------- #
# 2. Download
# --------------------------------------------------------------------------- #
def download_clip(clip: dict, dest: Path) -> Path | None:
    out = dest / f"{clip['id']}.mp4"
    try:
        run([
            sys.executable, "-m", "yt_dlp",
            "-q", "--no-warnings",
            "-f", "best[ext=mp4]/best",
            "-o", str(out),
            clip["url"],
        ])
    except subprocess.CalledProcessError:
        print(f"  ! download failed, skipping {clip['url']}")
        return None
    return out if out.exists() else None


# --------------------------------------------------------------------------- #
# 3. Normalize + caption
# --------------------------------------------------------------------------- #
CAPTION_POS = {
    "bottom-left":  ("40", "h-th-60"),
    "bottom-right": ("w-tw-40", "h-th-60"),
    "top-left":     ("40", "40"),
    "top-right":    ("w-tw-40", "40"),
}


def has_audio(path: Path) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def normalize_clip(src: Path, name: str, idx: int, cfg: dict, dest: Path) -> Path:
    """Re-encode to identical size/fps/codec and burn in the streamer's name."""
    w, h, fps = cfg["width"], cfg["height"], cfg["fps"]
    x, y = CAPTION_POS.get(cfg["caption_position"], CAPTION_POS["bottom-left"])

    # Write caption to a file so names with special characters don't break ffmpeg
    caption_file = dest / f"caption_{idx}.txt"
    caption_file.write_text(name, encoding="utf-8")

    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps},"
        f"drawtext=fontfile='{FONT}':textfile='{caption_file}':expansion=none:"
        f"fontsize={cfg['caption_font_size']}:fontcolor=white:"
        f"box=1:boxcolor=0x9146FF@0.85:boxborderw=18:"
        f"x={x}:y={y}"
    )

    out = dest / f"norm_{idx:03d}.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if not has_audio(src):
        # add a silent track so every segment has audio (concat needs it)
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
    cmd += [
        "-vf", vf,
        "-map", "0:v:0", "-map", "0:a:0" if has_audio(src) else "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(out),
    ]
    run(cmd)
    return out


# --------------------------------------------------------------------------- #
# 4. Stitch
# --------------------------------------------------------------------------- #
def stitch(parts: list[Path], out: Path) -> Path:
    list_file = out.parent / "concat.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ])
    return out


# --------------------------------------------------------------------------- #
# 5. YouTube upload
# --------------------------------------------------------------------------- #
EPISODE_FILE = ROOT / "episode_number.txt"


def read_episode_number() -> int:
    """The number the NEXT uploaded video will get. Starts at 1."""
    try:
        return max(int(EPISODE_FILE.read_text().strip()), 1)
    except (FileNotFoundError, ValueError):
        return 1


def make_title(template: str, game: str, date: str, first_clip: str, number: int) -> str:
    """
    Fill in the title template. {first_clip} is the Twitch title of the first clip
    in the video, {number} is the episode number. If the result is over YouTube's
    100-character limit, the clip title is shortened (with ...) so the rest of the
    title (e.g. "- Hunger Highlights #12") always stays intact.
    """
    # YouTube rejects titles containing < or >
    first_clip = " ".join(first_clip.replace("<", "").replace(">", "").split())
    if not first_clip:
        first_clip = f"Best {game} Moments"

    fill = dict(game=game, date=date, number=number)
    room = 100 - len(template.format(first_clip="", **fill))
    if len(first_clip) > room:
        first_clip = first_clip[: max(room - 3, 0)].rstrip() + "..."
    title = template.format(first_clip=first_clip, **fill)
    return title.replace("<", "").replace(">", "")[:100]


def build_metadata(cfg: dict, clips: list[dict], durations: list[float], number: int) -> dict:
    date = dt.datetime.now(dt.timezone.utc).strftime("%b %d, %Y")
    game = cfg["game_name"]
    title = make_title(cfg["title_template"], game, date, clips[0].get("title", ""), number)

    lines = [cfg["description_header"].format(game=game).strip(), "", "Clips:"]
    t = 0.0
    for c, d in zip(clips, durations):
        stamp = f"{int(t // 60)}:{int(t % 60):02d}"
        lines.append(f"{stamp} {c['broadcaster_name']} - https://twitch.tv/{c['broadcaster_login']}")
        t += d
    # first chapter must start at 0:00 for YouTube chapters to work - it does
    description = "\n".join(lines)[:4900]

    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": cfg["tags"] + [game],
            "categoryId": cfg["category_id"],
        },
        "status": {
            "privacyStatus": cfg["privacy_status"],
            "selfDeclaredMadeForKids": cfg["made_for_kids"],
        },
    }


def upload_to_youtube(video: Path, metadata: dict) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=env("YT_REFRESH_TOKEN"),
        client_id=env("YT_CLIENT_ID"),
        client_secret=env("YT_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    req = yt.videos().insert(
        part="snippet,status",
        body=metadata,
        media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True),
    )
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"  upload {int(status.progress() * 100)}%")
    vid = resp["id"]
    print(f"Uploaded: https://youtu.be/{vid}")
    return vid


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def duration_of(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--no-upload", action="store_true", help="build the video but don't upload")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    OUT.mkdir(exist_ok=True)

    rule = f"{cfg['min_clips']} clips or {cfg['min_total_seconds'] // 60} min of footage"

    clips = fetch_top_clips(cfg)
    if not enough_footage(len(clips), sum(c["duration"] for c in clips), cfg):
        print(f"SKIPPED: only {len(clips)} clips / "
              f"{sum(c['duration'] for c in clips):.0f}s available - need {rule}. No video today.")
        return

    used, parts, durations = [], [], []
    for i, clip in enumerate(clips):
        print(f"[{i + 1}/{len(clips)}] {clip['broadcaster_name']}: {clip['title']}")
        raw = download_clip(clip, WORK)
        if not raw:
            continue
        # Display names can be in scripts the font can't draw (e.g. Japanese);
        # fall back to the channel's login name, which is always plain ASCII.
        name = clip["broadcaster_name"]
        if not name.isascii():
            name = clip["broadcaster_login"]
        part = normalize_clip(raw, name, i, cfg, WORK)
        parts.append(part)
        durations.append(duration_of(part))
        used.append(clip)

    # Re-check with what actually downloaded (a failed download can drop us below the bar)
    if not enough_footage(len(parts), sum(durations), cfg):
        print(f"SKIPPED: only {len(parts)} clips / {sum(durations):.0f}s downloaded "
              f"successfully - need {rule}. No video today.")
        return

    final = stitch(parts, OUT / "compilation.mp4")
    number = read_episode_number()
    meta = build_metadata(cfg, used, durations, number)
    (OUT / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Built {final} ({sum(durations):.0f}s, {len(parts)} clips)")
    print(f"Title: {meta['snippet']['title']}")

    if args.no_upload:
        print("--no-upload set, skipping YouTube (episode number not used up).")
        return
    upload_to_youtube(final, meta)

    # Only count the number as used once the upload actually succeeded.
    # The GitHub workflow saves this file back to the repo after each run.
    EPISODE_FILE.write_text(f"{number + 1}\n")
    print(f"Next video will be #{number + 1}")


if __name__ == "__main__":
    main()
