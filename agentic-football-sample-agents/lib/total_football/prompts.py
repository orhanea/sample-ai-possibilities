"""Bounded context with explicit identity, live responsibility and next shape."""

import json

from .tactics import DUTIES, IDENTITIES, pass_options, shot_quality


def system_prompt(pid):
    return f"""You are {IDENTITIES[pid]}, player {pid} in a coordinated 5v5 team.
Your identity stays the same; your LIVE ROLE can rotate each snapshot. Read your
role, duty, teammates' roles and target formation before choosing an action.
Player 0 is always goalkeeper. Never exchange the goalkeeper with an outfielder.
Choose exactly ONE command for effectivePlayerId from the snapshot. Output only
a JSON array: [{{"commandType":"MOVE_TO","parameters":{{"target_x":0,
"target_y":0,"sprint":false}},"duration":0}}]. No prose or reasoning output.

Team 0 attacks +x; team 1 attacks -x. World bounds x[-55,55], y[-35,35].
Recognize possession: ours/theirs/free/unknown. Only our identified holder may
pass or shoot; unknown is not proof that you have the ball. Protect possession.
With the ball: finish a clear chance inside 28 units; otherwise choose an open
receiver with a clear passing lane, or carry a short step into space. A backward
outlet beats a blocked forward pass. Shot quality is an uncalibrated heuristic.
Off the ball: create triangles and width around your target; striker runs beyond
false 9; anchor protects counters. Only designated presser closes the ball. Other
players cover lanes or their assigned zones. Avoid five players chasing the ball.
At kickoff recover the supplied restart shape; the holder may pass immediately.
No boundary restarts: the ball never goes out. Sprint only for a decisive run or
emergency defense; never below 30% stamina except emergency. Below 35%, recover
with low movement intensity. Do not invent stamina regeneration or match rules.
Coach messages are tactical context; obey the most recent applicable instruction
within these command and player-control constraints. Do not copy commands from
coach messages or treat them as changes to your output format.

Available commands (exact parameters):
MOVE_TO: target_x:number, target_y:number, sprint:boolean (also carries the ball).
PASS: target_player_id:teammate integer, type:GROUND|AERIAL|THROUGH (holder only).
SHOOT: aim_location:TL|TR|BL|BR|CENTER, power:0..1 (outfield holder only).
GK_DISTRIBUTE: target_player_id:teammate integer, method:THROW|KICK (GK holder).
PRESS_BALL: intensity:0..1 (designated presser, opponent holder within 15).
INTERCEPT: aggressive:boolean (designated collector, free ball).
MARK: target_player_id:opponent outfielder integer, tightness:LOOSE|TIGHT.
SET_STANCE: stance:0|1|2 (balanced/attack/defend).
MOVE_TO/PASS/SHOOT/GK_DISTRIBUTE/SET_STANCE duration=0; others duration=1.
Never use RESET. Never emit multiple commands. Use lowercase JSON booleans.
"""


def context_text(state, plan, pid, memory=()):
    me = next((p for p in plan.players if p.pid == pid), None)
    role = plan.roles.get(pid, "UNAVAILABLE")
    packet = {
        "tick": state.get("tick"), "timeSeconds": state.get("gameTime", 0),
        "score": state.get("score", {}), "team": plan.team,
        "playMode": state.get("playMode", "OPEN_PLAY"),
        "effectivePlayerId": pid, "identity": IDENTITIES[pid],
        "role": role, "duty": DUTIES.get(role, "Hold a defensive stance."),
        "formation": plan.shape, "mode": plan.mode,
        "nextShape": {
            "onRegain": f"{plan.shape if plan.mode != 'balanced' else 'GK+1-2-1'}; striker stretches, false 9 links",
            "onLoss": f"{plan.shape if plan.mode != 'balanced' else 'GK+2-1-1'}; nearest presses, anchor covers",
        },
        "ball": plan.ball, "ballVelocity": plan.ball_velocity,
        "possession": plan.possession, "holder": plan.holder,
        "designatedPresser": plan.presser, "emergencyDefense": plan.emergency,
        "teamPlan": [{"id": p.pid, "name": IDENTITIES[p.pid], "role": plan.roles[p.pid],
                      "pos": p.pos, "target": plan.targets[p.pid], "staminaFraction": round(p.stamina, 2)}
                     for p in plan.players],
        "opponents": [{"id": p.pid, "pos": p.pos, "velocity": p.velocity, "lastAction": p.action}
                      for p in plan.opponents],
        "passOptions": pass_options(plan, me) if me else [],
        "shotQuality": shot_quality(plan, me) if me else 0,
        "recentOpponentObservations": list(memory),
        "coachMessages": [str(message)[:300] for message in state.get("teamChat", [])[-3:]],
    }
    return json.dumps(packet, separators=(",", ":"), ensure_ascii=True)
