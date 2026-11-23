"""Extracted legacy mechanisms. See data/legacy_sources.json; no old modules imported."""

import random

import math

import sys

from contextlib import contextmanager

from collections import Counter, deque

from dataclasses import dataclass, field

HEAT_WINDOW = 3
HEAT_DECAY = .5
REPOST_WEIGHT = 4

def fixed_graph(nodes, seed, neighbors=6, rewiring=.12):
    """以指定种子构建无向小世界图，返回邻接集合、去重边和重连边。"""
    rng = random.Random(seed)
    graph = [set() for _ in range(nodes)]
    for offset in range(1, neighbors // 2 + 1):
        for source in range(nodes):
            target = (source + offset) % nodes
            graph[source].add(target)
            graph[target].add(source)
    shortcuts = set()
    for offset in range(1, neighbors // 2 + 1):
        for source in range(nodes):
            target = (source + offset) % nodes
            if rng.random() >= rewiring:
                continue
            blocked = sorted(graph[source] | {source})
            if len(blocked) == nodes:
                continue
            # Draw the same rank as choice over the allowed nodes without scanning the population.
            replacement = rng.randrange(nodes - len(blocked))
            for node in blocked:
                if node > replacement:
                    break
                replacement += 1
            graph[source].remove(target)
            graph[target].remove(source)
            shortcuts.discard(tuple(sorted((source, target))))
            graph[source].add(replacement)
            graph[replacement].add(source)
            shortcuts.add(tuple(sorted((source, replacement))))
    edges = [(source, target) for source in range(nodes) for target in sorted(graph[source]) if source < target]
    return graph, edges, shortcuts

def daily_visit_weight(hour):
    """用三个截断余弦峰给出上午、下午与晚间的用户活跃权重。"""
    weight = 0.05
    for center, width, amplitude in ((10.5, 4.0, .79), (15.0, 4.0, .75), (20.0, 4.2, .87)):
        distance = abs((hour - center + 12) % 24 - 12)
        if distance < width:
            weight += amplitude * (1 + math.cos(math.pi * distance / width)) / 2
    return min(1.0, weight)

@dataclass(frozen=True)
class PostHeat:
    """保存一条帖子的衰减曝光、直接转发和可序列化热度。"""
    views: float = 0.0
    direct_reposts: float = 0.0

    @property
    def score(self):
        return self.views + REPOST_WEIGHT * self.direct_reposts

    def as_record(self):
        return {"weighted_views": self.views, "weighted_direct_reposts": self.direct_reposts, "score": self.score}

@dataclass
class PostHeatLedger:
    """按已完成轮次维护并重建逐帖热度快照。"""
    rounds: deque = field(default_factory=lambda: deque(maxlen=HEAT_WINDOW))

    def append(self, views, direct_reposts):
        self.rounds.append((views, direct_reposts))

    def snapshot(self):
        views, reposts = Counter(), Counter()
        for age, (round_views, round_reposts) in enumerate(reversed(self.rounds)):
            weight = HEAT_DECAY ** age
            for post_id, count in round_views.items():
                views[post_id] += weight * count
            for post_id, count in round_reposts.items():
                reposts[post_id] += weight * count
        return {post_id: PostHeat(views[post_id], reposts[post_id]) for post_id in views.keys() | reposts.keys()}

@contextmanager
def exclusive_lock(path):
    """OS releases the byte lock after interruption; no stale PID guessing."""
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == 'win32':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
