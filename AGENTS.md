# AGENTS.md

This repository is the HFH Internal CRM Telegram bot. It is a Python + aiogram 3 project with async SQLAlchemy models and SQLite-backed state. The main product is a role-based internal tool for dispatchers, couriers, laundry staff, and admins.

## Project map

- `main.py`: boots the bot, registers routers, initializes database and middleware.
- `config.py`: environment settings and admin IDs.
- `database/`: SQLAlchemy models and session helpers.
- `handlers/`: bot business logic by role.
  - `handlers/admin.py`: staff approval, admin role management, access checks.
  - `handlers/dispatcher.py`: order creation and dispatcher workflows.
  - `handlers/courier.py`: courier task flow and geolocation handling.
  - `handlers/laundry.py`: laundry intake and completion flow.
  - `handlers/common.py`: shared role helpers, keyboards, callback validation.
- `tests/`: unit tests covering common logic, database, geocoding, handlers, and geo behavior.

## How to work in this repo

- Keep the bot role-aware: admin, dispatcher, courier, and laundry actions are intentionally separated.
- Respect the existing callback-based UI patterns. Inline buttons are part of the workflow, not decoration.
- Always validate permission gates before acting on admin or staff commands.
- Prefer the existing database session pattern and commit writes explicitly after mutation.
- Maintain Russian-language UX strings where the app already uses them.

## Commands and validation

Run the project checks before finishing changes:

```bash
python -m unittest discover -s tests
python -m compileall -q .
```

Typical local setup:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## Admin-panel conventions and risks

The admin area is a high-risk part of this project because it governs user approval, role assignment, and access control. When making changes here, verify all of the following:

- Only configured admin IDs can access admin functions.
- Callback data is parsed safely and rejected on malformed input.
- Role assignment is idempotent and does not re-approve already approved users.
- Buttons and admin flows remain consistent with the role keyboard and inline markup.
- Staff management messages and notifications remain understandable and non-destructive.

## Change guidelines for agents

- Do not add broad or hidden permissions; keep access checks explicit and centralized.
- If you touch admin controls, test callback flows and error states as part of the change.
- If a bug is reported in a button or workflow, reproduce it in the relevant handler and fix the root cause rather than layering temporary patches.
- Keep patches narrow and consistent with the existing aiogram patterns.
- Prefer creating or updating tests for new behavior when the change affects handlers or admin logic.

## Files to read first for admin-related work

- `handlers/admin.py`
- `handlers/common.py`
- `config.py`
- `database/models.py`
- `tests/test_handlers.py`

## Non-goals

- This project does not have a web UI; behavior is Telegram-driven and command/button-driven.
- Do not introduce new frameworks or storage backends unless the existing architecture explicitly requires them.
- Avoid changing the bot’s role model or approval flow without examining the surrounding admin logic first.
