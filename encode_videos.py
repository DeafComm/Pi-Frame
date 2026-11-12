#!/usr/bin/env python3
"""
Convert videos for the Raspberry Pi 4 photo frame. Run this on the Linux Mint
machine, not on the Pi.

    python3 encode_videos.py /path/to/your/videos

Every video in that folder (and its subfolders) is converted into a "fixed"
folder inside it, sorted by when it was recorded:

    fixed/2012/November/clip.mp4

The date comes from the recording date stored in the video, or else from a
date in the file name (like VID_20121103_...). Videos with no trustworthy date
go into fixed/Unsorted; move those into the right fixed/<year>/<Month> folder
yourself. Your original files are never changed. Videos already converted
(anywhere in "fixed", including ones you moved) are skipped, so you can re-run
it after adding new videos.

What it does to each video:
  * H.264 video in an .mp4 file, which the Pi 4 decodes in hardware
  * Every video comes out exactly 1920x1080: the picture is sized to fill the
    screen without changing its shape, and black bars fill the rest. The Pi then
    never has to resize video while playing, which is where it tends to stall.
  * Keeps the shape of the picture (old widescreen/4:3 files stay correct)
  * Removes "comb" lines from old interlaced video, only when it detects them
  * Converts HDR phone video (washed-out colors on normal screens) to regular color
  * Constant frame rate, at most 30 fps (uneven phone/3gp frame rates can stall the Pi)
  * Files made by the older version of this script (not 1920x1080) are redone
  * Evens out the audio volume so all videos play at about the same loudness
  * Stereo AAC audio

Problems are written to encode.log in the folder you converted.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- Settings (safe to change) -------------------------------------------
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
MAX_FPS = 30            # 60 also works on a Pi 4, but 30 is smoother through VLC
QUALITY_CRF = 20        # lower = better quality and bigger files (18-23 is sensible)
SPEED_PRESET = "slow"   # slower = smaller files at the same quality
AUDIO_BITRATE = "160k"
LOUDNESS = -16          # target loudness in LUFS (typical for TV/online video)
TRUE_PEAK = -1.5
LOUDNESS_RANGE = 11
OUTPUT_FOLDER = "fixed"
LOG_NAME = "encode.log"
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mkv", ".avi",
                    ".mpg", ".mpeg", ".wmv", ".flv", ".webm", ".mts", ".m2ts", ".ts", ".vob"}
# ---------------------------------------------------------------------------

UNSORTED = "Unsorted"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTH_NAMES = {m.lower() for m in MONTHS}
TAG_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
                         r"\s*(Z|[+-]\d{2}:?\d{2})?")
NAME_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?"
                          r"(0[1-9]|[12]\d|3[01])(?!\d)")

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")


class EncodeLog:
    def __init__(self, path):
        self.path = path

    def error(self, source, message):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{stamp}  {source.name}\n")
            for line in message.strip().splitlines():
                f.write(f"    {line}\n")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


def ratio(text, default=0.0):
    """Turn '30000/1001' or '4:3' into a number."""
    if not text:
        return default
    parts = re.split(r"[/:]", text)
    try:
        if len(parts) == 2:
            num, den = float(parts[0]), float(parts[1])
            return num / den if den else default
        return float(parts[0])
    except ValueError:
        return default


def probe(path):
    result = run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                  "-of", "json", str(path)])
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not read the file:\n{result.stderr}")
    info = json.loads(result.stdout or "{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise RuntimeError("No video stream found in this file.")
    duration = ratio(info.get("format", {}).get("duration")) or ratio(video.get("duration"))
    return video, audio, duration


def parse_tag_date(text, is_utc):
    """Read a date like 2012-11-03T02:15:00.000000Z. Dates stored in UTC are
    converted to this computer's local time so videos near midnight on the last
    day of a month land in the right month."""
    m = TAG_DATE_RE.search(text or "")
    if not m:
        return None
    year, month, day, hour, minute = map(int, m.groups()[:5])
    try:
        when = datetime(year, month, day, hour, minute, int(m.group(6) or 0))
    except ValueError:
        return None
    if is_utc:
        zone = m.group(7)
        if zone and zone != "Z":
            sign = -1 if zone[0] == "-" else 1
            digits = zone[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            tz = timezone(sign * offset)
        else:
            tz = timezone.utc
        when = when.replace(tzinfo=tz).astimezone().replace(tzinfo=None)
    return when


def believable(when):
    """Reject dates that cameras and old phones write when their clock wasn't set."""
    return when is not None and 1990 <= when.year and when <= datetime.now() + timedelta(days=1)


def recorded_date(path):
    """When the video was recorded, and where that came from. (None, None) if unknown."""
    result = run(["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags",
                  "-of", "json", str(path)])
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    tag_sets = [data.get("format", {}).get("tags", {})]
    tag_sets += [s.get("tags", {}) for s in data.get("streams", [])]
    tag_sets = [{k.lower(): v for k, v in tags.items()} for tags in tag_sets if tags]

    # iPhones store the local date and time it was recorded
    for tags in tag_sets:
        when = parse_tag_date(tags.get("com.apple.quicktime.creationdate"), is_utc=False)
        if believable(when):
            return when, "recording date"
    # Most phones and cameras store the recording time in UTC
    for tags in tag_sets:
        when = parse_tag_date(tags.get("creation_time"), is_utc=True)
        if believable(when):
            return when, "recording date"
    # Phones usually name files by date: VID_20121103_142233.mp4
    m = NAME_DATE_RE.search(path.stem)
    if m:
        when = datetime(int(m.group(1)), int(m.group(2)), min(int(m.group(3)), 28))
        if believable(when):
            return when, "file name"
    return None, None


def rotation_of(video):
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            return int(float(side["rotation"]))
    return int(float(video.get("tags", {}).get("rotate", 0) or 0))


def target_size(video):
    """Size of the picture inside the 1920x1080 frame: square pixels, as large as
    fits without changing its shape, even numbers. The rest is black bars."""
    w, h = int(video["width"]), int(video["height"])
    sar = ratio(video.get("sample_aspect_ratio"), 1.0) or 1.0
    display_w, display_h = w * sar, float(h)
    if abs(rotation_of(video)) % 180 == 90:
        display_w, display_h = display_h, display_w
    scale = min(MAX_WIDTH / display_w, MAX_HEIGHT / display_h)

    def even(x):
        return max(2, int(round(x / 2)) * 2)

    return min(MAX_WIDTH, even(display_w * scale)), min(MAX_HEIGHT, even(display_h * scale))


def target_fps(video):
    """A constant frame rate: the video's own rate, capped at MAX_FPS. Phone and
    3gp video often has an uneven frame rate, which can make the Pi stall."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        text = video.get(key) or ""
        fps = ratio(text)
        if 5 <= fps <= 240:
            return (str(MAX_FPS), MAX_FPS) if fps > MAX_FPS + 0.5 else (text, fps)
    return str(MAX_FPS), MAX_FPS


def is_finished(path):
    """True if an existing output is already in the current format (exactly 1920x1080)."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    result = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)])
    return result.stdout.strip() == f"{MAX_WIDTH},{MAX_HEIGHT}"


def is_interlaced(path):
    """Look at the first 600 frames and decide whether they are interlaced."""
    result = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:v:0",
                  "-vf", "idet", "-frames:v", "600", "-an", "-f", "null", "-"])
    found = re.findall(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)",
                       result.stderr)
    if not found:
        return False
    tff, bff, progressive = map(int, found[-1])
    interlaced = tff + bff
    return interlaced >= 20 and interlaced > progressive


def measure_loudness(path):
    """First pass of two-pass loudness leveling. Returns None if there's nothing to level."""
    af = f"loudnorm=I={LOUDNESS}:TP={TRUE_PEAK}:LRA={LOUDNESS_RANGE}:print_format=json"
    result = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
                  "-af", af, "-vn", "-sn", "-f", "null", "-"])
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr, re.S)
    if not match:
        return None
    data = json.loads(match.group(0))
    try:
        if float(data["input_i"]) == float("-inf"):
            return None   # silent audio
    except (ValueError, KeyError):
        return None
    return data


def has_filter(name):
    result = run(["ffmpeg", "-hide_banner", "-filters"])
    return re.search(rf"^\s*\S+\s+{name}\s", result.stdout, re.M) is not None


def encode(source, dest, tmp, can_tonemap, log):
    video, audio, duration = probe(source)
    width, height = target_size(video)
    fps_text, fps = target_fps(video)
    hdr = video.get("color_transfer") in HDR_TRANSFERS
    interlaced = is_interlaced(source)
    loudness = measure_loudness(source) if audio is not None else None

    notes = [f"picture {width}x{height} in {MAX_WIDTH}x{MAX_HEIGHT}", f"{fps:.3g} fps"]
    filters = []
    if interlaced:
        filters.append("bwdif=mode=send_frame:parity=auto:deint=all")
        notes.append("deinterlaced")
    filters.append(f"fps={fps_text}")          # constant frame rate
    filters.append(f"scale={width}:{height}:flags=lanczos")
    filters.append("setsar=1")
    if hdr:
        if can_tonemap:
            filters.append(TONEMAP)
            notes.append("HDR converted")
        else:
            log.error(source, "HDR video, but this ffmpeg has no 'zscale' filter, so colors "
                              "may look washed out. Install a full ffmpeg build to fix.")
    filters.append("format=yuv420p")
    # Black bars baked in, so every video is exactly screen size and the Pi never
    # has to resize video while playing.
    filters.append(f"pad={MAX_WIDTH}:{MAX_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black")

    level = "4.1" if MAX_FPS <= 30 else "4.2"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats",
           "-i", str(source), "-map", "0:v:0",
           "-vf", ",".join(filters),
           "-c:v", "libx264", "-preset", SPEED_PRESET, "-crf", str(QUALITY_CRF),
           "-profile:v", "high", "-level:v", level, "-pix_fmt", "yuv420p"]
    if hdr and can_tonemap:
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]

    if audio is not None:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ac", "2", "-ar", "48000"]
        if loudness:
            cmd += ["-af",
                    f"loudnorm=I={LOUDNESS}:TP={TRUE_PEAK}:LRA={LOUDNESS_RANGE}"
                    f":measured_I={loudness['input_i']}:measured_TP={loudness['input_tp']}"
                    f":measured_LRA={loudness['input_lra']}:measured_thresh={loudness['input_thresh']}"
                    f":offset={loudness['target_offset']}:linear=true,aresample=48000"]
            notes.append("volume leveled")
    else:
        cmd += ["-an"]
        notes.append("no audio")

    cmd += ["-sn", "-dn", "-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4", str(tmp)]

    print(f"    {', '.join(notes)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    errors = []
    reader = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
    reader.start()

    done, speed = 0.0, ""
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and duration:
            try:
                done = min(100.0, int(value) / 1_000_000 / duration * 100)
            except ValueError:
                pass
        elif key == "speed":
            speed = value.strip()
        elif key == "progress":
            print(f"\r    {done:5.1f}%   speed {speed:<8}", end="", flush=True)
    proc.wait()
    reader.join()
    print()

    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "".join(errors[-20:]))
    tmp.replace(dest)


def find_sources(folder, out_dir):
    """Every video in the folder and its subfolders, except the output folder."""
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and Path(root, d) != out_dir)
        for name in sorted(files):
            if not name.startswith(".") and Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                found.append(Path(root) / name)
    return found


def output_names(sources):
    """Pick a unique output name per source.
    clip.mp4 + clip.3gp            -> clip.mp4, clip_3gp.mp4
    Trip/clip.mp4 + Party/clip.mp4 -> clip_Trip.mp4, clip_Party.mp4"""
    by_stem = {}
    for src in sources:
        by_stem.setdefault(src.stem.lower(), []).append(src)
    bases = {}
    for group in by_stem.values():
        for src in group:
            if len(group) > 1 and src.suffix.lower() != ".mp4":
                bases[src] = f"{src.stem}_{src.suffix.lstrip('.').lower()}"
            else:
                bases[src] = src.stem

    by_base = {}
    for src, base in bases.items():
        by_base.setdefault(base.lower(), []).append(src)
    names, used = {}, set()
    for group in by_base.values():
        for src in group:
            base = bases[src] if len(group) == 1 else f"{bases[src]}_{src.parent.name}"
            name, n = base, 2
            while name.lower() in used:
                name, n = f"{base}_{n}", n + 1
            used.add(name.lower())
            names[src] = f"{name}.mp4"
    return names


def existing_outputs(out_dir):
    """Already converted videos: those in <year>/<Month> or Unsorted folders (where
    the user may have moved them), and loose ones from the older, unsorted version."""
    placed, loose = {}, {}
    for path in out_dir.rglob("*.mp4"):
        if path.name.startswith("."):
            continue
        parts = path.relative_to(out_dir).parts
        if len(parts) == 1:
            loose[path.name.lower()] = path
        elif parts[0] == UNSORTED or (len(parts) >= 3 and re.fullmatch(r"\d{4}", parts[0])
                                      and parts[1].lower() in MONTH_NAMES):
            placed[path.name.lower()] = path
    return placed, loose


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else ".").expanduser().resolve()
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        return 1
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"{tool} is not installed. Install it with: sudo apt install ffmpeg")
            return 1

    out_dir = folder / OUTPUT_FOLDER
    sources = find_sources(folder, out_dir)
    if not sources:
        print(f"No videos found in {folder}")
        return 0

    out_dir.mkdir(exist_ok=True)
    log = EncodeLog(folder / LOG_NAME)
    can_tonemap = has_filter("zscale")
    names = output_names(sources)
    placed, loose = existing_outputs(out_dir)

    converted = skipped = moved = failed = 0
    current_tmp = None
    try:
        for number, src in enumerate(sources, 1):
            name = names[src]
            print(f"[{number}/{len(sources)}] {src.relative_to(folder)}")

            dest = placed.get(name.lower())
            old_copy = loose.get(name.lower())
            if dest is not None:
                if is_finished(dest):
                    print(f"    already converted ({dest.parent.relative_to(out_dir)}), skipping")
                    skipped += 1
                    continue
                print("    converted by an older version of this script, converting again")
            else:
                when, source_of_date = recorded_date(src)
                if when is not None:
                    month = MONTHS[when.month - 1]
                    place = Path(str(when.year), month)
                    print(f"    {month} {when.year} (from the {source_of_date})")
                else:
                    place = Path(UNSORTED)
                    print(f"    no recording date found -> {UNSORTED} (move it by hand)")
                    log.error(src, f"No recording date found, so it was put in "
                                   f"{OUTPUT_FOLDER}/{UNSORTED}. Move it into "
                                   f"{OUTPUT_FOLDER}/<year>/<Month> yourself.")
                dest = out_dir / place / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if old_copy is not None and is_finished(old_copy):
                    old_copy.replace(dest)
                    print(f"    moved the existing converted copy into {place}")
                    moved += 1
                    continue

            current_tmp = dest.parent / f".encoding-{dest.name}"
            started = time.monotonic()
            try:
                encode(src, dest, current_tmp, can_tonemap, log)
                minutes = (time.monotonic() - started) / 60
                print(f"    done in {minutes:.1f} min -> {dest.relative_to(folder)}")
                converted += 1
                if old_copy is not None and old_copy != dest:
                    old_copy.unlink(missing_ok=True)   # outdated copy from the older version
            except Exception as e:
                print(f"\n    FAILED (details in {LOG_NAME})")
                log.error(src, str(e))
                current_tmp.unlink(missing_ok=True)
                failed += 1
            current_tmp = None
    except KeyboardInterrupt:
        if current_tmp is not None:
            current_tmp.unlink(missing_ok=True)
        print("\nStopped. Run the script again to continue where it left off.")
        return 1

    print(f"\nConverted {converted}, moved {moved}, skipped {skipped}, failed {failed}.")
    unsorted = sorted((out_dir / UNSORTED).glob("*.mp4")) if (out_dir / UNSORTED).is_dir() else []
    if unsorted:
        print(f"\n{len(unsorted)} video(s) in {out_dir / UNSORTED} need a date. Move each one")
        print(f"into {out_dir}/<year>/<Month> (for example 2012/November):")
        for path in unsorted:
            print(f"    {path.name}")
    print(f"\nCopy the year folders in {out_dir} into the 'videos' folder on the Pi.")
    if failed:
        print(f"See {folder / LOG_NAME} for what went wrong.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
