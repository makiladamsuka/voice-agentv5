"""
Campus Wayfinding Engine
========================
Loads the 3D map graph built by the Map Builder, constructs an adjacency graph,
and uses Dijkstra's algorithm to find the shortest path between any two rooms.
Generates human-readable turn-by-turn directions.
"""

import json
import math
import heapq
import glob
from pathlib import Path
from difflib import SequenceMatcher


class Wayfinder:
    """Pathfinding engine that reads map_graph_floor_*.json files."""

    # The robot's default starting position
    ROBOT_LOCATION = "front desk"

    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = str(Path(__file__).resolve().parent.parent / "data")
        self.data_dir = Path(data_dir)

        # All floors: { "floor_1": { nodes: [...], edges: [...], buildings: {...} } }
        self.floors: dict = {}
        # Merged flat lookups
        self.nodes: dict = {}       # node_id -> node dict (with world coords added)
        self.edges: list = []
        self.buildings: dict = {}   # building_id -> building dict
        self.graph: dict = {}       # adjacency list: node_id -> [(neighbor_id, distance)]

        self.load_all_floors()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_all_floors(self):
        """Scan data dir for all map_graph_floor_*.json and load them."""
        pattern = str(self.data_dir / "map_graph_floor_*.json")
        files = sorted(glob.glob(pattern))

        if not files:
            print("⚠️  Wayfinder: No map files found in", self.data_dir)
            return

        for fpath in files:
            floor_name = Path(fpath).stem.replace("map_graph_", "")
            try:
                with open(fpath, "r") as f:
                    data = json.load(f)
                self.floors[floor_name] = data
                print(f"📍 Wayfinder: Loaded {floor_name} ({len(data.get('nodes', []))} nodes, {len(data.get('edges', []))} edges)")
            except Exception as e:
                print(f"⚠️  Wayfinder: Failed to load {fpath}: {e}")

        self._build_graph()

    def reload(self):
        """Reload all map data from disk (call after map edits)."""
        self.floors.clear()
        self.nodes.clear()
        self.edges.clear()
        self.buildings.clear()
        self.graph.clear()
        self.load_all_floors()

    # ------------------------------------------------------------------
    # Graph Construction
    # ------------------------------------------------------------------

    def _build_graph(self):
        """Build the adjacency list from all loaded floors."""
        self.graph = {}

        for floor_name, data in self.floors.items():
            buildings = data.get("buildings", {})
            self.buildings.update(buildings)

            for node in data.get("nodes", []):
                nid = node["id"]
                # Compute world position
                building = buildings.get(node["building"], {})
                bpos = building.get("position", [0, 0, 0])
                world_x = bpos[0] + node["x"]
                world_z = bpos[2] + node["z"]
                # Extract floor number (e.g. 'floor_2' -> 2)
                world_y = (0.1 if node.get("type") == "waypoint" else (node.get("size", [1, 1, 1])[1] / 2))

                node_entry = {
                    **node,
                    "floor": floor_name,
                    "world": [world_x, world_y, world_z],
                }
                self.nodes[nid] = node_entry
                if nid not in self.graph:
                    self.graph[nid] = []

            for edge in data.get("edges", []):
                src, tgt = edge["source"], edge["target"]
                if src in self.nodes and tgt in self.nodes:
                    dist = self._distance(self.nodes[src]["world"], self.nodes[tgt]["world"])
                    self.graph.setdefault(src, []).append((tgt, dist))
                    self.graph.setdefault(tgt, []).append((src, dist))
                    self.edges.append(edge)

        # Auto-link Staircases across floors
        staircases = []
        for nid, node in self.nodes.items():
            if node.get("label", "").lower() == "staircase":
                staircases.append(node)

        # Connect staircases that are in the same building and roughly at the same (X, Z)
        staircase_edges = 0
        for i in range(len(staircases)):
            for j in range(i + 1, len(staircases)):
                s1 = staircases[i]
                s2 = staircases[j]
                
                # Only connect if they are on different floors
                if s1["floor"] != s2["floor"]:
                    # Must be in the same building
                    if s1.get("building") == s2.get("building"):
                        # Calculate 2D distance (ignore Y)
                        dx = s1["world"][0] - s2["world"][0]
                        dz = s1["world"][2] - s2["world"][2]
                        dist_2d = math.sqrt(dx**2 + dz**2)
                        
                        # If they are vertically aligned (within 5 meters)
                        if dist_2d < 5.0:
                            # Add an edge (with a 10.0 distance penalty for taking stairs)
                            penalty = 10.0
                            self.graph.setdefault(s1["id"], []).append((s2["id"], penalty))
                            self.graph.setdefault(s2["id"], []).append((s1["id"], penalty))
                            staircase_edges += 1

        room_count = sum(1 for n in self.nodes.values() if n.get("type") != "waypoint")
        wp_count = sum(1 for n in self.nodes.values() if n.get("type") == "waypoint")
        print(f"✅ Wayfinder graph built: {room_count} rooms, {wp_count} waypoints, {len(self.edges)} edges, {staircase_edges} cross-floor links")

    @staticmethod
    def _distance(a: list, b: list) -> float:
        """Euclidean distance between two 3D points."""
        return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))

    # ------------------------------------------------------------------
    # Room Lookup (fuzzy matching)
    # ------------------------------------------------------------------

    def find_room(self, query: str) -> dict | None:
        """Find a room node by fuzzy label matching. Returns the node dict or None."""
        query_lower = query.lower().strip()

        # 1. Exact match
        for node in self.nodes.values():
            if node.get("type") == "waypoint":
                continue
            if node["label"].lower() == query_lower:
                return node

        # 2. Substring match
        for node in self.nodes.values():
            if node.get("type") == "waypoint":
                continue
            if query_lower in node["label"].lower() or node["label"].lower() in query_lower:
                return node

        # 3. Fuzzy match (SequenceMatcher)
        best_match = None
        best_ratio = 0.0
        for node in self.nodes.values():
            if node.get("type") == "waypoint":
                continue
            ratio = SequenceMatcher(None, query_lower, node["label"].lower()).ratio()
            if ratio > best_ratio and ratio > 0.5:
                best_ratio = ratio
                best_match = node

        return best_match

    def list_rooms(self) -> list[str]:
        """Return a list of all room labels (excluding waypoints)."""
        return [n["label"] for n in self.nodes.values() if n.get("type") != "waypoint"]

    # ------------------------------------------------------------------
    # Dijkstra's Algorithm
    # ------------------------------------------------------------------

    def _dijkstra(self, start_id: str, end_id: str) -> list[str] | None:
        """Find shortest path between two node IDs. Returns list of node IDs or None."""
        if start_id not in self.graph or end_id not in self.graph:
            return None

        dist = {start_id: 0.0}
        prev = {start_id: None}
        heap = [(0.0, start_id)]
        visited = set()

        while heap:
            d, u = heapq.heappop(heap)
            if u in visited:
                continue
            visited.add(u)

            if u == end_id:
                # Reconstruct path
                path = []
                node = end_id
                while node is not None:
                    path.append(node)
                    node = prev[node]
                return list(reversed(path))

            for neighbor, weight in self.graph.get(u, []):
                if neighbor in visited:
                    continue
                new_dist = d + weight
                if new_dist < dist.get(neighbor, float("inf")):
                    dist[neighbor] = new_dist
                    prev[neighbor] = u
                    heapq.heappush(heap, (new_dist, neighbor))

        return None  # No path found

    # ------------------------------------------------------------------
    # Direction Generation
    # ------------------------------------------------------------------

    def _compute_angle(self, p1: list, p2: list, p3: list) -> float:
        """Compute the signed turn angle at p2 (in degrees).
        Positive = right turn, Negative = left turn."""
        # Vectors: v1 = p1->p2,  v2 = p2->p3
        v1x, v1z = p2[0] - p1[0], p2[2] - p1[2]
        v2x, v2z = p3[0] - p2[0], p3[2] - p2[2]

        # Cross product (2D) gives sign of turn
        cross = v1x * v2z - v1z * v2x
        # Dot product for angle magnitude
        dot = v1x * v2x + v1z * v2z
        mag1 = math.sqrt(v1x ** 2 + v1z ** 2)
        mag2 = math.sqrt(v2x ** 2 + v2z ** 2)

        if mag1 == 0 or mag2 == 0:
            return 0.0

        cos_angle = max(-1, min(1, dot / (mag1 * mag2)))
        angle_deg = math.degrees(math.acos(cos_angle))

        # Sign: positive = right, negative = left
        return angle_deg if cross > 0 else -angle_deg

    def _generate_directions(self, path_ids: list[str], destination_label: str) -> str:
        """Convert a sequence of node IDs into human-readable directions."""
        if len(path_ids) < 2:
            return f"You are already at {destination_label}!"

        steps = []
        prev_building = None
        prev_floor = None

        for i in range(len(path_ids)):
            node = self.nodes[path_ids[i]]
            current_building = node.get("building", "")
            current_floor = node.get("floor", "floor_1")

            # Announce starting floor
            if i == 0:
                floor_num = current_floor.replace("floor_", "")
                steps.append(f"Starting on Floor {floor_num}")

            # Detect floor change
            if prev_floor and current_floor != prev_floor:
                floor_num = current_floor.replace("floor_", "")
                steps.append(f"Take the stairs to Floor {floor_num}")

            # Detect building change
            elif prev_building and current_building != prev_building:
                bname_old = self.buildings.get(prev_building, {}).get("name", prev_building)
                bname_new = self.buildings.get(current_building, {}).get("name", current_building)
                steps.append(f"Walk from {bname_old} to {bname_new}")

            prev_building = current_building
            prev_floor = current_floor

            # Turn detection (need 3 consecutive points)
            if 0 < i < len(path_ids) - 1:
                p1 = self.nodes[path_ids[i - 1]]["world"]
                p2 = self.nodes[path_ids[i]]["world"]
                p3 = self.nodes[path_ids[i + 1]]["world"]

                angle = self._compute_angle(p1, p2, p3)

                if abs(angle) < 30:
                    pass  # Continue straight — no instruction needed
                elif angle < -30:
                    steps.append("Turn left")
                elif angle > 30:
                    steps.append("Turn right")

        # Final instruction
        steps.append(f"You have arrived at {destination_label}")

        # Clean up: merge consecutive "continue straight"
        if not steps:
            steps = [f"Walk straight ahead to reach {destination_label}"]

        return ". ".join(steps) + "."

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_path(self, destination: str, start: str = None) -> dict | None:
        """
        Find the shortest path from start to destination.

        Args:
            destination: Room label to navigate to (e.g. "Dean Office")
            start: Starting room label. Defaults to ROBOT_LOCATION ("front desk").

        Returns:
            dict with keys: path_coords, directions, floor, destination, nodes, buildings
            or None if no path found.
        """
        start_label = start or self.ROBOT_LOCATION

        # Resolve room names
        start_node = self.find_room(start_label)
        end_node = self.find_room(destination)

        if not start_node:
            available = ", ".join(self.list_rooms())
            return {
                "error": f"I couldn't find a room called '{start_label}'. Available rooms: {available}"
            }

        if not end_node:
            available = ", ".join(self.list_rooms())
            return {
                "error": f"I couldn't find a room called '{destination}'. Available rooms: {available}"
            }

        if start_node["id"] == end_node["id"]:
            return {
                "error": f"You are already at {end_node['label']}!",
                "path_coords": [start_node["world"]],
                "directions": f"You are already at {end_node['label']}!",
            }

        # Run Dijkstra
        path_ids = self._dijkstra(start_node["id"], end_node["id"])

        if not path_ids:
            return {
                "error": f"I'm sorry, I couldn't find a connected path from '{start_node['label']}' to '{end_node['label']}'. "
                         f"The rooms might not be linked by paths in the map."
            }

        # Build result
        path_coords = [self.nodes[nid]["world"] for nid in path_ids]
        directions = self._generate_directions(path_ids, end_node["label"])

        return {
            "path_coords": path_coords,
            "directions": directions,
            "floor": end_node.get("floor", "floor_1"),
            "destination": end_node["label"],
            "start": start_node["label"],
            # Include full map data so frontend can render everything
            "nodes": [self.nodes[nid] for nid in self.nodes],
            "buildings": self.buildings,
        }


# ------------------------------------------------------------------
# Standalone test
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Wayfinder Standalone Test")
    print("=" * 60)

    wf = Wayfinder()

    print(f"\n📋 Available rooms: {wf.list_rooms()}")

    # Test: find path from front desk to dean office
    for dest in wf.list_rooms():
        print(f"\n🔍 Finding path to '{dest}'...")
        result = wf.find_path(dest)
        if result and "error" not in result:
            print(f"   ✅ Path found! {len(result['path_coords'])} waypoints")
            print(f"   📢 Directions: {result['directions']}")
        elif result:
            print(f"   ⚠️  {result['error']}")
        else:
            print(f"   ❌ No result")
