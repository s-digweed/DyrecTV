#!/usr/bin/env python3
"""Enrich the Toonami Aftermath + Snickelodeon channels in TOONAMIAM.xml with
real descriptions and season/episode numbers.

Strategy (option B):
  - normalize the on-screen title to a canonical series name (ALIAS),
  - if the programme has an episode title (<sub-title>), match it to a
    season/episode via the series' episode list (TVmaze/TVDB name maps),
    then attach that EPISODE's synopsis + <episode-num>,
  - otherwise fall back to the SHOW-level synopsis.

Sources: TMDB (key) -> TVmaze (keyless) -> TheTVDB (key). Everything is cached
in toonami_desc_cache.json so repeat runs don't re-hit the APIs. The engine is
lifted from the Whiplash generator. Only `requests` is required.

Usage:  python3 enrich_toonami.py <path-to-TOONAMIAM.xml>
Leaves MTV97 and every other channel untouched.
"""

import os, re, sys, json, time, html, bisect, difflib
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import requests
import xml.etree.ElementTree as ET

# ── channels we enrich: xmltv id -> (API scheduleName, stream delay minutes) ──
# East/West share one scheduleName; West is just the East feed delayed 180 min.
CH_MAP = {
    "ToonamiAftermath.us@East": ("Toonami Aftermath EST", 0),
    "ToonamiAftermath.us@West": ("Toonami Aftermath EST", 180),
    "Snickelodeon EST":         ("Snickelodeon EST", 0),
    "Snickelodeon EST+180":     ("Snickelodeon EST", 180),
}
TARGET_CHANNELS = set(CH_MAP)
API_ENDPOINT = "https://api.toonamiaftermath.com"
MATCH_TOL_S  = 120          # air-time match tolerance when recovering episodeNumber

# ── on-screen title -> canonical series name for lookups ──
ALIAS = {
    # Toonami
    "DBZ": "Dragon Ball Z",
    "Dragonball": "Dragon Ball",
    "Dexters Laboratory": "Dexter's Laboratory",
    "Ed Edd Eddy": "Ed, Edd n Eddy",
    "Space Ghost C2C": "Space Ghost Coast to Coast",
    "Full Metal Alchemist": "Fullmetal Alchemist",
    "Powerpuff Girls": "The Powerpuff Girls",
    "Jonny Quest Real Adventures": "The Real Adventures of Jonny Quest",
    "What A Cartoon! Show": "What a Cartoon!",
    "Dr Katz": "Dr. Katz, Professional Therapist",
    "Ranma": "Ranma ½",
    "Thundercats": "ThunderCats",
    "Reboot": "ReBoot",
    "Tick": "The Tick",
    "Batman": "Batman: The Animated Series",
    "Superman": "Superman: The Animated Series",
    "Spiderman": "Spider-Man (1967)",
    "Spider-Man": "Spider-Man: The Animated Series",
    "X-Men": "X-Men: The Animated Series",
    "Men in Black": "Men in Black: The Series",
    "Pokemon": "Pokémon",
    "Nadia - Secret of Blue Water": "Nadia: The Secret of Blue Water",
    "Record of Lodoss War TV": "Record of Lodoss War",
    "Yu Yu Hakusho": "Yu Yu Hakusho",
    "Lupin III": "Lupin the Third",
    # Snickelodeon
    "Rockos Modern Life": "Rocko's Modern Life",
    "Ren and Stimpy": "The Ren & Stimpy Show",
    "Pete and Pete": "The Adventures of Pete & Pete",
    "Grimms Fairy Tale Classics": "Grimm's Fairy Tale Classics",
    "The Littl Bits": "The Littl' Bits",
    "Allegras Window": "Allegra's Window",
    "Secret World Of Alex Mack": "The Secret World of Alex Mack",
    "Car 54, Where Are You": "Car 54, Where Are You?",
    "Mr Wizard": "Mr. Wizard's World",
    "Rugrats": "Rugrats",
    "Spider-Man (1967)": "Spider-Man (1967)",   # identity (used as a lookup key)
}

# ── per-title overrides: display name, lookup name, fixed desc, year field ──
# "display": on-screen <title>  |  "lookup": name used for metadata search
# "desc": pinned description (skips lookup)  |  "date": year -> <date> (movies/specials)
# "no_se": never attach season/episode (movies, or series we have no S/E for)
_D = "Dante journeys through the nine circles of Hell -- limbo, lust, gluttony, greed, anger, heresy, violence, fraud and treachery -- in search of his true love, Beatrice."
TITLE_OVERRIDES = {
    # display the classic series under its dub title, but keep the DB lookup on
    # "Saint Seiya" so the synopsis doesn't come from the 2003 reboot.
    "Saint Seiya": {"display": "Saint Seiya: Knights of the Zodiac", "lookup": "Saint Seiya"},
    # David the Gnome: show it under the short title, but query the DBs under its
    # full catalogued name so episode lookups actually match.
    "David The Gnome": {"display": "David the Gnome", "lookup": "The World of David the Gnome"},
    # ── movies / long specials (get a <date> year) ──
    "Little Giants": {
        "display": "Little Giants (1994)", "lookup": "Little Giants (1994)", "date": "1994",
        "no_se": True,
        "desc": "Misfits form their own opposing team to an elite peewee football team, "
                "coached by the elite team coach's brother."},
    "Dantes Inferno": {
        "display": "Dante's Inferno: An Animated Epic (2010)",
        "lookup": "Dante's Inferno: An Animated Epic", "date": "2010", "no_se": True, "desc": _D},
    "Scooby-Doo and The Legend Of The Vampire": {
        "display": "Scooby-Doo! and the Legend of the Vampire (2003)",
        "lookup": "Scooby-Doo! and the Legend of the Vampire", "date": "2003", "no_se": True,
        "desc": "The Mystery Gang travels to Australia for a vacation and attends a massive rock "
                "music festival near Vampire Rock. A legendary vampire creature known as the Yowie "
                "Yahoo begins kidnapping musical performers, prompting Scooby and the crew to solve "
                "the mystery."},
    "Bugs Bunnys Halloween Hijinks": {
        "display": "Bugs Bunny's Halloween Hijinks (2000)",
        "lookup": "Bugs Bunny's Halloween Hijinks", "date": "2000", "no_se": True},
    "The Dark Crystal": {
        "display": "The Dark Crystal (1982)", "lookup": "The Dark Crystal", "date": "1982",
        "no_se": True,
        "desc": "On another planet in the distant past, the last of the Gelfling race embarks on a "
                "quest to find the missing shard of a magical crystal and to restore order to his world."},
    "Interstella 5555": {
        "display": "Interstella 5555: The 5tory of the 5ecret 5tar 5ystem (2003)",
        "lookup": "Interstella 5555: The 5tory of the 5ecret 5tar 5ystem", "date": "2003",
        "no_se": True},
    # ── series (year in the title for distinction; no <date>) ──
    "Land of the Lost 1991": {
        "display": "Land of the Lost (1991)", "lookup": "Land of the Lost (1991)", "no_se": True,
        "desc": "Tom and his two teen children, Kevin and younger sister Annie, find themselves "
                "trapped in a parallel universe when their jeep falls into the time portal while "
                "exploring the countryside; together, they must learn to survive."},
    "The Tomorrow People": {
        "display": "The Tomorrow People (1992)", "lookup": "The Tomorrow People (1992)", "no_se": True,
        "desc": "The Tomorrow People are the next evolutionary stage of humans with abilities like "
                "teleportation, telepathy, and healing. Aided by an ancient spacecraft, they use "
                "their powers to protect the world while keeping their existence secret."},
    "Fist Of The North Star": {
        "display": "Fist of the North Star", "lookup": "Fist of the North Star (1984)", "no_se": True,
        "desc": "After a nuclear war turns Earth into a lawless wasteland, Kenshiro, a practitioner "
                "of the deadly martial art \"Hokuto Shinken\", fights a succession of tyrannical "
                "warriors to restore order."},
    "Super Sloppy Double Dare": {
        "display": "Super Sloppy Double Dare", "lookup": "Super Sloppy Double Dare", "no_se": True,
        "desc": "On your mark, get set, go! Join four contestants as they answer questions and take "
                "on messy physical challenges (like running in a giant hamster wheel, popping "
                "balloons filled with shaving cream, and more) for the chance to win Super Sloppy "
                "Double Dare!"},
    # ── series with S/E kept, just display/lookup fixes ──
    "Spiderman":  {"display": "Spider-Man (1967)", "lookup": "Spider-Man (1967)"},
    "Spider-Man": {"display": "Spider-Man: The Animated Series",
                   "lookup": "Spider-Man: The Animated Series"},
}

# pinned season/episode for specific (lookup-show, normalized episode name) pairs
EPISODE_SE_OVERRIDE = {
    ("Spider-Man: The Animated Series", "six forgotten warriors chapter 2 unclaimed legacy"): (5, 3),
    ("Spider-Man: The Animated Series", "six forgotten warriors chapter 4 the six fight again"): (5, 5),
}

# ── MANUAL S/E PINS ─────────────────────────────────────────────────────────
# Fill S/E for a specific (show, episode-name) that the matcher can't resolve
# confidently (old live-action with messy DB numbering, etc). These ALWAYS win.
# Key = (lookup show name, episode sub-title); matched case/punctuation-insensitively.
# Grab the names to add here from the generated `missing_se.txt` report.
#   e.g. ("Dragnet", "The Bank Jobs"): (2, 7),
SE_PINS = {
    # --- batch 3: from missing_se_5 (your fills) ---
    ('2 Stupid Dogs', 'Scirocco Mole'): (1, 9),
    ('Dragon Ball', 'Bulma and Son Goku'): (1, 1),
    ('Dragon Ball', 'What the...?! No Balls!'): (1, 2),
    ('Fullmetal Alchemist', 'Sealing the Homunculus'): (1, 47),
    ('Lupin the Third: Part II', 'ZenigataCon'): (1, 10),
    ('Sailor Moon', "Blinded By Love's Light"): (3, 6),
    ('Sailor Moon', 'Swept Off Her Feet'): (3, 5),
    ('Saint Seiya: Knights of the Zodiac',
     "Kidnapped! Corvus' Army Calls Unexpectedly on Saori"): (1, 29),
    ('Zoids: New Century', 'Frightday the 13th - Ready Ahhh'): (1, 14),
    ('Dragon Ball Z Abridged',
     'Arrival of Fear!! Salute, Ginyu Special Squadron!!'): (2, 9),
    # --- batch 5: from missing_se_7 ---
    ('The Brak Show', 'Fued'): (2, 6),
    ('Digimon Adventure', "Wizardmon's Gift by"): (1, 37),
    ('Digimon Adventure', "Wizardmon's Gift"): (1, 37),
    ('Dragon Ball', 'Yamcha, The Strong Yet Cruel Desert Bandit'): (1, 5),
    ('Fullmetal Alchemist', 'Goodbye'): (1, 48),
    ('Initial D: First Stage', 'Battle to the Limit! Eight-Six Versus GT-R'): (1, 9),
    ('Iron Man', 'Ultimo, Ultimo Lives, Crescendo'): (1, 3),
    ('Lupin the Third: Part II', 'The Sleight Before Christmas'): (1, 12),
    ('Lupin the Third: Part II', "Who's Vroomin' Who?"): (1, 11),
    ('Neon Genesis Evangelion', 'Tears'): (1, 23),
    ('Rurouni Kenshin', 'The Wolf Destroys the Eye of the Heart'): (2, 22),
    ('Sailor Moon', 'Damp Spirits'): (3, 8),
    ('Sailor Moon', 'Friendly Foes'): (3, 9),
    ('Sailor Moon', 'Lita Borrows Trouble'): (3, 7),
    ('Saint Seiya: Knights of the Zodiac', 'Dragon! Victory of Self-Sacrifice'): (1, 28),
    ('Saint Seiya: Knights of the Zodiac', 'Stone Seiya! Shield of Medusa'): (1, 27),
    ('The Tick', 'Ants in Pants!'): (2, 9),
    ('Yu-Gi-Oh!', 'The Dark Spirit Revealed (Part 3)'): (3, 35),
    ('Android Kikaider', 'The End of the Dream (Finale)'): (1, 13),
    ('Batman: The Animated Series', "The Joker's Favor"): (1, 7),
    ('Digimon Adventure', 'City Under Siege'): (1, 36),
    ('Digimon Adventure', 'Flower Power'): (1, 35),
    ('Dragon Ball Z', 'A Heavy Burden'): (2, 18),
    ('Dragon Ball Z', 'Arrival of The Ginyu Force'): (2, 22),
    ('Dragon Ball Z', 'Big Trouble for Bulma'): (2, 20),
    ('Dragon Ball Z', 'Elite Fighters of The Universe..'): (2, 23),
    ('Dragon Ball Z', 'Enter Goku'): (2, 26),
    ('Dragon Ball Z', 'Get Vegeta!!'): (2, 16),
    ('Dragon Ball Z', 'Goku... Super Saiyan?'): (2, 27),
    ('Dragon Ball Z', 'Immortality Denied'): (2, 19),
    ('Dragon Ball Z', 'No Refuge from Recoome'): (2, 25),
    ('Dragon Ball Z', 'Scramble for the Dragon Balls'): (2, 21),
    ('Dragon Ball Z', 'Time Tricks and Body Binds'): (2, 24),
    ('Dragon Ball Z', 'Vegeta Revived'): (2, 17),
    ('Fantastic Four', 'Danger In The Depths'): (1, 12),
    ('Fighting Spirit', 'A Step Further (Finale)'): (1, 75),
    ('Fullmetal Alchemist', 'A Rotted Heart'): (1, 45),
    ('Mobile Suit Gundam Wing', 'Passing Destinies (Recap)'): (1, 28),
    ('Mobile Suit Gundam Wing', 'The Locus of Victory and Defeat (Recap)'): (1, 27),
    ('I Am Weasel', 'I Am Cliched'): (4, 2),
    ('Initial D: First Stage', 'Conclusion! Dogfight!'): (1, 5),
    ('Lupin the Third: Part II', 'Cursed Case Scenario'): (1, 7),
    ('Lupin the Third: Part II', 'Disorient Express'): (1, 8),
    ('Lupin the Third: Part II', "Now Museum, Now You Don't"): (1, 9),
    ('Nadia: The Secret of Blue Water', 'The Little Fugitives'): (1, 2),
    ('Neon Genesis Evangelion', 'At Least, Be Human'): (1, 22),
    ('Neon Genesis Evangelion', 'The Birth of NERV'): (1, 21),
    ('Pokémon', 'Ash Catches a Pokemon'): (1, 3),
    ('Pokémon', 'Pokemon Emergency!'): (1, 2),
    ('Rurouni Kenshin', 'Crash! The Lethal Punch: The Fist of Sonosuke Screams!'): (2, 20),
    ('Sailor Moon', 'Bad Harmony'): (3, 4),
    ('Sailor Moon', 'Crystal Clear Again'): (3, 2),
    ('Sailor Moon', 'Diamond In The Rough'): (2, 40),
    ('Sailor Moon', 'Driving Dangerously'): (3, 3),
    ('Sailor Moon', 'Final Battle'): (2, 41),
    ('Sailor Moon', 'Follow The Leader'): (2, 42),
    ('Sailor Moon', 'Star Struck, Bad Luck'): (3, 1),
    ('Saint Seiya', 'Dragon! Victory of Self-Sacrifice'): (1, 28),
    ('Saint Seiya', 'Stone Seiya! Shield of Medusa'): (1, 27),
    ('Scooby Doo, Where Are You!', 'A Tiki Scare is No Fair'): (2, 6),
    ('Scooby Doo, Where Are You!', "Don't Fool with a Phantom"): (2, 8),
    ('Scooby Doo, Where Are You!', 'Haunted House Hang-Up'): (2, 5),
    ('Scooby Doo, Where Are You!', "Who's Afraid of the Big Bad Werewolf?"): (2, 7),
    ('Static Shock', 'She-Bang'): (3, 4),
    ('Superman: The Animated Series', 'Little Girl Lost (Part 2)'): (2, 28),
    ('The Mighty Thor', 'The Grey Gargoyle, The Wrath of Odin, Triumph in Stone'): (1, 7),
    ('The Real Adventures of Jonny Quest', 'In the Wake of the Mary Celeste'): (1, 14),
    ('The Tick', 'Evil Sits Down for a Moment'): (2, 7),
    ('The Tick', 'Heroes'): (2, 8),
    ('X-Men: The Animated Series', 'Sanctuary (Part 1)'): (4, 6),
    ('X-Men: The Animated Series', 'Sanctuary (Part 2)'): (4, 7),
    ('Yu-Gi-Oh!', 'The Dark Spirit Revealed (Part 1)'): (2, 33),
    ('Yu-Gi-Oh!', 'The Dark Spirit Revealed (Part 2)'): (2, 34),
    ('Zoids: New Century', 'The Brave Wild Eagle - The Raynos vs. The Zabat'): (1, 13),
    ('Zoids: New Century', 'The Sensational Three - Rematch with Jack Sisco'): (1, 11),
    ('Zoids: New Century', 'Zero is Stolen - The Fiery Battle'): (1, 12),
    # --- batch 2 (user corrections) ---
    ('Fifteen (Hillside)', 'Free Falling'): (1, 5),
    ('Flipper (1995)', 'Dolphin in Pursuit: Part 2'): (2, 3),
    ('Nickelodeon GUTS', 'Rebecca - Cam - Oliver'): (1, 38),
    ('Noozles', 'Run Away from Home'): (1, 21),
    ('Adventures of Superman', 'The Talking Clue'): (3, 2),
    ('Adventures of Superman', 'Through the Time Barrier'): (3, 1),
    ('The Adventures of Rocky and Bullwinkle and Friends', 'Many a Thousand Gone, or The Haul of Fame/Down to Earth, or Me and My Shatter'): (2, 39),
    ('The Adventures of Rocky and Bullwinkle and Friends', 'Hop Skip and Junk, or Bullwinkle\'s Big Tow/Bucks for Boris, or The Green Paper Caper'): (2, 40),
    ('The Adventures of Rocky and Bullwinkle and Friends', 'When Moose Meets Moose, or Two\'s a Crowd/The Midnight Chew-Chew, or This Gum for Hire'): (2, 41),
    ('The Adventures of Tintin', 'Land of Black Gold: Part 1'): (2, 10),
    ('The Adventures of Tintin', 'Land of Black Gold: Part 2'): (2, 11),
}

# Episode sub-title corrections (accents, split-separators, alternate names).
# Keyed by (display show, feed sub-title); applied to the shown <sub-title>.
SUBTITLE_OVERRIDE = {
    ("I Am Weasel", "I Am Cliched"): "I Am Clichéd",
    ("Pokémon", "Pokemon Emergency!"): "Pokémon Emergency!",
    ("The Mighty Thor", "The Grey Gargoyle, The Wrath of Odin, Triumph in Stone"):
        "The Grey Gargoyle / The Wrath of Odin / Triumph in Stone",
    ("Neon Genesis Evangelion", "At Least, Be Human"): "At Least, Be Human (Don't Be)",
    ("Rurouni Kenshin", "Crash! The Lethal Punch: The Fist of Sonosuke Screams!"):
        "Crash! The Lethal Punch, Futae No Kiwami: The Fist of Sonosuke Screams!",
    ("Zoids: New Century", "The Sensational Three - Rematch with Jack Sisco"):
        "The Sensational Three: Rematch with Jack Cisco",
    # --- batch 2: give placeholder "Episode #x.y" slots their real names ---
    ("All That", "Episode #2.19"): "Shai",
    ("All That", "Episode #2.20"): "IV Xample",
    ("Flipper (1995)", "Episode #3.21"): "The Wish",
    ("You Can't Do That on Television (1979)", "Episode #1.7"): "St. Patrick's Day",
    # --- batch 3: segment triplet + alternate episode name ---
    ("2 Stupid Dogs", "Scirocco Mole"): "Hollywood's Ark / Scirocco Mole / Trash Day",
    ("Lupin the Third: Part II", "ZenigataCon"): "Steal File M123 (ZenigataCon)",
    # --- batch 5 ---
    ("Iron Man", "Ultimo, Ultimo Lives, Crescendo"): "Ultimo / Ultimo Lives / Crescendo",
    ("Rurouni Kenshin", "The Wolf Destroys the Eye of the Heart"):
        "The Wolf Destroys the Eye of the Heart: The Fierce Attack of the Zero Stance Gatotsu",
}
def _subtitle_override(show, sub):
    if not sub:
        return None
    want = _norm(sub)
    for (s, fsub), repl in SUBTITLE_OVERRIDE.items():
        if s == show and _norm(fsub) == want:
            return repl
    return None

# Per-episode description overrides (hand-supplied), keyed by (show, feed sub).
# Wins over DB/show-level descriptions. Use for episodes the DBs lack.
DESC_OVERRIDE = {
    ("All That", "Episode #2.19"): "The cast plays a card trick on Josh; when a rabid elephant "
        "is on the loose, Ed is hit with a tranquilizer while elephant hunters are searching Good Burger.",
    ("All That", "Episode #2.20"): "Cold Bear Open; Good Booo-ger; Vital Information; Earboy on "
        "Trial; Did You Hear...; Peter & Flem.",
    ("Flipper (1995)", "Episode #3.21"): "A dying girl's one wish is to swim with a dolphin.",
    ("You Can't Do That on Television (1979)", "Episode #1.7"): "On St. Patrick's Day, amid disco-dancing "
        "finalists, call-in contests, and community announcements, Lisa sets out to get Bradfield "
        "wearin' green--slime, that is.",
    # --- batch 3 ---
    ("2 Stupid Dogs", "Scirocco Mole"): "On a TV game show, Secret and Morocco recall how they "
        "first met and challenged Sirocco Mole, Morocco's evil twin brother.",
    # --- batch 4 ---
    ("Dragon Ball Z Abridged", "Arrival of Fear!! Salute, Ginyu Special Squadron!!"):
        "The newly formed team three-star race off to try to gather the dragon balls before the "
        "Ginyu Force can find them. Unfortunately, their gambit does not pay off and they are "
        "forced to fight The Ginyu's. Gohan and Krillin are faced with the formidable Guldo whose "
        "psychic powers present a considerable threat -- can they take out this deadly foe?",
}
def _desc_override(show, sub):
    if not sub:
        return None
    want = _norm(sub)
    for (s, fsub), repl in DESC_OVERRIDE.items():
        if s == show and _norm(fsub) == want:
            return repl
    return None

# "Episode #2.19" / "Episode 2x19" placeholders encode the season/episode -> decode it.
_EP_PLACEHOLDER = re.compile(r"^\s*episode\s*#?\s*(\d+)\s*[.x]\s*(\d+)\s*$", re.I)
def _decode_placeholder_se(name):
    m = _EP_PLACEHOLDER.match(name or "")
    return (int(m.group(1)), int(m.group(2))) if m else None
def _se_pin(show, epname):
    if not epname:
        return None
    want = _norm(epname)
    for (s, sub), se in SE_PINS.items():
        if s == show and _norm(sub) == want:
            return se
    return None

# pinned show-level descriptions (consulted first in show_overview)
SHOW_DESC_OVERRIDE = {
    "RiffTrax": "Feature films and short subjects presented with comedic running commentary -- "
                "packed with jokes, asides, and relentless riffing from start to finish.",
    # Pinned '60s cartoons: episode synopses aren't reliably available and a bare
    # name lookup risks the wrong same-named series, so use a fixed series blurb.
    "Spider-Man (1967)": "The classic 1967 animated series following Peter Parker, a teenage "
        "photographer who battles a rogues' gallery of super-villains as the web-slinging hero "
        "Spider-Man.",
    "Hulk": "Animated adventures of Dr. Bruce Banner, who becomes the raging, super-strong Hulk "
        "whenever his temper flares -- from the 1966 Marvel Super Heroes cartoon.",
    "Birdman and the Galaxy Trio": "Hanna-Barbera's 1967 superhero cartoon: solar-powered Birdman "
        "fights evil for the agency Inter-Nation Security, paired with the space-faring Galaxy Trio.",
    # --- batch 4: show-level blurbs you supplied (fallback when no episode synopsis) ---
    # game / variety
    "Make the Grade": "Kids attempt to test their knowledge in a matter of mixed learning.",
    "What Would You Do?": "Ninety episodes of this Nickelodeon show were produced 1991 to 1993. "
        "Audience members were asked to volunteer to perform funny stunts, e.g. kissing a "
        "chimpanzee et cetera.",
    "Fifteen (Hillside)": "Students at the fictional Hillside School deal with a variety of issues, "
        "such as friendship, dating, divorce, and alcohol abuse.",
    "Legends of the Hidden Temple (1993)": "Six teams compete for the chance to search for the "
        "treasure inside the titular temple.",
    "Mr. Wizard's World": "Mr. Wizard and his young friends conduct a variety of science experiments.",
    "Wild & Crazy Kids": "Teams of kids compete against each other in a variety of physical "
        "competitions and sports.",
    # cartoons
    "Gumby Adventures": "The continuous adventures of Gumby and his pals. This time, he runs a farm "
        "which includes more pals such as a wooly mammoth, Denali, and a bee, Groobee.",
    "Lassie (1994)": "When a family of 4 moves from Baltimore to a farm in rural Virginia, they "
        "adopt an abandoned collie. The dog becomes the son's companion and protector, helping him "
        "adapt to rural life.",
    "KaBlam!": "An animated anthology show hosted by two kids who live in a comic book.",
    "Alvin & the Chipmunks": "Three chipmunk brothers named Alvin, Simon, and Theodore have been "
        "adopted by and are living with Dave (human). Each episode finds them getting into trouble "
        "and new and unusual situations.",
    "Animorphs": "Five teenagers and an alien with the ability to turn into any beast they touch "
        "vs. an army of parasitic aliens who are slowly infiltrating Earth.",
    "Beetlejuice (1989)": "Beetlejuice, a deceased con-man, travels along with his best friend "
        "Lydia and embarks on adventures in both the Neitherworld and the real world.",
    "David the Gnome": "The fantastic adventures of David and his wife Lisa traveling around the "
        "world to save the animals and defeating the trolls.",
    "Fireball XL5": "In 2062, Colonel Steve Zodiac of the World Space Patrol and the crew of his "
        "spaceship, Fireball XL5, explore Sector 25 of the galaxy, encountering friendly and "
        "hostile aliens along the way.",
    "Grimm's Fairy Tale Classics": "An animated series retelling a different folk or fairy tale in "
        "each episode.",
    "Maya": "The story of a young bee named Maya and her adventures.",
    "The Angry Beavers": "Brothers Daggett and Norbert Beaver have left home to gain independence "
        "by living on their own. Their goal is to live a wild bachelor lifestyle but, as might be "
        "expected from young brothers, they get into some weird situations.",
    "CatDog": "The life and times of a cat and a dog with a unique twist: they're connected, "
        "literally. Adding to their dilemma is Cat's annoyance with Dog, mainly caused by Dog's "
        "stupidity and Cat's up-tight personality.",
    "The Flintstones Meet Rockula and Frankenstone (1979)": "The Flintstones and Rubbles win a "
        "trip on \"Make a Deal or Don't\" to Count Rockula's castle in Rocksylvania where they "
        "have an unpleasant meeting with the Count and his servant Frankenstone.",
    "Dragon Ball Z Abridged: Celloween": "Krillin dreams he saves a mother and child from a giant "
        "Imperfect Cell in a parody-filled horror nightmare, only to wake up right before the "
        "androids arrive.",
}

# ── on-screen DISPLAY name fixes (feed title -> canonical). Used for both the
#    shown <title> and the metadata lookup; S/E handling is unchanged. ──
DISPLAY_CANON = {
    # Toonami
    "Batman": "Batman: The Animated Series",
    "Superman": "Superman: The Animated Series",
    "X-Men": "X-Men: The Animated Series",
    "Men in Black": "Men in Black: The Series",
    "DBZ": "Dragon Ball Z",
    "DBZ Abridged": "Dragon Ball Z Abridged",
    "DBZ Abridged - Celloween": "Dragon Ball Z Abridged: Celloween",
    "Dragonball": "Dragon Ball",
    "Dr Katz": "Dr. Katz, Professional Therapist",
    "Ed Edd Eddy": "Ed, Edd n Eddy",
    "Full Metal Alchemist": "Fullmetal Alchemist",
    "Gundam 08th MS Team": "Mobile Suit Gundam: The 08th MS Team",
    "Jonny Quest Real Adventures": "The Real Adventures of Jonny Quest",
    "Lupin III": "Lupin the Third: Part II",
    "Nadia - Secret of Blue Water": "Nadia: The Secret of Blue Water",
    "Pokemon": "Pokémon",
    "Powerpuff Girls": "The Powerpuff Girls",
    "Ranma": "Ranma ½",
    "Reboot": "ReBoot",
    "Record of Lodoss War TV": "Record of Lodoss War",
    "Scooby Doo": "Scooby Doo, Where Are You!",
    "Scooby-Doo": "Scooby Doo, Where Are You!",
    "Digimon": "Digimon Adventure (1999)",
    "Digimon Adventure": "Digimon Adventure (1999)",
    "Gundam Wing": "Mobile Suit Gundam Wing",
    "Initial D": "Initial D: First Stage",
    "Zoids": "Zoids: New Century",
    "Flipper: The New Adventures": "Flipper",
    "Global Guts": "Nickelodeon GUTS",
    "Tintin": "The Adventures of Tintin",
    "Space Ghost C2C": "Space Ghost Coast to Coast",
    "Thundercats": "ThunderCats",
    "Tick": "The Tick",
    "Yu Yu Hakusho": "YuYu Hakusho",
    "Birdman": "Birdman and the Galaxy Trio",
    # Snickelodeon
    "Allegras Window": "Allegra's Window",
    "Are You Afraid Of The Dark": "Are You Afraid of the Dark?",
    "BeetleJuice": "Beetlejuice (1989)",
    "Busy World of Richard Scarry": "The Busy World of Richard Scarry",
    "Car 54, Where Are You": "Car 54, Where Are You?",
    "Figure it Out Wild Style": "Figure It Out: Wild Style",
    "Flipper The New Adventures": "Flipper: The New Adventures",
    "Grimms Fairy Tale Classics": "Grimm's Fairy Tale Classics",
    "I Dream Of Jeannie": "I Dream of Jeannie",
    "Make The Grade": "Make the Grade",
    "Mysterious Cities of Gold": "The Mysterious Cities of Gold",
    "Pete and Pete": "The Adventures of Pete & Pete",
    "Ren and Stimpy": "The Ren & Stimpy Show",
    "Rockos Modern Life": "Rocko's Modern Life",
    "Rocky and Bullwinkle": "The Rocky and Bullwinkle Show",
    "Secret World Of Alex Mack": "The Secret World of Alex Mack",
    "Space 1999": "Space: 1999",
    "The Littl Bits": "The Littl' Bits",
    "What Would You Do": "What Would You Do?",
    # --- batch 4: display renames you asked for ---
    "Gumby": "Gumby Adventures",
    "Lassie": "Lassie (1954)",
    "Alvin and the Chipmunks": "Alvin and the Chipmunks (1983)",
    "Alvin & the Chipmunks": "Alvin and the Chipmunks (1983)",
    "Maya the Bee": "Maya the Bee (1975)",
    "Angry Beavers": "The Angry Beavers",
    "The Flintstones Meet Rockula And Frankenstone":
        "The Flintstones Meet Rockula and Frankenstone (1979)",
    "Fifteen": "Fifteen (Hillside)",
    "Legends of the Hidden Temple": "Legends of the Hidden Temple (1993)",
    "Mr Wizard": "Mr. Wizard's World",
    "Wild and Crazy Kids": "Wild & Crazy Kids",
    # --- batch 5: from the filled master-map sheet + missing_se_7 ---
    "Brak Show": "The Brak Show",
    "BeetleJuice": "Beetlejuice",
    "Are You Afraid Of The Dark": "Are You Afraid of the Dark? (1999)",
    "Dragnet": "Dragnet (1967)",
    "Figure it Out Wild Style": "Figure It Out",
    "Finders Keepers": "Finders Keepers (1987)",
    "Flipper": "Flipper (1964)",
    "Flipper The New Adventures": "Flipper (1995)",
    "Flipper: The New Adventures": "Flipper (1995)",
    "Get Smart": "Get Smart (1965)",
    "Get The Picture": "Get the Picture (1991)",
    "Nickelodeon Guts": "Nickelodeon GUTS",
    "Gullah Gullah Island": "Gullah Gullah Island",
    "Gullah, Gullah Island": "Gullah Gullah Island",
    "Figure it Out": "Figure It Out",
    "Hey Dude!": "Hey Dude",
    "I Spy": "I Spy (1965)",
    "Muppet Babies": "Muppet Babies (1984)",
    "Rocky and Bullwinkle": "The Adventures of Rocky and Bullwinkle and Friends",
    "Sgt. Bilko": "The Phil Silvers Show",
    "Stingray": "Stingray (1964)",
    "The Adventures of Superman": "Adventures of Superman",
    "Welcome Back Kotter": "Welcome Back, Kotter",
    "What Would You Do": "What Would You Do? (1991)",
    "You Can't Do That on Television": "You Can't Do That on Television (1979)",
}

# Segment anthologies: the feed lists 2-3 whole segment titles comma-separated,
# so for THESE shows a ", " is a separator -> normalize it to " / " (we can't do
# this globally because most shows have real commas inside a single title). Keyed
# by the DISPLAY name. Add shows here as you hit more.
SEGMENT_SHOWS = {
    "Iron Man", "The Mighty Thor", "Hulk", "Captain America", "Fantastic Four",
    "The Incredible Hulk", "Sub-Mariner", "The Marvel Super Heroes",
    "Spider-Man (1967)", "Space Ghost",
}

# Shows whose seasons have their own names -> display "<show> - <season name>",
# chosen by the episode's resolved season. Extend as needed.
SEASON_TITLE = {
    "Rurouni Kenshin": {1: "Wandering Samurai", 2: "Legend of Kyoto", 3: "Tales of Meiji"},
    # Sailor Moon (DiC/Cloverway arc names) — season 1 keeps the bare title; 2-5
    # show "Sailor Moon - <arc>". The feed/TAM supply the episode season.
    "Sailor Moon": {2: "R", 3: "S", 4: "SuperS", 5: "Sailor Stars"},
}

# Force a subtitle for a specific (show, season, ep) — e.g. the DiC English
# names for the Sailor Moon season-1 finale two-parter.
EP_SE_SUBTITLE = {
    ("Sailor Moon", 1, 45): "Day of Destiny",
    ("Sailor Moon", 1, 46): "Brand New Life",
}

# ── episode PINS from IMDb for segment-based '66/'67 cartoons. The feed numbers
#    each SEGMENT sequentially, which is wrong; these are the real broadcast
#    episodes (2-3 segments each), matched fuzzily by segment name. Each pin now
#    also carries the IMDb synopsis ('' when IMDb had none -> series blurb). ──
#
# CURATED_SHOWS never get a metadata-API / absolute-number S/E guess: their S/E
# comes only from PINNED_RAW and their description only from the pin's synopsis
# or the fixed series blurb. (A per-(show,S,E) API lookup matches the WRONG
# same-named series -- the 1994 Spider-Man, the 1978/1996 Hulk -- and attaches a
# wrong-series synopsis; that was the original Spider-Man '67 bug.)
CURATED_SHOWS = {"Spider-Man (1967)", "Hulk", "Birdman and the Galaxy Trio"}
# ====================================================================
# AUTO-GENERATED from the user's IMDb .mht files (gen_pins.py / gen_birdman.py).
# PINNED_RAW: (season, episode, 'segment/segment', 'synopsis') per show.
#   Spider-Man (1967): broadcast episodes, 3 seasons (S1 20, S2 19, S3 13).
#   Hulk: 13 broadcast triples (the feed airs 3 comma-separated segments).
#   Birdman and the Galaxy Trio: S1 broadcast triplets E1-E20 (organized).
# BIRDMAN_SEG_SYN (below) holds per-SEGMENT synopses for Birdman's individual
# segments; since the Birdman feed airs one segment at a time, that segment's own
# synopsis is preferred over the triplet's. A segment not in any triplet gets no
# S/E (the "unknowns"); a segment with no synopsis anywhere gets the series blurb.
# ====================================================================
PINNED_RAW = {
    "Spider-Man (1967)": [
        (1, 1, 'The Power of Dr. Octopus/Sub-Zero for Spidey', 'Out in the countryside, Peter escapes an accident with the help of his Spider-Man disguise, but discovers a cave, with Dr.Octopus in charge of some power machines due to rule the World. Doc Ock captures Spidey, then Miss Brant who happened to be in the area. The villain is keeping both prisoners as witnesses for his demonstration of power.'),
        (1, 2, 'Where Crawls the Lizard/Electro the Human Lightning Bolt', 'Spider-Man faces Electro, a man capable to shoot lightning rods with his hand and traveling on electric lines to go to his next heist.'),
        (1, 3, 'The Menace of Mysterio', 'After framing Spider-Man, Mysterio offers to kill the superhero for J. Jonah Jameson for a fee.'),
        (1, 4, 'The Sky Is Falling/Captured by J. Jonah Jameson', 'Spider-Man fights the Vulture who is controlling a massive flock of birds. In the second segment, the Daily Bugle publisher hunts Spider-Man with a remote-controlled robot.'),
        (1, 5, 'Never Step on a Scorpion/Sands of Crime', "The Scorpion, a super-human creation designed to vanquish Spider-Man, has a lethal sting in his tail and he's ready to sting J. Jonah Jameson. The Sandman heists the priceless Goliath Diamond and Spider-Man is the fall guy for the crime."),
        (1, 6, 'Diet of Destruction/The Witching Hour', "The Green Goblin tries to make a pact with J.Jonah Jameson in order to catch Spider-Man. However, Spidey must beware of his foe's witching powers."),
        (1, 7, 'The Kilowatt Kaper/The Peril of Parafino', 'Escaped from jail, Electro is trying to find, then trap Spiderman for revenge.'),
        (1, 8, 'Horn of the Rhino', "A bad case of the common cold complicates Spider-Man's efforts to stop The Rhino from stealing the components of a secret weapon."),
        (1, 9, 'The One-Eyed Idol/Fifth Avenue Phantom', 'An exotic African idol is used to hypnotize Jameson into embezzling for a villain. A mysterious hooded figure is using robotic store mannequins to commit robberies.'),
        (1, 10, 'The Revenge of Dr. Magneto/The Sinister Prime Minister', 'Spider-Man must stop a magnetism wielding mad scientist. Spidey must rescue a foreign prime minister that only he knows is kidnapped by an imposter.'),
        (1, 11, 'The Night of the Villains/Here Comes Trubble', 'A series of heists are made by mythical creatures, ruled by a woman who holds a library. Spider-Man must put a stop to this before it goes too far.'),
        (1, 12, 'Spider-Man Meets Dr. Noah Boddy/The Fantastic Fakir', 'A fakir is giving a hard time to Spider-Man, especially that he is a clever magician who can send spells to animals to attack the hero.'),
        (1, 13, 'Return of the Flying Dutchman/Farewell Performance', 'Trying to foil a mystery keeping the demolition of a playhouse, Spider-Man meets Blackwell the Magician who, behind his mischievous tricks, has a message to say to him.'),
        (1, 14, 'The Golden Rhino/Blueprint for Crime', "The Rhino sets a gold standard for his crime spree - by creating a statue of himself. Then, a mastermind uses a cow-boy and a thug to find blueprints to destroy New York. It's up to Spider-Man to foil and neutralize their evil plan."),
        (1, 15, 'The Spider and the Fly/The Slippery Dr. Von Schlick', 'Spider-Man must stop a couple of former clever circus acrobats, dressed in dark, specialized in robbing big bank safes.'),
        (1, 16, "The Vulture's Prey/The Dark Terrors", "In order to do a final battle with Spider-Man, the Vulture abducts J.Jonah Jameson and puts him next to a revolving pendulum. It's up to Spidey to stop the Vulture, otherwise his other nemesis Jamieson ends up in two halves."),
        (1, 17, 'The Terrible Triumph of Dr. Octopus/Magic Malice', 'Doc Ock steals a device in order to rule the world. Spider-Man must do something to keep this evil mastermind to achieve his dreadful threat.'),
        (1, 18, 'Fountain of Terror/Fiddler on the Loose', 'Spider-Man is back in Florida, where he must find the secret behind an old Spanish Fort, guarded by some would-be Conquistadors.'),
        (1, 19, 'To Catch a Spider/Double Identity', 'Dr Noah Boddy releases Electro, the Green Goblin and the Vulture to confront Spider-Man in a series of showdowns. Then, a Chameleon-like criminal eludes police and Spider-Man alike.'),
        (1, 20, 'Sting of the Scorpion/Trick or Treachery', "The Flys, a.k.a. the Patterson Twins, are paroled and freed. However, Spider-Man does not believe this and for a reason: they are framing him for all the heists they've done. It's up to Spidey to catch them in their act."),
        (2, 1, 'The Origin of Spiderman', 'Meek Peter Parker is bitten by a radioactive spider and acquires super-powers. He decides to use his new powers to get rich, but when tragedy strikes close to home, he learns a valuable lesson and vows to fight crime instead.'),
        (2, 2, 'King Pinned', 'Peter Parker overhears talk of a laboratory producing imitation pharmaceuticals, investigation as Spider-Man lead him to find out the whole plot has been engineered by a rotund mobster called the Kingpin.'),
        (2, 3, 'Swing City', "A new nuclear reactor has been built in the heart of Manhattan, and Peter Parker's science class is researching it. Peter is asked by his attractive classmate, Sonya, to help her that night with research about the reactor. As Spidey is making his way to Sonya's home, trouble is brewing. A demented radiation specialist breaks into the new reactor and threatens the city with ransom; unless he is paid $10 million, given amnesty from prosecution, and given permission to build his own reactor, he will use anti-gravity rays on Manhattan to lift it into the sky."),
        (2, 4, 'Criminals in the Clouds', 'The Sky-Master plots to wreak havoc on New York from his dirigible, so Spider-Man hitches a ride to burst his balloon.'),
        (2, 5, 'Menace from the Bottom of the World', 'When a city bank mysteriously vanishes into the street, J. Jonah Jameson sends a reporter named Hammond to investigate, while sending Peter to visit a scientist who has recorded voices supposedly from the center of the earth. Peter visits the scientist, and when a recording is played for him, he discovers it is the men - or beasts - who made the bank disappear, and they are plotting another one. Realizing it is only a few minutes until the plot is carried out, Peter becomes Spidey & heads for the bank. Sure enough, the bank disappears into the earth!'),
        (2, 6, 'Diamond Dust', 'After years of being dismissed as a bookworm, Peter Parker is trying to win a spot as a relief pitcher on his college baseball team.'),
        (2, 7, 'Spiderman Battles the Molemen', 'The Molemen from "Menace From the Bottom of the World" are stealing buildings again, taking back the sun, wealth and land that they feel should rightfully belong to them.'),
        (2, 8, 'Phantom from the Depths of Time', "One of the enslaved inhabitance from a small Island manages to send a distress call on the same frequency as spider-man's spidersense. He arrives to find the evil Dr Mantra using giant beetles to force the humans to work in the ore mines."),
        (2, 9, 'The Evil Sorcerer', 'Thousands of years before the dawn of civilization, evil magicians were fighting for supremacy - and Kotep, the Scarlet Sorcerer, was the most aggressive of these. When he loses a battle with an opponent, his own demons turn against him and place him in a time suspension. Six thousand years later, he is an exhibition at the museum where Peter is taking a course. The professor teaching it has found a spell that can supposedly revive the sorcerer, and when it is tested, Kotep does indeed come back to life - and turns on the professor.'),
        (2, 10, 'Vine', 'Spider-Man comes back again against the new evil villain Vine.'),
        (2, 11, 'Pardo Presents', "In the dead of night, a menacing giant cat is stalking across Manhattan & stealing valuables - furs, jewelry, cash - and bringing it back to a tiny apartment. The cat is the product of a demented sorcerer named Pardo, who is planning a vile scheme - to rob Manhattan's wealthiest citizens and then sap the souls out of their bodies. At a movie theater the following night, Peter attends a premiere of a strange film called 'My...Pet' with his girlfriend, Polly. When the film starts, the giant cat appears, and begins gassing the audience with a noxious gas that puts them into a trance."),
        (2, 12, 'Cloud City of Gold', "Peter Parker is on assignment in South America with some wealthy businessmen. Their plane crashes in the jungle and their only salvation is a frail boat on the river. However, the river ends as a whirlpool and all are caught in an eerie cave full of bats and unusual creatures. The businessmen are captured by the henchmen of a Conquistador, who uses a fire pit to terrorize and rule over this strange world. It's up to Spider-Man to free the menaced innocents and neutralize this paranoid tyrant."),
        (2, 13, "Neptune's Nose Cone", 'Peter Parker travels with Penny Jones in the Daily Bugle plane to the Antarctic to photograph a downed space capsule. After a crash landing on an island, Spider-Man must rescue Penny from a primitive tribe bent on human sacrifice.'),
        (2, 14, 'Home', 'Spider-Man stumbles upon a robbery perpetrated by a woman with the same spider powers he has. Spider-Man follows her and discovers that she is from a distant civilization that crash landed one Earth long ago.'),
        (2, 15, 'Blotto', "A demented movie producer named Clive has created a device called the Spirit-Scope, which he uses to set his darkest creation, called Blotto - a black, blob-like creature - loose from the screen. Blotto is released into Manhattan's streets, extending a dark tentacle over anything and when the tentacle retracts, the object is gone. Nothing is too small, or big, for him to consume - cars, lamp posts, mailboxes, even buildings. Peter is out driving with a celebrity when his car runs out of gas, just as Blotto moves in."),
        (2, 16, 'Thunder Rumble', 'Volton, The Martian God of Thunder, teams up with two crooks and rampages through the city, Luckily Spider-man and his team of teenage car driving helpers is on hand to trap, tie up and defeat Volton and save the day.'),
        (2, 17, 'Spiderman Meets Skyboy', "Jan Caldwell, the son of famed a scientist dons his father's powerful astro-helmet and joins forces with Spider-Man to combat the crazed Doctor Zap."),
        (2, 18, 'Cold Storage', 'Two jewel thieves are caught in the act by Spidey after robbing a jewelry store at midnight. They are trying to mix the jewels with ice & then smuggle them out of the country. One of the thieves - known as Dr. Cool - manipulates his trick cane, rendering Spidey unconscious. The henchman ties Spidey up & places him in a deep freeze, then turns the temperature to absolute zero! Spidey is unable to free himself before the cold takes over, and he awakes to find the city in ruins & inhabited by cavemen, who attack him.'),
        (2, 19, 'To Cage a Spider', 'Spider-man is in Jail. He agrees to help a group of inmates stage a prison breakout. While escaping things are not going to plan around them and the numbers start to dwindle.'),
        (3, 1, "The Winged Thing/Conner's Reptiles", 'The Vulture warns people to stay off the streets, then escapes from Spider-Man on a building site.'),
        (3, 2, 'Trouble with Snow/Spiderman vs. Desparado', "Electricity, when used responsibly, it is man's best friend, when not kept under control it gives life to evil city destroying snowmen! Desperado, a lasso wielding cowboy, uses his electronic horse to go on a one man crime-wave."),
        (3, 3, 'Sky Harbor/The Big Brainwasher', "Peter hears that his friend Mary Jane Watson has a new job as a dancer in a hip night club in Greenwich Village. However, he discovers that this is a front for the criminal activities of the Kingpin, a notorious gangster. It's up to Spider-Man to foil this plot, in the risk of being drowned in some water tank which the Kingpin installed in the backstage of the club to get rid of his enemies."),
        (3, 4, 'The Vanishing Dr. Vespasian/Scourge of the Scarf', "A series of bank robberies are taking place, with no evidence left behind. The robberies are the work of a group of thieves led by a chemist named Dr. Vespasian, who has created an invisibility formula called 'Invisoscine'. When Spidey investigates the robberies, Vespasian's dog, Brutus - who is also invisible - attacks Spidey. Brutus is thrown against a container of flour, which coats him & reveals him to Spidey. Spidey throws Brutus into a garbage can & traps him, but when police find signs of a struggle & pieces of Spidey's uniform, it is suspected that Spidey is dead."),
        (3, 5, 'Super Swami/The Birth of Micro Man', 'Koga, a Chinese magician, wreaks havoc on the city. Peter Parker picks up Professor Pretoris who is precariously persuade at pace by a party of pouncing police.'),
        (3, 6, 'Knight Must Fall/The Devious Dr. Dumpty', 'Peter is assigned to cover a parade for the Daily Bugle, and disguised as Spidey, he sits atop a building taking pictures. Among the floats is a movie star with a fortune in jewels on her - and in the crowd is a group of thieves. When a gas-filled balloon released by the thieves explodes, it knocks everyone out, except Spidey, who goes into action. The thieves escape in a hot-air balloon, but Spidey discovers their identities. They are led by a huge man named Dr. Humperdinck Dumpty, who has his thugs rain sandbags on Spidey, knocking him off the balloon.'),
        (3, 7, 'Up from Nowhere', "In New York harbor, a mysterious machine pops up out of the depths. This is the lab of Dr. Atlantian, a demented scientist from the lost continent of Atlantis. At the same time as Atlantian is planning to subjugate the world, Peter is learning about Atlantis - and its apparently highly advanced technology, some of which draws its power from the moon. Unknown to anyone, Atlantian deploys the moonbeam to create an earthquake, which draws attention from the armed forces. The army turns its tanks on Atlantian's reactor, now at the surface of the water."),
        (3, 8, 'Rollarama', 'Peter & his girlfriend Sue are at an old house going through its attic, when one of them discovers a large seed that responds quickly to sunlight. Putting it aside when Sue discovers a large machine - the Glutz Machine - in the attic, Peter turns his attention to the story of the professor who has gone back to 3 million B.C. While he & Sue are investigating, the seed suddenly grows hundredfold in size & rolls through the side of the house and into the street, smashing everything in its way.'),
        (3, 9, 'Rhino/The Madness of Mysterio', 'The Rhino, loving repeat performances, once again steals gold shipments with which to build a 14 karat statue of himself. Hopefully the city learn their lesson this time and beefs up security. Mysterio traps Spider-man in a deadly amusement park.'),
        (3, 10, 'Revolt in the Fifth Dimension', 'Luck, Suggestion and Determination must guide Spider-man through this trip into the 5th dimension. Spider-man finds himself face to face with The Skeletal Infinata, how can he defeat something that is completely of the mind?'),
        (3, 11, 'Specialists and Slaves', "Spidey's old enemy from 'Swing City' - the Radiation Specialist - is back! This time, he is much darker - even psychopathic. Following his release from jail, the specialist promptly revisits Manhattan's nuclear reactor, stuns the outdoor soldiers guarding it, and again commandeers the reactor. Realizing Spidey still poses a threat to him, the specialist contrives a scheme using a remote-controlled car to get Spidey off the island. Spidey soon finds out about the specialist's plans and gets back into Manhattan, even as the specialist lifts Manhattan into the sky once again."),
        (3, 12, 'Down to Earth', "Basically, this is Neptune's Nose Cone with the action taking place in the North Pole this time."),
        (3, 13, 'Trip to Tomorrow', 'Spider-man recounts an anthology of tales to Tom, a young wannabe hero running away from home.'),
    ],
    "Hulk": [
        (1, 1, 'Origin of the Hulk/Enter the Gorgon/To Be a Man', 'Bruce Banner becomes the Hulk by saving Rick Jones from a gamma bomb explosion.'),
        (1, 2, 'The Terror of the Toadmen/Bruce Banner Wanted for Treason!/Hulk Runs Amok', 'The Gorgon comes to America and fights the Hulk, only to be cured of his ugly disease by Hulk.'),
        (1, 3, 'A Titan Rides the Train!/The Horde of Humanoids!/On the Rampage!', 'The Gorgon takes Bruce Banner and Rick Jones hostage. Banner actually agrees to help Gorgon to cure him of his hideous disease.'),
        (1, 4, 'The Power of Doctor Banner!/Where Strides the Behemoth/Back from the Dead!', "After the army shoots down their UFO, the evil Toadmen seek refuge in an underground tunnel and magnetically pull the moon toward earth, causing great disasters. Banner escapes imprisonment, confronts the Toadmen's attack and saves earth."),
        (1, 5, 'Micro-Monsters/The Lair of the Leader/To Live Again', "The Leader plots to destroy the Hulk using a swarm of tiny, genetically engineered creatures and his loyal, mindless Humanoids. Bruce Banner then goes on the offensive to track down the Leader's primary subterranean stronghold, and the Hulk must overpower the Leader's traps to escape."),
        (1, 6, 'Brawn Against Brain/Captured at Last!/Enter...the Chameleon!', "Bruce Banner's indestructible robot is stolen, leading to a clash where the Hulk confronts the machine and the military tries to capture him. The Hulk is subdued and chained, reverting to Banner, and the Leader dispatches the Chameleon to infiltrate the army base and steal Banner's technology in disguise."),
        (1, 7, 'Within the Monster Dwells a Man!/Another World, Another Foe!/The Wisdom of the Watcher!', "The Leader, a mutated super villain, learns that Doctor Banner's newest nuclear device is being transported to another military base by a train, and sends one of his Humanoids to attack the train and steal the invention."),
        (1, 8, 'The Space Phantom/Sting of the Wasp/Exit the Hulk', 'While Banner is in custody of Glenn Talbot, the Leader prepares his invasion of the island with a horde of Humanoids.'),
        (1, 9, 'The Incredible Hulk vs The Metal Master/The Master Tests His Metal/Mind Over Metal', "In the midst of the epic battle against the Leader's Humanoids, the Hulk suddenly transforms back into Bruce Banner."),
        (1, 10, 'The Ringmaster/Captive of the Circus/The Grand Finale', 'Bruce Banner is hunted by the military for a crime he did not commit and The Hulk must fight for his justice.'),
        (1, 11, 'Enter Tyrannus/Beauty and the Beast/They Dwell in the Depths', 'Hulks faces a tank army of General Ross and escapes to the Himalayas - where he is promptly taken hostage as Bruce Banner.'),
        (1, 12, 'The Terror of the T Gun/I Against a World/Bruce Banner Is the Hulk!', 'Bruce Banner has a chance to save Glenn Talbot - and to get rid of the Hulk once and for all.'),
        (1, 13, 'The Man Called Boomerang!/The Hulk Intervenes/Less than Monster, More than Man!', 'Banner is in custody and gets word to Rick Jones, who shows up in the army to save Banner. Rick is wounded in battle, but the Hulk saves his life.'),
    ],
    "Birdman and the Galaxy Trio": [
        (1, 1, 'X the Eliminator/Revolt of the Robots/Morto the Marauder', 'F.E.A.R. hires X The Eliminator to destroy Birdman.'),
        (1, 2, 'The Ruthless Ringmaster/The Battle of the Aquatrons/Birdman Versus the Mummer', "An evil circus heists secret weapons for F.E.A.R. Birdman matches wits with a master of disguise. The Galaxy Trio must stop an alien dictator from melting earth's polar ice caps."),
        (1, 3, 'The Quake Threat/The Galaxy Trio Versus the Moltens of Meteorus/Avenger for Ransom', 'An evil genius has developed a device that enables him to cause earthquakes and he wants to sell it to the highest bidder.'),
        (1, 4, 'Birdman Versus Cumulus, the Storm King/The Galaxy Trio and the Sleeping Planet/Serpents of the Deep', 'Cumulus controls the weather in Central City and tries to black mail the government.'),
        (1, 5, 'Nitron the Human Bomb/The Galaxy Trio and the Peril of the Prison Planet/Mentok, the Mind Taker', 'A radioactive villain wants to join F.E.A.R but he also wants half control of the organization.'),
        (1, 6, 'The Purple Moss/Drackmore the Despot/The Deadly Trio', 'During an important peace treaty signing, a master of disguise kidnaps and impersonates the King Of Salaman.'),
        (1, 7, 'The Brain Thief/Titan, the Titanium Man/Birdman Versus the Constrictor', "Scientists are disappearing at the World's Fair and Birdman must find out where they are going."),
        (1, 8, 'Number One/The Duplitrons/Birdman Meets Birdgirl', "Birdman's number one enemy schemes to steal away his energy for use in a pirate satellite."),
        (1, 9, 'Birdman Meets Reducto/Computron Lives/Vulturo, Prince of Darkness', 'A mad scientist threatens to use his shrink ray on everyone unless Birdman is sent to him.'),
        (1, 10, 'The Chameleon/The Eye of Time/The Incredible Magnatroid', 'A shape shifting villain goes on a robbery spree.'),
        (1, 11, 'Hannibal the Hunter/The Cavemen of Primevia/The Empress of Evil', 'The Constrictor steals tops secret missile plans and will only return them if Birdman gives him Avenger.'),
        (1, 12, 'The Wings of Fear/The Demon Raiders/Birdman Meets Birdboy', 'An evil scientist creates a female version of Birdman to help him steal hydrogen bombs.'),
        (1, 13, 'The Menace of Dr. Millenium/The Rock Men/Birdman Versus Dr. Freezoids', 'A mad scientist reanimates prehistoric animals to steal a top secret missile plan.'),
        (1, 14, 'The Deadly Duplicator/Space Fugitives/Professor Nightshade', 'Using special glasses, a mad scientist is duplicating people to have them do his work.'),
        (1, 15, 'Train Trek/Space Slaves/Birdman Meets Moray of the Deep', 'Spyro hi-jacks a train so he can get into Atomic City and steal a new government rocket ship.'),
        (1, 16, 'Birdman and the Monster of the Mountain/Galaxy Trio Versus Growliath/The Return of Vulturo', 'Fed by rays from a hovering metal sphere, a suffocating purple moss is spreading over the countryside.'),
        (1, 17, 'Revenge of Dr. Millenium/Return to Aqueous/The Ant Ape', 'A giant magnetic robot steals an important shipment of titanium.'),
        (1, 18, 'Birdman Versus the Speed Demon/Invasion of the Sporoids/The Wild Weird West', ''),
        (1, 19, 'The Pirate Plot/Gralik of Gravitas/Skon of Space', 'Winged agents of F.E.A.R. are abducting foreign ambassadors.'),
        (1, 20, 'Murro the Marauder/Plastus the Pirate Planet/Morto Rides Again', 'A mysterious member of F.E.A.R. is trying to freeze the city with his freeze ray.'),
    ],
}

BIRDMAN_SEG_SYN = {
    'Avenger for Ransom': "Zardo has bird-napped Avenger and threatens to kill him if Birdman won't reveal military secrets.",
    'Birdman Meets Birdboy': 'Birdman finds a boy floating on a raft and accidentally gives him super powers.',
    'Birdman Versus the Speed Demon': 'An accident in a prison lab gives a prisoner lightning speed that enables him to escape.',
    'Birdman and the Monster of the Mountain': 'A monster is terrorizing a Himalayan village and Birdman must save the day.',
    'Galaxy Trio Versus Growliath': 'When a village in Tibet is regularly terrorized by a monster, Birdman and his friends follow the tracks and encounter him.',
    'Galaxy Trio Versus the Moltens of Meteorus': 'Birdman tracks Kyrov, who has been engineering earthquakes.',
    'Hannibal the Hunter': 'An evil big game hunter wants to add Birdman to his collection.',
    'Morto Rides Again': 'Morto breaks out of prison again and demands control of the country.',
    'Morto the Marauder': 'Morto the Marauder creates a suit of armor to break out of prison and get his revenge on Birdman.',
    'Murro the Marauder': 'A mysterious villain shows up at F.E.A.R. headquarters offering them military secrets and the ultimate defeat of Birdman. In return he wants absolute control of F.E.A.R.',
    'Professor Nightshade': 'An agent of F.E.A.R. steals a new device that can make entire cities disappear.',
    'Serpents of the Deep': "The government develops a device that can dig up gold from the ocean floor but it's stolen by an underwater villain.",
    'Skon of Space': 'An alien invasion fleet sends an advance scout to Earth to determine if it can be conquered.',
    'The Ant Ape': 'Professor Claw creates a robot to destroy Birdman.',
    'The Deadly Trio': 'Three evil scientists join forces to defeat Birdman.',
    'The Empress of Evil': 'The son of The Maharaja of Ramadan has been kidnapped by Medusa and Birdman must rescue him.',
    'The Pirate Plot': 'Captain Kidd has returned from the dead to raid the high seas.',
    'The Return of Vulturo': 'Vulturo creates a robotic Avenger clone and uses it to capture Birdboy.',
    'The Wild Weird West': 'Descendants of Jesse James uses space technology to finish what he started.',
}

# Titles that embed their own metadata or are riff one-offs.
_MST3K = re.compile(r'^\s*MST3K\s*-\s*S(\d+)E(\d+)\s*-\s*(.+)$', re.I)
_RIFF  = re.compile(r'^\s*Rifftrax\s*-\s*(.+)$', re.I)

def special_title(raw):
    """Return {show, [season, ep], [sub]} for MST3K / RiffTrax titles, else None."""
    m = _MST3K.match(raw)
    if m:
        return {"show": "Mystery Science Theater 3000",
                "season": int(m.group(1)), "ep": int(m.group(2)), "sub": m.group(3).strip()}
    m = _RIFF.match(raw)
    if m:
        return {"show": "RiffTrax", "sub": m.group(1).strip()}
    if raw.strip().lower() == "rifftrax shorts":
        return {"show": "RiffTrax", "shorts": True}
    return None

# RiffTrax feature riffs: normalized feed sub -> (display title, year, description)
RIFFTRAX_SHORTS_DESC = ("The stars of Mystery Science Theater 3000 (1988) riff on weird and "
                        "oddball educational shorts.")
RIFFTRAX_MAP = {
    "house on haunted hill": (
        "RiffTrax Live: House on Haunted Hill (2010)", "2010",
        "Hosted from Nashville on October 28th, 2010, the RiffTrax guys riff on \"House on "
        "Haunted Hill (1959)\" while also watching the short subjects \"Paper and I (1960)\" and "
        "\"Magical Disappearing Money (1972)\". Comedian, actor, and writer Paul F. Tompkins guest stars."),
    "drag me to hell": (
        "RiffTrax: Drag Me to Hell (2009)", "2009",
        "Join Mike and Bill on this sentimental excursion down Hell Lane. Just watch out for "
        "falling anvils and, really, just copious amounts of eyeball splatter."),
    "island of dr moreau": (
        "RiffTrax: The Island of Dr. Moreau (2006)", "2006",
        "And the people cried out with one voice, \"Maketh us a movie in which Marlon Brando can "
        "don a muumuu, false teeth, clown white make-up and a really gay bonnet. See that it also "
        "stareth Val Kilmer at his scenery-chewing best. And, yea, putteth the extras in hot, "
        "smelly animal suits and maketh you the plot absurd.\" And, lo, did John Frankenheimer "
        "deliver unto us The Island of Dr. Moreau. And it was good. Truly, you must see it to "
        "believe it. But you must only see it accompanied by this RiffTrax, for which Mike "
        "enlisted the talents of Kevin Murphy, or else you WILL die."),
    "twilight 4 breaking dawn": (
        "RiffTrax: The Twilight Saga: Breaking Dawn, Part 1 (2012)", "2012",
        "When word leaked that the final Twilight movie would be split into two parts, most "
        "people assumed that this was done by the studio as a cynical cash grab. Not so. The last "
        "chapter in the Twilight saga is so vast, so detailed, that it demanded the lush, "
        "panoramic two movie treatment.\n\n"
        "Okay, maybe they could have trimmed some of that twenty minute wedding because it was "
        "very straightforward and didn't impact the story in any way and essentially could have "
        "been a wedding from a Reese Witherspoon movie. And we probably didn't need every single "
        "one of the scenes where Jacob visits the Cullen's house and shouts at someone. And dear "
        "god, they are showing them playing chess on their honeymoon AGAIN!\n\n"
        "Fortunately, the remaining twelve minutes of the movie that advances the \"plot\" in "
        "some fashion makes up for the slow pace of the rest of the movie by being disgusting and "
        "incoherent. The birth of Bella and Edward's horrible mutant spawn is repellent, nasty "
        "and vile, and yes, we are just referring to the decision to name it Renesmee.*\n\n"
        "Also, this time the wolves go to a logging plant and communicate via telepathy.\n\n"
        "Mike, Kevin and Bill love to hang out at the logging plant too, or at least they did "
        "until that lame foreman called their parents and ruined all their fun.\n\n"
        "*DO NOT NAME YOUR CHILD THIS OR ALLOW ANYONE YOU KNOW TO NAME THEIR CHILD THIS"),
}

# ══════════════════════════════════════════════════════════════════════════
# Metadata engine (TMDB -> TVmaze -> TVDB), lifted from the Whiplash generator
# ══════════════════════════════════════════════════════════════════════════
TMDB_KEY  = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_LANG = "en-US"
DESC_CACHE_FILE = "toonami_desc_cache.json"
# Hand-built master map (from the Snick week sheet), keyed by "<norm show>|<episodeNumber>"
# -> {"se":[S,E] or null, "sub": episode name, "desc": synopsis}. Authoritative: a
# manual sub/desc/S-E here wins over everything; an S/E-only entry lets the DBs fill
# the name+desc by that S/E. Lives next to this script, committed to the repo.
MASTER_MAP_FILE = "master_map.json"
SHOW_TMDB_OVERRIDES = {}

def load_master_map():
    import os as _os
    for base in (_os.path.dirname(_os.path.abspath(__file__)), _os.getcwd()):
        p = _os.path.join(base, MASTER_MAP_FILE)
        try:
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
            print(f"  master map: {len(m)} episodes loaded from {MASTER_MAP_FILE}")
            return m
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"  master map: failed to parse {MASTER_MAP_FILE} ({e})")
            return {}
    print(f"  master map: {MASTER_MAP_FILE} not found; skipping")
    return {}

# ── Authoritative static episode guide (episode_guide.json) ──────────────
# Built offline from Taqi's saved IMDb/TMDB/Wikipedia/Fandom episode lists by
# build_guide.py. Keyed norm(display) -> {"display", "episodes": {str(absN):
# {se:[s,e]|null, sub, desc}}}. This is the FIX for segmented shows: TMDB
# counts each segment as an episode, so its absolute-number resolution lands on
# the wrong S/E; the guide maps the feed's absolute episodeNumber straight to
# the real broadcast half-hour. Authoritative for the shows it covers — it beats
# every automatic resolver below (only the hand master map outranks it).
GUIDE_FILE = "episode_guide.json"
def load_guide():
    import os as _os
    for base in (_os.path.dirname(_os.path.abspath(__file__)), _os.getcwd()):
        p = _os.path.join(base, GUIDE_FILE)
        try:
            with open(p, encoding="utf-8") as f:
                g = json.load(f)
            n = sum(len(v.get("episodes", {})) for v in g.values())
            print(f"  episode guide: {len(g)} shows / {n} episodes loaded from {GUIDE_FILE}")
            return g
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"  episode guide: failed to parse {GUIDE_FILE} ({e})")
            return {}
    print(f"  episode guide: {GUIDE_FILE} not found; skipping")
    return {}

_QYEAR = re.compile(r'\s*\(((?:19|20)\d\d)\)\s*$')
_TAGS  = re.compile(r"<[^>]+>")
_sess  = requests.Session()


def load_cache():
    try:
        with open(DESC_CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        c = {}
    for k in ("shows", "episodes", "tvmaze_shows", "tvmaze_episodes", "tmdb_seasons",
              "tvmaze_eplist", "tvdb_shows", "tvdb_episodes", "tvmaze_namemap",
              "tvdb_namemap", "show_syn", "simkl_id", "simkl_namemap", "omdb_ep"):
        c.setdefault(k, {})
    return c


def save_cache(c):
    with open(DESC_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=0, sort_keys=True)


def _tmdb_get(path, **params):
    params["api_key"] = TMDB_KEY
    for _ in range(3):
        try:
            r = _sess.get(TMDB_BASE + path, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
            return r.json() if r.status_code == 200 else None
        except requests.RequestException:
            time.sleep(1)
    return None


def _resolve_show_id(show, cache):
    if show in SHOW_TMDB_OVERRIDES:
        return SHOW_TMDB_OVERRIDES[show]
    key = show.lower()
    if key in cache["shows"]:
        return cache["shows"][key]
    ym = _QYEAR.search(show)
    query = show[:ym.start()].strip() if ym else show
    params = {"query": query}
    if ym:
        params["first_air_date_year"] = ym.group(1)
    data = _tmdb_get("/search/tv", **params)
    sid = data["results"][0]["id"] if (data and data.get("results")) else None
    cache["shows"][key] = sid
    return sid


def _rec(v):
    if isinstance(v, dict):
        return (v.get("o") or ""), (v.get("n") or "")
    return (v or ""), ""


def _tmdb_meta(show, season, ep, cache):
    if not TMDB_KEY:
        return "", ""
    sid = _resolve_show_id(show, cache)
    if not sid:
        return "", ""
    ck = f"{sid}|{season}|{ep}"
    if ck in cache["episodes"]:
        return _rec(cache["episodes"][ck])
    data = _tmdb_get(f"/tv/{sid}/season/{season}/episode/{ep}", language=TMDB_LANG)
    ov = ((data or {}).get("overview") or "").strip()
    nm = ((data or {}).get("name") or "").strip()
    cache["episodes"][ck] = {"o": ov, "n": nm}
    return ov, nm


# ── TVmaze (keyless) ──
ENABLE_TVMAZE = True
TVMAZE_BASE = "https://api.tvmaze.com"


def _tvmaze_get(path, **params):
    for _ in range(3):
        try:
            r = _sess.get(TVMAZE_BASE + path, params=params, timeout=20)
            time.sleep(0.2)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")) + 1); continue
            return r.json() if r.status_code == 200 else None
        except requests.RequestException:
            time.sleep(1)
    return None


def _resolve_tvmaze_id(show, cache):
    key = show.lower()
    if key in cache["tvmaze_shows"]:
        return cache["tvmaze_shows"][key]
    ym = _QYEAR.search(show)
    query = show[:ym.start()].strip() if ym else show
    data = _tvmaze_get("/singlesearch/shows", q=query)
    tid = data.get("id") if isinstance(data, dict) else None
    cache["tvmaze_shows"][key] = tid
    return tid


def _tvmaze_meta(show, season, ep, cache):
    tid = _resolve_tvmaze_id(show, cache)
    if not tid:
        return "", ""
    ck = f"{tid}|{season}|{ep}"
    if ck in cache["tvmaze_episodes"]:
        return _rec(cache["tvmaze_episodes"][ck])
    data = _tvmaze_get(f"/shows/{tid}/episodebynumber", season=season, number=ep)
    d = data if isinstance(data, dict) else {}
    ov = html.unescape(_TAGS.sub("", d.get("summary") or "")).strip()
    nm = (d.get("name") or "").strip()
    cache["tvmaze_episodes"][ck] = {"o": ov, "n": nm}
    return ov, nm


# ── TheTVDB (key) ──
TVDB_KEY = os.environ.get("TVDB_API_KEY", "").strip()
TVDB_BASE = "https://api4.thetvdb.com/v4"
ENABLE_TVDB = bool(TVDB_KEY)
_TVDB_TOKEN = None
SHOW_TVDB_OVERRIDES = {
    "Spider-Man: The Animated Series": "spider-man-1994",
    "Spider-Man (1967)": "spider-man-1967",
}


def _tvdb_login():
    global _TVDB_TOKEN
    if not TVDB_KEY:
        return None
    try:
        r = _sess.post(TVDB_BASE + "/login", json={"apikey": TVDB_KEY}, timeout=20)
        _TVDB_TOKEN = r.json().get("data", {}).get("token") if r.status_code == 200 else None
    except requests.RequestException:
        _TVDB_TOKEN = None
    return _TVDB_TOKEN


def _tvdb_get(path, **params):
    global _TVDB_TOKEN
    if not TVDB_KEY:
        return None
    if _TVDB_TOKEN is None and _tvdb_login() is None:
        return None
    for attempt in range(2):
        try:
            r = _sess.get(TVDB_BASE + path, params=params,
                          headers={"Authorization": f"Bearer {_TVDB_TOKEN}"}, timeout=20)
            if r.status_code == 401 and attempt == 0:
                _TVDB_TOKEN = None
                if _tvdb_login() is None:
                    return None
                continue
            return r.json().get("data") if r.status_code == 200 else None
        except requests.RequestException:
            time.sleep(1)
    return None


def _tvdb_search_id(query):
    data = _tvdb_get("/search", query=query, type="series")
    if isinstance(data, list) and data:
        return data[0].get("tvdb_id") or data[0].get("id")
    return None


def _resolve_tvdb_id(show, cache):
    if show in SHOW_TVDB_OVERRIDES:
        slug = SHOW_TVDB_OVERRIDES[show]
        ck = "slug:" + slug
        if ck in cache["tvdb_shows"]:
            return cache["tvdb_shows"][ck]
        d = _tvdb_get(f"/series/slug/{slug}")
        tid = d.get("id") if isinstance(d, dict) else None
        cache["tvdb_shows"][ck] = tid
        return tid
    key = show.lower()
    if key in cache["tvdb_shows"]:
        return cache["tvdb_shows"][key]
    ym = _QYEAR.search(show)
    query = show[:ym.start()].strip() if ym else show
    tid = _tvdb_search_id(query)
    if tid is None and ":" in query:
        tid = _tvdb_search_id(query.split(":")[0].strip())
    cache["tvdb_shows"][key] = tid
    return tid


def _tvdb_meta(show, season, ep, cache):
    if not ENABLE_TVDB:
        return "", ""
    tid = _resolve_tvdb_id(show, cache)
    if not tid:
        return "", ""
    ck = f"{tid}|{season}|{ep}"
    if ck in cache["tvdb_episodes"]:
        return _rec(cache["tvdb_episodes"][ck])
    ov = nm = ""
    for path in (f"/series/{tid}/episodes/default/eng", f"/series/{tid}/episodes/default"):
        data = _tvdb_get(path, season=season, episodeNumber=ep, page=0)
        eps = data.get("episodes") if isinstance(data, dict) else None
        if isinstance(eps, list) and eps:
            m = next((e for e in eps if e.get("seasonNumber") == season and e.get("number") == ep), eps[0])
            ov = (m.get("overview") or "").strip()
            if not nm:
                nm = (m.get("name") or "").strip()
            if ov:
                break
    cache["tvdb_episodes"][ck] = {"o": ov, "n": nm}
    return ov, nm


def _norm(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()

def _segments(name):
    """Normalized halves of a paired episode title like 'A / B' (for segment matching)."""
    if not name or "/" not in name:
        return []
    return [s for s in (_norm(p) for p in name.split("/")) if s]

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
          "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15}

def clean_chapter(s):
    """House rule: 'Foo, Chapter IV: Bar' -> 'Foo Chapter 4: Bar' (drop the comma
    before Chapter, roman numeral -> arabic)."""
    if not s:
        return s
    s = re.sub(r",\s*(Chapter)\b", r" \1", s, flags=re.I)
    def repl(m):
        return "Chapter " + str(_ROMAN.get(m.group(1).lower(), m.group(1)))
    s = re.sub(r"\bChapter\s+([IVXLCDM]+)\b", repl, s)
    return re.sub(r"\s{2,}", " ", s).strip()


# ── pinned-episode matching for segment-based cartoons ──
def _segkey(s):
    """Aggressive normalize for fuzzy segment matching: lowercase, drop 'Part N',
    strip punctuation and a leading 'the'."""
    s = (s or "").lower()
    s = re.sub(r"\bpart\s+\d+\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    return s.strip()

def _split_segments(sub):
    """Feed sub-title -> list of segment strings (handles ' / ', '/', and commas)."""
    parts = re.split(r"\s*/\s*|\s*,\s*", sub or "")
    return [p.strip() for p in parts if p.strip()]

def _build_pinned_index():
    """{show: (keys_list, {key:(S,E,full_title,synopsis)})} from PINNED_RAW.
    PINNED_RAW rows are (season, episode, 'seg/seg', synopsis)."""
    idx = {}
    for show, eps in PINNED_RAW.items():
        kmap = {}
        for s, e, raw, syn in eps:
            segs = [p.strip() for p in raw.split("/") if p.strip()]
            full = " / ".join(segs)
            for seg in segs:
                kmap.setdefault(_segkey(seg), (s, e, full, syn))
        idx[show] = (list(kmap.keys()), kmap)
    return idx

PINNED_INDEX = _build_pinned_index()

# Birdman airs ONE segment per slot, so even though we show the organized triplet
# (S/E + full title), the aired segment's own synopsis is more accurate than the
# triplet's -- match the feed segment name to its IMDb synopsis when we have one.
_BIRDMAN_SYN_INDEX = {_segkey(k): v for k, v in BIRDMAN_SEG_SYN.items()}
_BIRDMAN_SYN_KEYS = list(_BIRDMAN_SYN_INDEX.keys())

def pinned_lookup(show, sub):
    """Match a feed sub-title's segment to a pinned broadcast episode.
    Returns (season, ep, full_title, synopsis) or None."""
    entry = PINNED_INDEX.get(show)
    if not entry or not sub:
        return None
    keys, kmap = entry
    for seg in _split_segments(sub):
        k = _segkey(seg)
        if not k:
            continue
        if k in kmap:
            return kmap[k]
        hit = difflib.get_close_matches(k, keys, n=1, cutoff=0.82)
        if hit:
            return kmap[hit[0]]
    return None

def birdman_syn_lookup(sub):
    """Birdman segment name -> IMDb synopsis ('' if none known)."""
    for seg in _split_segments(sub or ""):
        k = _segkey(seg)
        if not k:
            continue
        if k in _BIRDMAN_SYN_INDEX:
            return _BIRDMAN_SYN_INDEX[k]
        hit = difflib.get_close_matches(k, _BIRDMAN_SYN_KEYS, n=1, cutoff=0.82)
        if hit:
            return _BIRDMAN_SYN_INDEX[hit[0]]
    return ""


def _tvmaze_namemap(tid, cache):
    key = str(tid)
    if key in cache["tvmaze_namemap"]:
        return cache["tvmaze_namemap"][key]
    data = _tvmaze_get(f"/shows/{tid}/episodes")
    m = {}
    if isinstance(data, list):
        for e in data:
            nm = _norm(e.get("name")); sn = e.get("season"); num = e.get("number")
            if nm and sn and num:
                m.setdefault(nm, [sn, num])
                for seg in _segments(e.get("name")):   # "A / B" pairs -> index each half
                    m.setdefault(seg, [sn, num])
    cache["tvmaze_namemap"][key] = m
    return m


def _tvdb_namemap(tid, cache):
    key = str(tid)
    if key in cache["tvdb_namemap"]:
        return cache["tvdb_namemap"][key]
    m = {}
    for page in range(10):
        data = _tvdb_get(f"/series/{tid}/episodes/default/eng", page=page)
        eps = data.get("episodes") if isinstance(data, dict) else None
        if not eps:
            break
        for e in eps:
            nm = _norm(e.get("name")); sn = e.get("seasonNumber"); num = e.get("number")
            if nm and sn and num:
                m.setdefault(nm, [sn, num])
                for seg in _segments(e.get("name")):
                    m.setdefault(seg, [sn, num])
        if len(eps) < 100:
            break
    cache["tvdb_namemap"][key] = m
    return m


# ── SIMKL (optional 4th resolver) ──────────────────────────────────────────
# Supplementary name->S/E source, gated on a SIMKL_CLIENT_ID secret. SIMKL is
# AniDB/TMDB/TVDB-backed with good anime + absolute coverage, so it can resolve
# episode names the other three miss. Best-effort: any failure just no-ops, and
# (like the other name maps) it only ever yields an S/E when an episode NAME
# matches, so it can't inject a wrong title.
SIMKL_CLIENT_ID = os.environ.get("SIMKL_CLIENT_ID", "").strip()
ENABLE_SIMKL = bool(SIMKL_CLIENT_ID)
SIMKL_BASE = "https://api.simkl.com"
_SIMKL_HITS = 0

def _simkl_get(path, **params):
    params.setdefault("client_id", SIMKL_CLIENT_ID)
    headers = {"simkl-api-key": SIMKL_CLIENT_ID, "Accept": "application/json"}
    for _ in range(2):
        try:
            r = _sess.get(SIMKL_BASE + path, params=params, headers=headers, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 502, 503):
                time.sleep(1); continue
            return None
        except requests.RequestException:
            time.sleep(1)
    return None

def _resolve_simkl_id(show, cache):
    key = _norm(show)
    if key in cache["simkl_id"]:
        return cache["simkl_id"][key]
    sid = None
    for ep in ("/search/tv", "/search/anime"):
        data = _simkl_get(ep, q=show)
        if isinstance(data, list) and data:
            ids = (data[0] or {}).get("ids") or {}
            sid = ids.get("simkl") or ids.get("simkl_id")
            if sid:
                break
    cache["simkl_id"][key] = sid
    return sid

def _simkl_namemap(sid, cache):
    key = str(sid)
    if key in cache["simkl_namemap"]:
        return cache["simkl_namemap"][key]
    m = {}
    data = _simkl_get(f"/tv/episodes/{sid}", extended="full")
    if isinstance(data, list):
        for e in data:
            nm = _norm(e.get("title"))
            sn = e.get("season"); num = e.get("episode")
            if nm and sn and num:
                m.setdefault(nm, [sn, num])
                for seg in _segments(e.get("title")):
                    m.setdefault(seg, [sn, num])
    cache["simkl_namemap"][key] = m
    return m

def _simkl_name_to_se(show, name, cache):
    if not ENABLE_SIMKL:
        return None, None
    try:
        target = _norm(name)
        if not target:
            return None, None
        sid = _resolve_simkl_id(show, cache)
        if not sid:
            return None, None
        m = _simkl_namemap(sid, cache)
        global _SIMKL_HITS
        se = m.get(target)
        if se:
            _SIMKL_HITS += 1
            return se[0], se[1]
        hit = difflib.get_close_matches(target, list(m.keys()), n=1, cutoff=0.90)
        if hit:
            _SIMKL_HITS += 1
            se = m[hit[0]]
            return se[0], se[1]
    except Exception:
        pass
    return None, None


def _name_to_se(show, name, cache):
    target = _norm(name)
    if not target:
        return None, None
    if ENABLE_TVMAZE:
        tid = _resolve_tvmaze_id(show, cache)
        if tid:
            se = _tvmaze_namemap(tid, cache).get(target)
            if se:
                return se[0], se[1]
    if ENABLE_TVDB:
        tid = _resolve_tvdb_id(show, cache)
        if tid:
            se = _tvdb_namemap(tid, cache).get(target)
            if se:
                return se[0], se[1]
    if ENABLE_SIMKL:                       # supplementary 4th source (exact + fuzzy)
        s, e = _simkl_name_to_se(show, name, cache)
        if s:
            return s, e
    return None, None


def _name_to_se_fuzzy(show, name, cache, cutoff=0.90):
    """Exact name lookup missed, but the feed's English episode name is usually
    only a spelling/punctuation hair off the DB's. A HIGH-cutoff fuzzy match (by
    name, so still reliable) recovers those without guessing from a raw number."""
    target = _norm(name)
    if not target or len(target) < 6:
        return None, None
    for enabled, resolve, namemap in (
            (ENABLE_TVMAZE, _resolve_tvmaze_id, _tvmaze_namemap),
            (ENABLE_TVDB,   _resolve_tvdb_id,   _tvdb_namemap)):
        if not enabled:
            continue
        tid = resolve(show, cache)
        if not tid:
            continue
        m = namemap(tid, cache)
        if not m:
            continue
        hit = difflib.get_close_matches(target, list(m.keys()), n=1, cutoff=cutoff)
        if hit:
            se = m[hit[0]]
            return se[0], se[1]
    return None, None


# ── OMDb (optional episode-plot source; IMDb-backed) ──────────────────────
# OMDb carries IMDb episode plots for old/obscure shows that TMDB/TVmaze/TVDB
# leave blank, so it's a strong LAST resort for the <desc>. Gated on OMDB_API_KEY.
# Episode lookup: ?t=<show>&Season=<s>&Episode=<e>&plot=full -> {Title, Plot}.
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "").strip()
ENABLE_OMDB = bool(OMDB_API_KEY)
OMDB_BASE = "https://www.omdbapi.com/"

# Path 1: auto-resolve S/E (+ DB name/synopsis) for nameless Snick/live-action
# slots straight from the feed's absolute episodeNumber. Additive gap-filler —
# see the "2b" tier in the enrich loop. Flip off to go back to leaving those
# slots as "(no episode name)".
ALLOW_ABS_SNICK = os.environ.get("ALLOW_ABS_SNICK", "1") != "0"

def _omdb_meta(show, season, ep, cache):
    """IMDb episode (overview, name) via OMDb, cached. '' on any miss/failure."""
    if not ENABLE_OMDB:
        return "", ""
    key = f"{_norm(show)}|{season}|{ep}"
    if key in cache["omdb_ep"]:
        c = cache["omdb_ep"][key]
        return c.get("ov", ""), c.get("nm", "")
    ov = nm = ""
    # strip a trailing "(YYYY)" to a year hint OMDb can use
    ym = _QYEAR.search(show); clean = (show[:ym.start()].strip() if ym else show)
    params = {"apikey": OMDB_API_KEY, "t": clean, "Season": season, "Episode": ep, "plot": "full"}
    if ym:
        params["y"] = ym.group(1)
    try:
        for _ in range(2):
            r = _sess.get(OMDB_BASE, params=params, timeout=20)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d.get("Response") == "True":
                    p = (d.get("Plot") or "").strip()
                    if p and p.upper() != "N/A":
                        ov = p
                    t = (d.get("Title") or "").strip()
                    if t and t.upper() != "N/A":
                        nm = t
                break
            if r.status_code in (429, 503):
                time.sleep(1); continue
            break
    except requests.RequestException:
        pass
    cache["omdb_ep"][key] = {"ov": ov, "nm": nm}
    return ov, nm


def episode_meta(show, season, ep, cache):
    """Return (overview, episode_name) with PER-FIELD source preference:
      - episode NAME  : TMDB -> TVmaze -> TVDB -> OMDb  (TMDB has the best English
                        titles, esp. for anime)
      - SYNOPSIS/overview: OMDb(IMDb) -> TMDB -> TVmaze -> TVDB  (you prefer IMDb)
    Each source is queried at most once and cached, so the two different orders
    don't multiply API calls across runs."""
    if season is None:
        return None, None
    _fn = {"tmdb": _tmdb_meta, "omdb": _omdb_meta, "tvmaze": _tvmaze_meta, "tvdb": _tvdb_meta}
    _on = {"tmdb": True, "omdb": ENABLE_OMDB, "tvmaze": ENABLE_TVMAZE, "tvdb": ENABLE_TVDB}
    _got = {}
    def get(src):
        if src not in _got:
            _got[src] = _fn[src](show, season, ep, cache) if _on[src] else ("", "")
        return _got[src]
    nm = ""
    for src in ("tmdb", "tvmaze", "tvdb", "omdb"):
        n = get(src)[1]
        if n:
            nm = n; break
    ov = ""
    for src in ("omdb", "tmdb", "tvmaze", "tvdb"):
        o = get(src)[0]
        if o:
            ov = o; break
    return (ov or None), (nm or None)


def show_overview(show, cache):
    if show in SHOW_DESC_OVERRIDE:
        return SHOW_DESC_OVERRIDE[show]
    if show in cache["show_syn"] and cache["show_syn"][show]:
        return cache["show_syn"][show]
    ym = _QYEAR.search(show)
    year = ym.group(1) if ym else None
    clean = show[:ym.start()].strip() if ym else show
    ov = ""
    if TMDB_KEY:
        params = {"query": clean}
        if year:
            params["first_air_date_year"] = year
        data = _tmdb_get("/search/tv", **params) or {}
        for res in (data.get("results") or [])[:3]:
            if res.get("overview"):
                ov = res["overview"].strip(); break
    if not ov and ENABLE_TVMAZE:
        d = _tvmaze_get("/singlesearch/shows", q=clean)
        if isinstance(d, dict) and d.get("summary"):
            ov = html.unescape(_TAGS.sub("", d["summary"])).strip()
    if not ov and ENABLE_TVDB:
        tid = _resolve_tvdb_id(clean, cache)
        if tid:
            d = _tvdb_get(f"/series/{tid}") or {}
            ov = (d.get("overview") or "").strip()
    if not ov and TMDB_KEY:                      # movie fallback (films / specials)
        params = {"query": clean}
        if year:
            params["year"] = year
        data = _tmdb_get("/search/movie", **params) or {}
        for res in (data.get("results") or [])[:3]:
            if res.get("overview"):
                ov = res["overview"].strip(); break
    cache["show_syn"][show] = ov
    return ov


# ── absolute-episode resolution (option B): number -> (season, ep, overview, name) ──
def _tvmaze_eplist_se(tid, cache):
    """Flat list of (season, number, overview, name) in air order (0 = ep 1)."""
    key = "se:" + str(tid)
    if key in cache["tvmaze_eplist"]:
        return cache["tvmaze_eplist"][key]
    data = _tvmaze_get(f"/shows/{tid}/episodes")
    lst = []
    if isinstance(data, list):
        for e in data:
            if e.get("season") and e.get("number"):
                lst.append([e["season"], e["number"],
                            html.unescape(_TAGS.sub("", e.get("summary") or "")).strip(),
                            (e.get("name") or "").strip()])
    cache["tvmaze_eplist"][key] = lst
    return lst


def _tmdb_seasons(sid, cache):
    key = str(sid)
    if key in cache["tmdb_seasons"]:
        return cache["tmdb_seasons"][key]
    data = _tmdb_get(f"/tv/{sid}")
    seasons = []
    if data:
        for s in data.get("seasons", []):
            if s.get("season_number", 0) >= 1 and s.get("episode_count"):
                seasons.append([s["season_number"], s["episode_count"]])
    seasons.sort()
    cache["tmdb_seasons"][key] = seasons
    return seasons


def absolute_se(show, absN, cache):
    """Map an ABSOLUTE episode number to (season, ep, overview, name)."""
    if not absN or absN < 1:
        return None, None, "", ""
    # Prefer TMDB's season structure (deterministic), then pull that episode.
    sid = _resolve_show_id(show, cache) if TMDB_KEY else None
    if sid:
        rem = absN
        for snum, cnt in _tmdb_seasons(sid, cache):
            if rem <= cnt:
                ov, nm = _tmdb_meta(show, snum, rem, cache)
                if ov or nm:
                    return snum, rem, ov, nm
                break
            rem -= cnt
    # Fallback: TVmaze flat air-order list already carries S/E + text.
    tid = _resolve_tvmaze_id(show, cache)
    if tid:
        lst = _tvmaze_eplist_se(tid, cache)
        if 1 <= absN <= len(lst):
            s, n, o, nm = lst[absN - 1]
            return s, n, o, nm
    return None, None, "", ""


# ── recover each programme's absolute episodeNumber from the live playlists ──
def _api_get(path):
    for _ in range(3):
        try:
            r = _sess.get(API_ENDPOINT + path, timeout=25)
            if r.status_code == 200:
                return r.json()
            return None
        except requests.RequestException:
            time.sleep(1)
    return None


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _as_int(v):
    """episodeNumber arrives as int or str; coerce, else None."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None

def build_epindex(base_sched, dates):
    """{base_sched -> sorted [(epoch_seconds, info_dict)]} from the live feed.

    info_dict carries the feed's OWN metadata for that air-time:
      num      -> absolute episodeNumber (int or None)
      episode  -> the real episode sub-title  (info.episode)  <-- ground truth
      fullname -> canonical show display name (info.fullname)
      image    -> poster URL                  (info.image)
      year     -> release year                (info.year)
      name     -> the feed's show slug/name   (top-level 'name')
    Using the feed's own info.episode as the sub-title is what keeps us honest:
    it's the same source the official site/TAM shows, so we never have to GUESS
    an episode name from an absolute-number lookup (that was the Dragnet bug)."""
    seen_pl, rows = set(), []
    for d in sorted(dates):
        lst = _api_get(f"/playlists?scheduleName={quote(base_sched)}"
                       f"&startDate={d}T00:00:00.000Z&thisWeek=true&weekStartDay=monday")
        if not isinstance(lst, list):
            continue
        for p in lst:
            pid = p.get("_id")
            if not pid or pid in seen_pl:
                continue
            seen_pl.add(pid)
            content = _api_get(f"/playlist?id={pid}&addInfo=true")
            pl = (content or {}).get("playlist") or {}
            for b in pl.get("blocks", []):
                for m in b.get("mediaList", []):
                    st = m.get("startDate")
                    if not st:
                        continue
                    info = m.get("info") or {}
                    episode  = info.get("episode")  or info.get("episodeName") or info.get("subtitle") or ""
                    fullname = info.get("fullname") or info.get("fullName")    or info.get("showName") or ""
                    image    = info.get("image")    or info.get("img")         or ""
                    rows.append((_dt(st).timestamp(), {
                        "num":      _as_int(m.get("episodeNumber")),
                        "episode":  str(episode).strip(),
                        "fullname": str(fullname).strip(),
                        "image":    str(image).strip(),
                        "year":     (str(info.get("year")).strip() if info.get("year") else ""),
                        "name":     (m.get("name") or "").strip(),
                    }))
    rows.sort(key=lambda r: r[0])
    return rows


def lookup_epinfo(rows, target_ts):
    """Nearest feed info_dict to target_ts within MATCH_TOL_S, else None."""
    if not rows:
        return None
    keys = [r[0] for r in rows]
    i = bisect.bisect_left(keys, target_ts)
    best = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(rows):
            d = abs(rows[j][0] - target_ts)
            if d <= MATCH_TOL_S and (best is None or d < best[0]):
                best = (d, rows[j][1])
    return best[1] if best else None


# ── TAM episode-name source for Snick/Nick ─────────────────────────────────
# The feed gives Snick programmes only an episodeNumber, no name. The user's own
# TAM repo (toonamiaftermath-cli) resolves those names and publishes them in its
# index.xml; we treat THAT as the authoritative Snick episode name, then derive
# S/E + descriptions from the normal name-match (DB) path. No manual work.
TAM_INDEX_URL = os.environ.get(
    "TAM_INDEX_URL", "https://raw.githubusercontent.com/s-digweed/TAM/main/index.xml")
TAM_CH_FOR = {              # our channel id -> TAM index channel id
    "Snickelodeon EST": "3",
    "Snickelodeon EST+180": "4",
    "ToonamiAftermath.us@East": "1",   # TAM also carries S/E + synopsis for the
    "ToonamiAftermath.us@West": "2",   # Toonami anime the databases don't index
}
TAM_TOL_S = 300             # cross-source air-time tolerance (feeds drift a little)

def _decode_xmltv_ns(text):
    """XMLTV 'season.episode.part' (zero-based) -> (season, episode) 1-based.
    '1.14.0/1' -> (2, 15).  None if it has no season or episode number."""
    if not text:
        return None
    m = re.match(r"\s*(\d+)?\s*\.\s*(\d+)?\s*\.", text)
    if not m or m.group(1) is None or m.group(2) is None:
        return None
    return (int(m.group(1)) + 1, int(m.group(2)) + 1)

def _xmltv_ts(start):
    """Parse 'YYYYmmddHHMMSS ±HHMM' to a true UTC epoch. Honoring the offset is
    essential: TAM's West channel is published at -0300, so ignoring it put West
    three hours off and it matched nothing."""
    parts = (start or "").split()
    if not parts:
        return None
    try:
        dt = datetime.strptime(parts[0], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    off = 0
    if len(parts) > 1 and len(parts[1]) == 5 and parts[1][0] in "+-":
        try:
            off = (1 if parts[1][0] == "+" else -1) * (
                int(parts[1][1:3]) * 3600 + int(parts[1][3:5]) * 60)
        except ValueError:
            off = 0
    return dt.timestamp() - off   # local wall-clock - offset = UTC

def load_tam_index():
    """{TAM channel id -> sorted [(ts, title, sub, se, desc)]} from TAM's index.xml.
    TAM is a FULL metadata source, not just a name bank: each programme carries the
    episode name (<sub-title>), the S/E (as xmltv_ns <episode-num>) AND a real
    episode synopsis (<desc>). We keep the show TITLE so data is only adopted when
    TAM has the SAME show at that air-time (schedules can differ at a timestamp)."""
    out = {}
    try:
        r = _sess.get(TAM_INDEX_URL, timeout=30)
        if r.status_code != 200:
            print(f"  TAM index: HTTP {r.status_code}; TAM enrichment will fall back")
            return out
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  TAM index: fetch/parse failed ({e}); TAM enrichment will fall back")
        return out
    for p in root.findall("programme"):
        cid = p.get("channel")
        title = (p.findtext("title") or "").strip()
        sub = (p.findtext("sub-title") or "").strip()
        desc = (p.findtext("desc") or "").strip()
        se = None
        for en in p.findall("episode-num"):
            if (en.get("system") or "").lower() in ("xmltv_ns", ""):
                se = _decode_xmltv_ns(en.text)
                if se:
                    break
        ts = _xmltv_ts(p.get("start"))
        # keep any row that carries at least one useful field for this slot
        if cid and title and ts is not None and (sub or se or desc):
            out.setdefault(cid, []).append((ts, title, sub, se, desc))
    for cid in out:
        out[cid].sort(key=lambda r: r[0])
    n = sum(len(v) for v in out.values())
    print(f"  TAM index: {n} programmes across channels {sorted(out)}")
    return out

def _title_matches(a, b):
    """True if two show titles refer to the same show (case/punct-insensitive,
    substring either way, or a strong fuzzy ratio)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.80

def tam_lookup(tam_index, our_channel, target_ts, our_titles):
    """Nearest TAM row {name, se, desc} for an air-time whose TAM title matches
    our show, or None if TAM has a different (or no) show at that time. TAM pairs
    the episode name with its S/E and a real synopsis, so this is a full source."""
    cid = TAM_CH_FOR.get(our_channel)
    rows = tam_index.get(cid) if cid else None
    if not rows or target_ts is None:
        return None
    cands = [t for t in our_titles if t]
    keys = [r[0] for r in rows]
    i = bisect.bisect_left(keys, target_ts)
    best = None
    # expand outward while within tolerance, keep only same-show rows, nearest wins
    for j in range(i - 4, i + 5):
        if 0 <= j < len(rows):
            ts_j, title_j, sub_j, se_j, desc_j = rows[j]
            d = abs(ts_j - target_ts)
            if d <= TAM_TOL_S and any(_title_matches(title_j, c) for c in cands):
                if best is None or d < best[0]:
                    best = (d, {"name": sub_j, "se": se_j, "desc": desc_j})
    return best[1] if best else None


# ══════════════════════════════════════════════════════════════════════════
# Enrichment
# ══════════════════════════════════════════════════════════════════════════
def _set_child(prog, tag, text, attrib=None):
    """Replace (or create) a single child element with given text/attrib."""
    for e in prog.findall(tag):
        prog.remove(e)
    if text is None and not attrib:
        return
    e = ET.SubElement(prog, tag, attrib or {})
    if text is not None:
        e.text = text


def _reorder(prog):
    """Keep XMLTV child order valid: title, sub-title, desc, date, episode-num, icon."""
    order = {"title": 0, "sub-title": 1, "desc": 2, "date": 3, "episode-num": 4, "icon": 5}
    kids = list(prog)
    for k in kids:
        prog.remove(k)
    for k in sorted(kids, key=lambda e: order.get(e.tag, 9)):
        prog.append(k)


def _prog_start_ts(prog):
    s = (prog.get("start") or "").split()[0]
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def enrich(path):
    tree = ET.parse(path)
    root = tree.getroot()
    cache = load_cache()
    progs = [p for p in root.findall("programme") if p.get("channel") in TARGET_CHANNELS]

    # ── build the absolute-episodeNumber index from the live feed (per base) ──
    # Collect, per base scheduleName, the set of local air-dates we need to cover
    # (programme start minus the channel's stream delay), then pull those playlists.
    want = {}   # base_sched -> set("YYYY-MM-DD")
    for p in progs:
        base, delay = CH_MAP[p.get("channel")]
        ts = _prog_start_ts(p)
        if ts is None:
            continue
        d = datetime.fromtimestamp(ts - delay * 60, tz=timezone.utc).strftime("%Y-%m-%d")
        want.setdefault(base, set()).add(d)
    epindex = {}
    for base, dates in want.items():
        try:
            epindex[base] = build_epindex(base, dates)
            print(f"  episodeNumber index [{base}]: {len(epindex[base])} entries "
                  f"across {len(dates)} day(s)")
        except Exception as e:
            print(f"  warn: could not build episode index for {base}: {e}")
            epindex[base] = []

    # ── Dump the raw live feed to feed_index.tsv (same data your manual curl TSV
    #    pulled) so it's always fresh in-repo and you never curl by hand. One row
    #    per aired slot: a BLANK episodeNumber column = the API gave no number for
    #    that slot (the true source gap); a populated one just needs a guide entry.
    try:
        fi = os.path.join(os.path.dirname(os.path.abspath(path)), "feed_index.tsv")
        with open(fi, "w", encoding="utf-8") as fh:
            fh.write("base\tstartDate\tshow_name\tepisodeNumber\tepisode_name\tfullname\n")
            for base in sorted(epindex):
                for ts, info in epindex[base]:
                    iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    num = info.get("num")
                    fh.write(f"{base}\t{iso}\t{info.get('name','')}\t"
                             f"{'' if num is None else num}\t{info.get('episode','')}\t"
                             f"{info.get('fullname','')}\n")
        print(f"  -> {fi}: raw feed index dumped ({sum(len(v) for v in epindex.values())} rows)")
    except Exception as e:
        print(f"  warn: could not write feed_index.tsv: {e}")

    # Snick episode names come from the TAM feed (the feed itself has none).
    tam_index = load_tam_index() if any(c in TAM_CH_FOR for c in TARGET_CHANNELS) else {}
    MASTER_MAP = load_master_map()
    GUIDE = load_guide()

    stats = {"progs": 0, "by_name": 0, "by_absolute": 0, "show_level": 0,
             "no_desc": 0, "ep_name_added": 0, "icons": 0, "se_dropped": 0,
             "se_pinned": 0, "from_tam": 0, "by_tam": 0, "by_repeat": 0, "by_master": 0,
             "by_guide": 0, "by_abs_snick": 0,
             "desc_episode": 0, "desc_generic": 0, "desc_none": 0, "desc_show_fixed": 0}
    dropped_se = set()    # (show, episode) name present but S/E withheld -> pin it
    abs_snick = {}        # (disp_show, absN) -> (S, E, name) auto-resolved nameless Snick -> review worklist
    generic_desc = set()  # (show, episode, reason) desc is generic/none -> DESC_OVERRIDE worklist
    records = []          # per-programme resolution, for repeat-fill + the desc report
    numfill = {}          # (norm show, feed episodeNumber) -> (name, (S,E), episode desc)
    for prog in progs:
        stats["progs"] += 1
        raw = (prog.findtext("title") or "").strip()
        if not raw:
            continue
        sp = special_title(raw)
        ovr = TITLE_OVERRIDES.get(raw)
        force_no_sub = False
        # resolve lookup show, on-screen title, pinned desc/date, and no-S/E flag
        if ovr:
            show = ovr.get("lookup", raw)
            disp_title = ovr.get("display")
            forced_desc = ovr.get("desc")
            year = ovr.get("date")
            no_se = ovr.get("no_se", False)
        elif sp and sp.get("season"):              # MST3K (keeps S/E)
            show = sp["show"]
            disp_title = "Mystery Science Theater 3000"
            forced_desc = None
            year = None
            no_se = False
        elif sp and sp.get("shorts"):              # RiffTrax Shorts
            show = "RiffTrax"
            disp_title = "RiffTrax Shorts (2007)"
            forced_desc = RIFFTRAX_SHORTS_DESC
            year = "2007"
            no_se = True
            force_no_sub = True
        elif sp:                                   # RiffTrax feature riff
            show = "RiffTrax"
            no_se = True
            rt = RIFFTRAX_MAP.get(_norm(sp.get("sub", "")))
            if rt:
                disp_title, year, forced_desc = rt
                force_no_sub = True                # the title already carries the film + year
            else:
                disp_title = "RiffTrax"
                year = None
                forced_desc = SHOW_DESC_OVERRIDE["RiffTrax"]
        elif raw in DISPLAY_CANON:                 # display-name fix (keep S/E)
            show = DISPLAY_CANON[raw]
            disp_title = DISPLAY_CANON[raw]
            forced_desc = None
            year = None
            no_se = False
        else:
            show = ALIAS.get(raw, raw)
            disp_title = None
            forced_desc = None
            year = None
            no_se = False

        is_curated = show in CURATED_SHOWS         # '60s cartoons: pins only, never guess S/E
        base, delay = CH_MAP[prog.get("channel")]
        ts = _prog_start_ts(prog)

        # ── pull the feed's OWN metadata for this air-time (ground truth) ──
        feed = lookup_epinfo(epindex.get(base, []), ts - delay * 60) if ts is not None else None
        feed_ep   = (feed or {}).get("episode", "")   # the real episode sub-title
        feed_num  = (feed or {}).get("num")           # absolute episodeNumber
        feed_img  = (feed or {}).get("image", "")     # poster
        # Episode-name precedence: the Toonami feed's own info.episode, else the
        # TAM feed's name for Snick (the feed itself has none), else the grabber's.
        grab_sub = (prog.findtext("sub-title") or "").strip()
        # TAM's row for this exact slot (name + S/E + synopsis). Never for the
        # curated '60s cartoons — those use hand-pinned data only.
        tam_row = (tam_lookup(tam_index, prog.get("channel"), ts, [raw, disp_title or show])
                   if not is_curated else None)
        tam_ep = (tam_row["name"] if (tam_row and not feed_ep) else "")
        if tam_ep:
            stats["from_tam"] += 1
        orig_sub = feed_ep or tam_ep or grab_sub
        clean_sub = clean_chapter(orig_sub)        # roman->arabic, drop comma before Chapter
        epname = clean_sub or (sp.get("sub") if sp else "")
        absN = feed_num
        # ── MASTER MAP (hand-verified Snick sheet, keyed by show + episodeNumber).
        #    Authoritative: a manual name/desc/S-E wins over everything; an S/E-only
        #    entry sets the badge and lets the DBs fill name+desc by that S/E.
        mm_hit = (MASTER_MAP.get(f"{_norm(raw)}|{absN}")
                  if (absN is not None and not is_curated) else None)
        mm_se   = tuple(mm_hit["se"]) if (mm_hit and mm_hit.get("se")) else None
        mm_sub  = (mm_hit.get("sub")  or "") if mm_hit else ""
        mm_desc = (mm_hit.get("desc") or "") if mm_hit else ""
        if mm_sub:                                  # manual episode name is the sub-title
            orig_sub = mm_sub
            clean_sub = clean_chapter(orig_sub)
            epname = clean_sub
        if mm_desc:                                 # manual synopsis wins (via forced_desc)
            forced_desc = mm_desc
        # ── AUTHORITATIVE EPISODE GUIDE (episode_guide.json) ──────────────
        #    For shows TMDB mis-resolves (segmented cartoons etc.), the static
        #    guide maps the feed's absolute episodeNumber straight to the real
        #    broadcast half-hour: correct S/E + segment-joined name + synopsis.
        #    Authoritative — it overrides the feed/TAM/DB name and every resolver
        #    below; only the hand master map (mm_*) outranks it. Keyed by the
        #    canonical display name (and raw/lookup as fallbacks).
        g_show = (GUIDE.get(_norm(disp_title or show)) or GUIDE.get(_norm(raw))
                  or GUIDE.get(_norm(show))) if not is_curated else None
        g_ent = (g_show["episodes"].get(str(absN))
                 if (g_show and absN is not None) else None)
        g_se   = tuple(g_ent["se"]) if (g_ent and g_ent.get("se")) else None
        g_sub  = (g_ent.get("sub")  or "") if g_ent else ""
        g_desc = (g_ent.get("desc") or "") if g_ent else ""
        g_omit = bool(g_ent) and g_ent.get("sub", "") == ""   # guide says: no subtitle
        if g_ent and not mm_sub:                    # guide subtitle wins over feed/TAM
            if g_sub:
                orig_sub = g_sub
                clean_sub = clean_chapter(orig_sub)
                epname = clean_sub
            elif g_omit:
                orig_sub = ""; clean_sub = ""; epname = ""
                force_no_sub = True
        if g_desc and not mm_desc:
            forced_desc = g_desc
        # TAM's S/E + synopsis describe the SAME episode we're showing only when
        # the identity agrees: the name came from TAM (Snick has no feed name), or
        # our episode name matches TAM's. Otherwise it's a different episode -> skip.
        tam_trust = bool(tam_row and epname and (
            (not feed_ep)
            or _norm(epname) == _norm(tam_row["name"])
            or _segkey(epname) == _segkey(tam_row["name"])
            or bool(set(_segments(epname)) & set(_segments(tam_row["name"])))
        ))
        tam_desc_use = tam_row["desc"] if (tam_row and tam_row["desc"] and tam_trust) else ""

        desc = ""
        season = ep = None
        ov = nm = ""
        source = None
        seg_fullname = ""
        pinned_full = ""
        pinned_syn = ""
        desc_kind = "episode"; desc_reason = ""
        try:
            if not no_se:
                # 0*) MASTER MAP S/E — hand-verified, wins over every resolver below.
                #     If you also supplied a synopsis it's already locked in via
                #     forced_desc; an S/E-only entry fetches name+desc from the DBs.
                if mm_se and season is None:
                    season, ep = mm_se
                    source = "master"
                    stats["by_master"] += 1
                    if not mm_desc:
                        ov, nm = episode_meta(show, season, ep, cache)
                # 0†) AUTHORITATIVE GUIDE S/E — real broadcast S/E for the feed's
                #     absolute episodeNumber. Beats every automatic resolver; we do
                #     NOT call episode_meta here (TMDB mis-keys these shows) — the
                #     name/synopsis already came from the guide above, and a missing
                #     synopsis falls to the show-level blurb rather than a wrong DB one.
                if g_se and season is None:
                    season, ep = g_se
                    source = "guide"
                    stats["by_guide"] += 1
                # 0) PINNED IMDb broadcast episodes for segment-based cartoons
                #    (Spider-Man '67, Hulk '66) -> correct S/E + full title + the
                #    IMDb synopsis for that episode (may be '' -> series blurb).
                #    We never call episode_meta() for curated shows: a per-(show,
                #    S,E) metadata lookup matches a WRONG same-named series (1994
                #    Spider-Man, 1978/1996 Hulk) and attaches a wrong synopsis.
                ps = pinned_lookup(show, epname) if (epname and is_curated) else None
                if ps:
                    season, ep, pinned_full, pinned_syn = ps
                    source = "name"
                # Birdman airs one segment per slot: prefer that segment's own IMDb
                # synopsis over the triplet's (more accurate for the aired short).
                # A segment with no triplet match still has no S/E (the "unknowns").
                if is_curated and show == "Birdman and the Galaxy Trio" and epname:
                    seg_syn = birdman_syn_lookup(epname)
                    if seg_syn:
                        pinned_syn = seg_syn
                # 0a) MANUAL S/E pins (always win) — you fill these from missing_se.txt
                #     keyed by the DISPLAY name (what the report logs / you see).
                if season is None and not is_curated:
                    mp = _se_pin(disp_title or show, epname)
                    if mp:
                        season, ep = mp
                        ov, nm = episode_meta(show, season, ep, cache)
                        source = "name"
                        stats["se_pinned"] += 1
                # 0b) explicit episode pin: (lookup show, normalized episode name)
                pin = EPISODE_SE_OVERRIDE.get((show, _norm(epname))) if (epname and season is None) else None
                if pin:
                    season, ep = pin
                    ov, nm = episode_meta(show, season, ep, cache)
                    source = "name"
                # 0b) MST3K titles carry their own S##E##
                if season is None and sp and sp.get("season"):
                    season, ep = sp["season"], sp["ep"]
                    ov, nm = episode_meta(show, season, ep, cache)
                    source = "name"
                # 0c) "Episode #S.E" placeholder names ENCODE the S/E -> decode it
                #     (reliable, not a guess: the number is literally in the name).
                if season is None and not is_curated:
                    dec = _decode_placeholder_se(epname)
                    if dec:
                        season, ep = dec
                        ov, nm = episode_meta(show, season, ep, cache)
                        source = "name"
                # 1) episode NAME match (exact / segment, best for Western cartoons)
                #    Skipped for pinned segment cartoons: if a segment isn't in the
                #    pinned list (e.g. a season we don't have), we do NOT guess S/E.
                if season is None and epname and not is_curated:
                    cands = [epname]
                    for sep in (" / ", "/", " - "):
                        if sep in epname:
                            cands.append(epname.split(sep)[0].strip())
                    for cand in cands:
                        season, ep = _name_to_se(show, cand, cache)
                        if season:
                            break
                    if season:
                        ov, nm = episode_meta(show, season, ep, cache)
                        source = "name"
                        # if we matched a HALF of a paired DB title, show the full pair
                        if nm and "/" in nm and _norm(epname) in _segments(nm):
                            seg_fullname = nm
                # 1b) FUZZY episode-name match — the feed's English name is usually
                #     only a spelling hair off the DB's; a high-cutoff fuzzy match
                #     (still NAME-based, so reliable) recovers those automatically.
                if season is None and epname and not is_curated:
                    s15, e15 = _name_to_se_fuzzy(show, epname, cache)
                    if s15:
                        season, ep = s15, e15
                        ov, nm = episode_meta(show, season, ep, cache)
                        source = "name"
                # 1c) TAM-DIRECT — only as a GAP-FILLER, after the existing name/DB
                #     matches and BEFORE the risky absolute guess. TAM pairs the
                #     episode name with its S/E (xmltv_ns) + a real synopsis, so when
                #     TAM's slot is the same episode we're showing (tam_trust) we take
                #     them straight. Gated on `season is None`, so it NEVER overrides a
                #     manual pin/override or anything name/DB already resolved — it only
                #     fills what would otherwise have fallen to a generic blurb. Being
                #     reliable (name-paired), it also pre-empts the absolute tier, which
                #     cuts down on withheld S/E.
                if season is None and not is_curated and tam_row and tam_row["se"] and tam_trust:
                    season, ep = tam_row["se"]
                    if tam_desc_use:
                        ov = tam_desc_use
                    nm = nm or tam_row["name"]
                    source = "tam"
                    stats["by_tam"] += 1
                # 2) ABSOLUTE episodeNumber from the feed, as corroboration only.
                #    The feed numbers sequentially but DB season layouts often
                #    disagree (the Dragnet bug), so we TRUST the absolute map only
                #    when the episode it lands on AGREES with the episode name we
                #    already have (from the feed or TAM). Otherwise keep the name
                #    and WITHHOLD S/E, logging it for an optional SE_PINS entry.
                #    If we have no name at all, we do NOT invent one.
                if season is None and absN and epname and not is_curated:
                    s2, e2n, ov2, nm2 = absolute_se(show, absN, cache)
                    if s2:
                        agree = bool(nm2) and (
                            _norm(nm2) == _norm(epname)
                            or _segkey(nm2) == _segkey(epname)
                            or bool(set(_segments(nm2)) & set(_segments(epname)))
                        )
                        if agree:
                            season, ep, ov, nm = s2, e2n, ov2, nm2
                            source = "absolute"
                        else:
                            dropped_se.add((disp_title or show, epname, absN))
                            stats["se_dropped"] += 1
                # 2b) ABSOLUTE for NAMELESS slots (Path 1, user-requested) — the
                #     Snick/live-action feed gives only an episodeNumber (no feed name,
                #     and Snick isn't in TAM/master/pins for this airing), so there is
                #     nothing to corroborate against. At your request we resolve S/E
                #     straight from the absolute index and let the DBs supply BOTH an
                #     episode name — TMDB first, so anime/kids shows get clean English
                #     titles — and a synopsis (OMDb/IMDb first). Strictly additive:
                #     gated on `not epname`, so it only touches slots that would
                #     otherwise read "(no episode name)" + a show-level blurb; it never
                #     overrides the master map, a pin, TAM, a name match, or the
                #     corroborated absolute tier (all of which run above and set
                #     season/epname). Every hit is logged to abs_snick.txt so you can
                #     promote the good ones into the master map (which then wins here).
                if (season is None and absN and not epname
                        and not is_curated and ALLOW_ABS_SNICK):
                    s3, e3, ov3, nm3 = absolute_se(show, absN, cache)
                    if s3:
                        season, ep = s3, e3
                        ov2, nm2 = episode_meta(show, season, ep, cache)
                        ov = ov2 or ov3
                        nm = nm2 or nm3
                        if nm:
                            orig_sub = nm
                            clean_sub = clean_chapter(orig_sub)
                            epname = clean_sub
                        source = "abs_snick"
                        stats["by_abs_snick"] += 1
                        abs_snick[(disp_title or show, absN)] = (season, ep, nm or "")
            # 3) description — also classify it (episode-specific vs generic vs none)
            #    so we can report what still needs a real episode synopsis.
            dov = _desc_override(disp_title or show, epname)
            desc_kind = "episode"; desc_reason = ""
            if dov:                              # hand-supplied episode description wins
                desc = dov
            elif forced_desc:
                desc = forced_desc
                if forced_desc == SHOW_DESC_OVERRIDE.get("RiffTrax"):
                    desc_kind, desc_reason = "generic", "RiffTrax house blurb (no per-riff synopsis)"
            elif is_curated:
                if pinned_syn:
                    desc = pinned_syn
                else:
                    desc = show_overview(show, cache)
                    desc_kind, desc_reason = "generic", "pinned cartoon: no synopsis supplied for this segment"
            elif season is not None:
                if ov:
                    desc = ov
                elif tam_desc_use:                 # TAM's own episode synopsis
                    desc = tam_desc_use
                else:
                    desc = show_overview(show, cache)
                    desc_kind, desc_reason = "generic", "S/E set but no DB/OMDb/TAM episode overview"
            elif tam_desc_use:                     # no S/E, but TAM gave a real synopsis
                desc = tam_desc_use
            else:
                desc = show_overview(show, cache)
                desc_kind, desc_reason = "generic", "no S/E resolved -> show-level blurb"
            # A hand-supplied SHOW-LEVEL blurb (SHOW_DESC_OVERRIDE) is an intentional,
            # final description for a show with no episode identity -> mark it resolved
            # ("show") so it drops off the worklist. Curated '60s cartoons keep their
            # "pinned cartoon: no synopsis" flag (handled above, is_curated).
            if (desc_kind == "generic" and not is_curated
                    and desc and desc == SHOW_DESC_OVERRIDE.get(disp_title or show)):
                desc_kind, desc_reason = "show", ""
            if not desc:
                desc_kind, desc_reason = "none", "no description found anywhere"
            # (description classification + the generic_desc report are tallied
            #  AFTER the repeat-fill pass below, from the per-programme records.)
            # tally how S/E was resolved
            if season is not None:
                if source in ("tam", "master", "guide", "abs_snick"):
                    pass                      # already counted in by_tam/by_master/by_guide/by_abs_snick
                else:
                    stats["by_name" if source == "name" else "by_absolute"] += 1
            else:
                stats["show_level"] += 1
        except Exception as e:
            print(f"  warn: {raw!r}: {e}")
            desc = desc or forced_desc or show_overview(show, cache)

        # targeted (show, season, ep) subtitle override (e.g. Sailor Moon S1 finale)
        _ovr_name = EP_SE_SUBTITLE.get((show, season, ep)) if season is not None else None
        if _ovr_name:
            orig_sub = _ovr_name
            clean_sub = clean_chapter(orig_sub)
            epname = clean_sub
            force_no_sub = False
        # sub-title: the feed's own info.episode (via orig_sub) is authoritative, so
        # we SET it rather than only patch the grabber's value (the grabber often
        # dropped it). RiffTrax carries everything in the title -> no sub.
        if force_no_sub:
            for e in prog.findall("sub-title"):
                prog.remove(e)
        else:
            ov_sub = _subtitle_override(disp_title or show, orig_sub)
            if pinned_full:                                  # curated cartoon full pair/triplet
                sub_text = pinned_full
            elif ov_sub:                                     # explicit correction (accent/split/alt-name)
                sub_text = ov_sub
            elif _decode_placeholder_se(orig_sub):
                # bare "Episode #2.19" placeholder with no real name supplied:
                # the S/E is already decoded onto the badge, so don't show the ugly
                # placeholder as the episode title -> leave it blank.
                sub_text = ""
            elif orig_sub:
                # feed/grabber episode name. Standardize the '/' separator (" / ").
                # Commas are normally part of a single title ("Patty, the Witness"),
                # so we DON'T split on them -- EXCEPT for segment anthologies, where
                # the feed comma-joins whole segment titles (Iron Man, Thor, Hulk...).
                base_for_seg = disp_title or show
                if base_for_seg in SEGMENT_SHOWS and "," in clean_sub and "/" not in clean_sub:
                    clean_sub = " / ".join(s.strip() for s in clean_sub.split(",") if s.strip())
                sub_text = " / ".join(s.strip() for s in re.split(r"\s*/\s*", clean_sub) if s.strip())
                # normalize "(Part N)" -> ": Part N" (keeps "(Finale)" etc. as-is)
                sub_text = re.sub(r"\s*\(Part\s+(\d+)\)", r": Part \1", sub_text)
            else:                                            # last-ditch backfill (e.g. Snick best-effort)
                sub_text = (sp.get("sub") if sp else "") or (nm if season is not None else "")
                if sub_text:
                    stats["ep_name_added"] += 1
            if sub_text:
                _set_child(prog, "sub-title", sub_text, {"lang": "en"})
            elif _decode_placeholder_se(orig_sub):
                for e in prog.findall("sub-title"):   # drop a stale "Episode #x.y"
                    prog.remove(e)

        # Season-named shows: display "<show> - <season name>" based on the resolved
        # season (e.g. Rurouni Kenshin S2 -> "Rurouni Kenshin - Legend of Kyoto").
        if season is not None and show in SEASON_TITLE and season in SEASON_TITLE[show]:
            disp_title = f"{show} - {SEASON_TITLE[show][season]}"

        if disp_title:
            te = prog.find("title")
            if te is not None:
                te.text = disp_title

        if desc:
            _set_child(prog, "desc", desc, {"lang": "en"})
        else:
            stats["no_desc"] += 1
        if year:                                   # year field for movies / long specials
            _set_child(prog, "date", str(year))
        if season is not None:
            _set_child(prog, "episode-num",
                       f"{season - 1}.{ep - 1}.", {"system": "xmltv_ns"})
            e2 = ET.SubElement(prog, "episode-num", {"system": "onscreen"})
            e2.text = f"S{season:02d} E{ep:02d}"
        # Rewrite the grabber's non-standard <image>URL</image> (which players
        # ignore) into a proper <icon src="URL"/> so posters actually show.
        for im in prog.findall("image"):
            src = (im.get("src") or (im.text or "")).strip()
            prog.remove(im)
            if src and prog.find("icon") is None:
                ET.SubElement(prog, "icon", {"src": src})
                stats["icons"] += 1
        # feed poster fallback (info.image) when the grab carried none
        if feed_img and prog.find("icon") is None:
            ET.SubElement(prog, "icon", {"src": feed_img})
            stats["icons"] += 1
        _reorder(prog)

        # record this programme for the repeat-fill pass + the description report.
        final_sub = (prog.findtext("sub-title") or "").strip()
        rec = {"prog": prog, "show": disp_title or show, "absN": absN,
               "se": (season, ep) if season is not None else None, "desc": desc,
               "kind": desc_kind, "reason": desc_reason, "epname": epname,
               "sp_sub": (sp.get("sub") if sp else ""),
               "curated": is_curated, "no_sub": force_no_sub}
        records.append(rec)
        # seed the (show, feed episodeNumber) -> full resolution map from airings we
        # fully resolved (name + S/E + a real episode synopsis).
        if (desc_kind == "episode" and final_sub and rec["se"] and absN is not None
                and not is_curated and not force_no_sub):
            numfill.setdefault((_norm(disp_title or show), absN),
                               (final_sub, rec["se"], desc))

    # ── REPEAT-FILL: Toonami Aftermath loops its schedule and the feed stamps
    #    every airing with an episodeNumber. An episode we FULLY resolved at one
    #    airing (name + S/E + a real synopsis) can therefore backfill its OTHER
    #    airings that arrived with no name — matched by (show, episodeNumber).
    #    Same show + same feed number = same episode, so it's reliable and needs
    #    no external source. Manual pins/overrides and TAM already won in pass 1;
    #    this only touches airings still lacking an episode-specific description,
    #    and only copies from a sibling that has the full set. ──
    for rec in records:
        if rec["kind"] == "episode" or rec["curated"] or rec["no_sub"]:
            continue
        if rec["absN"] is None:
            continue
        hit = numfill.get((_norm(rec["show"]), rec["absN"]))
        if not hit:
            continue
        sub_text, (s, e), dsc = hit
        prog = rec["prog"]
        _set_child(prog, "sub-title", sub_text, {"lang": "en"})
        _set_child(prog, "episode-num", f"{s - 1}.{e - 1}.", {"system": "xmltv_ns"})
        e2 = ET.SubElement(prog, "episode-num", {"system": "onscreen"})
        e2.text = f"S{s:02d} E{e:02d}"
        _set_child(prog, "desc", dsc, {"lang": "en"})
        _reorder(prog)
        rec.update(kind="episode", reason="", se=(s, e), desc=dsc)
        stats["by_repeat"] += 1

    # ── classify every programme's <desc> (AFTER repeat-fill) for the report ──
    for rec in records:
        if rec["kind"] == "episode":
            stats["desc_episode"] += 1
        elif rec["kind"] == "show":                 # intentional hand-supplied show blurb
            stats["desc_show_fixed"] += 1           # resolved -> NOT on the worklist
        else:
            label = rec["epname"] or rec["sp_sub"] or "(no episode name)"
            generic_desc.add((rec["show"], label, rec["reason"], rec["absN"]))
            stats["desc_generic" if rec["kind"] == "generic" else "desc_none"] += 1

    # ── close the 1-2 min gaps between programmes: per channel, each show's
    #    stop is stretched (or trimmed) to meet the next show's start, so the
    #    timeline is fully contiguous with no blank slots. ──
    from collections import defaultdict
    bych = defaultdict(list)
    for p in root.findall("programme"):
        bych[p.get("channel")].append(p)
    gaps_closed = 0
    for cid, plist in bych.items():
        plist.sort(key=lambda p: p.get("start") or "")
        for a, b in zip(plist, plist[1:]):
            if a.get("stop") != b.get("start"):
                a.set("stop", b.get("start"))
                gaps_closed += 1

    save_cache(cache)
    tree.write(path, encoding="UTF-8", xml_declaration=True)

    # ── gap report: every (show, episode) where we WITHHELD an S/E because the
    #    absolute-number guess didn't agree with the feed's episode name. This is
    #    your worklist: drop any you care about into SE_PINS to lock the S/E in. ──
    report = os.path.join(os.path.dirname(os.path.abspath(path)), "missing_se.txt")
    simkl_status = ("ON" if ENABLE_SIMKL else "OFF (no SIMKL_CLIENT_ID secret)")
    if ENABLE_SIMKL:
        simkl_status += f" — {_SIMKL_HITS} extra S/E this run"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        with open(report, "w", encoding="utf-8") as fh:
            # ── run status (visible here so you don't have to open the Actions log) ──
            fh.write("# ============================================================\n")
            fh.write(f"# RUN STATUS  ({now})\n")
            fh.write(f"#   SIMKL resolver   : {simkl_status}\n")
            fh.write(f"#   OMDb descriptions: {'ON' if ENABLE_OMDB else 'OFF (no OMDB_API_KEY secret)'}\n")
            fh.write(f"#   descriptions     : {stats['desc_episode']} episode-specific, "
                     f"{stats['desc_show_fixed']} fixed show-blurb, "
                     f"{stats['desc_generic']} generic, {stats['desc_none']} none "
                     f"(see generic_desc.txt)\n")
            fh.write(f"#   TAM index        : {'loaded' if tam_index else 'EMPTY (fetch failed?)'}\n")
            fh.write(f"#   S/E by name      : {stats['by_name']}  (manual pins: {stats['se_pinned']})\n")
            fh.write(f"#   S/E by absolute  : {stats['by_absolute']}\n")
            fh.write(f"#   S/E + desc / TAM : {stats['by_tam']}  (name+S/E+synopsis taken straight from TAM)\n")
            fh.write(f"#   master map       : {stats['by_master']}  (S/E from your hand-verified sheet)\n")
            fh.write(f"#   episode guide    : {stats['by_guide']}  (authoritative IMDb/TMDB/wiki guide, keyed by absolute #)\n")
            fh.write(f"#   abs-resolve Snick: {stats['by_abs_snick']}  (nameless slots auto-filled from episodeNumber -> see abs_snick.txt)\n")
            fh.write(f"#   repeat-fill      : {stats['by_repeat']}  (re-airings backfilled from a named sibling by episodeNumber)\n")
            fh.write(f"#   names from TAM   : {stats['from_tam']}\n")
            fh.write(f"#   S/E withheld     : {stats['se_dropped']}  (listed below)\n")
            fh.write("# ============================================================\n")
            fh.write("# NEEDS S/E: we have the correct episode NAME (from the feed or\n")
            fh.write("# TAM) + description, but couldn't confidently resolve a season/\n")
            fh.write("# episode number. Only the Sxx Eyy is missing. To lock one in,\n")
            fh.write("# fill the number and paste the line into SE_PINS:\n")
            fh.write('#   ("<show>", "<episode>"): (season, episode),\n')
            fh.write("# The trailing  epNum=  is the feed episodeNumber (blank = the API\n")
            fh.write("# gave none, so a guide can't key on it).\n")
            fh.write("# ============================================================\n")
            for show, epi, absN in sorted(dropped_se, key=lambda r: (r[0], r[1])):
                fh.write(f'    ("{show}", "{epi}"): (, ),   # epNum={"" if absN is None else absN}\n')
        print(f"  -> {report}: {len(dropped_se)} need S/E (names + descriptions are set)")
    except Exception as e:
        print(f"  warn: could not write {report}: {e}")

    # ── description report: every programme whose <desc> is NOT episode-specific
    #    (generic show/series blurb, house blurb, or nothing). Each line carries a
    #    reason. To hard-set one, paste into DESC_OVERRIDE in enrich_toonami.py:
    #      ("<show>", "<episode>"): "the real episode description",
    #    (S/E, sub-title and title are untouched — desc is its own field.) ──
    dreport = os.path.join(os.path.dirname(os.path.abspath(path)), "generic_desc.txt")
    try:
        with open(dreport, "w", encoding="utf-8") as fh:
            fh.write("# ============================================================\n")
            fh.write(f"# DESCRIPTION REPORT  ({now})\n")
            fh.write(f"#   episode-specific: {stats['desc_episode']}\n")
            fh.write(f"#   fixed show-blurb: {stats['desc_show_fixed']}  (hand-supplied, intentional)\n")
            fh.write(f"#   generic (blurb) : {stats['desc_generic']}\n")
            fh.write(f"#   none            : {stats['desc_none']}\n")
            fh.write("# These programmes do NOT have an episode-specific description.\n")
            fh.write("# Reason is given after each line, with the feed episodeNumber\n")
            fh.write("# (epNum=, blank = the API gave none). To set a real one, paste into\n")
            fh.write("# DESC_OVERRIDE (titles / sub-titles / S-E stay untouched):\n")
            fh.write('#   ("<show>", "<episode>"): "the real episode description",\n')
            fh.write("# ============================================================\n")
            for show, epi, reason, absN in sorted(generic_desc, key=lambda r: (r[0], r[1])):
                fh.write(f'    ("{show}", "{epi}"): "",   # epNum={"" if absN is None else absN}  {reason}\n')
        print(f"  -> {dreport}: {len(generic_desc)} without an episode description")
    except Exception as e:
        print(f"  warn: could not write {dreport}: {e}")

    # ── abs-resolve review worklist: every nameless Snick/live-action slot that
    #    Path 1 auto-filled from the absolute episodeNumber. These are UN-corroborated
    #    (there was no feed/TAM name to check against), so eyeball them. A good row ->
    #    promote it into snick_master_map.tsv / the master map (keyed show|number),
    #    which then wins here on every future run. A wrong one -> correct the S/E in
    #    the master map, or set ALLOW_ABS_SNICK=0 to turn the whole tier off. ──
    areport = os.path.join(os.path.dirname(os.path.abspath(path)), "abs_snick.txt")
    try:
        with open(areport, "w", encoding="utf-8") as fh:
            fh.write("# ============================================================\n")
            fh.write(f"# ABSOLUTE-RESOLVE REVIEW  ({now})\n")
            fh.write(f"#   auto-filled nameless slots: {len(abs_snick)}\n")
            fh.write("# The feed gave only an episodeNumber (no name) for these, so the\n")
            fh.write("# S/E, episode name and synopsis below were resolved straight from\n")
            fh.write("# that absolute index via the DBs -- NOT corroborated against a feed\n")
            fh.write("# name. Verify, then lock good ones into the master map (keyed\n")
            fh.write("# show|episodeNumber); the master map overrides this tier.\n")
            fh.write("# ============================================================\n")
            for (show, absN), (s, e, nm) in sorted(abs_snick.items(),
                                                   key=lambda kv: (kv[0][0], kv[0][1])):
                fh.write(f'    {show}  #{absN}  ->  S{s:02d}E{e:02d}  "{nm}"\n')
        print(f"  -> {areport}: {len(abs_snick)} nameless slots auto-filled (review & promote)")
    except Exception as e:
        print(f"  warn: could not write {areport}: {e}")

    print(f"enriched {stats['progs']} programmes on {len(TARGET_CHANNELS)} channels")
    print(f"  S/E by episode name    : {stats['by_name']}  (manual pins: {stats['se_pinned']})")
    print(f"  S/E by absolute number : {stats['by_absolute']}")
    print(f"  S/E + desc from TAM     : {stats['by_tam']}")
    print(f"  master map (your sheet) : {stats['by_master']}")
    print(f"  episode guide (authoritative) : {stats['by_guide']}")
    print(f"  abs-resolve Snick (new) : {stats['by_abs_snick']}")
    print(f"  repeat-fill (by number) : {stats['by_repeat']}")
    print(f"  names from TAM          : {stats['from_tam']}")
    print(f"  SIMKL resolver          : {'on' if ENABLE_SIMKL else 'off'}"
          f"{f', {_SIMKL_HITS} extra S/E' if ENABLE_SIMKL else ''}")
    print(f"  OMDb descriptions       : {'on' if ENABLE_OMDB else 'off'}")
    print(f"  descriptions           : {stats['desc_episode']} episode / "
          f"{stats['desc_show_fixed']} fixed-show / "
          f"{stats['desc_generic']} generic / {stats['desc_none']} none")
    print(f"  S/E withheld (logged)  : {stats['se_dropped']}")
    print(f"  show-level fallback    : {stats['show_level']}")
    print(f"  still no description   : {stats['no_desc']}")
    print(f"  posters -> icon        : {stats['icons']}")
    print(f"  gaps/overlaps closed   : {gaps_closed}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: enrich_toonami.py <TOONAMIAM.xml>"); sys.exit(2)
    if not TMDB_KEY and not TVDB_KEY:
        print("NOTE: no TMDB/TVDB keys set - nothing to enrich; leaving file as-is.")
        sys.exit(0)
    enrich(sys.argv[1])
