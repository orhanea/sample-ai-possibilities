"""Inspect roles, formations and fallbacks, or benchmark the local pipeline.

Usage: python inspect_team.py --state payload.json --team 1 --svg formation.svg
       python inspect_team.py --benchmark 1000
No arguments uses a demonstration snapshot, not simulated match results.
"""

import argparse
import html
import json
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "lib"))

from total_football.commands import fallback, validate
from total_football.prompts import context_text
from total_football.tactics import DUTIES, IDENTITIES, build_plan


def demo():
    players = []
    for team in (0, 1):
        for pid, (x, y) in enumerate(((-50, 0), (-25, 0), (-5, -17), (12, 15), (30, -4))):
            players.append({"agentId": f"agentId_{pid}", "teamCode": "home" if team == 0 else "away",
                            "position": {"x": x * (1 if team == 0 else -1), "y": y}, "stamina": 0.8})
    return {"tick": 20, "gameTime": 40, "matchDuration": 600,
            "score": {"home": 0, "away": 0}, "playMode": "OPEN_PLAY", "players": players,
            "ball": {"position": {"x": 12, "y": 15}, "isFree": False,
                     "possessionAgentId": "agentId_3", "possessionTeamId": 0}}


def formation_svg(plan, path):
    """A portable pitch diagram of target positions; no browser or assets needed."""
    def screen(pos):
        return 70 + (pos[0] + 55) * 7, 110 + (pos[1] + 35) * 7

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1050" height="680" viewBox="0 0 1050 680">',
             '<rect width="1050" height="680" fill="#101b27"/>',
             f'<text x="70" y="40" fill="white" font-family="sans-serif" font-size="24">Total Football | {html.escape(plan.shape)} | {plan.mode}</text>',
             '<text x="70" y="70" fill="#a6bed1" font-family="sans-serif" font-size="15">Circles: current position. Arrows and labels: assigned formation targets.</text>',
             '<defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7" fill="#54e2bb"/></marker></defs>',
             '<rect x="70" y="110" width="770" height="490" fill="#163e37" stroke="#91b6a0" stroke-width="2"/>',
             '<path d="M455,110 V600 M70,243 H196 V467 H70 M840,243 H714 V467 H840" fill="none" stroke="#91b6a0"/>',
             '<circle cx="455" cy="355" r="64" fill="none" stroke="#91b6a0"/>']
    for p in plan.opponents:
        x, y = screen(p.pos)
        parts.append(f'<circle cx="{x}" cy="{y}" r="12" fill="#b66b78" opacity="0.6"/>')
    for p in plan.players:
        x, y = screen(p.pos)
        tx, ty = screen(plan.targets[p.pid])
        parts.extend([
            f'<line x1="{x}" y1="{y}" x2="{tx}" y2="{ty}" stroke="#54e2bb" stroke-dasharray="5 4" marker-end="url(#arrow)"/>',
            f'<circle cx="{x}" cy="{y}" r="14" fill="#54e2bb"/>',
            f'<text x="{x}" y="{y+5}" text-anchor="middle" font-family="sans-serif" font-size="13">{p.pid}</text>',
            f'<circle cx="{tx}" cy="{ty}" r="17" fill="none" stroke="#54e2bb"/>',
            f'<text x="{tx}" y="{ty-24}" text-anchor="middle" fill="white" font-family="sans-serif" font-size="14">{IDENTITIES[p.pid]}</text>',
            f'<text x="{tx}" y="{ty+36}" text-anchor="middle" fill="#a6f1dc" font-family="sans-serif" font-size="11">{plan.roles[p.pid]}</text>',
        ])
    bx, by = screen(plan.ball)
    parts.append(f'<circle cx="{bx}" cy="{by}" r="6" fill="white" stroke="#101b27"/>')
    parts.append(f'<text x="70" y="640" fill="#a6bed1" font-family="sans-serif" font-size="15">Attack direction: {"right" if plan.direction == 1 else "left"} | Possession: {plan.possession} | Presser: {plan.presser}</text>')
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--team", type=int, choices=(0, 1), default=0)
    parser.add_argument("--svg", type=Path)
    parser.add_argument("--benchmark", type=int, default=0, metavar="ITERATIONS")
    args = parser.parse_args()
    state = json.loads(args.state.read_text(encoding="utf-8-sig")) if args.state else demo()
    state = state.get("prompt", state)
    if isinstance(state, str):
        state = json.loads(state)
    state = state.get("gameState", state)
    plan = build_plan(state, args.team)
    print(f"Formation: {plan.shape}; mode: {plan.mode}; possession: {plan.possession}; presser: {plan.presser}")
    for p in plan.players:
        print(f"P{p.pid} {IDENTITIES[p.pid]:9} {plan.roles[p.pid]:16} -> {plan.targets[p.pid]}")
        print(f"  {DUTIES[plan.roles[p.pid]]}")
        print(f"  Fallback: {json.dumps(fallback(plan, p.pid), separators=(',', ':'))}")
    if args.svg:
        formation_svg(plan, args.svg)
        print(f"Wrote {args.svg}")
    if args.benchmark:
        samples = []
        context_bytes = 0
        for _ in range(args.benchmark):
            start = perf_counter()
            current = build_plan(state, args.team)
            for p in current.players:
                context_bytes = max(context_bytes, len(context_text(state, current, p.pid)))
                assert validate(fallback(current, p.pid), current, p.pid)
            samples.append((perf_counter() - start) * 1000)
        samples.sort()
        print(json.dumps({"iterations": len(samples), "pipeline": "plan + 5 contexts + 5 validated fallbacks; excludes LLM/network",
                          "median_ms": round(median(samples), 3),
                          "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * .95))], 3),
                          "max_context_bytes": context_bytes}, indent=2))


if __name__ == "__main__":
    main()
