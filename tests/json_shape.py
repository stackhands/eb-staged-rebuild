"""Print the key paths of a JSON document on stdin, one per line, without the values.

Host-dependent reports (probe) keep their structure under test and leave the values out.
"""

import json
import sys


def paths(value, prefix=""):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from paths(value[key], f"{prefix}.{key}")
    else:
        yield f"{prefix} {type(value).__name__}" if value is not None else f"{prefix} null"


for line in paths(json.load(sys.stdin)):
    print(line)
