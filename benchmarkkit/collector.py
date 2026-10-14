from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .core import counter_delta, save_json, utc_now


class Collector:
    """Best-effort, read-only SQL telemetry; no reset or instrument enabling."""

    def __init__(self, connection_args, secret=""):
        self.connection_args = connection_args
        self.secret = secret
        self.connection = None
        self.capabilities = {}
        self.query_digests = {}
        self.cpu_enabled = False

    def connect(self):
        import pymysql
        self.connection = pymysql.connect(**self.connection_args, cursorclass=pymysql.cursors.DictCursor)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def rows(self, label, sql, params=None):
        started = time.monotonic()
        try:
            if self.connection is None:
                self.connect()
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            self.capabilities[label] = {"available": True}
            return {"available": True, "rows": rows,
                    "collection_ms": (time.monotonic() - started) * 1000}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if self.secret:
                error = error.replace(self.secret, "[REDACTED]")
            self.capabilities[label] = {"available": False, "reason": error}
            return {"available": False, "reason": error,
                    "collection_ms": (time.monotonic() - started) * 1000}

    @staticmethod
    def pairs(result):
        if not result.get("available"):
            return {}
        return {r["Variable_name"]: r["Value"] for r in result["rows"]}

    def metadata(self, workload, database):
        global_vars = self.rows("global_variables", "SHOW GLOBAL VARIABLES")
        engine_variable = self.rows("secondary_engine_variable", "SHOW SESSION VARIABLES LIKE 'use_secondary_engine'")
        supported = bool(engine_variable.get("rows"))
        consumers = self.rows("consumers", "SELECT * FROM performance_schema.setup_consumers")
        self.cpu_enabled = any(r.get("NAME") == "events_statements_cpu" and r.get("ENABLED") == "YES"
                               for r in consumers.get("rows", []))
        instruments = self.rows("instruments", "SELECT NAME, ENABLED, TIMED FROM performance_schema.setup_instruments")
        info = self.rows("server_identity", "SELECT VERSION() AS version, @@version_comment AS edition, @@server_uuid AS server_uuid")
        schema = self.rows("schema_overview", "SELECT TABLE_NAME, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, TABLE_COLLATION FROM information_schema.tables WHERE TABLE_SCHEMA=%s", (database,))
        definitions = {}
        for query in workload["queries"]:
            for case in query["cases"]:
                key = query["id"] + "/" + case["id"]
                digest = self.rows("statement_digest", "SELECT STATEMENT_DIGEST(%s) AS digest, STATEMENT_DIGEST_TEXT(%s) AS digest_text", (case["sql"], case["sql"]))
                if digest.get("rows"):
                    self.query_digests[key] = digest["rows"][0]
        # Definitions are limited to actual referenced tables, not every object.
        import re
        selected = set()
        identifier = r"(?:`(?:``|[^`])+`|[\w$]+)"
        references = re.compile(r"\b(?:FROM|JOIN)\s+(?:((?:" + identifier + r"))\s*\.\s*)?(" + identifier + r")", re.I)
        for query in workload["queries"]:
            for case in query["cases"]:
                for match in references.finditer(case["sql"]):
                    owner = (match.group(1) or database).strip('`').replace('``', '`')
                    name = match.group(2).strip('`').replace('``', '`')
                    selected.add((owner, name))
        for row in schema.get("rows", []):
            name = row["TABLE_NAME"]
            if (database, name) in selected:
                quoted = '`' + database.replace('`', '``') + '`.`' + name.replace('`', '``') + '`'
                definitions[name] = self.rows("table_definition", "SHOW CREATE TABLE " + quoted)
        for owner, name in selected:
            if owner != database:
                quoted = '`' + owner.replace('`', '``') + '`.`' + name.replace('`', '``') + '`'
                definitions[owner + '.' + name] = self.rows("table_definition", "SHOW CREATE TABLE " + quoted)
        return {"timestamp_utc": utc_now(), "global_variables": global_vars,
                "server": info, "consumers": consumers, "instruments": instruments,
                "schema_overview": schema, "schema_row_counts_are_estimates": True,
                "table_definitions": definitions,
                "digests": self.query_digests, "cpu_consumer_enabled": self.cpu_enabled,
                "primary_engine_session_setting_supported": supported,
                "monitor_session": self.rows("monitor_session_variables", "SHOW SESSION VARIABLES"),
                "monitor_tls": self.rows("monitor_tls", "SHOW SESSION STATUS LIKE 'Ssl_cipher'"),
                "collection_scope": "Instance metrics include other clients, background work and the collector. Digest metrics are instance-wide for the selected SQL, not exclusively this benchmark."}

    def explain(self, sql, setting):
        if setting:
            changed = self.rows("explain_engine_setting", "SET SESSION use_secondary_engine=%s", (setting,))
            if not changed.get("available"):
                return changed
        try:
            return self.rows("explain", "EXPLAIN FORMAT=JSON " + sql)
        finally:
            if setting:
                self.rows("monitor_primary_engine", "SET SESSION use_secondary_engine='OFF'")

    def snapshot(self, database, key=None, deep=True):
        started = time.monotonic()
        result = {"timestamp_utc": utc_now(), "unix": time.time(), "monotonic": time.monotonic(),
                  "global_status": self.rows("global_status", "SHOW GLOBAL STATUS")}
        if deep:
            result["global_variables"] = self.rows("global_variables", "SHOW GLOBAL VARIABLES")
            digest = self.query_digests.get(key or "", {}).get("digest")
            if digest:
                result["statements"] = self.rows("statements", "SELECT * FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME=%s AND DIGEST=%s", (database, digest))
            else:
                result["statements"] = {"available": False, "reason": "SQL digest mapping unavailable; no attribution guessed."}
            result["waits"] = self.rows("waits", "SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name WHERE COUNT_STAR>0")
            result["file_io"] = self.rows("file_io", "SELECT * FROM performance_schema.file_summary_by_event_name")
            result["memory"] = self.rows("memory", "SELECT EVENT_NAME, CURRENT_NUMBER_OF_BYTES_USED, HIGH_NUMBER_OF_BYTES_USED FROM performance_schema.memory_summary_global_by_event_name ORDER BY CURRENT_NUMBER_OF_BYTES_USED DESC LIMIT 101")
            result["table_io"] = self.rows("table_io", "SELECT * FROM performance_schema.table_io_waits_summary_by_table WHERE OBJECT_SCHEMA=%s LIMIT 2001", (database,))
            result["table_locks"] = self.rows("table_locks", "SELECT * FROM performance_schema.table_lock_waits_summary_by_table WHERE OBJECT_SCHEMA=%s LIMIT 2001", (database,))
            result["lock_waits"] = self.rows("lock_waits", "SELECT * FROM performance_schema.data_lock_waits LIMIT 101")
            result["innodb_metrics"] = self.rows("innodb_metrics", "SELECT NAME, SUBSYSTEM, COUNT, STATUS FROM information_schema.INNODB_METRICS WHERE STATUS='enabled'")
            result["innodb_status"] = self.rows("innodb_status", "SHOW ENGINE INNODB STATUS")
            result["heatwave_tables"] = self.rows("heatwave_tables", "SELECT * FROM performance_schema.rpd_tables LIMIT 10001")
            result["heatwave_table_names"] = self.rows("heatwave_table_names", "SELECT * FROM performance_schema.rpd_table_id LIMIT 10001")
            result["heatwave_nodes"] = self.rows("heatwave_nodes", "SELECT * FROM performance_schema.rpd_nodes")
            for table in ["tp_thread_group_state", "tp_thread_group_stats"]:
                result[table] = self.rows(table, f"SELECT * FROM performance_schema.{table}")
            result["truncated"] = [name for name, limit in [("memory", 100), ("table_io", 2000), ("table_locks", 2000), ("lock_waits", 100)] if len(result[name].get("rows", [])) > limit]
        result["collection_ms"] = (time.monotonic() - started) * 1000
        return result


def numeric_rows_delta(before, after, group):
    if not before.get("available") or not after.get("available"):
        return {"available": False, "reason": before.get("reason") or after.get("reason")}
    old = {tuple(r.get(k) for k in group): r for r in before.get("rows", [])}
    rows = []
    for row in after.get("rows", []):
        identity = tuple(row.get(k) for k in group)
        previous = old.get(identity)
        # A newly seen digest/event starts at zero within the interval.
        previous = previous if previous is not None else {k: 0 for k in row}
        counters = {k: v for k, v in row.items() if k.startswith("SUM_") or k.startswith("COUNT_")}
        diff = counter_delta(previous, counters)
        rows.append({**{k: row.get(k) for k in group}, **diff})
    return {"available": True, "rows": rows}


COUNTERS = ["Queries", "Questions", "Com_select", "Com_insert", "Com_update", "Com_delete",
            "Connections", "Aborted_connects", "Aborted_clients", "Bytes_received", "Bytes_sent",
            "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads", "Innodb_buffer_pool_wait_free",
            "Innodb_data_reads", "Innodb_data_writes", "Innodb_data_read", "Innodb_data_written",
            "Innodb_data_fsyncs", "Innodb_os_log_written", "Innodb_log_waits",
            "Innodb_row_lock_waits", "Innodb_row_lock_time", "Created_tmp_tables", "Created_tmp_disk_tables",
            "Sort_rows", "Sort_merge_passes", "Select_scan", "Select_full_join", "Table_locks_waited",
            "Threads_created", "Com_commit", "Com_rollback"]


def derive(before, after, cpu_enabled=False):
    old, new = Collector.pairs(before.get("global_status", {})), Collector.pairs(after.get("global_status", {}))
    elapsed = after.get("monotonic", after["unix"]) - before.get("monotonic", before["unix"])
    diffs = counter_delta(old, {k: new[k] for k in COUNTERS if k in new})
    d = diffs["values"]
    restarted = bool(old.get("Uptime") and new.get("Uptime") and float(new["Uptime"]) < float(old["Uptime"]))
    requests = d.get("Innodb_buffer_pool_read_requests")
    reads = d.get("Innodb_buffer_pool_reads")
    ratio = 100 * (1 - reads / requests) if requests and reads is not None else None
    result = {"seconds": elapsed, "global_counter_delta": diffs,
              "global_rates_per_second": {k: v / elapsed for k, v in d.items()} if elapsed > 0 else {},
              "server_restart_detected": restarted,
              "buffer_pool_hit_pct_interval": ratio,
              "cpu_consumer_enabled": cpu_enabled}
    for name, groups in [("statements", ["SCHEMA_NAME", "DIGEST"]), ("waits", ["EVENT_NAME"]),
                         ("file_io", ["EVENT_NAME"]), ("table_io", ["OBJECT_SCHEMA", "OBJECT_NAME"]),
                         ("table_locks", ["OBJECT_SCHEMA", "OBJECT_NAME"])]:
        result[name] = numeric_rows_delta(before.get(name, {}), after.get(name, {}), groups)
    for row in result["statements"].get("rows", []):
        count = row["values"].get("COUNT_STAR", 0)
        values = row["values"]
        row["elapsed_ms_per_exec"] = values["SUM_TIMER_WAIT"] / 1e9 / count if count and "SUM_TIMER_WAIT" in values else None
        row["cpu_ms_per_exec"] = values.get("SUM_CPU_TIME", 0) / 1e9 / count if count and cpu_enabled and "SUM_CPU_TIME" in values else None
        row["rows_examined_per_exec"] = values["SUM_ROWS_EXAMINED"] / count if count and "SUM_ROWS_EXAMINED" in values else None
    return result


class Sampler:
    def __init__(self, connection_args, secret, directory, database, interval):
        self.collector = Collector(connection_args, secret)
        self.directory = Path(directory)
        self.database = database
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        try:
            with (self.directory / "mysql_timeline.jsonl").open("w", encoding="utf-8") as handle:
                while not self.stop_event.is_set():
                    start = time.monotonic()
                    try:
                        snap = self.collector.snapshot(self.database, deep=False)
                        handle.write(json.dumps(snap, default=str) + "\n")
                        handle.flush()
                        if not snap["global_status"].get("available"):
                            self.collector.close()
                    except Exception as exc:
                        handle.write(json.dumps({"timestamp_utc": utc_now(), "collector_error": type(exc).__name__}) + "\n")
                    self.stop_event.wait(max(0.05, self.interval - (time.monotonic() - start)))
        finally:
            self.collector.close()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError("O coletor nao finalizou; a rodada nao pode ser considerada completa.")
