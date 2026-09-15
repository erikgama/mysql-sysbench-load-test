# MySQL Sysbench Load Test

Kit genérico para executar testes de carga e estresse em MySQL com Sysbench. O runner lê queries de um arquivo SQL, executa com concorrência configurável e grava TPS, erros, reconexões, p50, p95 e p99.

## Pré-requisitos

| Item | Obrigatório? | Uso |
|---|---|---|
| Python 3 | Sim | Executa o runner `sysbench_sql_runner.py`. |
| Sysbench 1.0+ com suporte a MySQL | Sim | Abre as conexões e executa a carga. |
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
