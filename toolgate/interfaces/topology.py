"""The gate as a diagram: the components, and the path a call takes between them.

The layout lives in Python, next to the data it draws, for one reason: geometry expressed only as
hand-written SVG cannot be checked. Here it is data, so ``layout_problems`` can assert the things a
reader would notice and a test can — boxes that overlap, an arrow that misses the box it points at,
an arrow drawn straight through a box it has nothing to do with, a label wider than the box that
holds it. The page renders what this module decides; it does not decide anything itself.

The nodes are the real collaborators (the same ones ``container.py`` wires). The edges are drawn
along the routes the gateway and engine really take, and ``path_of`` returns the edges *this* call
actually travelled, so the page animates a journey rather than a generic flow.
"""
from __future__ import annotations

NODE_W, NODE_H = 172, 64

# viewBox: wide enough for the longest label, tall enough for the return lanes below the boxes
VIEWBOX = (1340, 520)

# Two rows of components. The forward journey runs left to right along y=140; the sinks and the
# human sit below at y=380, which leaves the band between them free for the return lanes.
NODES = (
    {"id": "upstream", "label": "Upstream MCP", "sub": "the real tool",
     "kind": "external", "x": 250, "y": 40},
    {"id": "agent", "label": "MCP agent", "sub": "the caller",
     "kind": "external", "x": 30, "y": 140},
    {"id": "gateway", "label": "ToolGateProxy", "sub": "MCP stdio gateway",
     "kind": "frontend", "x": 250, "y": 140},
    {"id": "engine", "label": "Engine", "sub": "authorize use case",
     "kind": "backend", "x": 470, "y": 140},
    {"id": "gate", "label": "gate.cedar", "sub": "phase 1 · static",
     "kind": "security", "x": 690, "y": 140},
    {"id": "jev", "label": "Jev", "sub": "TypeSafe · the model",
     "kind": "external", "x": 910, "y": 140},
    {"id": "judgment", "label": "judgment.cedar", "sub": "phase 2 · enriched",
     "kind": "security", "x": 1130, "y": 140},
    {"id": "trace", "label": "TraceLog", "sub": "traces.jsonl",
     "kind": "database", "x": 470, "y": 380},
    {"id": "record", "label": "DecisionRecord", "sub": "decisions.jsonl",
     "kind": "database", "x": 690, "y": 380},
    {"id": "human", "label": "Human", "sub": "the approval queue",
     "kind": "external", "x": 30, "y": 380},
)

# Every relationship, with the waypoints it is drawn along. `points` starts on the source box and
# ends on the destination box; the checker below holds that promise.
EDGES = (
    {"id": "agent-gateway", "src": "agent", "dst": "gateway", "label": "tools/call",
     "points": ((202, 162), (250, 162))},
    {"id": "gateway-agent", "src": "gateway", "dst": "agent", "label": "the result",
     "points": ((250, 182), (202, 182))},
    {"id": "gateway-engine", "src": "gateway", "dst": "engine", "label": "authorize",
     "points": ((422, 162), (470, 162))},
    {"id": "gateway-upstream", "src": "gateway", "dst": "upstream", "label": "on ALLOW · forwarded",
     "points": ((336, 140), (336, 104))},
    {"id": "upstream-gateway", "src": "upstream", "dst": "gateway", "label": "the tool's reply",
     "points": ((376, 104), (376, 140))},
    {"id": "engine-gate", "src": "engine", "dst": "gate", "label": "phase 1 · request",
     "points": ((642, 162), (690, 162))},
    {"id": "engine-trace", "src": "engine", "dst": "trace", "label": "every boundary",
     "points": ((556, 204), (556, 380))},
    {"id": "engine-record", "src": "engine", "dst": "record", "label": "the verdict",
     "points": ((600, 204), (600, 350), (776, 350), (776, 380))},
    {"id": "gate-jev", "src": "gate", "dst": "jev", "label": "gray · the battery",
     "points": ((862, 162), (910, 162))},
    {"id": "jev-judgment", "src": "jev", "dst": "judgment", "label": "answers + thresholds",
     "points": ((1082, 162), (1130, 162))},
    {"id": "gate-gateway", "src": "gate", "dst": "gateway", "label": "on BLOCK · the decision",
     "points": ((700, 204), (700, 250), (310, 250), (310, 204))},
    {"id": "judgment-gateway", "src": "judgment", "dst": "gateway", "label": "the verdict",
     "points": ((1140, 204), (1140, 310), (390, 310), (390, 204))},
    {"id": "judgment-human", "src": "judgment", "dst": "human", "label": "on ESCALATE · queued",
     "points": ((1200, 204), (1200, 470), (116, 470), (116, 444))},
)

# What a packet arriving along each edge has just revealed: the boundary whose recorded payload
# becomes readable at that step. Deliberately *not* the destination's own boundary — on a return
# hop the message coming back is the decision or the verdict, not a fresh ingress, so reading
# `BOUNDARY_OF[dst]` there would open the wrong one.
OPENS = {
    "agent-gateway": "ingress",        # the call has arrived at the gate
    "gateway-engine": "normalize",     # and been reduced to an authorization request
    "engine-gate": "gate",             # phase 1 Cedar has been asked
    "gate-gateway": "verdict",         # blocked: the decision heads back
    "gate-jev": "judgment",            # gray: the battery goes out
    "jev-judgment": "verdict",         # answered: the gate now has its decision
    "judgment-gateway": "verdict",     # and it travels back
    "judgment-human": "human",         # escalated, and queued for a person
    "gateway-upstream": "result",      # forwarded — and here the trace goes blind
    "upstream-gateway": "result",      # the reply comes back, still unrecorded
    "gateway-agent": "result",         # what the agent finally receives
    "engine-trace": "verdict",         # the engine writes every boundary down
    "engine-record": "verdict",        # and writes the decision
}

# Which boundary of the journey each component owns, so a click on the diagram opens the real
# payload the trace recorded for that step rather than a description of it.
BOUNDARY_OF = {
    "agent": "ingress",
    "gateway": "ingress",
    "engine": "normalize",
    "gate": "gate",
    "jev": "judgment",
    "judgment": "judgment",
    "record": "verdict",
    "trace": "verdict",
    "upstream": "result",
    "human": "human",
}

_BY_ID = {node["id"]: node for node in NODES}
_EDGE_BY_ID = {edge["id"]: edge for edge in EDGES}


def node(node_id: str) -> dict:
    return _BY_ID[node_id]


def edge(edge_id: str) -> dict:
    return _EDGE_BY_ID[edge_id]


def box(node_id: str) -> tuple[int, int, int, int]:
    """The node's rectangle as (left, top, right, bottom)."""
    n = _BY_ID[node_id]
    return n["x"], n["y"], n["x"] + NODE_W, n["y"] + NODE_H


def path_of(entry: dict) -> list[str]:
    """The edges this call travelled, in order, derived from what was actually recorded.

    A phase-1 call never reaches the judgment model, so its path skips those edges entirely —
    the diagram shows the journey taken, not the journey that was available.
    """
    edges = ["agent-gateway", "gateway-engine", "engine-gate"]
    if entry.get("phase") == 2:
        edges += ["gate-jev", "jev-judgment"]
    if entry.get("decision") == "escalate":
        return edges + ["judgment-human"]
    edges.append("judgment-gateway" if entry.get("phase") == 2 else "gate-gateway")
    if entry.get("decision") == "allow":
        edges += ["gateway-upstream", "upstream-gateway"]
    return edges + ["gateway-agent"]


# --- the checker ------------------------------------------------------------------

def _segments(points):
    return list(zip(points, points[1:]))


def _on_border(point, rect, tol=1) -> bool:
    x, y = point
    left, top, right, bottom = rect
    inside_x = left - tol <= x <= right + tol
    inside_y = top - tol <= y <= bottom + tol
    return inside_x and inside_y


def _segment_hits_box(a, b, rect) -> bool:
    """Does the segment a→b enter the rectangle's interior? Axis-aligned segments only."""
    left, top, right, bottom = rect
    (x1, y1), (x2, y2) = a, b
    lo_x, hi_x = sorted((x1, x2))
    lo_y, hi_y = sorted((y1, y2))
    # shrink by a pixel so merely touching an edge does not count as entering
    return lo_x < right - 1 and hi_x > left + 1 and lo_y < bottom - 1 and hi_y > top + 1


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _label_fits(text: str, width: int, size: int = 11) -> bool:
    """A monospace estimate: 0.62 em per character is close enough to catch real overflow."""
    return len(text) * size * 0.62 <= width - 12


def layout_problems() -> list[str]:
    """Everything about the drawing that would look wrong, as plain sentences.

    Not a rendering check — it cannot see antialiasing or colour. It checks the geometry that is
    authored here, which is where a mistake would actually come from.
    """
    problems = []

    for i, a in enumerate(NODES):
        for b in NODES[i + 1:]:
            if _overlaps(box(a["id"]), box(b["id"])):
                problems.append(f"{a['id']} and {b['id']} overlap")

    for n in NODES:
        width = NODE_W - 16
        if not _label_fits(n["label"], width, 13):
            problems.append(f"{n['id']} label {n['label']!r} is wider than its box")
        if not _label_fits(n["sub"], width, 10):
            problems.append(f"{n['id']} subtitle {n['sub']!r} is wider than its box")

    for e in EDGES:
        points = e["points"]
        if not _on_border(points[0], box(e["src"])):
            problems.append(f"{e['id']} does not start on {e['src']}")
        if not _on_border(points[-1], box(e["dst"])):
            problems.append(f"{e['id']} does not end on {e['dst']}")
        for p in points:
            if not (0 <= p[0] <= VIEWBOX[0] and 0 <= p[1] <= VIEWBOX[1]):
                problems.append(f"{e['id']} has a waypoint {p} outside the viewBox")
        for seg in _segments(points):
            for n in NODES:
                if n["id"] in (e["src"], e["dst"]):
                    continue
                if _segment_hits_box(*seg, box(n["id"])):
                    problems.append(f"{e['id']} is drawn through {n['id']}")

    for n in NODES:
        left, top, right, bottom = box(n["id"])
        if right > VIEWBOX[0] or bottom > VIEWBOX[1]:
            problems.append(f"{n['id']} sticks out of the viewBox")

    return problems
