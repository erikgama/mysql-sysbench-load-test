# Guia do benchmark avançado MySQL

Python coordena a execucao; **Sysbench continua gerando a carga**. Um coletor Python consulta o MySQL em conexoes separadas. O runner simples `sysbench_sql_runner.py` continua disponivel.

## 1. Instalar e baixar o projeto

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv sysbench
```

Em distribuições da família RHEL/Fedora, os pacotes e repositórios disponíveis variam. Com `dnf`, tente:

```bash
sudo dnf install -y git python3 python3-pip sysbench
```

Se o pacote Sysbench não estiver disponível, peça ao administrador para habilitar um repositório aprovado ou instalar um binário com driver MySQL e LuaJIT. Não use um binário sem suporte a MySQL. Python precisa ser **3.10 ou superior**; confira antes de continuar.

Baixe o projeto e prepare o ambiente Python:

```bash
git clone https://github.com/erikgama/mysql-sysbench-load-test.git
cd mysql-sysbench-load-test
python3 --version
sysbench --version
sysbench --help | grep -- '--mysql-host'
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-benchmark.txt
```

O comando `--help` deve mostrar `--mysql-host`. Use Sysbench 1.0+ com MySQL e LuaJIT. Não precisa de MySQL Shell nem do cliente `mysql`. O PyMySQL é usado apenas na coleta. A execução real já foi validada em macOS; consulte os [testes e limites de compatibilidade](VALIDACAO_BENCHMARK.md) para Linux e versões MySQL.

Se você já clonou o projeto, entre na pasta, use `git pull --ff-only` e reinstale os requisitos. Preserve suas alterações locais antes de atualizar.

## 2. Selecionar as queries

Crie `meu_workload.sql` em um editor, com as queries uma após a outra. **Cada query termina com `;`**, mesmo quando ocupa várias linhas. O cliente escolhe seus SELECTs; comentários são aceitos, mas procedures, DELIMITER e comandos de escrita não.

```sql
SELECT id FROM minha_tabela WHERE id = 100;
SELECT COUNT(*) FROM outra_tabela WHERE data >= '2026-10-01';
```

As tabelas acima são ilustrativas: substitua por tabelas e colunas reais. Para um banco AirportDB já populado, existe [`examples/airportdb_individual.sql`](examples/airportdb_individual.sql), com seis SELECTs. O projeto não cria nem popula a base. Confirme que os IDs escolhidos existem; repetir consultas sem linhas pode produzir uma carga pouco representativa.

Cada query vira Q01, Q02 etc. **O teste executa cada query individualmente**, sem sorteio. O mesmo SQL e os mesmos parametros sao repetidos. Os SQLs e parametros concretos ficam em `workload.json`.

O token `{{RAND_INT:id:1:100000}}` tambem e aceito. Seus valores sao gerados **uma unica vez**, antes da carga, com `--seed 42`. `--case-count 3` gera tres casos fixos, testados separadamente. Repetir um token com o mesmo nome no SQL usa o mesmo valor. Funcoes SQL como RAND(), NOW() e UUID() continuam variando no servidor e geram aviso de nao determinismo.

Para selecionar parametros especificos, use `--cases-file examples/cases.json`. Cada caso tem nome e SQL completo; ajuste o exemplo ao seu workload. Cada caso tem warm-up e resultado separados. Repeticoes dos mesmos valores representam alta reutilizacao de dados, nao todo o comportamento de producao.

## 3. Validar sem conectar

```bash
.venv/bin/python mysql_benchmark.py run meu_workload.sql \
  --host teste --database teste --user teste \
  --threads 1 10 100 --events-per-query 1000 --dry-run
```

O plano mostra casos, destinos e total de eventos medidos. `--events-per-query` e o total **por query/caso/concorrencia/rodada**, dividido entre as threads. Precisa ser pelo menos igual a maior concorrencia. Warm-up e adicional e fica fora desses eventos.

## 4. Executar

Comece com uma conexão e uma rodada para validar acesso e coleta:

```bash
.venv/bin/python mysql_benchmark.py run meu_workload.sql \
  --host ENDPOINT --database SCHEMA --user USUARIO \
  --label AMBIENTE --threads 1 --events-per-query 100 \
  --repetitions 1 --engine innodb \
  --output results/validacao_01
```

Substitua ENDPOINT, SCHEMA e USUARIO. A porta padrão é 3306; para outra porta, acrescente `--port PORTA`. Garanta acesso de rede e um usuário autorizado a executar SELECTs. A coleta detalhada pode precisar de acesso a `performance_schema`, `information_schema` e privilégio `PROCESS`; ela registra dados indisponíveis e erros de permissão sem conceder privilégios nem alterar instrumentos automaticamente.

Depois, aumente a concorrência e as repetições para o teste planejado:

```bash
.venv/bin/python mysql_benchmark.py run meu_workload.sql \
  --host ENDPOINT --database SCHEMA --user USUARIO \
  --label OCI --threads 1 10 100 --events-per-query 1000 \
  --repetitions 3 --engine innodb \
  --environment-json examples/environment.json \
  --output results/oci_run_01
```

A senha e solicitada de forma oculta. Opcional: o runner pode ler `MYSQL_PASSWORD`. A senha do Sysbench fica em config temporario privado, removido ao encerrar; nao fica no Lua nem no comando do processo. O runner nao sobrescreve pastas existentes.

Com seis queries, um caso, três concorrências, três rodadas e um motor, esse segundo comando cria **54 cenários e 54.000 execuções medidas**. Só o warm-up exige pelo menos **54 minutos de carga**, além da medição e coleta. Use `--dry-run` para conferir seu plano. Todos os cenários são sequenciais; dentro de cada um, as threads executam em paralelo. `--threads 1 10 100` testa esses três níveis separadamente, não 111 conexões juntas. O coletor abre conexões adicionais.

Copie e ajuste `examples/environment.json` em `environment.local.json` e passe esse arquivo em `--environment-json` para registrar shape, memória, storage e posição da base. Essas informações são declaradas pelo operador, não descobertas ou comprovadas pelo runner.

`--monitor-user USUARIO_COLETA` permite um usuario separado. A senha adicional e lida de `MYSQL_MONITOR_PASSWORD` ou solicitada. A coleta usa SHOW e SELECT; ela nao ativa instrumentos, nao reseta contadores nem carrega tabelas no cluster. O destino do SQL, read-only e timeout sao configuracoes de **sessao**.

TLS e solicitado por padrao. `--ssl-ca CA.pem` verifica o certificado no coletor Python; o suporte/verificacao do Sysbench depende de sua biblioteca cliente. O cipher negociado de cada conexao de carga e registrado nos CSVs `session-*.csv`. `--mysql-ssl off` desativa a solicitacao. Nao confundir TLS solicitado com verificacao comprovada do certificado.

## 5. Escolher onde as queries executam

| Opcao | Configuracao de sessao | Comportamento |
|---|---|---|
| `--engine innodb` | OFF | Todas no motor primario; desabilita RAPID. |
| `--engine heatwave` | FORCED | Todas forçadas no RAPID; falham quando nao podem executar la. |
| `--engine auto` | ON | O otimizador decide por query; retorno ao InnoDB e permitido. |
| `--engine mixed --engine-map mapa.json` | Por query/caso | O usuario define o destino individualmente. |

Pode testar varios modos na mesma chamada: `--engine innodb heatwave auto`. Cada modo gera cenarios separados. Para o mapa:

```json
{
  "Q01": "innodb",
  "Q02": "heatwave",
  "Q03": "auto"
}
```

O mapa deve cobrir todas as queries; uma chave `Q01/C02` pode sobrescrever o destino de um caso. Remova hints `SET_VAR(use_secondary_engine=...)` dos SQLs para que nao contrariem o mapa. O runner os rejeita.

No comando da seção 4, substitua `--engine innodb` por uma das opções da tabela. Para destinos individuais, use `--engine mixed --engine-map engine_map.local.json`. Copie [`examples/engine_map.json`](examples/engine_map.json), ajuste o mapa para suas queries e escolha uma pasta de saída nova a cada execução. `heatwave`, `auto` e os destinos RAPID do mapa exigem que o servidor exponha `use_secondary_engine`; isso não existe em um RDS MySQL comum.

ON nao prova execucao analitica. O relatorio mostra a configuracao solicitada e os contadores PRIMARY/SECONDARY disponiveis. Esses contadores sao por digest na instancia e podem incluir outras sessoes; divergencias de contagem ficam explicitas. FORCED nao faz fallback silencioso. Tabelas RAPID precisam estar preparadas e disponiveis antes do teste; o runner coleta seu estado, mas nao executa ALTER/SECONDARY_LOAD. SQLs sem tabelas podem permanecer no primario mesmo com FORCED: confira a evidencia.

## 6. Warm-up consistente

Antes de **cada query/caso/motor/concorrencia/rodada**, o runner executa o mesmo SQL e avalia janelas consecutivas. Padrao inicial, ajustavel ao workload:

- Pelo menos 60 segundos efetivos de carga e 30 execucoes.
- Janelas de 20 segundos, com pelo menos 10 sucessos por janela.
- Tres janelas consecutivas com amplitude relativa de p50 e QPS ate 15%.
- Para InnoDB, tambem verifica leituras fisicas por execucao (metrica global da instancia).
- Limite de 600 segundos. Uma query em voo ou a coleta pode acrescentar tempo; timeout do cliente limita cada fase.

Amplitude = (maximo - minimo) / max(media, piso). Pisos: 0,01 ms de p50, 0,01 QPS e 1 leitura por query, evitando instabilidade matematica perto de zero. HeatWave/auto nao exigem estabilidade de leituras InnoDB para certificar o aquecimento; essas leituras nao representam o cache do cluster.

Se nao estabilizar, o cenario fica **warmup_not_stabilized** e a medicao nao comeca. `--allow-unstable-warmup` permite uma medicao sinalizada; ela e excluida da comparacao aquecida. `--skip-warmup` serve para diagnostico e tambem nao valida uma comparacao aquecida.

As conexoes sao recriadas entre janelas e na fase medida. O cache do servidor e preservado; nao ha FLUSH/reinicio. Isso aquece o caminho da query, nao garante toda a base na memoria. Os criterios mostram estabilidade observada, nao capacidade ilimitada ou equivalencia perfeita de cache entre ambientes.

## 7. Consultar resultados

Abra `results/oci_run_01/report.html` no navegador. Ele e independente de internet e nao usa CDN. Na pasta:

Para o primeiro comando de validação, o arquivo será `results/validacao_01/report.html`. Se o Linux não tiver interface gráfica, copie a pasta inteira de resultados para sua máquina e abra o HTML: os links para arquivos brutos dependem da estrutura da pasta.

- `run.json`, `summary.csv`, `workload.json`: plano e resultados estruturados.
- `metadata.before.json` e `metadata.after.json`: variaveis globais, consumidores, instrumentos, schema e capacidade informada.
- `scenarios/.../explain.json`: EXPLAIN por caso e destino, fora da medicao.
- `scenarios/.../warmup/window-.../`: evidencias de cada janela.
- `scenarios/.../measured/`: snapshots, deltas, timeline MySQL, log Sysbench, configuracoes das sessoes e amostras individuais completas em CSV e CSV.gz.

Leia nesta ordem:

1. **Status e warm-up:** só use rodadas completas e aquecidas na comparação. `incomplete`, falha SQL ou `warmup_not_stabilized` exigem analisar o log.
2. **Contagem:** confira eventos esperados, amostras coletadas, sucessos e erros de cada cenário.
3. **Latência e vazão:** p50, p95, p99, máximo e execuções/s por query/caso/motor/concorrência/rodada. Poucas amostras não dão um p99 confiável.
4. **Diagnóstico:** compare configurações, planos, deltas, leituras físicas, esperas e temporários. Use o CSV cloud para CPU e IOPS de infraestrutura.

O processo retorna código 0 se a execução terminou completa; uma execução incompleta retorna 1. `--skip-warmup` pode completar sem erros, mas **não** certifica aquecimento. Esse código não substitui a verificação de comparabilidade no relatório.

Latencias sao medidas no cliente ao redor de con:query, incluindo transferencia do resultado conforme o driver. Duracoes usam clock monotonic. Falhas nao entram nos percentis de sucesso. Retries automaticos estao desabilitados: a primeira falha SQL aborta a fase, preservando amostras e indicando incompletude. Cada worker recebe uma quota fixa e as amostras sao verificadas por thread/sequence.

O relatorio mostra latencia por segundo, conexoes, cache, leituras, writes, temporarios, ordenacoes, top waits, I/O por tabela/arquivo e detalhes de statements. Metricas indisponiveis mostram o motivo. Os snapshots completos preservam memoria instrumentada, locks, InnoDB status e thread pool quando acessiveis. Limites de linhas da coleta sao registrados.

**CPU:** CPU_TIME/SUM_CPU_TIME dependem da versao, permissao e do consumer `events_statements_cpu`. O runner consulta a configuracao e mostra indisponivel quando desabilitado. Nao habilita o consumer automaticamente. Tempo elapsed nao e CPU. CPU instrumentada no DB System nao soma o trabalho de todos os nos RAPID. Tamanhos/high-water marks nao sao subtraidos como contadores. Percentis globais do Performance Schema nao sao subtraidos para inventar percentis da rodada.

**Escopo:** contadores MySQL podem incluir carga externa, background e coleta. I/O instrumentado do MySQL e diferente de IOPS dos volumes. A soma dos tempos de statements/esperas pode ultrapassar o tempo de parede por concorrencia. Eventos SQL nao equivalem automaticamente a transacoes de negocio.

## 8. Adicionar metricas de cloud

A primeira versao aceita CSV exportado, sem exigir credenciais AWS/OCI. O formato longo preserva recurso, unidade, periodo e timestamp com timezone:

```csv
timestamp_utc,provider,resource,metric,value,unit,period_seconds
2026-10-14T15:00:00Z,oci,dbsystem,CPUUtilization,25,percent,60
2026-10-14T15:00:00Z,oci,heatwave-node-1,CPUUtilization,40,percent,60
```

Importe depois do teste, utilizando os timestamps da rodada:

```bash
.venv/bin/python mysql_benchmark.py report results/oci_run_01 \
  --cloud-metrics-csv metricas_cloud.csv
```

O HTML agrupa por recurso/metrica/unidade. Nao interpola um minuto como se fosse um segundo, nao soma DB System com cluster e nao inventa CPU onde nao ha amostras. Timestamp cloud e o informado pelo exportador; pontos dentro da janela sao exibidos com seu periodo original. Integracoes diretas com APIs cloud nao estao implementadas nesta versao.

## 9. Comparar ambientes

Use o mesmo arquivo SQL/casos, mesmos parametros, concorrencia, quantidade de eventos, repeticoes e criterios de warm-up. Os dados precisam representar a mesma posicao de base. Preencha `environment.json`; o script nao comprova igualdade dos dados a partir do hash do SQL.

```bash
.venv/bin/python mysql_benchmark.py compare \
  results/aws_run_01 results/oci_run_01 \
  --output results/comparacao_01.html
```

A comparacao verifica hash de workload, perfil e mapa de motores; apresenta medianas das rodadas completas e aquecidas e variaveis globais diferentes. Mediana de p99 de rodadas nao e p99 de amostras combinadas. Cenarios incompletos/nao aquecidos nao entram nas medianas.

Antes de uma POC, faca uma rodada pequena para validar permissoes, instrumentacao e tempo do coletor. Compare a mesma carga com o runner simples/sem diagnostico para quantificar overhead. O benchmark registra coleta_ms; nao presume overhead zero. Em uma unica conexao, o cenario mede repeticao individual; multiplas threads medem concorrencia fechada, nao uma taxa fixa de chegada de producao.

## 10. Cuidados e problemas comuns

| Situação | O que verificar |
|---|---|
| Falha de conexão | Endpoint, porta, rota, firewall, credenciais e TLS. Não aumente carga antes de resolver. |
| Erro TLS no Sysbench | Biblioteca cliente e certificados do binário instalado. Não desative TLS como solução de produção. |
| Pasta de saída já existe | Escolha outro nome em `--output`; o runner preserva os resultados anteriores. |
| Warm-up não estabilizou | Leia as janelas; verifique carga externa, custo da query e se o limite de tempo é suficiente. Não esconda o aviso. |
| CPU ou waits indisponíveis | Versão, privilégios e instrumentação habilitada. Ausência de métrica não significa consumo zero. |
| Query falhou no modo HeatWave | Elegibilidade, tabelas carregadas/atualizadas e erro do servidor. FORCED não troca silenciosamente para InnoDB. |

**Privacidade:** `results/` fica fora do Git, mas contém SQLs completos, parâmetros, host, metadados e mensagens do banco. Trate essa pasta como informação do cliente; revise antes de compartilhar. Arquivos com nomes `environment.local.json`, `cases.local.json` e `engine_map.local.json` também são ignorados. O arquivo SQL personalizado não é ignorado automaticamente: não publique SQLs privados nem senhas. O relatório é inspirado em diagnósticos de banco, não é um Oracle AWR nem reproduz todas as suas métricas.

Para ver todas as opções ou executar os testes automatizados:

```bash
.venv/bin/python mysql_benchmark.py run --help
.venv/bin/python -m unittest discover -s tests -v
```

## Referencias

- [Controle de motor HeatWave](https://dev.mysql.com/doc/heatwave/en/heatwave-system-variables.html)
- [Estado das tabelas RAPID](https://dev.mysql.com/doc/heatwave/en/mys-hw-rpd-tables-table.html)
- [Performance Schema: statements](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-statement-summary-tables.html)
- [Consumers e instrumentacao CPU](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-consumer-filtering.html)
- [Buffer pool](https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool.html)
