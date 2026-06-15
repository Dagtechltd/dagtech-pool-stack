#!/usr/bin/env python3
"""Verify release binaries match the target OS/architecture.

This uses executable headers directly instead of the host `file` command so the
check works from Linux, macOS, and Windows build hosts.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import struct
import sys


TARGETS = {
    "linux-arm64": ("elf", 183),
    "linux-amd64": ("elf", 62),
    "darwin-arm64": ("macho", 0x0100000C),
    "darwin-amd64": ("macho", 0x01000007),
    "windows-arm64": ("pe", 0xAA64),
    "windows-amd64": ("pe", 0x8664),
}

ELF_MACHINES = {
    62: "amd64",
    183: "arm64",
}
MACHO_CPUS = {
    0x01000007: "amd64",
    0x0100000C: "arm64",
}
PE_MACHINES = {
    0x8664: "amd64",
    0xAA64: "arm64",
}


def identify(path: Path) -> tuple[str, int, str]:
    with path.open("rb") as handle:
        data = handle.read(4096)
    if data.startswith(b"\x7fELF"):
        if len(data) < 20:
            raise ValueError("truncated ELF header")
        if data[4] != 2:
            raise ValueError("expected 64-bit ELF")
        endian = "<" if data[5] == 1 else ">" if data[5] == 2 else None
        if endian is None:
            raise ValueError("unknown ELF byte order")
        machine = struct.unpack(endian + "H", data[18:20])[0]
        return "elf", machine, ELF_MACHINES.get(machine, f"elf-machine-{machine}")

    if data.startswith(b"MZ"):
        if len(data) < 0x40:
            raise ValueError("truncated PE DOS header")
        pe_offset = struct.unpack("<I", data[0x3C:0x40])[0]
        if len(data) < pe_offset + 6 or data[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise ValueError("invalid PE header")
        machine = struct.unpack("<H", data[pe_offset + 4 : pe_offset + 6])[0]
        return "pe", machine, PE_MACHINES.get(machine, f"pe-machine-{machine}")

    magic = data[:4]
    if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        cpu = struct.unpack("<I", data[4:8])[0]
        return "macho", cpu, MACHO_CPUS.get(cpu, f"macho-cpu-{cpu:#x}")
    if magic in (b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"):
        cpu = struct.unpack(">I", data[4:8])[0]
        return "macho", cpu, MACHO_CPUS.get(cpu, f"macho-cpu-{cpu:#x}")
    if magic in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        raise ValueError("fat Mach-O binaries must be thinned before packaging")

    raise ValueError("unknown executable format")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("binaries", nargs="+", type=Path)
    args = parser.parse_args()

    expected_format, expected_machine = TARGETS[args.target]
    ok = True
    for path in args.binaries:
        try:
            actual_format, actual_machine, actual_arch = identify(path)
        except Exception as exc:
            print(f"{path}: invalid executable: {exc}", file=sys.stderr)
            ok = False
            continue
        if actual_format != expected_format or actual_machine != expected_machine:
            print(
                f"{path}: expected {args.target}, got {actual_format}/{actual_arch}",
                file=sys.stderr,
            )
            ok = False
            continue
        print(f"{path}: ok {args.target}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
