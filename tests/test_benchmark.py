import csv
import io
import json
import shutil
import subprocess
import tempfile
import time
from html.parser import HTMLParser
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from benchmarkkit.collector import derive, numeric_rows_delta
from benchmarkkit.core import (build_lua, counter_delta, latency_metrics, load_workload,
                               save_json, summarize_samples, validate_select, warmup_stable)
from benchmarkkit.report import compare_reports, import_cloud_csv, render_report
from benchmarkkit.runner import choose_engine, execute_phase, warm_up
from benchmarkkit.quality import comparison_issues, coverage_rows
from benchmarkkit.runner import write_summary_csv


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.context = tempfile.TemporaryDirectory()
        self.root = Path(self.context.name)

    def tearDown(self):
        self.context.cleanup()

    def workload(self, text):
        source = self.root / 'queries.sql'
        source.write_text(text)
        return source

    def test_reproducible_cases_and_shared_token(self):
        source = self.workload("SELECT {{RAND_INT:id:1:9999}} AS a, {{RAND_INT:id:1:9999}} AS b;")
        one = load_workload(source, 4, 42)
        two = load_workload(source, 4, 42)
        self.assertEqual(one, two)
        self.assertNotEqual(one['hash'], load_workload(source, 4, 43)['hash'])
        for case in one['queries'][0]['cases']:
            value = str(case['parameters']['id'])
            self.assertEqual(case['sql'].count(value), 2)

    def test_comments_and_semicolon_in_literal(self):
        source = self.workload("-- comment\nSELECT ';' AS value;\n# tail comment")
        self.assertEqual(len(load_workload(source)['queries']), 1)

    def test_reject_write_and_locking_queries(self):
        for sql in ['DELETE FROM t', 'SELECT * FROM t FOR UPDATE', "SELECT 1 INTO OUTFILE '/tmp/file'", 'WITH x AS (SELECT 1) DELETE FROM t', 'SELECT 1; SELECT 2', "SELECT /*+ SET_VAR(use_secondary_engine=ON) */ 1"]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                validate_select(sql)

    def test_fixed_cases_override_template(self):
        source = self.workload('SELECT 1;')
        cases = self.root / 'cases.json'
        cases.write_text(json.dumps({'Q01': [{'name': 'Small', 'sql': 'SELECT 2'}, {'name': 'Large', 'sql': 'SELECT 3'}]}))
        workload = load_workload(source, cases_file=cases)
        self.assertEqual([c['sql'] for c in workload['queries'][0]['cases']], ['SELECT 2', 'SELECT 3'])

    def test_conflicting_token_bounds_rejected(self):
        source = self.workload('SELECT {{RAND_INT:id:1:2}}, {{RAND_INT:id:3:4}};')
        with self.assertRaises(ValueError):
            load_workload(source)

    def test_counter_reset_not_negative_rate(self):
        value = counter_delta({'reads': 100}, {'reads': 2})
        self.assertNotIn('reads', value['values'])
        self.assertEqual(value['decreased_or_reset'], ['reads'])

    def test_large_counters_keep_integer_precision(self):
        big = 2**60
        self.assertEqual(counter_delta({'timer': big}, {'timer': big+1})['values']['timer'], 1)

    def test_client_row_count_preserved(self):
        file = self.root / 'thread-0.csv'
        file.write_text('thread,sequence,start_unix,elapsed_ms,success,rows_returned,error_code,error\n'
                        '0,1,1000,10,1,3,,\n0,2,1001,10,1,4,,\n')
        self.assertEqual(summarize_samples(self.root, 1, 2)['client_rows_returned'], 7)

    def test_digest_delta_computes_average_not_difference_of_averages(self):
        before = {'available': True, 'rows': [{'DIGEST': 'x', 'COUNT_STAR': 10, 'SUM_TIMER_WAIT': 1000}]}
        after = {'available': True, 'rows': [{'DIGEST': 'x', 'COUNT_STAR': 12, 'SUM_TIMER_WAIT': 3000, 'MAX_TIMER_WAIT': 9000}]}
        row = numeric_rows_delta(before, after, ['DIGEST'])['rows'][0]['values']
        self.assertEqual(row['COUNT_STAR'], 2)
        self.assertEqual(row['SUM_TIMER_WAIT'], 2000)
        self.assertNotIn('MAX_TIMER_WAIT', row)

    def test_percentile_nearest_rank(self):
        result = latency_metrics(list(range(1, 101)))
        self.assertEqual(result['p95_ms'], 95)
        self.assertEqual(result['p99_ms'], 99)

    def windows(self):
        return [{'valid': True, 'client': {'successes': 20, 'p50_ms': 100+i, 'qps': 20+i/10}, 'physical_reads_per_query': 0} for i in range(3)]

    def test_stability_needs_three_valid_windows(self):
        windows = self.windows()
        self.assertFalse(warmup_stable(windows[:2], 3, .15, 10)[0])
        self.assertTrue(warmup_stable(windows, 3, .15, 10)[0])
        windows[-1]['client']['p50_ms'] = 1000
        self.assertFalse(warmup_stable(windows, 3, .15, 10)[0])

    def test_absent_cache_does_not_mean_zero_and_heatwave_does_not_require_innodb_io(self):
        windows = self.windows()
        for w in windows:
            w['physical_reads_per_query'] = None
        self.assertFalse(warmup_stable(windows, 3, .15, 10)[0])
        self.assertTrue(warmup_stable(windows, 3, .15, 10, require_io=False)[0])

    def test_analytic_warmup_ignores_available_but_unstable_innodb_reads(self):
        windows = self.windows()
        for w, reads in zip(windows, [0, 10, 0]):
            w['physical_reads_per_query'] = reads
        self.assertTrue(warmup_stable(windows, 3, .15, 10, require_io=False)[0])
        self.assertFalse(warmup_stable(windows, 3, .15, 10, require_io=True)[0])

    def test_empty_results_distinguished_from_missing_legacy_row_counts(self):
        file = self.root / 'thread-0.csv'
        file.write_text('thread,sequence,start_unix,elapsed_ms,success,rows_returned\n'
                        '0,1,1000,10,1,0\n0,2,1001,10,1,1\n')
        result = summarize_samples(self.root, 1, 2)
        self.assertEqual(result['empty_result_successes'], 1)
        self.assertEqual(result['client_rows_returned'], 1)
        self.write_samples([(1,10,1)])
        self.assertIsNone(summarize_samples(self.root, 1, 1)['empty_result_successes'])

    def test_engine_map_specific_case_and_forced_modes(self):
        mapping = {'Q01': 'heatwave', 'Q01/C02': 'innodb'}
        self.assertEqual(choose_engine('mixed', mapping, 'Q01', 'C01'), 'heatwave')
        self.assertEqual(choose_engine('mixed', mapping, 'Q01', 'C02'), 'innodb')
        with self.assertRaises(ValueError):
            choose_engine('mixed', mapping, 'Q02', 'C01')
        lua = build_lua('SELECT 1', self.root, 1, 2, 30000, 'FORCED')
        self.assertIn("use_secondary_engine='FORCED'", lua)
        self.assertNotIn("use_secondary_engine='ON'", lua)
        self.assertLess(lua.index('SELECT CONNECTION_ID()'), lua.index("use_secondary_engine='FORCED'"))

    def write_samples(self, rows):
        path = self.root / 'thread-0.csv'
        fields = ['thread', 'sequence', 'start_unix', 'elapsed_ms', 'success', 'error_code', 'error']
        with path.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for seq, elapsed, success in rows:
                writer.writerow(dict(thread=0, sequence=seq, start_unix=1000+seq, elapsed_ms=elapsed,
                                     success=success, error_code='2013' if not success else '', error=''))

    def test_errors_not_mixed_into_successful_percentiles(self):
        self.write_samples([(1, 10, 1), (2, 999, 0)])
        summary = summarize_samples(self.root, 1, 2)
        self.assertEqual(summary['p99_ms'], 10)
        self.assertEqual(summary['errors'], 1)
        self.assertTrue(summary['quota_complete'])

    def test_duplicate_or_missing_samples_invalidate_quota(self):
        self.write_samples([(1, 10, 1), (1, 10, 1)])
        summary = summarize_samples(self.root, 1, 2)
        self.assertEqual(summary['malformed_or_duplicate'], 1)
        self.assertFalse(summary['quota_complete'])

    def test_cpu_unavailable_not_zero(self):
        snap = {'unix': 100, 'global_status': {'available': True, 'rows': []},
                'statements': {'available': True, 'rows': [{'SCHEMA_NAME': 'db', 'DIGEST': 'x', 'COUNT_STAR': 1, 'SUM_CPU_TIME': 0}]}}
        after = {**snap, 'unix': 101, 'statements': {'available': True, 'rows': [{'SCHEMA_NAME': 'db', 'DIGEST': 'x', 'COUNT_STAR': 2, 'SUM_CPU_TIME': 0}]}}
        self.assertIsNone(derive(snap, after, False)['statements']['rows'][0]['cpu_ms_per_exec'])

    def test_cloud_preserves_period_and_timezone(self):
        file = self.root / 'cloud.csv'
        file.write_text('timestamp_utc,provider,resource,metric,value,unit,period_seconds\n2026-10-14T15:00:00Z,oci,dbsystem,CPUUtilization,20,percent,60\n')
        self.assertEqual(import_cloud_csv(file)[0]['period_seconds'], 60)
        file.write_text(file.read_text().replace('15:00:00Z', '15:00:00'))
        with self.assertRaises(ValueError):
            import_cloud_csv(file)

    def test_html_escapes_sql(self):
        save_json(self.root / 'metadata.before.json', {'global_variables': {'available': True, 'rows': []}})
        run = {'label': '<script>alert(1)</script>', 'status': 'incomplete', 'started_utc': 'now',
               'sysbench_version': 'test', 'scope': 'test', 'profile': {}, 'workload_hash': 'x',
               'workload': {}, 'scenarios': []}
        render_report(self.root, run)
        html = (self.root / 'report.html').read_text()
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_compare_excludes_unstable_and_shows_config_differences(self):
        roots = []
        for n in range(2):
            directory = self.root / str(n)
            directory.mkdir()
            save_json(directory / 'metadata.before.json', {'global_variables': {'available': True, 'rows': [
                {'Variable_name': 'innodb_buffer_pool_size', 'Value': str((n+1)*1024)}]}})
            valid = self.eligible_scenario()
            unstable = {**valid, 'warmed_comparison': False, 'measured': {'client': {'qps': 9999, 'p95_ms': 9999, 'p99_ms': 9999}}}
            save_json(directory / 'run.json', {'label': str(n), 'workload_hash': 'same', 'profile': {},
                      'scenarios': [valid, unstable]})
            roots.append(directory)
        file = compare_reports(roots, self.root / 'compare.html')
        report = file.read_text()
        self.assertNotIn('9,999', report)
        self.assertIn('innodb_buffer_pool_size', report)
        self.assertIn('<td>11</td>', report)
        with self.assertRaises(ValueError):
            compare_reports(roots, file)

    def eligible_scenario(self, mode='innodb', engine='innodb'):
        return {'id':mode, 'valid':True, 'status':'completed', 'warmed_comparison':True,
                'query_id':'Q01', 'case_id':'C01', 'mode':mode, 'engine':engine, 'threads':1,
                'repetition':1, 'warmup':{'status':'stable'}, 'measured':{'valid':True,
                'counter_window_valid':True, 'client':{'successes':1000,'errors':0,'qps':10,
                'p95_ms':10,'p99_ms':11,'client_rows_returned':1000,'empty_result_successes':0,
                'query_window_seconds':100}}}

    def test_comparison_separates_modes_and_same_label_runs(self):
        roots=[]
        for n in range(2):
            folder=self.root/str(n);folder.mkdir();roots.append(folder)
            save_json(folder/'metadata.before.json',{'global_variables':{'available':True,'rows':[]}})
            save_json(folder/'run.json',{'run_id':str(n),'label':'MySQL','profile':{},'workload_hash':'x',
                'scenarios':[self.eligible_scenario('heatwave','heatwave'),self.eligible_scenario('mixed','heatwave')]})
        class Rows(HTMLParser):
            def __init__(self): super().__init__();self.rows=[];self.row=None;self.cell=None
            def handle_starttag(self, tag, attrs):
                if tag=='tr':self.row=[]
                if tag=='td':self.cell=''
            def handle_data(self,data):
                if self.cell is not None:self.cell+=data
            def handle_endtag(self,tag):
                if tag=='td':self.row.append(self.cell);self.cell=None
                if tag=='tr' and self.row:self.rows.append(self.row)
        parsed=Rows();parsed.feed(compare_reports(roots,self.root/'comparison.html').read_text())
        rows=[r for r in parsed.rows if r[0]=='Q01']
        self.assertEqual(len(rows),4)
        self.assertEqual({r[2] for r in rows},{'mixed','heatwave'})
        self.assertEqual(len({r[5] for r in rows}),2)
        self.assertTrue(all(r[6]=='1' for r in rows))
        with self.assertRaises(ValueError):compare_reports([roots[0],roots[0]],self.root/'duplicate.html')

    def test_summary_csv_preserves_run_scenario_and_mode(self):
        run={'run_id':'test-id','label':'MySQL','profile':{},'scenarios':[
            self.eligible_scenario('heatwave','heatwave'),self.eligible_scenario('mixed','heatwave')]}
        write_summary_csv(self.root,run)
        with (self.root/'summary.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual([r['mode'] for r in rows],['heatwave','mixed'])
        self.assertTrue(all(r['run_id']=='test-id' for r in rows))

    def test_short_successful_run_is_not_performance_qualified(self):
        scenario=self.eligible_scenario()
        scenario['measured']['client'].update(successes=10,query_window_seconds=1.6)
        issues=comparison_issues({'profile':{}},scenario)
        self.assertIn('too_few_samples',issues)
        self.assertIn('measurement_too_short',issues)
        self.assertNotIn('execution_incomplete',issues)

    def test_server_count_conflict_excludes_otherwise_complete_run(self):
        scenario = self.eligible_scenario()
        scenario['measured']['observed_engine'] = {
            'primary': 992, 'secondary': 0, 'matches_client_count': False}
        self.assertEqual(comparison_issues({'profile': {}}, scenario), ['server_client_count_conflict'])

    def test_coverage_does_not_turn_completed_into_full_telemetry(self):
        rows=coverage_rows({'cpu_consumer_enabled':False,'instruments':{'rows':[
            {'NAME':'wait/synch/mutex/x','ENABLED':'NO','TIMED':'NO'}]}},
            {'status':'completed','scenarios':[],'environment':{}},[])
        lookup={r[0]:r for r in rows}
        self.assertEqual(lookup['CPU por statement'][1],'Indisponivel')
        self.assertEqual(lookup['Capacidade e dados do ambiente'][1],'Incompleta')
        self.assertIn('0/1',lookup['Esperas de sincronizacao'][2])

    def test_counter_rates_use_actual_lightweight_query_timestamps(self):
        before={'unix':0,'monotonic':0,'global_status':{'available':True,'started_monotonic':10,
                'rows':[{'Variable_name':'Com_select','Value':'100'}]}}
        after={'unix':100,'monotonic':100,'global_status':{'available':True,'started_monotonic':12,
                'rows':[{'Variable_name':'Com_select','Value':'120'}]}}
        diag=derive(before,after)
        self.assertEqual(diag['seconds'],2)
        self.assertEqual(diag['global_rates_per_second']['Com_select'],10)

    @unittest.skipUnless(shutil.which('sysbench'), 'Sysbench not installed')
    def test_real_sysbench_gates_exclude_setup_and_preserve_quota(self):
        lua=self.root/'gate.lua';lua.write_text(build_lua('SELECT 1',self.root,3,9,30000,gated=True))
        process=subprocess.Popen(['sysbench',str(lua),'--mysql-dry-run=on','--threads=3',
            '--events=0','--time=0','run'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        def wait_markers(prefix):
            deadline=time.monotonic()+10
            while len(list(self.root.glob(prefix+'-*')))<3 and process.poll() is None:
                if time.monotonic()>deadline:self.fail('Barrier timed out: '+prefix)
                time.sleep(.01)
            self.assertEqual(len(list(self.root.glob(prefix+'-*'))),3)
        try:
            wait_markers('ready')
            self.assertFalse(list(self.root.glob('done-*')))
            (self.root/'start.signal').touch()
            wait_markers('done')
            self.assertIsNone(process.poll())
            summary=summarize_samples(self.root,3,9)
            self.assertEqual(summary['successes'],9)
            self.assertTrue(summary['quota_complete'])
            (self.root/'finish.signal').touch()
            log=process.communicate(timeout=10)[0]
            self.assertEqual(process.returncode,0,log)
        finally:
            if process.poll() is None:process.kill();process.communicate()

    def test_qps_uses_monotonic_duration_with_wall_clock_jump(self):
        file = self.root / 'thread-0.csv'
        file.write_text('thread,sequence,start_unix,start_monotonic,elapsed_ms,success,error_code,error\n'
                        '0,1,1000,10,100,1,,\n0,2,900,11,100,1,,\n')
        summary = summarize_samples(self.root, 1, 2)
        self.assertAlmostEqual(summary['query_window_seconds'], 1.1)
        self.assertAlmostEqual(summary['qps'], 2/1.1)

    def test_warmup_respects_minimum_load_time(self):
        args = SimpleNamespace(skip_warmup=False, warmup_max_seconds=100,
                               warmup_window_seconds=2, warmup_stable_windows=3,
                               warmup_tolerance=.15, warmup_min_samples=5,
                               warmup_min_events=30, warmup_min_seconds=10)
        phase = {'valid': True, 'status': 'completed', 'physical_reads_per_query': 0,
                 'client': {'successes': 10, 'qps': 5, 'p50_ms': 100, 'query_window_seconds': 2}}
        with patch('benchmarkkit.runner.execute_phase', return_value=phase) as execute:
            result = warm_up(args, None, {}, {'sql': 'SELECT 1'}, 'Q01/C01', 'innodb', 1,
                             self.root / 'warmup', 'not-used', {})
        self.assertTrue(result['stable'])
        self.assertEqual(execute.call_count, 5)
        self.assertEqual(result['load_seconds'], 10)

    def test_unsupported_analytic_engine_never_silently_runs_primary(self):
        result = execute_phase(None, None, {'primary_engine_session_setting_supported': False},
                               {'sql': 'SELECT 1'}, 'Q01/C01', 'heatwave', 1, 1, 0,
                               self.root / 'unsupported', '', {})
        self.assertFalse(result['valid'])
        self.assertEqual(result['status'], 'engine_unavailable')

    def test_phase_timeout_includes_setup_and_does_not_release_workload_late(self):
        directory = self.root / 'timeout'
        args = SimpleNamespace(sysbench='sysbench', query_timeout_ms=100, database='test',
            host='localhost', port=3306, user='test', mysql_ssl='off',
            monitor_interval=1, phase_timeout=1, allow_empty_results=False)
        snapshot = {'unix': 100, 'global_status': {'available': True, 'rows': []}}
        collector = Mock()
        clock = [0.0]
        def slow_snapshot(*unused):
            clock[0] = 2.0
            return snapshot
        collector.snapshot.side_effect = slow_snapshot
        collector.boundary_snapshot.return_value = snapshot
        process = Mock(stdout=io.StringIO(''))
        process.poll.return_value = None
        process.wait.return_value = 0
        def start_process(*unused, **kwargs):
            (directory / 'ready-0').touch()
            return process
        with patch('benchmarkkit.runner.subprocess.Popen', side_effect=start_process), \
             patch('benchmarkkit.runner.time.monotonic', side_effect=lambda: clock[0]), \
             patch('benchmarkkit.runner.Sampler'):
            result = execute_phase(args, collector,
                {'primary_engine_session_setting_supported': True, 'cpu_consumer_enabled': False},
                {'sql': 'SELECT 1'}, 'Q01/C01', 'innodb', 1, 1, 0, directory, '', {})
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['valid'])
        self.assertFalse((directory / 'start.signal').exists())
        process.terminate.assert_called()

    @unittest.skipUnless(shutil.which('sysbench'), 'Sysbench not installed')
    def test_real_sysbench_dry_driver_exact_per_thread_quota(self):
        lua = self.root / 'test.lua'
        lua.write_text(build_lua('SELECT 1', self.root, 7, 101, 30000))
        result = subprocess.run(['sysbench', str(lua), '--mysql-dry-run=on', '--threads=7',
                                 '--events=0', '--time=0', '--verbosity=3', 'run'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = summarize_samples(self.root, 7, 101)
        self.assertEqual(summary['successes'], 101)
        self.assertTrue(summary['quota_complete'])
        self.assertIn('total number of events:              101', result.stdout)


if __name__ == '__main__':
    unittest.main()
