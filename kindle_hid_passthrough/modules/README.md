# Bundled `uhid.ko` modules

Some Broadcom-era Kindles ship a kernel with `CONFIG_UHID` disabled and no
`/dev/uhid`. For those we bundle a prebuilt `uhid.ko` and `insmod` it at startup.
MediaTek Kindles (11th gen onward) have UHID in-kernel and need nothing.

Modules are matched by exact filename:
`uhid-{uname-r}-{trailing-build-from-/etc/version.txt}-{codename}.ko`.
A single model can need more than one module across firmware updates (different
`uname-r` or build). See `../../docs/uhid-research.md` for the build recipe.

## Coverage matrix

Legend: ✅ have it · ⚠️ built, not yet device-tested · ❌ missing · n/a native UHID

| Model | Codename | Chip | Kernel (`uname -r`) | uhid.ko status |
|-------|----------|------|---------------------|----------------|
| Oasis (2016) | duet | BCM4343 | 3.0.35-lab126 | ❌ needs uhid backport (kernel predates mainline uhid) |
| Basic 2 (2016) | heisenberg | BCM4343 | 3.10.53-lab126 | ⚠️ build 409749 |
| Oasis 2 (2017) | zelda | BCM4343 | 4.1.15-lab126 | ✅ builds 409745, 443455 |
| Paperwhite 4 (2018) | rex | BCM4343 | 4.1.15-lab126 | ✅ builds 435186, 476967 |
| Basic 3 (2019) | rex | BCM4343 | 4.1.15-lab126 | ✅ builds 435186, 476967 |
| Oasis 3 (2019) | zelda | BCM4343 | 4.1.15-lab126 | ✅ builds 409745, 443455 |
| Paperwhite 5 (2021) | — | MediaTek | — | n/a (native `/dev/uhid`) |
| Basic 4 (2022) | — | MediaTek | — | n/a |
| Scribe (2022) | — | MediaTek | — | n/a |
| Basic 5 (2024) | — | MediaTek | — | n/a |
| Paperwhite 6 (2024) | — | MediaTek | — | n/a |
| Scribe 2 (2024) | — | MediaTek | — | n/a |
| Colorsoft (2024) | — | MediaTek | — | n/a |
| Scribe 3 (2025) | — | MediaTek | — | n/a |
| Scribe CS (2025) | — | MediaTek | — | n/a |

`build` is the trailing number from `/etc/version.txt`. Kernel and build can vary
across firmware updates, so a covered model may still hit an unbuilt build. When a
Kindle hits a missing module the daemon logs the exact `uhid-...ko` filename plus
its model, codename, kernel, and `/etc/version.txt`, which is everything needed to
build it.
