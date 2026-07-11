# OBD Truck-Link Check — quick reference (no Claude session needed)

Everything software-side is fixed and verified. The only remaining step is
confirming the adapter hands data over on your F-150. Follow this at the truck.

## Do this
1. Flash `dist/gost-perimetr-os.img.xz` (Raspberry Pi Imager → Use custom →
   this file → **do NOT** apply OS customization). Boot the Pi.
2. In the truck: **key to RUN, engine running** (a PowerBoost in silent EV
   mode may keep the engine off — press the brake and start it so the gas
   engine is available; RPM 0 with engine off is normal, not a fault).
3. Plug the OBDLink into the OBD-II port (green LED on).
4. On the dashboard touchscreen: **SETTINGS → SOURCE → [ RUN OBD DIAGNOSTIC ]**.
5. Wait ~15 s. Read the box that appears.

## What the box means

| What you see | Meaning | Fix |
|---|---|---|
| `proto 6: RPM=<number> ... <== WORKS` | **Linked!** | Tap SOURCE → **AUTO (OBD2)**. DRIVE shows ● LIVE — OBDLink. Done. |
| `proto 6: RPM=None` + `010C raw: 41 0C ...` | Adapter got bytes but the number didn't parse | Send Claude the `010C raw:` line — it's a decode tweak. |
| `010C raw: SEARCHING...` (all protos) | Bus not linking | Engine must be RUNNING; reseat the OBD plug; try again. |
| `010C raw: CAN ERROR` | Wrong bus speed | Send Claude the screen — force a different protocol. |
| `010C raw: NO DATA` | ECU not answering that PID | Engine running? Try with the truck in motion briefly. |
| `PORTS: NONE FOUND` | Pi doesn't see the adapter | USB cable/port issue — reseat; try the other USB ports. |

## No-touchscreen fallback (SSH)
```bash
sudo systemctl stop hud-backend && sleep 1
/opt/gost/venv/bin/python3 /opt/gost/tools/test_obd.py
sudo systemctl start hud-backend
```

## If it links but you want to lock it in
Once AUTO shows LIVE once, the working protocol is saved to
`/opt/gost/state/obd.json` and every future boot links on the first try.

## If anything's wrong, send Claude
A photo of the diagnostic box (or the `test_obd.py` output). That single
image says exactly what to change — it's a one-line fix from there.
