#!/usr/bin/env python3
"""Run a list of SELECT queries with Sysbench concurrency and event controls."""

from __future__ import annotations

import argparse
import getpass
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def split_sql_statements(source: str) -> list[str]:
    """Split statements on semicolons outside strings, identifiers and comments.

    This deliberately supports normal SQL queries, strings and SQL comments.
    It does not support custom DELIMITER blocks or stored-program definitions.
    """
    statements: list[str] = []
    current: list[str] = []
    index = 0
    state = "normal"

    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if state == "normal":
            if char == "'":
                state = "single_quote"
            elif char == '"':
                state = "double_quote"
            elif char == "`":
                state = "backtick"
            elif char == "#":
                state = "line_comment"
            elif char == "-" and next_char == "-" and (index + 2 == len(source) or source[index + 2].isspace()):
                state = "line_comment"
            elif char == "/" and next_char == "*":
                state = "block_comment"
            elif char == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                index += 1
                continue
        elif state == "single_quote":
            if char == "\\" and index + 1 < len(source):
                current.extend((char, next_char))
                index += 2
                continue
            if char == "'":
                if next_char == "'":
                    current.extend((char, next_char))
                    index += 2
                    continue
                state = "normal"
        elif state == "double_quote":
            if char == "\\" and index + 1 < len(source):
                current.extend((char, next_char))
                index += 2
                continue
            if char == '"':
                if next_char == '"':
                    current.extend((char, next_char))
                    index += 2
                    continue
                state = "normal"
        elif state == "backtick" and char == "`":
            state = "normal"
        elif state == "line_comment" and char in "\r\n":
            state = "normal"
        elif state == "block_comment" and char == "*" and next_char == "/":
            current.extend((char, next_char))
            index += 2
            state = "normal"
            continue

        current.append(char)
        index += 1

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def lua_long_string(value: str) -> str:
    """Encode a Lua long string without changing the SQL text."""
    equals = ""
    while f"]{equals}]" in value:
        equals += "="
    return f"[{equals}[{value}]{equals}]"


def build_lua(statements: list[str], mode: str, latency_directory: Path | None) -> str:
    queries = ",\n".join(f"  {lua_long_string(statement)}" for statement in statements)
    choose_query = (
        "selected_index = sysbench.rand.uniform(1, #queries)\n  query = queries[selected_index]"
        if mode == "random"
        else "selected_index = query_index\n  query = queries[selected_index]\n  query_index = (query_index % #queries) + 1"
    )
    latency_setup = ""
    execute_query = "con:query(query)"
    latency_finish = ""
    if latency_directory:
        latency_dir = lua_long_string(str(latency_directory))
        latency_setup = f"""
-- LuaJIT FFI measures wall-clock time around each MySQL query.
local ffi = require("ffi")
ffi.cdef[[
  typedef long time_t;
  typedef int suseconds_t;
  struct timeval {{ time_t tv_sec; suseconds_t tv_usec; }};
  int gettimeofday(struct timeval *tp, void *tzp);
]]
local latency_dir = {latency_dir}
local latency_file
local latency_buffer = {{}}

local function now_us()
  local tv = ffi.new("struct timeval")
  assert(ffi.C.gettimeofday(tv, nil) == 0, "gettimeofday failed")
  return tonumber(tv.tv_sec) * 1000000 + tonumber(tv.tv_usec)
end

local function flush_latencies()
  if #latency_buffer > 0 then
    latency_file:write(table.concat(latency_buffer))
    latency_file:flush()
    latency_buffer = {{}}
  end
end
"""
        execute_query = """
  local started_us = now_us()
  con:query(query)
  local elapsed_ms = (now_us() - started_us) / 1000
  latency_buffer[#latency_buffer + 1] = selected_index .. "," .. string.format("%.3f", elapsed_ms) .. "\\n"
  if #latency_buffer >= 100 then
    flush_latencies()
  end
"""
        latency_finish = """
  flush_latencies()
  latency_file:close()
"""
    return f"""-- Generated by sysbench_sql_runner.py. Do not add credentials here.
local queries = {{
{queries}
}}
{latency_setup}

function thread_init()
  con = sysbench.sql.driver():connect()
  query_index = 1
  {"latency_file = assert(io.open(latency_dir .. '/thread-' .. sysbench.tid .. '.csv', 'w'))" if latency_directory else ""}
end

function thread_done()
  {latency_finish}
  con:disconnect()
end

function event()
  local query, selected_index
  {choose_query}
  local random_values = {{}}
  query = string.gsub(query, "{{{{RAND_INT:([%w_]+):(%d+):(%d+)}}}}", function(name, minimum, maximum)
    if random_values[name] == nil then
      random_values[name] = sysbench.rand.uniform(tonumber(minimum), tonumber(maximum))
    end
    return tostring(random_values[name])
  end)
  {execute_query}
end
"""


def percentile(sorted_values: list[float], percentage: float) -> float:
    """Nearest-rank percentile, appropriate for a finite benchmark sample."""
    return sorted_values[max(0, math.ceil((percentage / 100) * len(sorted_values)) - 1)]


def summarize_latencies(latency_directory: Path, statement_count: int, expected_events: int, return_code: int) -> str:
    """Read latency samples emitted independently by each Lua worker."""
    values: dict[int, list[float]] = defaultdict(list)
    malformed = 0
    for sample_file in sorted(latency_directory.glob("thread-*.csv")):
        for line in sample_file.read_text(encoding="utf-8").splitlines():
            try:
                query_index, milliseconds = line.split(",", maxsplit=1)
                values[int(query_index)].append(float(milliseconds))
            except (ValueError, TypeError):
                malformed += 1

    all_values = [value for query_values in values.values() for value in query_values]
    lines = ["", "# Client-side latency percentiles (measured around each con:query call)"]
    lines.append(f"# Samples collected: {len(all_values)} of {expected_events} expected events")
    if return_code != 0 or len(all_values) != expected_events or malformed:
        lines.append("# WARNING: INCOMPLETE RUN — do not use these percentiles for comparison.")
        if return_code != 0:
            lines.append(f"# Sysbench exit code: {return_code}")
        if malformed:
            lines.append(f"# Malformed samples ignored: {malformed}")

    def metrics(label: str, samples: list[float]) -> list[str]:
        if not samples:
            return [f"{label}: no samples"]
        samples.sort()
        return [
            f"{label} samples: {len(samples)}",
            f"{label} p50 (ms): {percentile(samples, 50):.3f}",
            f"{label} p95 (ms): {percentile(samples, 95):.3f}",
            f"{label} p99 (ms): {percentile(samples, 99):.3f}",
            f"{label} max (ms): {samples[-1]:.3f}",
        ]

    lines.extend(metrics("overall", all_values))
    for query_index in range(1, statement_count + 1):
        lines.extend(metrics(f"query_{query_index}", values[query_index]))
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run semicolon-separated SQL queries through Sysbench. Events are global across all threads."
    )
    parser.add_argument("sql_file", type=Path, help="UTF-8 .sql file containing one or more statements terminated by ;")
    parser.add_argument("--host", required=True, help="MySQL endpoint")
    parser.add_argument("--database", required=True, help="Database/schema name")
    parser.add_argument("--user", required=True, help="Benchmark user")
    parser.add_argument("--port", type=int, default=3306, help="MySQL port (default: 3306)")
    parser.add_argument("--threads", type=int, default=100, help="Concurrent Sysbench threads/connections (default: 100)")
    parser.add_argument("--events", type=int, default=10_000, help="Total events across all threads (default: 10000)")
    parser.add_argument("--query-mode", choices=("random", "round-robin"), default="random", help="How each event selects a SQL statement")
    parser.add_argument("--report-interval", type=int, default=1, help="Seconds between Sysbench progress reports (default: 1)")
    parser.add_argument("--percentile", type=int, default=99, help="Percentile requested from Sysbench (default: 99)")
    parser.add_argument("--mysql-ssl", choices=("on", "off"), default="on", help="Pass SSL preference to Sysbench (default: on)")
    parser.add_argument("--password-env", default="MYSQL_PASSWORD", help="Environment variable for the password (default: MYSQL_PASSWORD)")
    parser.add_argument("--sysbench", default="sysbench", help="Sysbench executable or absolute path (default: sysbench)")
    parser.add_argument("--output", type=Path, help="Output log path; defaults to results/<timestamp>.txt")
    parser.add_argument("--keep-lua", action="store_true", help="Keep the generated Lua workload beside the output log")
    parser.add_argument("--no-latency-capture", action="store_false", dest="capture_latencies", help="Disable Python-calculated client-side p50/p95/p99 collection")
    parser.add_argument("--keep-latencies", action="store_true", help="Keep raw per-event latency CSV files beside the output log")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the execution plan without connecting")
    parser.add_argument("--extra-sysbench-arg", action="append", default=[], help="Additional Sysbench argument; may be repeated")
    args = parser.parse_args()
    if args.threads < 1 or args.events < 1:
        parser.error("--threads and --events must be positive integers")
    return args


def main() -> int:
    args = parse_args()
    if not args.sql_file.is_file():
        print(f"SQL file not found: {args.sql_file}", file=sys.stderr)
        return 2

    statements = split_sql_statements(args.sql_file.read_text(encoding="utf-8"))
    if not statements:
        print("No SQL statements found. Terminate each query with a semicolon (;).", file=sys.stderr)
        return 2

    print(f"Loaded {len(statements)} SQL statement(s) from {args.sql_file}")
    print("Supported random token: {{RAND_INT:name:min:max}}; it is replaced with a literal before MySQL receives the query.")
    print(f"Plan: {args.events} total events, {args.threads} concurrent threads, selection={args.query_mode}")
    if args.dry_run:
        for position, statement in enumerate(statements, start=1):
            preview = " ".join(statement.split())
            print(f"  {position}. {preview[:180]}")
        return 0

    sysbench_path = shutil.which(args.sysbench) if not os.path.sep in args.sysbench else args.sysbench
    if not sysbench_path:
        print(f"Sysbench executable not found: {args.sysbench}", file=sys.stderr)
        return 2

    password = os.environ.get(args.password_env)
    if password is None:
        password = getpass.getpass(f"MySQL password for {args.user}@{args.host}: ")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output or Path("results") / f"sysbench_{args.sql_file.stem}_{timestamp}.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    latency_context = tempfile.TemporaryDirectory(prefix="sysbench-latencies-") if args.capture_latencies else None
    latency_directory = Path(latency_context.name) if latency_context else None
    with tempfile.NamedTemporaryFile(mode="w", suffix=".lua", encoding="utf-8", delete=False) as temp_file:
        temp_file.write(build_lua(statements, args.query_mode, latency_directory))
        lua_path = Path(temp_file.name)

    command = [
        str(sysbench_path), str(lua_path),
        f"--mysql-host={args.host}", f"--mysql-port={args.port}",
        f"--mysql-user={args.user}", f"--mysql-password={password}",
        f"--mysql-db={args.database}", f"--mysql-ssl={args.mysql_ssl}",
        f"--threads={args.threads}", f"--events={args.events}", "--time=0",
        f"--report-interval={args.report_interval}", f"--percentile={args.percentile}",
        *args.extra_sysbench_arg, "run",
    ]

    print(f"Running Sysbench; full output will be saved to {output_path}")
    latency_summary = ""
    with output_path.open("w", encoding="utf-8") as output_file:
        output_file.write(f"# UTC started: {datetime.now(timezone.utc).isoformat()}\n")
        output_file.write(f"# SQL file: {args.sql_file}\n")
        output_file.write(f"# Statements: {len(statements)}; events: {args.events}; threads: {args.threads}; mode: {args.query_mode}\n\n")
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output_file.write(completed.stdout)
        if latency_directory:
            latency_summary = summarize_latencies(latency_directory, len(statements), args.events, completed.returncode)
            output_file.write(latency_summary)

    if args.keep_lua:
        saved_lua = output_path.with_suffix(".lua")
        lua_path.replace(saved_lua)
        print(f"Generated Lua workload retained at {saved_lua}")
    else:
        lua_path.unlink(missing_ok=True)

    if latency_directory and args.keep_latencies:
        saved_latencies = output_path.with_name(f"{output_path.stem}_latencies")
        if saved_latencies.exists():
            shutil.rmtree(saved_latencies)
        shutil.copytree(latency_directory, saved_latencies)
        print(f"Raw latency samples retained at {saved_latencies}")
    if latency_context:
        latency_context.cleanup()

    print(completed.stdout, end="")
    if latency_summary:
        print(latency_summary, end="")
    print(f"Output saved to {output_path}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
