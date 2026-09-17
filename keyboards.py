from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/status"), KeyboardButton(text="/balance")],
            [KeyboardButton(text="/settings"), KeyboardButton(text="/ssid_status")],
            [KeyboardButton(text="/signal_only"), KeyboardButton(text="/stop_trading")],
            [KeyboardButton(text="/help")],
        ],
        resize_keyboard=True,
    )
