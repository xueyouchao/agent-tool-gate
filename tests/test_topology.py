"""The diagram's geometry, checked as data.

The layout lives in Python for exactly one reason: so it can be checked. These tests check the
drawing, and then check the checker — a validator that never fires is worse than none, so each
kind of mistake it claims to catch is introduced here on purpose.
"""
import pytest

from toolgate.interfaces import topology
from toolgate.interfaces.topology import (
    BOUNDARY_OF, EDGES, NODES, OPENS, layout_problems, path_of,
)

FOUR_SHAPES = [
    ({"phase": 1, "decision": "block"}, "phase 1 · block"),
    ({"phase": 1, "decision": "allow"}, "phase 1 · allow"),
    ({"phase": 2, "decision": "allow"}, "gray · allow"),
    ({"phase": 2, "decision": "escalate"}, "gray · escalate"),
]

# The message a reader should be handed at each step of each shape of call. Spelled out rather
# than derived, so a change to the route has to be looked at rather than absorbed.
STEPS = {
    "phase 1 · block": ["ingress", "normalize", "gate", "verdict", "result"],
    "phase 1 · allow": ["ingress", "normalize", "gate", "verdict", "result", "result", "result"],
    "gray · allow": ["ingress", "normalize", "gate", "judgment", "verdict", "verdict", "result",
                     "result", "result"],
    "gray · escalate": ["ingress", "normalize", "gate", "judgment", "verdict", "human"],
}


def with_node(node_id, **changes):
    return tuple({**n, **changes} if n["id"] == node_id else n for n in NODES)


def with_edge(edge_id, **changes):
    return tuple({**e, **changes} if e["id"] == edge_id else e for e in EDGES)


@pytest.fixture
def swap(monkeypatch):
    """Put a deliberately broken layout in place, the way `layout_problems` would see it."""
    def _swap(nodes=None, edges=None):
        if nodes is not None:
            monkeypatch.setattr(topology, "NODES", nodes)
            monkeypatch.setattr(topology, "_BY_ID", {n["id"]: n for n in nodes})
        if edges is not None:
            monkeypatch.setattr(topology, "EDGES", edges)
            monkeypatch.setattr(topology, "_EDGE_BY_ID", {e["id"]: e for e in edges})
    return _swap


# --- the drawing ----------------------------------------------------------------

def test_the_authored_layout_has_nothing_wrong_with_it():
    assert layout_problems() == []


def test_every_edge_connects_two_nodes_that_exist():
    for e in EDGES:
        assert e["src"] in BOUNDARY_OF, e["id"]
        assert e["dst"] in BOUNDARY_OF, e["id"]


def test_every_component_owns_a_boundary_so_a_click_can_open_something():
    assert set(BOUNDARY_OF) == {n["id"] for n in NODES}


@pytest.mark.parametrize("entry,label", FOUR_SHAPES)
def test_every_component_a_path_visits_has_a_stage_to_open(entry, label):
    """Clicking a component must open something real — so every component a call reaches has a
    boundary the stage list actually produced for that call."""
    from toolgate.interfaces.viewer import stages_of

    boundaries = {s["boundary"] for s in stages_of(entry)}
    for edge_id in path_of(entry):
        visited = topology.edge(edge_id)["dst"]
        assert BOUNDARY_OF[visited] in boundaries, f"{label}: {visited} has no stage"


# --- the journeys ---------------------------------------------------------------

@pytest.mark.parametrize("entry,label", FOUR_SHAPES)
def test_every_shape_of_call_has_a_path(entry, label):
    assert path_of(entry), label


@pytest.mark.parametrize("entry,label", FOUR_SHAPES)
def test_every_edge_a_path_uses_exists(entry, label):
    known = {e["id"] for e in EDGES}
    assert set(path_of(entry)) <= known, label


@pytest.mark.parametrize("entry,label", FOUR_SHAPES)
def test_a_path_is_an_unbroken_chain_from_the_agent_to_the_end(entry, label):
    """Each edge must start where the last one finished, or the packet would teleport."""
    path = path_of(entry)
    assert topology.edge(path[0])["src"] == "agent", label
    for before, after in zip(path, path[1:]):
        assert topology.edge(before)["dst"] == topology.edge(after)["src"], f"{label}: {before}→{after}"


def test_a_phase_one_call_never_visits_the_model():
    """It is a fast path: no Jev, no judgment policy, and no cost."""
    for decision in ("block", "allow"):
        path = path_of({"phase": 1, "decision": decision})
        assert not any("jev" in e or "judgment" in e for e in path)


def test_a_blocked_call_never_reaches_the_tool():
    path = path_of({"phase": 1, "decision": "block"})
    assert not any("upstream" in e for e in path)
    assert path[-1] == "gateway-agent"


def test_an_allowed_call_reaches_the_tool_and_comes_back():
    path = path_of({"phase": 1, "decision": "allow"})
    assert "gateway-upstream" in path and "upstream-gateway" in path


def test_an_escalation_stops_at_the_human():
    path = path_of({"phase": 2, "decision": "escalate"})
    assert path[-1] == "judgment-human"
    assert not any("upstream" in e for e in path)


def test_only_a_gray_call_reaches_the_judgment_model():
    for decision in ("allow", "escalate"):
        path = path_of({"phase": 2, "decision": decision})
        assert "gate-jev" in path and "jev-judgment" in path


def test_the_side_branches_are_exactly_the_two_stores():
    """TraceLog and DecisionRecord are written by the engine, not travelled through: they hang off
    the diagram as side branches. Anything else with no path is a missing journey, not a design."""
    reachable = {topology.edge(e)["dst"] for entry, _ in FOUR_SHAPES for e in path_of(entry)}
    assert {n["id"] for n in NODES} - reachable == {"trace", "record"}


def test_every_component_can_be_opened_by_a_click():
    """Clicking a component must never come up empty — which is what a side branch would do if the
    page only ever looked at a call's path. For those, the boundary they own must still be one some
    shape of call produces."""
    from toolgate.interfaces.viewer import stages_of

    reachable, boundaries = set(), set()
    for entry, _ in FOUR_SHAPES:
        reachable |= {topology.edge(e)["dst"] for e in path_of(entry)}
        boundaries |= {s["boundary"] for s in stages_of(entry)}

    unreachable = [n["id"] for n in NODES
                   if n["id"] not in reachable and BOUNDARY_OF[n["id"]] not in boundaries]
    assert unreachable == []


# --- stepping: what each arrival reveals ----------------------------------------

def test_every_edge_says_what_arriving_along_it_reveals():
    assert {e["id"] for e in EDGES} == set(OPENS)


def test_every_step_opens_a_boundary_that_has_something_to_show():
    """Stepping only helps if every step has a message. If an edge opened a boundary no call shape
    ever produces, that step would land on an empty panel with nothing to say why."""
    from toolgate.interfaces.viewer import stages_of

    shown = set()
    for entry, _ in FOUR_SHAPES:
        shown |= {s["boundary"] for s in stages_of(entry)}
    assert set(OPENS.values()) <= shown


def test_a_return_hop_does_not_open_the_destination_boundary():
    """The mistake this map exists to prevent: on a return hop, reading the destination's own
    boundary would show `ingress` when the message coming back is the decision or the verdict."""
    for edge_id in ("gate-gateway", "judgment-gateway", "gateway-agent", "upstream-gateway"):
        assert OPENS[edge_id] != BOUNDARY_OF[topology.edge(edge_id)["dst"]], edge_id


@pytest.mark.parametrize("entry,label", FOUR_SHAPES, ids=[s[1] for s in FOUR_SHAPES])
def test_a_call_steps_through_the_messages_we_designed(entry, label):
    assert [OPENS[e] for e in path_of(entry)] == STEPS[label]


def test_no_step_is_asked_for_a_boundary_the_call_never_reaches():
    """The page maps a boundary back to a step when you click a chip or a component. Every stage of
    every shape must be findable, or that click would silently do nothing."""
    from toolgate.interfaces.viewer import stages_of

    for entry, label in FOUR_SHAPES:
        opened = {OPENS[e] for e in path_of(entry)}
        for stage in stages_of(entry):
            assert stage["boundary"] in opened, f"{label}: {stage['boundary']} has no step"


# --- the checker itself, on purpose-broken layouts --------------------------------

def test_it_catches_two_boxes_sitting_on_top_of_each_other(swap):
    swap(nodes=with_node("agent", x=260))
    assert any("overlap" in p for p in layout_problems())


def test_it_catches_an_arrow_starting_in_empty_space(swap):
    swap(edges=with_edge("agent-gateway", points=((240, 162), (250, 162))))
    assert any("does not start on agent" in p for p in layout_problems())


def test_it_catches_an_arrow_that_misses_the_box_it_points_at(swap):
    swap(edges=with_edge("engine-trace", dst="record"))
    assert any("does not end on record" in p for p in layout_problems())


def test_it_catches_an_arrow_drawn_through_an_unrelated_box(swap):
    """The classic diagram bug: a line straight through a component it has nothing to do with."""
    swap(edges=EDGES + ({"id": "gate-human", "src": "gate", "dst": "human", "label": "x",
                         "points": ((776, 204), (776, 470), (116, 470), (116, 444))},))
    assert any("drawn through record" in p for p in layout_problems())


def test_it_catches_a_label_wider_than_its_box(swap):
    swap(nodes=with_node("engine", label="EngineThatKeepsGoingOnAndOnForever"))
    assert any("wider than its box" in p for p in layout_problems())


def test_it_catches_a_waypoint_off_the_canvas(swap):
    swap(edges=with_edge("engine-trace", points=((556, 204), (556, 900))))
    assert any("outside the viewBox" in p for p in layout_problems())
