# Validacao do benchmark avancado

## Revisao de correcoes — 15/10/2026

### Codigo e testes automatizados

**33 testes passaram localmente**, incluindo Sysbench real com driver dry-run. A suite verifica tambem:

- Separacao entre modos `heatwave`/`mixed` e entre execucoes com o mesmo rotulo; rejeicao de pastas/run_id duplicados.
- Identidade de execucao, modo e qualidade no CSV.
- Warm-up analitico sem bloqueio por leituras fisicas InnoDB variaveis.
- Barreiras antes/depois da carga, quotas exatas e taxas calculadas no intervalo real dos contadores leves.
- Timeout unico do processo por fase, incluindo preparacao; carga nao e liberada depois do prazo.
- Retorno vazio, quantidade de amostras, duracao minima e contagem servidor/cliente divergente.
- CPU desabilitada e cobertura incompleta explicitamente identificadas.

A [validacao no GitHub Actions](https://github.com/erikgama/mysql-sysbench-load-test/actions/runs/38800977727) passou no commit de codigo `42e6ff7`: **33 testes em Ubuntu/Python 3.10 e 33 em Ubuntu/Python 3.12**, incluindo os dois testes com Sysbench real em dry-run. A CI nao acessa o banco de desenvolvimento.

**Complemento de apresentacao (15/10):** `SHOW GLOBAL VARIABLES` passou a aparecer em tabela aberta no final do HTML, com valores inicial/final, alteracoes, horarios e atalho no topo. Os **36 testes locais** passaram, incluindo tres novos testes de completude (mais de 1.000 variaveis), escaping e coleta ausente. No relatorio real, as **1.001 variaveis** e seus dois valores foram conferidos contra os snapshots, sem truncamento. Apenas o HTML foi regenerado; nao houve nova carga nem alteracao dos dados brutos.

### Validacao conectada, somente SELECT

| Verificacao | Resultado observado |
|---|---|
| Quatro modos, 40 eventos por modo | 160 sucessos, zero erros; InnoDB/auto: 40 PRIMARY cada; HeatWave/mixed: 40 SECONDARY cada. |
| Seis queries, 4 conexoes, 40 eventos/query | 240 sucessos, zero erros e nenhum retorno vazio; contagens medidas por digest coincidiram. |
| Resultado vazio intencional | SQL completou, mas o benchmark retornou 1 e registrou `empty_result`, como esperado. |
| CPU obrigatoria com consumer desabilitado | Retorno 1 antes da carga, zero cenarios executados. Nenhuma instrumentacao global alterada. |
| Warm-up padrao + 1.000 eventos, 4 conexoes | 60,378 s efetivos de aquecimento e 1.512 execucoes de warm-up. Depois, 1.000/1.000 sucessos medidos em 39,666 s, zero erros. |
| Smoke final apos ajuste de timeout | Mais 5/5 sucessos, uma conexao, sem warm-up; apenas validacao funcional. |

**Ressalva da rodada de 1.000 eventos:** o cliente registrou 1.000 sucessos/linhas retornadas e o delta de `SUM_ROWS_SENT` tambem foi 1.000, mas `COUNT_STAR` aumentou 992. Os snapshots leves e detalhados preservaram essa divergencia. A causa nao foi determinada; nao e prova de perda de queries. O cenario ficou corretamente fora da comparacao com `server_client_count_conflict`. Houve divergencias por digest tambem em nove janelas de warm-up; os registros originais foram mantidos.

Uma auditoria independente recalculou os percentis p50/p95/p99, media, QPS, quotas por thread, linhas retornadas e intervalos a partir de **4.002 amostras em 45 fases**, incluindo warm-up e o caso vazio intencional. Foram conferidos os CSVs originais contra o arquivo comprimido, os links do HTML, a identidade do CSV e as divergencias de servidor. O smoke final de cinco eventos e adicional a essa contagem.

O intervalo dos contadores agora e separado da preparacao: em um cenario, carga de **6,203 s**, contadores leves de **6,528 s** e snapshots detalhados de **9,874 s**. Na rodada de 1.000 eventos, foram **39,666 / 39,990 / 43,505 s**, respectivamente. O intervalo leve ainda inclui round trips do coletor; contadores de instancia nao representam exclusivamente o workload.

### Limites que permanecem explicitos

- Rodadas curtas dos quatro modos/seis queries sao testes funcionais e ficam fora das medianas pelos limites padrao de amostras/duracao. A rodada de 1.000 eventos tambem foi excluida, pela divergencia por digest. **Esta validacao nao e um resultado comparativo entre clouds.**
- CPU por statement estava indisponivel, havia instrumentos de sincronizacao desabilitados e nao foi importado CSV cloud. O relatorio mostra essas lacunas; nao inventa valores nem habilita instrumentacao global.
- Os numeros do `sysbench.log` permanecem brutos. QPS/percentis de referencia estao no HTML, `client_summary.json` e `workload_summary.txt`; p99 nativo zero nao foi tratado como latencia zero.
- A instalacao local padrao apresentou erro TLS 2026. A validacao conectada usou o binario local previamente funcional e TLS desligado no ambiente de desenvolvimento autorizado. TLS continua ligado por padrao no runner; a falha dessa biblioteca local nao foi corrigida nesta revisao.
- Resultados e dados privados continuam apenas em `results/`, ignorados pelo Git. Os exemplos publicos nao incluem endpoint nem senha.

## Historico — 14/10/2026

### Verificacoes automatizadas

23 testes passaram localmente, incluindo:

- Geracao reproduzivel de casos e rejeicao de comandos de escrita/hints que contrariem o destino.
- Percentis, separacao de erros, deteccao de amostras faltantes/duplicadas e preservacao de contadores inteiros grandes.
- Deltas de contadores e deteccao de reset; CPU indisponivel nao vira zero.
- Warm-up por estabilidade com minimo efetivo de tempo sob carga.
- Mapa por query/caso, motor analitico indisponivel sem fallback silencioso.
- CSV cloud com timezone/periodo, comparacao sem incluir rodadas nao aquecidas, HTML escapado.
- **Sysbench real com driver dry-run:** 101 eventos distribuidos exatamente entre 7 threads, sem acessar banco.

```bash
python3 -m unittest discover -s tests -v
```

A [validação no GitHub Actions](https://github.com/erikgama/mysql-sysbench-load-test/actions/runs/38647938321) passou em Ubuntu com Python **3.10 e 3.12**, no commit `eb64c04`: 23 testes em cada versão, incluindo Sysbench real com driver dry-run. Essa CI não conecta a um banco real.

### Verificacoes reais, somente-leitura

AirportDB em MySQL `26.7.0-cloud`, no ambiente autorizado pelo usuario. Nao houve ALTER, carga de tabelas, reset de contadores nem ativacao de instrumentacao.

- Seis queries pontuais, com 1 e 4 conexoes, 20 eventos por cenario: **11 de 12 cenarios medidos completos**, totalizando 220 sucessos medidos. Um cenario nao atingiu estabilidade no warm-up curto e foi suspenso; o teste inteiro ficou corretamente `incomplete`.
- Uma agregacao pequena em quatro modos: **40 sucessos medidos**, 10 em cada modo, zero erros, com warm-up estabilizado nos criterios reduzidos da validacao funcional.

| Modo | Configuracao | PRIMARY observado | SECONDARY observado |
|---|---|---:|---:|
| innodb | OFF | 10 | 0 |
| heatwave | FORCED | 0 | 10 |
| auto | ON | 10 | 0 |
| mixed, Q01=heatwave | FORCED | 0 | 10 |

Os deltas de COUNT_STAR coincidiram com os eventos do cliente. Sao evidencias por digest da instancia e nao um mecanismo de atribuicao exclusivo para qualquer banco com carga externa. O modo automatico escolheu InnoDB **nesta consulta**, nao como regra para outras consultas.

Uma verificacao adicional no RAPID confirmou captura de conexao, duracao monotonic e linhas retornadas no cliente. Um SELECT intencional em tabela inexistente confirmou captura de errno 1146/SQLSTATE 42S02, zero sucessos, sem retries e resultado incompleto. O HTML foi aberto e conferido no navegador local.

### Limites desta validacao

- Smoke tests usam poucas amostras e criterios curtos de warm-up (janelas de 1 ou 2 segundos, tolerancia 50%). **Nao sao resultados de performance para comparar clouds**, nem validam dimensionamento ou p99 representativo.
- O consumer `events_statements_cpu` estava desabilitado: CPU por statement ficou indisponivel. Nao foi habilitado automaticamente.
- Nao houve importacao de CPU/storage da cloud nesta rodada. O suporte CSV foi verificado nos testes automatizados; integracao direta com APIs cloud nao existe nesta versao.
- A biblioteca cliente dos dois binarios Sysbench locais falhou com TLS solicitado (erro 2026). A validacao funcional usou `--mysql-ssl off` no banco de desenvolvimento, como nos comandos anteriores. A conexao de carga registrou cipher vazio. O padrao do runner continua `on` e a negociacao precisa ser validada no ambiente do cliente.
- O coletor, o servidor e o gerador possuem escopos diferentes. Contadores globais incluem atividade externa e o proprio monitoramento. Medir o overhead e a resolucao das metricas continua necessario para uma POC de performance.
- Windows não foi validado. Os testes automatizados passaram em Linux/Ubuntu; a execução conectada a um banco real deste registro foi em macOS. A validação real foi em 26.7; compatibilidade com MySQL 8.0/8.4 depende das capacidades detectadas e ainda requer rodada real nessas versões.

Os resultados brutos locais ficam em `results/` e sao ignorados pelo Git. Nao ha endpoint, senha nem resultados privados nos exemplos publicos.
