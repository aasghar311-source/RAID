"""Run every raid.tests.test_* module. Usage (from backend/): python -m raid.tests.run_all"""

import importlib
import pkgutil
import traceback

import raid.tests as tests_pkg


def main() -> int:
    total = passed = 0
    failures: list[str] = []
    for mod in pkgutil.iter_modules(tests_pkg.__path__):
        if not mod.name.startswith("test_"):
            continue
        m = importlib.import_module(f"raid.tests.{mod.name}")
        for name in sorted(vars(m)):
            fn = getattr(m, name)
            if name.startswith("test_") and callable(fn):
                total += 1
                try:
                    fn()
                    passed += 1
                except Exception:  # noqa: BLE001
                    failures.append(f"{mod.name}.{name}")
                    print(f"  FAIL {mod.name}.{name}")
                    traceback.print_exc()
    print(f"\n{passed}/{total} raid unit tests passed")
    if failures:
        print("FAILURES:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
