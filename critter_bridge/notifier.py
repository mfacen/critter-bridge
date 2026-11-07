"""Board-originated Telegram pushes.

The HTTP request only enqueues the message; the Telegram HTTPS call happens
here so a slow api.telegram.org never blocks the board or the bridge UI.
"""

import logging
import queue
import threading

from . import telegram

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, store, events, maxsize=64):
        self.store = store
        self.events = events
        self._queue = queue.Queue(maxsize=maxsize)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="notifier").start()

    def enqueue(self, text, chat_id="", who="", panel=""):
        self._queue.put_nowait({
            "text": str(text),
            "chat_id": str(chat_id or ""),
            "who": str(who or ""),
            "panel": str(panel or ""),
        })

    def _loop(self):
        while True:
            item = self._queue.get()
            try:
                settings = self.store.settings()
                chat_id = item["chat_id"] or str(settings.get("telegram_chat_id") or "")
                ok, detail = telegram.send(
                    settings.get("telegram_token", ""),
                    chat_id,
                    item["text"],
                )
                who = item["who"] or "board"
                panel = item["panel"] or "?"
                self.events.add(
                    "telegram" if ok else "error",
                    "push %s/%s: %s" % (who, panel, detail),
                )
            except Exception:
                log.exception("telegram push failed")
            finally:
                self._queue.task_done()
