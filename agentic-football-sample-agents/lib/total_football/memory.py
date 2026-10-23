"""Bounded, match-isolated local observations; never a coordination authority."""

from collections import OrderedDict, deque
from threading import Lock

from .tactics import number


class OpponentMemory:
    def __init__(self, capacity=300, max_matches=16):
        self.capacity = capacity
        self.max_matches = max_matches
        self._matches = OrderedDict()
        self._lock = Lock()

    def observe(self, match_id, state, plan):
        if not match_id:
            return ()
        key = (str(match_id)[:256], plan.team)
        tick = number(state.get("tick"), number(state.get("gameTime")))
        with self._lock:
            history = self._matches.setdefault(key, deque(maxlen=self.capacity))
            self._matches.move_to_end(key)
            while len(self._matches) > self.max_matches:
                self._matches.popitem(last=False)
            if history and tick < history[-1]["tick"]:
                return tuple(list(history)[-3:])
            sample = {"tick": tick, "time": number(state.get("gameTime")),
                      "opponents": [{"id": p.pid, "pos": p.pos, "velocity": p.velocity, "action": p.action}
                                    for p in plan.opponents]}
            if not history or tick > history[-1]["tick"]:
                history.append(sample)
            return tuple(list(history)[-3:])
