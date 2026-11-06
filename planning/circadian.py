"""Pure timing and scoring for the opt-in sleep policy."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from math import cos, isfinite, pi
from statistics import median


def bounded(value, low, high, default=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return default
    return min(high, max(low, value))


@dataclass(frozen=True)
class NightWindow:
    bedtime: datetime
    wake: datetime
    start: datetime
    latest_start: datetime
    earliest_wake: datetime
    latest_wake: datetime


def night_window(now, sleep_minute, wake_minute, drift):
    """Select the night containing now, or the next one in local wall time."""
    delta = timedelta(minutes=bounded(drift, 0, 90, 45))
    for day in (-1, 0, 1):
        midnight = datetime.combine(now.date() + timedelta(days=day), time.min, now.tzinfo)
        bedtime = midnight + timedelta(minutes=sleep_minute)
        wake = midnight + timedelta(minutes=wake_minute + (1440 if wake_minute <= sleep_minute else 0))
        if now < wake + delta or day == 1:
            return NightWindow(bedtime, wake, bedtime - delta, bedtime + delta,
                               max(bedtime, wake - delta), wake + delta)


def circadian_sleepiness(local_minute, peak_minute):
    return 50 * (1 + cos(2 * pi * (local_minute - peak_minute) / 1440))


def sleep_score(energy, debt, local_minute, peak_minute, commitment_bonus=0,
                interruptibility=1, *, engagement=1, weights=(.50, .30, .20)):
    pressure = 100 * (1 - bounded(energy, 0, 1, 1))
    rhythm = circadian_sleepiness(local_minute, peak_minute)
    fatigue = 100 * bounded(debt, 0, 12) / 12
    parts = {
        "pressure": weights[0] * pressure,
        "circadian": weights[1] * rhythm,
        "debt": weights[2] * fatigue,
        "schedule": bounded(commitment_bonus, 0, 18),
        "activity": -12 * bounded(engagement, 0, 1, 1) * (1 - bounded(interruptibility, 0, 1, 1)),
    }
    return {**{key: round(value, 2) for key, value in parts.items()},
            "score": round(bounded(sum(parts.values()), 0, 100), 2)}


def onset_band(score, previous="awake"):
    if score >= (65 if previous == "winding_down" else 72):
        return "winding_down"
    if score >= (48 if previous in {"drowsy", "winding_down"} else 55):
        return "drowsy"
    return "awake"


def phase_shift(offsets, previous=0):
    """Robust phase correction, capped both per new night and in total."""
    previous = bounded(previous, -30, 30)
    if len(offsets) < 3:
        return int(previous)
    target = bounded(round(median(offsets[-7:]) / 2), -30, 30)
    return int(bounded(target, previous - 15, previous + 15))


def nap_reason(now, woke_at, energy, debt):
    minute = now.hour * 60 + now.minute
    if minute < 13 * 60:
        return "too_early"
    if minute > 15 * 60 + 15:
        return "window_closed"
    if woke_at is None or woke_at.date() != now.date() or woke_at > now:
        return "no_wake"
    if now.timestamp() - woke_at.timestamp() < 3 * 3600:
        return "late_wake"
    energy = bounded(energy, 0, 1, 1)
    fatigue = 80 * (1 - energy) + 20 * bounded(debt, 0, 12) / 12
    return "not_sleepy" if energy >= .75 or fatigue < 40 else "ready"
