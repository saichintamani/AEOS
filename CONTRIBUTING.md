# Contributing to AEOS

Thank you for your interest in contributing to AEOS!

## Quick Start

```bash
git clone https://github.com/your-org/aeos
cd aeos
pip install -e ".[dev]"
make test
```

## Development Workflow

1. **Fork** the repository
2. **Branch** from `main`: `git checkout -b feat/your-feature`
3. **Write tests** before or alongside your code
4. **Run tests**: `make test`
5. **Lint**: `make lint`
6. **Open a PR** against `main`

## Code Standards

- Python 3.11+
- All new code must have tests
- Async-first: use `asyncio`, not threads for I/O
- Type hints on all public functions
- No print statements — use structured logging (`get_logger(__name__)`)
- Follow existing module patterns (see `app/distributed/` for examples)

## Architecture Guidelines

Before adding a new subsystem, read:
- `docs/architecture/000-VISION.md`
- `docs/architecture/014-ARCHITECTURE_DECISION_RECORDS.md`
- `ARCHITECTURE_CONSTITUTION.md`

If you're adding distributed functionality, ensure:
- Invariants are registered with the InvariantEngine
- State machines are defined in `app/distributed/validation/state_machine.py`
- Protocol steps are traced via ProtocolValidator

## Test Structure

```
tests/
  unit/           # Pure unit tests (no I/O, no server)
  integration/    # Tests requiring Redis/Kafka/etc.
  unit/validation/  # Invariant + protocol + state machine tests
```

Run specific suites:
```bash
make test-unit
make test-validation
make test-integration   # Requires running infrastructure
```

## Submitting Issues

Use GitHub Issues. Include:
- AEOS version (`aeos version`)
- Python version
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs

## License

By contributing, you agree your contributions will be licensed under Apache 2.0.
