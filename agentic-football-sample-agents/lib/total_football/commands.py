"""Role-aware fallback and strict, state-aware output validation."""

import math

from .tactics import clamp, distance, pass_options, shot_quality


def command(kind, plan, pid, parameters, duration=0):
    return {"commandType": kind, "playerId": pid, "teamId": plan.team,
            "parameters": parameters, "duration": duration}


def fallback(plan, pid):
    """Return one useful action when the model misses its deadline or schema."""
    me = next((p for p in plan.players if p.pid == pid), None)
    if me is None:
        return command("SET_STANCE", plan, pid, {"stance": 2})
    if plan.possession == "ours" and plan.holder == pid:
        options = pass_options(plan, me)
        if pid != 0 and distance(me.pos, (55 * plan.direction, 0)) <= 28 and shot_quality(plan, me) >= 0.4:
            return command("SHOOT", plan, pid, {"aim_location": "BL" if me.pos[1] > 0 else "BR", "power": 0.85})
        safe = next((o for o in options if o["lane"] >= 3 and o["space"] >= 3), None)
        if safe:
            kind = "GK_DISTRIBUTE" if pid == 0 else "PASS"
            params = {"target_player_id": safe["player"]}
            params.update({"method": "THROW" if safe["distance"] < 25 else "KICK"} if pid == 0 else {"type": "GROUND"})
            return command(kind, plan, pid, params)
        if pid == 0:
            if options:
                return command("GK_DISTRIBUTE", plan, pid,
                               {"target_player_id": options[0]["player"], "method": "KICK"})
            return command("SET_STANCE", plan, pid, {"stance": 2})
        candidates = [(clamp(me.pos[0] + 7 * plan.direction, -52, 52), clamp(me.pos[1] + dy, -30, 30))
                      for dy in (0, -7, 7)]
        target = max(candidates, key=lambda pos: min((distance(pos, p.pos) for p in plan.opponents), default=30))
        return command("MOVE_TO", plan, pid, {"target_x": target[0], "target_y": target[1], "sprint": False})
    if not plan.restart and plan.presser == pid:
        if plan.possession == "free":
            return command("INTERCEPT", plan, pid, {"aggressive": me.stamina >= 0.3 or plan.emergency}, 1)
        if plan.possession == "theirs" and distance(me.pos, plan.ball) <= 15:
            return command("PRESS_BALL", plan, pid, {"intensity": 0.45 if me.stamina < 0.35 else 0.75}, 1)
        # Unknown possession means approach, never invent a pass or shot.
        target = (clamp(plan.ball[0] + 0.4 * plan.ball_velocity[0], -53, 53),
                  clamp(plan.ball[1] + 0.4 * plan.ball_velocity[1], -33, 33))
    else:
        target = plan.targets[pid]
        if pid == 0 and plan.possession != "ours":
            # Guard the projected goal-line crossing for shots travelling at us.
            vx, vy = plan.ball_velocity
            if vx * plan.direction < -0.1:
                seconds = (-51 * plan.direction - plan.ball[0]) / vx
                if 0 < seconds < 3:
                    target = (-51 * plan.direction, clamp(plan.ball[1] + seconds * vy, -6, 6))
    sprint = distance(me.pos, target) > 12 and (me.stamina >= 0.6 or plan.emergency)
    if pid == 0 and not plan.emergency:
        sprint = False
    return command("MOVE_TO", plan, pid, {"target_x": target[0], "target_y": target[1], "sprint": sprint})


def validate(candidate, plan, pid):
    """Return a sanitized command or None. No fabricated targets or parameters.

    RESET is deliberately excluded because it changes the whole team. Maintained
    commands expire next tick so stale overrides cannot persist across phases.
    """
    if not isinstance(candidate, dict):
        return None
    params = candidate.get("parameters")
    if not isinstance(params, dict):
        return None
    kind = candidate.get("commandType")
    me = next((p for p in plan.players if p.pid == pid), None)
    if me is None:
        return None
    has_ball = plan.possession == "ours" and plan.holder == pid

    def numeric(key, low, high):
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(key)
        return clamp(value, low, high)

    def target(players):
        value = params.get("target_player_id")
        if isinstance(value, bool) or not isinstance(value, int) or not any(p.pid == value for p in players):
            raise ValueError("target")
        return value

    try:
        duration = 0
        if kind == "MOVE_TO":
            sprint = params.get("sprint")
            if not isinstance(sprint, bool):
                return None
            x, y = numeric("target_x", -53, 53), numeric("target_y", -33, 33)
            if pid == 0:
                x = clamp(x * plan.direction, -53, -43) * plan.direction
                y = clamp(y, -16, 16)
            if plan.roles.get(pid) == "ANCHOR" and plan.presser != pid and not has_ball:
                x = min(x * plan.direction, -5) * plan.direction
            clean = {"target_x": x, "target_y": y,
                     "sprint": sprint and (me.stamina >= 0.3 or plan.emergency)}
        elif kind in ("PASS", "GK_DISTRIBUTE"):
            if not has_ball or (kind == "GK_DISTRIBUTE" and pid != 0):
                return None
            receiver = target(tuple(p for p in plan.players if p.pid != pid))
            key, allowed = ("method", ("THROW", "KICK")) if kind == "GK_DISTRIBUTE" else ("type", ("GROUND", "AERIAL", "THROUGH"))
            if params.get(key) not in allowed:
                return None
            clean = {"target_player_id": receiver, key: params[key]}
        elif kind == "SHOOT":
            if not has_ball or pid == 0 or params.get("aim_location") not in ("TL", "TR", "BL", "BR", "CENTER"):
                return None
            if distance(me.pos, (55 * plan.direction, 0)) > 28:
                return None
            clean = {"aim_location": params["aim_location"], "power": numeric("power", 0, 1)}
        elif kind == "PRESS_BALL":
            if plan.restart or plan.presser != pid or plan.possession != "theirs" or distance(me.pos, plan.ball) > 15:
                return None
            clean = {"intensity": min(numeric("intensity", 0, 1), 0.45 if me.stamina < 0.35 else 0.85)}
            duration = 1
        elif kind == "INTERCEPT":
            if plan.restart or plan.possession != "free" or plan.presser != pid or not isinstance(params.get("aggressive"), bool):
                return None
            clean = {"aggressive": params["aggressive"] and (me.stamina >= 0.3 or plan.emergency)}
            duration = 1
        elif kind == "MARK":
            if plan.restart or plan.possession == "ours" or pid == 0:
                return None
            receiver = target(plan.opponents)
            if receiver == 0 or params.get("tightness") not in ("LOOSE", "TIGHT"):
                return None
            clean = {"target_player_id": receiver, "tightness": params["tightness"]}
            duration = 1
        elif kind == "SET_STANCE":
            stance = params.get("stance")
            if type(stance) is not int or stance not in (0, 1, 2):
                return None
            clean = {"stance": stance}
        else:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return command(kind, plan, pid, clean, duration)
