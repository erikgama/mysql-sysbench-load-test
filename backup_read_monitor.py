#!/usr/bin/env python3
"""Monitor read availability and latency during a MySQL backup.

The program keeps one MySQL connection per worker open and triggers one
read-only query on every worker at the same time, at a fixed interval.
It writes one aggregate row per round to a CSV file.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import math
import statistics
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import pymysql
except ImportError as exc:
    raise SystemExit(
        "PyMySQL nao encontrado. Crie um venv e instale: "
        "python3 -m pip install PyMySQL"
    ) from exc


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[rank]


class Monitor:
    def __init__(self, args: argparse.Namespace, password: str) -> None:
        self.args = args
        self.password = password
        self.condition = threading.Condition()
        self.ready = threading.Barrier(args.threads + 1)
        self.round_number = 0
        self.stop = False
        self.results: dict[int, list[tuple[bool, float, str]]] = defaultdict(list)
        self.threads: list[threading.Thread] = []

    def connect(self):
        return pymysql.connect(
            host=self.args.host,
            port=self.args.port,
            user=self.args.user,
            password=self.password,
            database=self.args.database,
            connect_timeout=self.args.connect_timeout,
            read_timeout=self.args.read_timeout,
            write_timeout=self.args.read_timeout,
            autocommit=True,
            ssl=None if self.args.mysql_ssl == "off" else {},
        )

    def worker(self, worker_id: int) -> None:
        connection = None
        try:
            connection = self.connect()
            self.ready.wait(timeout=self.args.connect_timeout + 30)
            last_round = 0
            while True:
                with self.condition:
                    while not self.stop and self.round_number == last_round:
                        self.condition.wait()
                    if self.stop:
                        return
                    current_round = self.round_number
                    last_round = current_round

                started = time.perf_counter()
                success = True
                message = ""
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(self.args.query)
                        cursor.fetchall()
                except Exception as exc:  # Driver errors are test data, not crashes.
                    success = False
                    message = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:300]
                    try:
                        connection.close()
                    except Exception:
                        pass
                    connection = None
                    try:
                        connection = self.connect()
                    except Exception as reconnect_exc:
                        message += f" | reconnect: {type(reconnect_exc).__name__}: {reconnect_exc}"[:300]

                elapsed_ms = (time.perf_counter() - started) * 1000
                with self.condition:
                    self.results[current_round].append((success, elapsed_ms, message))
                    self.condition.notify_all()
        except Exception as exc:
            print(f"worker {worker_id}: falha ao conectar: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            try:
                self.ready.abort()
            except threading.BrokenBarrierError:
                pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def start(self) -> None:
        for worker_id in range(1, self.args.threads + 1):
            thread = threading.Thread(target=self.worker, args=(worker_id,), daemon=True)
            thread.start()
            self.threads.append(thread)
        try:
            self.ready.wait(timeout=self.args.connect_timeout + 30)
        except threading.BrokenBarrierError as exc:
            self.shutdown()
            raise RuntimeError("Nao foi possivel abrir todas as conexoes iniciais.") from exc

    def run_round(self, number: int) -> list[tuple[bool, float, str]]:
        with self.condition:
            self.round_number = number
            self.condition.notify_all()
            deadline = time.monotonic() + self.args.round_timeout
            while len(self.results[number]) < self.args.threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=remaining)
            return list(self.results[number])

    def shutdown(self) -> None:
        with self.condition:
            self.stop = True
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(timeout=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor concorrente de SELECTs durante backup MySQL.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--threads", type=int, default=100)
    parser.add_argument("--interval", type=float, default=2.0, help="Segundos entre o inicio das rodadas.")
    parser.add_argument("--duration", type=float, default=0, help="Segundos totais; 0 executa ate Ctrl+C.")
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--read-timeout", type=int, default=30)
    parser.add_argument("--round-timeout", type=float, default=30.0)
    parser.add_argument("--mysql-ssl", choices=("on", "off"), default="on")
    parser.add_argument("--query", default="SELECT flight_id FROM flight LIMIT 1")
    parser.add_argument("--log", required=True, help="Caminho do CSV de saida.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.threads < 1 or args.interval <= 0 or args.round_timeout <= 0:
        raise SystemExit("threads, interval e round-timeout devem ser maiores que zero.")

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    password = getpass.getpass("Senha MySQL: ")
    monitor = Monitor(args, password)
    print(f"Abrindo {args.threads} conexoes persistentes...", flush=True)
    monitor.start()
    print("READY - pressione Ctrl+C quando o backup terminar.", flush=True)

    fields = [
        "timestamp_utc", "round", "planned_connections", "responses", "successes",
        "errors", "missing", "p50_ms", "p95_ms", "p99_ms", "avg_ms", "max_ms",
        "error_examples",
    ]
    started_at = time.monotonic()
    round_number = 0
    try:
        with log_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            while not args.duration or time.monotonic() - started_at < args.duration:
                round_number += 1
                round_started = time.monotonic()
                responses = monitor.run_round(round_number)
                successes = [latency for ok, latency, _ in responses if ok]
                errors = [message for ok, _, message in responses if not ok]
                row = {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "round": round_number,
                    "planned_connections": args.threads,
                    "responses": len(responses),
                    "successes": len(successes),
                    "errors": len(errors),
                    "missing": args.threads - len(responses),
                    "p50_ms": f"{percentile(successes, 0.50):.3f}" if successes else "",
                    "p95_ms": f"{percentile(successes, 0.95):.3f}" if successes else "",
                    "p99_ms": f"{percentile(successes, 0.99):.3f}" if successes else "",
                    "avg_ms": f"{statistics.mean(successes):.3f}" if successes else "",
                    "max_ms": f"{max(successes):.3f}" if successes else "",
                    "error_examples": " | ".join(message for message in errors if message)[:1000],
                }
                writer.writerow(row)
                handle.flush()
                print(
                    f"round={round_number} ok={row['successes']}/{args.threads} "
                    f"errors={row['errors']} missing={row['missing']} "
                    f"p95={row['p95_ms'] or '-'}ms p99={row['p99_ms'] or '-'}ms "
                    f"max={row['max_ms'] or '-'}ms",
                    flush=True,
                )
                sleep_for = args.interval - (time.monotonic() - round_started)
                if sleep_for > 0:
                    time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("Monitor interrompido.", flush=True)
    finally:
        monitor.shutdown()
        password = ""
    print(f"Log salvo em {log_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
