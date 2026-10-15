from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import random
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

from sysbench_sql_runner import lua_long_string, split_sql_statements


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str,
                               allow_nan=False), encoding="utf-8")
    temp.replace(path)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def code_only(sql):
    """Mask comments and literals for conservative statement checks."""
    pattern = r"'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`(?:``|[^`])*`|/\*.*?\*/|\#.*?$|--(?=\s|$).*?$"
    return re.sub(pattern, " ", sql, flags=re.S | re.M)


def validate_select(sql):
    parts = [p for p in split_sql_statements(sql) if code_only(p).strip()]
    if len(parts) != 1 or not re.match(r"^\s*(SELECT|WITH)\b", code_only(parts[0]), re.I):
        raise ValueError("Cada caso precisa conter um unico SELECT ou WITH ... SELECT.")
    if re.search(r"\b(INTO\s+(OUTFILE|DUMPFILE)|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE)\b", code_only(sql), re.I):
        raise ValueError("Este benchmark e somente-leitura, sem exportacao ou locking reads.")
    if re.match(r"^\s*WITH\b", code_only(sql), re.I) and re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", code_only(sql), re.I):
        raise ValueError("CTE de escrita nao e aceita.")
    if re.search(r"SET_VAR\s*\(\s*use_secondary_engine\s*=", sql, re.I):
        raise ValueError("Remova o hint SET_VAR(use_secondary_engine): o destino sera controlado pelo runner.")
    return parts[0]


def load_workload(sql_file, case_count=1, seed=42, cases_file=None):
    source = Path(sql_file).read_text(encoding="utf-8")
    statements = [s for s in split_sql_statements(source) if code_only(s).strip()]
    explicit = json.loads(Path(cases_file).read_text()) if cases_file else {}
    if not isinstance(explicit, dict):
        raise ValueError("O arquivo de casos precisa ser um objeto JSON com chaves Q01, Q02...")
    queries = []
    token = re.compile(r"\{\{RAND_INT:([\w]+):(\d+):(\d+)\}\}")
    for index, statement in enumerate(statements, 1):
        validate_select(statement)
        qid = f"Q{index:02d}"
        cases = []
        if qid in explicit:
            if not isinstance(explicit[qid], list) or not explicit[qid]:
                raise ValueError(f"{qid}: informe uma lista nao vazia de casos.")
            for number, case in enumerate(explicit[qid], 1):
                sql = validate_select(case["sql"])
                if "{{" in sql:
                    raise ValueError(f"{qid}: o SQL do caso precisa ter valores concretos.")
                cases.append({"id": f"C{number:02d}", "name": case.get("name", f"Caso {number}"), "sql": sql})
        else:
            rng = random.Random(int(sha(statement + str(seed)), 16))
            for number in range(1, (case_count if token.search(statement) else 1) + 1):
                values = {}
                def replace(match):
                    name, low, high = match.groups()
                    bounds = (int(low), int(high))
                    if bounds[0] > bounds[1]:
                        raise ValueError(f"{qid}: faixa RAND_INT invalida.")
                    if name in values and values[name][0] != bounds:
                        raise ValueError(f"{qid}: token {name} tem faixas diferentes.")
                    if name not in values:
                        values[name] = (bounds, rng.randint(*bounds))
                    return str(values[name][1])
                sql = token.sub(replace, statement)
                if "{{" in sql:
                    raise ValueError(f"{qid}: token nao suportado.")
                cases.append({"id": f"C{number:02d}", "name": f"Caso {number}", "sql": sql,
                              "parameters": {k: v[1] for k, v in values.items()}})
        warnings = []
        if any(re.search(r"\b(RAND|UUID|NOW|SYSDATE|CURRENT_TIMESTAMP)\b", code_only(c["sql"]), re.I) for c in cases):
            warnings.append("SQL usa funcao nao deterministica; os resultados podem variar mesmo com casos fixos.")
        queries.append({"id": qid, "template": statement, "cases": cases, "warnings": warnings})
    if not queries:
        raise ValueError("Nenhuma query encontrada.")
    unknown = set(explicit) - {q["id"] for q in queries}
    if unknown:
        raise ValueError(f"Casos sem query correspondente: {sorted(unknown)}")
    return {"queries": queries, "seed": seed,
            "hash": sha(json.dumps(queries, sort_keys=True, ensure_ascii=False))}


def percentile(values, p):
    if not values:
        return None
    return values[max(0, math.ceil(len(values) * p / 100) - 1)]


def latency_metrics(values):
    ordered = sorted(values)
    return {"samples": len(ordered), "min_ms": ordered[0] if ordered else None,
            "avg_ms": statistics.fmean(ordered) if ordered else None,
            "p50_ms": percentile(ordered, 50), "p95_ms": percentile(ordered, 95),
            "p99_ms": percentile(ordered, 99), "max_ms": ordered[-1] if ordered else None}


def counter_delta(before, after):
    delta, resets = {}, []
    def number(value):
        if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"[-+]?\d+", value)):
            return int(value)
        return float(value)
    for key, end in after.items():
        try:
            begin, end = number(before[key]), number(end)
        except (KeyError, ValueError, TypeError):
            continue
        if not math.isfinite(begin) or not math.isfinite(end):
            continue
        if end < begin:
            resets.append(key)
        else:
            delta[key] = end - begin
    return {"values": delta, "decreased_or_reset": resets}


def summarize_samples(directory, threads, events):
    directory = Path(directory)
    fields = ["thread", "connection_id", "sequence", "start_unix", "start_monotonic", "elapsed_ms", "success", "rows_returned", "error_code", "sql_state", "error"]
    values, seen, failures, bad, rows = [], set(), [], 0, 0
    first, last, mono_first, mono_last = None, None, None, None
    timeline = {}
    quotas = {i: events // threads + int(i < events % threads) for i in range(threads)} if events else None
    counts = {i: 0 for i in range(threads)}
    returned_rows, returned_available, empty_results = 0, True, 0
    with gzip.open(directory / "client_samples.csv.gz", "wt", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for file in sorted(directory.glob("thread-*.csv")):
            with file.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    try:
                        tid, seq = int(row["thread"]), int(row["sequence"])
                        start, elapsed = float(row["start_unix"]), float(row["elapsed_ms"])
                        monotonic = float(row.get("start_monotonic") or start)
                        ok = row["success"] == "1"
                        if tid not in counts or seq < 1 or elapsed < 0 or not math.isfinite(elapsed) or not math.isfinite(start) or not math.isfinite(monotonic):
                            raise ValueError("invalid sample")
                        if (tid, seq) in seen:
                            raise ValueError("duplicate sample")
                        seen.add((tid, seq))
                        counts[tid] += 1
                        end = start + elapsed / 1000
                        first = start if first is None else min(first, start)
                        last = end if last is None else max(last, end)
                        mono_first = monotonic if mono_first is None else min(mono_first, monotonic)
                        mono_last = monotonic + elapsed / 1000 if mono_last is None else max(mono_last, monotonic + elapsed / 1000)
                        bucket = timeline.setdefault(math.floor(end), {"values": [], "errors": 0})
                        if ok:
                            values.append(elapsed)
                            bucket["values"].append(elapsed)
                            if row.get("rows_returned") not in (None, ""):
                                returned_rows += int(row["rows_returned"])
                                empty_results += int(int(row["rows_returned"]) == 0)
                            else:
                                returned_available = False
                        else:
                            bucket["errors"] += 1
                            failures.append(row)
                        writer.writerow({k: row.get(k, "") for k in fields})
                        rows += 1
                    except (ValueError, KeyError, TypeError):
                        bad += 1
    duration = mono_last - mono_first if mono_first is not None and mono_last is not None else 0
    exact = quotas is None or all(counts[i] == quotas[i] and
                                 all((i, n) in seen for n in range(1, quotas[i] + 1)) for i in counts)
    summary = {**latency_metrics(values), "attempts": rows, "successes": len(values),
               "client_rows_returned": returned_rows if returned_available and values else None,
               "empty_result_successes": empty_results if returned_available and values else None,
               "errors": len(failures), "error_examples": failures[:20], "malformed_or_duplicate": bad,
               "per_thread_attempts": counts, "expected_events": events or None,
               "quota_complete": exact, "start_unix": first, "end_unix": last,
               "start_monotonic": mono_first, "end_monotonic": mono_last,
               "query_window_seconds": duration, "qps": len(values) / duration if duration else None,
               "small_tail_sample": len(values) < 1000,
               "timeline": [{"unix": t, **latency_metrics(b["values"]), "errors": b["errors"]}
                            for t, b in sorted(timeline.items())]}
    save_json(directory / "client_summary.json", summary)
    return summary


def warmup_stable(windows, count, tolerance, min_samples, require_io=True):
    if len(windows) < count:
        return False, {"reason": "insufficient_windows"}
    recent = windows[-count:]
    if any(w["client"]["successes"] < min_samples or not w["valid"] for w in recent):
        return False, {"reason": "insufficient_samples_or_errors"}
    checks = {}
    for key, floor in [("p50_ms", 0.01), ("qps", 0.01), ("physical_reads_per_query", 1.0)]:
        if key == "physical_reads_per_query" and not require_io:
            checks[key] = {"required": False, "reason": "InnoDB I/O does not gate analytic/auto warm-up."}
            continue
        vals = [(w["client"].get(key) if key != "physical_reads_per_query" else w.get(key)) for w in recent]
        if any(v is None for v in vals):
            checks[key] = {"available": False}
            if key != "physical_reads_per_query" or require_io:
                return False, {"reason": "missing_metric", "checks": checks}
            continue
        spread = (max(vals) - min(vals)) / max(abs(statistics.fmean(vals)), floor)
        checks[key] = {"available": True, "relative_spread": spread, "values": vals,
                       "stable": spread <= tolerance}
    return all(c.get("stable", True) for c in checks.values()), {"checks": checks}


def build_lua(sql, directory, threads, events, max_execution_ms, engine_setting=None,
              gated=False, duration_seconds=0):
    """Quota per thread, monotonic duration, no random selection or automatic retry."""
    if engine_setting not in (None, "OFF", "ON", "FORCED"):
        raise ValueError("Invalid secondary engine setting")
    setup = (f"con:query(\"SET SESSION use_secondary_engine='{engine_setting}'\")\n"
             "  local _, actual = con:query_row(\"SHOW SESSION VARIABLES LIKE 'use_secondary_engine'\")\n"
             "  metadata:write('use_secondary_engine,' .. csv(actual) .. '\\n')"
             if engine_setting else "metadata:write('use_secondary_engine,unsupported\\n')")
    return f'''local ffi = require("ffi")
ffi.cdef[[
struct timespec {{ long tv_sec; long tv_nsec; }};
int clock_gettime(int clock_id, struct timespec *tp);
int usleep(unsigned int usec);
]]
local CLOCK_MONOTONIC = ffi.os == "OSX" and 6 or 1
local function clock(id)
  local ts = ffi.new("struct timespec")
  assert(ffi.C.clock_gettime(id, ts) == 0, "clock_gettime failed")
  return tonumber(ts.tv_sec) + tonumber(ts.tv_nsec) / 1000000000
end
local query = {lua_long_string(sql)}
local directory = {lua_long_string(str(Path(directory).resolve()))}
local con, samples, sequence, quota, connection_id
local gated = {str(gated).lower()}
local function signal(name)
  local f = assert(io.open(directory .. '/' .. name, 'w')); f:write('ready'); f:close()
end
local function wait_for(name)
  while true do
    local f = io.open(directory .. '/' .. name, 'r')
    if f then f:close(); return end
    ffi.C.usleep(1000)
  end
end
local function csv(value)
  value = tostring(value or ""):gsub("[\\r\\n]", " ")
  return '"' .. value:gsub('"', '""') .. '"'
end
function thread_init()
  con = sysbench.sql.driver():connect()
  con:query("SET SESSION autocommit=1")
  con:query("SET SESSION TRANSACTION READ ONLY")
  con:query("SET SESSION max_execution_time={max_execution_ms}")
  samples = assert(io.open(directory .. '/thread-' .. sysbench.tid .. '.csv', 'w'))
  samples:setvbuf('full', 16384)
  samples:write("thread,connection_id,sequence,start_unix,start_monotonic,elapsed_ms,success,rows_returned,error_code,sql_state,error\\n")
  local metadata = assert(io.open(directory .. '/session-' .. sysbench.tid .. '.csv', 'w'))
  metadata:write("name,value\\n")
  connection_id = con:query_row("SELECT CONNECTION_ID()")
  metadata:write("connection_id," .. csv(connection_id) .. "\\n")
  local _, cipher = con:query_row("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
  metadata:write("Ssl_cipher," .. csv(cipher) .. "\\n")
  for _, name in ipairs({{"autocommit", "transaction_isolation", "transaction_read_only", "sql_mode", "max_execution_time", "optimizer_switch"}}) do
    metadata:write(name .. ',' .. csv(con:query_row("SELECT @@session." .. name)) .. "\\n")
  end
  {setup}
  metadata:close()
  sequence = 0
  quota = {events} > 0 and (math.floor({events} / {threads}) + (sysbench.tid < ({events} % {threads}) and 1 or 0)) or nil
end
function thread_done()
  if samples then samples:flush(); samples:close() end
  if con then con:disconnect() end
end
function event()
  sequence = sequence + 1
  local utc = clock(0)
  local start = clock(CLOCK_MONOTONIC)
  local ok, result = pcall(function() return con:query(query) end)
  local elapsed = (clock(CLOCK_MONOTONIC) - start) * 1000
  local code, state, message, returned = '', '', '', 0
  if ok and result ~= nil then returned = tonumber(result.nrows) or 0 end
  if not ok then
    code = type(result) == 'table' and (result.sql_errno or '') or ''
    state = type(result) == 'table' and (result.sql_state or '') or ''
    message = type(result) == 'table' and (result.sql_errmsg or result.message or 'SQL error') or tostring(result)
  end
  samples:write(string.format('%d,%s,%d,%.6f,%.6f,%.6f,%d,%d,%s,%s,%s\\n', sysbench.tid, csv(connection_id), sequence, utc, start, elapsed,
                              ok and 1 or 0, returned, csv(code), csv(state), csv(message)))
  if sequence % 100 == 0 or not ok then samples:flush() end
  if not ok then error('BENCHMARK_SQL_ERROR ' .. tostring(code) .. ' [' .. state .. '] ' .. message) end
end
-- Use Sysbench's public event counters, avoiding a synthetic terminal event.
function thread_run(tid)
  if gated then signal('ready-' .. tid); wait_for('start.signal') end
  local deadline = {duration_seconds} > 0 and (clock(CLOCK_MONOTONIC) + {duration_seconds}) or nil
  while (not quota or sequence < quota) and (not deadline or clock(CLOCK_MONOTONIC) < deadline) and ffi.C.sb_more_events(tid) do
    ffi.C.sb_event_start(tid)
    event()
    ffi.C.sb_event_stop(tid)
  end
  if gated then
    samples:flush()
    signal('done-' .. tid)
    wait_for('finish.signal')
  end
end
'''
