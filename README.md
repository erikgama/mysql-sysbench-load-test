# MySQL Sysbench Load Test

Kit genérico para executar testes de carga e estresse em MySQL com Python e Sysbench. Escolha o benchmark avançado para diagnóstico detalhado ou o runner simples para uma carga mista de queries.

## Benchmark avançado: queries individuais e diagnóstico

O novo `mysql_benchmark.py` testa cada query/caso separadamente, com quotas por thread, warm-up por estabilidade, coleta SQL antes/durante/depois e relatório HTML independente de internet. Suporta InnoDB, HeatWave forçado, escolha automática e mapa de destino por query. O runner simples abaixo continua disponível.

O relatório distingue execução completa, cobertura do diagnóstico e elegibilidade para comparação. Detecta resultados vazios, registra as janelas dos contadores e separa modos/ambientes na comparação. As métricas de referência vêm das amostras do workload; o log nativo do Sysbench é mantido para diagnóstico.

No final do HTML, `SHOW GLOBAL VARIABLES` aparece em uma tabela aberta com todas as variáveis coletadas, valores inicial/final e indicação de alterações — não apenas em JSON expansível.

**Comece pelo [passo a passo do novo script](GUIA_BENCHMARK_AVANCADO.md)**: instalação no Linux, download do projeto, arquivo SQL, execução e leitura do HTML. Não é necessário MySQL Shell nem o cliente `mysql`.

A senha é solicitada de forma oculta. O guia também explica casos fixos, CPU/storage da cloud e comparação de ambientes. Exemplos públicos AirportDB, casos e mapa de motores estão em [`examples/`](examples/). Ajuste-os ao banco do cliente. CPU e outros dados dependem da instrumentação disponível; informações ausentes são identificadas no relatório.

Veja as [evidências e limites da validação](VALIDACAO_BENCHMARK.md). Os testes funcionais não devem ser utilizados como comparação de performance entre clouds.

## Pré-requisitos

| Item | Obrigatório? | Uso |
|---|---|---|
| Python 3 | Sim | Coordena a execução; o benchmark avançado exige Python 3.10+. |
| Sysbench 1.0+ com suporte a MySQL | Sim | Abre as conexões e executa a carga. |
| PyMySQL | Apenas no avançado e monitor de backup | Coleta informações usando conexões separadas; não substitui o Sysbench na carga. |
| Acesso de rede, endpoint e usuário MySQL | Sim | Permite chegar ao banco que será testado. |
| MySQL Shell ou cliente `mysql` | Não | São opcionais para testar conectividade ou montar queries. O benchmark não precisa deles. |

No Linux, instale Python 3 e Sysbench:

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y python3 sysbench

# Distribuições com dnf
sudo dnf install -y python3 sysbench
```

Valide a instalação:

```bash
python3 --version
sysbench --version
sysbench --help | grep -- '--mysql-host'
```

O último comando deve mostrar a opção `--mysql-host`, confirmando que o Sysbench reconhece os parâmetros de MySQL.

## Arquivos

| Arquivo | Uso |
|---|---|
| `mysql_benchmark.py` | Novo benchmark individual, warm-up, diagnóstico e comparação. |
| `benchmarkkit/` | Coleta MySQL, execução e geração do relatório HTML. |
| `requirements-benchmark.txt` | Dependência Python do benchmark avançado. |
| `GUIA_BENCHMARK_AVANCADO.md` | Passo a passo do novo script, desde o download. |
| `VALIDACAO_BENCHMARK.md` | Testes realizados e limites conhecidos. |
| `examples/` | Queries AirportDB, casos fixos, mapa de motores e capacidade. |
| `sysbench_sql_runner.py` | Runner simples para carga mista round-robin ou aleatória. |
| `queries_template.sql` | Modelo para adaptar às tabelas e queries do ambiente. |
| `GUIA_TESTE_CARGA_MYSQL.md` | Passo a passo de instalação e execução em Linux. |
| `backup_read_monitor.py` | Monitor de disponibilidade e latência durante backup, com conexões persistentes. |

## Uso rápido do runner simples

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

## Monitorar leituras durante um backup

Para validar disponibilidade e latência durante um backup, instale o único driver adicional em um ambiente virtual e execute o monitor. Ele mantém 100 conexões abertas e dispara uma query somente-leitura em paralelo a cada dois segundos. A senha é solicitada de forma oculta.

```bash
python3 -m venv .venv
.venv/bin/pip install PyMySQL

.venv/bin/python backup_read_monitor.py \
  --host ENDPOINT_DO_BANCO \
  --database NOME_DO_SCHEMA \
  --user USUARIO_DE_LEITURA \
  --threads 100 \
  --interval 2 \
  --query "SELECT id FROM SUA_TABELA LIMIT 1" \
  --log results/backup_read_monitor.csv
```

Inicie o backup somente depois da mensagem `READY`. Pare o monitor com `Ctrl+C` após o backup; o CSV terá uma linha por rodada, com p50, p95, p99, máximo e contagem de erros.
