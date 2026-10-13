"""Totem stage: detect day/night and pop the appropriate totem item.

Reconstruction of ``_execute_totem_check`` and the per-mode actions. Every
action shares the same "consumable" gesture: open the equipment bag (press
twice), equip the item, then left-click the fishing spot to apply it.
"""
from __future__ import annotations

import time

from .. import perception as P


def _apply_consumable(bot, bag_key, item_key, bag_delay, equip_delay) -> None:
    """bag x2 -> equip item -> click the shake area to use it."""
    bot.input.send_key_press(bag_key)
    if bag_delay > 0 and not bot.sleep(bag_delay):
        return
    bot.input.send_key_press(bag_key)
    bot.input.send_key_press(item_key)
    if equip_delay > 0 and not bot.sleep(equip_delay):
        return
    # use it on the game view at the park point (never on the hotbar)
    click_x, click_y = bot.park_point()
    bot.input.click_at(click_x, click_y, 1)


def execute_totem_check(bot) -> None:
    style = bot.cfg.get("totem_style", "Disabled")
    if style == "Disabled":
        return
    s = bot.cfg
    print(f"[Totem] check mode={style}")

    # Direct modes: no day/night detection, always pop the special totem.
    if style in ("Shiny", "Sparkling", "Mutation"):
        _apply_consumable(
            bot,
            s.get("hotbar_equipment_bag", "2"),
            s.get("hotbar_aurora_totem", "3"),
            float(s.get("totem_direct_bag_delay", 0.1)),
            float(s.get("totem_direct_equip_delay", 0.075)),
        )
        return

    time_of_day = P.detect_day_or_night(
        bot.cfg.totem_box, bot._totem_sun_tmpl, bot._totem_moon_tmpl
    )
    print(f"[Totem] detected: {time_of_day}")

    if style == "Night":
        # Want night; if it's currently day, pop the sundial to force night.
        if time_of_day == "day":
            _night_action(bot)
    elif style == "Day":
        if time_of_day == "night":
            _day_action(bot)
    elif style == "Aurora":
        if time_of_day != "unknown":
            _apply_consumable(
                bot,
                s.get("hotbar_equipment_bag", "2"),
                s.get("hotbar_aurora_totem", "3"),
                float(s.get("totem_aurora_bag_delay", 0.1)),
                float(s.get("totem_aurora_equip_delay", 0.075)),
            )
    elif style == "Rainbow":
        if time_of_day != "unknown":
            _apply_consumable(
                bot,
                s.get("hotbar_equipment_bag", "2"),
                s.get("hotbar_aurora_totem", "3"),
                float(s.get("totem_rainbow_bag_delay", 0.1)),
                float(s.get("totem_rainbow_equip_delay", 0.075)),
            )


def _night_action(bot) -> None:
    s = bot.cfg
    _apply_consumable(
        bot,
        s.get("hotbar_equipment_bag", "2"),
        s.get("hotbar_sundial", "4"),
        float(s.get("totem_night_bag_delay", 0.1)),
        float(s.get("totem_night_equip_delay", 0.075)),
    )


def _day_action(bot) -> None:
    s = bot.cfg
    _apply_consumable(
        bot,
        s.get("hotbar_equipment_bag", "2"),
        s.get("hotbar_mystic_mirror", "7"),
        float(s.get("totem_day_bag_delay", 0.1)),
        float(s.get("totem_day_equip_delay", 0.075)),
    )
