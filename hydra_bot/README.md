# hydra_bot — core rewrite

A clean-room Python rewrite of the **core** of the Hydra fishing macro,
reconstructed from the decompiled `HydraBackend.exe`.

> **What this actually is.** Despite the name, `hydra.exe` is **not** the
> THC-Hydra password cracker. It is a game-automation macro for the Roblox
> fishing game *Fisch* — it reads the screen (MSS / DXcam / YOLO) and sends
> mouse/keyboard input to automate casting, shaking, and reeling. Its only
> network use is optional Discord webhook notifications; there is no
> credential/attack code anywhere in it.

## How the original is built

| File | What it is |
|------|------------|
| `hydra.exe` (8 MB) | **Tauri** (Rust + WebView) GUI front-end |
| `HydraBackend.exe` (296 MB) | **PyInstaller onefile, Python 3.14** — the entire bot (one ~15k-line module). Bundles PyTorch/Ultralytics, OpenCV, MSS, DXcam, pywin32 |
| `AppraiserXBackend.exe` (32 MB) | PyInstaller OCR helper (Tesseract) |
| `YOLO rods/*.pt` | Ultralytics models for the YOLO fish-track style |
| `Detection/{sun,moon}.png` | templates for totem day/night detection |
| `config.json` | ~490 settings + detection boxes |

The GUI runs the backend with `--headless` and drives it over a **localhost
HTTP API on port 9760**. This rewrite reproduces that backend + API.

## What was rebuilt (and what wasn't)

Target language is **Python** — the original already is Python, and the stack
(PyTorch/OpenCV/Tkinter) makes a C++ port pointless. Per the chosen scope this
rewrite covers the **core bot** and **omits the cosmetic layer**:

**Included**
- Main cycle orchestration (`run_bot_cycle`)
- **Cast**: Normal + Perfect, including the Perfect-Cast *Idiotproof* release
  solver (green-bobber tracking + white-fill prediction)
- **Shake**: Pixel mode + the "fish hooked" exit probe
- **Fish (reel)**: the PD control loop for **Color / Line / YOLO** tracking
- **Totem** (day/night template match + item use), **Potion**, **Misc** loadout
- `config.json` loading with per-rod overrides (`<key>_<Rod>`)
- The full **HTTP control API**
- **Discord webhook** notifications (`discord.py`)

**Omitted** (cosmetic / out of scope): the Tk starfield GUI, the in-app
shop/rebirth/passive-income meta-game, auto-reconnect, and the
Circle/Navigation shake styles and Prediction/GigaSigma/Velocity release styles (Pixel + Idiotproof are the
defaults; the others share the same loops and differ only in threshold maths).

## Layout

```
hydra_bot/
  constants.py     Win32 flags, timings, colours (recovered values)
  config.py        Config load/save, per-rod override lookup
  win_input.py     keyboard/mouse via user32 + win32api (Windows only)
  capture.py       MSS / DXcam screen capture -> BGR frames
  detection.py     pixel primitives (green/white/line/colour, velocity)
  perception.py    fish colour/line perception, YOLO, day/night match
  stages/
    cast.py        normal + perfect cast + idiotproof release
    shake.py       pixel shake + exit check
    fish.py        PD reel controller (Color/Line/YOLO)
    totem.py       day/night totem actions
    potion.py      potion use
  bot.py           HydraBot: state + cycle orchestration
  discord.py       Discord webhook notifier (background thread)
  api.py           localhost HTTP control API
  __main__.py      `python -m hydra_bot --headless`
  default_config.json   the recovered defaults
```

Place the `YOLO rods/` and `Detection/` folders next to the `hydra_bot`
package (same layout as the original) if you use YOLO or totem detection.

## GUI / exe (easiest)

```bat
python run_gui.py          :: run the GUI straight from source
build_exe.bat              :: build dist\HydraGUI\HydraGUI.exe (no YOLO)
build_exe.bat full         :: same, but bundles PyTorch/Ultralytics for YOLO
```

The GUI runs the bot in-process and saves every change to `config.json`
(next to the exe). The dark UI mirrors the original Hydra front-end. Tabs:
**Main** (capture mode, hotkey/hotbar Rebind, window prefs, drag-to-select
detection areas, cursor park point, stage toggles, loadout), **Cast** (style + step sequence),
**Shake**, **Fish** (track style, add/delete rods, per-rod colours with a
click-to-pick screen eyedropper, per-rod reel tuning), **Totem** (+ potions),
**Discord** (webhook notifications + test button), **Reconnect** (settings
stored only), **Extra** (reload/reset,
searchable editor for every key, live log). Global hotkeys still work while
Roblox is focused: F3 start/stop, F1 change area, F4 exit (all rebindable).

## Running (headless / API)

The bot targets **Windows + Roblox** (it sends OS input and captures the
screen), exactly like the original. On Windows:

```bash
pip install -r requirements.txt
python -m hydra_bot --headless --config config.json          # API only
python -m hydra_bot --headless --autostart                   # start fishing now
```

Then drive it over HTTP (this is what the Tauri GUI does):

```bash
curl http://127.0.0.1:9760/health
curl http://127.0.0.1:9760/state
curl -X POST http://127.0.0.1:9760/command -d '{"action":"toggle_start_stop"}'
curl -X POST http://127.0.0.1:9760/command -d '{"action":"set_setting","key":"cast_mode","value":"Perfect"}'
```

The pure-logic layer (config, detection, perception, PD control, API) imports
and runs on any OS; only `win_input` and live capture require Windows + a
display.

## HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | liveness |
| GET | `/state` | run state, status, loop/cycle counts, rod |
| GET | `/config` | full config document |
| GET | `/yolo_models` | available `.pt` files |
| GET | `/logs` | recent log lines |
| POST | `/config` | replace + save config document |
| POST | `/command` | run an action (below) |

`/command` actions: `toggle_start_stop`, `set_setting {key,value}`,
`set_gui_setting {key,value}`, `reload_config`, `save_full_config {config}`,
`reset_config`, `quit_app`, `test_webhook` (sends a test message; returns
`{"status": "ok"|"error", "message": ...}`).

## Cursor parking

Casting presses the mouse wherever the cursor is, so a cursor left on the
hotbar selects or unequips items. The bot keeps it on a **park point**
(Main tab → *Cursor Park*) and moves it back there before every cast, before
the Perfect-cast press, after the camera look-drags, and after every fish
stage (all rods). Each move is *verified*: Roblox handles input once a frame
and can put the cursor back where a right-drag started a frame later, so the
bot waits a frame, checks with `GetCursorPos`, and retries.

* **Auto** (default): centred over the shake area, 10% below its top, and
  always at least 12% of the screen height above the reel bar (the hotbar is
  below the reel bar).
* **Custom**: click *Pick* and click anywhere on the game view
  (`cursor_park_x/y`). The area below the reel bar is shaded as a no-go zone.

Totem and potion clicks use the same point. The headless backend now also
enables per-monitor DPI awareness (the GUI already did), so display scaling no
longer shifts cursor positions.

## Discord webhooks

Turn on in the **Discord** tab and paste a channel webhook URL
(*Channel settings → Integrations → Webhooks*). Messages are embeds with the
rod, loops, fish caught, cycles, runtime, fish/hour and current status; the
*Screenshot* style also attaches a capture of the screen.

| Setting | Effect |
|---------|--------|
| `discord_enabled`, `webhook_url` | on/off and destination |
| `discord_message_style` | `Screenshot` or `Text` |
| `loops_screenshot` / `loops_text` | report every N loops for that style (0 = off) |
| `discord_status_interval_minutes` | periodic status while running (0 = off) |
| `discord_notify_start_stop` | message on start / stop / exit |
| `discord_notify_errors` | message when a cycle crashes (max once a minute) |
| `discord_ping_user_id` | user to @mention on errors |

Sending happens on a background thread, so the bot never waits on the network;
rate limits (HTTP 429) are retried. **Send Test** (or the `test_webhook` API
action) posts one message and reports the result. The shipped defaults have no
webhook and notifications off.

## Rod specials (`stages/rod_specials.py`)

Some rods add a mechanic on top of the normal reel. These were worked out from
gameplay recordings and hook into the reel loop (toggle each on the Fish tab
when that rod is selected; `rod_specials_enabled` turns them all off):

| Rod | What happens in-game | What the bot does |
|-----|----------------------|-------------------|
| **Pinions Aria** | Music notes fall straight down onto the bar (~1.4 s); catching one with the bar raises progress speed | Finds notes above the bar by `pinions_note_color_1`, steers the bar under the lowest one while keeping the fish inside the bar (`pinions_note_threshold` = max chase distance) |
| **Halibut Harpoon** | Harpoon volleys (black arrows) and red slashes cross the bar | Only a tall vertical line counts as the fish line; ignores the box borders and big jumps, and holds the last position through slashes (`halibut_*`) |
| **Stellarwave Melody** | Zodiac signs scroll through the bar; touching them in order (♈ ♉ ♊ …) lights the stars above | Reads the lit-star count, cuts the next sign from the top row, finds it in the bar by template matching and moves the cursor onto it (+ click) (`stellarwave_*`) |

## Overlays (`overlay.py`)

The three overlay switches on the Main tab (GUI Settings) draw indicators over
Roblox while the bot runs: a **status pill** (stage, hold/release, error,
counts), a **fishing overlay** (bar brackets, fish/bar/steering markers on a
rail under the bar, HOLD/RELEASE, plus Pinions note, Halibut "line hidden" and
Stellarwave sign-aim markers) and a **perfect-cast gauge** (fill, predicted
fill, release threshold). The window is click-through and excluded from screen
capture so the bot never sees its own drawings; where capture exclusion isn't
supported only the status pill is shown.

## Reel control law (recovered)

```
error        = target_x - bar_center_x
bar_velocity = (bar_center_x - last_bar_x) / dt
damping      = 2.0 if (|error| shrinking and bar moving toward target) else 0.5
control      = clamp(kp*error + kd*bar_velocity*damping, -pd_clamp, +pd_clamp)
hold_mouse   = control > 0
```
with far-left → force release and far-right → force hold. Gains `kp/kd/pd_clamp`
and scan FPS are per-rod (`fish_kp_<Rod>` etc.).

## Legal / ToS note

Game automation typically violates a game's Terms of Service and can get an
account banned. This reconstruction is for interoperability and understanding
of software in your possession. Use it responsibly and at your own risk.
