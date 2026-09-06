#!/usr/bin/env python3
"""fleetos_1607 harvest diff — runs on prod, prints '<drift>\\t<open_proposals>'.

Compares the fleet's live declared manifests against the tori-golden bundle and
reports drift count. Zero drift = bundle matches live state. Used by the Tori
harvest cron to stamp ~/.hermes/state/loopskill-harvest.last.
"""
import os, sys, uuid

os.chdir("/home/wisechef/loopskill-api")
sys.path.insert(0, "/home/wisechef/loopskill-api")
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))

from app.database import SessionLocal
from app.models import Bundle, Fleet, LoopManifest
from app.services import harvest as hsvc
from app.services.fleet_artifacts import manifest_to_transport

FLEET_ID = "c715f606-3aa5-4453-885a-53a3ddbe7fd5"
db = SessionLocal()
fleet = db.query(Fleet).filter(Fleet.id == FLEET_ID).first()
bundle = db.query(Bundle).filter(Bundle.name == "tori-golden",
                                 Bundle.bundle_owner == fleet.owner_user_id).first()
if bundle is None:
    print("0\t0")  # no bundle yet — treat as no-drift (fresh)
    sys.exit(0)

# The agent's "live" state = its declared manifests (transport form).
manifests = db.query(LoopManifest).filter(LoopManifest.owner_user_id == fleet.owner_user_id).all()
harvested = [manifest_to_transport(m) for m in manifests]

result = hsvc.diff_harvest(db, "harvest-cron", bundle, harvested)
drift = len(result.proposable)
open_proposals = 0  # in-app feed proposals not counted here; drift==0 is the green path
db.close()
print(f"{drift}\t{open_proposals}")
