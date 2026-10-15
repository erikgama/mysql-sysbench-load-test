#!/usr/bin/env python3
"""Individual SQL benchmarks, adaptive warm-up and read-only MySQL telemetry."""
from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import shutil
import ssl
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from benchmarkkit.core import load_workload, save_json
from benchmarkkit.report import compare_reports, import_cloud_csv, render_report
from benchmarkkit.runner import choose_engine, perform


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='Testar cada query/caso individualmente')
    run.add_argument('sql_file', type=Path)
    for name in ['host', 'database', 'user']:
        run.add_argument('--' + name, required=True)
    run.add_argument('--port', type=int, default=3306)
    run.add_argument('--label', default='MySQL')
    run.add_argument('--threads', type=int, nargs='+', default=[1])
    run.add_argument('--events-per-query', type=int, default=1000,
                     help='Execucoes TOTAIS por query/caso/concorrencia/rodada, divididas entre threads')
    run.add_argument('--allow-empty-results', action='store_true', help='Permitir SELECTs sem linhas, quando intencionais')
    run.add_argument('--require-statement-cpu', action='store_true', help='Abortar antes da carga se o consumer de CPU estiver indisponivel; nao altera instrumentacao')
    run.add_argument('--min-measurement-seconds', type=float, default=30, help='Duracao minima para incluir um cenario na comparacao (nao adiciona eventos)')
    run.add_argument('--min-tail-samples', type=int, default=1000, help='Amostras minimas para incluir um cenario na comparacao')
    run.add_argument('--repetitions', type=int, default=3)
    run.add_argument('--engine', nargs='+', choices=['innodb', 'heatwave', 'auto', 'mixed'], default=['innodb'])
    run.add_argument('--engine-map', type=Path, help='JSON: {"Q01":"innodb","Q02":"heatwave"}; opcional Q01/C01')
    run.add_argument('--cases-file', type=Path, help='JSON: {"Q01":[{"name":"caso","sql":"SELECT ..."}]}')
    run.add_argument('--case-count', type=int, default=1, help='Casos RAND_INT gerados uma unica vez antes da carga')
    run.add_argument('--seed', type=int, default=42)
    run.add_argument('--warmup-min-seconds', type=float, default=60)
    run.add_argument('--warmup-max-seconds', type=float, default=600)
    run.add_argument('--warmup-window-seconds', type=int, default=20)
    run.add_argument('--warmup-stable-windows', type=int, default=3)
    run.add_argument('--warmup-tolerance', type=float, default=.15,
                     help='Amplitude relativa maxima entre janelas (0.15 = 15%%)')
    run.add_argument('--warmup-min-events', type=int, default=30)
    run.add_argument('--warmup-min-samples', type=int, default=10, help='Minimo por janela de estabilidade')
    run.add_argument('--skip-warmup', action='store_true', help='Diagnostico sem aquecimento; nao validado como rodada aquecida')
    run.add_argument('--allow-unstable-warmup', action='store_true', help='Medir mesmo sem estabilidade, com resultado sinalizado')
    run.add_argument('--monitor-interval', type=float, default=2)
    run.add_argument('--monitor-user', help='Usuario opcional separado, para coleta SQL')
    run.add_argument('--monitor-password-env', default='MYSQL_MONITOR_PASSWORD')
    run.add_argument('--password-env', default='MYSQL_PASSWORD')
    run.add_argument('--mysql-ssl', choices=['on', 'off'], default='on')
    run.add_argument('--ssl-ca', type=Path, help='CA para verificacao TLS do coletor Python')
    run.add_argument('--sysbench', default='sysbench')
    run.add_argument('--query-timeout-ms', type=int, default=30000)
    run.add_argument('--phase-timeout', type=float, default=600, help='Limite do processo cliente por fase em segundos')
    run.add_argument('--environment-json', type=Path, help='Capacidade, dataset_id, storage, regiao, HA etc.')
    run.add_argument('--cloud-metrics-csv', type=Path, help='Metricas cloud no formato longo documentado')
    run.add_argument('--output', type=Path, help='Nova pasta de resultados; nunca sobrescreve')
    run.add_argument('--dry-run', action='store_true', help='Exibir plano sem senha, conexao ou arquivos de resultado')
    comparison = sub.add_parser('compare', help='Comparar pastas de rodadas')
    comparison.add_argument('runs', type=Path, nargs='+')
    comparison.add_argument('--output', type=Path, required=True)
    report = sub.add_parser('report', help='Regenerar HTML; opcional importar metricas cloud apos a carga')
    report.add_argument('run_directory', type=Path)
    report.add_argument('--cloud-metrics-csv', type=Path)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == 'compare':
            if len(args.runs) < 2:
                p.error('Informe pelo menos duas pastas para comparar.')
            print(compare_reports(args.runs, args.output))
            return 0
        if args.command == 'report':
            run = json.loads((args.run_directory / 'run.json').read_text())
            if args.cloud_metrics_csv:
                save_json(args.run_directory / 'cloud_metrics.json', import_cloud_csv(args.cloud_metrics_csv))
            render_report(args.run_directory, run)
            print(args.run_directory / 'report.html')
            return 0
        if min(args.threads) < 1 or args.events_per_query < max(args.threads):
            p.error('threads >=1 e events-per-query >= maior concorrencia sao obrigatorios.')
        if len(set(args.threads)) != len(args.threads) or len(set(args.engine)) != len(args.engine):
            p.error('Nao repita concorrencias ou motores no mesmo comando.')
        if args.repetitions < 1 or args.case_count < 1 or args.warmup_stable_windows < 2:
            p.error('repetitions/case-count >=1 e warmup-stable-windows >=2.')
        numeric = [args.warmup_min_seconds, args.warmup_max_seconds, args.warmup_tolerance,
                   args.monitor_interval, args.phase_timeout, args.min_measurement_seconds]
        if not all(math.isfinite(v) for v in numeric) or args.min_measurement_seconds <= 0 or args.min_tail_samples < 1:
            p.error('Limites precisam ser finitos; min-measurement-seconds > 0 e min-tail-samples >= 1.')
        if (args.warmup_min_seconds < 0 or args.warmup_max_seconds <= 0 or
            args.warmup_min_seconds > args.warmup_max_seconds or args.warmup_window_seconds < 1 or
            args.warmup_tolerance <= 0 or args.warmup_min_samples < 1 or args.warmup_min_events < 1 or
            args.monitor_interval < .1 or args.query_timeout_ms < 1 or args.phase_timeout <= 0):
            p.error('Limites de tempo, amostras e tolerancia invalidos.')
        workload = load_workload(args.sql_file, args.case_count, args.seed, args.cases_file)
        mapping = json.loads(args.engine_map.read_text()) if args.engine_map else {}
        if not isinstance(mapping, dict):
            p.error('engine-map precisa ser um objeto JSON.')
        valid_keys = {q['id'] for q in workload['queries']} | {q['id']+'/'+c['id'] for q in workload['queries'] for c in q['cases']}
        if set(mapping) - valid_keys or any(v not in ['innodb', 'heatwave', 'auto'] for v in mapping.values()):
            p.error('engine-map tem query/caso ou motor invalido.')
        for mode in args.engine:
            for q in workload['queries']:
                for c in q['cases']:
                    choose_engine(mode, mapping, q['id'], c['id'])
        scenarios = sum(len(q['cases']) for q in workload['queries']) * len(args.threads) * len(args.engine) * args.repetitions
        plan = {'workload': workload, 'threads': args.threads, 'engines': args.engine, 'engine_map': mapping,
                'scenarios': scenarios, 'measured_events_total': scenarios * args.events_per_query,
                'minimum_warmup_load_seconds_total': 0 if args.skip_warmup else scenarios * args.warmup_min_seconds,
                'events_per_query_case_concurrency_repetition': args.events_per_query,
                'warmup_additional_and_excluded': not args.skip_warmup}
        if args.dry_run:
            print(json.dumps(plan, indent=2, ensure_ascii=False))
            return 0
        args.sysbench = shutil.which(args.sysbench)
        if not args.sysbench:
            p.error('Sysbench nao encontrado.')
        try:
            import pymysql  # noqa: F401
        except ImportError:
            p.error('Instale o coletor: python3 -m pip install -r requirements-benchmark.txt')
        output = args.output or Path('results') / ('benchmark_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:6])
        if output.exists():
            p.error('A pasta de saida ja existe. Use outra para preservar os resultados.')
        environment = json.loads(args.environment_json.read_text()) if args.environment_json else {}
        cloud = import_cloud_csv(args.cloud_metrics_csv)
        password = os.environ.get(args.password_env)
        if password is None:
            password = getpass.getpass('Senha do usuario de carga: ')
        monitor_password = password
        if args.monitor_user and args.monitor_user != args.user:
            monitor_password = os.environ.get(args.monitor_password_env)
            if monitor_password is None:
                monitor_password = getpass.getpass('Senha do usuario de monitoramento: ')
        connection_args = dict(host=args.host, port=args.port, user=args.monitor_user or args.user,
                               password=monitor_password, database=args.database, autocommit=True,
                               connect_timeout=5, read_timeout=5, write_timeout=5, charset='utf8mb4')
        if args.mysql_ssl == 'on':
            connection_args['ssl'] = ({'ca': str(args.ssl_ca), 'check_hostname': True} if args.ssl_ca else
                                      {'check_hostname': False, 'verify_mode': ssl.CERT_NONE})
        suffix = ' + warm-up' if not args.skip_warmup else '; warm-up desativado'
        print(f"Plano: {scenarios} cenarios; {plan['measured_events_total']} execucoes medidas{suffix}.")
        run = perform(args, workload, output, password, connection_args, environment, mapping)
        if cloud:
            save_json(output / 'cloud_metrics.json', cloud)
            render_report(output, run)
        print(f"Resultado {run['status']}: {output.resolve() / 'report.html'}")
        return 0 if run['status'] == 'completed' else 1
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f'Erro: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
