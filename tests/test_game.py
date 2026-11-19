"""Physics / rules tests for server/game.py, including a scripted 2-player
solution for every level (proves that each level can actually be finished)."""
import unittest

from server.game import TILE, World, overlaps
from server.levels import LEVELS


def make_world(rows, n=2, **opts):
    level = {"name": "test", "map": rows}
    level.update(opts)
    return World(level, [(f"p{i + 1}", i) for i in range(n)])


class Driver:
    """Small helper that 'plays' the game by setting inputs and stepping."""

    def __init__(self, world: World):
        self.w = world

    def p(self, name):
        return self.w.by_name[name]

    def idle(self, *names):
        for n in names or self.w.by_name:
            self.w.set_input(n, False, False, False)

    def step(self, n=1):
        for _ in range(n):
            self.w.step()

    def walk_to(self, name, x, limit=900):
        for _ in range(limit):
            p = self.p(name)
            if abs(p.x - x) <= 1.7:
                break
            self.w.set_input(name, p.x > x, p.x < x, False)
            before = p.x
            self.w.step()
            if p.x == before and p.on_ground:
                break       # blocked
        self.w.set_input(name, False, False, False)
        self.settle(name)
        return self.p(name).x

    def settle(self, name, limit=200):
        for _ in range(limit):
            if self.p(name).on_ground:
                return
            self.w.step()

    def hold(self, name, left=False, right=False, jump=False, ticks=1, until=None):
        for _ in range(ticks):
            self.w.set_input(name, left, right, jump)
            self.w.step()
            if until and until():
                break
        self.w.set_input(name, False, False, False)

    def jump_right_until(self, name, cond, limit=120):
        """Jump while holding right; release right once cond() is true."""
        for _ in range(limit):
            self.w.set_input(name, False, not cond(), True)
            self.w.step()
            if cond():
                break
        self.w.set_input(name, False, False, True)
        self.settle(name)
        self.idle(name)
        self.step()

    def climb_onto(self, climber, base):
        b = self.p(base)
        self.jump_right_until(climber, lambda: self.p(climber).x >= b.x - 1)
        return self.p(climber).standing_on is b


class LevelDataTest(unittest.TestCase):
    def test_all_levels_parse_for_every_team_size(self):
        for i, level in enumerate(LEVELS):
            for n in (2, 3, 4):
                with self.subTest(level=i, players=n):
                    w = World(level, [(f"p{k}", k) for k in range(n)])
                    self.assertEqual((w.rows, w.cols), (16, 30))
                    self.assertEqual(len(w.players), n)
                    self.assertIsNotNone(w.door)
                    spawn_points = {p.spawn for p in w.players}
                    self.assertEqual(len(spawn_points), n, "players must not share a spawn")
                    for gate in w.gates:
                        self.assertTrue(any(b.letter in w.links[gate] for b in w.buttons),
                                        f"gate {gate} has no button")


class PhysicsTest(unittest.TestCase):
    def test_two_tile_step_is_jumpable_three_is_not(self):
        top, air, floor = "#" * 12, "#..........#", "#" * 12
        two = [top] + [air] * 12 + ["#.....######", "#.1...######", floor]
        three = [top] + [air] * 11 + ["#.....######"] * 2 + ["#.1...######", floor]
        for rows, should_reach in ((two, True), (three, False)):
            w = make_world(rows, n=1)
            d = Driver(w)
            d.walk_to("p1", 5 * TILE - 29)
            d.hold("p1", right=True, jump=True, ticks=50)
            d.settle("p1")
            on_top = d.p("p1").x > 6 * TILE
            self.assertEqual(on_top, should_reach)

    def test_standing_on_a_teammate_reaches_three_tiles(self):
        rows = ["#" * 12] + ["#..........#"] * 11 + [
            "#.....######",
            "#.....######",
            "#.1.2.######",
            "#" * 12,
        ]
        w = make_world(rows)
        d = Driver(w)
        d.walk_to("p2", 6 * TILE - 30)          # base right next to the wall
        d.walk_to("p1", 6 * TILE - 90)
        self.assertTrue(d.climb_onto("p1", "p2"))
        d.hold("p1", right=True, jump=True, ticks=40)
        d.settle("p1")
        self.assertGreater(d.p("p1").x, 6 * TILE)
        self.assertAlmostEqual(d.p("p1").y + d.p("p1").h, 12 * TILE, places=3)

    def test_rider_moves_with_the_player_below(self):
        rows = ["#" * 20] + ["#" + "." * 18 + "#"] * 13 + ["#.1." + "." * 15 + "#", "#" * 20]
        w = make_world(rows)
        w.players[1].x, w.players[1].y = w.players[0].x, w.players[0].y - 40
        d = Driver(w)
        d.settle("p2")
        self.assertIs(d.p("p2").standing_on, d.p("p1"))
        start = d.p("p2").x
        d.hold("p1", right=True, ticks=20)
        self.assertAlmostEqual(d.p("p2").x - start, d.p("p1").x - w.players[0].spawn[0], places=3)

    def test_heavy_box_needs_every_player(self):
        rows = ["#" * 20] + ["#" + "." * 18 + "#"] * 12 + [
            "#......O...........#",
            "#.1.2..............#",
            "#" * 20,
        ]
        w = make_world(rows)
        d = Driver(w)
        box = w.boxes[0]
        self.assertEqual(box.weight, 2)
        d.walk_to("p2", box.x - 28)
        start = box.x
        d.hold("p2", right=True, ticks=30)
        self.assertEqual(box.x, start, "one player alone must not move the heavy box")
        d.walk_to("p1", d.p("p2").x - 28)
        for _ in range(30):
            w.set_input("p1", False, True, False)
            w.set_input("p2", False, True, False)
            w.step()
        self.assertGreater(box.x, start + 20)

    def test_button_opens_gate_and_closing_gate_ejects(self):
        rows = ["#" * 20] + ["#" + "." * 18 + "#"] * 12 + [
            "#..........A.......#",
            "#.1...a....A.......#",
            "#" * 20,
        ]
        w = make_world(rows, n=1)
        d = Driver(w)
        d.walk_to("p1", 6 * TILE + 2)
        d.step()
        self.assertTrue(w.gate_open["A"])
        d.walk_to("p1", 11 * TILE + 2)          # stand inside the open gate? it closes first
        self.assertFalse(w.gate_open["A"])
        p = d.p("p1")
        self.assertFalse(overlaps(*p.rect, 11 * TILE, 13 * TILE, TILE, 2 * TILE))

    def test_key_door_and_level_clear(self):
        rows = ["#" * 20] + ["#" + "." * 18 + "#"] * 13 + ["#1...K.........D...#", "#" * 20]
        w = make_world(rows, n=1)
        d = Driver(w)
        d.walk_to("p1", 17 * TILE)
        self.assertTrue(w.key_used)
        self.assertTrue(w.door_open)
        self.assertTrue(w.cleared)

    def test_spikes_respawn_and_return_the_key(self):
        rows = ["#" * 20] + ["#" + "." * 18 + "#"] * 13 + ["#1.K...^^......D...#", "#" * 20]
        w = make_world(rows, n=1)
        d = Driver(w)
        d.hold("p1", right=True, ticks=200, until=lambda: ("respawn", "p1") in w.events)
        p = d.p("p1")
        self.assertEqual((p.x, p.y), p.spawn)
        self.assertFalse(p.has_key)
        self.assertIsNone(w.key_holder)


class TeamSizeTest(unittest.TestCase):
    def test_heavy_box_with_four_players_needs_all_four(self):
        for pushers, should_move in ((3, False), (4, True)):
            w = World(LEVELS[2], [(f"p{i + 1}", i) for i in range(4)])
            box = w.boxes[0]
            self.assertEqual(box.weight, 4)
            start = box.x
            for _ in range(200):
                for i, p in enumerate(w.players):
                    w.set_input(p.name, False, i >= 4 - pushers, False)
                w.step()
            self.assertEqual(box.x > start + 10, should_move, f"{pushers} pushers")


class LevelSolutionTest(unittest.TestCase):
    """Scripted two-player solutions: every level must be beatable."""

    def world(self, idx, n=2):
        return World(LEVELS[idx], [(f"p{i + 1}", i) for i in range(n)])

    def finish(self, d: Driver, *names):
        for name in names:
            d.walk_to(name, d.w.door[0] + 2, limit=1500)
            self.assertTrue(d.p(name).entered, f"{name} should be through the door")

    def test_level_1(self):
        w = self.world(0)
        d = Driver(w)
        d.walk_to("p2", 12 * TILE - 30)          # base, left of the floating platform
        d.walk_to("p1", 9 * TILE)
        self.assertTrue(d.climb_onto("p1", "p2"))
        d.hold("p1", right=True, jump=True, ticks=30)
        d.walk_to("p1", 17 * TILE)
        self.assertTrue(d.p("p1").has_key)
        for name in ("p1", "p2"):
            d.walk_to(name, 22 * TILE - 30)
            d.hold(name, right=True, jump=True, ticks=40)
            d.walk_to(name, 27 * TILE + 2)
        self.assertTrue(w.cleared)

    def test_level_2(self):
        w = self.world(1)
        d = Driver(w)
        box = w.boxes[0]
        d.hold("p2", right=True, ticks=400, until=lambda: box.x >= 11 * TILE)
        d.step(5)
        self.assertTrue(w.gate_open["A"], "box + player on the 2-button opens the gate")
        d.walk_to("p1", d.p("p2").x - 40)
        d.jump_right_until("p1", lambda: d.p("p1").x > 12 * TILE + 4)
        d.walk_to("p1", 16 * TILE + 2)           # hold button b on the far side
        self.assertTrue(w.gate_open["A"])
        d.jump_right_until("p2", lambda: d.p("p2").x > 12 * TILE + 4)
        d.walk_to("p2", 14 * TILE + 10)
        d.jump_right_until("p2", lambda: d.p("p2").x > 17 * TILE + 4)
        d.walk_to("p2", 22 * TILE - 30)          # base next to the key shelf
        d.walk_to("p1", 20 * TILE)
        self.assertTrue(d.climb_onto("p1", "p2"))
        d.hold("p1", right=True, jump=True, ticks=30)
        d.walk_to("p1", 25 * TILE)
        self.assertTrue(d.p("p1").has_key)
        self.finish(d, "p1", "p2")
        self.assertTrue(w.cleared)

    def test_level_3(self):
        w = self.world(2)
        d = Driver(w)
        box = w.boxes[0]
        for _ in range(600):
            w.set_input("p1", False, True, False)
            w.set_input("p2", False, True, False)
            w.step()
            if box.on_ground and box.y >= 13 * TILE - 0.01:
                break
        d.idle()
        self.assertEqual((box.x, box.y), (19 * TILE, 13 * TILE), "heavy box fills the spike slot")
        for name in ("p2", "p1"):
            d.walk_to(name, 20 * TILE)
            d.hold(name, right=True, jump=True, ticks=30)
        d.walk_to("p2", 24 * TILE - 30)
        d.walk_to("p1", 22 * TILE)
        self.assertTrue(d.climb_onto("p1", "p2"))
        d.hold("p1", right=True, jump=True, ticks=30)
        d.walk_to("p1", 27 * TILE)
        self.assertTrue(d.p("p1").has_key)
        d.walk_to("p1", 28 * TILE + 2)
        d.walk_to("p2", 28 * TILE + 2)
        self.assertTrue(w.cleared)

    def test_level_4(self):
        w = self.world(3)
        d = Driver(w)
        d.walk_to("p2", 13 * TILE - 30)          # base next to the closed gate
        d.walk_to("p1", 11 * TILE)
        self.assertTrue(d.climb_onto("p1", "p2"))
        d.hold("p1", right=True, jump=True, ticks=40)
        d.walk_to("p1", 15 * TILE + 2)           # stand on button a
        self.assertTrue(w.gate_open["A"])
        d.walk_to("p2", 14 * TILE)
        d.jump_right_until("p2", lambda: d.p("p2").x > 16 * TILE + 4)
        d.walk_to("p2", 17 * TILE - 2)
        d.hold("p2", right=True, jump=True, ticks=40)   # over the spikes
        box = w.boxes[0]
        d.hold("p2", right=True, ticks=200, until=lambda: box.x >= 23 * TILE)
        self.assertGreaterEqual(box.x, 23 * TILE)
        d.walk_to("p1", 17 * TILE - 2)
        d.hold("p1", right=True, jump=True, ticks=40)
        d.settle("p1")
        self.assertFalse(w.gate_open["A"])
        d.jump_right_until("p2", lambda: d.p("p2").x >= box.x + 1)   # stand on the box
        self.assertIs(d.p("p2").standing_on, box)
        d.walk_to("p1", box.x - 40)
        self.assertTrue(d.climb_onto("p1", "p2"))
        d.hold("p1", right=True, jump=True, ticks=30)
        d.walk_to("p1", 27 * TILE)
        self.assertTrue(d.p("p1").has_key)
        d.walk_to("p1", 28 * TILE + 2)
        d.walk_to("p2", 28 * TILE + 2)
        self.assertTrue(w.cleared)

    def test_level_5(self):
        w = self.world(4)
        d = Driver(w)
        heavy, light = w.boxes
        for _ in range(600):
            w.set_input("p1", False, True, False)
            w.set_input("p2", False, True, False)
            w.step()
            if heavy.x >= 13 * TILE:
                break
        d.idle()
        self.assertEqual(heavy.x, 13 * TILE)
        self.assertTrue(w.gate_open["A"])
        # p2 hops over the heavy box and climbs light box -> shelf -> key, then waits there
        d.jump_right_until("p2", lambda: d.p("p2").x > 15 * TILE + 4)
        d.walk_to("p2", light.x - 30)
        d.jump_right_until("p2", lambda: d.p("p2").x >= light.x + 1)
        self.assertIs(d.p("p2").standing_on, light)
        d.hold("p2", right=True, jump=True, ticks=25)
        d.walk_to("p2", 19 * TILE + 6)
        self.assertTrue(d.p("p2").has_key)
        # p1 pushes the light box onto the 2-button
        d.jump_right_until("p1", lambda: d.p("p1").x > 15 * TILE + 4)
        d.hold("p1", right=True, ticks=400, until=lambda: light.x >= 23 * TILE)
        d.step(3)
        self.assertTrue(w.gate_open["B"])
        # p2 jumps down, over p1 and the box, through gate B onto button c
        d.walk_to("p2", 21 * TILE + 2)
        d.jump_right_until("p2", lambda: d.p("p2").x > 25 * TILE + 2)
        d.walk_to("p2", 25 * TILE + 2)
        d.walk_to("p1", 22 * TILE - 6)
        d.jump_right_until("p1", lambda: d.p("p1").x > 24 * TILE + 4, limit=200)
        self.assertTrue(w.gate_open["B"])
        self.finish(d, "p2", "p1")
        self.assertTrue(w.cleared)


if __name__ == "__main__":
    unittest.main()
