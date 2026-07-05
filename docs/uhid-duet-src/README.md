Backported `uhid.c` for kernel `3.0.35-lab126` (Kindle Oasis 1 / duet).

Mainline `drivers/hid/uhid.c` landed in Linux 3.6, so on 3.0.35 it has to be
backported rather than enabled. `uhid.c` here is adapted from the 3.15+ driver
down to the pre-3.15 `hid_ll_driver` (no `.raw_request`/`.output_report`),
keeping only the event ABI the daemon uses: `UHID_CREATE2`, `UHID_INPUT2`,
`UHID_DESTROY` (plus legacy `CREATE`/`INPUT`). `uhid.h` is the mainline v4.1
header, whose struct layout matches what `uhid_handler.py` packs.

The stock Oasis 1 kernel has no HID core either, so `build.sh` also builds
`hid.ko` (`hid-core.o` + `hid-input.o`); the daemon loads it before uhid.ko.

Files:
- `uhid.c` - the backported driver (goes in `drivers/hid/`)
- `uhid.h` - the UHID ABI header (goes in `include/linux/`)
- `build.sh` - reproduces the shipped `hid.ko` + `uhid.ko`

Run `SRC=... TCBIN=... COMPATLIB=... ./build.sh`. See `../uhid-research.md`
for the toolchain, config, and on-device notes.
