"""Deterministic, topic-aware visual themes for lesson stages."""
import hashlib
import re


_ARCHETYPES = (
    (("space", "planet", "star", "astronomy", "orbit", "galaxy", "moon"), "space", ("astronaut", "diver", "robot", "explorer"), 225),
    (("ai", "artificial intelligence", "neural", "machine learning", "robot", "data"), "neural", ("robot", "engineer", "inventor"), 185),
    (("chemistry", "chemical", "molecule", "atom", "reaction", "laboratory", "lab"), "laboratory", ("scientist", "inventor", "engineer"), 145),
    (("photosynthesis", "plant", "biology", "cell", "nature", "ecosystem", "animal"), "nature", ("naturalist", "explorer", "scientist"), 105),
    (("ocean", "water", "sea", "wave", "marine"), "ocean", ("diver", "explorer", "astronaut"), 195),
    (("math", "equation", "geometry", "number", "algebra", "physics", "newton"), "blueprint", ("inventor", "engineer", "robot"), 265),
    (("code", "python", "program", "loop", "algorithm", "binary", "software"), "terminal", ("engineer", "robot", "inventor"), 35),
)


def theme_for(topic, lesson_key=""):
    normalized = re.sub(r"\s+", " ", topic.strip().casefold())
    digest = hashlib.sha256(f"{normalized}|{lesson_key}".encode("utf-8")).hexdigest()
    archetype, character_options, base_hue = "studio", ("teacher", "explorer", "naturalist"), 285
    for words, candidate, roles, hue in _ARCHETYPES:
        if any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in words):
            archetype, character_options, base_hue = candidate, roles, hue
            break
    character = character_options[int(digest[14:16], 16) % len(character_options)]
    hue = (base_hue + int(digest[:4], 16) % 70 - 35) % 360
    accent_hue = (hue + 36 + int(digest[4:8], 16) % 28) % 360
    secondary_hue = (hue + 170 + int(digest[8:12], 16) % 35) % 360
    return {
        "id": digest[:16],
        "archetype": archetype,
        "character": character,
        "motion": int(digest[12:14], 16) % 4,
        "palette": {
            "background": f"hsl({hue} 42% 10%)",
            "surface": f"hsl({hue} 38% 17%)",
            "grid": f"hsl({hue} 35% 29%)",
            "accent": f"hsl({accent_hue} 88% 65%)",
            "secondary": f"hsl({secondary_hue} 76% 68%)",
            "glow": f"hsl({hue} 70% 30%)",
            "character": f"hsl({accent_hue} 75% 55%)",
            "characterShade": f"hsl({accent_hue} 70% 38%)",
        },
    }
