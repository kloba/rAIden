import dataclasses
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from raiden.config import Config  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    c = dataclasses.replace(
        Config(),
        workdir=tmp_path / "opt",
        state_dir=tmp_path / "state",
        pg_log_dir=tmp_path / "log",
        pgdata=tmp_path / "pgdata",
        pg_conf_dir=tmp_path / "etc",
        endpoint="https://example.invalid/",
        quiet_s=0.2,
        max_wait_s=2.0,
        verify_window_s=1.0,
        evolve_enabled=False,
        liveness_interval_s=0.2,
        patrol_interval_s=0,
    )
    for d in (c.workdir, c.state_dir, c.pg_log_dir, c.skills_dir, c.checks_dir, c.pg_conf_dir / "conf.d", c.pgdata):
        d.mkdir(parents=True, exist_ok=True)
    return c
