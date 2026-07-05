#!/usr/bin/env python3
"""Seed the five Wave Farm staff operators with the default PIN 0000.

Idempotent: re-running re-asserts PIN 0000 + email/name for these five and
leaves any other operator (e.g. forrest) untouched. Run on the tower box, or
set USERS_FILE. Names/emails sourced from Wave Farm directory (wavefarm.org).
"""
import add_user

STAFF = [
    ("galen",    "Galen Joseph-Hunter",   "galen@wavefarm.org"),
    ("meredith", "Meredith Kooi",         "meredith@wavefarm.org"),
    ("caroline", "Caroline Preziosi",     "caroline@wavefarm.org"),
    ("jimmy",    "Jimmy Garver",          "jimmy@wavefarm.org"),
    ("bianca",   "Bianca Felix Biberaj",  "bianca@wavefarm.org"),
]

if __name__ == '__main__':
    print(f"Seeding into {add_user.USERS_FILE}")
    for name, display, email in STAFF:
        add_user.set_user(name, pin="0000", email=email, display=display)
        print(f"  ✓ {name:<10} PIN 0000  {display} <{email}>")
    print("Done. Existing operators preserved.")
