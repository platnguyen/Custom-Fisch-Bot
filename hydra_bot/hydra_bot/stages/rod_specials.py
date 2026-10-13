"""Rod-specific minigame mechanics, worked out from gameplay recordings.

Some rods add something on top of the normal "keep the bar on the fish" reel.
Each special hooks into the reel loop in ``stages/fish.py``:

* ``filter_target(frame, target_x, now)`` - clean up the perceived target line
* ``tick(now, is_holding)``               - side actions (extra captures/clicks)
* ``adjust_target(target_x, bar_left, bar_right, now)`` - steer the bar

All x values handed to/returned from the hooks are relative to the fish box,
like the rest of the reel loop.

Pinions Aria
    Music notes fall straight down onto the reel bar (fixed x, ~1.4 s from
    spawn to landing). Catching a note with the player bar raises progress
    speed. While a note is falling the bar is steered under it, but never so
    far that the fish line leaves the bar.

Halibut Harpoon
    Harpoon volleys (black arrows) and red slash effects fly across the bar.
    The arrows match the dark target-line colour and drag the target away;
    the slashes briefly hide the line. Only full-height vertical runs count as
    the target line, and the last good position is held through short gaps.

Stellarwave Melody
    The 12 zodiac signs are shown in order above the bar, with a star under
    each. Signs scroll through the bar in a shuffled order; touching the next
    sign in sequence lights its star. The next sign's glyph is cut from the top
    row, found in the bar by template matching, and the cursor is moved onto it
    (with a click) while the reel keeps running.

Pure detection helpers are module-level functions so they can be checked
against recorded frames without a live game.
"""
from __future__ import annotations

import os
import re
import time
from typing import List, Optional, Tuple

import numpy as np

from .. import detection as D
from ..capture import Capture, screen_size

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def color_mask(frame, hex_color, tolerance):
    bgr = D.hex_to_bgr(hex_color)
    if bgr is None:
        return None
    target = np.array(bgr, dtype=np.int16)
    lower = np.clip(target - tolerance, 0, 255).astype(np.uint8)
    upper = np.clip(target + tolerance, 0, 255).astype(np.uint8)
    return cv2.inRange(frame, lower, upper)


def _runs(flags, max_gap=0):
    """Contiguous True runs as (start, end) inclusive, merging gaps <= max_gap."""
    runs: List[Tuple[int, int]] = []
    start = None
    for i, v in enumerate(list(flags) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if runs and start - runs[-1][1] - 1 <= max_gap:
                runs[-1] = (runs[-1][0], i - 1)
            else:
                runs.append((start, i - 1))
            start = None
    return runs


def filter_line_target(frame, hex_color, tolerance, last_x=None, min_height=0.45,
                       max_jump=None):
    """x of a tall vertical line of ``hex_color`` (closest to last_x).

    Diagonal or short shapes of the same colour (projectiles) and the box's own
    left/right borders are ignored. With ``last_x`` and ``max_jump``, a line
    further than ``max_jump`` away doesn't count (returns None).
    """
    mask = color_mask(frame, hex_color, tolerance)
    if mask is None:
        return None
    h, w = mask.shape[:2]
    counts = (mask > 0).sum(axis=0)
    cols = counts >= max(2, int(h * min_height))
    edge = max(3, w // 100)
    runs = [r for r in _runs(cols, max_gap=2) if r[0] >= edge and r[1] < w - edge]
    if not runs:
        return None
    centers = [(a + b) / 2.0 for a, b in runs]
    if last_x is None:
        # strongest line wins when there's no history
        best = max(range(len(runs)), key=lambda i: counts[runs[i][0]:runs[i][1] + 1].sum())
        return centers[best]
    x = min(centers, key=lambda c: abs(c - last_x))
    if max_jump is not None and abs(x - last_x) > max_jump:
        return None
    return x


def find_falling_notes(frame, hex_color, tolerance, min_area):
    """[(center_x, bottom_y, area)] of note-coloured blobs in ``frame``."""
    mask = color_mask(frame, hex_color, tolerance)
    if mask is None:
        return []
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(mask)
    notes = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area >= min_area:
            notes.append((x + w / 2.0, y + h, int(area)))
    return notes


def find_zodiac_row(region, k=1.0):
    """Locate the top row of 12 zodiac glyphs in ``region``.

    Returns (band_top, band_bottom, [(x1, x2)] * 12) or None.
    """
    bright = region.max(axis=2) > 200
    row_counts = bright.sum(axis=1)
    for top, bottom in _runs(row_counts >= int(20 * k), max_gap=2):
        height = bottom - top + 1
        if not (15 * k <= height <= 50 * k):
            continue
        cols = bright[top:bottom + 1].any(axis=0)
        segs = _runs(cols, max_gap=int(8 * k))
        segs = [s for s in segs if s[1] - s[0] >= 6 * k]
        if len(segs) != 12:
            continue
        centers = np.array([(a + b) / 2.0 for a, b in segs])
        gaps = np.diff(centers)
        if gaps.min() > 0 and gaps.std() < 0.2 * gaps.mean():
            return top, bottom, segs
    return None


def count_lit_stars(region, band_top, band_bottom, segs, k=1.0):
    """Stars sit ~53px (at 1080p) below the glyph row; lit ones are white."""
    star_y = int((band_top + band_bottom) / 2 + 53 * k)
    r = max(2, int(4 * k))
    if star_y + r >= region.shape[0]:
        return None
    lit = 0
    for a, b in segs:
        cx = (a + b) // 2
        win = region[star_y - r:star_y + r + 1, cx - r:cx + r + 1]
        if win.size and (win.min(axis=2) > 215).mean() > 0.5:
            lit += 1
        else:
            break
    return lit


def _grad(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mag = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    return cv2.GaussianBlur(mag, (5, 5), 0)


def glyph_candidates(search, template, scales, n=6, spacing=None):
    """Up to ``n`` best (score, scale, (cx, cy)) spots for ``template`` in ``search``.

    Spots are at least ``spacing`` px apart in x (default: template width at the
    smallest scale), best first. Flat patches and already-collected (white)
    glyphs are skipped.
    """
    sg = _grad(search)
    tg = _grad(template)
    white = (search.min(axis=2) > 225).astype(np.float32)
    w = sg.shape[1]
    col_score = np.full(w, -1.0, np.float32)
    col_y = np.zeros(w, np.int32)
    col_scale = np.zeros(w, np.float32)
    for sc in scales:
        t = cv2.resize(tg, None, fx=sc, fy=sc, interpolation=cv2.INTER_LINEAR)
        th, tw = t.shape[:2]
        if th >= sg.shape[0] or tw >= sg.shape[1]:
            continue
        res = cv2.matchTemplate(sg, t, cv2.TM_CCOEFF_NORMED)
        rows = slice(th // 2, th // 2 + res.shape[0])
        cols = slice(tw // 2, tw // 2 + res.shape[1])
        # flat patches (e.g. the empty progress bar) give a bogus 1.0
        mean = cv2.boxFilter(sg, -1, (tw, th), normalize=True)
        sq = cv2.boxFilter(sg * sg, -1, (tw, th), normalize=True)
        res[(sq - mean * mean)[rows, cols] < 0.01 * float(t.var())] = -1.0
        # collected signs turn white; never aim at those
        res[cv2.boxFilter(white, -1, (tw, th), normalize=True)[rows, cols] > 0.08] = -1.0
        best_y = res.argmax(axis=0)
        best = res[best_y, np.arange(res.shape[1])]
        xs = np.arange(res.shape[1]) + tw // 2
        better = best > col_score[xs]
        col_score[xs[better]] = best[better]
        col_y[xs[better]] = best_y[better] + th // 2
        col_scale[xs[better]] = sc
    if spacing is None:
        spacing = max(10, int(template.shape[1] * min(scales)))
    out = []
    left = col_score.copy()
    while len(out) < n:
        x = int(left.argmax())
        if left[x] <= -1.0:
            break
        out.append((float(left[x]), float(col_scale[x]), (x, int(col_y[x]))))
        left[max(0, x - spacing):x + spacing + 1] = -1.0
    return out


def match_glyph(search, template, scales):
    """Best (score, scale, (cx, cy)) of ``template`` inside ``search`` (BGR)."""
    found = glyph_candidates(search, template, scales, n=1)
    return found[0] if found else (-1.0, None, None)


class ZodiacYolo:
    """YOLO model for the zodiac signs in the Stellarwave reel bar.

    Trained on the bar strip (1240x124 at 1080p: fish-box centre +-620 px wide,
    fish box y1-45 .. y2+45 tall); classes are ZODIAC in order.
    """

    MODEL = "Stellarwave Signs.pt"

    def __init__(self):
        import warnings
        from ultralytics import YOLO  # raises if not installed
        from ..perception import _resource_dir

        path = os.path.join(_resource_dir("YOLO rods"), self.MODEL)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.model = YOLO(path, verbose=False)

    def detect(self, band, confidence, k=1.0):
        """[(class index, confidence, cx, cy)] in ``band`` pixel coordinates."""
        img = band
        if abs(k - 1.0) > 0.02:  # the model was trained on 1080p strips
            img = cv2.resize(band, None, fx=1.0 / k, fy=1.0 / k, interpolation=cv2.INTER_AREA)
        res = self.model.predict(img, conf=confidence, imgsz=640, verbose=False)
        out = []
        if res:
            for box in res[0].boxes:
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                out.append((int(box.cls[0]), float(box.conf[0]),
                            float(x1 + x2) / 2 * k, float(y1 + y2) / 2 * k))
        return out


# ---------------------------------------------------------------------------
# Specials
# ---------------------------------------------------------------------------
class RodSpecial:
    name = ""
    enable_key = ""

    def __init__(self, bot, box):
        self.bot = bot
        self.cfg = bot.cfg
        self.box = box
        try:
            self.screen_w, self.screen_h = screen_size()
        except Exception:
            self.screen_w, self.screen_h = 1920, getattr(bot, "screen_height", 1080)
        self.k = self.screen_h / 1080.0  # Roblox UI scales with screen height
        self._caps: list = []

    def _capture(self, region):
        cap = Capture(region, use_mss=True)
        self._caps.append(cap)
        return cap

    def _clip(self, x1, y1, x2, y2):
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(self.screen_w, int(x2)), min(self.screen_h, int(y2))
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}

    def filter_target(self, frame, target_x, now):
        return target_x

    def tick(self, now, is_holding):
        pass

    def adjust_target(self, target_x, bar_left, bar_right, now):
        return target_x

    def close(self):
        for cap in self._caps:
            cap.close()


class PinionsAria(RodSpecial):
    name = "Pinions Aria"
    enable_key = "pinions_note_chase_enabled"
    SCAN_INTERVAL = 0.03
    NOTE_MEMORY = 0.25  # keep chasing this long after the note was last seen

    def __init__(self, bot, box):
        super().__init__(bot, box)
        s = self.cfg
        self.color = s.get("pinions_note_color_1", "#b2cfff")
        self.tol = int(s.get("pinions_note_tolerance", 10))
        self.max_dist = float(s.get("pinions_note_threshold", 800)) * self.k
        self.min_area = int(s.get("pinions_note_min_area", 60)) * self.k * self.k
        # everything above the bar, where notes fall
        self.region = self._clip(box["x1"] - 40 * self.k, box["y1"] - 830 * self.k,
                                 box["x2"] + 40 * self.k, box["y1"] - 6 * self.k)
        self.cap = self._capture(self.region)
        self.fish_x = None
        self.note_x = None
        self.note_seen = 0.0
        self._last_scan = 0.0

    def tick(self, now, is_holding):
        if now - self._last_scan < self.SCAN_INTERVAL:
            return
        self._last_scan = now
        try:
            frame = self.cap.grab()
        except Exception:
            return
        if frame is None:
            return
        notes = find_falling_notes(frame, self.color, self.tol, self.min_area)
        offset = self.region["x1"] - self.box["x1"]
        best = None
        for cx, bottom, _area in notes:
            x = cx + offset
            if self.fish_x is not None and abs(x - self.fish_x) > self.max_dist:
                continue
            if best is None or bottom > best[1]:  # the one closest to landing
                best = (x, bottom)
        if best is not None:
            if self.note_x is None:
                print(f"[Pinions] note falling at x={best[0]:.0f}")
            self.note_x = best[0]
            self.note_seen = now

    def filter_target(self, frame, target_x, now):
        if target_x is not None:
            self.fish_x = target_x
        return target_x

    def adjust_target(self, target_x, bar_left, bar_right, now):
        if self.note_x is None:
            return target_x
        if now - self.note_seen > self.NOTE_MEMORY:
            self.note_x = None  # landed (or missed)
            return target_x
        if bar_left is None or bar_right is None:
            return target_x
        half = abs(bar_right - bar_left) / 2.0
        reach = max(0.0, half - max(6.0, 0.2 * half))  # keep the fish inside the bar
        return min(max(self.note_x, target_x - reach), target_x + reach)


class HalibutHarpoon(RodSpecial):
    name = "Halibut Harpoon"
    enable_key = "halibut_harpoon_filter_enabled"

    def __init__(self, bot, box):
        super().__init__(bot, box)
        s = self.cfg
        self.color = s.rod_color("fish_target_line_color")
        self.tol = s.rod_tolerance("fish_target_line_tolerance")
        self.min_height = float(s.get("halibut_line_min_height", 0.45))
        self.hold = float(s.get("halibut_target_hold", 0.35))
        self.max_jump = float(s.get("halibut_max_jump", 60)) * self.k
        self.last_x = None
        self.last_seen = 0.0
        self.line_hidden = False

    def filter_target(self, frame, target_x, now):
        recent = self.last_x is not None and now - self.last_seen <= self.hold
        # while the line is recent, ignore far-away candidates; after that, re-acquire
        x = filter_line_target(frame, self.color, self.tol,
                               self.last_x if recent else None, self.min_height,
                               self.max_jump if recent else None)
        if x is not None:
            self.last_x = x
            self.last_seen = now
            self.line_hidden = False
            return x
        if recent:
            self.line_hidden = True
            return self.last_x  # line hidden by a slash / harpoon flash
        return None


ZODIAC = ("Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo", "Libra", "Scorpio",
          "Sagittarius", "Capricorn", "Aquarius", "Pisces")


class StellarwaveMelody(RodSpecial):
    name = "Stellarwave Melody"
    enable_key = "stellarwave_zodiac_enabled"
    SCAN_INTERVAL = 0.12
    RETRY_AFTER = 0.8  # re-aim if the star didn't light
    SIGNS_ONLY_RETRY = 0.5  # signs-only mode keeps re-aiming until it lights
    ROW_GONE_AFTER = 2.0  # signs-only: minigame counts as over once the row is gone this long
    TOUCH_TIME = 0.08
    HOVER_TIME = 0.04  # signs-only: wait after moving onto a sign before pressing
    PRESS_TIME = 0.05  # signs-only: how long the button is held down
    MISS_MEMORY = 3.0  # signs-only: skip a spot that didn't light the star for this long
    MISS_RADIUS = 30  # px (1080p) around a missed spot that's skipped
    SIGNS_ONLY_MIN_SCORE = 0.35  # misses get retried elsewhere, so accept weaker matches  # stay on the sign this long so the game registers it

    def __init__(self, bot, box):
        super().__init__(bot, box)
        s = self.cfg
        k = self.k
        cx = (box["x1"] + box["x2"]) / 2.0
        self.region = self._clip(cx - 620 * k, box["y1"] - 240 * k,
                                 cx + 620 * k, box["y2"] + 60 * k)
        self.cap = self._capture(self.region)
        self.click = bool(s.get("stellarwave_click_signs", True))
        self.threshold = float(s.get("stellarwave_match_threshold", 0.55))
        # clicks allowed per sign (1 = click once, never retry the same sign)
        self.max_clicks = max(1, int(s.get("stellarwave_max_clicks", 1)))
        # skip the bar steering in the reel loop and only click signs
        self.signs_only = bool(s.get("stellarwave_signs_only", True))
        self._clicks = {}  # sign index -> clicks sent
        self._scales = list(np.arange(1.3, 2.31, 0.05))
        self._scale = None
        self._last_scan = 0.0
        self._aimed = None  # (sign index, time)
        self._aim_pos = None  # screen (x, y) of the last aim
        self._misses = []  # [(sign index, x, y, time)] spots that didn't light the star
        self.yolo = None
        self.yolo_conf = float(s.get("stellarwave_yolo_confidence", 0.35))
        if s.get("stellarwave_use_yolo", True):
            try:
                self.yolo = ZodiacYolo()
                print("[Stellarwave] using YOLO sign detector")
            except Exception as e:
                print(f"[Stellarwave] YOLO sign detector unavailable ({e}); "
                      "using template matching")
        self._parked = False
        self._last_lit = None
        self._last_why = 0.0
        self.row_seen = time.time()  # last time the zodiac row was on screen
        print(f"[Stellarwave] signs_only={self.signs_only} click={self.click} "
              f"threshold={self.threshold} region={self.region} k={self.k:.2f}")
        self.park = bot.park_point()

    def close(self):
        # the cursor may still be resting on a sign near the bottom of the
        # screen; the next cast clicks wherever it is, which can hit the hotbar
        try:
            self.bot.input.set_cursor(*self.park)
        except Exception:
            pass
        super().close()

    def minigame_visible(self, now):
        """True while the zodiac row was seen recently (the minigame is still up)."""
        return now - self.row_seen < self.ROW_GONE_AFTER

    def _why(self, now, msg):
        """Say why no click happened, at most once a second."""
        if now - self._last_why >= 1.0:
            self._last_why = now
            print(f"[Stellarwave] {msg}")

    def tick(self, now, is_holding):
        if now - self._last_scan < self.SCAN_INTERVAL:
            return
        self._last_scan = now
        try:
            region = self.cap.grab()
        except Exception as e:
            self._why(now, f"capture failed: {e}")
            return
        if region is None:
            self._why(now, "capture returned nothing")
            return
        k = self.k
        row = find_zodiac_row(region[: int(200 * k)], k)
        if row is None:
            self._why(now, "zodiac row not found")
            return
        top, bottom, segs = row
        self.row_seen = now
        lit = count_lit_stars(region, top, bottom, segs, k)
        if lit is None or lit >= 12:
            self._why(now, f"lit stars = {lit}")
            return
        if lit != self._last_lit:
            if self._last_lit is not None:
                print(f"[Stellarwave] {ZODIAC[lit - 1]} collected ({lit}/12)")
            self._last_lit = lit
        if not self.signs_only and self._clicks.get(lit, 0) >= self.max_clicks:
            return  # already clicked this sign as many times as allowed
        if self._aimed and self._aimed[0] == lit:
            retry = self.SIGNS_ONLY_RETRY if self.signs_only else self.RETRY_AFTER
            if now - self._aimed[1] < retry:
                return
            if not self._parked:
                if self.signs_only and self._aim_pos is not None:
                    # wrong spot: try the next-best candidate for this sign
                    self._misses.append((lit, *self._aim_pos, now))
                    print(f"[Stellarwave] {ZODIAC[lit]} didn't light at {self._aim_pos}, "
                          "trying another spot")
                # our own cursor may be covering the sign; move it off and look again
                self.bot.input.set_cursor(*self.park)
                self._parked = True
                return

        a, b = segs[lit]
        pad = max(2, int(4 * k))
        template = region[max(0, top - 2):bottom + 3, max(0, a - pad):b + pad + 1]
        # only the reel bar's height: the progress bar below it confuses matching
        y0 = max(0, int(self.box["y1"] - self.region["y1"] - 45 * k))
        y1 = int(self.box["y2"] - self.region["y1"] + 45 * k)
        search = region[y0:y1]
        scales = self._scales if self._scale is None else [
            s for s in self._scales if abs(s - self._scale) <= 0.101]
        if self.yolo is not None:
            try:
                dets = self.yolo.detect(search, self.yolo_conf, k)
            except Exception as e:
                self._why(now, f"YOLO failed: {e}")
                return
            found = sorted(((c, None, (int(cx), int(cy))) for cls, c, cx, cy in dets
                            if cls == lit), reverse=True)
        else:
            found = glyph_candidates(search, template, scales)
        if self.signs_only:
            self._misses = [m for m in self._misses if now - m[3] < self.MISS_MEMORY]
            r = self.MISS_RADIUS * k
            ox, oy = self.region["x1"], self.region["y1"] + y0
            found = [c for c in found if not any(
                m[0] == lit and abs(ox + c[2][0] - m[1]) < r and abs(oy + c[2][1] - m[2]) < r
                for m in self._misses)]
            min_score = min(self.threshold, self.SIGNS_ONLY_MIN_SCORE)
        else:
            min_score = self.threshold
        if self.yolo is not None:
            min_score = self.yolo_conf  # already filtered by the model
        score, scale, pos = found[0] if found else (-1.0, None, None)
        if pos is None or score < min_score:
            self._why(now, f"{ZODIAC[lit]} not found (best score {score:.2f})")
            return
        if scale is not None and score >= 0.6:
            self._scale = scale
        x = self.region["x1"] + pos[0]
        y = self.region["y1"] + y0 + pos[1]
        print(f"[Stellarwave] aiming at {ZODIAC[lit]} ({x}, {y}) score {score:.2f}")
        inp = self.bot.input
        inp.set_cursor(int(x), int(y))
        if self.signs_only:
            # Roblox reads input once a frame: let it see the cursor over the sign
            # (hover) for a couple of frames before pressing, and hold the press
            # long enough to span a frame, or the click is missed
            time.sleep(self.HOVER_TIME)
            inp.set_cursor(int(x), int(y))
            time.sleep(self.HOVER_TIME)
            if self.click:
                if is_holding:
                    inp.left_up()
                    time.sleep(self.PRESS_TIME)
                inp.left_down()
                time.sleep(self.PRESS_TIME)
                inp.left_up()
            time.sleep(self.TOUCH_TIME)
        elif self.click:
            # a quick click that leaves the reel's hold state as it was
            if is_holding:
                inp.left_up()
                time.sleep(0.02)
                inp.left_down()
            else:
                inp.left_down()
                time.sleep(0.02)
                inp.left_up()
            # move off the sign straight away so the reel's own holds/taps
            # ("Click & Hold Anywhere") don't keep clicking it
            inp.set_cursor(*self.park)
        self._clicks[lit] = self._clicks.get(lit, 0) + 1
        self._aimed = (lit, now)
        self._aim_pos = (int(x), int(y))
        self._parked = False
        overlay = getattr(self.bot, "overlay", None)
        if overlay is not None:
            overlay["zodiac"] = {"t": time.time(), "x": int(x), "y": int(y),
                                 "sign": ZODIAC[lit], "n": lit}


SPECIALS = (
    (re.compile(r"pinions?aria"), PinionsAria),
    (re.compile(r"halibut"), HalibutHarpoon),
    (re.compile(r"stellarwave"), StellarwaveMelody),
)


def special_for_rod(rod: str):
    """The RodSpecial class for a rod name (case/spacing-insensitive), or None."""
    key = re.sub(r"[^a-z]", "", (rod or "").lower())
    for pattern, cls in SPECIALS:
        if pattern.search(key):
            return cls
    return None


def create(bot, box) -> Optional[RodSpecial]:
    cls = special_for_rod(bot.cfg.current_rod())
    if cls is None or not bot.cfg.get(cls.enable_key, True):
        return None
    try:
        special = cls(bot, box)
    except Exception as e:
        print(f" {cls.name} special unavailable: {e}")
        return None
    print(f" Rod special active: {cls.name}")
    return special
