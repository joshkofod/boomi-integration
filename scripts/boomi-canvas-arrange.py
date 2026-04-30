#!/usr/bin/env python3
"""
Boomi Process Canvas Arranger

Validates step-path integrity and arranges shape layout in Boomi process XML.
Reads a process component XML file, checks for broken connections and orphaned
shapes, then repositions shapes for clean visual layout in the Boomi GUI.

Usage:
    python3 boomi-canvas-arrange.py <process-xml-file> [--dry-run] [--no-layout]

Options:
    --dry-run     Report issues without modifying the file
    --no-layout   Fix integrity issues only, don't rearrange layout

Exit codes:
    0   Clean — no integrity issues found
    1   Issues found (reported to stdout/stderr)
    2   Error (bad file, parse failure, etc.)
"""

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set

# ── Constants ────────────────────────────────────────────────────────────────

BNS = "http://api.platform.boomi.com/"
NS = {"bns": BNS}

# Layout spacing
H_SPACING = 224.0        # horizontal gap between sequential shapes (225 is recommended)
V_SPACING = 160.0        # vertical gap between main branches
V_SUB_SPACING = 112.0    # vertical offset for sub-branches
START_X = 48.0
START_Y = 46.0
DP_OFFSET_X = 176.0      # dragpoint x offset from shape
DP_OFFSET_Y = 10.0       # dragpoint y offset from shape center

# Terminal shape types — no outbound path required
TERMINAL_TYPES = {"stop", "returndocuments", "exception"}

# Shapes that can have multiple outputs (branches)
BRANCH_TYPES = {"decision", "route", "trycatch"}


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Dragpoint:
    name: str
    to_shape: str
    identifier: str = ""
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    element: ET.Element = field(default=None, repr=False)


@dataclass
class Shape:
    name: str
    shapetype: str
    userlabel: str = ""
    x: float = 0.0
    y: float = 0.0
    element: ET.Element = field(default=None, repr=False)
    dragpoints: list = field(default_factory=list)

    @property
    def is_terminal(self):
        return self.shapetype in TERMINAL_TYPES

    @property
    def is_branch(self):
        return self.shapetype in BRANCH_TYPES

    @property
    def has_outbound(self):
        return any(dp.to_shape and dp.to_shape != "unset" for dp in self.dragpoints)


@dataclass
class IntegrityIssue:
    severity: str       # "error" | "warning"
    category: str       # "orphan" | "broken_path" | "unset_toShape" | "no_outbound"
    shape_name: str
    message: str


# ── XML Parsing ──────────────────────────────────────────────────────────────

def parse_process_xml(filepath: str) -> tuple[ET.ElementTree, dict[str, Shape]]:
    """Parse Boomi process XML and extract shape graph."""
    # Register namespace prefix so ET.write() uses 'bns:' not 'ns0:'
    ET.register_namespace("bns", BNS)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")

    tree = ET.parse(filepath)
    root = tree.getroot()

    # Find the <shapes> element (may be under bns:object/process/shapes)
    shapes_elem = None
    for elem in root.iter(f"{{{BNS}}}shapes"):
        shapes_elem = elem
        break
    if shapes_elem is None:
        # Try without namespace
        for elem in root.iter("shapes"):
            shapes_elem = elem
            break
    if shapes_elem is None:
        raise ValueError("No <shapes> element found in XML")

    shapes = {}
    for shape_elem in shapes_elem:
        # Skip non-shape elements
        tag = shape_elem.tag.replace(f"{{{BNS}}}", "")
        if tag != "shape":
            continue

        name = shape_elem.get("name", "")
        if not name:
            continue

        shapetype = shape_elem.get("shapetype", "")
        userlabel = shape_elem.get("userlabel", "")
        x = float(shape_elem.get("x", "0"))
        y = float(shape_elem.get("y", "0"))

        shape = Shape(
            name=name,
            shapetype=shapetype,
            userlabel=userlabel,
            x=x,
            y=y,
            element=shape_elem,
        )

        # Parse dragpoints
        for dp_elem in shape_elem.iter(f"{{{BNS}}}dragpoint"):
            dp = Dragpoint(
                name=dp_elem.get("name", ""),
                to_shape=dp_elem.get("toShape", ""),
                identifier=dp_elem.get("identifier", ""),
                text=dp_elem.get("text", ""),
                x=float(dp_elem.get("x", "0")),
                y=float(dp_elem.get("y", "0")),
                element=dp_elem,
            )
            shape.dragpoints.append(dp)

        # Also try without namespace
        if not shape.dragpoints:
            for dp_elem in shape_elem.iter("dragpoint"):
                dp = Dragpoint(
                    name=dp_elem.get("name", ""),
                    to_shape=dp_elem.get("toShape", ""),
                    identifier=dp_elem.get("identifier", ""),
                    text=dp_elem.get("text", ""),
                    x=float(dp_elem.get("x", "0")),
                    y=float(dp_elem.get("y", "0")),
                    element=dp_elem,
                )
                shape.dragpoints.append(dp)

        shapes[name] = shape

    return tree, shapes


# ── Integrity Checks ─────────────────────────────────────────────────────────

def check_integrity(shapes: dict[str, Shape]) -> list[IntegrityIssue]:
    """Check step-path integrity. Returns list of issues."""
    issues = []

    # Find start shape
    start_shapes = [s for s in shapes.values() if s.shapetype == "start"]
    if not start_shapes:
        issues.append(IntegrityIssue(
            "error", "no_start", "", "No start shape found in process"
        ))
        return issues

    # Build inbound map: shape_name → set of shapes that point TO it
    inbound = defaultdict(set)
    for shape in shapes.values():
        for dp in shape.dragpoints:
            if dp.to_shape and dp.to_shape != "unset" and dp.to_shape in shapes:
                inbound[dp.to_shape].add(shape.name)

    # BFS from start to find reachable shapes
    reachable = set()
    queue = deque([start_shapes[0].name])
    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        shape = shapes[current]
        for dp in shape.dragpoints:
            if dp.to_shape and dp.to_shape != "unset" and dp.to_shape in shapes:
                if dp.to_shape not in reachable:
                    queue.append(dp.to_shape)

    # Check each shape
    for shape in shapes.values():
        # 1. Orphaned shapes (not reachable from start)
        if shape.name not in reachable and shape.shapetype != "start":
            issues.append(IntegrityIssue(
                "warning", "orphan", shape.name,
                f"Shape '{shape.name}' ({shape.shapetype}) is not reachable from start"
            ))

        # 2. Non-terminal shapes with no outbound path
        if not shape.is_terminal and not shape.has_outbound:
            # Process calls with no return path are OK
            if shape.shapetype == "processcall":
                has_return = any(dp.identifier for dp in shape.dragpoints)
                if not has_return:
                    continue
            issues.append(IntegrityIssue(
                "warning", "no_outbound", shape.name,
                f"Shape '{shape.name}' ({shape.shapetype}) has no outbound connections"
            ))

        # 3. Unset toShape on dragpoints
        for dp in shape.dragpoints:
            if dp.to_shape == "unset":
                issues.append(IntegrityIssue(
                    "warning", "unset_toShape", shape.name,
                    f"Shape '{shape.name}' has dragpoint '{dp.name}' with toShape='unset'"
                ))

        # 4. Dragpoint pointing to non-existent shape
        for dp in shape.dragpoints:
            if dp.to_shape and dp.to_shape != "unset" and dp.to_shape not in shapes:
                issues.append(IntegrityIssue(
                    "error", "broken_path", shape.name,
                    f"Shape '{shape.name}' dragpoint '{dp.name}' points to missing shape '{dp.to_shape}'"
                ))

    return issues


# ── Layout Algorithm ─────────────────────────────────────────────────────────

def compute_layout(shapes: dict[str, Shape]) -> dict[str, tuple[float, float]]:
    """Compute new x,y positions for all shapes. Returns {shape_name: (x, y)}."""
    if not shapes:
        return {}

    # Find start shape
    start_shapes = [s for s in shapes.values() if s.shapetype == "start"]
    if not start_shapes:
        return {}

    start = start_shapes[0]

    # Build adjacency: shape → list of target shapes (in dragpoint order)
    adjacency: dict[str, list[str]] = {}
    for shape in shapes.values():
        targets = []
        for dp in shape.dragpoints:
            if dp.to_shape and dp.to_shape != "unset" and dp.to_shape in shapes:
                targets.append(dp.to_shape)
        adjacency[shape.name] = targets

    # Build reverse adjacency: shape → list of source shapes
    reverse_adj: dict[str, list[str]] = defaultdict(list)
    for src, targets in adjacency.items():
        for tgt in targets:
            reverse_adj[tgt].append(src)

    # ── Step 1: Assign layers (x position) via BFS ──
    layers: dict[str, int] = {}
    queue = deque([start.name])
    layers[start.name] = 0
    
    # Processed counter to detect infinite loops on malformed cycles
    processed_count = 0
    max_safe_iter = len(shapes) * 2

    while queue and processed_count < max_safe_iter:
        current = queue.popleft()
        processed_count += 1
        current_layer = layers[current]
        for target in adjacency.get(current, []):
            new_layer = current_layer + 1
            # If target already has a layer, take the max (merge point)
            if target in layers:
                if new_layer > layers[target]:
                    layers[target] = new_layer
                    # Re-process downstream of merge points
                    queue.append(target)
            else:
                layers[target] = new_layer
                queue.append(target)

    # Assign layer 0 to any unreached shapes (orphans)
    # Find reachable shapes from start via BFS
    reachable_set = set()
    rq = deque([start.name])
    while rq:
        cur = rq.popleft()
        if cur in reachable_set:
            continue
        reachable_set.add(cur)
        for t in adjacency.get(cur, []):
            if t not in reachable_set:
                rq.append(t)

    orphan_shapes = [n for n in shapes if n not in reachable_set and shapes[n].shapetype != "start"]
    for name in shapes:
        if name not in layers:
            layers[name] = 0

    # ── Step 2: Assign branch tracks (y position) ──
    # Track assignment: main flow = 0, branches get incrementing track numbers
    tracks: dict[str, int] = {}  # shape_name → track index
    track_depth: dict[int, float] = {}  # track → accumulated height needed

    def assign_tracks(shape_name: str, track: int, depth: float, visited_path: Set[str]):
        """DFS to assign tracks, branching at decision/route shapes."""
        if shape_name in visited_path:
            return # Cycle protection
            
        if shape_name in tracks:
            return  # Already assigned (merge point)
            
        tracks[shape_name] = track
        track_depth[track] = max(track_depth.get(track, 0), depth)

        shape = shapes[shape_name]
        targets = adjacency.get(shape_name, [])

        # Create new path set for this branch
        new_path = visited_path | {shape_name}

        if len(targets) <= 1:
            # Single output — continue on same track
            for t in targets:
                assign_tracks(t, track, depth, new_path)
        else:
            # Multiple outputs (branch) — assign sub-tracks
            # First target stays on current track (main path)
            # Additional targets get new tracks below
            next_track_base = max(track_depth.keys(), default=0) + 1
            for i, target in enumerate(targets):
                if i == 0:
                    # Main path continues on same track
                    assign_tracks(target, track, depth, new_path)
                else:
                    # Branch — new track
                    branch_track = next_track_base + i - 1
                    branch_depth = depth + (i * V_SPACING)
                    assign_tracks(target, branch_track, branch_depth, new_path)

    assign_tracks(start.name, 0, 0.0, set())

    # ── Step 3: Compute final x,y coordinates ──
    positions: dict[str, tuple[float, float]] = {}

    # Sort shapes by layer for deterministic output
    sorted_shapes = sorted(shapes.keys(), key=lambda n: (layers[n], tracks.get(n, 0), n))

    # Track y-offsets
    track_y: dict[int, float] = {}
    unique_tracks = sorted(set(tracks.values()))
    for i, track_id in enumerate(unique_tracks):
        if i == 0:
            track_y[track_id] = START_Y
        else:
            # Check previous track depth to avoid overlaps if tracks are uneven
            track_y[track_id] = START_Y + (i * V_SPACING)

    for name in sorted_shapes:
        layer = layers[name]
        track = tracks.get(name, 0)
        x = START_X + (layer * H_SPACING)
        y = track_y.get(track, START_Y)
        positions[name] = (x, y)

    # ── Step 4: Place orphaned shapes below main flow ──
    if orphan_shapes:
        max_main_y = max((y for _, (_, y) in positions.items()), default=START_Y)
        orphan_y_start = max_main_y + V_SPACING * 1.5
        for i, name in enumerate(orphan_shapes):
            row = i // 5
            col = i % 5
            positions[name] = (START_X + (col * H_SPACING), orphan_y_start + (row * V_SPACING))

    # ── Step 5: Adjust merge points ──
    # Merge points (shapes with multiple inbound paths) should be positioned
    # between their sources if possible
    for name, shape in shapes.items():
        sources = reverse_adj.get(name, [])
        if len(sources) > 1:
            # This is a merge point — find the tracks of its sources
            source_y_values = [positions.get(s, (0, START_Y))[1] for s in sources]
            source_y_values.sort()
            
            # Position at the vertical midpoint of sources
            mid_y = sum(source_y_values) / len(source_y_values)
            
            # Snap to 8-unit grid (Boomi preference)
            mid_y = round(mid_y / 8) * 8
            
            # Push x right of ALL sources
            max_source_x = max(positions.get(s, (0, 0))[0] for s in sources)
            merge_x = max_source_x + H_SPACING
            
            # Update layer if we pushed it significantly
            new_layer = int((merge_x - START_X) / H_SPACING)
            layers[name] = max(layers[name], new_layer)
            
            positions[name] = (merge_x, mid_y)

    return positions


# ── Apply Changes ────────────────────────────────────────────────────────────

def apply_positions(shapes: dict[str, Shape], positions: dict[str, tuple[float, float]]):
    """Update shape x/y attributes in the XML. Also updates dragpoint positions."""
    for name, (x, y) in positions.items():
        shape = shapes.get(name)
        if not shape or not shape.element:
            continue

        shape.element.set("x", f"{x:.1f}")
        shape.element.set("y", f"{y:.1f}")

        # Update dragpoint positions to follow the shape
        # Standard Boomi layout places dragpoints on the right edge
        for dp in shape.dragpoints:
            if dp.element is not None:
                # Base offset for standard shapes
                dp_x = x + DP_OFFSET_X
                dp_y = y + DP_OFFSET_Y
                
                # If there are multiple dragpoints (Decision/Route), offset them vertically
                if len(shape.dragpoints) > 1:
                    try:
                        # Identifiers are usually '1', '2', etc.
                        # We spread them by 24 units vertically
                        idx = int(dp.identifier) - 1 if dp.identifier.isdigit() else 0
                        dp_y += (idx * 24.0)
                    except:
                        pass
                
                dp.element.set("x", f"{dp_x:.1f}")
                dp.element.set("y", f"{dp_y:.1f}")


# ── Report ───────────────────────────────────────────────────────────────────

def print_report(issues: list[IntegrityIssue], shapes: dict[str, Shape],
                 positions: Optional[dict[str, tuple[float, float]]]):
    """Print integrity and layout report."""
    print("=" * 60)
    print("BOOMI CANVAS ARRANGER — REPORT")
    print("=" * 60)

    # Integrity
    print(f"\n📋 Shapes: {len(shapes)}")

    if not issues:
        print("\n✅ Step-path integrity: CLEAN — no issues found")
    else:
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]

        if errors:
            print(f"\n❌ Errors ({len(errors)}):")
            for issue in errors:
                print(f"   • {issue.message}")

        if warnings:
            print(f"\n⚠️  Warnings ({len(warnings)}):")
            for issue in warnings:
                print(f"   • {issue.message}")

    # Layout summary
    if positions:
        moved = 0
        for name, (new_x, new_y) in positions.items():
            shape = shapes.get(name)
            if shape and (abs(shape.x - new_x) > 1 or abs(shape.y - new_y) > 1):
                moved += 1

        if moved > 0:
            print(f"\n📐 Layout: {moved} shapes repositioned")
        else:
            print(f"\n📐 Layout: already clean, no changes needed")

        # Show final positions (sorted by coordinates for readable list)
        print("\n   Shape positions:")
        pos_list = sorted(positions.keys(), key=lambda n: (positions[n][1], positions[n][0]))
        for name in pos_list:
            x, y = positions[name]
            shape = shapes[name]
            label = f" ({shape.userlabel})" if shape.userlabel else ""
            marker = ""
            old_x, old_y = shape.x, shape.y
            if abs(old_x - x) > 1 or abs(old_y - y) > 1:
                marker = " ← moved"
            print(f"   {name:15s} [{shape.shapetype:14s}] x={x:7.1f} y={y:7.1f}{label}{marker}")

    print("\n" + "=" * 60)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    filepath = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    no_layout = "--no-layout" in sys.argv

    path = Path(filepath)
    if not path.exists():
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        sys.exit(2)

    # Parse
    try:
        tree, shapes = parse_process_xml(filepath)
    except Exception as e:
        print(f"Error parsing XML: {e}", file=sys.stderr)
        sys.exit(2)

    if not shapes:
        print("Warning: No shapes found in process", file=sys.stderr)
        sys.exit(0)

    # Integrity check
    issues = check_integrity(shapes)

    # Layout
    positions = None
    if not no_layout:
        positions = compute_layout(shapes)

    # Report
    print_report(issues, shapes, positions)

    # Apply changes
    if not dry_run:
        if positions:
            apply_positions(shapes, positions)
        
        # Write back
        # Note: xml_declaration=True and encoding="UTF-8" are important for Boomi
        tree.write(filepath, xml_declaration=True, encoding="UTF-8")
        print(f"\n💾 Updated: {filepath}")
    else:
        print(f"\n🔍 Dry run — no changes written")

    # Exit code
    has_errors = any(i.severity == "error" for i in issues)
    sys.exit(1 if has_errors or issues else 0)


if __name__ == "__main__":
    main()
