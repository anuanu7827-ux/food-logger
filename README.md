# Food Logger

A small Flask + SQLite app for logging food by barcode, with daily/weekly nutrition summaries, calorie goals with alerts, and an accessible large-button UI.

## Features
- Barcode lookup (browser camera or typed) via Open Food Facts, cached locally
- Manual food entry when a product isn't found
- Daily and weekly summaries: calories, carbs, protein, fat
- Daily calorie goal with warning and over-limit alerts (once per threshold per day)
- Max 3 logs of the same food per day (race-safe), undo support
- No duplicate timestamps; idempotency keys make client retries safe
- Retrieve up to 1 year of logs by date range, paginated
- Accessible UI: large touch targets, 20px text, high contrast, ARIA labels, table view of chart data

## Run
```bash
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000 (Chrome or Edge for camera scanning).

Demo barcodes: `1234567890123`, `1111111111116`, `2222222222222`.

## API
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/foods/barcode/<code>` | Look up (and cache) a food |
| POST | `/api/foods` | Add a food manually |
| POST | `/api/logs` | Log a food (`food_id`, `servings`, `idempotency_key`) |
| DELETE | `/api/logs/<id>` | Undo a log |
| GET | `/api/logs?from=&to=` | Logs in a date range (max 1 year) |
| GET | `/api/summary?range=day\|week` | Totals, per-day data, goal status |
| GET/PUT | `/api/goal` | Read or set calorie goal |

The client sends its timezone in an `X-TZ` header so "day" means the user's local day.

## Notes
This is a demo with a single user and SQLite. For production, add authentication, use Postgres (partition logs by month), and send push notifications from a background worker.

## License
MIT
