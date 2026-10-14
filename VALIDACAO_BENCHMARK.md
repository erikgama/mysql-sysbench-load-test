# Validacao do benchmark avancado — 14/10/2026

## Verificacoes automatizadas

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

A CI adicionada executara os testes em Ubuntu/Python 3.10 e 3.12 quando o codigo for publicado. Essa CI ainda nao foi executada no GitHub.

## Verificacoes reais, somente-leitura

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

## Limites desta validacao

- Smoke tests usam poucas amostras e criterios curtos de warm-up (janelas de 1 ou 2 segundos, tolerancia 50%). **Nao sao resultados de performance para comparar clouds**, nem validam dimensionamento ou p99 representativo.
- O consumer `events_statements_cpu` estava desabilitado: CPU por statement ficou indisponivel. Nao foi habilitado automaticamente.
- Nao houve importacao de CPU/storage da cloud nesta rodada. O suporte CSV foi verificado nos testes automatizados; integracao direta com APIs cloud nao existe nesta versao.
- A biblioteca cliente dos dois binarios Sysbench locais falhou com TLS solicitado (erro 2026). A validacao funcional usou `--mysql-ssl off` no banco de desenvolvimento, como nos comandos anteriores. A conexao de carga registrou cipher vazio. O padrao do runner continua `on` e a negociacao precisa ser validada no ambiente do cliente.
- O coletor, o servidor e o gerador possuem escopos diferentes. Contadores globais incluem atividade externa e o proprio monitoramento. Medir o overhead e a resolucao das metricas continua necessario para uma POC de performance.
- Windows nao foi validado. Linux foi preparado no codigo/CI; a execucao real deste registro foi em macOS. A validacao real foi em 26.7; compatibilidade com MySQL 8.0/8.4 depende das capacidades detectadas e ainda requer rodada real nessas versoes.

Os resultados brutos locais ficam em `results/` e sao ignorados pelo Git. Nao ha endpoint, senha nem resultados privados nos exemplos publicos.
