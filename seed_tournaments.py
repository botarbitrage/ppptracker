#!/usr/bin/env python3
"""
seed_tournaments.py — Seed Firestore with:
  /config/blind_structure_base   (levels 1-51, shared by all PPPoker MTTs)
  /config/payout_structure       (20 entrant-count brackets, pays up to 175th)
  /tournaments/deep_freeze
  /tournaments/crazy_2

Run once (or re-run to update):
  FIREBASE_SERVICE_ACCOUNT_JSON='...' python seed_tournaments.py

Uses the same env var as app.py. Safe to re-run — all writes use set(merge=False).
"""

import os, json, sys
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore

# ── DB init ───────────────────────────────────────────────────────────────────

def get_db():
    if firebase_admin._apps:
        return admin_firestore.client()
    sa_json = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON', '')
    if sa_json:
        cred = credentials.Certificate(json.loads(sa_json))
    else:
        cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)
    return admin_firestore.client()

# ── Blind structure base (levels 1-51, identical across all PPPoker tournaments) ─

BLIND_LEVELS_BASE = [
    {"level": 1,  "sb": 25,         "bb": 50,          "ante": 0},
    {"level": 2,  "sb": 50,         "bb": 100,         "ante": 0},
    {"level": 3,  "sb": 100,        "bb": 200,         "ante": 25},
    {"level": 4,  "sb": 150,        "bb": 300,         "ante": 50},
    {"level": 5,  "sb": 200,        "bb": 400,         "ante": 50},
    {"level": 6,  "sb": 250,        "bb": 500,         "ante": 75},
    {"level": 7,  "sb": 300,        "bb": 600,         "ante": 75},
    {"level": 8,  "sb": 400,        "bb": 800,         "ante": 100},
    {"level": 9,  "sb": 500,        "bb": 1000,        "ante": 150},
    {"level": 10, "sb": 600,        "bb": 1200,        "ante": 200},
    {"level": 11, "sb": 800,        "bb": 1600,        "ante": 200},
    {"level": 12, "sb": 1000,       "bb": 2000,        "ante": 300},
    {"level": 13, "sb": 1200,       "bb": 2400,        "ante": 300},
    {"level": 14, "sb": 1500,       "bb": 3000,        "ante": 400},
    {"level": 15, "sb": 2000,       "bb": 4000,        "ante": 500},
    {"level": 16, "sb": 2500,       "bb": 5000,        "ante": 800},
    {"level": 17, "sb": 3000,       "bb": 6000,        "ante": 1000},
    {"level": 18, "sb": 4000,       "bb": 8000,        "ante": 1000},
    {"level": 19, "sb": 5000,       "bb": 10000,       "ante": 1500},
    {"level": 20, "sb": 6000,       "bb": 12000,       "ante": 1500},
    {"level": 21, "sb": 8000,       "bb": 16000,       "ante": 2000},
    {"level": 22, "sb": 10000,      "bb": 20000,       "ante": 3000},
    {"level": 23, "sb": 12000,      "bb": 24000,       "ante": 3000},
    {"level": 24, "sb": 15000,      "bb": 30000,       "ante": 4000},
    {"level": 25, "sb": 20000,      "bb": 40000,       "ante": 5000},
    {"level": 26, "sb": 25000,      "bb": 50000,       "ante": 6000},
    {"level": 27, "sb": 30000,      "bb": 60000,       "ante": 8000},
    {"level": 28, "sb": 40000,      "bb": 80000,       "ante": 10000},
    {"level": 29, "sb": 50000,      "bb": 100000,      "ante": 15000},
    {"level": 30, "sb": 60000,      "bb": 120000,      "ante": 15000},
    {"level": 31, "sb": 80000,      "bb": 160000,      "ante": 20000},
    {"level": 32, "sb": 100000,     "bb": 200000,      "ante": 30000},
    {"level": 33, "sb": 120000,     "bb": 240000,      "ante": 30000},
    {"level": 34, "sb": 150000,     "bb": 300000,      "ante": 50000},
    {"level": 35, "sb": 200000,     "bb": 400000,      "ante": 50000},
    {"level": 36, "sb": 250000,     "bb": 500000,      "ante": 75000},
    {"level": 37, "sb": 300000,     "bb": 600000,      "ante": 100000},
    {"level": 38, "sb": 400000,     "bb": 800000,      "ante": 100000},
    {"level": 39, "sb": 500000,     "bb": 1000000,     "ante": 150000},
    {"level": 40, "sb": 600000,     "bb": 1200000,     "ante": 200000},
    {"level": 41, "sb": 800000,     "bb": 1600000,     "ante": 250000},
    {"level": 42, "sb": 1000000,    "bb": 2000000,     "ante": 300000},
    {"level": 43, "sb": 1500000,    "bb": 3000000,     "ante": 400000},
    {"level": 44, "sb": 2000000,    "bb": 4000000,     "ante": 500000},
    {"level": 45, "sb": 2500000,    "bb": 5000000,     "ante": 800000},
    {"level": 46, "sb": 3000000,    "bb": 6000000,     "ante": 1000000},
    {"level": 47, "sb": 4000000,    "bb": 8000000,     "ante": 1000000},
    {"level": 48, "sb": 5000000,    "bb": 10000000,    "ante": 1500000},
    {"level": 49, "sb": 6000000,    "bb": 12000000,    "ante": 1500000},
    {"level": 50, "sb": 8000000,    "bb": 16000000,    "ante": 2000000},
    {"level": 51, "sb": 10000000,   "bb": 20000000,    "ante": 3000000},
]

# ── LUCKY DAY — completely different 60-level structure (not based on BLIND_LEVELS_BASE) ─
LUCKY_DAY_BLIND_LEVELS = [
    {"level": 1,  "sb": 50,          "bb": 100,          "ante": 0},
    {"level": 2,  "sb": 100,         "bb": 200,          "ante": 0},
    {"level": 3,  "sb": 150,         "bb": 300,          "ante": 50},
    {"level": 4,  "sb": 200,         "bb": 400,          "ante": 50},
    {"level": 5,  "sb": 300,         "bb": 600,          "ante": 75},
    {"level": 6,  "sb": 400,         "bb": 800,          "ante": 100},
    {"level": 7,  "sb": 500,         "bb": 1000,         "ante": 150},
    {"level": 8,  "sb": 600,         "bb": 1200,         "ante": 200},
    {"level": 9,  "sb": 800,         "bb": 1600,         "ante": 200},
    {"level": 10, "sb": 1000,        "bb": 2000,         "ante": 300},
    {"level": 11, "sb": 1500,        "bb": 3000,         "ante": 400},
    {"level": 12, "sb": 2000,        "bb": 4000,         "ante": 500},
    {"level": 13, "sb": 3000,        "bb": 6000,         "ante": 800},
    {"level": 14, "sb": 4000,        "bb": 8000,         "ante": 1000},
    {"level": 15, "sb": 5000,        "bb": 10000,        "ante": 1500},
    {"level": 16, "sb": 6000,        "bb": 12000,        "ante": 1500},
    {"level": 17, "sb": 8000,        "bb": 16000,        "ante": 2000},
    {"level": 18, "sb": 10000,       "bb": 20000,        "ante": 3000},
    {"level": 19, "sb": 15000,       "bb": 30000,        "ante": 4000},
    {"level": 20, "sb": 20000,       "bb": 40000,        "ante": 5000},
    {"level": 21, "sb": 30000,       "bb": 60000,        "ante": 8000},
    {"level": 22, "sb": 40000,       "bb": 80000,        "ante": 10000},
    {"level": 23, "sb": 50000,       "bb": 100000,       "ante": 15000},
    {"level": 24, "sb": 60000,       "bb": 120000,       "ante": 15000},
    {"level": 25, "sb": 80000,       "bb": 160000,       "ante": 20000},
    {"level": 26, "sb": 100000,      "bb": 200000,       "ante": 30000},
    {"level": 27, "sb": 150000,      "bb": 300000,       "ante": 40000},
    {"level": 28, "sb": 200000,      "bb": 400000,       "ante": 50000},
    {"level": 29, "sb": 300000,      "bb": 600000,       "ante": 80000},
    {"level": 30, "sb": 400000,      "bb": 800000,       "ante": 100000},
    {"level": 31, "sb": 500000,      "bb": 1000000,      "ante": 150000},
    {"level": 32, "sb": 600000,      "bb": 1200000,      "ante": 175000},
    {"level": 33, "sb": 800000,      "bb": 1600000,      "ante": 200000},
    {"level": 34, "sb": 1000000,     "bb": 2000000,      "ante": 300000},
    {"level": 35, "sb": 1500000,     "bb": 3000000,      "ante": 400000},
    {"level": 36, "sb": 2000000,     "bb": 4000000,      "ante": 500000},
    {"level": 37, "sb": 3000000,     "bb": 6000000,      "ante": 700000},
    {"level": 38, "sb": 4000000,     "bb": 8000000,      "ante": 1000000},
    {"level": 39, "sb": 5000000,     "bb": 10000000,     "ante": 1500000},
    {"level": 40, "sb": 6000000,     "bb": 12000000,     "ante": 1750000},
    {"level": 41, "sb": 8000000,     "bb": 16000000,     "ante": 2000000},
    {"level": 42, "sb": 10000000,    "bb": 20000000,     "ante": 3000000},
    {"level": 43, "sb": 15000000,    "bb": 30000000,     "ante": 4000000},
    {"level": 44, "sb": 20000000,    "bb": 40000000,     "ante": 5000000},
    {"level": 45, "sb": 30000000,    "bb": 60000000,     "ante": 7000000},
    {"level": 46, "sb": 40000000,    "bb": 80000000,     "ante": 10000000},
    {"level": 47, "sb": 50000000,    "bb": 100000000,    "ante": 15000000},
    {"level": 48, "sb": 60000000,    "bb": 120000000,    "ante": 17500000},
    {"level": 49, "sb": 80000000,    "bb": 160000000,    "ante": 20000000},
    {"level": 50, "sb": 100000000,   "bb": 200000000,    "ante": 30000000},
    {"level": 51, "sb": 150000000,   "bb": 300000000,    "ante": 40000000},
    {"level": 52, "sb": 200000000,   "bb": 400000000,    "ante": 50000000},
    {"level": 53, "sb": 300000000,   "bb": 600000000,    "ante": 70000000},
    {"level": 54, "sb": 400000000,   "bb": 800000000,    "ante": 100000000},
    {"level": 55, "sb": 500000000,   "bb": 1000000000,   "ante": 150000000},
    {"level": 56, "sb": 600000000,   "bb": 1200000000,   "ante": 175000000},
    {"level": 57, "sb": 800000000,   "bb": 1600000000,   "ante": 200000000},
    {"level": 58, "sb": 1000000000,  "bb": 2000000000,   "ante": 300000000},
    {"level": 59, "sb": 1500000000,  "bb": 3000000000,   "ante": 400000000},
    {"level": 60, "sb": 2000000000,  "bb": 4000000000,   "ante": 500000000},
]

# CRAZY 2 / East PKO SAT share levels 52-68
CRAZY2_EXTRA_LEVELS = [
    {"level": 52, "sb": 15000000,   "bb": 30000000,    "ante": 3000000},
    {"level": 53, "sb": 20000000,   "bb": 40000000,    "ante": 5000000},
    {"level": 54, "sb": 30000000,   "bb": 60000000,    "ante": 10000000},
    {"level": 55, "sb": 40000000,   "bb": 80000000,    "ante": 10000000},
    {"level": 56, "sb": 50000000,   "bb": 100000000,   "ante": 15000000},
    {"level": 57, "sb": 60000000,   "bb": 120000000,   "ante": 15000000},
    {"level": 58, "sb": 80000000,   "bb": 160000000,   "ante": 20000000},
    {"level": 59, "sb": 100000000,  "bb": 200000000,   "ante": 30000000},
    {"level": 60, "sb": 150000000,  "bb": 300000000,   "ante": 30000000},
    {"level": 61, "sb": 200000000,  "bb": 400000000,   "ante": 50000000},
    {"level": 62, "sb": 300000000,  "bb": 600000000,   "ante": 100000000},
    {"level": 63, "sb": 400000000,  "bb": 800000000,   "ante": 100000000},
    {"level": 64, "sb": 500000000,  "bb": 1000000000,  "ante": 150000000},
    {"level": 65, "sb": 600000000,  "bb": 1200000000,  "ante": 150000000},
    {"level": 66, "sb": 800000000,  "bb": 1600000000,  "ante": 200000000},
    {"level": 67, "sb": 1000000000, "bb": 2000000000,  "ante": 300000000},
    {"level": 68, "sb": 1500000000, "bb": 3000000000,  "ante": 300000000},
]

# East PKO SAT uses BLIND_LEVELS_BASE + CRAZY2_EXTRA_LEVELS + 2 more
EAST_PKO_SAT_EXTRA_LEVELS = CRAZY2_EXTRA_LEVELS + [
    {"level": 69, "sb": 2000000000, "bb": 4000000000,  "ante": 500000000},
    {"level": 70, "sb": 3000000000, "bb": 6000000000,  "ante": 1000000000},
]

# ── Payout structure ──────────────────────────────────────────────────────────
# Source: PPPoker Payout Structure screenshots (all tournaments share this table).
# Percentages represent share of the prize pool paid to each finishing position.
# Brackets are entrant-count ranges; payouts stop where a cell was empty in the table.
# NOTE: 1st-place values for 151-200 and 201-250 were cut off in source screenshots —
#       marked with a comment and estimated from the surrounding trend.
#
# Each bracket entry:
#   min_players, max_players: entrant count range (inclusive)
#   pays_to: highest rank that receives a payout
#   payouts: [{rank_min, rank_max, pct}] — pct is % of prize pool
#            rank groups (e.g. 11-15) all receive the same pct each

PAYOUT_BRACKETS = [
    {
        "min_players": 3, "max_players": 10, "pays_to": 2,
        "payouts": [
            {"rank_min": 1, "rank_max": 1, "pct": 70.0},
            {"rank_min": 2, "rank_max": 2, "pct": 30.0},
        ]
    },
    {
        "min_players": 11, "max_players": 20, "pays_to": 3,
        "payouts": [
            {"rank_min": 1, "rank_max": 1, "pct": 50.0},
            {"rank_min": 2, "rank_max": 2, "pct": 30.0},
            {"rank_min": 3, "rank_max": 3, "pct": 20.0},
        ]
    },
    {
        "min_players": 21, "max_players": 30, "pays_to": 5,
        "payouts": [
            {"rank_min": 1, "rank_max": 1, "pct": 37.0},
            {"rank_min": 2, "rank_max": 2, "pct": 25.0},
            {"rank_min": 3, "rank_max": 3, "pct": 15.0},
            {"rank_min": 4, "rank_max": 4, "pct": 12.0},
            {"rank_min": 5, "rank_max": 5, "pct": 11.0},
        ]
    },
    {
        "min_players": 31, "max_players": 40, "pays_to": 6,
        "payouts": [
            {"rank_min": 1, "rank_max": 1, "pct": 35.0},
            {"rank_min": 2, "rank_max": 2, "pct": 22.0},
            {"rank_min": 3, "rank_max": 3, "pct": 15.0},
            {"rank_min": 4, "rank_max": 4, "pct": 11.0},
            {"rank_min": 5, "rank_max": 5, "pct": 9.0},
            {"rank_min": 6, "rank_max": 6, "pct": 8.0},
        ]
    },
    {
        "min_players": 41, "max_players": 50, "pays_to": 8,
        "payouts": [
            {"rank_min": 1, "rank_max": 1, "pct": 32.0},
            {"rank_min": 2, "rank_max": 2, "pct": 18.0},
            {"rank_min": 3, "rank_max": 3, "pct": 12.5},
            {"rank_min": 4, "rank_max": 4, "pct": 10.5},
            {"rank_min": 5, "rank_max": 5, "pct": 8.3},
            {"rank_min": 6, "rank_max": 6, "pct": 7.3},
            {"rank_min": 7, "rank_max": 7, "pct": 6.2},
            {"rank_min": 8, "rank_max": 8, "pct": 5.2},
        ]
    },
    {
        "min_players": 51, "max_players": 60, "pays_to": 9,
        "payouts": [
            {"rank_min": 1, "rank_max": 1, "pct": 30.0},
            {"rank_min": 2, "rank_max": 2, "pct": 17.5},
            {"rank_min": 3, "rank_max": 3, "pct": 12.2},
            {"rank_min": 4, "rank_max": 4, "pct": 10.2},
            {"rank_min": 5, "rank_max": 5, "pct": 8.1},
            {"rank_min": 6, "rank_max": 6, "pct": 7.1},
            {"rank_min": 7, "rank_max": 7, "pct": 6.1},
            {"rank_min": 8, "rank_max": 8, "pct": 5.1},
            {"rank_min": 9, "rank_max": 9, "pct": 3.7},
        ]
    },
    {
        "min_players": 61, "max_players": 75, "pays_to": 10,
        "payouts": [
            {"rank_min": 1, "rank_max": 1, "pct": 29.0},
            {"rank_min": 2, "rank_max": 2, "pct": 17.0},
            {"rank_min": 3, "rank_max": 3, "pct": 12.0},
            {"rank_min": 4, "rank_max": 4, "pct": 10.0},
            {"rank_min": 5, "rank_max": 5, "pct": 8.0},
            {"rank_min": 6, "rank_max": 6, "pct": 6.9},
            {"rank_min": 7, "rank_max": 7, "pct": 5.9},
            {"rank_min": 8, "rank_max": 8, "pct": 4.9},
            {"rank_min": 9, "rank_max": 9, "pct": 3.5},
            {"rank_min": 10, "rank_max": 10, "pct": 2.8},
        ]
    },
    {
        "min_players": 75, "max_players": 100, "pays_to": 15,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 28.5},
            {"rank_min": 2,  "rank_max": 2,  "pct": 16.5},
            {"rank_min": 3,  "rank_max": 3,  "pct": 10.0},
            {"rank_min": 4,  "rank_max": 4,  "pct": 8.0},
            {"rank_min": 5,  "rank_max": 5,  "pct": 6.9},
            {"rank_min": 6,  "rank_max": 6,  "pct": 5.9},
            {"rank_min": 7,  "rank_max": 7,  "pct": 4.9},
            {"rank_min": 8,  "rank_max": 8,  "pct": 3.8},
            {"rank_min": 9,  "rank_max": 9,  "pct": 2.7},
            {"rank_min": 10, "rank_max": 10, "pct": 2.3},
            {"rank_min": 11, "rank_max": 15, "pct": 2.1},
        ]
    },
    {
        "min_players": 101, "max_players": 125, "pays_to": 20,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 28.0},
            {"rank_min": 2,  "rank_max": 2,  "pct": 16.2},
            {"rank_min": 3,  "rank_max": 3,  "pct": 9.3},
            {"rank_min": 4,  "rank_max": 4,  "pct": 7.3},
            {"rank_min": 5,  "rank_max": 5,  "pct": 5.3},
            {"rank_min": 6,  "rank_max": 6,  "pct": 5.3},
            {"rank_min": 7,  "rank_max": 7,  "pct": 4.2},
            {"rank_min": 8,  "rank_max": 8,  "pct": 3.0},
            {"rank_min": 9,  "rank_max": 9,  "pct": 2.1},
            {"rank_min": 10, "rank_max": 10, "pct": 1.8},
            {"rank_min": 11, "rank_max": 15, "pct": 1.7},
            {"rank_min": 16, "rank_max": 20, "pct": 1.6},
        ]
    },
    {
        "min_players": 126, "max_players": 150, "pays_to": 25,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 27.5},
            {"rank_min": 2,  "rank_max": 2,  "pct": 16.0},
            {"rank_min": 3,  "rank_max": 3,  "pct": 9.1},
            {"rank_min": 4,  "rank_max": 4,  "pct": 7.1},
            {"rank_min": 5,  "rank_max": 5,  "pct": 5.1},
            {"rank_min": 6,  "rank_max": 6,  "pct": 5.1},
            {"rank_min": 7,  "rank_max": 7,  "pct": 3.9},
            {"rank_min": 8,  "rank_max": 8,  "pct": 2.7},
            {"rank_min": 9,  "rank_max": 9,  "pct": 1.8},
            {"rank_min": 10, "rank_max": 10, "pct": 1.45},
            {"rank_min": 11, "rank_max": 15, "pct": 1.35},
            {"rank_min": 16, "rank_max": 20, "pct": 1.28},
            {"rank_min": 21, "rank_max": 25, "pct": 1.22},
        ]
    },
    {
        # NOTE: 1st place % estimated (~27.5) — cut off in source screenshot
        "min_players": 151, "max_players": 200, "pays_to": 30,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 27.5},  # estimated
            {"rank_min": 2,  "rank_max": 2,  "pct": 15.75},
            {"rank_min": 3,  "rank_max": 3,  "pct": 9.0},
            {"rank_min": 4,  "rank_max": 4,  "pct": 7.0},
            {"rank_min": 5,  "rank_max": 5,  "pct": 6.0},
            {"rank_min": 6,  "rank_max": 6,  "pct": 5.0},
            {"rank_min": 7,  "rank_max": 7,  "pct": 3.8},
            {"rank_min": 8,  "rank_max": 8,  "pct": 2.4},
            {"rank_min": 9,  "rank_max": 9,  "pct": 1.7},
            {"rank_min": 10, "rank_max": 10, "pct": 1.3},
            {"rank_min": 11, "rank_max": 15, "pct": 1.2},
            {"rank_min": 16, "rank_max": 20, "pct": 1.05},
            {"rank_min": 21, "rank_max": 25, "pct": 1.0},
            {"rank_min": 26, "rank_max": 30, "pct": 0.96},
        ]
    },
    {
        # NOTE: 1st place % estimated (~27.2) — cut off in source screenshot
        "min_players": 201, "max_players": 250, "pays_to": 35,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 27.2},  # estimated
            {"rank_min": 2,  "rank_max": 2,  "pct": 15.5},
            {"rank_min": 3,  "rank_max": 3,  "pct": 8.85},
            {"rank_min": 4,  "rank_max": 4,  "pct": 6.8},
            {"rank_min": 5,  "rank_max": 5,  "pct": 5.8},
            {"rank_min": 6,  "rank_max": 6,  "pct": 4.8},
            {"rank_min": 7,  "rank_max": 7,  "pct": 3.6},
            {"rank_min": 8,  "rank_max": 8,  "pct": 2.4},
            {"rank_min": 9,  "rank_max": 9,  "pct": 1.7},
            {"rank_min": 10, "rank_max": 10, "pct": 1.3},
            {"rank_min": 11, "rank_max": 15, "pct": 1.15},
            {"rank_min": 16, "rank_max": 20, "pct": 1.0},
            {"rank_min": 21, "rank_max": 25, "pct": 0.9},
            {"rank_min": 26, "rank_max": 30, "pct": 0.8},
            {"rank_min": 31, "rank_max": 35, "pct": 0.7},
        ]
    },
    {
        "min_players": 251, "max_players": 300, "pays_to": 40,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 27.0},
            {"rank_min": 2,  "rank_max": 2,  "pct": 15.0},
            {"rank_min": 3,  "rank_max": 3,  "pct": 8.8},
            {"rank_min": 4,  "rank_max": 4,  "pct": 6.8},
            {"rank_min": 5,  "rank_max": 5,  "pct": 5.7},
            {"rank_min": 6,  "rank_max": 6,  "pct": 4.7},
            {"rank_min": 7,  "rank_max": 7,  "pct": 3.6},
            {"rank_min": 8,  "rank_max": 8,  "pct": 2.4},
            {"rank_min": 9,  "rank_max": 9,  "pct": 1.7},
            {"rank_min": 10, "rank_max": 10, "pct": 1.3},
            {"rank_min": 11, "rank_max": 15, "pct": 1.15},
            {"rank_min": 16, "rank_max": 20, "pct": 1.0},
            {"rank_min": 21, "rank_max": 25, "pct": 0.8},
            {"rank_min": 26, "rank_max": 30, "pct": 0.7},
            {"rank_min": 31, "rank_max": 35, "pct": 0.6},
            {"rank_min": 36, "rank_max": 40, "pct": 0.55},
        ]
    },
    {
        "min_players": 301, "max_players": 350, "pays_to": 50,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 26.0},
            {"rank_min": 2,  "rank_max": 2,  "pct": 14.5},
            {"rank_min": 3,  "rank_max": 3,  "pct": 8.6},
            {"rank_min": 4,  "rank_max": 4,  "pct": 6.6},
            {"rank_min": 5,  "rank_max": 5,  "pct": 5.6},
            {"rank_min": 6,  "rank_max": 6,  "pct": 4.6},
            {"rank_min": 7,  "rank_max": 7,  "pct": 3.5},
            {"rank_min": 8,  "rank_max": 8,  "pct": 2.4},
            {"rank_min": 9,  "rank_max": 9,  "pct": 1.6},
            {"rank_min": 10, "rank_max": 10, "pct": 1.2},
            {"rank_min": 11, "rank_max": 15, "pct": 1.05},
            {"rank_min": 16, "rank_max": 20, "pct": 0.9},
            {"rank_min": 21, "rank_max": 25, "pct": 0.7},
            {"rank_min": 26, "rank_max": 30, "pct": 0.6},
            {"rank_min": 31, "rank_max": 35, "pct": 0.53},
            {"rank_min": 36, "rank_max": 40, "pct": 0.5},
            {"rank_min": 41, "rank_max": 50, "pct": 0.46},
        ]
    },
    {
        "min_players": 351, "max_players": 400, "pays_to": 60,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 25.0},
            {"rank_min": 2,  "rank_max": 2,  "pct": 14.0},
            {"rank_min": 3,  "rank_max": 3,  "pct": 8.5},
            {"rank_min": 4,  "rank_max": 4,  "pct": 6.5},
            {"rank_min": 5,  "rank_max": 5,  "pct": 5.5},
            {"rank_min": 6,  "rank_max": 6,  "pct": 4.5},
            {"rank_min": 7,  "rank_max": 7,  "pct": 3.4},
            {"rank_min": 8,  "rank_max": 8,  "pct": 2.3},
            {"rank_min": 9,  "rank_max": 9,  "pct": 1.5},
            {"rank_min": 10, "rank_max": 10, "pct": 1.2},
            {"rank_min": 11, "rank_max": 15, "pct": 1.0},
            {"rank_min": 16, "rank_max": 20, "pct": 0.8},
            {"rank_min": 21, "rank_max": 25, "pct": 0.64},
            {"rank_min": 26, "rank_max": 30, "pct": 0.54},
            {"rank_min": 31, "rank_max": 35, "pct": 0.45},
            {"rank_min": 36, "rank_max": 40, "pct": 0.43},
            {"rank_min": 41, "rank_max": 50, "pct": 0.42},
            {"rank_min": 51, "rank_max": 60, "pct": 0.41},
        ]
    },
    {
        "min_players": 401, "max_players": 500, "pays_to": 75,
        "payouts": [
            {"rank_min": 1,  "rank_max": 1,  "pct": 24.5},
            {"rank_min": 2,  "rank_max": 2,  "pct": 13.75},
            {"rank_min": 3,  "rank_max": 3,  "pct": 8.4},
            {"rank_min": 4,  "rank_max": 4,  "pct": 6.4},
            {"rank_min": 5,  "rank_max": 5,  "pct": 5.4},
            {"rank_min": 6,  "rank_max": 6,  "pct": 4.4},
            {"rank_min": 7,  "rank_max": 7,  "pct": 3.3},
            {"rank_min": 8,  "rank_max": 8,  "pct": 2.3},
            {"rank_min": 9,  "rank_max": 9,  "pct": 1.45},
            {"rank_min": 10, "rank_max": 10, "pct": 1.2},
            {"rank_min": 11, "rank_max": 15, "pct": 0.95},
            {"rank_min": 16, "rank_max": 20, "pct": 0.75},
            {"rank_min": 21, "rank_max": 25, "pct": 0.58},
            {"rank_min": 26, "rank_max": 30, "pct": 0.48},
            {"rank_min": 31, "rank_max": 35, "pct": 0.4},
            {"rank_min": 36, "rank_max": 40, "pct": 0.36},
            {"rank_min": 41, "rank_max": 50, "pct": 0.34},
            {"rank_min": 51, "rank_max": 60, "pct": 0.33},
            {"rank_min": 61, "rank_max": 75, "pct": 0.32},
        ]
    },
    {
        "min_players": 501, "max_players": 600, "pays_to": 100,
        "payouts": [
            {"rank_min": 1,   "rank_max": 1,   "pct": 24.0},
            {"rank_min": 2,   "rank_max": 2,   "pct": 12.7},
            {"rank_min": 3,   "rank_max": 3,   "pct": 8.3},
            {"rank_min": 4,   "rank_max": 4,   "pct": 6.3},
            {"rank_min": 5,   "rank_max": 5,   "pct": 5.3},
            {"rank_min": 6,   "rank_max": 6,   "pct": 4.3},
            {"rank_min": 7,   "rank_max": 7,   "pct": 3.2},
            {"rank_min": 8,   "rank_max": 8,   "pct": 2.2},
            {"rank_min": 9,   "rank_max": 9,   "pct": 1.4},
            {"rank_min": 10,  "rank_max": 10,  "pct": 1.1},
            {"rank_min": 11,  "rank_max": 15,  "pct": 0.95},
            {"rank_min": 16,  "rank_max": 20,  "pct": 0.75},
            {"rank_min": 21,  "rank_max": 25,  "pct": 0.56},
            {"rank_min": 26,  "rank_max": 30,  "pct": 0.45},
            {"rank_min": 31,  "rank_max": 35,  "pct": 0.38},
            {"rank_min": 36,  "rank_max": 40,  "pct": 0.34},
            {"rank_min": 41,  "rank_max": 50,  "pct": 0.29},
            {"rank_min": 51,  "rank_max": 60,  "pct": 0.27},
            {"rank_min": 61,  "rank_max": 75,  "pct": 0.22},
            {"rank_min": 76,  "rank_max": 100, "pct": 0.21},
        ]
    },
    {
        "min_players": 601, "max_players": 700, "pays_to": 125,
        "payouts": [
            {"rank_min": 1,   "rank_max": 1,   "pct": 23.5},
            {"rank_min": 2,   "rank_max": 2,   "pct": 12.5},
            {"rank_min": 3,   "rank_max": 3,   "pct": 8.2},
            {"rank_min": 4,   "rank_max": 4,   "pct": 6.2},
            {"rank_min": 5,   "rank_max": 5,   "pct": 5.2},
            {"rank_min": 6,   "rank_max": 6,   "pct": 4.2},
            {"rank_min": 7,   "rank_max": 7,   "pct": 3.1},
            {"rank_min": 8,   "rank_max": 8,   "pct": 2.2},
            {"rank_min": 9,   "rank_max": 9,   "pct": 1.4},
            {"rank_min": 10,  "rank_max": 10,  "pct": 1.1},
            {"rank_min": 11,  "rank_max": 15,  "pct": 0.9},
            {"rank_min": 16,  "rank_max": 20,  "pct": 0.7},
            {"rank_min": 21,  "rank_max": 25,  "pct": 0.51},
            {"rank_min": 26,  "rank_max": 30,  "pct": 0.41},
            {"rank_min": 31,  "rank_max": 35,  "pct": 0.33},
            {"rank_min": 36,  "rank_max": 40,  "pct": 0.3},
            {"rank_min": 41,  "rank_max": 50,  "pct": 0.25},
            {"rank_min": 51,  "rank_max": 60,  "pct": 0.23},
            {"rank_min": 61,  "rank_max": 75,  "pct": 0.22},
            {"rank_min": 76,  "rank_max": 100, "pct": 0.18},
            {"rank_min": 101, "rank_max": 125, "pct": 0.17},
        ]
    },
    {
        "min_players": 701, "max_players": 800, "pays_to": 150,
        "payouts": [
            {"rank_min": 1,   "rank_max": 1,   "pct": 23.0},
            {"rank_min": 2,   "rank_max": 2,   "pct": 12.15},
            {"rank_min": 3,   "rank_max": 3,   "pct": 8.0},
            {"rank_min": 4,   "rank_max": 4,   "pct": 6.0},
            {"rank_min": 5,   "rank_max": 5,   "pct": 5.0},
            {"rank_min": 6,   "rank_max": 6,   "pct": 4.0},
            {"rank_min": 7,   "rank_max": 7,   "pct": 2.9},
            {"rank_min": 8,   "rank_max": 8,   "pct": 2.1},
            {"rank_min": 9,   "rank_max": 9,   "pct": 1.3},
            {"rank_min": 10,  "rank_max": 10,  "pct": 1.0},
            {"rank_min": 11,  "rank_max": 15,  "pct": 0.85},
            {"rank_min": 16,  "rank_max": 20,  "pct": 0.65},
            {"rank_min": 21,  "rank_max": 25,  "pct": 0.45},
            {"rank_min": 26,  "rank_max": 30,  "pct": 0.35},
            {"rank_min": 31,  "rank_max": 35,  "pct": 0.3},
            {"rank_min": 36,  "rank_max": 40,  "pct": 0.24},
            {"rank_min": 41,  "rank_max": 50,  "pct": 0.23},
            {"rank_min": 51,  "rank_max": 60,  "pct": 0.22},
            {"rank_min": 61,  "rank_max": 75,  "pct": 0.21},
            {"rank_min": 76,  "rank_max": 100, "pct": 0.19},
            {"rank_min": 101, "rank_max": 125, "pct": 0.17},
            {"rank_min": 126, "rank_max": 150, "pct": 0.16},
        ]
    },
    {
        "min_players": 801, "max_players": 900, "pays_to": 175,
        "payouts": [
            {"rank_min": 1,   "rank_max": 1,   "pct": 22.5},
            {"rank_min": 2,   "rank_max": 2,   "pct": 11.7},
            {"rank_min": 3,   "rank_max": 3,   "pct": 7.9},
            {"rank_min": 4,   "rank_max": 4,   "pct": 5.9},
            {"rank_min": 5,   "rank_max": 5,   "pct": 4.9},
            {"rank_min": 6,   "rank_max": 6,   "pct": 3.9},
            {"rank_min": 7,   "rank_max": 7,   "pct": 2.9},
            {"rank_min": 8,   "rank_max": 8,   "pct": 2.1},
            {"rank_min": 9,   "rank_max": 9,   "pct": 1.3},
            {"rank_min": 10,  "rank_max": 10,  "pct": 1.0},
            {"rank_min": 11,  "rank_max": 15,  "pct": 0.8},
            {"rank_min": 16,  "rank_max": 20,  "pct": 0.6},
            {"rank_min": 21,  "rank_max": 25,  "pct": 0.45},
            {"rank_min": 26,  "rank_max": 30,  "pct": 0.34},
            {"rank_min": 31,  "rank_max": 35,  "pct": 0.29},
            {"rank_min": 36,  "rank_max": 40,  "pct": 0.22},
            {"rank_min": 41,  "rank_max": 50,  "pct": 0.21},
            {"rank_min": 51,  "rank_max": 60,  "pct": 0.2},
            {"rank_min": 61,  "rank_max": 75,  "pct": 0.19},
            {"rank_min": 76,  "rank_max": 100, "pct": 0.18},
            {"rank_min": 101, "rank_max": 125, "pct": 0.16},
            {"rank_min": 126, "rank_max": 150, "pct": 0.15},
            {"rank_min": 151, "rank_max": 175, "pct": 0.14},
        ]
    },
]

# ── Tournament records ────────────────────────────────────────────────────────

TOURNAMENTS = [
    {
        "id": "deep_freeze",
        "data": {
            "name": "DEEP FREEZE",
            "type": "MTT",
            "is_pko": False,
            "currency": "AUD",
            "buy_in_total": 30.40,
            "buy_in_prize": 27.36,
            "buy_in_rake": 3.04,
            "starting_chips": 100000,
            "starting_time": "6:30 PM",            # ADL time
            "level_duration_min": 12,           # After LR
            "level_duration_rebuy_min": None,    # No rebuy period
            "level_duration_ft_min": 10,         # Final Table
            "late_reg_level": 14,
            "rebuy": False,
            "rebuy_type": "none",
            "rebuy_max_per_session": None,
            "rebuy_cost_multiplier": None,
            "rebuy_period_end_level": None,
            "rebuy_bulk_options": [],
            "addon": False,
            "addon_cost_multiplier": None,
            "addon_max_units": None,
            "addon_bulk_options": [],
            "player_min": 2,
            "player_max": 7000,
            "break_every_min": 55,
            "break_duration_min": 5,
            "blind_structure_extra": CRAZY2_EXTRA_LEVELS,  # Same 68 levels as CRAZY 2
            "itm_h": 4.0,
            "end_h": 5.5,
            "payout_ref": "standard",
            "active": True,
            "created_at": datetime.now(timezone.utc),
        }
    },
    {
        "id": "crazy_2",
        "data": {
            "name": "CRAZY 2",
            "type": "MTT",
            "is_pko": False,
            "currency": "AUD",
            "buy_in_total": 7.60,
            "buy_in_prize": 6.84,
            "buy_in_rake": 0.76,
            "starting_chips": 20000,
            "starting_time": "7:30 PM",            # ADL time
            "level_duration_min": 10,            # After LR
            "level_duration_rebuy_min": 12,      # Before LR (rebuy period)
            "level_duration_ft_min": 10,         # Final Table
            "late_reg_level": 12,
            "rebuy": True,
            "rebuy_type": "unlimited",
            "rebuy_max_per_session": None,
            "rebuy_cost_multiplier": 1,
            "rebuy_period_end_level": 12,
            "rebuy_bulk_options": [1, 2, 3],
            "addon": True,
            "addon_cost_multiplier": 3,
            "addon_max_units": 1,
            "addon_bulk_options": [1, 2, 3],
            "player_min": 2,
            "player_max": 7000,
            "break_every_min": 55,
            "break_duration_min": 5,
            "blind_structure_extra": CRAZY2_EXTRA_LEVELS,  # Same 68 levels as DEEP FREEZE
            "itm_h": 3.0,
            "end_h": 4.0,
            "payout_ref": "standard",
            "active": True,
            "created_at": datetime.now(timezone.utc),
        }
    },
    {
        "id": "lucky_day",
        "data": {
            "name": "LUCKY DAY",
            "type": "MTT",
            "is_mtt": True,
            "is_pko": False,
            "currency": "AUD",
            "buy_in_total": 3.04,
            "buy_in_prize": 2.74,
            "buy_in_rake": 0.30,
            "starting_chips": 20000,
            "starting_time": "6:30 PM",            # ADL time
            "level_duration_min": 8,               # After LR
            "level_duration_rebuy_min": 10,        # Before LR (rebuy period)
            "level_duration_ft_min": 8,            # Final Table
            "late_reg_level": 11,
            "rebuy": True,
            "rebuy_type": "unlimited",
            "rebuy_max_per_session": None,
            "rebuy_cost_multiplier": 1,
            "rebuy_period_end_level": 11,
            "rebuy_bulk_options": [1],             # No x2/x3 bundle
            "addon": True,
            "addon_cost_multiplier": 3,
            "addon_max_units": 1,
            "addon_bulk_options": [1],
            "player_min": 2,
            "player_max": 7000,
            "break_every_min": 55,
            "break_duration_min": 5,
            # Full 60-level structure — does NOT use BLIND_LEVELS_BASE
            "blind_structure_override": True,
            "blind_structure_extra": LUCKY_DAY_BLIND_LEVELS,
            "itm_h": 3.0,
            "end_h": 4.0,
            "payout_ref": "standard",
            "active": True,
            "created_at": datetime.now(timezone.utc),
        }
    },
    {
        "id": "east_pko_sat",
        "data": {
            "name": "East PKO SAT",
            "type": "SAT",
            "is_mtt": True,
            "is_pko": False,
            "currency": "AUD",
            "buy_in_total": 1.52,
            "buy_in_prize": 1.37,
            "buy_in_rake": 0.15,
            "starting_chips": 10000,
            "starting_time": "7:30 PM",            # ADL time
            "level_duration_min": 5,               # After LR
            "level_duration_rebuy_min": 6,         # Before LR (rebuy period)
            "level_duration_ft_min": 5,            # Final Table
            "late_reg_level": 10,
            "rebuy": True,
            "rebuy_type": "unlimited",
            "rebuy_max_per_session": None,
            "rebuy_cost_multiplier": 1,
            "rebuy_period_end_level": 10,
            "rebuy_bulk_options": [1, 2, 3],
            "addon": True,
            "addon_cost_multiplier": 3,
            "addon_max_units": 1,
            "addon_bulk_options": [1],
            "player_min": 2,
            "player_max": 7000,
            "break_every_min": 55,
            "break_duration_min": 5,
            # BLIND_LEVELS_BASE (51) + CRAZY2_EXTRA (52-68) + 2 more (69-70) = 70 levels
            "blind_structure_extra": EAST_PKO_SAT_EXTRA_LEVELS,
            "itm_h": 3.0,
            "end_h": 4.0,
            "payout_ref": "standard",
            "active": True,
            "created_at": datetime.now(timezone.utc),
        }
    },
    {
        "id": "texas",
        "data": {
            "name": "TEXAS",
            "type": "MTT",
            "is_mtt": True,
            "is_pko": False,
            "currency": "AUD",
            "buy_in_total": 6.08,
            "buy_in_prize": 5.48,
            "buy_in_rake": 0.60,
            "starting_chips": 20000,
            "starting_time": "8:30 PM",
            "level_duration_min": 10,              # All phases same
            "level_duration_rebuy_min": None,
            "level_duration_ft_min": None,
            "late_reg_level": 11,
            "rebuy": True,
            "rebuy_type": "unlimited",
            "rebuy_max_per_session": None,
            "rebuy_cost_multiplier": 1,
            "rebuy_period_end_level": 11,
            "rebuy_bulk_options": [1, 2],
            "addon": True,
            "addon_cost_multiplier": 3,
            "addon_max_units": 1,
            "addon_bulk_options": [1, 3],
            "player_min": 2,
            "player_max": 7000,
            "break_every_min": 55,
            "break_duration_min": 5,
            # Same 60-level structure as LUCKY DAY
            "blind_structure_override": True,
            "blind_structure_extra": LUCKY_DAY_BLIND_LEVELS,
            "itm_h": 3.0,
            "end_h": 4.0,
            "payout_ref": "standard",
            "active": True,
            "created_at": datetime.now(timezone.utc),
        }
    },
    {
        "id": "mini",
        "data": {
            "name": "MINI",
            "type": "MTT",
            "is_mtt": True,
            "is_pko": False,
            "currency": "AUD",
            "buy_in_total": 7.60,
            "buy_in_prize": 6.84,
            "buy_in_rake": 0.76,
            "starting_chips": 20000,
            "starting_time": "11:30 PM",
            "level_duration_min": 6,               # All phases same
            "level_duration_rebuy_min": None,
            "level_duration_ft_min": None,
            "late_reg_level": 12,
            "rebuy": True,
            "rebuy_type": "unlimited",
            "rebuy_max_per_session": None,
            "rebuy_cost_multiplier": 1,
            "rebuy_period_end_level": 12,
            "rebuy_bulk_options": [1],             # No x2/x3 bundle
            "addon": True,
            "addon_cost_multiplier": 3,
            "addon_max_units": 1,
            "addon_bulk_options": [1],
            "player_min": 2,
            "player_max": 7000,
            "break_every_min": 55,
            "break_duration_min": 5,
            # Same 70-level structure as East PKO SAT
            "blind_structure_extra": EAST_PKO_SAT_EXTRA_LEVELS,
            "itm_h": 4.0,
            "end_h": 5.5,
            "payout_ref": "standard",
            "active": True,
            "created_at": datetime.now(timezone.utc),
        }
    },
]

# ── Seed functions ────────────────────────────────────────────────────────────

def seed_blind_structure_base(db):
    ref = db.collection('config').document('blind_structure_base')
    ref.set({
        "levels": BLIND_LEVELS_BASE,
        "level_count": len(BLIND_LEVELS_BASE),
        "note": "Levels 1-51 are identical across all PPPoker MTT formats",
        "updated_at": datetime.now(timezone.utc),
    })
    print(f"  OK blind_structure_base: {len(BLIND_LEVELS_BASE)} levels written")

def seed_payout_structure(db):
    ref = db.collection('config').document('payout_structure')
    ref.set({
        "brackets": PAYOUT_BRACKETS,
        "bracket_count": len(PAYOUT_BRACKETS),
        "note": "Shared payout table for all PPPoker tournaments. "
                "1st-place % for 151-200 and 201-250 are estimated (cut off in source screenshots).",
        "updated_at": datetime.now(timezone.utc),
    })
    print(f"  OK payout_structure: {len(PAYOUT_BRACKETS)} brackets written")

def seed_tournaments(db):
    for t in TOURNAMENTS:
        ref = db.collection('tournaments').document(t['id'])
        ref.set(t['data'])
        extras = len(t['data'].get('blind_structure_extra', []))
        if t['data'].get('blind_structure_override'):
            total_levels = extras
        else:
            total_levels = len(BLIND_LEVELS_BASE) + extras
        print(f"  OK tournaments/{t['id']}: {t['data']['name']} "
              f"(AUD {t['data']['buy_in_total']:.2f}, {total_levels} blind levels)")

def seed_admin_config(db):
    ADMIN_UID = os.getenv('ADMIN_UID', '')
    if not ADMIN_UID:
        print("  WARN: ADMIN_UID env var not set -- skipping admin config seed")
        print("    Set it and re-run, or add manually to /config/admins in Firestore")
        return
    ref = db.collection('config').document('admins')
    ref.set({"uids": [ADMIN_UID], "updated_at": datetime.now(timezone.utc)})
    print(f"  OK config/admins: UID {ADMIN_UID} set as admin")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("Seeding Firestore...")
    db = get_db()
    seed_blind_structure_base(db)
    seed_payout_structure(db)
    seed_tournaments(db)
    seed_admin_config(db)
    print("\nDone. Collections written:")
    print("  /config/blind_structure_base")
    print("  /config/payout_structure")
    print("  /config/admins  (if ADMIN_UID was set)")
    print("  /tournaments/deep_freeze")
    print("  /tournaments/crazy_2")
