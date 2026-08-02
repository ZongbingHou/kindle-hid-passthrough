#!/usr/bin/env python3
"""Tests for the cpuidle latency hold (issue #120). Run: python3 tests/test_cpu_latency.py"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'kindle_hid_passthrough'))

import cpu_latency  # noqa: E402
from cpu_latency import CpuLatencyHold  # noqa: E402

# i.MX7D on the NXP 4.1 BSP, as reported on the Oasis 2 in issue #120.
IMX7D = [('WFI', 1), ('WAIT', 50), ('LOW-POWER-IDLE', 500)]


def fake_cpuidle(states, cpus=2):
    root = tempfile.mkdtemp()
    for cpu in range(cpus):
        for index, (name, latency) in enumerate(states):
            path = os.path.join(root, f'cpu{cpu}', 'cpuidle', f'state{index}')
            os.makedirs(path)
            with open(os.path.join(path, 'name'), 'w') as f:
                f.write(name + '\n')
            with open(os.path.join(path, 'latency'), 'w') as f:
                f.write(f'{latency}\n')
            with open(os.path.join(path, 'disable'), 'w') as f:
                f.write('0\n')
    cpu_latency.CPUIDLE_GLOB = os.path.join(root, 'cpu*', 'cpuidle')
    return root


def test_imx7d_blocks_only_the_deepest_state():
    root = fake_cpuidle(IMX7D)
    try:
        hold = CpuLatencyHold(2000000)
        assert abs(hold.budget_us - 80.0) < 0.01, hold.budget_us
        target, paths = hold._plan()
        # WAIT (50us) stays allowed, LOW-POWER-IDLE (500us) does not.
        assert target == 50, target
        assert len(paths) == 2, paths  # state2 on both CPUs
        assert all(p.endswith('state2/disable') for p in paths), paths
    finally:
        shutil.rmtree(root)


def test_no_hold_when_every_state_fits():
    root = fake_cpuidle([('WFI', 1), ('WAIT', 50)])
    try:
        assert CpuLatencyHold(2000000)._plan() is None
    finally:
        shutil.rmtree(root)


def test_never_forces_wfi_only():
    """Even if state1 exceeds the budget, keep it: WFI-only would cost battery."""
    root = fake_cpuidle([('WFI', 1), ('WAIT', 300), ('LOW-POWER-IDLE', 500)])
    try:
        target, paths = CpuLatencyHold(2000000)._plan()
        assert target == 300, target
        assert all(p.endswith('state2/disable') for p in paths), paths
    finally:
        shutil.rmtree(root)


def test_lower_baud_allows_deeper_idle():
    """The budget scales with the FIFO drain time, not a hardcoded state index."""
    root = fake_cpuidle(IMX7D)
    try:
        assert CpuLatencyHold(115200)._plan() is None
    finally:
        shutil.rmtree(root)


def test_sysfs_fallback_restores_original_values():
    root = fake_cpuidle(IMX7D)
    cpu_latency.CPU_DMA_LATENCY_DEV = os.path.join(root, 'no-such-device')
    try:
        hold = CpuLatencyHold(2000000)
        hold.acquire()
        state2 = os.path.join(root, 'cpu0', 'cpuidle', 'state2', 'disable')
        state1 = os.path.join(root, 'cpu0', 'cpuidle', 'state1', 'disable')
        assert open(state2).read().strip() == '1'
        assert open(state1).read().strip() == '0'
        hold.release()
        assert open(state2).read().strip() == '0'
        # Releasing twice must not resurrect the constraint or throw.
        hold.release()
        assert open(state2).read().strip() == '0'
    finally:
        shutil.rmtree(root)


def test_acquire_is_idempotent():
    root = fake_cpuidle(IMX7D)
    cpu_latency.CPU_DMA_LATENCY_DEV = os.path.join(root, 'no-such-device')
    try:
        hold = CpuLatencyHold(2000000)
        hold.acquire()
        held = dict(hold._restore)
        hold.acquire()
        assert hold._restore == held, hold._restore
        hold.release()
        assert hold._restore == {}
    finally:
        shutil.rmtree(root)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
