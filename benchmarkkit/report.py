from __future__ import annotations

import csv
import html
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .collector import Collector, COUNTERS
from .core import save_json
from .quality import comparison_issues, coverage_rows


def esc(value):
    return html.escape(str(value if value is not None else "Indisponivel"))


def fmt(value):
    if isinstance(value, bool):
        return 'Sim' if value else 'Nao'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.3f}"
    return esc(value)


def table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join('<th>' + esc(h) + '</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + fmt(v) + '</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def details(title, value):
    return '<details><summary>' + esc(title) + '</summary><pre>' + esc(json.dumps(value, indent=2, ensure_ascii=False, default=str)) + '</pre></details>'


def chart(title, series, unit):
    """Self-contained SVG; gaps remain gaps, missing samples are not zero."""
    colors = ['#1769d2', '#cc4c02', '#13835e', '#8a3ffc']
    points = [(t, v) for _, data in series for t, v in data if isinstance(v, (int, float)) and math.isfinite(v)]
    if not points:
        return '<p>' + esc(title) + ': sem amostras disponiveis.</p>'
    lo, hi = min(t for t, _ in points), max(t for t, _ in points)
    bottom, top = min(0, min(v for _, v in points)), max(v for _, v in points)
    span = max(hi - lo, 1)
    yspan = max(top - bottom, 0.001)
    svg = ['<svg viewBox="0 0 900 240" role="img" aria-label="' + esc(title) + '">',
           '<path d="M60 15V200H880" fill="none" stroke="#9aa9bc"/>']
    for fraction in [0, .5, 1]:
        y = 200 - fraction * 180
        svg.append(f'<text x="6" y="{y}" font-size="12">{fmt(bottom + fraction*yspan)}</text>')
    for index, (name, data) in enumerate(series):
        color = colors[index % len(colors)]
        valid = [(t, v) for t, v in data if isinstance(v, (int, float)) and math.isfinite(v)]
        for t, v in valid:
            x, y = 60 + (t-lo)/span*820, 200 - (v-bottom)/yspan*180
            svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}"><title>{esc(name)}: {fmt(v)} {esc(unit)}; Unix {t}</title></circle>')
        svg.append(f'<text x="{60+index*200}" y="230" fill="{color}" font-size="13">{esc(name)}</text>')
    svg.append('</svg>')
    return '<h4>' + esc(title) + ' (' + esc(unit) + ')</h4>' + ''.join(svg) + '<small>Amostras no mesmo eixo UTC: ' + esc(datetime.fromtimestamp(lo, timezone.utc).isoformat()) + ' a ' + esc(datetime.fromtimestamp(hi, timezone.utc).isoformat()) + '.</small>'


def import_cloud_csv(source):
    if not source:
        return []
    rows = []
    with Path(source).open(newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        required = {'timestamp_utc', 'provider', 'resource', 'metric', 'value', 'unit', 'period_seconds'}
        if not required <= set(reader.fieldnames or []):
            raise ValueError('CSV cloud precisa das colunas: ' + ', '.join(sorted(required)))
        for row in reader:
            ts = datetime.fromisoformat(row['timestamp_utc'].replace('Z', '+00:00'))
            if ts.tzinfo is None:
                raise ValueError('timestamp_utc precisa incluir timezone (Z ou +00:00).')
            value, period = float(row['value']), float(row['period_seconds'])
            if not math.isfinite(value) or not math.isfinite(period) or period <= 0:
                raise ValueError('Valor/periodo cloud invalido.')
            rows.append({**row, 'unix': ts.timestamp(), 'value': value, 'period_seconds': period})
    return rows


STYLE = '''body{font:16px system-ui,sans-serif;color:#203047;background:#f4f7fb;margin:0}main{max-width:1200px;margin:auto;padding:28px}h1,h2,h3{color:#15539c}section{background:white;padding:24px;margin:20px 0;border:1px solid #dbe5f2;border-radius:12px}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;border-bottom:1px solid #dde5ef;padding:9px}th{background:#eaf2ff}pre{overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}details{margin:15px 0}summary{cursor:pointer;font-weight:600}a{color:#1769d2}svg{width:100%;max-height:260px}small{color:#566981}.warning{background:#fff3da;padding:12px;border-left:4px solid #d88700}.badge{padding:5px 10px;background:#eaf2ff;border-radius:5px}'''


def document(title, body):
    return '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + esc(title) + '</title><style>' + STYLE + '</style><main>' + body + '</main></html>'


def render_report(output, run):
    output = Path(output)
    metadata = json.loads((output / 'metadata.before.json').read_text())
    cloud_file = output / 'cloud_metrics.json'
    cloud = json.loads(cloud_file.read_text()) if cloud_file.exists() else []
    body = '<h1>MySQL Benchmark — ' + esc(run['label']) + '</h1><p class="badge">Estado: ' + esc(run['status']) + '</p>'
    body += '<p>' + esc(run['started_utc']) + ' · ' + esc(run['sysbench_version']) + '</p>'
    body += '<p>SQLs e casos fixos, testados individualmente. Percentis do cliente excluem warm-up. Um evento corresponde a uma execucao SQL; nao implica uma transacao de negocio.</p>'
    body += '<section><h2>Cobertura e qualidade dos dados</h2>' + table(['Informacao', 'Estado', 'Detalhe'], coverage_rows(metadata, run, cloud)) + '</section>'
    body += '<p class="warning">Use QPS e percentis deste relatorio, calculados das amostras do workload. O sysbench.log e bruto: seus comandos/QPS incluem preparacao de conexoes e seu p99 nativo pode ser zero/indisponivel. workload_summary.txt traz os valores de referencia de cada fase.</p>'
    body += '<p class="warning">Contadores da instancia incluem outras aplicacoes, trabalho de background e o coletor. Estatisticas por digest tambem podem incluir outras sessoes. CPU de statements corresponde ao tempo instrumentado no DB System; nao representa a soma de CPU dos nos analiticos. Memoria instrumentada nao equivale ao RSS total do processo.</p>'
    body += details('Capacidade e descricao do ambiente', run.get('environment', {}))
    body += details('Plano, parametros, perfil e hash de reproducibilidade', {'workload_hash': run['workload_hash'], 'profile': run['profile'], 'engine_map': run.get('engine_map'), 'workload': run['workload']})
    body += '<section><h2>Resultados por query, caso, motor e concorrencia</h2>'
    rows = []
    for s in run['scenarios']:
        c = s.get('measured', {}).get('client', {})
        rows.append([s['query_id'], s['case_id'], s['mode'], s['engine'], s['threads'], s['repetition'],
                     s['status'], s.get('warmup', {}).get('status'), c.get('successes'), c.get('errors'),
                     c.get('qps'), c.get('p50_ms'), c.get('p95_ms'), c.get('p99_ms'), c.get('max_ms'),
                     'Elegivel' if not comparison_issues(run, s) else ', '.join(comparison_issues(run, s))])
    body += table(['Query', 'Caso', 'Modo', 'Motor solicitado', 'Threads', 'Rodada', 'Estado', 'Warm-up', 'Sucessos', 'Erros', 'Workload QPS', 'p50 ms', 'p95 ms', 'p99 ms', 'Max ms', 'Comparacao'], rows)
    body += '</section>'
    sql_rank = []
    for s in run['scenarios']:
        diag = s.get('measured', {}).get('diagnostics', {})
        for r in diag.get('statements', {}).get('rows', []):
            values = r['values']
            sql_rank.append([s['id'], values.get('COUNT_STAR'),
                             values['SUM_TIMER_WAIT']/1e12 if 'SUM_TIMER_WAIT' in values else None,
                             values['SUM_CPU_TIME']/1e12 if diag.get('cpu_consumer_enabled') and 'SUM_CPU_TIME' in values else None,
                             r.get('rows_examined_per_exec'), values.get('SUM_ROWS_SENT'),
                             values.get('SUM_CREATED_TMP_TABLES'), values.get('SUM_CREATED_TMP_DISK_TABLES'),
                             values.get('COUNT_SECONDARY')])
    headers = ['Cenario', 'Execucoes servidor', 'Elapsed acumulado s', 'CPU instrumentada s',
               'Linhas examinadas/exec', 'Linhas retornadas (servidor)', 'Temporarios', 'Temporarios disco', 'Execucoes SECONDARY']
    body += '<section><h2>SQLs ordenados por tempo no servidor</h2>'
    body += '<p>Contadores por digest na instancia, durante cada fase medida. Podem incluir outras sessoes. Elapsed do servidor difere da latencia observada no cliente.</p>'
    body += table(headers, sorted(sql_rank, key=lambda r: r[2] or 0, reverse=True))
    cpu_rank = [r for r in sql_rank if r[3] is not None]
    body += '<h3>SQLs ordenados por CPU instrumentada</h3>'
    body += table(headers, sorted(cpu_rank, key=lambda r: r[3], reverse=True)) if cpu_rank else '<p>CPU por statement indisponivel neste teste. Consulte a configuracao do consumer e as metricas cloud.</p>'
    body += '</section>'
    for s in run['scenarios']:
        body += '<section><h2>' + esc(s['id']) + '</h2><pre>' + esc(s['sql']) + '</pre>'
        warmup = s.get('warmup', {})
        body += '<p>Warm-up: ' + esc(warmup.get('status')) + '. Novas conexoes sao abertas em cada janela e na medicao; o cache do servidor e preservado.</p>'
        windows = warmup.get('windows', [])
        body += table(['Janela', 'Execucoes cliente', 'Contagem por digest coincide?', 'Tempo sob carga s', 'p50 ms', 'QPS', 'Leituras fisicas/execucao (instancia)', 'Estado'],
                      [[i+1, w['client'].get('successes'), w.get('observed_engine', {}).get('matches_client_count'), w['client'].get('query_window_seconds'), w['client'].get('p50_ms'), w['client'].get('qps'), w.get('physical_reads_per_query'), w['status']] for i, w in enumerate(windows)])
        body += details('Criterios e evidencias do warm-up', {k: v for k, v in warmup.items() if k != 'windows'})
        m = s.get('measured')
        if m:
            c, diag = m['client'], m.get('diagnostics', {})
            if not m.get('valid') or not s.get('warmed_comparison'):
                body += '<p class="warning">Rodada incompleta ou sem warm-up estabilizado. Nao usar como comparacao aquecida validada.</p>'
            issues = comparison_issues(run, s)
            if issues:
                body += '<p class="warning">Cenario excluido das medianas comparativas: ' + esc(', '.join(issues)) + '. Limites minimos sao criterios operacionais, nao garantia estatistica de p99.</p>'
            if c.get('empty_result_successes'):
                body += '<p class="warning">Resultados vazios: ' + esc(c['empty_result_successes']) + ' execucoes. Confirme se essa carga e intencional.</p>'
            body += details('Erros de inicializacao/execucao Sysbench', m.get('fatal_messages', []))
            if m.get('observed_engine', {}).get('matches_client_count') is False:
                observed = m['observed_engine']
                server_count = observed['primary'] + observed['secondary']
                body += '<p class="warning">Contagens divergentes: ' + esc(c.get('successes')) + ' sucessos no cliente e ' + esc(server_count) + ' execucoes no delta por digest do servidor. As duas evidencias foram preservadas; a causa nao foi determinada. Nao equivale, por si so, a perda de consultas. Cenario excluido da comparacao.</p>'
            if c.get('small_tail_sample'):
                body += '<p class="warning">Menos de 1.000 amostras: p99 tem poucas observacoes na cauda; avaliar variacao entre rodadas.</p>'
            body += details('Motor configurado e evidencias de execucao', {'session_setting': m.get('session_engine'), 'observed': m.get('observed_engine')})
            body += details('Configuracoes reais das conexoes de carga e TLS', {'sessions': m.get('worker_sessions'),
                            'routing_verified': m.get('session_routing_verified'),
                            'tls_requested': m.get('tls_requested'), 'cipher_present': m.get('tls_cipher_present_all_workers')})
            body += table(['Metrica', 'Valor'], [[k, v] for k, v in c.items() if k not in ['timeline', 'error_examples', 'per_thread_attempts']])
            timeline = c.get('timeline', [])
            body += chart('Latencia do cliente por segundo (percentis de cada janela)', [(p, [(r['unix'], r.get(p+'_ms')) for r in timeline]) for p in ['p50', 'p95', 'p99']], 'ms')
            body += chart('Execucoes concluidas por segundo', [('Sucessos', [(r['unix'], r['samples']) for r in timeline]), ('Erros', [(r['unix'], r['errors']) for r in timeline])], 'execucoes')
            mysql_file = output / s['directory'] / 'measured' / 'mysql_timeline.jsonl'
            samples = [json.loads(line) for line in mysql_file.read_text().splitlines()] if mysql_file.exists() else []
            mysql = [(r.get('unix'), Collector.pairs(r.get('global_status', {}))) for r in samples if r.get('unix')]
            body += chart('Conexoes e trabalho ativo na instancia', [(name, [(t, float(v[name]) if name in v else None) for t, v in mysql]) for name in ['Threads_connected', 'Threads_running']], 'conexoes/threads')
            body += chart('Buffer pool: ocupacao no tempo', [(name, [(t, float(v[name]) if name in v else None) for t, v in mysql]) for name in ['Innodb_buffer_pool_pages_data', 'Innodb_buffer_pool_pages_free', 'Innodb_buffer_pool_pages_dirty']], 'paginas')
            rate_series = []
            for name in ['Innodb_buffer_pool_reads', 'Innodb_data_reads', 'Innodb_data_writes']:
                rates = []
                for (old_t, old), (new_t, new) in zip(mysql, mysql[1:]):
                    if name in old and name in new and new_t > old_t and float(new[name]) >= float(old[name]):
                        rates.append((new_t, (float(new[name])-float(old[name])) / (new_t-old_t)))
                rate_series.append((name, rates))
            body += chart('Leituras e escritas observadas no MySQL', rate_series, 'operacoes/s')
            cloud_groups = defaultdict(list)
            for row in cloud:
                # Keep original provider period, units and resource, with no interpolation.
                if c.get('start_unix') is not None and c['start_unix'] <= row['unix'] <= c['end_unix']:
                    cloud_groups[(row['provider'], row['resource'], row['metric'], row['unit'])].append(row)
            if not cloud_groups:
                body += '<p>Metricas cloud de CPU/memoria/storage: nenhuma amostra importada dentro da janela medida.</p>'
            for group, records in cloud_groups.items():
                body += chart(' / '.join(group[:3]), [('Cloud', [(r['unix'], r['value']) for r in records])], group[3])
                body += '<small>Periodos originais em segundos: ' + esc(sorted({r['period_seconds'] for r in records})) + '.</small>'
            body += '<h3>Cache, I/O, concorrencia e carga — diferencas no intervalo</h3>'
            body += '<p>Duracao das queries: ' + fmt(c.get('query_window_seconds')) + ' s; intervalo dos contadores globais: ' + fmt(diag.get('seconds')) + ' s; intervalo ampliado dos snapshots detalhados: ' + fmt(diag.get('deep_snapshot_seconds', diag.get('seconds'))) + ' s.</p>'
            body += details('Limites temporais e escopo dos contadores', diag.get('counter_window', {'source': 'legacy_deep_snapshots', 'scope': 'Registro antigo: inclui preparacao/coleta. Nao e taxa exclusiva da carga.'}))
            body += '<p>As taxas abaixo usam o intervalo dos contadores, que inclui os round trips das amostragens. Outras sessoes/background/coletor podem contribuir. I/O e waits detalhados usam snapshots mais amplos; nao sao IOPS exclusivos do workload.</p>'
            delta = diag.get('global_counter_delta', {}).get('values', {})
            hit_ratio = diag.get('buffer_pool_hit_pct_interval')
            body += '<p>Hit ratio aproximado do buffer pool no intervalo: ' + fmt(hit_ratio) + ('%' if hit_ratio is not None else '') + '.</p>'
            body += table(['Contador da instancia', 'Delta', 'Por segundo'], [[k, delta[k], diag.get('global_rates_per_second', {}).get(k)] for k in COUNTERS if k in delta])
            for name, label in [('statements', 'SQL: CPU, linhas, temporarios e ordenacoes'), ('file_io', 'I/O instrumentado pelo MySQL'), ('table_io', 'I/O por tabela'), ('table_locks', 'Locks por tabela')]:
                body += details(label, diag.get(name, {}))
            waits = diag.get('waits', {}).get('rows', [])
            waits = [r for r in waits if r.get('EVENT_NAME') != 'idle' and r['values'].get('COUNT_STAR', 0) > 0]
            waits = sorted(waits, key=lambda r: r['values'].get('SUM_TIMER_WAIT', 0), reverse=True)
            body += '<h3>Principais esperas da instancia</h3>' + table(['Evento', 'Ocorrencias', 'Tempo acumulado s'], [[r.get('EVENT_NAME'), r['values'].get('COUNT_STAR'), r['values'].get('SUM_TIMER_WAIT', 0)/1e12] for r in waits[:20]])
            body += '<p>Idle fica nos snapshots, fora deste ranking. Tempos acumulados podem exceder a duracao da rodada devido a concorrencia. Nao representam percentuais de DB time do Oracle AWR.</p>'
            body += details('Erros individuais', c.get('error_examples', []))
        explain_file = output / s['directory'] / 'explain.json'
        if explain_file.exists():
            body += details('EXPLAIN FORMAT=JSON (fora da medicao)', json.loads(explain_file.read_text()))
        base = s['directory']
        body += '<p>Evidencias: <a href="' + esc(base) + '/">pasta do cenario</a>'
        if m:
            for file in ['before.json', 'after.json', 'load.before.json', 'load.after.json', 'diagnostics.json', 'mysql_timeline.jsonl', 'client_samples.csv.gz', 'workload_summary.txt', 'sysbench.log']:
                if (output / base / 'measured' / file).exists():
                    body += ' · <a href="' + esc(base + '/measured/' + file) + '">' + esc(file) + '</a>'
        body += '</p></section>'
    body += '<section><h2>Configuracao completa e cobertura da coleta</h2>'
    for name, value in metadata.items():
        body += details(name, value)
    body += details('Capacidades e falhas de coleta', run.get('collector_capabilities', {}))
    body += '<p><a href="metadata.after.json">Configuracao final</a> · <a href="run.json">Resultado estruturado</a> · <a href="summary.csv">Resumo CSV</a></p></section>'
    (output / 'report.html').write_text(document('MySQL Benchmark — ' + run['label'], body), encoding='utf-8')


def compare_reports(run_paths, output):
    if len({Path(p).resolve() for p in run_paths}) != len(run_paths):
        raise ValueError('A mesma pasta foi informada mais de uma vez.')
    runs = [json.loads((Path(p) / 'run.json').read_text()) for p in run_paths]
    identities = [r.get('run_id') for r in runs if r.get('run_id')]
    if len(set(identities)) != len(identities):
        raise ValueError('As pastas incluem copias da mesma execucao (run_id repetido).')
    labels = [f'R{i+1:02d} | {r["label"]} | {Path(p).name}' for i, (r, p) in enumerate(zip(runs, run_paths))]
    body = '<h1>Comparacao MySQL Benchmark</h1>'
    same_workload = len({r['workload_hash'] for r in runs}) == 1
    same_profile = len({json.dumps(r['profile'], sort_keys=True) for r in runs}) == 1
    same_map = len({json.dumps(r.get('engine_map'), sort_keys=True) for r in runs}) == 1
    body += '<p>Mesmo SQL/casos: ' + str(same_workload) + '; mesmo perfil: ' + str(same_profile) + '; mesmo mapa de motores: ' + str(same_map) + '.</p>'
    body += '<p class="warning">Igualdade dos dados de negocio e carga concorrente externa nao e comprovada pelo hash do SQL. Verifique dataset_id, estado dos dados e evidencias do motor. Medianas de percentis por rodada nao sao percentis de amostras combinadas.</p>'
    if not (same_workload and same_profile and same_map):
        body += '<p class="warning">Planos diferentes: esta comparacao nao e equivalente.</p>'
    groups = defaultdict(list)
    excluded = []
    for r, label in zip(runs, labels):
        for s in r['scenarios']:
            issues = comparison_issues(r, s)
            if not issues:
                groups[(s['query_id'], s['case_id'], s.get('mode', s['engine']), s['engine'], s['threads'], label)].append(s['measured']['client'])
            else:
                excluded.append([label, s.get('id', s['query_id']), ', '.join(issues)])
        body += details('Ambiente ' + label, r.get('environment', {}))
    body += table(['Query', 'Caso', 'Modo', 'Motor solicitado', 'Threads', 'Execucao / ambiente', 'Rodadas elegiveis', 'Mediana workload QPS', 'Mediana p95 ms', 'Mediana p99 ms'],
                  [[*key, len(rows), *[statistics.median(c[field] for c in rows) for field in ['qps', 'p95_ms', 'p99_ms']]] for key, rows in sorted(groups.items())])
    body += '<h2>Cenarios excluidos</h2>' + table(['Execucao', 'Cenario', 'Motivo'], excluded)
    all_vars = []
    for p in run_paths:
        meta = json.loads((Path(p) / 'metadata.before.json').read_text())
        all_vars.append(Collector.pairs(meta['global_variables']))
    differences = [[key, *[v.get(key, 'Indisponivel') for v in all_vars]] for key in sorted(set().union(*all_vars)) if len({v.get(key) for v in all_vars}) > 1]
    body += '<h2>Variaveis globais diferentes</h2>' + table(['Variavel', *labels], differences)
    output = Path(output)
    if output.exists():
        raise ValueError('O arquivo de comparacao ja existe; use outro nome.')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document('Comparacao MySQL', body), encoding='utf-8')
    return output
