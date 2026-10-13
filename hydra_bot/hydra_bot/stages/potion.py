"""Potion stage: open bag, equip the potion, apply it on the fishing spot.

Reconstruction of ``_execute_potion_use``.
"""
from __future__ import annotations


def execute_potion_use(bot) -> None:
    s = bot.cfg
    bag_key = s.get("hotbar_equipment_bag", "2")
    potion_key = s.get("hotbar_potion", "5")
    bag_delay = float(s.get("potion_bag_delay", 0.1))
    equip_delay = float(s.get("potion_equip_delay", 0.075))

    bot.input.send_key_press(bag_key)
    if bag_delay > 0 and not bot.sleep(bag_delay):
        return
    bot.input.send_key_press(bag_key)
    bot.input.send_key_press(potion_key)
    if equip_delay > 0 and not bot.sleep(equip_delay):
        return

    # use it on the game view at the park point (never on the hotbar)
    click_x, click_y = bot.park_point()
    bot.input.click_at(click_x, click_y, 1)
