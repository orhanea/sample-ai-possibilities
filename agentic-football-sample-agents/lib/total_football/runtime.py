"""Deadline-bounded inference without shared conversations or worker buildup."""

import asyncio
from concurrent.futures import Future
import json
import logging
import os
from threading import Lock, Thread
from time import perf_counter

from json_tolerant import parse_json_tolerant
from parsing import _iter_array_candidates

from .commands import command, fallback, validate
from .memory import OpponentMemory
from .prompts import context_text, system_prompt
from .tactics import build_plan, clamp, number


class InferenceBusy(Exception):
    """An earlier model call is still in flight; use this tick's fallback."""


class SingleFlightModel:
    """At most one background call per runtime, even if requests time out.

    A socket call cannot reliably be cancelled from Python. A daemon worker
    lets the request return on time; while it drains, subsequent ticks use
    their own fresh fallback rather than queueing calls or reusing stale output.
    Every call constructs its own Agent, so model conversations never mix.
    """

    def __init__(self, pid):
        self.pid = pid
        self._lock = Lock()
        self._future = None
        self._model = None

    def _infer(self, text):
        from botocore.config import Config
        from strands import Agent
        from strands.models import BedrockModel

        if self._model is None:
            self._model = BedrockModel(
                model_id=os.environ.get("FOOTBALL_MODEL_ID", "us.amazon.nova-micro-v1:0"),
                region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
                max_tokens=160, temperature=0.1,
                boto_client_config=Config(connect_timeout=0.5, read_timeout=1.0,
                                          retries={"total_max_attempts": 1}),
            )
        agent = Agent(model=self._model, system_prompt=system_prompt(self.pid),
                      tools=[], callback_handler=None)
        return str(agent(text))

    def start(self, text):
        with self._lock:
            if self._future is not None and not self._future.done():
                raise InferenceBusy()
            future = Future()
            self._future = future

            def run():
                try:
                    future.set_result(self._infer(text))
                except Exception as exc:
                    future.set_exception(exc)

            Thread(target=run, name="football-inference", daemon=True).start()
            return future


def parse_one(text):
    """Recover common JSON formatting, but require exactly one action."""
    if not isinstance(text, str) or len(text) > 8192:
        return None
    result = parse_json_tolerant(text)
    if result is None:
        for candidate in _iter_array_candidates(text):
            result = parse_json_tolerant(candidate)
            if result is not None:
                break
    if result is None:
        return None
    parsed, _ = result
    if isinstance(parsed, list):
        return parsed[0] if len(parsed) == 1 and isinstance(parsed[0], dict) else None
    return parsed if isinstance(parsed, dict) else None


class DecisionEngine:
    def __init__(self, pid, model=None, budget_seconds=None, match_seconds=None, logger=None):
        self.pid = pid
        self.model = model if model is not None else SingleFlightModel(pid)
        self.memory = OpponentMemory()
        configured = budget_seconds if budget_seconds is not None else os.environ.get("FOOTBALL_DECISION_SECONDS", 0.7)
        self.budget = clamp(number(configured, 0.7), 0.02, 4.0)
        duration = match_seconds if match_seconds is not None else os.environ.get("FOOTBALL_MATCH_SECONDS", 5400)
        self.match_seconds = max(1, number(duration, 5400))
        self.log = logger if logger is not None else logging.getLogger(__name__)

    async def decide(self, payload, context=None):
        start = perf_counter()
        source = "fallback"
        pid = self.pid
        plan = None
        try:
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            data = payload.get("prompt", payload)
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict) or type(data.get("teamId")) is not int or data["teamId"] not in (0, 1):
                raise ValueError("explicit teamId 0 or 1 required")
            ids = data.get("myPlayers", [self.pid])
            if not isinstance(ids, list) or len(ids) != 1 or type(ids[0]) is not int or ids[0] not in range(5):
                raise ValueError("myPlayers must contain exactly one player index")
            pid = ids[0]
            state = data.get("gameState", {})
            if not isinstance(state, dict):
                raise ValueError("gameState must be an object")
            plan = build_plan(state, data["teamId"], self.match_seconds)
            chosen = validate(fallback(plan, pid), plan, pid)
            if chosen is None:
                chosen = command("SET_STANCE", plan, pid, {"stance": 2})
            match_id = state.get("matchId") or data.get("matchId") or getattr(context, "session_id", None)
            observations = self.memory.observe(match_id, state, plan)
            text = context_text(state, plan, pid, observations)
            remaining = self.budget - (perf_counter() - start)
            try:
                if remaining <= 0:
                    raise TimeoutError()
                future = self.model.start(text)
                wrapped = asyncio.wrap_future(future)
                wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                done, _ = await asyncio.wait({wrapped}, timeout=remaining)
                if not done:
                    raise TimeoutError()
                response = wrapped.result()
                candidate = validate(parse_one(response), plan, pid)
                if candidate is not None:
                    chosen, source = candidate, "llm"
                else:
                    self.log.warning("football invalid model command; using role fallback")
            except (TimeoutError, InferenceBusy):
                source = "deadline_or_busy"
            except Exception:
                self.log.exception("football model failed; using role fallback")
            return [chosen]
        except Exception:
            self.log.exception("football invalid state or planning failure")
            if plan is not None:
                return [command("SET_STANCE", plan, pid, {"stance": 2})]
            return []
        finally:
            elapsed = (perf_counter() - start) * 1000
            self.log.info("football_decision player=%s role=%s mode=%s source=%s latency_ms=%.2f",
                          pid, plan.roles.get(pid) if plan else None,
                          plan.mode if plan else None, source, elapsed)
            if elapsed > 850:
                self.log.warning("football_latency_violation player=%s latency_ms=%.2f", pid, elapsed)


def register(app, pid):
    engine = DecisionEngine(pid, logger=app.logger)

    @app.entrypoint
    async def invoke(payload, context):
        yield json.dumps(await engine.decide(payload, context), separators=(",", ":"))

    return engine
