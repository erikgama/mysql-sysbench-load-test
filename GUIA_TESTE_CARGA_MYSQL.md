# Guia rápido — teste de carga MySQL com Sysbench

Este guia executa as queries do seu arquivo SQL com 100 conexões simultâneas e 10.000 execuções totais. Para comparar dois ambientes, use a mesma máquina de carga, o mesmo arquivo SQL, o mesmo número de threads e o mesmo número de eventos.

## 1. Preparar uma máquina Linux

Use uma VM Linux com acesso de rede ao banco. Ela precisa de Python 3 e Sysbench 1.0 ou superior.

**Ubuntu/Debian**

```bash
sudo apt update
sudo apt install -y python3 sysbench
```

**Distribuições com `dnf`**

```bash
sudo dnf install -y python3 sysbench
```

Valide:

```bash
python3 --version
sysbench --version
```

## 2. Copiar e adaptar o workload

Copie esta pasta para a VM. Duplique o template e adapte as queries ao schema que será testado:

```bash
cp queries_template.sql meu_workload.sql
```

Edite `meu_workload.sql`: cada query deve terminar com `;`. Para gerar um inteiro aleatório sem perder o uso de índice, utilize:

```sql
WHERE id = {{RAND_INT:id:1:1000000}};
```

## 3. Validar o arquivo

Este passo não acessa o banco:

```bash
python3 sysbench_sql_runner.py meu_workload.sql \
  --host teste --database teste --user teste --dry-run
```

Confirme a quantidade de queries carregadas.

## 4. Executar a rodada

Substitua os valores em maiúsculo. A senha será solicitada de forma oculta.

```bash
python3 sysbench_sql_runner.py meu_workload.sql \
  --host ENDPOINT_DO_BANCO \
  --database NOME_DO_SCHEMA \
  --user USUARIO_DE_BENCHMARK \
  --threads 100 \
  --events 10000 \
  --query-mode round-robin \
  --mysql-ssl on \
  --output results/ambiente_run_01.txt
```

- `--threads 100`: 100 conexões concorrentes.
- `--events 10000`: 10.000 execuções no total.
- `--query-mode round-robin`: alterna as queries na ordem do arquivo.

Mantenha `--mysql-ssl on` quando o endpoint usar certificado TLS confiável.

## 5. Validar o resultado

No final do arquivo em `results/`, confirme:

```text
# Samples collected: 10000 of 10000 expected events
overall p95 (ms): ...
overall p99 (ms): ...
ignored errors: 0
reconnects: 0
```

Se houver `FATAL`, `WARNING: INCOMPLETE RUN`, erros ou reconexões, descarte a rodada.

## 6. Comparar ambientes

1. Faça uma rodada de aquecimento e descarte o resultado.
2. Execute cinco rodadas em cada ambiente.
3. Compare TPS, p95, p99, máximo, erros e reconexões.
4. Use a mediana das cinco rodadas para a conclusão.

| Ambiente | Rodada | TPS | p95 ms | p99 ms | Máx. ms | Erros | Reconexões |
|---|---:|---:|---:|---:|---:|---:|---:|
| Ambiente A | 1 |  |  |  |  | 0 | 0 |
| Ambiente B | 1 |  |  |  |  | 0 | 0 |

Os percentis a utilizar são `overall p95` e `overall p99` da seção `Client-side latency percentiles` gerada pelo runner.
