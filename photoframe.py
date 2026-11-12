#!/usr/bin/env python3
"""
Raspberry Pi photo frame - network share version.

Shows the current month's photos from a network share (read-only) together
with the current month's videos from the SD card, one year at a time:
every November from the oldest year to the newest, then back to the oldest.
Within a year, photos and videos are shown in random order.

Photos on the share:  <share>/<year>/<Month>/...   e.g. 2012/November
Videos on the SD card: videos/<year>/<Month>/...   e.g. videos/2012/November

Settings are read from photoframe.conf and errors are written to
photoframe.log, both in the same folder as this script.
Start and stop it over SSH with start_frame.sh / stop_frame.sh.
"""

import configparser
import logging
import logging.handlers
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "photoframe.conf"
LOG_FILE = SCRIPT_DIR / "photoframe.log"

PICTURE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".mov", ".3gp", ".avi"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTH_NUMBER = {name.lower(): number for number, name in enumerate(MONTHS, 1)}
YEAR_RE = re.compile(r"\d{4}")

POLL_MS = 200               # how often the main loop checks the player
VIDEO_START_TIMEOUT = 20    # seconds a video may take to start before it is skipped
VIDEO_STALL_TIMEOUT = 15    # seconds VLC's playback clock may stop before the video is skipped
VIDEO_FROZEN_TIMEOUT = 10   # seconds the picture may freeze (sound still going) before skipping
VIDEO_END_GRACE = 15        # the last seconds of a video may show a still picture (sound runs longer)
VIDEO_OVERRUN_GRACE = 20    # seconds a video may run past its length before it is skipped
WATCHDOG_TIMEOUT = 30       # seconds VLC may lock up the frame before the frame restarts itself

PICTURE_LOAD_TIMEOUT = 25   # seconds a photo may take to load from the share
SHARE_JOB_TIMEOUT = 45      # seconds a folder listing on the share may take
SHARE_RETRY_SECONDS = 15    # how often to try the share again while it is down
EMPTY_RETRY_SECONDS = 600   # how often to look again when there is nothing for this month
CLOCK_RETRY_SECONDS = 5     # how often to check whether the date/time is set
CLOCK_LOG_SECONDS = 300     # how often to note in the log that we are still waiting for it

SKIPPED_ON_START = []       # videos skipped after a lock-up (passed along on restart)

DEFAULTS = {
    "frame": {
        "picture_seconds": "8",
        "picture_mode": "native",
        "video_volume": "70",
        "videos_folder": "videos",
        "wait_for_clock": "yes",
    },
    "share": {
        "server": "192.168.50.115",
        "share_name": "dropbox",
        "mount_point": "/mnt/pi-frame-share",
    },
}

log = logging.getLogger("photoframe")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def setup_logging():
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.critical("Unexpected error, photo frame stopped", exc_info=(exc_type, exc, tb))

    def log_uncaught_thread(args):
        log.error("Unexpected error in background thread",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = log_uncaught
    threading.excepthook = log_uncaught_thread


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
class Settings:
    def __init__(self):
        cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
        cfg.read_dict(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                cfg.read(CONFIG_FILE, encoding="utf-8")
            except configparser.Error as e:
                log.error("Could not read %s (%s). Using default settings.", CONFIG_FILE.name, e)
        else:
            log.error("Settings file %s not found. Using default settings.", CONFIG_FILE)

        frame, share = cfg["frame"], cfg["share"]

        self.picture_seconds = self._number(frame, "picture_seconds", float, 1, 3600)

        mode = frame.get("picture_mode").strip().lower()
        if mode not in ("native", "fullscreen"):
            log.error("picture_mode '%s' is not valid (use native or fullscreen). Using native.", mode)
            mode = "native"
        self.picture_mode = mode

        self.video_volume = int(self._number(frame, "video_volume", int, 0, 100))
        self.videos_folder = self._folder(frame.get("videos_folder"))

        try:
            self.wait_for_clock = frame.getboolean("wait_for_clock")
        except ValueError:
            log.error("wait_for_clock = '%s' is not valid (use yes or no). Using yes.",
                      frame.get("wait_for_clock"))
            self.wait_for_clock = True

        self.share_folder = self._folder(share.get("mount_point"))

    @staticmethod
    def _number(section, key, kind, low, high):
        raw = section.get(key)
        default = DEFAULTS[section.name][key]
        try:
            value = kind(raw)
            if not (low <= value <= high):
                raise ValueError
            return value
        except ValueError:
            log.error("%s = '%s' is not valid (must be %s to %s). Using %s.",
                      key, raw, low, high, default)
            return kind(default)

    @staticmethod
    def _folder(value):
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        return path


# --------------------------------------------------------------------------
# Finding files
# --------------------------------------------------------------------------
class ShareNotMounted(OSError):
    pass


def is_mounted(path):
    return os.path.ismount(path)


def media_files(folder, extensions, raise_errors):
    """All files with the given extensions in a folder and its subfolders.
    Hidden files and other file types are skipped quietly."""
    found, errors = [], []
    for root, dirs, files in os.walk(folder, onerror=errors.append):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.startswith(".") and Path(name).suffix.lower() in extensions:
                found.append(Path(root) / name)
    if errors:
        if raise_errors:
            raise errors[0]
        for e in errors:
            log.error("Could not read folder: %s", e)
    return found


def share_years(share_root):
    """Year folders (2008, 2009, ...) at the top of the share. Raises OSError if
    the share can't be reached."""
    if not is_mounted(share_root):
        raise ShareNotMounted(f"the network share is not connected at {share_root}")
    with os.scandir(share_root) as entries:
        return sorted(int(e.name) for e in entries
                      if YEAR_RE.fullmatch(e.name) and e.is_dir())


def share_pictures(share_root, year, month):
    """Photos in <share>/<year>/<Month>, or [] if that folder doesn't exist.
    Raises OSError if the share can't be reached."""
    if not is_mounted(share_root):
        raise ShareNotMounted(f"the network share is not connected at {share_root}")
    month_name = MONTHS[month - 1].lower()
    try:
        with os.scandir(Path(share_root) / str(year)) as entries:
            folder = next((Path(e.path) for e in entries
                           if e.name.lower() == month_name and e.is_dir()), None)
    except FileNotFoundError:
        return []
    if folder is None:
        return []
    return media_files(folder, PICTURE_EXTENSIONS, raise_errors=True)


def share_reachable(share_root):
    try:
        share_years(share_root)
        return True
    except OSError:
        return False


def index_local_videos(videos_root):
    """Videos on the SD card, grouped by (year, month) from videos/<year>/<Month>/.
    Also returns the videos that are not inside a year/month folder."""
    if not videos_root.is_dir():
        try:
            videos_root.mkdir(parents=True, exist_ok=True)
            log.info("Videos folder did not exist, so it was created: %s", videos_root)
        except OSError as e:
            log.error("Videos folder not found and could not be created: %s (%s)", videos_root, e)
        return {}, []
    index, loose = {}, []
    for path in media_files(videos_root, VIDEO_EXTENSIONS, raise_errors=False):
        parts = path.relative_to(videos_root).parts
        month = MONTH_NUMBER.get(parts[1].lower()) if len(parts) >= 3 else None
        if month and YEAR_RE.fullmatch(parts[0]):
            index.setdefault((int(parts[0]), month), []).append(path)
        else:
            loose.append(path)
    return index, loose


def clock_is_set():
    """True once the Pi has set its clock from the internet (it has no clock battery)."""
    try:
        result = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                                capture_output=True, text=True, timeout=10)
        answer = result.stdout.strip().lower()
        if answer in ("yes", "no"):
            return answer == "yes"
    except (OSError, subprocess.SubprocessError):
        pass
    if Path("/run/systemd/timesync/synchronized").exists():
        return True
    return datetime.now().year >= 2026


# --------------------------------------------------------------------------
# Background work (anything that touches the network share)
# --------------------------------------------------------------------------
class Job:
    """Runs a function in a background thread, so a slow or unreachable network
    share can never freeze the screen."""

    def __init__(self, kind, fn, *args, **info):
        self.kind = kind
        self.info = info
        self.result = None
        self.error = None
        self.started = time.monotonic()
        self._done = threading.Event()
        threading.Thread(target=self._run, args=(fn, args), daemon=True).start()

    def _run(self, fn, args):
        try:
            self.result = fn(*args)
        except Exception as e:
            self.error = e
        finally:
            self._done.set()

    @property
    def done(self):
        return self._done.is_set()

    def age(self):
        return time.monotonic() - self.started


def scan_years_job(share_root):
    try:
        return share_years(share_root), None
    except OSError as e:
        return None, e


def scan_pictures_job(share_root, year, month):
    try:
        return share_pictures(share_root, year, month), None
    except OSError as e:
        return None, e


class Block:
    """Everything to show for one year: photos and videos in random order."""

    def __init__(self, year, items):
        self.year = year
        self.queue = list(items)
        random.shuffle(self.queue)

    def pop(self):
        return self.queue.pop() if self.queue else None


# --------------------------------------------------------------------------
# Picture preparation
# --------------------------------------------------------------------------
def render_picture(src, dest, screen_w, screen_h, mode):
    """Draw the picture centered on a black screen-sized canvas and save it.

    native     : shown at its real size; only shrunk if bigger than the screen
    fullscreen : enlarged or shrunk to fill the screen, keeping its proportions
    """
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        w, h = im.size
        sideways = im.getexif().get(0x0112, 1) in (5, 6, 7, 8)
        shown_w, shown_h = (h, w) if sideways else (w, h)
        scale = min(screen_w / shown_w, screen_h / shown_h)
        if mode == "native":
            scale = min(1.0, scale)
        if scale < 1.0:
            # Large JPEGs: decode at a reduced size straight away (much faster on a Pi)
            im.draft(None, (max(1, int(w * scale)) + 1, max(1, int(h * scale)) + 1))

        im = ImageOps.exif_transpose(im)   # turn sideways phone photos upright
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        im = im.convert("RGBA" if has_alpha else "RGB")

        target = (max(1, round(shown_w * scale)), max(1, round(shown_h * scale)))
        if im.size != target:
            im = im.resize(target, Image.LANCZOS)

        canvas = Image.new("RGB", (screen_w, screen_h), "black")
        x = (screen_w - im.width) // 2
        y = (screen_h - im.height) // 2
        canvas.paste(im, (x, y), im if has_alpha else None)
        canvas.save(dest, "PNG", compress_level=1)


class PreparedItem:
    """The next item to show. Photos are loaded and rendered in a background thread."""

    def __init__(self, item, frame_file=None, settings=None, screen=None):
        self.item = item
        self.kind, self.path = item
        self.frame_file = frame_file
        self.error = None
        self.network_error = False
        self.created = time.monotonic()
        self.thread = None
        if self.kind == "picture":
            self.thread = threading.Thread(
                target=self._render, args=(settings, screen), daemon=True)
            self.thread.start()

    def _render(self, settings, screen):
        try:
            render_picture(self.path, self.frame_file, screen[0], screen[1], settings.picture_mode)
        except Exception as e:
            self.error = e
            # A broken photo, or the share went away while reading it?
            self.network_error = not share_reachable(settings.share_folder)

    @property
    def ready(self):
        return self.thread is None or not self.thread.is_alive()

    def age(self):
        return time.monotonic() - self.created


# --------------------------------------------------------------------------
# The frame
# --------------------------------------------------------------------------
class PhotoFrame:
    def __init__(self, settings, tk, vlc, local_videos):
        self.settings = settings
        self.vlc = vlc
        self.local_videos = local_videos
        self.stopping = False

        # Black full-screen window that VLC draws into. It stays open the whole
        # time, so the desktop never shows between items.
        self.root = tk.Tk()
        self.root.title("Photo Frame")
        self.root.configure(bg="black", cursor="none")
        self.root.attributes("-fullscreen", True)
        self.video_area = tk.Frame(self.root, bg="black", cursor="none")
        self.video_area.pack(fill="both", expand=True)
        self.root.update()

        self.screen = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        log.info("Screen size %dx%d, picture mode %s, %g seconds per picture, volume %d%%",
                 self.screen[0], self.screen[1], settings.picture_mode,
                 settings.picture_seconds, settings.video_volume)

        # Text shown on top of everything: a centered message, and a small
        # "Waiting on Network" notice in the bottom-right corner.
        self.center_label = tk.Label(
            self.root, text="", fg="#c8c8c8", bg="black", justify="center",
            font=("DejaVu Sans", max(14, self.screen[1] // 36)),
            wraplength=int(self.screen[0] * 0.9), cursor="none")
        self.corner_label = tk.Label(
            self.root, text="Waiting on Network", fg="#ffb000", bg="black",
            font=("DejaVu Sans", max(12, self.screen[1] // 54)),
            padx=14, pady=8, cursor="none")
        self.center_text = None
        self.corner_shown = False

        self.instance = vlc.Instance([
            "--no-video-title-show",   # don't print the file name on screen
            "--no-osd",
            "--no-snapshot-preview",
            "--no-spu",                # no subtitles
            "--stats",                 # frame counts, used to notice a frozen picture
            "--mouse-hide-timeout=0",
            "--quiet",
        ])
        self.player = self.instance.media_player_new()
        self.player.set_xwindow(self.video_area.winfo_id())
        self.player.video_set_mouse_input(False)
        self.player.video_set_key_input(False)

        # Rendered photos are kept in RAM, one file per photo.
        base = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
        self.temp_dir = Path(tempfile.mkdtemp(prefix="photoframe-", dir=base))
        self.frame_counter = 0

        # What is on screen
        self.current = None
        self.upcoming = None
        self.media = None
        self.started_at = 0.0
        self.volume_set = False
        self.last_video_time = -1
        self.last_progress_at = 0.0
        self.video_length = 0
        self.last_frames = -1
        self.last_frame_at = 0.0
        self.frame_stats_work = False   # becomes True once VLC reports a displayed frame
        self.stats = vlc.MediaStats() if hasattr(vlc, "MediaStats") else None
        self.bad = set(SKIPPED_ON_START)   # files that failed; not tried again this run

        # Where we are in the year-by-year cycle
        self.cycle_month = None
        self.cycle_years = []
        self.year_pos = -1
        self.cycle_shown = 0
        self.last_cycle_logged = None
        self.block = None
        self.job = None
        self.idle = False
        self.idle_until = 0.0
        self.idle_month = None

        # Network share status
        self.share_ok = True
        self.share_check = None
        self.share_checked_at = 0.0

        # Date/time
        self.waiting_for_clock = settings.wait_for_clock
        self.clock_job = None
        self.clock_checked_at = 0.0
        self.clock_logged_at = time.monotonic()
        if self.waiting_for_clock:
            log.info("Waiting for the Pi to get the date and time from the internet")
            self._show_center("Waiting on date/time")

        # Watchdog: if VLC locks up inside a call, the main loop stops ticking.
        # A background thread notices and restarts the frame, skipping that video.
        self.heartbeat = time.monotonic()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---- on-screen messages --------------------------------------------
    def _show_center(self, text):
        if text == self.center_text:
            return
        self.center_text = text
        self.center_label.configure(text=text)
        self.center_label.place(relx=0.5, rely=0.5, anchor="center")
        self.center_label.lift()

    def _hide_center(self):
        if self.center_text is not None:
            self.center_text = None
            self.center_label.place_forget()

    def _update_corner(self):
        show = not self.share_ok and not self.waiting_for_clock
        if show and not self.corner_shown:
            self.corner_label.place(relx=1.0, rely=1.0, anchor="se", x=-24, y=-24)
            self.corner_label.lift()
        elif not show and self.corner_shown:
            self.corner_label.place_forget()
        self.corner_shown = show

    def _set_share(self, ok, reason=None):
        if ok == self.share_ok:
            return
        self.share_ok = ok
        if ok:
            log.info("Network share is reachable again; photos are back")
            if self.idle:
                self.idle_until = 0   # look for photos right away
        else:
            log.error("Network share can't be reached (%s). Playing local videos only "
                      "and retrying every %d seconds.", reason, SHARE_RETRY_SECONDS)
            self.share_checked_at = time.monotonic()
        self._update_corner()

    # ---- choosing what comes next --------------------------------------
    def _mark_bad(self, path, message, *args):
        log.error(message, *args)
        self.bad.add(str(path))

    def _next_item(self):
        """The next (kind, path) to show, or None if nothing is ready yet."""
        if self.idle:
            if time.monotonic() < self.idle_until and datetime.now().month == self.idle_month:
                return None
            self.idle = False
            self._hide_center()

        for _ in range(100):
            month = datetime.now().month
            if self.cycle_month is not None and month != self.cycle_month:
                log.info("It's a new month: now showing %s, starting with the oldest year",
                         MONTHS[month - 1])
                self.cycle_month = None
                self.block = None
                self.job = None

            if self.block is not None:
                item = self.block.pop()
                while item is not None and (str(item[1]) in self.bad or
                                            (item[0] == "picture" and not self.share_ok)):
                    item = self.block.pop()
                if item is not None:
                    return item

            if not self._next_block():
                return None
        return None

    def _start_job(self):
        """Start looking up whatever comes after the current year. Returns False if
        there is nothing to look up right now (the frame went idle)."""
        root = self.settings.share_folder
        month = self.cycle_month
        if month is None or self.year_pos + 1 >= len(self.cycle_years):
            if month is not None and self.cycle_shown == 0:
                self._go_idle()
                return False
            self.job = Job("cycle", scan_years_job, root, month=datetime.now().month)
        else:
            pos = self.year_pos + 1
            year = self.cycle_years[pos]
            self.job = Job("block", scan_pictures_job, root, year, month,
                           month=month, year=year, pos=pos)
        return True

    def _next_block(self):
        """Move on to the next year. Returns True once a new year is ready to show."""
        if self.job is None and not self._start_job():
            return False
        job = self.job
        if not job.done:
            if job.age() < SHARE_JOB_TIMEOUT:
                return False
            found, error = None, "it stopped answering"   # carry on without it
        elif job.error is not None:
            log.error("Unexpected error while reading the network share: %r", job.error)
            found, error = None, job.error
        else:
            found, error = job.result
        self.job = None

        if found is None:
            self._set_share(False, error)
        else:
            self._set_share(True)

        if job.kind == "cycle":
            month = job.info["month"]
            if month != datetime.now().month:
                return False                     # month changed meanwhile; look again
            video_years = {year for (year, m) in self.local_videos if m == month}
            years = sorted(set(found or []) | video_years)
            self.cycle_month = month
            self.cycle_years = years
            self.year_pos = -1
            self.cycle_shown = 0
            self.block = None
            if not years:
                self._go_idle()
                return False
            summary = (month, years[0], years[-1])
            if summary != self.last_cycle_logged:
                self.last_cycle_logged = summary
                log.info("Showing %s from %d to %d", MONTHS[month - 1], years[0], years[-1])
            return self._next_block()

        if job.info["month"] != self.cycle_month:
            return False                         # left over from before the month changed
        year = job.info["year"]
        self.year_pos = job.info["pos"]
        pictures = found or []
        videos = self.local_videos.get((year, job.info["month"]), [])
        self.block = Block(year, [("picture", p) for p in pictures] +
                                 [("video", v) for v in videos])
        return True

    def _go_idle(self):
        """Nothing at all to show for this month. Wait, then look again."""
        self.idle = True
        self.idle_month = datetime.now().month
        self.cycle_month = None
        self.block = None
        self.job = None
        if self.share_ok:
            month = MONTHS[self.idle_month - 1]
            log.warning("No photos or videos found for %s in any year. Looking again in "
                        "%d minutes.", month, EMPTY_RETRY_SECONDS // 60)
            self.idle_until = time.monotonic() + EMPTY_RETRY_SECONDS
        else:
            self.idle_until = time.monotonic() + SHARE_RETRY_SECONDS * 4

    def _idle_message(self):
        if not self.share_ok:
            return None   # just the "Waiting on Network" corner notice
        month = MONTHS[self.idle_month - 1]
        return (f"No pictures or videos found for {month}\n\n"
                f"Photos are read from:\n{self.settings.share_folder}/<year>/{month}\n\n"
                f"Videos are read from:\n{self.settings.videos_folder}/<year>/{month}")

    # ---- item handling -------------------------------------------------
    def _prepare(self, item):
        frame_file = None
        if item[0] == "picture":
            self.frame_counter += 1
            frame_file = self.temp_dir / f"frame_{self.frame_counter}.png"
        return PreparedItem(item, frame_file, self.settings, self.screen)

    def advance(self):
        """Switch to the next item. If it isn't ready yet, the current item stays up."""
        for _ in range(100):
            if self.upcoming is None:
                item = self._next_item()
                if item is None:
                    if self.idle:
                        self._blank()
                    return
                self.upcoming = self._prepare(item)

            prepared = self.upcoming
            if not prepared.ready:
                if prepared.age() > PICTURE_LOAD_TIMEOUT:
                    self.upcoming = None
                    self._set_share(False, "a photo took more than %d seconds to load"
                                    % PICTURE_LOAD_TIMEOUT)
                    continue
                return   # photo still loading; try again next tick

            self.upcoming = None
            if prepared.error is not None:
                if prepared.network_error:
                    self._set_share(False, prepared.error)
                else:
                    self._mark_bad(prepared.path, "Skipping picture %s: %s",
                                   prepared.path, prepared.error)
                continue

            self._show(prepared)
            item = self._next_item()
            if item is not None:
                self.upcoming = self._prepare(item)
            return

    def _blank(self):
        """Clear the screen and show the idle message (if any)."""
        if self.current is not None:
            self.player.stop()
            self.current = None
            self._clean_temp()
        message = self._idle_message()
        if message:
            self._show_center(message)
        else:
            self._hide_center()

    def _show(self, prepared):
        self._hide_center()
        if prepared.kind == "picture":
            media = self.instance.media_new_path(str(prepared.frame_file))
            media.add_option(":image-duration=-1")   # we decide when to move on
        else:
            media = self.instance.media_new_path(str(prepared.path))
            log.info("Playing video %s", prepared.path.name)
        self.player.set_media(media)
        self.player.play()
        if self.media is not None:
            self.media.release()
        self.media = media

        self.current = prepared
        self.cycle_shown += 1
        self.started_at = time.monotonic()
        self.volume_set = False
        self.last_video_time = -1
        self.last_progress_at = self.started_at
        self.video_length = 0
        self.last_frames = -1
        self.last_frame_at = self.started_at
        if prepared.kind == "video":
            self._apply_volume()
        self._clean_temp()

    def _clean_temp(self):
        """Delete rendered photos that are no longer on screen."""
        keep = self.current.frame_file if self.current else None
        try:
            for f in self.temp_dir.iterdir():
                if f != keep:
                    f.unlink(missing_ok=True)
        except OSError:
            pass

    def _apply_volume(self):
        if not self.volume_set and self.player.audio_set_volume(self.settings.video_volume) == 0:
            self.volume_set = True

    # ---- main loop -----------------------------------------------------
    def tick(self):
        if self.stopping:
            return
        self.heartbeat = time.monotonic()
        try:
            if self.waiting_for_clock:
                self._check_clock()
            else:
                self._check_share()
                if self.current is None:
                    self.advance()
                elif self.current.kind == "picture":
                    self._check_picture()
                else:
                    self._check_video()
        except Exception:
            log.exception("Unexpected error while switching items")
            self.current = None
        if not self.stopping:
            self.root.after(POLL_MS, self.tick)

    def _check_clock(self):
        now = time.monotonic()
        if self.clock_job is None:
            if now - self.clock_checked_at >= CLOCK_RETRY_SECONDS:
                self.clock_job = Job("clock", clock_is_set)
            return
        if not self.clock_job.done:
            return
        is_set = self.clock_job.result is True
        self.clock_job = None
        self.clock_checked_at = now
        if is_set:
            self.waiting_for_clock = False
            log.info("Date and time are set: %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
            self._hide_center()
            self._update_corner()
        elif now - self.clock_logged_at >= CLOCK_LOG_SECONDS:
            self.clock_logged_at = now
            log.warning("Still waiting for the date and time. The Pi needs an internet "
                        "connection to set its clock.")

    def _check_share(self):
        """While the share is down, keep trying it in the background."""
        check = self.share_check
        if check is not None:
            if check.done:
                self.share_check = None
                if check.result is not None and check.result[0] is not None:
                    self._set_share(True)
            elif check.age() > SHARE_JOB_TIMEOUT:
                self.share_check = None
        if (not self.share_ok and self.share_check is None and
                time.monotonic() - self.share_checked_at >= SHARE_RETRY_SECONDS):
            self.share_checked_at = time.monotonic()
            self.share_check = Job("check", scan_years_job, self.settings.share_folder)

    def _check_picture(self):
        State = self.vlc.State
        if self.player.get_state() == State.Error:
            self._mark_bad(self.current.path, "VLC could not show picture %s", self.current.path)
            self.advance()
        elif time.monotonic() - self.started_at >= self.settings.picture_seconds:
            self.advance()

    def _check_video(self):
        State = self.vlc.State
        now = time.monotonic()
        state = self.player.get_state()
        path = self.current.path

        if state in (State.Ended, State.Stopped):
            self.advance()
        elif state == State.Error:
            self._mark_bad(path, "VLC could not play video %s", path)
            self.advance()
        elif state in (State.NothingSpecial, State.Opening, State.Buffering):
            if now - self.started_at > VIDEO_START_TIMEOUT:
                self._mark_bad(path, "Video did not start within %d seconds, skipping: %s",
                               VIDEO_START_TIMEOUT, path)
                self.advance()
        elif state == State.Playing:
            self._apply_volume()
            if self.video_length <= 0:
                self.video_length = max(0, self.player.get_length())   # milliseconds
            position = self.player.get_time()

            # 1. VLC's playback clock stopped moving
            if position != self.last_video_time:
                self.last_video_time = position
                self.last_progress_at = now
            elif now - self.last_progress_at > VIDEO_STALL_TIMEOUT:
                self._video_failed("playback stopped moving for %d seconds" % VIDEO_STALL_TIMEOUT)
                return

            # 2. The picture froze even though the sound/clock keeps going
            frames = self._displayed_frames()
            if frames is not None:
                if frames > 0:
                    self.frame_stats_work = True
                if frames > self.last_frames:
                    self.last_frames = frames
                    self.last_frame_at = now
                near_end = (self.video_length > 0 and
                            position >= self.video_length - VIDEO_END_GRACE * 1000)
                if (self.frame_stats_work and not near_end
                        and now - self.last_frame_at > VIDEO_FROZEN_TIMEOUT):
                    self._video_failed("picture froze for %d seconds" % VIDEO_FROZEN_TIMEOUT)
                    return

            # 3. The video has run far longer than it should
            if self.video_length > 0:
                limit = self.video_length / 1000 + VIDEO_OVERRUN_GRACE
                if now - self.started_at > limit:
                    self._video_failed("still running %d seconds after it should have ended"
                                       % VIDEO_OVERRUN_GRACE)
                    return
        elif state == State.Paused:
            self.player.play()

    def _displayed_frames(self):
        """Number of video frames VLC has put on screen, or None if VLC can't tell us."""
        if self.stats is None or self.media is None:
            return None
        try:
            if self.media.get_stats(self.stats):
                return self.stats.displayed_pictures
        except Exception:
            self.stats = None   # this VLC version doesn't support it; stop asking
        return None

    def _video_failed(self, reason):
        self._mark_bad(self.current.path, "Video %s, skipping it for the rest of this run "
                       "(try re-encoding it): %s", reason, self.current.path)
        self.advance()

    # ---- watchdog ------------------------------------------------------
    def _watchdog(self):
        while not self.stopping:
            time.sleep(2)
            stuck_for = time.monotonic() - self.heartbeat
            if stuck_for < WATCHDOG_TIMEOUT or self.stopping:
                continue
            current = self.current
            skip = list(SKIPPED_ON_START)
            if current is not None and current.kind == "video":
                skip.append(str(current.path))
                log.error("VLC locked up for %d seconds on video %s. Restarting the frame "
                          "and skipping that video (try re-encoding it).", stuck_for, current.path)
            else:
                log.error("VLC locked up for %d seconds. Restarting the frame.", stuck_for)
            restart_self(skip)

    # ---- start / stop --------------------------------------------------
    def run(self):
        self.root.after(0, self.tick)
        self.root.mainloop()

    def shutdown(self, *_):
        if self.stopping:
            return
        self.stopping = True
        log.info("Photo frame stopping")
        try:
            if self.media is not None:
                self.media.release()
            self.player.stop()
            self.player.release()
            self.instance.release()
        except Exception:
            log.exception("Error while stopping VLC")
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        try:
            self.root.destroy()
        except Exception:
            pass


def restart_self(skip):
    """Replace this process with a fresh copy of the frame (same process ID, so
    stop_frame.sh still finds it). Videos in `skip` are left out of the new run."""
    for handler in log.handlers:
        handler.flush()
    args = [sys.executable, str(Path(__file__).resolve())]
    for path in skip:
        args += ["--skip", path]
    os.execv(sys.executable, args)


def parse_skips(argv):
    skips, i = [], 1
    while i < len(argv):
        if argv[i] == "--skip" and i + 1 < len(argv):
            skips.append(argv[i + 1])
            i += 2
        else:
            i += 1
    return skips


def main():
    setup_logging()
    log.info("Photo frame starting (network share version)")

    try:
        import tkinter as tk
        import vlc
        import PIL  # noqa: F401  (checked here so a missing package is logged clearly)
    except ImportError as e:
        log.critical("A required package is missing (%s). "
                     "Install with: sudo apt install vlc python3-vlc python3-tk python3-pil", e)
        return 1

    settings = Settings()
    log.info("Photos from the network share at %s, videos from %s",
             settings.share_folder, settings.videos_folder)

    local_videos, loose = index_local_videos(settings.videos_folder)
    log.info("Found %d videos in year/month folders",
             sum(len(v) for v in local_videos.values()))
    if loose:
        names = ", ".join(str(p.relative_to(settings.videos_folder)) for p in loose[:10])
        more = f" and {len(loose) - 10} more" if len(loose) > 10 else ""
        log.warning("%d video(s) are not inside a year/month folder (like videos/2012/November) "
                    "and won't be shown: %s%s", len(loose), names, more)

    SKIPPED_ON_START.extend(parse_skips(sys.argv))
    if SKIPPED_ON_START:
        log.info("Restarted after a VLC lock-up. Skipping %d video(s) for this run: %s",
                 len(SKIPPED_ON_START), ", ".join(Path(s).name for s in SKIPPED_ON_START))

    try:
        frame = PhotoFrame(settings, tk, vlc, local_videos)
    except tk.TclError as e:
        log.critical("Could not open the display (%s). Is the desktop running and "
                     "was the frame started with start_frame.sh?", e)
        return 1

    def on_signal(*_):
        frame.root.after(0, frame.shutdown)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    frame.run()
    log.info("Photo frame stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
