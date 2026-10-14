from __future__ import annotations

import csv
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .collector import Collector, Sampler, derive
from .core import build_lua, save_json, summarize_samples, utc_now, warmup_stable


ENGINE_SETTINGS = {"innodb": "OFF", "heatwave": "FORCED", "auto": "ON"}


def choose_engine(mode, mapping, query_id, case_id):
    selected = mapping.get(f"{query_id}/{case_id}", mapping.get(query_id)) if mode == "mixed" else mode
    if selected not in ENGINE_SETTINGS:
        raise ValueError(f"Sem destino valido para {query_id}/{case_id}: {selected!r}")
    return selected


def execute_phase(args, collector, metadata, case, key, engine, threads, events, seconds, directory,
                  password, connection_args):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    supported = metadata["primary_engine_session_setting_supported"]
    setting = ENGINE_SETTINGS[engine] if supported else None
    if not supported and engine != "innodb":
        phase = {"valid": False, "status": "engine_unavailable", "engine": engine,
                 "reason": "use_secondary_engine unavailable: cannot silently substitute InnoDB.",
                 "directory": str(directory), "client": {"successes": 0, "errors": 0}}
        save_json(directory / "phase.json", phase)
        return phase
    save_json(directory / "before.json", before := collector.snapshot(args.database, key))
    lua = directory / "workload.lua"
    lua.write_text(build_lua(case["sql"], directory, threads, events, args.query_timeout_ms, setting), encoding="utf-8")
    sampler = Sampler(connection_args, password, directory, args.database, args.monitor_interval)
    command = [args.sysbench, str(lua.resolve()), f"--threads={threads}", "--events=0",
               f"--time={seconds}", "--report-interval=1", "--percentile=99",
               "--mysql-ignore-errors=all", "--db-ps-mode=disable",
               f"--mysql-host={args.host}", f"--mysql-port={args.port}",
               f"--mysql-user={args.user}", f"--mysql-db={args.database}",
               f"--mysql-ssl={args.mysql_ssl}"]
    if any(c in password for c in "\r\n\0"):
        raise ValueError("Senha com newline ou NUL nao e suportada no config-file do Sysbench.")
    phase_start = utc_now()
    started = time.monotonic()
    timeout = min(args.phase_timeout, seconds + args.query_timeout_ms / 1000 + 30) if seconds else args.phase_timeout
    timed_out = False
    interrupted = False
    process = None
    reader = None
    rc = -1
    sampler.start()
    try:
        # Credentials never enter process arguments or retained Lua/log files.
        with tempfile.TemporaryDirectory(prefix="mysql-benchmark-secret-") as secret_dir:
            os.chmod(secret_dir, 0o700)
            config = Path(secret_dir) / "sysbench.conf"
            descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("mysql-password=" + password + "\n")
            process = subprocess.Popen([command[0], f"--config-file={config}", *command[1:], "run"],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            def drain():
                with (directory / "sysbench.log").open("w", encoding="utf-8") as handle:
                    for line in process.stdout:
                        handle.write(line.replace(password, "[REDACTED]") if password else line)
                        handle.flush()
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                rc = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    rc = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    rc = process.wait()
            except KeyboardInterrupt:
                interrupted = True
                process.terminate()
                try:
                    rc = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    rc = process.wait()
            finally:
                reader.join(timeout=5)
                if process.stdout:
                    process.stdout.close()
    finally:
        sampler.stop()
    after = collector.snapshot(args.database, key)
    save_json(directory / "after.json", after)
    client = summarize_samples(directory, threads, events)
    sessions = {}
    for session_file in directory.glob("session-*.csv"):
        with session_file.open(newline="", encoding="utf-8") as handle:
            sessions[session_file.stem] = {r["name"]: r["value"] for r in csv.DictReader(handle)}
    routing_verified = len(sessions) == threads and (setting is None or all(
        s.get("use_secondary_engine") == setting for s in sessions.values()))
    tls_verified = len(sessions) == threads and all(s.get("Ssl_cipher") for s in sessions.values())
    diagnostic = derive(before, after, metadata["cpu_consumer_enabled"])
    save_json(directory / "diagnostics.json", diagnostic)
    log = (directory / "sysbench.log").read_text(encoding="utf-8") if (directory / "sysbench.log").exists() else ""
    sql_valid = bool(rc == 0 and not timed_out and not interrupted and "FATAL" not in log and
                     client["successes"] > 0 and client["errors"] == 0 and
                     client["malformed_or_duplicate"] == 0 and client["quota_complete"] and
                     routing_verified and (args.mysql_ssl == "off" or tls_verified))
    telemetry_valid = bool(before["global_status"].get("available") and after["global_status"].get("available") and
                           not diagnostic["server_restart_detected"] and
                           not diagnostic["global_counter_delta"]["decreased_or_reset"])
    statement_rows = diagnostic["statements"].get("rows", [])
    observed = {"scope": "Instance-wide counters for selected digest; other sessions may contribute.",
                "available": bool(statement_rows), "primary": None, "secondary": None}
    if len(statement_rows) == 1:
        values = statement_rows[0]["values"]
        count, secondary = values.get("COUNT_STAR"), values.get("COUNT_SECONDARY")
        if count is not None and secondary is not None:
            observed.update(primary=count - secondary, secondary=secondary)
            observed["matches_client_count"] = count == client["successes"]
            # Session controls establish the requested routing; digest evidence is independent.
            if engine == "innodb" and secondary > 0:
                observed["scope_conflict"] = True
    requests = diagnostic["global_counter_delta"]["values"].get("Innodb_buffer_pool_reads")
    phase = {"valid": sql_valid, "status": "completed" if sql_valid else "incomplete",
             "fatal_messages": [line for line in log.splitlines() if "FATAL" in line][:20],
             "telemetry_complete": telemetry_valid, "engine": engine, "session_engine": setting,
             "observed_engine": observed, "threads": threads, "events": events or None,
             "start_utc": phase_start, "end_utc": utc_now(), "process_seconds": time.monotonic() - started,
             "timed_out": timed_out, "interrupted": interrupted, "exit_code": rc,
             "client": client, "diagnostics": diagnostic,
             "physical_reads_per_query": requests / client["successes"] if requests is not None and client["successes"] else None,
             "worker_sessions": sessions, "session_routing_verified": routing_verified,
             "tls_requested": args.mysql_ssl, "tls_cipher_present_all_workers": bool(tls_verified),
             "directory": str(directory), "automatic_retries": 0,
             "connections_recreated_for_each_phase": True}
    save_json(directory / "phase.json", phase)
    if interrupted:
        raise KeyboardInterrupt
    return phase


def warm_up(args, collector, metadata, case, key, engine, threads, directory, password, connection_args):
    windows = []
    started = time.monotonic()
    evaluation = {"reason": "not_started"}
    if args.skip_warmup:
        return {"status": "skipped", "stable": False, "windows": [], "seconds": 0}
    while time.monotonic() - started < args.warmup_max_seconds:
        remaining = args.warmup_max_seconds - (time.monotonic() - started)
        window_seconds = min(args.warmup_window_seconds, int(remaining))
        if window_seconds < 1:
            break
        print(f"  warm-up janela {len(windows) + 1}: {window_seconds}s", flush=True)
        phase = execute_phase(args, collector, metadata, case, key, engine, threads, 0, window_seconds,
                              Path(directory) / f"window-{len(windows) + 1:02d}", password, connection_args)
        windows.append(phase)
        if not phase["valid"]:
            evaluation = {"reason": "warmup_execution_failed"}
            break
        stable, evaluation = warmup_stable(windows, args.warmup_stable_windows, args.warmup_tolerance,
                                          args.warmup_min_samples, require_io=engine == "innodb")
        load_seconds = sum(w["client"].get("query_window_seconds", 0) for w in windows)
        samples = sum(w["client"]["successes"] for w in windows)
        if stable and load_seconds >= args.warmup_min_seconds and samples >= args.warmup_min_events:
            result = {"status": "stable", "stable": True, "seconds": time.monotonic() - started,
                      "load_seconds": load_seconds, "samples": samples, "evaluation": evaluation, "windows": windows}
            save_json(Path(directory) / "warmup.json", result)
            return result
    result = {"status": "not_stabilized", "stable": False, "seconds": time.monotonic() - started,
              "evaluation": evaluation, "windows": windows}
    save_json(Path(directory) / "warmup.json", result)
    return result


def perform(args, workload, output, password, connection_args, environment, mapping):
    from .report import render_report
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save_json(output / "workload.json", workload)
    collector = Collector(connection_args, connection_args.get("password", ""))
    metadata = collector.metadata(workload, args.database)
    save_json(output / "metadata.before.json", metadata)
    profile = {k: getattr(args, k) for k in ["threads", "events_per_query", "repetitions", "engine",
               "warmup_min_seconds", "warmup_max_seconds", "warmup_window_seconds", "warmup_stable_windows",
               "warmup_tolerance", "warmup_min_events", "warmup_min_samples", "skip_warmup", "allow_unstable_warmup",
               "query_timeout_ms", "monitor_interval"]}
    run = {"format_version": 1, "label": args.label, "started_utc": utc_now(), "status": "running",
           "workload_hash": workload["hash"], "workload": workload, "profile": profile,
           "engine_map": mapping, "environment": environment, "database": args.database,
           "endpoint": args.host, "sysbench_version": subprocess.check_output([args.sysbench, "--version"], text=True).strip(),
           "metadata_file": "metadata.before.json", "scenarios": [],
           "scope": metadata["collection_scope"], "cloud_metrics_file": str(args.cloud_metrics_csv) if args.cloud_metrics_csv else None}
    try:
        if not metadata["server"].get("available"):
            raise RuntimeError("Nao foi possivel conectar ao banco/coletar a identidade; veja metadata.before.json.")
        for query in workload["queries"]:
            for case in query["cases"]:
                key = query["id"] + "/" + case["id"]
                for mode in args.engine:
                    engine = choose_engine(mode, mapping, query["id"], case["id"])
                    for threads in args.threads:
                        for repetition in range(1, args.repetitions + 1):
                            scenario_id = f"{query['id']}_{case['id']}_{mode}_{engine}_t{threads}_r{repetition}"
                            scenario_dir = output / "scenarios" / scenario_id
                            scenario_dir.mkdir(parents=True)
                            print(f"{scenario_id}: {args.events_per_query} execucoes medidas", flush=True)
                            scenario = {"id": scenario_id, "query_id": query["id"], "case_id": case["id"],
                                        "case_name": case["name"], "sql": case["sql"], "mode": mode,
                                        "engine": engine, "threads": threads, "repetition": repetition,
                                        "directory": str(scenario_dir.relative_to(output)), "status": "preparing"}
                            run["scenarios"].append(scenario)
                            save_json(output / "run.json", run)
                            if not metadata["primary_engine_session_setting_supported"] and engine != "innodb":
                                scenario.update(status="engine_unavailable", valid=False,
                                                reason="Servidor nao expoe use_secondary_engine; nenhum fallback silencioso.")
                                continue
                            setting = ENGINE_SETTINGS[engine] if metadata["primary_engine_session_setting_supported"] else None
                            save_json(scenario_dir / "explain.json", collector.explain(case["sql"], setting))
                            warmup = warm_up(args, collector, metadata, case, key, engine, threads,
                                             scenario_dir / "warmup", password, connection_args)
                            scenario["warmup"] = warmup
                            if not warmup["stable"] and not args.skip_warmup and not args.allow_unstable_warmup:
                                scenario.update(status="warmup_not_stabilized", valid=False)
                                continue
                            measured = execute_phase(args, collector, metadata, case, key, engine, threads,
                                                     args.events_per_query, 0, scenario_dir / "measured", password, connection_args)
                            scenario["measured"] = measured
                            scenario.update(status=measured["status"], valid=measured["valid"],
                                            warmed_comparison=measured["valid"] and warmup["stable"])
                            print(f"  {scenario['status']}: p95={measured['client'].get('p95_ms')} ms; p99={measured['client'].get('p99_ms')} ms", flush=True)
                            save_json(output / "run.json", run)
                            render_report(output, run)
        run["status"] = "completed" if run["scenarios"] and all(s.get("valid") for s in run["scenarios"]) else "incomplete"
    except KeyboardInterrupt:
        run["status"] = "interrupted"
    except Exception as exc:
        run["status"] = "failed"
        message = f"{type(exc).__name__}: {exc}"
        run["error"] = message.replace(password, "[REDACTED]") if password else message
    finally:
        run["finished_utc"] = utc_now()
        run["collector_capabilities"] = collector.capabilities
        save_json(output / "metadata.after.json", collector.metadata(workload, args.database))
        collector.close()
        save_json(output / "run.json", run)
        write_summary_csv(output, run)
        render_report(output, run)
    return run


def write_summary_csv(output, run):
    fields = ["query", "case", "engine", "threads", "repetition", "status", "warmup", "successes", "errors",
              "qps", "avg_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms", "cpu_ms_per_exec"]
    with (Path(output) / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for s in run["scenarios"]:
            m = s.get("measured", {})
            c = m.get("client", {})
            statements = m.get("diagnostics", {}).get("statements", {}).get("rows", [])
            row = {"query": s["query_id"], "case": s["case_id"], "engine": s["engine"],
                   "threads": s["threads"], "repetition": s["repetition"], "status": s["status"],
                   "warmup": s.get("warmup", {}).get("status"),
                   "cpu_ms_per_exec": statements[0].get("cpu_ms_per_exec") if len(statements) == 1 else None}
            row.update({k: c.get(k) for k in fields if k in c})
            writer.writerow(row)
