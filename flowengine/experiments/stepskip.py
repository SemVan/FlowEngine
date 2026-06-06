"""Step-skipping test — Phase 3 scaffold.

Protocol (sketched in docs/STEPSKIP.md):
1. Home the axis under test.
2. Step the axis through a parameter ramp (feedrate or load):
   for each setpoint:
       record baseline M114
       command N steps at the setpoint
       wait_idle + M114
       record pressure (PressureSensor.read())
       compare commanded vs actual position; flag step loss if delta > tolerance
3. Emit CSV (setpoint, commanded_steps, actual_steps, pressure, ts) and a
   markdown summary identifying the first setpoint where loss occurred.
"""

from __future__ import annotations


class StepSkipTest:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "Step-skip test is Phase 3. See docs/STEPSKIP.md for the protocol and parameters."
        )
