#!/usr/bin/env python3
"""Verify the rendered ToolGate DDD diagram geometrically.

Checks the things you would otherwise have to eyeball:
  * the rings actually nest (Adapter > Application > Domain)
  * no two nodes overlap
  * every component sits inside a ring and every external sits outside
  * nothing is clipped by the SVG viewBox

Usage:
    python3 docs/verify_layout.py docs/toolgate-ddd.html

If the file has not been rendered yet, headless Chrome is used to render it first.
Requires: beautifulsoup4  (pip install beautifulsoup4)

Exit code 0 = PASS, 1 = FAIL.
"""
import re
import shutil
import subprocess
import sys
import pathlib

from bs4 import BeautifulSoup

TRANSFORM = re.compile(r"translate\(\s*(-?[\d.]+)\s*[, ]\s*(-?[\d.]+)\s*\)")

RING_ORDER = [("Application_Ring", "Adapter_Ring"), ("Domain_Ring", "Application_Ring")]
SUB_RINGS = [
    ("Domain_Model", "Domain_Ring"), ("Domain_Rules", "Domain_Ring"),
    ("Driving_Adapters", "Adapter_Ring"), ("Driven_Adapters", "Adapter_Ring"),
]


def get_dom(path: str) -> str:
    """Return a DOM dump; render headlessly first if it is not already rendered."""
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    if '<svg id="tg-graph-' in text:
        return text
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        sys.exit("no rendered SVG in file and no headless Chrome available")
    url = "file://" + str(pathlib.Path(path).resolve())
    out = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=25000", "--dump-dom", url],
        capture_output=True, text=True, timeout=180,
    )
    return out.stdout


def load_svg(dom: str) -> str:
    m = re.search(r'(<svg id="tg-graph-\d+".*?</svg>)', dom, re.S)
    if not m:
        sys.exit("could not find the rendered mermaid <svg> in the DOM")
    return m.group(1)


def bare(gid: str) -> str:
    """'tg-graph-1-flowchart-GW-0' -> 'GW';  'tg-graph-1-Adapter_Ring' -> 'Adapter_Ring'."""
    m = re.match(r"^tg-graph-\d+-flowchart-(.+)-\d+$", gid or "")
    if m:
        return m.group(1)
    m = re.match(r"^tg-graph-\d+-(.+)$", gid or "")
    return m.group(1) if m else (gid or "")


def abs_rect(el):
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    w, h = float(el.get("width") or 0), float(el.get("height") or 0)
    dx = dy = 0.0
    for p in el.find_parents("g"):
        m = TRANSFORM.search(p.get("transform") or "")
        if m:
            dx += float(m.group(1))
            dy += float(m.group(2))
    return (dx + x, dy + y, w, h)


def union(rects):
    xs = [r[0] for r in rects]
    ys = [r[1] for r in rects]
    xe = [r[0] + r[2] for r in rects]
    ye = [r[1] + r[3] for r in rects]
    return (min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))


def inside(a, b, tol=0.6):
    return (a[0] >= b[0] - tol and a[1] >= b[1] - tol
            and a[0] + a[2] <= b[0] + b[2] + tol and a[1] + a[3] <= b[1] + b[3] + tol)


def overlap(a, b):
    ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return ox * oy if ox > 0 and oy > 0 else 0.0


def main(path: str) -> int:
    soup = BeautifulSoup(load_svg(get_dom(path)), "html.parser")

    clusters = []
    for g in soup.find_all("g", class_="cluster"):
        own = g.find("rect", recursive=False)
        if own is not None:
            clusters.append((bare(g.get("id", "")), abs_rect(own)))
    have = dict(clusters)

    nodes = []
    for g in soup.find_all("g", class_="node"):
        rs = [abs_rect(r) for r in g.find_all("rect")]
        if rs:
            nodes.append((bare(g.get("id", "")), union(rs), "external" in (g.get("class") or [])))

    svg_el = soup.find("svg")
    vb = svg_el.get("viewBox") or svg_el.get("viewbox")
    if vb:
        p = [float(v) for v in vb.replace(",", " ").split()]
        vw, vh = p[2], p[3]
    else:
        vw, vh = float(svg_el.get("width")), float(svg_el.get("height"))

    print(f"clusters={len(clusters)} nodes={len(nodes)} natural={vw:.0f}x{vh:.0f} "
          f"aspect={vw / vh:.2f}:1")

    ok = True
    print("\nring nesting:")
    for inner, outer in RING_ORDER + SUB_RINGS:
        if inner in have and outer in have:
            r = inside(have[inner], have[outer])
            ok &= r
            print(f"  {'PASS' if r else 'FAIL'}  {inner} inside {outer}")
        else:
            print(f"  FAIL  missing cluster {inner!r} or {outer!r}")
            ok = False

    print("\noverlaps:")
    bad = 0
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if overlap(nodes[i][1], nodes[j][1]) > 1.0:
                bad += 1
                print(f"  FAIL  {nodes[i][0]} overlaps {nodes[j][0]}")
    ok &= not bad
    print("  none" if not bad else f"  {bad} overlapping pair(s)")

    print("\nring membership:")
    mis = 0
    for nid, bb, is_ext in nodes:
        cont = [c for c, box in clusters if inside(bb, box)]
        if is_ext and cont:
            mis += 1
            print(f"  FAIL  external {nid} sits inside {cont}")
        elif not is_ext and not cont:
            mis += 1
            print(f"  FAIL  {nid} is outside every ring")
    ok &= not mis
    print("  components in, externals out" if not mis else f"  {mis} misplaced")

    print("\nviewBox fit:")
    clip = 0
    for cid, bb in clusters + [(n, b) for n, b, _ in nodes]:
        if bb[0] < -0.6 or bb[1] < -0.6 or bb[0] + bb[2] > vw + 0.6 or bb[1] + bb[3] > vh + 0.6:
            clip += 1
            print(f"  FAIL  clipped: {cid}")
    ok &= not clip
    print("  nothing clipped" if not clip else f"  {clip} clipped")

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
