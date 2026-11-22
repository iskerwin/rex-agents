from rex_agents.links import Links, locate

FRONTS = {10: ("s", "w", "b1"), 20: ("s", "w", "b2")}
LINEAGE = {10: [10, 5], 31: [31, 20, 5], 40: [40, 5]}


def test_a_task_is_in_the_pane_whose_front_it_is_or_runs_under():
    assert locate([10, 31, 40], FRONTS, LINEAGE.get) == {10: ("s", "w", "b1"), 31: ("s", "w", "b2")}


def test_links_go_only_to_a_known_pane():
    class FakeRex:
        went = []
        def fronts(self):
            return FRONTS
        def go(self, place):
            self.went.append(place)
    rex = FakeRex()
    links = Links(rex)
    links.refresh([{"pid": 10}, {"pid": None}])
    assert links.places == {10: ("s", "w", "b1")}
    links.go(99)
    links.go(10)
    import time
    for _ in range(50):
        if rex.went:
            break
        time.sleep(0.01)
    assert rex.went == [("s", "w", "b1")]
