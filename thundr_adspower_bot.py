from __future__ import annotations

import sys

from thundr_bot.config import BotConfig
from thundr_bot.runtime import RuntimeController


def main() -> int:
    config = BotConfig.from_env()

    if not config.user_ids:
        print("No AdsPower user IDs configured. Set ADSPOWER_USER_IDS or edit defaults.")
        return 1

    controller = RuntimeController(config)
    try:
        result = controller.start()
    except (KeyboardInterrupt, InterruptedError):
        # RuntimeController handles stop event and summary writing in finally.
        return 130

    if result.status in {"success", "stopped"}:
        return 0

    print(f"Runtime failed: {result.fatal_error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
