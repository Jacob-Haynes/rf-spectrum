from rfspectrum.engine import Engine
from rfspectrum.frame import Frame
from rfspectrum.simulator import SimulatedEnvironment


def run_sim(preset: str, own: list[str], seconds: float, background: bool = False, seed: int = 7,
            engine: Engine | None = None, env: SimulatedEnvironment | None = None, t0: float = 1000.0):
    env = env or SimulatedEnvironment(seed=seed, background=background, scenario="manual")
    for em in own:
        env.set_emitter(em, True, t0)
    engine = engine or Engine()
    events = []
    t = t0
    while t < t0 + seconds:
        events += engine.process(Frame(t, preset, env.render(preset, t)))
        t += 0.1
    return engine, events, t
