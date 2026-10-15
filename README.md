# Employee Attendance & Analytics API

Backend service for employee attendance tracking and analytics, built with FastAPI, PyMongo, and MongoDB.

## Prerequisites

- Python 3.10+ (tested on Python 3.13)
- MongoDB 6.0+ running locally or remotely

## Setup and Running

1. **Clone the repository and enter the directory**:
   ```bash
   cd candidate_kit
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   By default, the app connects to MongoDB at `mongodb://localhost:27017` and uses the database `attendance_db`. You can override these with environment variables:
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```
   A `.env` file in the root directory is also loaded automatically if present.

5. **Start the API server**:
   ```bash
   python -m uvicorn app.main:app --port 8000
   ```
   The interactive API documentation will be available at `http://localhost:8000/docs`.

## Running Tests

All automated tests use dedicated, temporary test databases that clean up after themselves.

To run the full test suite:
```bash
python -m pytest tests/ -v
```

Test suite breakdown:
- `tests/test_phase2.py`: Timestamp validation, timezone conversions, and business rules R1–R5 (overnight shifts, grace periods, overtime, half-up rounding, half-day).
- `tests/test_phase3.py`: Index initialization, health probe, employee CRUD, pagination, and punch-in.
- `tests/test_phase4.py`: Atomic punch-out, 24-hour shift limits, concurrency, and manual regularization history.
- `tests/test_phase5.py`: Analytics aggregations (monthly employee summary, department summary, late leaderboard, department trend with gap-filling), and index explain verification.

## Architecture Notes

- **Single Application File**: All application code lives in `app/main.py` as specified by the assignment.
- **Automatic Indexing**: Database indexes are built automatically on startup via FastAPI's lifespan event.
- **Concurrency Safety**:
  - `POST /employees`: Backed by a unique index on `emp_code`. Duplicate creates return HTTP 409.
  - `POST /attendance/punch-in`: Backed by a unique compound index on `(emp_code, date)`. Competing requests return HTTP 409.
  - `POST /attendance/punch-out`: Uses atomic `find_one_and_update` on open records to prevent race conditions.
  - `PATCH /attendance/{emp_code}/{date}`: Uses optimistic concurrency control matching the audit history length to avoid lost updates.
- **Server-Side Aggregations**: All analytics queries run directly inside MongoDB aggregation pipelines using `$lookup`, `$densify`, and `$setWindowFields` rather than loading collections into Python memory.
- **No Database IDs in the API**: The API addresses employees by `emp_code` and attendance records by `(emp_code, date)`. MongoDB's internal `_id` is never exposed.

## Available Endpoints

- `GET /health`: Checks database connectivity.
- `POST /employees`: Create a new employee.
- `GET /employees`: Paginated employee list sorted by `emp_code`.
- `POST /attendance/punch-in`: Record a punch-in.
- `GET /attendance`: Paginated attendance list sorted by date descending.
- `POST /attendance/punch-out`: Record a punch-out.
- `PATCH /attendance/{emp_code}/{date}`: Regularize an attendance record.
- `GET /analytics/employees/{emp_code}/monthly`: Monthly attendance stats for one employee.
- `GET /analytics/departments/summary`: Monthly metrics aggregated by department.
- `GET /analytics/leaderboard/late`: Top late employees using competition ranking.
- `GET /analytics/departments/{department}/trend`: Daily attendance trend with 7-day moving average.
- `GET /admin/explain/{endpoint}`: MongoDB execution plan inspection.
