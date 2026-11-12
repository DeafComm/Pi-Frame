# Raspberry Pi Photo Frame

Shows your pictures and plays your videos (with sound) in random order, full screen, on a Raspberry Pi 4.
Nothing repeats until everything has been shown once. Nothing is ever stretched out of shape; empty space is black.

## What's in this package

| File | Goes where | What it does |
|---|---|---|
| `photoframe.py` | Pi: `~/Documents` | The photo frame |
| `photoframe.conf` | Pi: `~/Documents` | Settings (picture time, picture mode, volume, folders) |
| `start_frame.sh` | Pi: `~/Documents` | Starts the frame (use over SSH) |
| `stop_frame.sh` | Pi: `~/Documents` | Stops the frame |
| `encode_videos.py` | Linux Mint PC | Converts videos so they play smoothly on the Pi |

On the Pi, everything lives in Documents:

```
~/Documents/
├── photoframe.py
├── photoframe.conf
├── photoframe.log      (created automatically, errors go here)
├── start_frame.sh
├── stop_frame.sh
├── pictures/           (your .jpg / .png files, subfolders are fine)
└── videos/             (the converted videos from the "fixed" folder)
```

---

## Part 1: Set up the Pi (one time)

All of these are done from an SSH session on the Pi.

**1. Install the packages the frame needs**

```bash
sudo apt update
sudo apt install -y vlc python3-vlc python3-tk python3-pil
```

**2. Copy the files and make the folders.** From your Mint PC, in the folder holding these files
(replace `USER` and `PI-ADDRESS` with your Pi's username and IP address or hostname):

```bash
scp photoframe.py photoframe.conf start_frame.sh stop_frame.sh USER@PI-ADDRESS:~/Documents/
```

Then on the Pi:

```bash
cd ~/Documents
chmod +x start_frame.sh stop_frame.sh
mkdir -p pictures videos
```

**3. Send sound to the 3.5mm jack.** Run `sudo raspi-config`, choose **System Options → Audio**, and pick the
headphones / AV jack. Or do it with these two commands: run `wpctl status`, find the line under **Sinks**
that mentions Headphones or the built-in audio jack, note its number, then run `wpctl set-default NUMBER`.

**4. Stop the screen from going black after 10 minutes.** Run `sudo raspi-config`, choose
**Display Options → Screen Blanking**, and select **No**.

**5. Make sure the Pi logs into the desktop by itself.** In `sudo raspi-config`, choose
**System Options → Boot / Auto Login → Desktop Autologin**. The frame draws on that desktop, so it must be
running even though no keyboard or mouse is attached.

**6. Reboot** so the screen blanking and login changes take effect: `sudo reboot`

---

## Part 2: Use it

Over SSH, on the Pi:

```bash
~/Documents/start_frame.sh     # start
~/Documents/stop_frame.sh      # stop
```

You can close your SSH session after starting; the frame keeps running.

**To change pictures, videos, or settings:** stop the frame, make your changes, start it again.
The frame reads the folders and settings only when it starts.

**To check for problems:**

```bash
tail -n 30 ~/Documents/photoframe.log
```

The log records start and stop times, how many files were found, and any errors (for example a picture that
couldn't be opened or a video that wouldn't play). Bad files are skipped and logged; the frame keeps going.
The log is capped at 5 MB, with one older copy kept as `photoframe.log.1`.

### Settings (`photoframe.conf`)

| Setting | Default | Meaning |
|---|---|---|
| `picture_seconds` | `8` | Seconds each picture stays on screen (1 to 3600) |
| `picture_mode` | `native` | `native`: pictures shown at their real size, centered on black, and shrunk only if larger than the screen. `fullscreen`: every picture enlarged or shrunk to fill the screen as far as it can while keeping its shape |
| `video_volume` | `70` | Video volume, 0 to 100 |
| `pictures_folder` | `pictures` | Folder of pictures. A plain name means a folder inside Documents |
| `videos_folder` | `videos` | Folder of videos |

If a setting is mistyped, the frame uses the default for it and writes an error in the log.

Videos always fill the screen as far as they can while keeping their shape, so an old 4:3 video has black
bars on the left and right.

---

## Part 3: Convert videos (on the Linux Mint PC)

Put the videos you want to convert in a folder, then run:

```bash
python3 encode_videos.py /path/to/that/folder
```

The converted copies go into a `fixed` folder inside it. Your originals are never changed. Copy the results to
the Pi (stop the frame first):

```bash
scp /path/to/that/folder/fixed/*.mp4 USER@PI-ADDRESS:~/Documents/videos/
```

What the converter does:

- Makes H.264 `.mp4` files, which the Pi 4 decodes in hardware
- Makes every video exactly 1920x1080: the picture is sized to fill the screen without changing its shape,
  and black bars are built into the file. The Pi then never has to resize video while playing
- Keeps the correct shape, including old widescreen DVD-style video and sideways phone video
- Uses a steady frame rate (uneven phone and .3gp frame rates can make the Pi stall)
- Removes the "comb" lines from old interlaced camcorder video, only when it detects them
- Converts HDR phone video to normal colors, so it doesn't look washed out
- Limits frame rate to 30 fps for smooth playback
- Evens out the volume so every video plays at about the same loudness
- Handles `.3gp` files too

**Re-running is safe:** videos already in `fixed` are skipped, so add new videos to the folder and run it again.
Videos made by the first version of this script that aren't 1920x1080 are converted again automatically.
If you press Ctrl+C, the half-finished file is removed and the next run picks up where it stopped.
If two files share a name (`clip.mp4` and `clip.3gp`), the second becomes `clip_3gp.mp4`.

Errors are written to `encode.log` in the folder you converted. A 4K video can take several minutes per minute
of footage, depending on your PC; the progress line shows the speed.

Settings like quality, frame rate limit, and loudness are at the top of `encode_videos.py` if you ever want to
change them.

---

## Troubleshooting

**`start_frame.sh` says the frame didn't start, and the log mentions the display.**
The desktop must be logged in on the Pi's screen (see Part 1, step 5). If it is, run `ls /tmp/.X11-unix/` on
the Pi. If you see `X1` instead of `X0`, start the frame with `FRAME_DISPLAY=:1 ~/Documents/start_frame.sh`.

**No sound.** Recheck Part 1, step 3, and check `video_volume` in the settings. Make sure the speaker is
plugged into the 3.5mm jack, not HDMI.

**Screen goes black after a while.** Screen blanking is still on; see Part 1, step 4, then reboot.

**A video freezes.** The frame now recovers by itself:

- If the picture freezes for 10 seconds, or the video runs 20 seconds past its length, the frame skips it.
- If VLC locks up completely for 30 seconds, the frame restarts itself (the screen may flash for a moment)
  and leaves that video out until the next time you start it.

Either way, the log names the video. Run `grep -i video ~/Documents/photoframe.log` to see which files were
skipped. Re-run `encode_videos.py` on their originals and copy the new versions over.

**A video stutters.** Make sure it went through `encode_videos.py`. Files copied straight from a phone or
camera (especially 4K or HEVC) are much harder for the Pi to play.

**The screen shows "No pictures or videos found".** Both folders are empty (or were just created because they
were missing). Copy files into the folders shown on screen, then run `stop_frame.sh` and `start_frame.sh`.
If only one folder has files, the frame plays those and no message appears.

**A specific file never shows.** Check the log; it names any picture or video that was skipped and why.

---

## Later: starting at boot

When you're ready, the frame can be started automatically at login. That isn't set up yet, as requested.
