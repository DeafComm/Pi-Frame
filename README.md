# Raspberry Pi Photo Frame: Network Share Version

> **This is the `Pi-Frame-Remote` branch**, the version that reads photos from a network share (TrueNAS).
> The standard version, which reads photos from the SD card, is on the `main` branch.

Shows this month's photos from your network share and this month's videos from the SD card, full screen,
on a Raspberry Pi 4. It goes one year at a time, oldest first: every November from 2008, then 2009, and so on
up to the newest year, then back to the oldest. Within each year, that month's photos and videos play in random
order. On the 1st of each month it switches to the new month and starts again from the oldest year.

Nothing is ever stretched out of shape; empty space is black. The frame only ever **reads** from the share.

## What's in this package

| File | Goes where | What it does |
|---|---|---|
| `photoframe.py` | Pi: `~/Documents` | The photo frame |
| `photoframe.conf` | Pi: `~/Documents` | Settings (picture time, picture mode, volume, share address) |
| `setup_share.sh` | Pi: `~/Documents` | One-time setup that connects the network share (read-only) |
| `start_frame.sh` | Pi: `~/Documents` | Starts the frame (use over SSH) |
| `stop_frame.sh` | Pi: `~/Documents` | Stops the frame |
| `encode_videos.py` | Linux Mint PC | Converts videos and sorts them into year/month folders |

### Where things live

Photos stay on the share, in year and month folders:

```
//192.168.50.115/dropbox/
├── 2008/
│   ├── January/
│   ├── ...
│   └── November/      (photos: .jpg and .png; other files are ignored)
├── 2009/
└── ...
```

On the Pi, everything else lives in Documents, with videos in the same year/month layout:

```
~/Documents/
├── photoframe.py
├── photoframe.conf
├── photoframe.log         (created automatically, errors go here)
├── setup_share.sh
├── start_frame.sh
├── stop_frame.sh
└── videos/
    ├── 2009/
    │   └── November/      (converted videos from the "fixed" folder)
    └── 2012/
        └── November/
```

---

## Part 1: Set up the Pi (one time)

All of these are done from an SSH session on the Pi.

**1. Install the packages the frame needs**

```bash
sudo apt update
sudo apt install -y vlc python3-vlc python3-tk python3-pil cifs-utils
```

**2. Copy the files.** From your Mint PC, in the folder holding these files
(replace `USER` and `PI-ADDRESS` with your Pi's username and IP address or hostname):

```bash
scp photoframe.py photoframe.conf setup_share.sh start_frame.sh stop_frame.sh USER@PI-ADDRESS:~/Documents/
```

Then on the Pi:

```bash
cd ~/Documents
chmod +x setup_share.sh start_frame.sh stop_frame.sh
mkdir -p videos
```

**3. Connect the network share.** The server address (`192.168.50.115`) and share name (`dropbox`) are already
in `photoframe.conf`. Run:

```bash
sudo ./setup_share.sh
```

It asks for the share's username and password. These are saved in `/etc/pi-frame/share-credentials`, which
only the system administrator account can read. The script then connects the share read-only at
`/mnt/pi-frame-share` and tests it. When it works, it lists the year folders it found. From then on the Pi
connects the share by itself, including after a reboot or power outage.

If the server address, share name or password ever changes, update `photoframe.conf` if needed and run
`sudo ./setup_share.sh` again.

**4. Set the time zone.** The frame uses the Pi's clock to decide which month to show. Run `sudo raspi-config`,
choose **Localisation Options → Timezone**, and pick **America → Chicago**.

**5. Send sound to the 3.5mm jack.** Run `sudo raspi-config`, choose **System Options → Audio**, and pick the
headphones / AV jack. Or do it with these two commands: run `wpctl status`, find the line under **Sinks**
that mentions Headphones or the built-in audio jack, note its number, then run `wpctl set-default NUMBER`.

**6. Stop the screen from going black after 10 minutes.** Run `sudo raspi-config`, choose
**Display Options → Screen Blanking**, and select **No**.

**7. Make sure the Pi logs into the desktop by itself.** In `sudo raspi-config`, choose
**System Options → Boot / Auto Login → Desktop Autologin**. The frame draws on that desktop, so it must be
running even though no keyboard or mouse is attached.

**8. Reboot** so the time zone, screen blanking and login changes take effect: `sudo reboot`

---

## Part 2: Use it

Over SSH, on the Pi:

```bash
~/Documents/start_frame.sh     # start
~/Documents/stop_frame.sh      # stop
```

You can close your SSH session after starting; the frame keeps running.

### What you'll see

| On screen | What it means |
|---|---|
| **Waiting on date/time** (center) | The Pi has no clock battery, so after it's switched on it has to get the date from the internet. The frame waits so it never shows the wrong month. This usually takes a few seconds once Wi-Fi connects. |
| **Waiting on Network** (bottom-right corner) | The share can't be reached (Wi-Fi or TrueNAS is down). This month's videos keep playing, and the frame checks the share every 15 seconds. Photos come back on their own when it's reachable. |
| **No pictures or videos found for …** (center) | The share is reachable, but no year has photos or videos for this month. The frame looks again every 10 minutes and at the start of each new month. |

**Adding photos:** new photos you add to the share are picked up automatically. The frame re-reads each year's
folder every time it reaches that year.

**Adding videos or changing settings:** stop the frame, make your changes, start it again.
The frame reads the `videos` folder and the settings only when it starts.

**To check for problems:**

```bash
tail -n 30 ~/Documents/photoframe.log
```

The log records start and stop times, which month and years are being shown, when the share drops and comes
back, and any errors (for example a photo that couldn't be opened or a video that wouldn't play). Bad files
are skipped and logged; the frame keeps going. The log is capped at 5 MB, with one older copy kept as
`photoframe.log.1`.

### Settings (`photoframe.conf`)

| Setting | Default | Meaning |
|---|---|---|
| `picture_seconds` | `8` | Seconds each picture stays on screen (1 to 3600) |
| `picture_mode` | `native` | `native`: pictures shown at their real size, centered on black, and shrunk only if larger than the screen. `fullscreen`: every picture enlarged or shrunk to fill the screen as far as it can while keeping its shape |
| `video_volume` | `70` | Video volume, 0 to 100 |
| `videos_folder` | `videos` | Folder of videos, organized as `<year>/<Month>`. A plain name means a folder inside Documents |
| `wait_for_clock` | `yes` | Wait for the correct date and time before showing anything |
| `server` | `192.168.50.115` | The TrueNAS address (run `setup_share.sh` again after changing it) |
| `share_name` | `dropbox` | The share's name (run `setup_share.sh` again after changing it) |
| `mount_point` | `/mnt/pi-frame-share` | Where the share appears on the Pi |

If a setting is mistyped, the frame uses the default for it and writes an error in the log.

---

## Part 3: Convert and sort videos (on the Linux Mint PC)

Put the videos you want to convert in a folder (subfolders are fine), then run:

```bash
python3 encode_videos.py /path/to/that/folder
```

The converted copies go into a `fixed` folder inside it, **sorted by when each video was recorded**:

```
fixed/
├── 2012/
│   └── November/
│       └── VID_20121103_142233.mp4
├── 2015/
│   └── July/
└── Unsorted/          (videos with no trustworthy date: you sort these)
```

**How it finds the date:**

1. The recording date stored inside the video by the phone or camera (most phones and cameras do this).
2. If there isn't one, a date in the file name, like `VID_20121103_142233.mp4` or `20121103_142233.mp4`.
3. If neither is found, or the date is clearly wrong (like 1970, which some devices write when their clock was
   never set), the video goes into `fixed/Unsorted`.

**Videos in `Unsorted`:** move each one into the right `fixed/<year>/<Month>` folder yourself, creating the
folders if needed. Spell the month in full with a capital letter, for example `fixed/2009/May`. At the end of
each run the script lists the videos still waiting in `Unsorted`, and `encode.log` lists them too. The frame
ignores anything in `Unsorted`.

**Copy the results to the Pi** (stop the frame first). This copies the year folders into `videos` and leaves
out `Unsorted`:

```bash
rsync -av --exclude Unsorted /path/to/that/folder/fixed/ USER@PI-ADDRESS:~/Documents/videos/
```

What the converter does to each video:

- Makes H.264 `.mp4` files, which the Pi 4 decodes in hardware
- Makes every video exactly 1920x1080: the picture is sized to fill the screen without changing its shape,
  and black bars are built into the file. The Pi then never has to resize video while playing
- Keeps the correct shape, including old widescreen DVD-style video and sideways phone video
- Uses a steady frame rate, at most 30 fps (uneven phone and .3gp frame rates can make the Pi stall)
- Removes the "comb" lines from old interlaced camcorder video, only when it detects them
- Converts HDR phone video to normal colors, so it doesn't look washed out
- Evens out the volume so every video plays at about the same loudness
- Handles `.3gp` files too

**Re-running is safe:** videos already converted are skipped, wherever they are in `fixed`. That includes
videos you moved out of `Unsorted`. Add new videos to the folder and run it again.

- **Older versions' output:** files converted by an older version of this script into the top of `fixed` are
  moved into their year/month folder automatically, and re-converted if they aren't 1920x1080.
- **Stopping:** if you press Ctrl+C, the half-finished file is removed and the next run picks up where it
  stopped.
- **Duplicate names:** if two files share a name, the copies are given different names. For example,
  `clip.mp4` and `clip.3gp` become `clip.mp4` and `clip_3gp.mp4`, and `Trip/clip.mp4` and `Party/clip.mp4`
  become `clip_Trip.mp4` and `clip_Party.mp4`.

Errors are written to `encode.log` in the folder you converted. A 4K video can take several minutes per minute
of footage, depending on your PC; the progress line shows the speed.

Settings like quality, frame rate limit, and loudness are at the top of `encode_videos.py` if you ever want to
change them.

---

## Troubleshooting

**"Waiting on date/time" never goes away.** The Pi can't reach the internet to set its clock. Check Wi-Fi, then
run `timedatectl` on the Pi: it should show `System clock synchronized: yes` and `NTP service: active`.
To skip the wait (not recommended), set `wait_for_clock = no`.

**"Waiting on Network" stays on.** Check that TrueNAS is running and the Pi is on Wi-Fi, then run
`ls /mnt/pi-frame-share` on the Pi. It should list the year folders. If it shows an error, run
`sudo ./setup_share.sh` again. It tests the connection and says what went wrong.

**`start_frame.sh` says the frame didn't start, and the log mentions the display.**
The desktop must be logged in on the Pi's screen (see Part 1, step 7). If it is, run `ls /tmp/.X11-unix/` on
the Pi. If you see `X1` instead of `X0`, start the frame with `FRAME_DISPLAY=:1 ~/Documents/start_frame.sh`.

**The wrong month is showing.** Check the time zone (Part 1, step 4) and run `date` on the Pi.

**A video never plays.** It must be inside a year/month folder, like `videos/2012/November/clip.mp4`, with the
month spelled in full. When the frame starts, it logs any videos it's ignoring because they aren't in a
year/month folder.

**No sound.** Recheck Part 1, step 5, and check `video_volume` in the settings. Make sure the speaker is
plugged into the 3.5mm jack, not HDMI.

**Screen goes black after a while.** Screen blanking is still on; see Part 1, step 6, then reboot.

**A video freezes.** The frame recovers by itself:

- If the picture freezes for 10 seconds, or the video runs 20 seconds past its length, the frame skips it.
- If VLC locks up completely for 30 seconds, the frame restarts itself (the screen may flash for a moment)
  and leaves that video out until the next time you start it.

Either way, the log names the video. Run `grep -i video ~/Documents/photoframe.log` to see which files were
skipped. Re-run `encode_videos.py` on their originals and copy the new versions over.

**A specific file never shows.** Check the log; it names any picture or video that was skipped and why.

---

## Later: starting at boot

When you're ready, the frame can be started automatically at login. That isn't set up yet, as requested.
