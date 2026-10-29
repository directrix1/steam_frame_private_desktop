#!/usr/bin/env python3
"""Regenerate icons/frame-private-desktop.{svg,png}.

Breeze's "desktop" icon (what the Frame's built-in Desktop entry shows) with
a padlock badge. Needs the Breeze icon theme and rsvg-convert (both on
SteamOS). Breeze icons are LGPL-3.0-or-later (KDE).
"""
import re
import subprocess
from pathlib import Path

SRC = Path("/usr/share/icons/breeze/places/96/desktop.svg")
OUT = Path(__file__).resolve().parent

base = SRC.read_text(encoding="utf-8")
base = re.sub(r"<\?xml[^>]*\?>", "", base).strip()
# Nest the original icon as an inner <svg>, unchanged.
inner = re.sub(r"<svg\b", '<svg x="0" y="0" width="96" height="96"', base, count=1)

padlock = """
  <defs>
    <linearGradient id="fpdLockBody" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#fdd85d"/>
      <stop offset="1" stop-color="#e5a50a"/>
    </linearGradient>
    <filter id="fpdShadow" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="1.5" stdDeviation="1.5" flood-color="#000" flood-opacity="0.45"/>
    </filter>
  </defs>
  <g filter="url(#fpdShadow)">
    <path d="M58 58 V48 a12 12 0 0 1 24 0 V58" fill="none" stroke="#fcfcfc" stroke-width="10" stroke-linecap="round"/>
    <path d="M58 58 V48 a12 12 0 0 1 24 0 V58" fill="none" stroke="#5b6168" stroke-width="5.5" stroke-linecap="round"/>
    <rect x="49" y="55" width="42" height="35" rx="6" fill="url(#fpdLockBody)" stroke="#fcfcfc" stroke-width="2.5"/>
    <circle cx="70" cy="69" r="4.5" fill="#5b4000"/>
    <path d="M67.8 70 h4.4 l1.2 11 h-6.8 z" fill="#5b4000"/>
  </g>
"""

svg = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
       'viewBox="0 0 96 96" width="96" height="96">\n'
       f"{inner}\n{padlock}</svg>\n")

svg_path = OUT / "frame-private-desktop.svg"
svg_path.write_text(svg, encoding="utf-8")
subprocess.run(["rsvg-convert", "-w", "256", "-h", "256", str(svg_path),
                "-o", str(OUT / "frame-private-desktop.png")], check=True)
print("wrote", svg_path, "and", OUT / "frame-private-desktop.png")
