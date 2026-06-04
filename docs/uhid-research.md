# Building uhid.ko for BCM-era Kindles

8th-10th gen Kindle kernels shipped without CONFIG_UHID. To build a working .ko for a given firmware:

1. Grab the kernel source for that firmware from https://www.amazon.com/gp/help/customer/display.html?nodeId=200203720 (e.g. `Kindle_src_5.18.2_4434550025.tar.gz`).
2. Extract twice (outer tarball wraps `gplrelease/linux-4.1.15.tar.gz`).
3. Cross-compile with Linaro 4.9.4 (matches Amazon's gcc 4.9.1): https://releases.linaro.org/components/toolchain/binaries/4.9-2017.01/arm-linux-gnueabihf/
4. Apply Amazon's defconfig (`imx_v7_zelda_defconfig` for KO2/KOA3, `imx_v7_rex_defconfig` for PW4/Basic 4, `imx_wario_defconfig` for Basic 2).
5. `scripts/config --module CONFIG_UHID && scripts/config --disable CONFIG_FB_MXC_EINK_V2_PANEL CONFIG_FB_MXC_EINK_PANEL`
6. `make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- vmlinux modules` (the vmlinux pass is what populates Module.symvers with module_layout's CRC, without it the module gets "no symbol version for module_layout" at insmod time).
7. `make M=drivers/hid modules` produces `drivers/hid/uhid.ko`.

Verify with `readelf -A uhid.ko` (expect `Tag_CPU_arch: v7`) and `readelf -S uhid.ko | grep __versions` (expect non-zero size).

Filename for shipping: `uhid-{uname-r}-{trailing-build-number-from-/etc/version.txt}-{codename}.ko`.
