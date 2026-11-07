"""Rule engine: panel-state conditions -> Telegram messages.

State semantics:
  * A rule starts "armed". It fires whenever its condition is true and the
    cooldown has elapsed.
  * With hysteresis > 0, the rule goes idle after firing and re-arms only
    once the value moves back past the threshold by the deadband
    (for ">"/">=" the value must drop below threshold - hysteresis; for
    "<"/"<=" it must rise above threshold + hysteresis).
  * With hysteresis == 0 the rule stays armed and the cooldown alone limits
    repeat sends.
  * "=="/"!=" have no deadband concept; cooldowns do the throttling.

All rule state is in memory; a bridge restart re-arms every rule.
"""

import operator
import time

from . import telegram

_OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
UP_OPS = (">", ">=")
DOWN_OPS = ("<", "<=")


def _condition_fired(op, value, threshold):
    fn = _OPS.get(op)
    if fn is None:
        return False
    return fn(value, threshold)


def _may_rearm(op, value, threshold, hysteresis):
    """Is the rule past its deadband right now? (True when no deadband.)"""
    if hysteresis <= 0:
        return True
    if op in UP_OPS:
        return value < (threshold - hysteresis)
    if op in DOWN_OPS:
        return value > (threshold + hysteresis)
    return True


def render(message, device, panel, value):
    return (message
            .replace("{device}", device)
            .replace("{panel}", panel)
            .replace("{value}", "%g" % value))


class RuleEngine:
    def __init__(self, store, events):
        self.store = store
        self.events = events
        self._state = {}  # rule_id -> {"armed": bool, "last_fire": float (monotonic)}

    def process(self, board, values, panel_meta, device_name):
        """Evaluate every applicable rule against a freshly polled board.

        Returns a list of (rule_id, ok, detail) for the sends it attempted.
        """
        device = board.get("name") or device_name or board["id"]
        panel_names = {m["id"]: (m.get("name") or m["id"]) for m in (panel_meta or [])}
        # Telegram panels are one-shot notification triggers handled by the
        # board's push transport. Ignore them here so a bridge rule cannot
        # double-send the same alert.
        telegram_panels = {
            m["id"] for m in (panel_meta or []) if m.get("type") == "telegram"
        }
        results = []
        now = time.monotonic()
        for rule in self.store.rules():
            if not rule.get("enabled", True):
                continue
            if rule.get("board_id") and rule["board_id"] != board["id"]:
                continue
            pid = rule.get("panel_id")
            if pid not in values or pid in telegram_panels:
                continue
            value = values[pid]
            try:
                threshold = float(rule.get("threshold", 0))
            except (TypeError, ValueError):
                continue
            op = rule.get("op", ">")
            st = self._state.setdefault(rule["id"], {"armed": True, "last_fire": 0.0})
            try:
                hysteresis = abs(float(rule.get("hysteresis") or 0))
            except (TypeError, ValueError):
                hysteresis = 0.0

            if not st["armed"] and not _may_rearm(op, value, threshold, hysteresis):
                continue
            st["armed"] = True

            if not _condition_fired(op, value, threshold):
                continue

            try:
                cooldown = float(rule.get("cooldown_s") or 300)
            except (TypeError, ValueError):
                cooldown = 300.0
            if now - st["last_fire"] < cooldown:
                continue

            st["last_fire"] = now
            st["armed"] = hysteresis <= 0  # with a deadband, wait for it to clear

            panel_name = panel_names.get(pid, pid)
            message = rule.get("message", "{device}: {panel} = {value}")
            text = render(message, device, panel_name, value)
            ok, detail = telegram.send(
                self.store.settings().get("telegram_token", ""),
                rule.get("chat_id", ""),
                text,
            )
            self.events.add(
                "telegram" if ok else "error",
                "%s.%s %s %g -> %s (value=%g)" % (device, panel_name, op, threshold, detail, value),
            )
            results.append((rule["id"], ok, detail))
        return results
