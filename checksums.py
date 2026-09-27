#!/usr/bin/env python3
"""SHA-256 manifest for recordings/.

  python3 checksums.py write    # (re)generate SHA256SUMS
  python3 checksums.py verify   # check every file against SHA256SUMS; exit 1 on any mismatch

SHA256SUMS uses the `sha256sum` format, so `sha256sum -c SHA256SUMS` also works.
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "SHA256SUMS")


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def files():
    out = []
    for root, _, names in os.walk(os.path.join(HERE, "recordings")):
        for n in names:
            out.append(os.path.relpath(os.path.join(root, n), HERE).replace(os.sep, "/"))
    return sorted(out)


def write():
    with open(MANIFEST, "w") as fh:
        for rel in files():
            fh.write("%s  %s\n" % (digest(os.path.join(HERE, rel)), rel))
    print("wrote SHA256SUMS: %d files" % len(files()))


def verify():
    expected = {}
    for line in open(MANIFEST):
        h, rel = line.rstrip("\n").split("  ", 1)
        expected[rel] = h
    present = set(files())
    bad = [r for r, h in expected.items() if r in present and digest(os.path.join(HERE, r)) != h]
    missing = sorted(set(expected) - present)
    extra = sorted(present - set(expected))
    for r in bad:
        print("MISMATCH", r)
    for r in missing:
        print("MISSING ", r)
    for r in extra:
        print("EXTRA   ", r)
    ok = not (bad or missing or extra)
    print("verify: %d files, %s" % (len(expected), "all OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    sys.exit(write() if cmd == "write" else verify())
