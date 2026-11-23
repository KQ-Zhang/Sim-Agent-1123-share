"""Dual-channel environment adapted from E19; fixed replay bypasses endogenous exposure."""
import math
from collections import Counter
from .legacy import daily_visit_weight, PostHeatLedger
from .storage import keyed_rng, digest


def initial_state(nodes):
    return {"next_tick": 0, "messages": [], "own": [[] for _ in range(nodes)],
            "seen": [{} for _ in range(nodes)], "heat_rounds": [], "records": []}


def heat_snapshot(state):
    ledger = PostHeatLedger()
    for views, reposts in state["heat_rounds"]:
        ledger.append(Counter(views), Counter(reposts))
    return ledger.snapshot()


def contexts(cfg, scenario, graph, state, seed, mode):
    env, tick = cfg["environment"], state["next_tick"]
    heat = heat_snapshot(state)
    all_messages = [*scenario["messages"], *state["messages"]]
    available = [m for m in all_messages if m["tick"] <= tick and tick-m["tick"] < env["message_lifetime"]]
    lookup = {m["id"]: m for m in all_messages}
    result = []
    for node in range(env["nodes"]):
        profile = scenario["profiles"][node % len(scenario["profiles"])]
        hour = (env["start_hour"] + tick * env["step_minutes"] / 60) % 24
        rate = env["online_rate"] * (daily_visit_weight(hour) if env["diurnal"] else 1)
        online = keyed_rng(seed, "online", tick, node).random() < rate
        channel = "community" if keyed_rng(seed, "channel", tick, node).random() < env["community_rate"] else "platform"
        remembered = []
        own = [p for p in state["own"][node] if tick-p["tick"] <= env["memory_rounds"]] if env["memory_rounds"] else []
        if mode == "fixed_replay":
            replay = scenario["replay"][str(tick)]
            online, channel = True, replay["channel"]
            cards = [lookup[pid] for pid in replay["cards"]]
            own = replay["history"]
        elif not online:
            cards = []
        elif channel == "community":
            candidates = [m for m in available if m["channel"] == channel and m["author"] in graph[node] and m["tick"] < tick]
            # E19 exposes the latest committed message per neighbor.
            latest = {}
            for m in sorted(candidates, key=lambda m: (m["tick"], m["id"])):
                latest[m["author"]] = m
            cards = sorted(latest.values(), key=lambda m: (-m["tick"], m["id"]))[:env["capacity"]]
        else:
            candidates = [m for m in available if m["channel"] == "platform" and m["author"] != node]
            def priority(m):
                interest = sum(word in m["text"] for word in profile["interests"])
                score = heat[m["id"]].score if m["id"] in heat else 0
                weight = math.exp(-(tick-m["tick"])/12) * (1 + env["heat_weight"]*math.log1p(score))
                draw = max(1e-15, keyed_rng(seed, "ranking", tick, node, m["id"]).random())
                return -math.log(draw)/weight - env["interest_weight"]*interest, m["id"]
            cards = sorted(candidates, key=priority)[:env["capacity"]]
        if online and mode == "closed_loop":
            remembered = [lookup[pid] for pid, seen in sorted(state["seen"][node].items())
                          if env["memory_rounds"] and tick-seen <= env["memory_rounds"] and lookup[pid]["channel"] != channel]
        result.append({"schema_version": 1, "tick": tick, "agent": node, "online": online,
                       "channel": channel, "profile": profile, "scenario": scenario["background"],
                       "question": scenario["question"], "cards": cards, "remembered": remembered, "history": own})
    return result


def commit(cfg, scenario, state, inputs, answers, fixture):
    tick = state["next_tick"]
    made, records = [], []
    views, reposts = Counter(), Counter()
    for context, (answer, request_id) in zip(inputs, answers, strict=True):
        node = context["agent"]
        for post in context["cards"]:
            state["seen"][node][post["id"]] = tick
            if context["channel"] == "platform":
                views[post["id"]] += 1
        if answer and answer["action"] != "silent":
            lookup = {p["id"]: p for p in [*context["cards"], *context["remembered"]]}
            source = lookup.get(answer["target"])
            post = {"id": f"r{tick}-a{node}", "event": scenario["id"], "tick": tick, "author": node,
                    "channel": context["channel"], "parent": answer["target"],
                    "text": source["text"] if source else answer["text"]}
            made.append(post)
            state["own"][node].append(post)
            if answer["action"] == "repost" and context["channel"] == "platform":
                reposts[answer["target"]] += 1
        status = "offline" if not context["online"] else "silent" if answer["action"] == "silent" else "published"
        records.append({"schema_version": 1, "fixture": fixture, "event": scenario["id"], "tick": tick,
                        "agent": node, "status": status, "context": context, "input_sha256": digest(context),
                        "action": answer, "request_id": request_id,
                        "measurement": {"axis": cfg["measurement"]["axis"], "scope": "current_utterance",
                                        "time_range": [tick, tick], "value": None, "status": "not_measured"}})
    # No participant sees another participant's action from the current round.
    state["messages"].extend(made)
    state["heat_rounds"] = [*state["heat_rounds"], [dict(views), dict(reposts)]][-3:]
    state["records"].extend(records)
    state["next_tick"] += 1
