#!/usr/bin/env python3
"""
Convert videos for the Raspberry Pi 4 photo frame. Run this on the Linux Mint
machine, not on the Pi.

    python3 encode_videos.py /path/to/your/videos

Every video in that folder is converted into a "fixed" folder inside it.
Your original files are never changed. Files already in "fixed" are skipped,
so you can re-run it after adding new videos.

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
import re
import shutil
import subprocess
import sys
import threading
import time
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


def output_names(sources):
    """Pick an output name per source. If two files share a name (clip.mp4 and
    clip.3gp), the non-mp4 one becomes clip_3gp.mp4 so nothing is overwritten."""
    by_stem = {}
    for src in sources:
        by_stem.setdefault(src.stem.lower(), []).append(src)
    names = {}
    for group in by_stem.values():
        for src in group:
            if len(group) > 1 and src.suffix.lower() != ".mp4":
                names[src] = f"{src.stem}_{src.suffix.lstrip('.').lower()}.mp4"
            else:
                names[src] = f"{src.stem}.mp4"
    return names


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else ".").expanduser().resolve()
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        return 1
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"{tool} is not installed. Install it with: sudo apt install ffmpeg")
            return 1

    sources = sorted(p for p in folder.iterdir()
                     if p.is_file() and not p.name.startswith(".")
                     and p.suffix.lower() in VIDEO_EXTENSIONS)
    if not sources:
        print(f"No videos found in {folder}")
        return 0

    out_dir = folder / OUTPUT_FOLDER
    out_dir.mkdir(exist_ok=True)
    log = EncodeLog(folder / LOG_NAME)
    can_tonemap = has_filter("zscale")
    names = output_names(sources)

    converted = skipped = failed = 0
    current_tmp = None
    try:
        for number, src in enumerate(sources, 1):
            dest = out_dir / names[src]
            print(f"[{number}/{len(sources)}] {src.name}")
            if is_finished(dest):
                print("    already converted, skipping")
                skipped += 1
                continue
            if dest.exists():
                print("    converted by the older version of this script, converting again")
            current_tmp = out_dir / f".encoding-{dest.name}"
            started = time.monotonic()
            try:
                encode(src, dest, current_tmp, can_tonemap, log)
                minutes = (time.monotonic() - started) / 60
                print(f"    done in {minutes:.1f} min -> {OUTPUT_FOLDER}/{dest.name}")
                converted += 1
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

    print(f"\nConverted {converted}, skipped {skipped}, failed {failed}.")
    print(f"Copy the files in {out_dir} into the 'videos' folder on the Pi.")
    if failed:
        print(f"See {folder / LOG_NAME} for what went wrong.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
