"""Gemeinsame Jinja2-Umgebung fuer alle Bereiche.

`asset_v` haengt an die Stylesheet-Adresse eine Kennung des Prozessstarts:
ohne sie behielt der Browser nach einem Neubau die alte styles.css
(Designabgleich 2026-09-29).
"""

from __future__ import annotations

import time

from fastapi.templating import Jinja2Templates

from app.advent import initialen, ornament_name, siegel_kontur

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["asset_v"] = str(int(time.time()))
templates.env.filters["initialen"] = initialen
templates.env.filters["ornament_name"] = ornament_name
templates.env.filters["siegel_kontur"] = siegel_kontur
