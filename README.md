# MySQL Sysbench Load Test

Kit genérico para executar testes de carga e estresse em MySQL com Sysbench. O runner lê queries de um arquivo SQL, executa com concorrência configurável e grava TPS, erros, reconexões, p50, p95 e p99.

## Arquivos

| Arquivo | Uso |
|---|---|
| `sysbench_sql_runner.py` | Runner principal. |
| `queries_template.sql` | Modelo para adaptar às tabelas e queries do ambiente. |
| `GUIA_TESTE_CARGA_MYSQL.md` | Passo a passo de instalação e execução em Linux. |

## Uso rápido

1. Copie `queries_template.sql` para um arquivo de workload e ajuste tabelas, colunas e faixas de IDs.
2. Valide o arquivo sem conectar ao banco:

```bash
python3 sysbench_sql_runner.py meu_workload.sql \
  --host teste --database teste --user teste --dry-run
```

3. Execute o teste:

```bash
python3 sysbench_sql_runner.py meu_workload.sql \
  --host ENDPOINT_DO_BANCO \
  --database NOME_DO_SCHEMA \
  --user USUARIO_DE_BENCHMARK \
  --threads 100 \
  --events 10000 \
  --query-mode round-robin \
  --mysql-ssl on \
  --output results/teste_run_01.txt
```

A senha é solicitada de forma oculta. Ela não é gravada no arquivo de saída nem no Lua temporário.

## Como interpretar

No final do arquivo de resultado, use a seção abaixo para a análise:

```text
# Samples collected: 10000 of 10000 expected events
overall p95 (ms): ...
overall p99 (ms): ...
```

Uma rodada é válida somente quando a quantidade de amostras for igual ao total de eventos, `ignored errors` e `reconnects` forem zero, e não existir `FATAL` nem `WARNING: INCOMPLETE RUN`.

Leia o [guia completo](GUIA_TESTE_CARGA_MYSQL.md) antes de executar a primeira rodada.
