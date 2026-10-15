"""Explicit measurement quality, separate from successful SQL execution."""


def comparison_issues(run, scenario):
    measured = scenario.get('measured', {})
    client = measured.get('client', {})
    profile = run.get('profile', {})
    issues = []
    if not scenario.get('valid', measured.get('valid', False)):
        issues.append('execution_incomplete')
    if not scenario.get('warmed_comparison'):
        issues.append('warmup_not_validated')
    if client.get('successes', 0) < profile.get('min_tail_samples', 1000):
        issues.append('too_few_samples')
    if client.get('query_window_seconds', 0) < profile.get('min_measurement_seconds', 30):
        issues.append('measurement_too_short')
    if client.get('client_rows_returned') is None:
        issues.append('returned_rows_not_verified')
    if client.get('empty_result_successes') and not profile.get('allow_empty_results', False):
        issues.append('unexpected_empty_results')
    if measured.get('counter_window_valid') is False:
        issues.append('counter_window_invalid')
    observed = measured.get('observed_engine', {})
    if observed.get('scope_conflict') or observed.get('matches_client_count') is False:
        issues.append('server_client_count_conflict')
    return issues


def coverage_rows(metadata, run, cloud):
    scenarios = run.get('scenarios', [])
    measured = [s['measured'] for s in scenarios if 'measured' in s]
    instruments = metadata.get('instruments', {}).get('rows', [])
    sync = [r for r in instruments if r.get('NAME', '').startswith('wait/synch/')]
    timed = sum(r.get('ENABLED') == 'YES' and r.get('TIMED') == 'YES' for r in sync)
    cpu = metadata.get('cpu_consumer_enabled', False) and bool(measured) and all(
        any(r.get('cpu_ms_per_exec') is not None for r in m.get('diagnostics', {}).get('statements', {}).get('rows', []))
        for m in measured)
    cloud_in_window = sum(any(m.get('client', {}).get('start_unix') is not None and
        m['client']['start_unix'] <= r['unix'] <= m['client']['end_unix'] for m in measured) for r in cloud)
    environment = run.get('environment', {})
    required = ['provider', 'region', 'shape', 'cpu_unit_type', 'cpu_units', 'memory_gib', 'dataset_id']
    missing = [k for k in required if environment.get(k) in (None, '') or 'PREENCHER' in str(environment.get(k))]
    return [
        ['Execucao SQL', run.get('status'), 'Completed indica execucao; nao garante diagnostico completo ou validade estatistica.'],
        ['Variaveis globais', 'Disponivel' if metadata.get('global_variables', {}).get('available') else 'Indisponivel',
         f"{len(metadata.get('global_variables', {}).get('rows', []))} variaveis coletadas"],
        ['CPU por statement', 'Disponivel' if cpu else 'Indisponivel',
         'Exige events_statements_cpu habilitado, coluna SUM_CPU_TIME e dados no intervalo; nao equivale a CPU total do host.'],
        ['Esperas de sincronizacao', 'Disponivel' if sync and timed == len(sync) else 'Parcial/indisponivel',
         f'{timed}/{len(sync)} instrumentos habilitados e cronometrados; zero observado nao prova ausencia de espera.'],
        ['Memoria instrumentada', 'Parcial', 'Snapshots dos maiores consumidores (limite 101); veja truncated em before/after.json. Nao mede RSS total.'],
        ['CPU/memoria/storage cloud', 'Importacao presente' if cloud_in_window else 'Indisponivel na carga',
         f'{len(cloud)} pontos importados; {cloud_in_window} dentro de janelas medidas. Cada metrica/recurso depende do CSV fornecido.'],
        ['Capacidade e dados do ambiente', 'Informada pelo operador' if not missing else 'Incompleta', ', '.join(missing)],
        ['CPU/memoria do gerador e RTT isolado', 'Nao coletados', 'Latencia do cliente inclui rede, driver e servidor; nao e tempo de CPU.'],
    ]
