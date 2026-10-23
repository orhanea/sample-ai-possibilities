"""Small, pure tactical calculations shared by all five isolated runtimes.

No network or mutable team state: equal snapshots produce equal assignments.
Coordinates in the planner are attack-relative; commands use world coordinates.
"""

from dataclasses import dataclass
from itertools import permutations
import math

from state import _is_my_team, _player_idx


ROLES = ("ANCHOR", "CONNECTOR", "FALSE_9", "SHADOW_STRIKER")
IDENTITIES = ("Sentinel", "Bastion", "Compass", "Maestro", "Dagger")
DUTIES = {
    "SWEEPER_KEEPER": "Protect goal, narrow shooting angles, sweep only inside your box, distribute safely.",
    "ANCHOR": "Protect the central counterattack lane; remain behind the ball; recycle possession.",
    "CONNECTOR": "Offer a wide passing triangle, switch play, cover the anchor when it steps out.",
    "FALSE_9": "Drop between lines, receive on the opposite side to connector, release the striker.",
    "SHADOW_STRIKER": "Stretch the last defender, run a free channel, attack the far post for cutbacks.",
}


def number(value, default=0.0):
    """Finite numeric input only; bools and malformed telemetry are not numbers."""
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def xy(value):
    value = value if isinstance(value, dict) else {}
    return number(value.get("x")), number(value.get("y"))


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass(frozen=True)
class Player:
    pid: int
    team: int
    pos: tuple[float, float]
    velocity: tuple[float, float]
    stamina: float
    action: str


@dataclass(frozen=True)
class Plan:
    team: int
    players: tuple[Player, ...]
    opponents: tuple[Player, ...]
    ball: tuple[float, float]
    ball_velocity: tuple[float, float]
    possession: str
    holder: int | None
    roles: dict[int, str]
    targets: dict[int, tuple[float, float]]
    mode: str
    shape: str
    presser: int | None
    emergency: bool
    restart: bool

    @property
    def direction(self):
        return 1 if self.team == 0 else -1


def _players(state):
    raw = [p for p in state.get("players", []) if isinstance(p, dict)]
    percent = state.get("staminaScale") == "percent" or (
        state.get("staminaScale") != "fraction"
        and any(number(p.get("stamina"), 1) > 1 for p in raw)
    )
    result = {}
    for p in raw:
        if "agentId" in p:
            aid = p["agentId"]
            if not isinstance(aid, str) or not aid.rsplit("_", 1)[-1].isdigit():
                continue
        pid = _player_idx(p)
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 <= pid <= 4:
            continue
        code = p.get("teamCode")
        if isinstance(code, str) and code.strip().lower() not in ("home", "away"):
            continue
        if not isinstance(code, str) and p.get("teamId") not in (0, 1):
            continue
        team = 0 if _is_my_team(p, 0) else 1
        stamina = number(p.get("stamina"), 100 if percent else 1)
        result[team, pid] = Player(
            pid, team, xy(p.get("position")), xy(p.get("velocity")),
            clamp(stamina / (100 if percent else 1), 0, 1),
            str(p.get("lastAction", p.get("currentAction", "")))[:48],
        )
    return tuple(result[key] for key in sorted(result))


def _possession(state, players, team):
    ball = state.get("ball", {})
    if ball.get("isFree") is True:
        return "free", None
    aid = ball.get("possessionAgentId")
    pid = ball.get("possessionPlayerId")
    if isinstance(aid, str):
        try:
            pid = int(aid.rsplit("_", 1)[-1])
        except ValueError:
            return "unknown", None
    if pid is None:
        return ("unknown" if ball.get("isFree") is False else "free"), None
    matches = [p for p in players if p.pid == pid]
    owner = ball.get("possessionTeamId")
    code = ball.get("possessionTeamCode")
    if isinstance(code, str) and code.lower() in ("home", "away"):
        owner = 0 if code.lower() == "home" else 1
    if owner in (0, 1):
        matches = [p for p in matches if p.team == owner]
    if len(matches) != 1:
        return "unknown", None
    return ("ours" if matches[0].team == team else "theirs"), pid


def _mode(state, team, match_seconds):
    score = state.get("score", {})
    difference = number(score.get("home")) - number(score.get("away"))
    difference *= 1 if team == 0 else -1
    duration = number(state.get("matchDuration"), match_seconds)
    remaining = max(0, duration - number(state.get("gameTime")))
    if difference > 0 and remaining <= duration / 9:
        return "defensive"
    if difference < 0 and remaining <= duration / 6:
        return "attacking"
    return "balanced"


def build_plan(state: dict, team: int, match_seconds: float = 5400) -> Plan:
    """Assign four outfield roles globally (at most 24 permutations).

    Travel cost discourages unnecessary rotations. Workload cost moves tired
    players into quieter roles; position bias breaks ties without fixing roles.
    Player 0 remains the only goalkeeper, as required by the game protocol.
    """
    all_players = _players(state)
    ours = tuple(p for p in all_players if p.team == team)
    opponents = tuple(p for p in all_players if p.team != team)
    ball = xy(state.get("ball", {}).get("position"))
    ball_velocity = xy(state.get("ball", {}).get("velocity"))
    possession, holder = _possession(state, all_players, team)
    direction = 1 if team == 0 else -1
    mode = _mode(state, team, match_seconds)
    restart = str(state.get("playMode", "OPEN_PLAY")).upper() in ("KICK_OFF", "KICKOFF", "FREE_KICK")
    if mode == "defensive":
        xs, shape = (-34, -22, -10, 12), "GK+3-1"
    elif mode == "attacking":
        xs, shape = (-14, 10, 25, 38), "GK+1-1-2"
    elif possession == "ours":
        xs, shape = (-22, 0, 14, 32), "GK+1-2-1"
    else:
        xs, shape = (-28, -12, -4, 18), "GK+2-1-1"
    if restart:
        xs = (-28, -12, -7, -1)
        shape = "GK+1-2-1 restart"
    shift = clamp(ball[0] * direction * 0.15, -5, 5)
    lateral = clamp(ball[1] * 0.25, -5, 5)
    ys = (0, -17, 15, -4)
    slots = {
        role: (round(clamp((x + (0 if restart else shift)) * direction, -50, 50), 2), round(clamp(y + lateral, -30, 30), 2))
        for role, x, y in zip(ROLES, xs, ys)
    }
    outfield = tuple(p for p in ours if p.pid != 0)
    workload = {"ANCHOR": 0.1, "CONNECTOR": 0.65, "FALSE_9": 0.45, "SHADOW_STRIKER": 1.0}

    def cost(assignment):
        total = 0.0
        for p, role in zip(outfield, assignment):
            travel = distance(p.pos, slots[role]) ** 2
            fatigue = max(0, 0.6 - p.stamina) * 3500 * workload[role]
            bias = 70 if role != ROLES[p.pid - 1] else 0
            total += travel + fatigue + bias
        return total

    assignment = min(permutations(ROLES, len(outfield)), key=cost)
    roles = {p.pid: role for p, role in zip(outfield, assignment)}
    targets = {p.pid: slots[roles[p.pid]] for p in outfield}
    if any(p.pid == 0 for p in ours):
        roles[0] = "SWEEPER_KEEPER"
        keeper_x = -48 if possession == "ours" and ball[0] * direction > 0 else -51
        targets[0] = (keeper_x * direction, clamp(ball[1] * 0.16, -5, 5))
    enemy_holder = next((p for p in opponents if p.pid == holder), None) if possession == "theirs" else None
    threat = enemy_holder.pos if enemy_holder else ball
    emergency = possession != "ours" and distance(threat, (-55 * direction, 0)) < 20
    eligible = outfield
    if possession == "ours" or restart:
        eligible = ()
    if possession == "free" and ball[0] * direction < -43 and abs(ball[1]) < 16:
        eligible = ours
    nearest = min((distance(p.pos, threat) for p in eligible), default=math.inf)
    tied = [p for p in eligible if distance(p.pos, threat) <= nearest + 1]
    chosen = min(tied, key=lambda p: (-p.stamina, p.pid)) if tied else None
    return Plan(team, ours, opponents, ball, ball_velocity, possession, holder,
                roles, targets, mode, shape, chosen.pid if chosen else None,
                emergency, restart)


def lane_clearance(start, end, opponents):
    """Minimum distance of a defender to the interior of a pass/shot segment."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length2 = dx * dx + dy * dy
    clearance = 50.0
    if length2 < 0.01:
        return 0.0
    for p in opponents:
        pos = (p.pos[0] + 0.3 * p.velocity[0], p.pos[1] + 0.3 * p.velocity[1])
        t = ((pos[0] - start[0]) * dx + (pos[1] - start[1]) * dy) / length2
        if 0.05 < t < 1.0:
            clearance = min(clearance, distance(pos, (start[0] + t * dx, start[1] + t * dy)))
    return clearance


def pass_options(plan, me):
    """Rank safe outlets by lane, receiver pressure, progress and distance."""
    options = []
    for target in plan.players:
        if target.pid == me.pid:
            continue
        length = distance(me.pos, target.pos)
        if length < 2:
            continue
        lane = lane_clearance(me.pos, target.pos, plan.opponents)
        space = min((distance(target.pos, p.pos) for p in plan.opponents), default=30)
        progress = (target.pos[0] - me.pos[0]) * plan.direction
        value = min(lane, 10) * 2.5 + min(space, 12) + progress * 0.35 - length * 0.18
        options.append({"player": target.pid, "value": round(value, 2),
                        "lane": round(lane, 1), "space": round(space, 1),
                        "distance": round(length, 1)})
    return sorted(options, key=lambda o: (-o["value"], o["player"]))


def shot_quality(plan, me):
    """Heuristic opportunity score, NOT a calibrated engine goal probability."""
    goal = (55 * plan.direction, 0)
    length = distance(me.pos, goal)
    angle = math.atan2(abs(me.pos[1]), max(0.1, 55 - me.pos[0] * plan.direction))
    pressure = min((distance(me.pos, p.pos) for p in plan.opponents), default=30)
    lane = lane_clearance(me.pos, goal, plan.opponents)
    quality = clamp(1 - length / 48 - angle / math.pi - max(0, 4 - pressure) * 0.08, 0, 1)
    if lane < 2:
        quality *= 0.45
    return round(quality, 3)
