"""Transport modes Entur surfaces, mapped to our friendlier vocabulary.

Reference data — exposed at GET /modes for free.
"""

MODES: list[dict] = [
    {"code": "train", "label": "Train", "entur": "rail"},
    {"code": "bus", "label": "Bus", "entur": "bus"},
    {"code": "tram", "label": "Tram", "entur": "tram"},
    {"code": "metro", "label": "Metro", "entur": "metro"},
    {"code": "ferry", "label": "Ferry", "entur": "water"},
    {"code": "air", "label": "Flight", "entur": "air"},
    {"code": "coach", "label": "Coach", "entur": "coach"},
    {"code": "funicular", "label": "Funicular", "entur": "funicular"},
    {"code": "lift", "label": "Cable lift / chairlift", "entur": "lift"},
    {"code": "cableway", "label": "Aerial cableway", "entur": "cableway"},
    {"code": "walk", "label": "Walking leg", "entur": "foot"},
    {"code": "bike", "label": "Bicycle leg", "entur": "bicycle"},
    {"code": "scooter", "label": "Scooter leg", "entur": "scooter"},
]


def all_modes() -> list[dict]:
    return MODES
