# Architectural Decisions

1. **Indexes**: We put unique indexes on `employees.emp_code` and `attendance_logs(emp_code, date)` to lock down natural keys. For query speed, we indexed `(date, emp_code)` for pagination, `(emp_code, punch_in)` for punch-outs, and `(date, late_minutes, emp_code)` for covered leaderboard queries. We considered `(emp_code, date, status)` but rejected it since `(emp_code, date)` is already unique.

2. **Punch-in race**: When two identical punch-in requests hit simultaneously, both pass validation and call `insert_one`. MongoDB's unique index on `(emp_code, date)` allows only one write. The losing request raises a `DuplicateKeyError`, which we catch and return as HTTP 409 Conflict.

3. **Leaderboard ties**: We use MongoDB's `$rank` window operator for standard competition ranking (1, 2, 2, 4). Filtering `rank <= limit` happens after ranking, so all employees tied at the cutoff rank are returned, sorted by `emp_code` ascending.

4. **Headcount**: The department summary pipeline starts on `employees` (matching `joined_on <= month_end`) and `$lookup`s into `attendance_logs`. Starting on employees ensures anyone with zero logs is still counted in headcount.

5. **100x scale**: At 10 million rows, computing aggregations on the fly across collections will lag. We would maintain pre-aggregated daily and monthly rollup collections updated via MongoDB Change Streams.
