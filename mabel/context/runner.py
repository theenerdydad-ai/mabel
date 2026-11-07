"""Gateway-owned subprocess entry point for the first Python capability contract."""

import importlib.util
import json
import sys
from pathlib import Path


def main() -> None:
    executor_path = Path(sys.argv[1])
    callable_name = sys.argv[2]
    specification = importlib.util.spec_from_file_location("mabel_user_capability", executor_path)
    if specification is None or specification.loader is None:
        raise RuntimeError("Capability executor could not be loaded.")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    function = getattr(module, callable_name, None)
    if not callable(function):
        raise RuntimeError("Capability executor callable is missing.")
    raw_arguments = sys.stdin.read()
    arguments = json.loads(raw_arguments) if raw_arguments else None
    result = function() if arguments is None else function(arguments)
    if not isinstance(result, dict):
        raise RuntimeError("Capability result must be a JSON object.")
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
