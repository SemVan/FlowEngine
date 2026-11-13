"""Parse M105 without confusing temperature, target and raw ADC readings."""

from __future__ import annotations

import math
import re

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_REPORT = re.compile(
    rf"(?<![\w@])(T\d*|B|C|P|L|M|R|ADC\d+):\s*({_NUMBER})"
    rf"(?:\s*/\s*{_NUMBER}(?:\s*\(\s*({_NUMBER})\s*\))?)?"
)


def parse_sensor_report(lines: list[str]) -> dict[str, float]:
    """T0.adc denotes the parenthesized SHOW_TEMP_ADC_VALUES field, not °C.

    ADC0 etc. are supported only when custom firmware explicitly reports them.
    T is the active hotend and must not be guessed to mean T0.
    """
    values: dict[str, float] = {}
    for line in lines:
        for match in _REPORT.finditer(line):
            source, value, adc = match.groups()
            if math.isfinite(float(value)):
                values[source] = float(value)
            if adc is not None and math.isfinite(float(adc)):
                values[source + ".adc"] = float(adc)
    return values
