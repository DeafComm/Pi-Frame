#!/usr/bin/env python3
"""
Raspberry Pi photo frame.

Shows pictures (JPEG/PNG) and plays videos (with audio) in random order,
full screen, with hard cuts. Settings are read from photoframe.conf and
errors are written to photoframe.log, both in the same folder as this script.

Start and stop it over SSH with start_frame.sh / stop_frame.sh.
"""

import configparser
import logging
import logging.handlers
import os
import random
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "photoframe.conf"
LOG_FILE = SCRIPT_DIR / "photoframe.log"

PICTURE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".mov", ".3gp", ".avi"}

POLL_MS = 200               # how often the main loop checks the player
VIDEO_START_TIMEOUT = 20    # seconds a video may take to start before it is skipped
VIDEO_STALL_TIMEOUT = 15    # seconds VLC's playback clock may stop before the video is skipped
VIDEO_FROZEN_TIMEOUT = 10   # seconds the picture may freeze (sound still going) before skipping
VIDEO_END_GRACE = 15        # the last seconds of a video may show a still picture (sound runs longer)
VIDEO_OVERRUN_GRACE = 20    # seconds a video may run past its length before it is skipped
WATCHDOG_TIMEOUT = 30       # seconds VLC may lock up the frame before the frame restarts itself

SKIPPED_ON_START = []       # videos skipped after a lock-up (passed along on restart)

DEFAULTS = {
    "picture_seconds": "8",
    "picture_mode": "native",
    "video_volume": "70",
    "pictures_folder": "pictures",
    "videos_folder": "videos",
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
        cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        cfg["frame"] = DEFAULTS
        if CONFIG_FILE.exists():
            try:
                cfg.read(CONFIG_FILE, encoding="utf-8")
            except configparser.Error as e:
                log.error("Could not read %s (%s). Using default settings.", CONFIG_FILE.name, e)
        else:
            log.error("Settings file %s not found. Using default settings.", CONFIG_FILE)

        s = cfg["frame"]

        self.picture_seconds = self._number(s, "picture_seconds", float, 1, 3600)

        mode = s.get("picture_mode", DEFAULTS["picture_mode"]).strip().lower()
        if mode not in ("native", "fullscreen"):
            log.error("picture_mode '%s' is not valid (use native or fullscreen). Using native.", mode)
            mode = "native"
        self.picture_mode = mode

        self.video_volume = int(self._number(s, "video_volume", int, 0, 100))

        self.pictures_folder = self._folder(s.get("pictures_folder", DEFAULTS["pictures_folder"]))
        self.videos_folder = self._folder(s.get("videos_folder", DEFAULTS["videos_folder"]))

    @staticmethod
    def _number(section, key, kind, low, high):
        raw = section.get(key, DEFAULTS[key])
        try:
            value = kind(raw)
            if not (low <= value <= high):
                raise ValueError
            return value
        except ValueError:
            log.error("%s = '%s' is not valid (must be %s to %s). Using %s.",
                      key, raw, low, high, DEFAULTS[key])
            return kind(DEFAULTS[key])

    @staticmethod
    def _folder(value):
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        return path


# --------------------------------------------------------------------------
# File discovery and random order
# --------------------------------------------------------------------------
def find_files(folder, extensions, label):
    if not folder.is_dir():
        try:
            folder.mkdir(parents=True, exist_ok=True)
            log.info("%s folder did not exist, so it was created: %s", label, folder)
        except OSError as e:
            log.error("%s folder not found and could not be created: %s (%s)", label, folder, e)
        return []
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in extensions:
                found.append(Path(root) / name)
    return found


class Playlist:
    """Random order; every item plays once before any item repeats."""

    def __init__(self, items):
        self.items = list(items)
        self.queue = []
        self.last = None

    def next(self):
        if not self.items:
            return None
        if not self.queue:
            self.queue = self.items[:]
            random.shuffle(self.queue)
            # don't show the same item twice in a row across a reshuffle
            if len(self.queue) > 1 and self.queue[-1] == self.last:
                self.queue[0], self.queue[-1] = self.queue[-1], self.queue[0]
        self.last = self.queue.pop()
        return self.last

    def remove(self, item):
        if item in self.items:
            self.items.remove(item)
        while item in self.queue:
            self.queue.remove(item)

    def __len__(self):
        return len(self.items)


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
        im = ImageOps.exif_transpose(im)   # turn sideways phone photos upright
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        im = im.convert("RGBA" if has_alpha else "RGB")

        w, h = im.size
        scale = min(screen_w / w, screen_h / h)
        if mode == "native":
            scale = min(1.0, scale)
        if scale != 1.0:
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            im = im.resize(new_size, Image.LANCZOS)

        canvas = Image.new("RGB", (screen_w, screen_h), "black")
        x = (screen_w - im.width) // 2
        y = (screen_h - im.height) // 2
        canvas.paste(im, (x, y), im if has_alpha else None)
        canvas.save(dest, "PNG", compress_level=1)


class PreparedItem:
    """The next item to show. Pictures are rendered in a background thread."""

    def __init__(self, item, frame_file=None, settings=None, screen=None):
        self.item = item
        self.kind, self.path = item
        self.frame_file = frame_file
        self.error = None
        self.thread = None
        if self.kind == "picture":
            self.thread = threading.Thread(
                target=self._render, args=(settings, screen), daemon=True)
            self.thread.start()

    def _render(self, settings, screen):
        try:
            render_picture(self.path, self.frame_file, screen[0], screen[1], settings.picture_mode)
        except Exception as e:  # corrupt or unreadable picture
            self.error = e

    @property
    def ready(self):
        return self.thread is None or not self.thread.is_alive()


# --------------------------------------------------------------------------
# The frame
# --------------------------------------------------------------------------
class PhotoFrame:
    def __init__(self, settings, playlist, tk, vlc):
        self.settings = settings
        self.playlist = playlist
        self.vlc = vlc
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

        # Two alternating files in RAM for rendered pictures, so VLC is never
        # reading a file while the next picture is being written.
        base = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
        self.temp_dir = Path(tempfile.mkdtemp(prefix="photoframe-", dir=base))
        self.frame_files = [self.temp_dir / "frame_0.png", self.temp_dir / "frame_1.png"]

        self.current = None
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

        # Watchdog: if VLC locks up inside a call, the main loop stops ticking.
        # A background thread notices and restarts the frame, skipping that video.
        self.heartbeat = time.monotonic()
        threading.Thread(target=self._watchdog, daemon=True).start()

        self.upcoming = self._prepare_next()

    # ---- item handling -------------------------------------------------
    def _prepare_next(self):
        item = self.playlist.next()
        if item is None:
            return None
        frame_file = None
        if item[0] == "picture":
            # always use the file that is NOT on screen right now
            on_screen = self.current.frame_file if self.current else None
            frame_file = self.frame_files[1] if on_screen == self.frame_files[0] else self.frame_files[0]
        return PreparedItem(item, frame_file, self.settings, self.screen)

    def _drop(self, item, message, *args):
        log.error(message, *args)
        self.playlist.remove(item)

    def advance(self):
        """Switch to the next item. Returns False if it should be retried shortly."""
        for _ in range(max(1, len(self.playlist)) + 1):
            if self.upcoming is None:
                self.upcoming = self._prepare_next()
                if self.upcoming is None:
                    log.error("Nothing left that can be shown. Photo frame stopping.")
                    self.shutdown()
                    return True

            if not self.upcoming.ready:
                return False   # picture still rendering; try again next tick

            prepared = self.upcoming
            self.upcoming = None
            if prepared.error is not None:
                self._drop(prepared.item, "Skipping picture %s: %s", prepared.path, prepared.error)
                continue

            self._show(prepared)
            self.upcoming = self._prepare_next()
            return True
        return True

    def _show(self, prepared):
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
        self.started_at = time.monotonic()
        self.volume_set = False
        self.last_video_time = -1
        self.last_progress_at = self.started_at
        self.video_length = 0
        self.last_frames = -1
        self.last_frame_at = self.started_at
        if prepared.kind == "video":
            self._apply_volume()

    def _apply_volume(self):
        if not self.volume_set and self.player.audio_set_volume(self.settings.video_volume) == 0:
            self.volume_set = True

    # ---- main loop -----------------------------------------------------
    def tick(self):
        if self.stopping:
            return
        self.heartbeat = time.monotonic()
        try:
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

    def _check_picture(self):
        State = self.vlc.State
        if self.player.get_state() == State.Error:
            self._drop(self.current.item, "VLC could not show picture %s", self.current.path)
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
            self._drop(self.current.item, "VLC could not play video %s", path)
            self.advance()
        elif state in (State.NothingSpecial, State.Opening, State.Buffering):
            if now - self.started_at > VIDEO_START_TIMEOUT:
                log.error("Video did not start within %d seconds, skipping: %s",
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
        self._drop(self.current.item, "Video %s, skipping it for the rest of this run "
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


def show_empty_message(tk, settings):
    """Both folders are empty: show a black screen with instructions until stopped."""
    root = tk.Tk()
    root.title("Photo Frame")
    root.configure(bg="black", cursor="none")
    root.attributes("-fullscreen", True)
    root.update()

    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    message = (
        "No pictures or videos found\n\n"
        "Add pictures (.jpg or .png) to:\n"
        f"{settings.pictures_folder}\n\n"
        "Add videos to:\n"
        f"{settings.videos_folder}\n\n"
        "Then stop and start the photo frame."
    )
    label = tk.Label(root, text=message, fg="#c8c8c8", bg="black", justify="center",
                     font=("DejaVu Sans", max(14, screen_h // 36)),
                     wraplength=int(screen_w * 0.9), cursor="none")
    label.place(relx=0.5, rely=0.5, anchor="center")

    def on_signal(*_):
        root.after(0, root.destroy)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # Tk only lets Python handle the stop signal while Python code is running,
    # so wake up regularly to stay responsive to stop_frame.sh.
    def keep_alive():
        root.after(500, keep_alive)

    keep_alive()
    root.mainloop()


def main():
    setup_logging()
    log.info("Photo frame starting")

    try:
        import tkinter as tk
        import vlc
        import PIL  # noqa: F401  (checked here so a missing package is logged clearly)
    except ImportError as e:
        log.critical("A required package is missing (%s). "
                     "Install with: sudo apt install vlc python3-vlc python3-tk python3-pil", e)
        return 1

    settings = Settings()
    pictures = find_files(settings.pictures_folder, PICTURE_EXTENSIONS, "Pictures")
    videos = find_files(settings.videos_folder, VIDEO_EXTENSIONS, "Videos")
    log.info("Found %d pictures and %d videos", len(pictures), len(videos))

    SKIPPED_ON_START.extend(parse_skips(sys.argv))
    if SKIPPED_ON_START:
        skip = set(SKIPPED_ON_START)
        videos = [v for v in videos if str(v) not in skip]
        log.info("Restarted after a VLC lock-up. Skipping %d video(s) for this run: %s",
                 len(skip), ", ".join(Path(s).name for s in skip))

    items = [("picture", p) for p in pictures] + [("video", v) for v in videos]

    try:
        if not items:
            log.warning("No pictures or videos found. Showing instructions on screen "
                        "until the frame is stopped.")
            show_empty_message(tk, settings)
            log.info("Photo frame stopped")
            return 0
        frame = PhotoFrame(settings, Playlist(items), tk, vlc)
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
