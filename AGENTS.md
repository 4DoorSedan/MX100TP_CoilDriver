# AGENTS.md

## 1. User Interface & Plotting
- **Framework**: Use `dearpygui` for the user interface.
- **Plotting**: Use native Dear PyGui plots and series (`add_plot`, `add_line_series`, etc.). Do not pull in heavy third-party plotting libraries like Matplotlib or Qt.
- **Decoupling**: Keep domain logic and calculations separate from GUI code so core features can run headlessly.

---

## 2. Testing Standards
- **Framework**: Use `pytest`.
- **Directory Layout**: All tests must live in a dedicated `tests/` directory separate from the application source code.
- **Test Coverage**: Write automated unit tests for all core logic, data processing, and math.
- **Verification**: Run `pytest` and confirm all tests pass before completing any task or refactor.

---

## 3. Documentation & README
- **Living Documentation**: Keep `README.md` accurate and up to date with every change.
- **README Requirements**:
  - Clear overview of what the tool does.
  - Setup and installation instructions.
  - How to run the application.
  - How to execute the test suite.
- **Code Comments**: Include clear docstrings for non-trivial functions and modules.
