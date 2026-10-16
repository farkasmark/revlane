# Python idioms review

Reviewed commit: `5b68b73`  
Review date: 2026-10-16

## Assessment

The codebase is mostly idiomatic Python. Its dataclasses, async context
managers, keyword-only options, transport abstraction, logging, and package
layout are sensible. A wholesale rewrite would add little value.

The findings below distinguish concrete bugs from improvements to API design,
exception handling, typing, and formatting. The review did not modify application
code.

## Findings

### 1. Bug: Monitor overwrites a Thread method

Location: [`qcar/web/server.py`](../qcar/web/server.py), `Monitor.__init__()`.

`Monitor` subclasses `threading.Thread` but assigns a `threading.Event` to
`self._stop`, colliding with the superclass's internal `_stop()` method.
Calling `join()` after stopping the monitor was reproduced raising:

```text
TypeError: 'Event' object is not callable
```

Rename the event to `_stop_event` and update its uses. Avoid colliding with
superclass internals.

### 2. Bug: HTTP handlers share instance configuration globally

Location: [`qcar/web/server.py`](../qcar/web/server.py), `WebControl.__init__()`.

Each server assigns its controller and verbosity to the shared `Handler` class:

```python
Handler.controller = self.controller
Handler.verbose = verbose
```

Creating a second server replaces the configuration used by the first server's
handlers. Both servers consequently use the newest controller.

Bind configuration to the server instance, or create a separate handler subclass
for each server.

### 3. Exception handling: backend absorbs BaseException

Location: [`qcar/gui/backend.py`](../qcar/gui/backend.py), `CarBackend._main()`.

The action handler catches `BaseException` and continues after recording an
error. This includes cancellation and termination signals, not just ordinary
operation failures.

Catch normal failures with `Exception` and explicitly propagate cancellation.
Distinguish this from cleanup handlers that catch a broad exception, release
resources, and immediately re-raise it.

### 4. Public API: connection validation uses assert

Location: [`qcar/car.py`](../qcar/car.py), `QCar.write()` and `QCar.write_raw()`.

Both methods use `assert self._radio is not None` to validate connection state.
An unconnected `write_raw()` was reproduced raising `AssertionError`. Python's
optimized mode removes assertion checks, so this also gives different failure
behavior depending on interpreter options.

Raise an explicit `ConnectionError` for this public API precondition. Reserve
assertions for internal invariants.

### 5. Import behavior: entry point runs on import

Location: [`main.py`](../main.py), module-level platform dispatch.

Importing the module runs either the Android application or CLI immediately.
This makes importing entry-point helpers for tests or tooling unsafe.

Move dispatch into a function and invoke it under:

```python
if __name__ == "__main__":
    main()
```

### 6. Typing: stable interfaces are unnecessarily implicit

Locations include [`qcar/gui/base.py`](../qcar/gui/base.py),
[`qcar/web/server.py`](../qcar/web/server.py), and
[`qcar/radio_android.py`](../qcar/radio_android.py).

Backend, widget, and bridge interfaces use bare dictionaries, untyped collections,
and defensive `getattr()` calls. These patterns are valid Python, but obscure
the contracts between components whose interfaces are known within the project.

Type stable internal interfaces. For example, a small `Protocol` can describe
the backend consumed by the web controller and implemented by both the real
backend and test doubles. Localize unavoidable JNI dynamism rather than forcing
static types onto every Java object.

## Choices that do not require refactoring

- Duck typing, callbacks, abstract base classes, and platform-specific lazy
  imports are idiomatic Python. They do not need replacing merely to look more
  formal.
- Different watchdogs are not automatically duplication: input freshness, GUI
  liveness, web-client liveness, and link recovery address different boundaries.
- A `src/` layout, enums, and splitting the CLI are optional design choices,
  not requirements for good Python.
- Broad exception suppression can be appropriate during best-effort cleanup.
  Silently suppressing ordinary operational failures makes diagnosis harder.

## Validation

The following read-only checks were run:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m ruff check --no-cache \
    qcar tests main.py assets/branding/make_artwork.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m ruff format --check --no-cache \
    qcar tests main.py assets/branding/make_artwork.py
```

- Configured Ruff lint checks passed.
- Ruff's formatter would change 24 of the 39 inspected files, mostly wrapping
  and spacing. Formatter adoption is a separate style decision; these
  differences do not establish incorrect Python or a violation of an existing
  formatter requirement.
- Small reproductions confirmed the `Monitor.join()` failure and the
  unconnected-write `AssertionError`.
- No application test suite or real-device BLE validation was performed as part
  of this idioms review.

## Suggested order

Fix the two concrete bugs first, then improve exception boundaries and interface
typing. Keep any formatter adoption separate from behavior changes.
