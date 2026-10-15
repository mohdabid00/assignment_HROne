"""
Employee Attendance & Analytics API - Phase 3 Implementation

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Configure PyMongo with deliberate timezone awareness and connection timeout
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

MIN_EPOCH_MILLIS = 100_000_000_000      # ~1973-03-03
MAX_EPOCH_MILLIS = 4_102_444_800_000    # 2100-01-01


# --------------------------------------------------------------------------- #
# Strict Timestamp Validation
# --------------------------------------------------------------------------- #
def validate_strict_epoch_millis(v: Any) -> Any:
    """
    Validates integer epoch milliseconds per contract:
    - Rejects booleans, floats, strings, and other non-integers.
    - Enforces bounds [100000000000, 4102444800000].
    - Null / None is preserved for optional/nullable fields.
    """
    if v is None:
        return None
    # In Python, bool is a subclass of int, so isinstance(True, int) is True.
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Timestamp must be an integer count of epoch milliseconds (strings, floats, and booleans rejected)")
    if v < MIN_EPOCH_MILLIS or v > MAX_EPOCH_MILLIS:
        raise ValueError(f"Timestamp out of bounds [{MIN_EPOCH_MILLIS}, {MAX_EPOCH_MILLIS}]")
    return v


StrictEpochMillis = Annotated[int, BeforeValidator(validate_strict_epoch_millis)]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_employee(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            datetime.strptime(self.joined_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date in YYYY-MM-DD format")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictEpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictEpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictEpochMillis] = None
    punch_out: Optional[StrictEpochMillis] = None


# --------------------------------------------------------------------------- #
# Helpers: Timezone, Instants, and Business Rules R1-R5
# --------------------------------------------------------------------------- #
def truncate_instant_ms(epoch_ms: int) -> int:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    return (epoch_ms // 1000) * 1000


def get_current_epoch_ms() -> int:
    """Returns current server time as truncated whole-second epoch milliseconds."""
    now = datetime.now(timezone.utc)
    return int(now.timestamp()) * 1000


def epoch_ms_to_utc_dt(epoch_ms: int) -> datetime:
    """
    Converts epoch milliseconds into a timezone-aware UTC datetime.
    Truncates sub-second precision to whole seconds per R1.
    """
    sec = epoch_ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Converts a BSON or Python datetime into integer epoch milliseconds.
    Safely handles naive and timezone-aware datetimes.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def utc_to_ist_dt(dt: datetime) -> datetime:
    """Converts a UTC datetime (or naive assumed UTC) to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def parse_shift_time(time_str: str) -> tuple[int, int]:
    """Parses 'HH:MM' string into (hour, minute)."""
    h, m = map(int, time_str.split(":"))
    return h, m


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Overnight shifts have shift_end <= shift_start (e.g. 22:00 to 06:00)."""
    return shift_end <= shift_start


def get_shift_instants(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """
    Returns (start_ist, end_ist) timezone-aware datetimes in IST for a given shift date.
    R1: For overnight shifts, shift_end falls on the next calendar day.
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    sh, sm = parse_shift_time(shift_start)
    eh, em = parse_shift_time(shift_end)
    start_ist = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)
    if is_overnight_shift(shift_start, shift_end):
        end_date = d + timedelta(days=1)
    else:
        end_date = d
    end_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    return start_ist, end_ist


def derive_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start):
      a punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = utc_to_ist_dt(punch_in_utc)
    punch_in_time_sec = punch_in_ist.strftime("%H:%M:%S")
    shift_end_sec = f"{shift_end}:00"

    if is_overnight_shift(shift_start, shift_end) and punch_in_time_sec < shift_end_sec:
        shift_date = punch_in_ist.date() - timedelta(days=1)
    else:
        shift_date = punch_in_ist.date()
    return shift_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R2: Late calculation.
    Grace period 10 minutes.
    Late only if the punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since the grace period ended.
    Shift 09:30: 09:40:00 gives 0; 09:40:01 gives 10; 10:15:59 gives 45.
    """
    punch_in_ist = utc_to_ist_dt(punch_in_utc)
    start_ist, _ = get_shift_instants(date_str, shift_start, shift_end)
    diff_sec = int((punch_in_ist - start_ist).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R3: Overtime.
    Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    punch_out_ist = utc_to_ist_dt(punch_out_utc)
    _, end_ist = get_shift_instants(date_str, shift_start, shift_end)
    diff_sec = int((punch_out_ist - end_ist).total_seconds())
    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    Avoids Python's built-in round() (Banker's rounding).
    """
    duration_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    hours_dec = Decimal(duration_sec) / Decimal(3600)
    rounded = hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: Half day.
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def validate_punch_out_bounds(punch_in_utc: datetime, punch_out_utc: datetime) -> None:
    """
    Validates punch-out instant against punch-in:
    422 if punched_at <= punch_in or more than 24 hours after it.
    """
    duration_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if duration_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
    if duration_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot exceed 24 hours from punch_in")


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_epoch_ms(to_val)
        changes[field] = {"from": from_val, "to": to_val}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = dt_to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def serialize_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Database Index Initialization
# --------------------------------------------------------------------------- #
def init_indexes(target_db=None):
    """
    Initializes all required indexes idempotently on employees and attendance_logs collections.
    Fails clearly if index creation encounters conflicting options or unrecoverable error.
    """
    database = target_db if target_db is not None else db
    try:
        # 1. employees indexes
        database.employees.create_index([("emp_code", 1)], unique=True, name="idx_emp_code_unique")
        database.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_emp_dept_code")
        database.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)], name="idx_emp_dept_joined")
        database.employees.create_index([("joined_on", 1)], name="idx_emp_joined_on")

        # 2. attendance_logs indexes
        database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="idx_att_emp_date_unique")
        database.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_att_date_desc_emp_asc")
        database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_att_emp_punch_in_desc")
        database.attendance_logs.create_index([("date", 1), ("late_minutes", -1), ("emp_code", 1)], name="idx_att_date_late_emp")
        database.attendance_logs.create_index([("date", 1), ("status", 1), ("emp_code", 1)], name="idx_att_date_status_emp")
    except PyMongoError as exc:
        raise RuntimeError(f"Database index initialization failed: {exc}") from exc


# Initialize indexes at startup
try:
    init_indexes(db)
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Application Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize indexes idempotently at startup
    try:
        init_indexes(db)
    except Exception as e:
        raise RuntimeError(f"Startup index creation failed: {e}") from e
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": f"Database unavailable: {str(e)}"},
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_at_ms = truncate_instant_ms(body.punched_at)
    else:
        punched_at_ms = get_current_epoch_ms()

    punch_in_utc = epoch_ms_to_utc_dt(punched_at_ms)
    att_date = derive_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_utc, emp["shift_start"], emp["shift_end"], att_date)

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}):
        raise HTTPException(status_code=409, detail="already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_at_ms = truncate_instant_ms(body.punched_at)
    else:
        punched_at_ms = get_current_epoch_ms()

    punch_out_utc = epoch_ms_to_utc_dt(punched_at_ms)

    # Find candidate record where punch_in <= punch_out_utc
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )

    if not rec:
        # Check if there is an existing record with punch_in > punch_out_utc
        future_rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punch_out_utc}},
            sort=[("punch_in", 1)],
        )
        if future_rec:
            raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
        raise HTTPException(status_code=404, detail="no punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    validate_punch_out_bounds(rec["punch_in"], punch_out_utc)

    work_hours = compute_work_hours(rec["punch_in"], punch_out_utc)
    half_day = compute_half_day(work_hours)
    overtime_minutes = compute_overtime(punch_out_utc, emp["shift_start"], emp["shift_end"], rec["date"])

    # Atomic conditional update
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": rec["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return serialize_attendance_doc(updated)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="date_from must be a valid YYYY-MM-DD date")
    if date_to:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="date_to must be a valid YYYY-MM-DD date")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
    if status and status not in valid_statuses:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(valid_statuses)}")

    q = build_attendance_list_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date in path must be a valid YYYY-MM-DD date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    MAX_RETRIES = 5
    for _ in range(MAX_RETRIES):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not rec:
            raise HTTPException(status_code=404, detail="Attendance record not found for this date")

        old_status = rec["status"]
        old_punch_in = rec.get("punch_in")
        old_punch_out = rec.get("punch_out")
        old_work_hours = rec.get("work_hours")
        old_late_minutes = rec.get("late_minutes", 0)
        old_overtime_minutes = rec.get("overtime_minutes", 0)
        old_half_day = rec.get("half_day", False)

        # Status and punch time consistency checks
        if body.status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE")
            new_status = body.status
            new_punch_in = None
            new_punch_out = None
        else:
            new_status = body.status if body.status is not None else old_status

            if body.punch_in is not None:
                new_punch_in = epoch_ms_to_utc_dt(truncate_instant_ms(body.punch_in))
            else:
                new_punch_in = old_punch_in

            if body.punch_out is not None:
                new_punch_out = epoch_ms_to_utc_dt(truncate_instant_ms(body.punch_out))
            else:
                new_punch_out = old_punch_out

            if new_status in ("ABSENT", "LEAVE"):
                if body.punch_in is not None or body.punch_out is not None:
                    raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out for ABSENT or LEAVE records")
                new_punch_in = None
                new_punch_out = None
            else:
                # Presence status requires punch_in
                if new_punch_in is None:
                    raise HTTPException(status_code=422, detail="Presence status requires a punch_in instant")

                # punch_in must stay on the record's attendance date per R1
                computed_date = derive_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
                if computed_date != date:
                    raise HTTPException(status_code=422, detail=f"punch_in instant belongs to attendance date {computed_date}, not {date}")

                if new_punch_out is not None:
                    validate_punch_out_bounds(new_punch_in, new_punch_out)

        # Recompute derived fields
        if new_status in ("ABSENT", "LEAVE"):
            new_work_hours = None
            new_late_minutes = 0
            new_overtime_minutes = 0
            new_half_day = False
        else:
            new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"], date)
            if new_punch_out is not None:
                new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
                new_half_day = compute_half_day(new_work_hours)
                new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            else:
                new_work_hours = None
                new_half_day = False
                new_overtime_minutes = 0

        # Determine changes
        changes = {}
        if new_status != old_status:
            changes["status"] = {"from": old_status, "to": new_status}
        if new_punch_in != old_punch_in:
            changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
        if new_punch_out != old_punch_out:
            changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
        if new_work_hours != old_work_hours:
            changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
        if new_late_minutes != old_late_minutes:
            changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
        if new_overtime_minutes != old_overtime_minutes:
            changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
        if new_half_day != old_half_day:
            changes["half_day"] = {"from": old_half_day, "to": new_half_day}

        if not changes:
            raise HTTPException(status_code=422, detail="Request produces no actual changes to the attendance record")

        history_entry = {
            "at": datetime.now(timezone.utc).replace(microsecond=0),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        # Optimistic Concurrency Control (OCC) based on history length
        cur_history_len = len(rec.get("history", []))
        condition = {
            "emp_code": emp_code,
            "date": date,
            f"history.{cur_history_len}": {"$exists": False},
        }
        if cur_history_len > 0:
            condition[f"history.{cur_history_len - 1}"] = {"$exists": True}

        update_op = {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        }

        updated = db.attendance_logs.find_one_and_update(condition, update_op, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return serialize_attendance_doc(updated)

    raise HTTPException(status_code=409, detail="Concurrent modification detected; please retry")


# --------------------------------------------------------------------------- #
# Analytics Pipeline Builders & Helpers
# --------------------------------------------------------------------------- #
def validate_month_format(month_str: str) -> None:
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month_str):
        raise HTTPException(status_code=422, detail="month must be in YYYY-MM format")


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_weekday": {
                    "$lte": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        5,
                    ]
                },
                "is_presence": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]],
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_presence"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage: dict = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": month_start, "$lte": month_end}
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}}, 5]},
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
                "work_hours_records": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$log.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$work_hours_records",
                            "as": "r",
                            "in": "$$r.work_hours",
                        }
                    }
                },
                "work_hours_count": {"$size": "$work_hours_records"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "dept_work_hours_sum": {"$sum": "$work_hours_sum"},
                "dept_work_hours_count": {"$sum": "$work_hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$dept_work_hours_count", 0]},
                        {"$round": [{"$divide": ["$dept_work_hours_sum", "$dept_work_hours_count"]}, 2]},
                        None,
                    ]
                },
                "dept_work_hours_sum": 1,
                "dept_work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp.emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$emp.emp_code",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    f_d = datetime.fromisoformat(f"{from_date}T00:00:00+00:00")
    t_d = datetime.fromisoformat(f"{to_date}T00:00:00+00:00") + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [f_d, t_d],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        5,
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    joined_on = employee["joined_on"]

    working_days = 0
    for day in range(1, num_days + 1):
        day_str = f"{year:04d}-{m:02d}-{day:02d}"
        if day_str >= joined_on:
            if datetime(year, m, day).weekday() < 5:  # Mon=0, Fri=4
                working_days += 1

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_results = list(db.attendance_logs.aggregate(pipeline))

    if agg_results:
        agg = agg_results[0]
        present_days = float(Decimal(str(agg.get("present_days", 0.0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal(100)
        attendance_pct = float(pct.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for item in raw_items:
        if item.get("dept_work_hours_count", 0) > 0:
            avg_dec = Decimal(str(item["dept_work_hours_sum"])) / Decimal(str(item["dept_work_hours_count"]))
            avg_work_hours = float(avg_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            avg_work_hours = None

        present_days = float(Decimal(str(item.get("present_days", 0.0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        items.append({
            "department": item["department"],
            "headcount": item["headcount"],
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": item["late_count"],
            "total_late_minutes": item["total_late_minutes"],
            "leave_count": item["leave_count"],
            "on_duty_count": item["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department:path}/trend")
def get_department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        f_date = date.fromisoformat(from_date)
        t_date = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must be in valid YYYY-MM-DD format")

    if t_date < f_date:
        raise HTTPException(status_code=422, detail="'to' date cannot be earlier than 'from' date")

    if (t_date - f_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for it in raw_items:
        att_rate = it.get("attendance_rate")
        if att_rate is not None:
            att_rate = float(Decimal(str(att_rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        mov_avg = it.get("moving_avg_7d")
        if mov_avg is not None:
            mov_avg = float(Decimal(str(mov_avg)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        pres_count = float(Decimal(str(it.get("present_count", 0.0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        items.append({
            "date": it["date"],
            "is_working_day": it["is_working_day"],
            "headcount": int(it["headcount"]),
            "present_count": pres_count,
            "late_count": int(it["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })
    return {"department": department, "items": items}



# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
ExplainEndpointName = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpointName,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(status_code=422, detail="date_from must be a valid YYYY-MM-DD date")
        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(status_code=422, detail="date_to must be a valid YYYY-MM-DD date")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

        valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
        if status and status not in valid_statuses:
            raise HTTPException(status_code=422, detail=f"status must be one of {sorted(valid_statuses)}")

        q = build_attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")
        validate_month_format(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        validate_month_format(month)
        pipeline = build_department_summary_pipeline(month, department)
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        validate_month_format(month)
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from and to dates are required for department_trend")
        try:
            f_d = date.fromisoformat(from_date)
            t_d = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="from and to must be valid YYYY-MM-DD dates")
        if t_d < f_d:
            raise HTTPException(status_code=422, detail="'to' date cannot be earlier than 'from' date")
        if (t_d - f_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    else:
        raise HTTPException(status_code=422, detail="Invalid endpoint name")

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": json.loads(json_util.dumps(explain_doc)),
    }
