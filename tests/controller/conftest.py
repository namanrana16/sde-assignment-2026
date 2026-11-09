"""Shared fixtures: the real sim app in-process via TestClient (tests may import sim)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sim.server
from controller.client import SimClient
from controller.ingest import reset_epoch_anchor
from controller.store import Store
from sim.scenario import load_scenario
from sim.world import World

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_epoch_anchor():
    """ingest keeps a module-global epoch offset; never let one test's anchor leak into the next."""
    yield
    reset_epoch_anchor()


def build_world(tmp_path: Path, days: int = 2, history_days: int = 2) -> World:
    sc = load_scenario(str(ROOT / "scenarios" / "public_01_dev.yaml"))
    sc["days"], sc["history_days"] = days, history_days
    world = World(sc, tmp_path / "run")
    sim.server._world = world
    return world


@pytest.fixture()
def world(tmp_path):
    return build_world(tmp_path)


@pytest.fixture()
def client(world):
    with TestClient(sim.server.app) as http:
        yield SimClient(http=http, sleep=lambda s: None)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state" / "t.db")
    yield s
    s.close()
